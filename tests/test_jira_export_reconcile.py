"""A dropped Jira export must be detectable and self-repairing (Jira blocker 2).

THE ARCHITECTURE THIS REPLACES, and it is the same one that produced the stale task
panel. Exports were fire-and-forget: ``fire_jira`` created a background task, caught
exceptions, and IGNORED the boolean return — so an export that ran and simply did not
take produced no exception, no log and no record. ``sync_loop`` only imported, so
nothing ever compared the two systems afterwards. A single dropped export left Jira
showing a ticket open while the swarm had it done, permanently, because nothing looked
again.

The operator has hit exactly this: Jira showing "Labels=swarm" tickets open while
Preview returned 0, because the tickets were done in Swarm and ``export_status`` had
failed.

Reacting to an event optimises latency; CORRECTNESS needs a comparable fact. ``status``
is the desired state, ``jira_exported_status`` is what Jira acknowledged, and their
difference means the export is outstanding — whatever the reason, whether the failure
was an exception, a False return, a process restart mid-flight, or Jira being down.
That is the same move as the board version that fixed the task panel.

WHAT IS NOT CLAIMED: none of this proves the swarm's own Jira credentials work, or that
a real Atlassian instance accepts a transition. It proves the DIVERGENCE IS DETECTED
and retried, which is the property that was missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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


def _linked(board: TaskBoard, key: str = "RCG-1") -> SwarmTask:
    task = board.add(SwarmTask(title="synced work", description=""))
    board.set_jira_key(task.id, key)
    return task


class _FakeJira:
    """Stands in for the Atlassian client. Records calls and can be made to fail the
    two ways that matter: raising, and returning False without raising."""

    enabled = True

    def __init__(self, *, accept: bool = True):
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
    svc._broadcast_ws = lambda _payload: None
    svc._track_task = lambda t: None
    return svc


# --- the acknowledgement is what makes divergence a fact ----------------------


@pytest.mark.asyncio
async def test_a_successful_export_records_what_jira_acknowledged(board: TaskBoard):
    task = _linked(board)
    board.assign(task.id, "api")
    board.activate(task.id)
    svc = _service(board, _FakeJira(accept=True))

    assert await svc.export_status(task.id, TaskStatus.ACTIVE) is True
    assert task.jira_exported_status == "active", (
        "a successful export did not record the acknowledged status, so the reconciler "
        "cannot tell it from one that never happened"
    )


@pytest.mark.asyncio
async def test_a_refused_export_records_nothing_and_stays_outstanding(board: TaskBoard):
    """The silent case: export_status returns False without raising. Before this it
    produced no exception, no log and no record at all."""
    task = _linked(board)
    board.assign(task.id, "api")
    board.activate(task.id)
    svc = _service(board, _FakeJira(accept=False))

    assert await svc.export_status(task.id, TaskStatus.ACTIVE) is False
    assert task.jira_exported_status == "", (
        "a REFUSED export recorded an acknowledgement, which would make the reconciler "
        "believe Jira is up to date when it is not"
    )


# --- reconciliation ----------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_re_exports_a_task_jira_never_acknowledged(board: TaskBoard):
    """The property the whole design exists for: a lost export is repaired without
    anyone noticing it was lost."""
    task = _linked(board)
    board.assign(task.id, "api")
    board.activate(task.id)

    dropped = _FakeJira(accept=False)
    await _service(board, dropped).export_status(task.id, TaskStatus.ACTIVE)
    assert task.jira_exported_status == "", "positive control: the export must have failed"

    working = _FakeJira(accept=True)
    repaired = await _service(board, working).reconcile_exports()

    assert repaired == 1, "the reconciler did not repair the outstanding export"
    assert working.exported == [("RCG-1", "active")], (
        f"the reconciler exported the wrong thing: {working.exported}"
    )
    assert task.jira_exported_status == "active"


@pytest.mark.asyncio
async def test_reconcile_does_nothing_when_everything_is_acknowledged(board: TaskBoard):
    """Otherwise every sync interval re-exports the whole board, which is both noisy
    against the Jira API and would mask a real divergence in the log."""
    task = _linked(board)
    board.assign(task.id, "api")
    board.activate(task.id)
    jira = _FakeJira(accept=True)
    svc = _service(board, jira)
    await svc.export_status(task.id, TaskStatus.ACTIVE)
    jira.exported.clear()

    assert await svc.reconcile_exports() == 0
    assert jira.exported == [], f"re-exported an already-acknowledged task: {jira.exported}"


@pytest.mark.asyncio
async def test_reconcile_ignores_tasks_with_no_jira_key(board: TaskBoard):
    """A task the swarm owns alone is not out of sync with anything."""
    local = board.add(SwarmTask(title="local only", description=""))
    board.assign(local.id, "api")
    jira = _FakeJira(accept=True)

    # Asserted via the LOG, not the export count. export_status already refuses a task
    # with no jira_key, so dropping the filter changes nothing observable in the
    # exports — and a control that removed it left this test green. What does change is
    # that every local task gets reported as "outstanding" every sync interval, which
    # buries a real divergence in noise.
    import logging

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    # get_logger("server.jira_service") produces "swarm.server.jira_service" — the
    # wrong name here would attach the handler to nothing and pass vacuously.
    logger = logging.getLogger("swarm.server.jira_service")
    handler = _Capture()
    logger.addHandler(handler)
    try:
        assert await _service(board, jira).reconcile_exports() == 0
    finally:
        logger.removeHandler(handler)

    assert jira.exported == [], "exported a task that has no Jira ticket"
    assert not [r for r in records if "reconcile" in r], (
        f"a task with no Jira ticket was reported as out of sync: {records}"
    )


@pytest.mark.asyncio
async def test_a_later_status_change_makes_the_task_outstanding_again(board: TaskBoard):
    """The comparison must track the CURRENT status, not merely 'has been exported
    once' — otherwise the first export would mark the task synced forever."""
    task = _linked(board)
    board.assign(task.id, "api")
    board.activate(task.id)
    jira = _FakeJira(accept=True)
    svc = _service(board, jira)
    await svc.export_status(task.id, TaskStatus.ACTIVE)

    board.complete(task.id, "done")
    jira.exported.clear()
    assert await svc.reconcile_exports() == 1, (
        "a status change after a successful export did not register as outstanding"
    )
    assert jira.exported == [("RCG-1", "done")]


# --- wiring ------------------------------------------------------------------


def test_the_sync_loop_reconciles_and_not_only_imports():
    """Import alone leaves the OUTBOUND direction unchecked, which is the direction the
    two systems actually drifted in."""
    src = Path("src/swarm/server/jira_service.py").read_text()
    body = src[src.index("async def sync_loop") :]
    code = "\n".join(ln for ln in body.split("\n") if not ln.strip().startswith("#"))
    assert "reconcile_exports()" in code, (
        "the sync loop still only imports; a dropped export is never noticed"
    )


def test_fire_and_forget_no_longer_discards_the_return_value():
    """A False return is not an error and raises nothing — it was the invisible half."""
    import re

    src = Path("src/swarm/server/jira_service.py").read_text()
    body = src[src.index("def fire_jira") : src.index("def fire_export")]
    # The RESULT OF THE AWAIT must be what is inspected. Checking for the words
    # "result" and "is False" was too weak: an injection that awaited the call and then
    # assigned result = None kept both strings and still discarded the value.
    assert re.search(r"result\s*=\s*await coro_factory", body), (
        "fire_jira no longer binds the awaited result, so an export that ran and did "
        "not take is silent again"
    )
    assert re.search(r"if\s+result\s+is\s+False", body), (
        "the bound result is never compared, so a False return still goes unreported"
    )
