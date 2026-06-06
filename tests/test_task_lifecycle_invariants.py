"""#405 task-lifecycle invariants: BLOCKED status, operator-action type,
INV-1/2/3 enforcement + reconciliation.

INV-1  ≤1 ACTIVE task per worker
INV-2  ACTIVE ⇒ worker working OR task blocked (no ACTIVE+RESTING+no-blocker)
INV-3  a worker's "current task" == its single ACTIVE task, or None
"""

from __future__ import annotations

import pytest

from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask, TaskStatus, TaskType


@pytest.fixture
def board():
    return TaskBoard()


def _assigned(board, title, worker):
    t = board.create(title=title)
    board.assign(t.id, worker)
    return board.get(t.id)


# --- foundation: BLOCKED status + operator-action type -----------------


def test_blocked_status_exists_and_block_transitions():
    assert TaskStatus.BLOCKED.value == "blocked"
    t = SwarmTask(title="x")
    t.assign("api")
    t.block("waiting on #200")
    assert t.status == TaskStatus.BLOCKED
    assert t.block_reason == "waiting on #200"
    # BLOCKED is not auto-assignable and not "active".
    assert t.is_available is False


def test_operator_action_task_type_exists():
    assert TaskType.OPERATOR.value == "operator"
    t = SwarmTask(title="rotate org token", task_type=TaskType.OPERATOR)
    assert t.is_operator_action is True
    assert SwarmTask(title="y", task_type=TaskType.CHORE).is_operator_action is False


# --- INV-3 accessor: current_task_for_worker ---------------------------


def test_current_task_for_worker_is_single_active_or_none(board):
    a = _assigned(board, "a", "api")
    b = _assigned(board, "b", "api")
    assert board.current_task_for_worker("api") is None  # nothing active yet
    board.activate(a.id)
    assert board.current_task_for_worker("api").id == a.id
    # Activating b must demote a (INV-1) so the accessor stays unambiguous.
    board.activate(b.id)
    cur = board.current_task_for_worker("api")
    assert cur.id == b.id
    assert board.get(a.id).status == TaskStatus.ASSIGNED


# --- INV-1: activate() enforces ≤1 active ------------------------------


def test_activate_demotes_prior_active(board):
    a = _assigned(board, "a", "api")
    b = _assigned(board, "b", "api")
    board.activate(a.id)
    board.activate(b.id)
    actives = [t for t in board.tasks_for_worker("api") if t.status == TaskStatus.ACTIVE]
    assert [t.id for t in actives] == [b.id]


def test_activate_skips_operator_action(board):
    op = board.create(title="org admin", task_type=TaskType.OPERATOR)
    board.assign(op.id, "api")
    assert board.activate(op.id) is False  # operator-action never goes ACTIVE
    assert board.get(op.id).status != TaskStatus.ACTIVE


# --- INV-1/2/3 reconciliation ------------------------------------------


def test_reconcile_demotes_extra_active_and_idle_active(board):
    # Worker w1: two ACTIVE (INV-1 violation) while RESTING (INV-2 violation).
    a = _assigned(board, "a", "w1")
    b = _assigned(board, "b", "w1")
    a.status = TaskStatus.ACTIVE
    b.status = TaskStatus.ACTIVE
    # Worker w2: one ACTIVE but w2 is working — must be left alone.
    c = _assigned(board, "c", "w2")
    c.status = TaskStatus.ACTIVE
    # Operator-action somehow ACTIVE — must be demoted regardless of state.
    op = board.create(title="op", task_type=TaskType.OPERATOR)
    board.assign(op.id, "w3")
    op.status = TaskStatus.ACTIVE

    repairs = board.reconcile_invariants(working_workers={"w2"})

    # w1: collapsed to exactly one ACTIVE, and since w1 not working that
    # one is also demoted → zero ACTIVE for w1.
    w1_active = [t for t in board.tasks_for_worker("w1") if t.status == TaskStatus.ACTIVE]
    assert w1_active == []
    # w2 working → its single ACTIVE preserved.
    assert board.get(c.id).status == TaskStatus.ACTIVE
    # operator-action demoted out of ACTIVE.
    assert board.get(op.id).status != TaskStatus.ACTIVE
    # repairs reported for audit/buzz, and idempotent on re-run.
    assert repairs
    assert board.reconcile_invariants(working_workers={"w2"}) == []


def test_reconcile_blocked_when_blocker_binding(board):
    a = _assigned(board, "a", "w1")
    a.status = TaskStatus.ACTIVE
    # w1 not working, but task has a blocker binding → BLOCKED not ASSIGNED.
    repairs = board.reconcile_invariants(working_workers=set(), blocked_task_ids={a.id})
    assert board.get(a.id).status == TaskStatus.BLOCKED
    assert repairs


# --- P2 (#611): started_at + safe _recon_inv1 tiebreak ---


def test_start_stamps_started_at():
    """#611 P2: task.start() records when work began."""
    t = SwarmTask(title="x")
    assert t.started_at is None
    t.start()
    assert t.started_at is not None and t.started_at > 0


def test_reconcile_inv1_keeps_earliest_started_not_newest_updated(board):
    """#611 P2: with >1 ACTIVE per worker, keep the EARLIEST-STARTED task (the
    in-flight job), NOT the newest-by-updated_at. Demoting the long-running task
    would interrupt it — this is what would have killed #604's 27k-record run.

    Red under the old tiebreak: updated_at favours `new`, so `old` (in-flight)
    gets demoted."""
    old = _assigned(board, "in-flight", "w1")
    new = _assigned(board, "newer", "w1")
    old.status = TaskStatus.ACTIVE
    new.status = TaskStatus.ACTIVE
    # started earlier but every field disagrees with the old updated_at rule:
    old.started_at, old.updated_at = 1000.0, 1000.0
    new.started_at, new.updated_at = 2000.0, 2000.0

    board.reconcile_invariants(working_workers={"w1"})

    assert board.get(old.id).status == TaskStatus.ACTIVE  # earliest-started survives
    assert board.get(new.id).status == TaskStatus.ASSIGNED


def test_reconcile_inv1_null_started_at_falls_back_to_created_at(board):
    """Legacy ACTIVE tasks (started_at NULL) sort by created_at."""
    first = _assigned(board, "first", "w1")
    second = _assigned(board, "second", "w1")
    first.status = TaskStatus.ACTIVE
    second.status = TaskStatus.ACTIVE
    first.started_at = None
    second.started_at = None
    first.created_at, second.created_at = 1000.0, 2000.0

    board.reconcile_invariants(working_workers={"w1"})

    assert board.get(first.id).status == TaskStatus.ACTIVE  # earliest created survives
    assert board.get(second.id).status == TaskStatus.ASSIGNED
