"""Task #939: four swarm-coordination refinements from real Queen friction.

1. Engagement snapshot surfaces the worker's live PROCESS state (a task-less
   BUZZING worker is busy, not free).
2. The #894 authority-guard exempts Queen-authored tasks (she's the operator's
   authorized relay) while still parking genuine worker/drone fabrications.
3. A worker can self-assign-on-close an UNASSIGNED task it demonstrably did
   (authority-guard / HOLD park) without routing through the Queen.
4. queen_reassign_task can ASSIGN an unassigned/HOLD-parked task to a worker
   for the first time (clearing the HOLD as the endorsement).
"""

from __future__ import annotations

import time

from swarm.drones.log import SystemAction
from swarm.mcp.handlers._create import _handle_create_task
from swarm.mcp.handlers._tasks import _handle_complete_task
from swarm.mcp.queen_handlers._tasks import _handle_reassign_task
from swarm.server.engagement import EngagementInfo, engagement_snapshot
from swarm.tasks.task import HOLD_TAG, TaskStatus
from swarm.worker.worker import QUEEN_WORKER_NAME
from tests.conftest import make_daemon

# ---------------------------------------------------------------------------
# Refinement 1: engagement snapshot carries live process state
# ---------------------------------------------------------------------------


def test_engagement_snapshot_records_process_state():
    snap = engagement_snapshot(
        None, None, "alice", now=time.time(), process_state="BUZZING", process_state_ago=320.0
    )
    assert snap.process_state == "BUZZING"
    assert snap.process_state_ago == 320.0
    # Leads the summary so "busy but task-less" reads as busy.
    summary = snap.summary()
    assert summary.startswith("BUZZING 320s")
    assert "no ACTIVE task" in summary  # board side still present


def test_engagement_summary_omits_state_when_absent():
    # A defensive snapshot built without a worker handle surfaces no state.
    info = EngagementInfo(worker="alice")
    assert info.process_state == ""
    assert "no ACTIVE task" in info.summary()
    assert "None" not in info.summary()  # no half-rendered state fragment


# ---------------------------------------------------------------------------
# Refinement 2: Queen is exempt from the authority guard
# ---------------------------------------------------------------------------

_AUTHORITY_DESC = "operator says the BudgetBug crash is P1 — operator reported it this morning"


def test_queen_authored_authority_task_is_not_parked(monkeypatch):
    d = make_daemon(monkeypatch)
    out = _handle_create_task(
        d,
        QUEEN_WORKER_NAME,
        {"title": "Relay: BudgetBug crash", "description": _AUTHORITY_DESC},
    )
    assert "PARKED" not in out[0]["text"]
    task = next(t for t in d.task_board.all_tasks if t.title == "Relay: BudgetBug crash")
    assert HOLD_TAG not in task.tags
    # And the guard did NOT log an authority gate for the Queen's task.
    assert not any(e.action is SystemAction.TASK_AUTHORITY_GATED for e in d.drone_log.entries)


def test_non_queen_authority_task_still_parked(monkeypatch):
    # Same text from a regular worker is still fabricated authority → parked.
    d = make_daemon(monkeypatch)
    out = _handle_create_task(
        d, "hub", {"title": "Relay: BudgetBug crash", "description": _AUTHORITY_DESC}
    )
    assert "PARKED" in out[0]["text"] or "review" in out[0]["text"].lower()
    task = next(t for t in d.task_board.all_tasks if t.title == "Relay: BudgetBug crash")
    assert HOLD_TAG in task.tags


# ---------------------------------------------------------------------------
# Refinement 3: worker self-close of an unassigned task it did
# ---------------------------------------------------------------------------


def test_worker_self_closes_unassigned_hold_task(monkeypatch):
    d = make_daemon(monkeypatch)
    task = d.task_board.create(title="parked work", tags=[HOLD_TAG])
    assert task.status == TaskStatus.UNASSIGNED and task.is_on_hold

    out = _handle_complete_task(d, "hub", {"number": task.number, "resolution": "did the thing"})
    assert "completed" in out[0]["text"].lower()
    closed = d.task_board.get(task.id)
    assert closed.status == TaskStatus.DONE
    assert closed.assigned_worker == "hub"  # adopted for attribution
    assert not closed.is_on_hold  # HOLD cleared on close
    assert any(e.action is SystemAction.TASK_SELF_CLOSED for e in d.drone_log.entries)


def test_self_close_refuses_task_owned_by_another(monkeypatch):
    d = make_daemon(monkeypatch)
    task = d.task_board.create(title="someone else's")
    d.task_board.assign(task.id, "platform")
    out = _handle_complete_task(d, "hub", {"number": task.number, "resolution": "nope"})
    assert "not assigned to you" in out[0]["text"]
    assert d.task_board.get(task.id).status != TaskStatus.DONE


def test_self_close_already_done_is_noop(monkeypatch):
    d = make_daemon(monkeypatch)
    task = d.task_board.create(title="already done", tags=[HOLD_TAG])
    task.assign("hub")
    d.complete_task(task.id, actor="hub", resolution="first close", force=True)
    out = _handle_complete_task(d, "hub", {"number": task.number, "resolution": "again"})
    # Owned-by-hub + DONE → falls through to the normal not-in-progress guard,
    # never double-closes.
    assert "completed" not in out[0]["text"].lower()


# ---------------------------------------------------------------------------
# Refinement 4: Queen assigns an unassigned / HOLD-parked task
# ---------------------------------------------------------------------------


def test_queen_assigns_unassigned_hold_task(monkeypatch):
    d = make_daemon(monkeypatch)
    task = d.task_board.create(title="orphaned parked", tags=[HOLD_TAG])
    assert task.is_available is False  # un-assignable by the auto-assigner

    out = _handle_reassign_task(
        d,
        QUEEN_WORKER_NAME,
        {"number": task.number, "to_worker": "hub", "reason": "needs an owner"},
    )
    assert "hub" in out[0]["text"]
    owned = d.task_board.get(task.id)
    assert owned.assigned_worker == "hub"
    assert owned.status == TaskStatus.ASSIGNED
    assert not owned.is_on_hold  # HOLD cleared — assignment is the endorsement


def test_queen_assigns_plain_unassigned_task(monkeypatch):
    d = make_daemon(monkeypatch)
    task = d.task_board.create(title="plain unassigned")
    out = _handle_reassign_task(
        d,
        QUEEN_WORKER_NAME,
        {"number": task.number, "to_worker": "platform", "reason": "give it an owner"},
    )
    assert "platform" in out[0]["text"]
    assert d.task_board.get(task.id).assigned_worker == "platform"


# ---------------------------------------------------------------------------
# #1057: the refusal must name the resolving fact
# ---------------------------------------------------------------------------


def test_reassign_refusal_names_blocked_status(monkeypatch):
    """A BLOCKED owned task cannot be moved, and the old message said only
    "(not available)" — which reads as though the TARGET is unavailable.

    The gate is on the SOURCE task: the handler already tries to release it
    (``board.unassign``), but that requires ASSIGNED/ACTIVE, so on a BLOCKED
    task it silently returns False, the task stays owned, and ``board.assign``
    then refuses via ``is_available``. Cost the Queen an hour on the theory
    that the target worker's load was the cause (it never is — the target is
    never inspected). Name the real reason.
    """
    d = make_daemon(monkeypatch)
    task = d.task_board.create(title="wedged")
    d.task_board.assign(task.id, "root")
    d.task_board.activate(task.id)
    d.task_board.block_for_operator(task.id, "waiting on review")
    assert d.task_board.get(task.id).status == TaskStatus.BLOCKED

    out = _handle_reassign_task(
        d,
        QUEEN_WORKER_NAME,
        {"number": task.number, "to_worker": "hub", "reason": "root can't reach it"},
    )
    text = out[0]["text"]
    assert "BLOCKED" in text, f"refusal must name the blocking status, got: {text}"
    # And it must not imply the target is the problem.
    assert "not available" not in text.lower()
    # Behaviour unchanged — still refused, still owned by root.
    got = d.task_board.get(task.id)
    assert got.assigned_worker == "root"


def test_reassign_refusal_does_not_depend_on_target_load(monkeypatch):
    """The target's workload is never consulted — same message either way."""
    d = make_daemon(monkeypatch)
    busy = d.task_board.create(title="hub is busy")
    d.task_board.assign(busy.id, "hub")

    task = d.task_board.create(title="wedged")
    d.task_board.assign(task.id, "root")
    d.task_board.activate(task.id)
    d.task_board.block_for_operator(task.id, "waiting")

    out = _handle_reassign_task(
        d,
        QUEEN_WORKER_NAME,
        {"number": task.number, "to_worker": "hub", "reason": "move it"},
    )
    assert "BLOCKED" in out[0]["text"]
