"""InvariantReconciler — task-board state-invariant repair (#405).

Extracted from :class:`~swarm.server.daemon.SwarmDaemon` (audit
finding #1).  Runs the :meth:`TaskBoard.reconcile_invariants` sweep
against the live worker/blocker state and buzz-logs every auto-repair.

See ``docs/specs/daemon-god-object-refactor.md`` and
``docs/specs/task-board-invariants.md`` for the policy.
"""

from __future__ import annotations

import math
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

# Seconds a worker must sit RESTING before INV-2 treats it as ABSENT (#1571).
#
# MEASURED, NOT CHOSEN. Across 45 RESTING episodes in which the worker held an ACTIVE
# task and then resumed (buzz_log STATE_TRANSITION × task_history ACTIVE intervals,
# 2026-07-14 → 08-13): median 130s, p90 2439s, p95 3394s. Share those episodes a given
# threshold would have wrongly declared absent — 300s: 37.8%, 900s: 26.7%, 1200s: 24.4%,
# 1800s: 22.2%, 3600s: 4.4%, 7200s: 2.2%. The knee is at an hour and nothing changes
# between 1200 and 1800, which is why raising the display threshold could not fix this.
#
# Used as the fallback when the configured value is unusable, so that a zero or a missing
# knob cannot mean "demote instantly" — see :meth:`InvariantReconciler._absent_threshold`.
INV2_ABSENT_THRESHOLD_DEFAULT = 3600.0


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
        absent_threshold: Callable[[], float] | None = None,
    ) -> None:
        self._task_board = task_board
        self._task_history = task_history
        self._drone_log = drone_log
        self._blocker_store = blocker_store
        self._get_workers = get_workers
        # #1571: seconds RESTING before INV-2 calls a worker absent. A CALLABLE, not a
        # value, so the operator's config edits hot-apply — ``sleeping_threshold`` was
        # changed at runtime via PUT /api/config on that very ticket and this knob has to
        # answer the same way. None (tests, legacy callers) uses the measured default.
        self._absent_threshold_source = absent_threshold

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

    def _absent_threshold(self) -> float:
        """Seconds RESTING before a worker counts as absent (#1571).

        FAILS TO THE MEASURED DEFAULT, NEVER TO ZERO. A zero would mean "demote
        instantly" — every RESTING worker loses its ACTIVE task on the next sweep — so
        a missing, non-numeric, non-positive or non-finite value resolves to
        ``INV2_ABSENT_THRESHOLD_DEFAULT`` instead. Infinity is rejected for the mirror
        reason: it would silently disable #405's repair, and a guard that never fires
        looks exactly like a guard with nothing to do.

        A raising callable is caught rather than propagated: a broken config read must
        not take out invariant repair for the whole board.
        """
        if self._absent_threshold_source is None:
            return INV2_ABSENT_THRESHOLD_DEFAULT
        try:
            raw = float(self._absent_threshold_source())
        except Exception:
            _log.warning("invariants: absent_threshold lookup failed", exc_info=True)
            return INV2_ABSENT_THRESHOLD_DEFAULT
        if not math.isfinite(raw) or raw <= 0:
            return INV2_ABSENT_THRESHOLD_DEFAULT
        return raw

    def absent_workers(self) -> set[str]:
        """Workers that are ABSENT rather than merely paused (#1538, #1571).

        The distinction INV-2 actually needs is OWNED vs ABANDONED, not BUSY vs IDLE.
        RESTING alone is a pause and must not cost a worker its ACTIVE row; absence is
        a dead process (STUNG) or a silence long enough to mean the work was dropped.

        #1571 SPLIT THIS OFF FROM THE DISPLAY THRESHOLD, and the measurement is why.
        #1538 keyed absence on SLEEPING to avoid inventing a second definition that
        could drift — good reasoning, but it assumed the display value was ~20 minutes.
        Measured against 45 RESTING episodes where the worker held an ACTIVE task and
        then resumed, 20 minutes is wrong 24.4% of the time; an hour is wrong 4.4%.
        Confirmed live: all 15 real INV-2 demotions after #1538 shipped were followed by
        the same worker returning to that same task, none of them absent.

        The two knobs have genuinely different jobs. ``sleeping_threshold`` decides when
        a tile looks asleep, where 20 minutes is right and an hour would be a bad
        dashboard. This decides when the daemon overrules a worker about what it is
        working on, where the cost of being early is a task silently taken away. While
        they were one knob, lowering the display one silently re-armed task demotion —
        the trap that survived raising it from 300 to 1200 on #1571.

        DELIBERATELY COMPUTED FROM ``state``, NOT ``display_state``. Two consequences
        that were previously implicit and are now explicit:

        * STUNG is absence with nothing to wait for, so it does not go through the
          timer — gating a dead process behind an hour would delay #405's repair for
          the one case that is certain.
        * The Queen is exempt from the timer (always-on by design), which used to ride
          on ``display_state`` never returning SLEEPING for her. Reading ``state``
          directly would have dropped that silently, so it is spelled out here. She is
          still demoted when STUNG — the exemption is about idleness, not immortality.
        """
        threshold = self._absent_threshold()
        absent: set[str] = set()
        for w in self._get_workers():
            if w.state == WorkerState.STUNG:
                absent.add(w.name)
            elif (
                not w.is_queen and w.state == WorkerState.RESTING and w.state_duration >= threshold
            ):
                absent.add(w.name)
        return absent

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
