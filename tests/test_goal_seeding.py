"""Native `/goal` seeding from task acceptance criteria (v1).

At task dispatch, a task's ``acceptance_criteria`` are translated into a
native ``/goal <condition>`` line injected into the worker's PTY — but
only on providers whose CLI has native ``/goal`` (Claude Code, Codex).
The provider owns the keep-working loop; Swarm builds no evaluator.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from swarm.drones.log import SystemAction
from swarm.providers import get_provider
from swarm.server.messages import render_goal_condition
from swarm.worker.worker import Worker
from tests.conftest import make_daemon
from tests.fakes.process import FakeWorkerProcess

# --- provider capability -----------------------------------------------


def test_claude_and_codex_support_native_goal():
    assert get_provider("claude").supports_native_goal is True
    assert get_provider("codex").supports_native_goal is True


def test_gemini_and_opencode_do_not_support_native_goal():
    # Inherit the base default (False) — v1 is a clean no-op there.
    assert get_provider("gemini").supports_native_goal is False
    assert get_provider("opencode").supports_native_goal is False


# --- render_goal_condition ---------------------------------------------


def test_empty_criteria_renders_empty_string():
    assert render_goal_condition([], max_turns=25) == ""


def test_renders_each_criterion_and_proof_directive_and_bound():
    out = render_goal_condition(["uv run pytest exits 0", "git status is clean"], max_turns=25)
    assert "uv run pytest exits 0" in out
    assert "git status is clean" in out
    # Evaluator only sees the transcript — condition must ask for proof.
    assert "demonstrated in your" in out.lower()
    # Docs-recommended runaway bound, parameterised.
    assert "stop after 25 turns" in out


def test_condition_is_one_line():
    out = render_goal_condition(["a", "b\nstill b", "c"], max_turns=10)
    assert "\n" not in out


def test_condition_truncated_to_4000_chars():
    huge = ["x" * 5000, "y" * 5000]
    out = render_goal_condition(huge, max_turns=10)
    assert len(out) <= 4000


# --- start_task injection ----------------------------------------------


@pytest.fixture
def daemon(monkeypatch):
    workers = [
        Worker(name="api", path="/tmp/api", process=FakeWorkerProcess(name="api")),
        Worker(
            name="gem",
            path="/tmp/gem",
            process=FakeWorkerProcess(name="gem"),
            provider_name="gemini",
        ),
    ]
    return make_daemon(monkeypatch, workers=workers)


def _assigned_task(daemon, worker, *, criteria):
    task = daemon.create_task(title="Goal task", description="do it")
    daemon.task_board.get(task.id).acceptance_criteria = list(criteria)
    return task


def _goal_sends(mock_send):
    return [
        c.args[1]
        for c in mock_send.call_args_list
        if len(c.args) > 1 and str(c.args[1]).startswith("/goal ")
    ]


async def test_goal_injected_for_capable_provider_with_criteria(daemon):
    task = _assigned_task(daemon, "api", criteria=["tests pass", "lint clean"])
    await daemon.assign_task(task.id, "api")
    with patch.object(daemon, "send_to_worker", new_callable=AsyncMock) as mock_send:
        await daemon.start_task(task.id)
    goals = _goal_sends(mock_send)
    assert len(goals) == 1
    assert "tests pass" in goals[0] and "lint clean" in goals[0]
    assert SystemAction.GOAL_SET in [e.action for e in daemon.drone_log.entries]


async def test_no_goal_when_task_has_no_criteria(daemon):
    task = _assigned_task(daemon, "api", criteria=[])
    await daemon.assign_task(task.id, "api")
    with patch.object(daemon, "send_to_worker", new_callable=AsyncMock) as mock_send:
        await daemon.start_task(task.id)
    assert _goal_sends(mock_send) == []
    assert SystemAction.GOAL_SET not in [e.action for e in daemon.drone_log.entries]


async def test_no_goal_for_incapable_provider(daemon):
    task = _assigned_task(daemon, "gem", criteria=["tests pass"])
    await daemon.assign_task(task.id, "gem")
    with patch.object(daemon, "send_to_worker", new_callable=AsyncMock) as mock_send:
        await daemon.start_task(task.id)
    assert _goal_sends(mock_send) == []


async def test_no_goal_when_flag_disabled(daemon):
    daemon.config.drones.native_goal_enabled = False
    task = _assigned_task(daemon, "api", criteria=["tests pass"])
    await daemon.assign_task(task.id, "api")
    with patch.object(daemon, "send_to_worker", new_callable=AsyncMock) as mock_send:
        await daemon.start_task(task.id)
    assert _goal_sends(mock_send) == []


async def test_goal_uses_configured_max_turns(daemon):
    daemon.config.drones.native_goal_max_turns = 7
    task = _assigned_task(daemon, "api", criteria=["tests pass"])
    await daemon.assign_task(task.id, "api")
    with patch.object(daemon, "send_to_worker", new_callable=AsyncMock) as mock_send:
        await daemon.start_task(task.id)
    goals = _goal_sends(mock_send)
    assert len(goals) == 1
    assert "stop after 7 turns" in goals[0]


# --- task #524: cross-project from-worker dispatch -----------------------
#
# The bug: ``_maybe_seed_goal`` did not consult the task's source_worker /
# target_worker fields. When a cross-project task somehow landed on the
# from-worker (the requester) rather than the to-worker (the implementer),
# the to-worker's acceptance criteria got seeded as a ``/goal`` on the
# from-worker — which could not satisfy them (different repo, different
# code paths). The Stop-hook then looped indefinitely, burning tokens.
# Concrete repro: cross-project task #523 (from=rcg-networks → to=platform)
# burned ~$10 / 257K output tokens before the operator reassigned.


def _cross_project_task(daemon, *, source: str, target: str, criteria: list[str]):
    """Build a task with cross-project from→to attribution + criteria."""
    task = daemon.create_task(title="x-project goal task", description="do it elsewhere")
    t = daemon.task_board.get(task.id)
    t.acceptance_criteria = list(criteria)
    t.source_worker = source
    t.target_worker = target
    t.is_cross_project = True
    return task


async def test_goal_skipped_when_dispatch_lands_on_cross_project_from_worker(daemon):
    """Bug repro: if the dispatch ends up on the FROM-worker of a
    cross-project task, the to-worker's criteria must NOT be seeded as
    /goal on the from-worker (which can't satisfy them)."""
    # Cross-project shape: from=api (the requester), to=other-worker.
    # Then assign + start on `api` — the bug condition.
    task = _cross_project_task(
        daemon,
        source="api",
        target="some-other-repo",
        criteria=["other-repo's tests pass", "other-repo migration deployed"],
    )
    await daemon.assign_task(task.id, "api")
    with patch.object(daemon, "send_to_worker", new_callable=AsyncMock) as mock_send:
        await daemon.start_task(task.id)
    # No /goal sent — the guard suppressed it.
    assert _goal_sends(mock_send) == []
    # GOAL_SET was NOT emitted; GOAL_SKIPPED WAS, naming the cross-project context.
    actions = [e.action for e in daemon.drone_log.entries]
    assert SystemAction.GOAL_SET not in actions
    assert SystemAction.GOAL_SKIPPED in actions


async def test_goal_still_seeded_when_dispatch_lands_on_cross_project_target(daemon):
    """The happy cross-project path: dispatch lands on the to-worker
    (the implementer). /goal must still be seeded there as before."""
    # Cross-project task with from=other-worker → to=api. Dispatch on
    # `api` (the to-worker) — the intended case. The guard MUST NOT
    # fire here; this is the legitimate cross-project dispatch.
    task = _cross_project_task(
        daemon,
        source="other-requester",
        target="api",
        criteria=["api tests pass", "api lint clean"],
    )
    await daemon.assign_task(task.id, "api")
    with patch.object(daemon, "send_to_worker", new_callable=AsyncMock) as mock_send:
        await daemon.start_task(task.id)
    goals = _goal_sends(mock_send)
    assert len(goals) == 1
    assert "api tests pass" in goals[0]
    actions = [e.action for e in daemon.drone_log.entries]
    assert SystemAction.GOAL_SET in actions
    assert SystemAction.GOAL_SKIPPED not in actions
