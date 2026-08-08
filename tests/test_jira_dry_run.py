"""Enabling Jira must not be a bulk write to someone else's tracker (v2 phase 3).

WHAT HAPPENED, 2026-08-07. A schema migration added ``jira_exported_status`` with an
empty default, which made all 25 linked tasks read as "never acknowledged". The
reconciler ran on its own five-minute schedule and transitioned **14 real WWD tickets**
before anyone had looked at it. Nothing was broken by that — those tickets were already
done — but the blast radius of a settings toggle was other people's tickets.

THE DISTINCTION THIS DRAWS, and it is the whole design:

* an INDIVIDUAL export is the direct consequence of an action someone just took — a
  worker closed a task, the operator moved it. Gating those would break a working
  integration on upgrade, and they are not the dangerous path.
* the RECONCILE SWEEP is a bulk convergence that runs unattended on a timer and can
  move many tickets at once. That is what needs an explicit go-ahead.

So the sweep refuses to write for a project whose workflow the operator has not
confirmed, says so, and ``plan_exports`` shows exactly what it would have done.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm.config.models import JiraConfig
from swarm.db.core import SwarmDB
from swarm.db.task_store import SqliteTaskStore
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask, TaskStatus


@pytest.fixture
def db(tmp_path: Path) -> SwarmDB:
    return SwarmDB(tmp_path / "swarm.db")


@pytest.fixture
def board(db: SwarmDB) -> TaskBoard:
    return TaskBoard(store=SqliteTaskStore(db))


class _FakeJira:
    enabled = True

    def __init__(self, cfg: JiraConfig, *, accept: bool = True):
        self._config = cfg
        self.accept = accept
        self.exported: list[tuple[str, str]] = []

    async def export_status(self, task, new_status) -> bool:
        self.exported.append((task.jira_key, new_status.value))
        return self.accept


def _service(board: TaskBoard, jira: _FakeJira):
    from swarm.server.jira_service import JiraService

    svc = JiraService.__new__(JiraService)
    svc._task_board = board
    svc._get_jira = lambda: jira
    svc._drone_log = None
    svc._broadcast_ws = lambda _p: None
    svc._track_task = lambda _t: None
    return svc


def _linked(board: TaskBoard, key: str, worker: str = "api") -> SwarmTask:
    """One linked, ACTIVE task.

    The worker is a parameter because INV-1 allows one ACTIVE task per worker:
    activating a second task for the same worker DEMOTES the first to ASSIGNED. An
    earlier version of this file used one worker for both tasks and then asserted the
    first was still 'active' — the invariant was working correctly and the test was
    wrong.
    """
    task = board.add(SwarmTask(title=f"work {key}", description=""))
    board.set_jira_key(task.id, key)
    board.assign(task.id, worker)
    board.activate(task.id)
    return task


# --- the dry run -------------------------------------------------------------


def test_the_plan_reports_what_would_change_without_touching_jira(board: TaskBoard):
    cfg = JiraConfig(enabled=True, projects=["WWD"])
    jira = _FakeJira(cfg)
    _linked(board, "WWD-1")

    plan = _service(board, jira).plan_exports()

    assert len(plan) == 1, f"the outstanding task is not in the plan: {plan}"
    assert plan[0]["jira_key"] == "WWD-1"
    assert plan[0]["would_become"] == "active"
    assert plan[0]["acknowledged"] is None, "a never-exported task should say so"
    assert jira.exported == [], "planning wrote to Jira — it must be a pure read"


def test_the_plan_is_empty_when_everything_is_acknowledged(board: TaskBoard):
    cfg = JiraConfig(enabled=True, projects=["WWD"])
    task = _linked(board, "WWD-2")
    board.record_jira_export(task.id, task.status.value)
    assert _service(board, _FakeJira(cfg)).plan_exports() == []


def test_the_plan_marks_which_projects_are_unconfirmed(board: TaskBoard):
    """The operator needs to see WHY nothing will move, not just that nothing did."""
    cfg = JiraConfig(enabled=True, projects=["WWD", "IS"], confirmed_projects=["WWD"])
    _linked(board, "WWD-3", worker="api")
    _linked(board, "IS-4", worker="web")

    plan = {p["jira_key"]: p for p in _service(board, _FakeJira(cfg)).plan_exports()}
    assert plan["WWD-3"]["project_confirmed"] is True
    assert plan["IS-4"]["project_confirmed"] is False


# --- the gate ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_sweep_does_not_write_for_an_unconfirmed_project(board: TaskBoard):
    """THE PROPERTY. This is the 14-real-tickets case."""
    cfg = JiraConfig(enabled=True, projects=["WWD"])  # nothing confirmed
    jira = _FakeJira(cfg)
    _linked(board, "WWD-5")

    written = await _service(board, jira).reconcile_exports()

    assert written == 0, "the sweep converged an unconfirmed project"
    assert jira.exported == [], (
        f"enabling the integration transitioned real tickets with no go-ahead: {jira.exported}"
    )


@pytest.mark.asyncio
async def test_the_sweep_writes_once_the_project_is_confirmed(board: TaskBoard):
    """The gate must be a gate, not a wall — confirming has to actually let it through,
    or the integration is permanently inert."""
    cfg = JiraConfig(enabled=True, projects=["WWD"], confirmed_projects=["WWD"])
    jira = _FakeJira(cfg)
    _linked(board, "WWD-6")

    assert await _service(board, jira).reconcile_exports() == 1
    assert jira.exported == [("WWD-6", "active")]


@pytest.mark.asyncio
async def test_confirmation_is_per_project(board: TaskBoard):
    """Confirming WWD must not silently authorise IS — that is the blast radius the
    whole phase exists to contain."""
    cfg = JiraConfig(enabled=True, projects=["WWD", "IS"], confirmed_projects=["WWD"])
    jira = _FakeJira(cfg)
    _linked(board, "WWD-7", worker="api")
    _linked(board, "IS-8", worker="web")

    await _service(board, jira).reconcile_exports()

    assert ("WWD-7", "active") in jira.exported
    assert not any(k.startswith("IS-") for k, _ in jira.exported), (
        f"an unconfirmed project was converged anyway: {jira.exported}"
    )


@pytest.mark.asyncio
async def test_an_individual_export_is_NOT_gated(board: TaskBoard):
    """Deliberate asymmetry. An export caused by a real task transition is a direct
    consequence of something a person or worker just did; gating it would break a
    working integration on upgrade. Only the unattended batch needs a go-ahead."""
    cfg = JiraConfig(enabled=True, projects=["WWD"])  # unconfirmed
    jira = _FakeJira(cfg)
    task = _linked(board, "WWD-9")

    ok = await _service(board, jira).export_status(task.id, TaskStatus.DONE)

    assert ok is True, "a direct export was blocked by the bulk-convergence gate"
    assert jira.exported == [("WWD-9", "done")]


@pytest.mark.asyncio
async def test_a_config_without_confirmation_support_is_not_gated(board: TaskBoard):
    """Upgrade safety: a pre-v2 config object has no is_confirmed, and treating that as
    'unconfirmed' would silently stop a working integration."""

    class _OldConfig:
        pass

    jira = _FakeJira(JiraConfig(enabled=True))
    jira._config = _OldConfig()
    _linked(board, "WWD-10")

    assert await _service(board, jira).reconcile_exports() == 1


# --- the endpoints exist and do the right thing ------------------------------


def test_the_setup_endpoints_are_registered():
    src = Path("src/swarm/server/routes/jira.py").read_text()
    for route in ("/api/jira/discover", "/api/jira/plan", "/api/jira/confirm"):
        assert route in src, f"{route} is not registered; the setup flow has no data source"


def test_discover_and_plan_are_reads_and_confirm_is_the_only_write():
    """A 'preview' that writes is worse than no preview: it teaches the operator the
    button is safe."""
    src = Path("src/swarm/server/routes/jira.py").read_text()
    assert 'add_get("/api/jira/discover"' in src, "discover must be a GET"
    assert 'add_get("/api/jira/plan"' in src, "plan must be a GET"
    assert 'add_post("/api/jira/confirm"' in src, "confirm must be a POST"


def test_confirm_stores_the_map_the_operator_approved():
    """Re-deriving the mapping at confirm time would let what is stored differ from
    what was on screen — the operator would be approving something they never saw."""
    src = Path("src/swarm/server/routes/jira.py").read_text()
    body = src[src.index("async def handle_jira_confirm") :]
    assert 'body.get("status_map")' in body, (
        "confirm does not take the approved mapping from the request, so it stores "
        "something the operator may not have seen"
    )
    assert "confirmed_projects.append" in body, "confirming does not record confirmation"
