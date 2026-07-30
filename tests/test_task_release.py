"""#1059 — releasing and reassigning an owned task from any holdable status.

The board had no supported path to move an owned, non-UNASSIGNED task.
Four verbs refused a BLOCKED one for four *different* reasons — assign
needs UNASSIGNED, start_task needs ASSIGNED, unassign needs
ASSIGNED/ACTIVE, and no worker-side tool releases ownership at all —
which is why it presented as several unrelated bugs. The only escape was
force-complete → reopen → approve → reassign, and step one writes a
COMPLETED history entry for work that was never done.
"""

from __future__ import annotations

import pytest

from swarm.mcp.queen_handlers._tasks import _handle_reassign_task
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskAction
from swarm.tasks.task import PARKED_TAG, TaskStatus
from tests.conftest import make_daemon

QUEEN = "queen"


@pytest.fixture
def board():
    return TaskBoard()


def _in_status(board: TaskBoard, status: TaskStatus, worker: str = "api"):
    """Build an owned task sitting in *status*."""
    t = board.create(title=f"task in {status.value}")
    board.assign(t.id, worker)
    if status is TaskStatus.ASSIGNED:
        return board.get(t.id)
    board.activate(t.id)
    if status is TaskStatus.ACTIVE:
        return board.get(t.id)
    if status is TaskStatus.BLOCKED:
        assert board.block_for_operator(t.id, "waiting on a human") is True
        return board.get(t.id)
    raise AssertionError(f"unsupported setup status {status}")


# --- board.release, from each of the three source states ----------------


@pytest.mark.parametrize("status", [TaskStatus.ASSIGNED, TaskStatus.ACTIVE, TaskStatus.BLOCKED])
def test_release_from_each_holdable_status(board, status) -> None:
    t = _in_status(board, status)
    assert t.status is status and t.assigned_worker == "api"

    assert board.release(t.id) is True

    got = board.get(t.id)
    assert got.status is TaskStatus.UNASSIGNED
    assert got.assigned_worker is None


def test_release_refuses_terminal_tasks(board) -> None:
    """A closed task has no owner to release; reopen is that verb."""
    t = _in_status(board, TaskStatus.ACTIVE)
    board.complete(t.id, resolution="done")
    assert board.release(t.id) is False
    assert board.get(t.id).status is TaskStatus.DONE


def test_release_refuses_an_already_ownerless_task(board) -> None:
    t = board.create(title="never owned")
    assert board.release(t.id) is False


def test_release_is_falsey_for_a_missing_task(board) -> None:
    assert board.release("no-such-id") is False


def test_release_clears_the_parked_marker(board) -> None:
    """#1015's marker belongs to the OLD owner's set-down; a released task
    must not arrive at its next owner still flagged do-not-dispatch."""
    t = _in_status(board, TaskStatus.ACTIVE)
    assert board.park(t.id, "api", "set down") is True
    assert PARKED_TAG in board.get(t.id).tags

    board.release(t.id)

    got = board.get(t.id)
    assert PARKED_TAG not in got.tags
    assert not got.is_on_hold


# --- INV-1: release only ever moves a task OUT of ACTIVE ----------------


def test_release_preserves_one_active_task_per_worker(board) -> None:
    """#405 INV-1 / #611. Release cannot create a second ACTIVE task
    because it only ever leaves ACTIVE, never enters it."""
    first = _in_status(board, TaskStatus.ACTIVE, worker="api")
    board.release(first.id)

    second = _in_status(board, TaskStatus.ACTIVE, worker="api")

    active = [t for t in board.all_tasks if t.status is TaskStatus.ACTIVE]
    assert [t.id for t in active] == [second.id]
    assert board.get(first.id).status is TaskStatus.UNASSIGNED


# --- reassign end to end, from each state -------------------------------


@pytest.mark.parametrize("status", [TaskStatus.ASSIGNED, TaskStatus.ACTIVE, TaskStatus.BLOCKED])
def test_queen_reassign_moves_an_owned_task(monkeypatch, status) -> None:
    d = make_daemon(monkeypatch)
    t = _in_status(d.task_board, status, worker="api")

    out = _handle_reassign_task(
        d, QUEEN, {"number": t.number, "to_worker": "web", "reason": "api can't reach it"}
    )

    assert "web" in out[0]["text"]
    got = d.task_board.get(t.id)
    assert got.assigned_worker == "web"
    assert got.status is TaskStatus.ASSIGNED


def test_reassigning_a_blocked_task_writes_no_completed_entry(monkeypatch) -> None:
    """The whole point. The old escape hatch force-completed first, putting
    a COMPLETED entry in the history of work that was never done."""
    d = make_daemon(monkeypatch)
    t = _in_status(d.task_board, TaskStatus.BLOCKED, worker="api")

    _handle_reassign_task(d, QUEEN, {"number": t.number, "to_worker": "web", "reason": "move it"})

    actions = [e.action for e in d.task_history.get_events(t.id)]
    assert TaskAction.COMPLETED not in actions
    assert d.task_board.get(t.id).status is not TaskStatus.DONE


def test_reassigning_clears_blocker_rows(monkeypatch) -> None:
    """A released task must not arrive at its new owner still carrying the
    OLD owner's blocker — the IdleWatcher would go on suppressing nudges
    for a dependency that is nobody's any more."""
    d = make_daemon(monkeypatch)
    t = _in_status(d.task_board, TaskStatus.BLOCKED, worker="api")

    cleared: list[int] = []

    class _Store:
        def clear_for_task(self, number: int) -> int:
            cleared.append(number)
            return 2

    d.blocker_store = _Store()

    _handle_reassign_task(d, QUEEN, {"number": t.number, "to_worker": "web", "reason": "move it"})

    assert cleared == [t.number], "blocker rows must be cleared on release"


def test_reassign_survives_a_missing_blocker_store(monkeypatch) -> None:
    """Clearing is best-effort; the release is the load-bearing part."""
    d = make_daemon(monkeypatch)
    t = _in_status(d.task_board, TaskStatus.BLOCKED, worker="api")
    d.blocker_store = None

    _handle_reassign_task(d, QUEEN, {"number": t.number, "to_worker": "web", "reason": "move it"})

    assert d.task_board.get(t.id).assigned_worker == "web"


# --- the ownership guard is untouched -----------------------------------


def test_release_is_not_reachable_from_a_worker_tool(monkeypatch) -> None:
    """#1059 adds a QUEEN path. It must not become a way for a worker to
    mutate another worker's task — that authority question is #1045's, and
    the guard stays exactly as strict."""
    from swarm.mcp.tools import TOOLS, handle_tool_call

    assert not [t for t in TOOLS if "release" in t["name"]]

    d = make_daemon(monkeypatch)
    t = _in_status(d.task_board, TaskStatus.ACTIVE, worker="api")

    args = {"number": t.number, "resolution": "x"}
    out = str(handle_tool_call(d, "web", "swarm_complete_task", args))

    assert "not assigned to you" in out
    assert d.task_board.get(t.id).status is not TaskStatus.DONE


def test_non_queen_cannot_reassign(monkeypatch) -> None:
    d = make_daemon(monkeypatch)
    t = _in_status(d.task_board, TaskStatus.BLOCKED, worker="api")

    args = {"number": t.number, "to_worker": "web", "reason": "grab"}
    out = str(_handle_reassign_task(d, "web", args))

    assert "queen" in out.lower()
    assert d.task_board.get(t.id).assigned_worker == "api"
