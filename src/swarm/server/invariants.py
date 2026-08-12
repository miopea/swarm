"""InvariantReconciler — task-board state-invariant repair (#405).

Extracted from :class:`~swarm.server.daemon.SwarmDaemon` (audit
finding #1).  Runs the :meth:`TaskBoard.reconcile_invariants` sweep
against the live worker/blocker state and buzz-logs every auto-repair.

See ``docs/specs/daemon-god-object-refactor.md`` and
``docs/specs/task-board-invariants.md`` for the policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from swarm.drones.log import LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.tasks.history import TaskAction
from swarm.worker.worker import WorkerState

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.drones.log import DroneLog
    from swarm.tasks.blockers import BlockerStore
    from swarm.tasks.board import TaskBoard
    from swarm.tasks.history import SqliteTaskHistory, TaskHistory
    from swarm.worker.worker import Worker


_log = get_logger("server.invariants")


class InvariantReconciler:
    """Repair task-board invariants against live worker/blocker state.

    Reads:
      * ``workers`` list — to know who's BUZZING/WAITING.
      * ``blocker_store`` — to know who has a live blocker binding.
      * ``task_board`` — the source of truth for ACTIVE/ASSIGNED rows.

    Writes (via ``task_board.reconcile_invariants``):
      * Auto-demotes / unassigns tasks that violate the invariants.
      * Emits ``SystemAction.TASK_RECONCILED`` per repair and a
        ``TaskAction.UNASSIGNED`` history entry so the operator can
        audit the auto-corrections post-hoc.
      * #1527: stalled-dispatch findings instead emit
        ``TASK_DISPATCH_STALLED`` / ``TaskAction.DISPATCH_STALLED``,
        because that rule REPORTS a dispatch that never landed rather
        than moving a status — logging it as a reconcile would claim a
        transition that never happened.
    """

    def __init__(
        self,
        *,
        task_board: TaskBoard | None,
        task_history: TaskHistory | SqliteTaskHistory,
        drone_log: DroneLog,
        blocker_store: BlockerStore | None,
        get_workers: Callable[[], list[Worker]],
    ) -> None:
        self._task_board = task_board
        self._task_history = task_history
        self._drone_log = drone_log
        self._blocker_store = blocker_store
        self._get_workers = get_workers

    def working_workers(self) -> set[str]:
        """Workers genuinely engaged on a turn (BUZZING/WAITING).

        #1538 CORRECTED THIS DOCSTRING'S CLAIM. It used to say "anything else
        (RESTING/SLEEPING/STUNG) cannot legitimately hold an ACTIVE task", and
        INV-2 demoted on that basis. It is false: ACTIVE means "this is the task
        I am on", not "I am mid-token-generation", so a RESTING worker at its
        prompt still owns its task. See :meth:`absent_workers`, which is what
        INV-2 now demotes on.
        """
        workers: list[Worker] = self._get_workers()
        return {w.name for w in workers if w.state in (WorkerState.BUZZING, WorkerState.WAITING)}

    def absent_workers(self) -> set[str]:
        """Workers that are ABSENT rather than merely paused (#1538).

        The distinction INV-2 actually needs is OWNED vs ABANDONED, not BUSY vs
        IDLE. SLEEPING and STUNG mean absence — the first is RESTING that has
        outlasted ``sleeping_threshold``, the second is a dead process. RESTING
        alone is a pause and must not cost a worker its ACTIVE row.

        The grace period is NOT re-invented here: the state machine already
        promotes RESTING → SLEEPING after ``sleeping_threshold`` (worker.py), so
        keying off SLEEPING inherits one operator-tunable definition instead of
        adding a second that would drift from it.

        READS ``display_state``, NOT ``state``, AND THAT IS LOAD-BEARING. SLEEPING
        is DERIVED — ``Worker.state`` is never set to it; it stays RESTING and
        ``display_state`` rewrites it once ``state_duration`` passes the threshold
        (worker.py:333-341). Matching on ``state`` here would silently never see a
        sleeping worker, so this would only ever fire for STUNG and #405's
        stale-row repair would quietly stop working — a guard that under-fires and
        reports nothing looks exactly like a guard with nothing to do.

        Consequence worth knowing: ``display_state`` never returns SLEEPING for the
        Queen (she is always-on by design), so her ACTIVE rows are only ever
        demoted when STUNG.
        """
        workers: list[Worker] = self._get_workers()
        return {
            w.name for w in workers if w.display_state in (WorkerState.SLEEPING, WorkerState.STUNG)
        }

    def blocked_task_ids(self) -> set[str]:
        """IDs of ACTIVE/ASSIGNED tasks with a live ``swarm_report_blocker``
        binding — these park to BLOCKED (not ASSIGNED) under INV-2."""
        if self._blocker_store is None or self._task_board is None:
            return set()
        bindings: set[tuple[str, int]] = set()
        for w in self._get_workers():
            try:
                for b in self._blocker_store.list_for_worker(w.name):
                    bindings.add((b.worker, b.task_number))
            except Exception:
                continue
        return {
            t.id
            for t in self._task_board.assigned_or_active_tasks
            if (t.assigned_worker or "", t.number) in bindings
        }

    def reconcile_active_per_worker(self) -> None:
        """Demote stale concurrent ACTIVE tasks at boot.

        Older daemon versions left prior ACTIVE tasks ACTIVE when a
        newer one was dispatched, so the board could accumulate
        multiple ACTIVE rows per worker. The dashboard's IN PROGRESS
        label must reflect what the worker is actually processing, so
        on boot we keep the most recently updated ACTIVE per worker
        and demote the rest to ASSIGNED.
        """
        # #405: full INV-1/2/3 + operator-action reconciliation (was a
        # startup-only >1-ACTIVE sweep). Repairs the live corrupt
        # records and buzz-logs each so the operator can audit
        # auto-corrections.
        self.run("startup")

    def run(self, reason: str) -> None:
        """Run the task-board invariant reconciler.

        Buzz-logs + history every auto-repair (#405).
        """
        if self._task_board is None:
            return
        try:
            repairs = self._task_board.reconcile_invariants(
                working_workers=self.working_workers(),
                blocked_task_ids=self.blocked_task_ids(),
                absent_workers=self.absent_workers(),
            )
        except Exception:
            _log.warning("invariant reconciliation failed", exc_info=True)
            return
        for r in repairs:
            detail = f"{reason}: #{r['task_id'][:8]} {r['from']}→{r['to']} ({r['reason']})"
            # #1527: a stalled-dispatch repair REPORTS, it does not move status, so
            # it must not be logged as TASK_RECONCILED/UNASSIGNED — that would
            # claim a status change that never happened and make the row unusable
            # as evidence for the thing it exists to prove.
            if r.get("kind") == "stalled_dispatch":
                buzz_action = SystemAction.TASK_DISPATCH_STALLED
                hist_action = TaskAction.DISPATCH_STALLED
            else:
                buzz_action = SystemAction.TASK_RECONCILED
                hist_action = TaskAction.UNASSIGNED
            try:
                self._drone_log.add(
                    buzz_action,
                    r.get("worker") or "system",
                    detail,
                    category=LogCategory.TASK,
                    metadata=dict(r),
                )
                self._task_history.append(r["task_id"], hist_action, actor="system", detail=detail)
            except Exception:
                _log.debug("reconcile audit log failed", exc_info=True)
        if repairs:
            _log.info("invariant reconcile (%s): repaired %d records", reason, len(repairs))
