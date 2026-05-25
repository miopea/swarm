"""PollDispatcher — poll loop lifecycle, backoff, and per-tick orchestration."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from swarm.drones.backoff import compute_backoff
from swarm.drones.log import DroneAction
from swarm.logging import get_logger
from swarm.pty.process import ProcessError
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from swarm.drones.pilot import DronePilot

_log = get_logger("drones.poll_dispatcher")


class PollDispatcher:
    """Owns the poll loop, backoff, error tracking, and per-tick dispatch.

    Extracted from :class:`~swarm.drones.pilot.DronePilot` to reduce
    pilot.py complexity.
    """

    def __init__(self, pilot: DronePilot) -> None:
        self._pilot = pilot
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._tick: int = 0
        self._idle_streak: int = 0
        self._poll_lock = asyncio.Lock()
        self._poll_failures: dict[str, tuple[int, float]] = {}
        self._consecutive_errors: int = 0
        self._all_done_streak: int = 0

    # --- Lifecycle ---

    def start(self) -> None:
        """Start the poll loop task."""
        self._running = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.loop())
            self._task.add_done_callback(self._on_loop_done)

    def stop(self) -> None:
        """Stop the poll loop task."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    @staticmethod
    def _on_loop_done(task: asyncio.Task[None]) -> None:
        """Log when the poll loop task finishes unexpectedly."""
        if task.cancelled():
            _log.info("poll loop task was cancelled")
        elif exc := task.exception():
            _log.error("poll loop task died with exception: %s", exc, exc_info=exc)
        else:
            _log.info("poll loop task exited normally")

    # --- Public API ---

    async def poll_once(self) -> bool:
        """Run one poll cycle across all workers."""
        # Revive poll loop if it died unexpectedly
        if self._running and (self._task is None or self._task.done()):
            _log.warning("poll loop was dead — restarting")
            self._task = asyncio.create_task(self.loop())
        if self._poll_lock.locked():
            return False  # Another poll is in progress — skip
        async with self._poll_lock:
            had_action, _any_state_changed = await self.poll_once_locked()
            self._consecutive_errors = 0
            return had_action

    async def poll_once_locked(self) -> tuple[bool, bool]:
        """Returns (had_action, any_state_changed)."""
        p = self._pilot
        any_state_changed = False
        dead_workers: list[Worker] = []
        had_action = False
        max_poll_failures = p.drone_config.max_poll_failures
        p._deferred_actions = []
        now = time.time()

        for worker in list(p.workers):
            if p._state_tracker._is_suspended_skip(worker, now=now):
                continue

            try:
                # ``enabled`` gates _run_decision_sync inside the tracker; it
                # used to be threaded through the pilot's _poll_single_worker
                # shim which is now gone, so the dispatcher passes it
                # explicitly. Missing it silently defaults to True and makes
                # disabled-pilot tests fire CONTINUE/etc. against WAITING
                # workers (seen in the terminal-approval suite).
                action, _transitioned, changed = p._state_tracker._poll_single_worker(
                    worker, dead_workers, now=now, enabled=p.enabled
                )
                had_action |= action
                any_state_changed |= changed
                # Successful poll — clear any failure counter
                self._poll_failures.pop(worker.name, None)
            except (ProcessError, OSError) as exc:
                import time as _time

                prev_fails, _ = self._poll_failures.get(worker.name, (0, 0.0))
                fails = prev_fails + 1
                self._poll_failures[worker.name] = (fails, _time.monotonic())
                # ConnectionError / timeout → transient; likely holder hiccup
                is_transient = isinstance(exc, (ConnectionError, TimeoutError))
                _log.warning(
                    "poll failed for %s (%d/%d, %s)",
                    worker.name,
                    fails,
                    max_poll_failures,
                    "transient" if is_transient else "permanent",
                    exc_info=True,
                )
                # Transient errors get double the threshold before tripping
                threshold = max_poll_failures * 2 if is_transient else max_poll_failures
                if fails >= threshold:
                    _log.warning(
                        "circuit breaker tripped for %s — treating as dead",
                        worker.name,
                    )
                    dead_workers.append(worker)
                    had_action = True

        # Execute deferred async actions (send_enter, revive)
        await p._execute_deferred_actions()

        if dead_workers:
            self._cleanup_dead_workers(dead_workers)

        if await self._run_periodic_tasks():
            had_action = True

        # Speculative task preparation for idle workers
        if p.enabled and p.task_board:
            await self._speculate_for_idle_workers()

        self._tick += 1
        return had_action, any_state_changed

    def _cleanup_dead_workers(self, dead_workers: list[Worker]) -> None:
        """Remove dead workers from tracking and unassign their tasks."""
        p = self._pilot
        for dw in dead_workers:
            p.workers.remove(dw)
            p._state_tracker.cleanup_dead_worker(dw)
            self._poll_failures.pop(dw.name, None)
            _log.info("removed dead worker: %s", dw.name)
            if p.task_board:
                p.task_board.unassign_worker(dw.name)
        p.emit("workers_changed")

    async def _run_periodic_tasks(self) -> bool:
        """Run periodic background tasks: completions, auto-assign, coordination."""
        p = self._pilot
        had_action = False
        # Periodic cleanup of stale proposed-completion entries
        if self._tick > 0 and self._tick % p._PROPOSED_COMPLETION_CLEANUP_INTERVAL == 0:
            p._cleanup_stale_proposed_completions()
        if p.enabled and p.task_board:
            if p._check_task_completions():
                had_action = True
        # Auto-assign: skip when Queen has auto_assign_tasks disabled
        p._needs_assign_check = False
        if p.enabled and p.task_board and p.queen and p.queen.auto_assign_tasks:
            if await p._auto_assign_tasks():
                had_action = True
        # Periodic hive-coordination cycle removed (task #253 spec B).
        # Coverage is duplicated by specialized drones — see
        # ``docs/specs/headless-queen-architecture.md``.
        # Oversight: signal-triggered Queen monitoring
        if (
            p.enabled
            and p.queen
            and p._oversight
            and self._tick > 0
            and self._tick % p._oversight_interval == 0
        ):
            if await p._oversight_cycle():
                had_action = True
        if await self._run_idle_watcher_sweep():
            had_action = True
        if await self._run_inter_worker_watcher_sweep():
            had_action = True
        if await self._run_context_pressure_sweep():
            had_action = True
        if await self._run_dreamer_sweep():
            had_action = True
        return had_action

    async def _run_idle_watcher_sweep(self) -> bool:
        """Run the idle-watcher sweep (task #225 Phase 2).

        Wall-clock driven, not tick-driven — ``sweep()`` internally no-ops
        when the configured interval hasn't elapsed. Errors are swallowed
        with a warning so one bad sweep can't take down the poll loop.
        """
        p = self._pilot
        if not (p.enabled and p.idle_watcher.enabled):
            return False
        try:
            return bool(await p.idle_watcher.sweep(p.workers))
        except Exception:
            _log.warning("idle_watcher sweep failed", exc_info=True)
            return False

    async def _run_inter_worker_watcher_sweep(self) -> bool:
        """Run the inter-worker message watcher (task #235 Phase 3).

        Same wall-clock cadence as the idle watcher (shared config), and
        same fault-isolation discipline: a single bad sweep can't take
        down the poll loop.
        """
        p = self._pilot
        if not (p.enabled and p.inter_worker_watcher.enabled):
            return False
        try:
            return bool(await p.inter_worker_watcher.sweep(p.workers))
        except Exception:
            _log.warning("inter_worker_watcher sweep failed", exc_info=True)
            return False

    async def _run_dreamer_sweep(self) -> bool:
        """Run the Dreamer pattern-mining sweep.

        Wall-clock driven on a much longer cadence (default 4h) than
        the idle / inter-worker watchers, which is enforced by the
        dreamer's own ``due()`` check. Same fault-isolation pattern —
        a failed mining pass can't take down the poll loop.
        """
        p = self._pilot
        dreamer = getattr(p, "dreamer", None)
        if dreamer is None or not (p.enabled and dreamer.enabled):
            return False
        try:
            return bool(await dreamer.sweep())
        except Exception:
            _log.warning("dreamer sweep failed", exc_info=True)
            return False

    async def _run_context_pressure_sweep(self) -> bool:
        """Run the context-pressure watcher (item 3 of the 10-repo bundle).

        Tick-driven (once per poll cycle, not on a separate wall-clock
        cadence) — the watcher's hysteresis prevents repeat fires even
        when the dispatcher polls aggressively. Fault-isolated like
        the other sweep helpers so one bad worker can't kill the loop.
        """
        p = self._pilot
        if not (p.enabled and p.context_pressure_watcher.enabled):
            return False
        try:
            return bool(await p.context_pressure_watcher.sweep(p.workers))
        except Exception:
            _log.warning("context_pressure sweep failed", exc_info=True)
            return False

    def _compute_backoff(self) -> float:
        """Compute poll interval based on worker states and idle streak."""
        p = self._pilot
        return compute_backoff(
            workers=p.workers,
            config=p.drone_config,
            idle_streak=self._idle_streak,
            base_interval=p._base_interval,
            max_interval=p._max_interval,
            pressure_level=p._pressure_level,
            focused_workers=p._focused_workers,
            focus_interval=p._focus_interval,
        )

    def _handle_poll_error(self) -> None:
        """Track consecutive poll loop errors with escalating severity."""
        self._consecutive_errors += 1
        if self._consecutive_errors <= 5:
            _log.warning(
                "poll loop error (%d consecutive) — recovering next cycle",
                self._consecutive_errors,
                exc_info=True,
            )
        else:
            _log.error(
                "poll loop error (%d consecutive) — recovering next cycle",
                self._consecutive_errors,
                exc_info=True,
            )
        if self._consecutive_errors == 5:
            self._pilot.emit("poll_errors_exceeded", self._consecutive_errors)

    async def _speculate_for_idle_workers(self) -> None:
        """Pre-load task context on RESTING workers with matching pending tasks.

        Guardrails:
        1. Config toggle: ``speculation_enabled`` must be True in drones config
        2. Task-to-worker match: task's ``target_worker`` must name this worker
        3. Rate limit: skip workers recently rate-limited
        4. Operator activity: skip workers with active terminal sessions
        """
        p = self._pilot
        if not p.drone_config.speculation_enabled:
            return

        from swarm.tasks.task import TaskStatus

        for worker in p.workers:
            if worker.state != WorkerState.RESTING:
                continue
            if worker.speculating_task_id is not None:
                continue
            proc = worker.process
            if not proc:
                continue
            # Guard: skip if operator is actively using the terminal
            if proc.is_user_active:
                continue
            # Guard: skip if worker was recently rate-limited
            rl_time = p._state_tracker._rate_limit_seen.get(worker.name, 0)
            if rl_time and (time.time() - rl_time) < 300:
                continue
            # Guard: only speculate tasks targeted at this specific worker
            pending = [
                t
                for t in p.task_board.all_tasks
                if t.status == TaskStatus.UNASSIGNED
                and t.assigned_worker is None
                and t.target_worker == worker.name
            ]
            if not pending:
                continue
            task = pending[0]
            msg = (
                f"Prepare for upcoming task: {task.title}\n"
                f"{task.description}\n"
                f"Read relevant files but do not make changes yet."
            )
            try:
                await proc.send_keys(msg, enter=True)
                worker.speculating_task_id = task.id
                p.log.add(
                    DroneAction.CONTINUED,
                    worker.name,
                    f"speculating: pre-loading context for #{task.number}",
                    metadata={"source": "speculation"},
                )
            except (ProcessError, OSError):
                _log.debug("speculation failed for %s", worker.name)

    async def loop(self) -> None:
        """Main poll loop — runs until stopped."""
        p = self._pilot
        _log.info("poll loop started (enabled=%s, workers=%d)", p.enabled, len(p.workers))
        try:
            while self._running:
                backoff = p._base_interval
                try:
                    p._had_substantive_action = False
                    p._state_tracker.any_became_active = False
                    async with self._poll_lock:
                        _had_action, _any_changed = await self.poll_once_locked()

                        # Track idle streak for adaptive backoff.
                        if p._had_substantive_action or p._state_tracker.any_became_active:
                            self._idle_streak = 0
                        else:
                            self._idle_streak += 1

                        # Auto-terminate when all workers are gone
                        if not p.workers:
                            _log.warning("all workers gone — stopping pilot")
                            p.enabled = False
                            self._running = False
                            p.emit("hive_empty")
                            break

                        # Detect hive completion: all tasks done, all workers idle
                        if (
                            p.enabled
                            and p.drone_config.auto_stop_on_complete
                            and p.task_board
                            and p._saw_completion
                            and not p.task_board.available_tasks
                            and not p.task_board.active_tasks
                            and all(
                                w.display_state in (WorkerState.RESTING, WorkerState.SLEEPING)
                                for w in p.workers
                            )
                        ):
                            self._all_done_streak += 1
                            if self._all_done_streak >= 3:
                                _log.info("all tasks done, all workers idle — hive complete")
                                p.enabled = False
                                self._running = False
                                p.emit("hive_complete")
                                break
                        else:
                            self._all_done_streak = 0

                        backoff = self._compute_backoff()
                    self._consecutive_errors = 0
                except Exception:  # broad catch: poll loop must not die
                    self._handle_poll_error()

                await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            _log.debug("poll loop cancelled (shutdown)")
            raise
        except BaseException:
            _log.error("poll loop terminated unexpectedly", exc_info=True)
            raise
        finally:
            _log.info("poll loop exited (running=%s)", self._running)
