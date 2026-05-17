"""#406: swarm_park_task — a worker hands its own ACTIVE task back to
ASSIGNED with a reason, no blocker binding. Composes with #405 INV-1/2/3
immediately (no reconciler/reload).
"""

from __future__ import annotations

import pytest

from swarm.drones.log import SystemAction
from swarm.mcp.tools import _handle_park_task
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import TaskStatus
from tests.conftest import make_daemon


@pytest.fixture
def board():
    return TaskBoard()


@pytest.fixture
def daemon(monkeypatch):
    return make_daemon(monkeypatch)


def _active(board, title, worker):
    t = board.create(title=title)
    board.assign(t.id, worker)
    board.activate(t.id)
    return board.get(t.id)


# --- TaskBoard.park -----------------------------------------------------


def test_park_own_active_task_to_assigned(board):
    t = _active(board, "x", "api")
    assert board.park(t.id, "api", "operator preempt") is True
    got = board.get(t.id)
    assert got.status == TaskStatus.ASSIGNED
    assert got.assigned_worker == "api"  # still owns it — just set down
    # INV-3: worker now has no ACTIVE task (immediately, no reconciler).
    assert board.current_task_for_worker("api") is None


def test_park_rejects_non_owner(board):
    t = _active(board, "x", "api")
    assert board.park(t.id, "web", "not mine") is False
    assert board.get(t.id).status == TaskStatus.ACTIVE  # untouched


def test_park_rejects_non_active(board):
    t = board.create(title="x")
    board.assign(t.id, "api")  # ASSIGNED, not ACTIVE
    assert board.park(t.id, "api", "r") is False
    assert board.park("missing-id", "api", "r") is False


# --- swarm_park_task MCP handler ---------------------------------------


def test_handler_parks_callers_active_task_with_buzz(daemon):
    t = daemon.task_board.create(title="initiative")
    daemon.task_board.assign(t.id, "swarm")
    daemon.task_board.activate(t.id)

    out = _handle_park_task(daemon, "swarm", {"reason": "operator preempt to #405"})

    assert "parked" in out[0]["text"].lower()
    assert daemon.task_board.get(t.id).status == TaskStatus.ASSIGNED
    assert SystemAction.TASK_PARKED in [e.action for e in daemon.drone_log.entries]


def test_handler_requires_reason(daemon):
    t = daemon.task_board.create(title="x")
    daemon.task_board.assign(t.id, "swarm")
    daemon.task_board.activate(t.id)
    out = _handle_park_task(daemon, "swarm", {})
    assert "reason" in out[0]["text"].lower()
    assert daemon.task_board.get(t.id).status == TaskStatus.ACTIVE  # unchanged


def test_handler_no_active_task(daemon):
    out = _handle_park_task(daemon, "swarm", {"reason": "nothing to park"})
    assert "no active task" in out[0]["text"].lower()


def test_park_is_not_a_blocker(daemon):
    """Distinct from swarm_report_blocker — parking creates NO binding."""
    t = daemon.task_board.create(title="x")
    daemon.task_board.assign(t.id, "swarm")
    daemon.task_board.activate(t.id)
    _handle_park_task(daemon, "swarm", {"reason": "scope change"})
    store = getattr(daemon, "blocker_store", None)
    if store is not None:
        assert store.list_for_worker("swarm") == []  # no blocker binding


def test_preempt_scenario_board_immediately_truthful(daemon):
    """Worker parks mid-initiative; board is truthful with no reload."""
    a = daemon.task_board.create(title="phase work")
    daemon.task_board.assign(a.id, "swarm")
    daemon.task_board.activate(a.id)
    assert daemon.task_board.current_task_for_worker("swarm").id == a.id

    _handle_park_task(daemon, "swarm", {"reason": "operator STOP — pivot"})

    # No reconciler, no daemon reload — immediately coherent:
    assert daemon.task_board.current_task_for_worker("swarm") is None
    assert daemon.task_board.get(a.id).status == TaskStatus.ASSIGNED
    active = [
        t for t in daemon.task_board.tasks_for_worker("swarm") if t.status == TaskStatus.ACTIVE
    ]
    assert active == []  # INV-1/2/3 satisfied


# --- #407: ambiguity refusal + explicit task_number --------------------
#
# Pre-#405-reload / un-reconciled boards can hold >1 ACTIVE task for one
# worker (activate() enforces INV-1, but corrupt state set directly does
# not). The 2026-05-17 public-website incident: worker owned 4 tasks,
# intended to park one, the no-arg tool silently parked an arbitrary
# (genuinely-blocked) other one. swarm_park_task must take an explicit
# task_number and refuse to guess when the no-arg case is ambiguous.


def _force_active(board, title, worker):
    """Set a task ACTIVE *without* activate()'s INV-1 demotion — i.e.
    reproduce the un-reconciled multi-active state from the incident."""
    t = board.create(title=title)
    board.assign(t.id, worker)
    obj = board.get(t.id)
    obj.status = TaskStatus.ACTIVE
    return obj


def test_explicit_task_number_parks_the_right_one_among_several(daemon):
    b = daemon.task_board
    keep = _force_active(b, "keep running", "swarm")
    target = _force_active(b, "set this down", "swarm")

    out = _handle_park_task(
        daemon, "swarm", {"reason": "execution exhausted", "task_number": target.number}
    )

    assert "parked" in out[0]["text"].lower()
    assert f"#{target.number}" in out[0]["text"]
    assert b.get(target.id).status == TaskStatus.ASSIGNED  # the intended one
    assert b.get(keep.id).status == TaskStatus.ACTIVE  # the other one untouched


def test_omitted_with_multiple_active_refuses_and_lists_candidates(daemon):
    b = daemon.task_board
    one = _force_active(b, "a", "swarm")
    two = _force_active(b, "b", "swarm")

    out = _handle_park_task(daemon, "swarm", {"reason": "pivot"})
    text = out[0]["text"]

    # Refuses, names every candidate, mutates nothing.
    assert "parked" not in text.lower()
    assert f"#{one.number}" in text and f"#{two.number}" in text
    assert "task_number" in text
    assert b.get(one.id).status == TaskStatus.ACTIVE
    assert b.get(two.id).status == TaskStatus.ACTIVE
    assert SystemAction.TASK_PARKED not in [e.action for e in daemon.drone_log.entries]


def test_omitted_with_single_active_still_works(daemon):
    """Back-compat: the common #406 case (one active task) is unchanged."""
    t = _force_active(daemon.task_board, "solo", "swarm")
    out = _handle_park_task(daemon, "swarm", {"reason": "operator preempt"})
    assert "parked" in out[0]["text"].lower()
    assert daemon.task_board.get(t.id).status == TaskStatus.ASSIGNED


def test_explicit_task_not_owned_by_caller_rejected_no_mutation(daemon):
    b = daemon.task_board
    mine = _force_active(b, "mine", "swarm")
    theirs = _force_active(b, "theirs", "web")

    out = _handle_park_task(daemon, "swarm", {"reason": "oops", "task_number": theirs.number})

    assert "not assigned to you" in out[0]["text"].lower()
    assert b.get(theirs.id).status == TaskStatus.ACTIVE  # untouched
    assert b.get(mine.id).status == TaskStatus.ACTIVE  # mine untouched too


def test_explicit_task_not_active_rejected_no_mutation(daemon):
    b = daemon.task_board
    t = b.create(title="assigned only")
    b.assign(t.id, "swarm")  # ASSIGNED, never activated

    out = _handle_park_task(daemon, "swarm", {"reason": "x", "task_number": t.number})

    assert "not active" in out[0]["text"].lower()
    assert b.get(t.id).status == TaskStatus.ASSIGNED  # unchanged (was already)
    assert SystemAction.TASK_PARKED not in [e.action for e in daemon.drone_log.entries]


def test_public_website_incident_shape(daemon):
    """Faithful repro: worker owns 4 tasks (a couple ACTIVE incl. a
    genuinely-blocked one), intends a specific one. Parking that one must
    NOT touch the blocked task — the exact skew #405/#407 end."""
    b = daemon.task_board
    # #393-shaped: ACTIVE but genuinely blocked behind another — must NOT move.
    blocked = _force_active(b, "393 blocked behind 394", "public-website")
    # #394-shaped: the upstream, assigned.
    upstream = b.create(title="394 upstream")
    b.assign(upstream.id, "public-website")
    # #398-shaped: another owned, assigned.
    other = b.create(title="398 other")
    b.assign(other.id, "public-website")
    # #399-shaped: ACTIVE, execution exhausted — the one to set down.
    intended = _force_active(b, "399 npm visibility", "public-website")

    out = _handle_park_task(
        daemon,
        "public-website",
        {"reason": "execution exhausted", "task_number": intended.number},
    )

    assert "parked" in out[0]["text"].lower()
    assert b.get(intended.id).status == TaskStatus.ASSIGNED  # the right one
    assert b.get(blocked.id).status == TaskStatus.ACTIVE  # #393 untouched
    assert b.get(upstream.id).status == TaskStatus.ASSIGNED  # unchanged
    assert b.get(other.id).status == TaskStatus.ASSIGNED  # unchanged


def test_invalid_task_number_rejected_no_mutation(daemon):
    t = _force_active(daemon.task_board, "x", "swarm")
    out = _handle_park_task(daemon, "swarm", {"reason": "r", "task_number": "not-a-number"})
    assert "task_number" in out[0]["text"].lower()
    assert daemon.task_board.get(t.id).status == TaskStatus.ACTIVE  # untouched
