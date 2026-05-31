"""Idle-watcher drone — nudge RESTING workers sitting on assigned tasks.

Phase 2 of task #225. Phase 1 of the same ticket fixed the common case —
``swarm_create_task(target_worker=X)`` now dispatches the task into X's PTY
on assignment. But that only covers the happy path. If a worker drops a
task mid-turn (crash, compact, network hiccup) or the Queen hand-assigns
via a path that doesn't go through ``assign_and_start_task``, the worker
can still end up RESTING with an ASSIGNED/IN_PROGRESS task it's not
actually working on. This watcher sweeps periodically and catches those.

Scope: intentionally narrow. The watcher doesn't diagnose — it just pokes
the worker with a pointer at its own tools (``swarm_task_status mine``,
``swarm_check_messages``) so the worker can decide whether to resume or
report a blocker. Every nudge is logged to the buzz log so the operator
can tune cadence or catch runaway prompting.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from swarm.drones.log import DroneAction, LogCategory, SystemAction
from swarm.drones.nudge_guard import ESCALATE, SILENT, RepeatNudgeGuard
from swarm.logging import get_logger
from swarm.worker.worker import WorkerState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from swarm.config import DroneConfig
    from swarm.drones.log import DroneLog
    from swarm.tasks.blockers import Blocker, BlockerStore
    from swarm.tasks.board import TaskBoard
    from swarm.tasks.task import SwarmTask
    from swarm.worker.worker import Worker


_log = get_logger("drones.idle_watcher")

# After firing /mcp the worker spends a moment showing/dismissing the MCP
# dialog. Wait this many seconds before sending the regular task nudge so
# Claude Code has time to settle back at an empty prompt and re-establish
# its MCP transport. Without the follow-up the worker would sit idle until
# the next sweep (default 180s) — task #315.
_MCP_FOLLOWUP_DELAY_SECONDS = 5.0


# States where a worker is "idle" from the watcher's perspective.  BUZZING
# means the worker is already producing output so we leave it alone.
# WAITING is an approval prompt — operator/drone rules handle that path.
# STUNG means the worker process has exited; revive is a separate concern.
_IDLE_STATES: frozenset[WorkerState] = frozenset({WorkerState.RESTING, WorkerState.SLEEPING})


def _nudge_message(task_numbers: list[int]) -> str:
    """Build the PTY message sent to an idle worker.

    Kept short and tool-centric — we want the worker to call its existing
    status + message MCP tools rather than treat this like a new prompt.
    """
    if len(task_numbers) == 1:
        task_ref = f"#{task_numbers[0]}"
    else:
        task_ref = ", ".join(f"#{n}" for n in task_numbers)
    return (
        f"You have {task_ref} active but appear idle. "
        "Run `swarm_task_status filter=mine` and `swarm_check_messages`, "
        "then resume or report a blocker."
    )


class IdleWatcher:
    """Periodic sweep: idle workers with active tasks get a nudge.

    Parameters
    ----------
    drone_config:
        Owns ``idle_nudge_interval_seconds`` and
        ``idle_nudge_debounce_seconds``. ``interval <= 0`` disables the
        watcher entirely.
    task_board:
        Source of truth for "does this worker have an active task".
    drone_log:
        Every nudge is appended as ``AUTO_NUDGE`` under ``LogCategory.DRONE``.
    send_to_worker:
        Async callable ``(worker_name, message, *, _log_operator=False) -> None``.
        Mirrors ``SwarmDaemon.send_to_worker`` — injected so tests can
        substitute a fake without dragging in a full daemon.
    rate_limit_check:
        Optional ``(worker_name) -> bool``.  Returning ``True`` skips
        the nudge for that worker (e.g. hit the 5hr Claude quota —
        prompting would stack stale work behind a dead quota).
    """

    def __init__(
        self,
        *,
        drone_config: DroneConfig,
        task_board: TaskBoard | None,
        drone_log: DroneLog,
        send_to_worker: Callable[..., Awaitable[None]],
        rate_limit_check: Callable[[str], bool] | None = None,
        blocker_store: BlockerStore | None = None,
        message_has_newer: Callable[[str, float], bool] | None = None,
        mcp_activity_lookup: Callable[[str], float | None] | None = None,
        daemon_start_time: float | None = None,
        mcp_followup_delay_seconds: float = _MCP_FOLLOWUP_DELAY_SECONDS,
        escalate_to_operator: Callable[[str, str], None] | None = None,
    ) -> None:
        self._config = drone_config
        self._task_board = task_board
        self._drone_log = drone_log
        self._send_to_worker = send_to_worker
        self._rate_limit_check = rate_limit_check
        # Task #315: how long to wait between firing /mcp and the
        # follow-up task nudge. Overridable so tests can run with 0
        # without sleeping for real wall time.
        self._mcp_followup_delay = mcp_followup_delay_seconds
        # Track in-flight follow-up tasks so they aren't garbage-collected
        # mid-sleep and so daemon shutdown can cancel them cleanly.
        self._mcp_followups: set[asyncio.Task[None]] = set()
        # Task #250: worker-reported blockers. When a worker calls
        # ``swarm_report_blocker`` we store "worker X is blocked on task
        # #Y until Y completes OR a new message lands"; the watcher
        # skips that worker's nudge until one of those clears.
        # ``message_has_newer(worker, since_ts)`` returns True if the
        # worker has any message newer than ``since_ts`` — typically
        # wired to ``message_store`` at the daemon level, left None in
        # tests that don't exercise the message-clear path.
        self._blocker_store = blocker_store
        self._message_has_newer = message_has_newer
        # Task #257: MCP tools-dropped detection. When the daemon reloads,
        # Claude Code's HTTP MCP transport can give up reconnecting after
        # its retry ceiling. If a worker sits idle through a reload, its
        # client-side tool registry is empty and the normal nudge above is
        # useless (worker can't call swarm_check_messages / task_status).
        # Recovery: detect the state (no MCP activity since daemon start
        # *and* unread inbox) and inject ``/mcp\n`` via PTY to force
        # re-initialize client-side.  ``mcp_activity_lookup(worker_name)``
        # returns the worker's most recent MCP dispatch timestamp (wall
        # time) or None.  ``daemon_start_time`` is the daemon's own boot
        # timestamp.  Both None = feature disabled.
        self._mcp_activity_lookup = mcp_activity_lookup
        self._daemon_start_time = daemon_start_time
        # (worker_name, task_id) → last-nudge monotonic timestamp
        self._last_nudge: dict[tuple[str, str], float] = {}
        # worker_name → monotonic timestamp of last MCP-refresh injection.
        # Debounced separately from the regular nudge because we want at
        # most one ``/mcp`` injection per worker per boot cycle.
        self._mcp_refresh_fired: set[str] = set()
        # Two-strike rule (operator feedback 2026-05-01): "no MCP activity
        # since daemon boot" alone is too coarse — a worker that's just
        # legitimately parked on a task (no tool call yet) trips the same
        # signal as a worker whose Claude Code transport actually died.
        # First sweep records the strike and falls through to the normal
        # task nudge; if the transport is fine the worker answers the
        # nudge with an MCP call and ``_needs_mcp_refresh`` flips to
        # False. Only a second sweep that *still* sees zero activity fires
        # ``/mcp``.
        self._mcp_first_strike: set[str] = set()
        self._last_sweep: float = 0.0
        # Task #546: stop nudging + escalate to operator after
        # idle_nudge_max_repeats consecutive no-progress nudges, instead
        # of looping forever on a task the worker can't progress.
        # ``escalate_to_operator(worker_name, detail)`` surfaces one
        # operator-facing attention item; None disables escalation (the
        # guard then still caps the loop by going SILENT, just without an
        # operator ping — e.g. in tests).
        self._escalate_to_operator = escalate_to_operator
        self._nudge_guard = RepeatNudgeGuard()

    @property
    def interval_seconds(self) -> float:
        return float(self._config.idle_nudge_interval_seconds or 0.0)

    @property
    def debounce_seconds(self) -> float:
        return float(self._config.idle_nudge_debounce_seconds or 0.0)

    @property
    def _max_repeats(self) -> int:
        """Task #546: consecutive no-progress nudges before escalate-and-quiet.
        Read live from config so hot-reload picks it up; 0 disables the cap."""
        return int(getattr(self._config, "idle_nudge_max_repeats", 0) or 0)

    @property
    def enabled(self) -> bool:
        return self.interval_seconds > 0 and self._task_board is not None

    def due(self, *, now: float | None = None) -> bool:
        """Has enough wall time elapsed since the last sweep?"""
        if not self.enabled:
            return False
        now = now if now is not None else time.monotonic()
        return (now - self._last_sweep) >= self.interval_seconds

    async def sweep(self, workers: list[Worker], *, now: float | None = None) -> int:
        """Run one sweep.  Returns the number of nudges actually sent.

        Safe to call more often than ``interval_seconds``; no-ops when not
        due. Caller can force a sweep by passing a ``now`` value that pushes
        past the threshold.
        """
        if not self.enabled:
            return 0
        now = now if now is not None else time.monotonic()
        if (now - self._last_sweep) < self.interval_seconds:
            return 0
        self._last_sweep = now

        sent = 0
        tasks_by_worker = self._bucket_active_tasks_by_worker()
        for worker in workers:
            if not self._should_nudge(worker, now=now):
                continue
            active = tasks_by_worker.get(worker.name, [])
            if not active:
                continue
            # Task #250: worker-reported blocker takes precedence over
            # the nudge. If the blocker store says this worker is still
            # blocked (task not completed, no new messages since the
            # report), skip the nudge + log an AUTO_NUDGE_SKIPPED entry
            # so the audit trail shows WHY the worker wasn't nudged.
            blocker = self._active_blocker(worker.name)
            if blocker is not None:
                self._drone_log.add(
                    SystemAction.AUTO_NUDGE_SKIPPED,
                    worker.name,
                    f"reported blocker on #{blocker.task_number} "
                    f"(waiting on #{blocker.blocked_by_task})",
                    category=LogCategory.DRONE,
                )
                continue
            # Task #257: detect the "client-side MCP tools dropped after
            # daemon reload" state.  If this worker hasn't made any MCP
            # calls since the daemon started, the normal nudge is
            # useless (the worker can't call ``swarm_check_messages`` or
            # ``swarm_task_status`` — the client tool registry is empty).
            # Two-strike rule: the first sighting falls through to the
            # normal nudge so a worker with a healthy transport gets a
            # chance to answer (its MCP call clears the stale signal).
            # Only the second consecutive sighting injects ``/mcp``.
            if self._needs_mcp_refresh(worker.name):
                if worker.name in self._mcp_first_strike:
                    await self._fire_mcp_refresh(worker.name)
                    continue
                self._mcp_first_strike.add(worker.name)
            numbers = sorted({t.number for t in active})
            # Debounce per (worker, task_id) — don't spam the same work.
            task_ids = [t.id for t in active]
            fresh_keys = [
                (worker.name, tid) for tid in task_ids if self._is_fresh(worker.name, tid, now=now)
            ]
            if not fresh_keys:
                continue
            if await self._dispatch_or_escalate(worker, active, numbers, fresh_keys, now=now):
                sent += 1
        return sent

    async def _dispatch_or_escalate(
        self,
        worker: Worker,
        active: list[SwarmTask],
        numbers: list[int],
        fresh_keys: list[tuple[str, str]],
        *,
        now: float,
    ) -> bool:
        """A nudge is due for ``worker``; send it, or escalate + go quiet.

        Task #546: consult the repeat-guard. If the worker has been nudged
        ``idle_nudge_max_repeats`` times with no progress, stop poking and
        escalate to the operator once (then stay SILENT until something
        changes). Otherwise send the normal nudge. Returns True only when
        a real nudge was sent (so the caller's ``sent`` tally stays
        accurate). The fingerprint captures "did anything change worth
        re-nudging": worker display-state + each active task's
        (number, status).
        """
        fingerprint = (
            worker.display_state.value,
            tuple(sorted((t.number, t.status.value) for t in active)),
        )
        decision = self._nudge_guard.decide(worker.name, fingerprint, max_repeats=self._max_repeats)
        # Mark the debounce in all branches so the guard is re-consulted at
        # most once per debounce window, not on every sweep.
        for key in fresh_keys:
            self._last_nudge[key] = now
        if decision == SILENT:
            return False  # already escalated; quiet until fingerprint changes
        if decision == ESCALATE:
            self._escalate(worker.name, numbers)
            return False
        # NUDGE → normal poke.
        message = _nudge_message(numbers)
        try:
            await self._send_to_worker(worker.name, message, _log_operator=False)
        except Exception:
            # Don't let one failed worker kill the sweep — log and move on.
            _log.warning("idle_watcher: send_to_worker failed for %s", worker.name, exc_info=True)
            return False
        self._drone_log.add(
            DroneAction.AUTO_NUDGE,
            worker.name,
            f"idle with active task(s): {', '.join(f'#{n}' for n in numbers)}",
            category=LogCategory.DRONE,
        )
        return True

    def _escalate(self, worker_name: str, numbers: list[int]) -> None:
        """Stop nudging ``worker_name`` and surface one operator attention
        item (task #546). Best-effort — a callback failure must not break
        the sweep."""
        detail = (
            f"idle on {', '.join(f'#{n}' for n in numbers)} across "
            f"{self._max_repeats} nudges with no progress — escalated to operator"
        )
        self._drone_log.add(
            SystemAction.AUTO_NUDGE_ESCALATED,
            worker_name,
            detail,
            category=LogCategory.DRONE,
        )
        if self._escalate_to_operator is not None:
            try:
                self._escalate_to_operator(worker_name, detail)
            except Exception:
                _log.debug(
                    "idle_watcher: escalate_to_operator raised for %s",
                    worker_name,
                    exc_info=True,
                )

    def _bucket_active_tasks_by_worker(self) -> dict[str, list]:
        """Snapshot the board's active tasks once and group by assignee.

        Calling ``active_tasks_for_worker`` inside the sweep loop was O(W·T) —
        each call re-snapshotted the full task dict under the board lock.
        One pass over ``active_tasks`` plus dict lookups in the loop drops
        that to O(T) regardless of worker count.
        """
        bucketed: dict[str, list] = {}
        for t in self._task_board.active_tasks:
            if t.assigned_worker:
                bucketed.setdefault(t.assigned_worker, []).append(t)
        return bucketed

    def _should_nudge(self, worker: Worker, *, now: float) -> bool:
        """Cheap filters applied BEFORE we look at the task board."""
        if worker.display_state not in _IDLE_STATES:
            return False
        if self._rate_limit_check is not None:
            try:
                if self._rate_limit_check(worker.name):
                    return False
            except Exception:
                _log.debug(
                    "idle_watcher: rate_limit_check raised for %s", worker.name, exc_info=True
                )
                return False
        return True

    def _active_blocker(self, worker_name: str) -> Blocker | None:
        """Return the first still-active blocker for ``worker_name``, or None.

        Delegates to :meth:`BlockerStore.has_active_blocker`, wiring in
        "is this task-number completed?" via the task board and "has
        a new message arrived?" via ``message_has_newer``. Both
        auto-clear paths run inside the store call.
        """
        if self._blocker_store is None:
            return None

        def _is_completed(task_number: int) -> bool:
            board = self._task_board
            if board is None:
                return False
            for t in getattr(board, "all_tasks", []):
                if t.number == task_number:
                    return t.status.value == "done"
            return False

        def _on_auto_clear(b: Blocker, reason: str) -> None:
            """Task #529: surface the auto-clear in the buzz log so an
            operator audit can see WHY a previously-blocked worker is
            being nudged again (without this, the only signal is the
            ABSENCE of subsequent AUTO_NUDGE_SKIPPED entries — easy to
            miss). ``reason`` is one of ``target_done`` (the blocker
            target task became done/etc.) or ``message_since`` (new
            inbox traffic landed after the blocker was filed)."""
            self._drone_log.add(
                SystemAction.BLOCKER_AUTO_CLEARED,
                worker_name,
                (
                    f"blocker on #{b.task_number} cleared "
                    f"(reason={reason}, target=#{b.blocked_by_task})"
                ),
                category=LogCategory.DRONE,
            )

        return self._blocker_store.has_active_blocker(
            worker_name,
            is_task_completed=_is_completed,
            has_message_since=self._message_has_newer,
            on_auto_clear=_on_auto_clear,
        )

    def _is_fresh(self, worker_name: str, task_id: str, *, now: float) -> bool:
        """True when ``(worker, task)`` hasn't been nudged within the debounce."""
        last = self._last_nudge.get((worker_name, task_id))
        if last is None:
            return True
        if self.debounce_seconds <= 0:
            return True
        return (now - last) >= self.debounce_seconds

    def _needs_mcp_refresh(self, worker_name: str) -> bool:
        """True when ``worker_name`` has probably lost its client-side MCP tools.

        Criteria (all must hold):
        - MCP-activity tracking is wired (``mcp_activity_lookup`` +
          ``daemon_start_time`` both set).
        - This boot cycle hasn't already fired a refresh for this worker.
        - The worker has made zero MCP calls since the daemon started
          (either no record at all, or the last timestamp predates
          ``daemon_start_time``).

        The "worker has active tasks" check is done by the caller — we
        only fire the refresh on workers the watcher would have nudged
        anyway, so a genuinely idle-with-nothing-to-do worker doesn't
        get pinged for no reason.
        """
        if self._mcp_activity_lookup is None or self._daemon_start_time is None:
            return False
        if worker_name in self._mcp_refresh_fired:
            return False
        last_mcp = self._mcp_activity_lookup(worker_name)
        if last_mcp is None:
            return True
        return last_mcp < self._daemon_start_time

    async def _fire_mcp_refresh(self, worker_name: str) -> None:
        """Inject ``/mcp`` into the worker's PTY and log the intervention.

        Claude Code's ``/mcp`` slash command forces a full MCP client
        re-initialize (re-fetches ``tools/list``, reconnects transports,
        refreshes the tool registry). On success the worker's tool
        surface is restored and future sweeps will trip normal nudge
        behaviour instead of landing here.

        After firing /mcp we schedule a delayed follow-up that sends the
        regular task nudge (task #315). Without it the worker would sit
        at an empty post-dialog prompt until the next sweep —
        ``idle_nudge_interval_seconds`` (default 180s) — which the
        operator perceives as the worker being "stranded".
        """
        self._mcp_refresh_fired.add(worker_name)
        try:
            await self._send_to_worker(worker_name, "/mcp", _log_operator=False)
        except Exception:
            _log.warning("idle_watcher: mcp refresh send failed for %s", worker_name, exc_info=True)
            # Don't leave the refresh flag set on failure — next sweep
            # can retry rather than silently giving up.
            self._mcp_refresh_fired.discard(worker_name)
            return
        self._drone_log.add(
            SystemAction.MCP_TOOLS_STALE,
            worker_name,
            "no MCP activity since daemon start — injected /mcp to force re-init",
            category=LogCategory.MCP,
        )
        # Schedule the follow-up nudge so the worker doesn't sit idle for
        # a full sweep interval after dismissing the dialog. Fire-and-
        # forget; we hold a reference in ``_mcp_followups`` so the task
        # isn't garbage-collected before it runs.
        followup = asyncio.create_task(self._followup_nudge_after_mcp(worker_name))
        self._mcp_followups.add(followup)
        followup.add_done_callback(self._mcp_followups.discard)

    async def _followup_nudge_after_mcp(self, worker_name: str) -> None:
        """Send the regular task nudge a few seconds after ``/mcp`` fires.

        Re-queries the task board at fire time so a task completed/cancelled
        in the interim is respected. Updates ``_last_nudge`` so the regular
        sweep debounce treats this as the worker's most recent nudge.
        """
        try:
            if self._mcp_followup_delay > 0:
                await asyncio.sleep(self._mcp_followup_delay)
        except asyncio.CancelledError:
            return
        if self._task_board is None:
            return
        active = self._task_board.active_tasks_for_worker(worker_name)
        if not active:
            return
        numbers = sorted({t.number for t in active})
        task_ids = [t.id for t in active]
        message = _nudge_message(numbers)
        try:
            await self._send_to_worker(worker_name, message, _log_operator=False)
        except Exception:
            _log.warning(
                "idle_watcher: post-/mcp follow-up nudge failed for %s",
                worker_name,
                exc_info=True,
            )
            return
        now = time.monotonic()
        for tid in task_ids:
            self._last_nudge[(worker_name, tid)] = now
        self._drone_log.add(
            DroneAction.AUTO_NUDGE,
            worker_name,
            f"post-/mcp follow-up: active task(s) {', '.join(f'#{n}' for n in numbers)}",
            category=LogCategory.DRONE,
        )
