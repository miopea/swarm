"""Task #941: a worker can self-close its OWN done-but-BLOCKED task.

Sibling of #939 (which covered the UNASSIGNED case). Before this, a worker that
parked its own task in BLOCKED (swarm_report_blocker, operator hold) and then
finished the work could NOT close it — swarm_complete_task rejected any
non-ACTIVE status ("not in progress"), so closure had to route through the
Queen's force-complete. The fix routes an owner's BLOCKED-task close through the
force path: clears the blocker binding, passes the status gate, keeps the full
audit trail, and (DONE being terminal) survives reconciliation.
"""

from __future__ import annotations

from types import SimpleNamespace

from swarm.drones.log import SystemAction
from swarm.mcp.handlers._tasks import _handle_complete_task
from swarm.tasks.task import TaskStatus
from tests.conftest import make_daemon


def _blocked_task(d, *, worker="swarm", title="done but blocked"):
    task = d.task_board.create(title=title)
    d.task_board.assign(task.id, worker)
    task.block(reason="was waiting on an upstream task")
    return task


def test_worker_self_closes_own_blocked_task(monkeypatch):
    d = make_daemon(monkeypatch)
    cleared: list[int] = []
    d.blocker_store = SimpleNamespace(clear_for_task=lambda n: cleared.append(n) or 1)

    task = _blocked_task(d)
    assert task.status == TaskStatus.BLOCKED and task.assigned_worker == "swarm"

    out = _handle_complete_task(
        d, "swarm", {"number": task.number, "resolution": "finished the work"}
    )
    assert "completed" in out[0]["text"].lower()

    closed = d.task_board.get(task.id)
    assert closed.status == TaskStatus.DONE
    assert task.number in cleared  # blocker binding cleared on close
    # Audit trail recorded.
    assert any(e.action is SystemAction.TASK_COMPLETED for e in d.drone_log.entries)


def test_blocked_self_close_survives_reconciliation(monkeypatch):
    d = make_daemon(monkeypatch)
    d.blocker_store = SimpleNamespace(clear_for_task=lambda n: 0)
    task = _blocked_task(d)

    _handle_complete_task(d, "swarm", {"number": task.number, "resolution": "done"})
    assert d.task_board.get(task.id).status == TaskStatus.DONE

    # A reconciler sweep must not revert a legitimate worker-initiated close —
    # DONE is terminal and the binding is gone, so it stays closed.
    d.task_board.reconcile_invariants(working_workers=set(), blocked_task_ids=set())
    assert d.task_board.get(task.id).status == TaskStatus.DONE


def test_blocked_close_rejected_for_another_workers_task(monkeypatch):
    d = make_daemon(monkeypatch)
    d.blocker_store = SimpleNamespace(clear_for_task=lambda n: 0)
    task = _blocked_task(d, worker="platform")

    out = _handle_complete_task(d, "swarm", {"number": task.number, "resolution": "nope"})
    assert "not assigned to you" in out[0]["text"]
    assert d.task_board.get(task.id).status == TaskStatus.BLOCKED  # untouched


def test_no_blocker_store_still_closes_blocked_task(monkeypatch):
    # Defensive: the force path no-ops the clear when no store is wired.
    d = make_daemon(monkeypatch)
    assert d.blocker_store is None
    task = _blocked_task(d)

    out = _handle_complete_task(d, "swarm", {"number": task.number, "resolution": "done"})
    assert "completed" in out[0]["text"].lower()
    assert d.task_board.get(task.id).status == TaskStatus.DONE
