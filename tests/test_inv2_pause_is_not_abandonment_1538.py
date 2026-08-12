"""#1538 — a pause is not abandonment: INV-2 must not demote a paused worker's ACTIVE task.

WHAT WAS HAPPENING. `swarm_start_task` worked and its success was true. Then the worker went
RESTING — ordinary, a worker is RESTING whenever it is at its prompt or between turns — the
reactive reconciler fired on that state change, and INV-2 demoted ACTIVE → ASSIGNED:

    17:02:52  public-website  state→RESTING: #1b560cdc active→assigned (INV-2: not working)

404 of 418 TASK_RECONCILED rows were this demotion; three workers were sitting in the demoted
state when it was measured. It read as a bug in the VERB, because the worker sees success and a
later board read shows ASSIGNED with nothing connecting the two.

docs/specs/worker-asserted-active.md §3 already had the rule — "once a worker has asserted, the
daemon demoting it is the daemon overruling the only party that knows what is running" — and its
AC-4 shipped with no test. These are that test.

THE CONTROLS MATTER MORE THAN THE FIX HERE. It would be trivial to satisfy the first test by
disabling INV-2 entirely, which would reintroduce exactly what #405 was filed for. So every
"still demotes" case below is a guard against over-fixing.
"""

from __future__ import annotations

from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask, TaskStatus

WORKER = "platform"


def _board_with_active() -> tuple[TaskBoard, SwarmTask]:
    board = TaskBoard()
    task = board.add(SwarmTask(title="t", status=TaskStatus.ASSIGNED, assigned_worker=WORKER))
    board.activate(task.id)
    assert task.status == TaskStatus.ACTIVE, "fixture must start ACTIVE or it proves nothing"
    return board, task


# ---------------------------------------------------------------------------
# THE DEFECT
# ---------------------------------------------------------------------------


def test_a_resting_worker_keeps_its_active_task():
    """THE FIX. RESTING is a pause, and a pause is not abandonment.

    The worker appears in neither `working` (it is not BUZZING) nor `absent` (it is
    not SLEEPING/STUNG) — exactly the state every worker passes through between
    turns.
    """
    board, task = _board_with_active()

    repairs = board.reconcile_invariants(working_workers=set(), absent_workers=set())

    assert task.status == TaskStatus.ACTIVE, "a paused worker lost its ACTIVE row"
    assert repairs == []


def test_the_assertion_survives_a_reconcile_pass_not_just_an_immediate_read():
    """AC4 POSITIVE CONTROL, per the operator's instruction.

    An immediate read-back after start_task shows ACTIVE and proves NOTHING — the
    entire nature of this bug is a later revert. So this runs the reconciler (twice,
    as both the reactive and periodic sweeps would) and asserts the row is STILL
    active afterwards.
    """
    board, task = _board_with_active()

    for _ in range(2):
        board.reconcile_invariants(working_workers=set(), absent_workers=set())

    assert task.status == TaskStatus.ACTIVE


def test_a_busy_worker_still_keeps_its_task():
    """Unchanged behaviour — the case that always worked."""
    board, task = _board_with_active()

    board.reconcile_invariants(working_workers={WORKER}, absent_workers=set())

    assert task.status == TaskStatus.ACTIVE


# ---------------------------------------------------------------------------
# #405 MUST STILL WORK — the controls against over-fixing
# ---------------------------------------------------------------------------


def test_an_absent_worker_still_loses_its_stale_active_row():
    """THE CONTROL THAT STOPS THIS BEING "DISABLE INV-2".

    Without this, deleting the demotion outright would pass every test above while
    reintroducing the stale-ACTIVE-row accumulation #405 was filed for.
    """
    board, task = _board_with_active()

    repairs = board.reconcile_invariants(working_workers=set(), absent_workers={WORKER})

    assert task.status == TaskStatus.ASSIGNED
    assert len(repairs) == 1
    assert "absent" in repairs[0]["reason"]


def test_the_blocker_branch_still_fires_for_a_merely_resting_worker():
    """DELIBERATELY UNCHANGED. A live blocker binding is a fact about the WORK —
    the worker said it cannot proceed — not about whether it happens to be paused.
    So this fires for RESTING, where the plain demotion no longer does."""
    board, task = _board_with_active()

    repairs = board.reconcile_invariants(
        working_workers=set(), absent_workers=set(), blocked_task_ids={task.id}
    )

    assert task.status == TaskStatus.BLOCKED
    assert len(repairs) == 1


def test_inv1_still_collapses_two_active_rows_for_one_worker():
    """The other invariant #405 exists for must be untouched by this change."""
    board = TaskBoard()
    a = board.add(SwarmTask(title="a", status=TaskStatus.ASSIGNED, assigned_worker=WORKER))
    b = board.add(SwarmTask(title="b", status=TaskStatus.ASSIGNED, assigned_worker=WORKER))
    board.activate(a.id)
    a.status = TaskStatus.ACTIVE
    b.status = TaskStatus.ACTIVE

    board.reconcile_invariants(working_workers={WORKER}, absent_workers=set())

    actives = [t for t in (a, b) if t.status == TaskStatus.ACTIVE]
    assert len(actives) == 1, "INV-1 no longer collapses concurrent ACTIVE rows"


def test_reconcile_is_still_idempotent():
    """A second pass with unchanged inputs must report nothing."""
    board, _ = _board_with_active()

    board.reconcile_invariants(working_workers=set(), absent_workers={WORKER})
    second = board.reconcile_invariants(working_workers=set(), absent_workers={WORKER})

    assert second == []


def test_absent_defaults_to_never_demoting():
    """Legacy callers that pass no `absent` set must fail SAFE.

    A row left ACTIVE is visible and self-correcting; a row wrongly demoted is
    neither. Pinned so a future caller added without the argument cannot silently
    restore the old behaviour.
    """
    board, task = _board_with_active()

    board.reconcile_invariants(working_workers=set())

    assert task.status == TaskStatus.ACTIVE
