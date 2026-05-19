"""OversightHandler — signal-triggered monitoring and intervention dispatch."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from swarm.drones.log import LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.drones.log import DroneLog
    from swarm.queen.oversight import OversightMonitor, OversightResult
    from swarm.queen.queen import Queen
    from swarm.tasks.board import TaskBoard

_log = get_logger("drones.oversight")


class OversightHandler:
    """Handles oversight signal detection and Queen evaluation.

    Extracted from :class:`~swarm.drones.pilot.DronePilot` to reduce
    pilot.py complexity.
    """

    def __init__(
        self,
        workers: list[Worker],
        log: DroneLog,
        queen: Queen | None,
        task_board: TaskBoard | None,
        oversight_monitor: OversightMonitor | None,
        emit: Callable[..., None],
        capture_outputs: Callable[[], dict[str, str]],
    ) -> None:
        self.workers = workers
        self.log = log
        self.queen = queen
        self.task_board = task_board
        self._oversight = oversight_monitor
        self._emit = emit
        self._capture_outputs = capture_outputs

    def set_oversight(self, monitor: OversightMonitor | None) -> None:
        """Update the oversight monitor reference."""
        self._oversight = monitor

    async def oversight_cycle(self) -> bool:
        """Run oversight signal detection and Queen evaluation.

        Returns ``True`` if any intervention was triggered.
        """
        monitor = self._oversight
        if monitor is None or not monitor.enabled or not self.queen:
            return False

        worker_outputs = self._capture_outputs()
        signals = monitor.collect_signals(self.workers, self.task_board, worker_outputs)

        # Operator-blocked-stall guard (deterministic, Queen-free so it
        # survives a rate-limit storm): raise ONE park proposal per
        # stalled ACTIVE task and suppress this cycle's drift
        # intervention for those workers (parking IS the intervention).
        had_action = False
        parked_workers: set[str] = set()
        for wname, task_id, reason in monitor.collect_park_proposals(self.workers, self.task_board):
            worker = next((w for w in self.workers if w.name == wname), None)
            if worker is None:
                continue
            self._emit("park_proposal", worker, task_id, reason)
            self.log.add(
                SystemAction.PARK_PROPOSED,
                wname,
                reason,
                category=LogCategory.QUEEN,
                is_notification=True,
            )
            parked_workers.add(wname)
            had_action = True

        signals = [s for s in signals if s.worker_name not in parked_workers]
        if not signals:
            return had_action

        for signal in signals:
            self.log.add(
                SystemAction.OVERSIGHT_SIGNAL,
                signal.worker_name,
                f"{signal.signal_type.value}: {signal.description}",
                category=LogCategory.QUEEN,
            )

            output = worker_outputs.get(signal.worker_name, "")
            task_info = ""
            if signal.task_id and self.task_board:
                task = self.task_board.get(signal.task_id)
                if task:
                    task_info = f"{task.title}: {task.description}"

            result = await monitor.evaluate_signal(signal, self.queen, output, task_info)
            if result is None:
                self.log.add(
                    SystemAction.OVERSIGHT_RATE_LIMITED,
                    signal.worker_name,
                    f"oversight rate limited: {signal.signal_type.value}",
                    category=LogCategory.QUEEN,
                )
                continue

            acted = await self._handle_oversight_result(result)
            if acted:
                had_action = True

        return had_action

    async def _handle_oversight_result(self, result: OversightResult) -> bool:
        """Execute the intervention recommended by oversight evaluation."""
        from swarm.queen.oversight import Severity

        worker = next(
            (w for w in self.workers if w.name == result.signal.worker_name),
            None,
        )
        if not worker:
            return False

        # Operator-engagement gate (task #340): if the operator has typed in
        # the worker's PTY within the configured window, a periodic drift
        # signal must not interrupt them. Hard precondition gate — applied
        # before logging the intervention so the audit trail reflects the
        # skip rather than a phantom redirect.
        if result.action == "redirect" and worker.process and self._oversight is not None:
            window_min = self._oversight._config.operator_engagement_minutes
            if window_min > 0 and worker.process.operator_engaged_within(window_min * 60):
                self.log.add(
                    SystemAction.OVERSIGHT_INTERVENTION_SKIPPED,
                    worker.name,
                    f"redirect skipped — operator engaged within {window_min:.0f}m",
                    category=LogCategory.QUEEN,
                    is_notification=False,
                )
                _log.info(
                    "oversight redirect skipped for %s: operator engaged within %.0fm",
                    worker.name,
                    window_min,
                )
                return False

        detail = f"oversight {result.severity.value}: {result.action} — {result.reasoning}"
        self.log.add(
            SystemAction.OVERSIGHT_INTERVENTION,
            worker.name,
            detail,
            category=LogCategory.QUEEN,
            is_notification=result.severity != Severity.MINOR,
        )

        if result.action == "note":
            _log.info("oversight note for %s: %s", worker.name, result.message[:80])
            return True

        elif result.action == "redirect" and worker.process:
            if not worker.process.is_user_active and result.message:
                # Use Escape (not SIGINT) to interrupt Claude safely.
                # SIGINT kills the entire process group and can crash Claude.
                await worker.process.send_escape()
                # Wait for Claude to process the escape and return to a prompt.
                # send_escape is async-safe but Claude needs time to stop its
                # current operation before it can accept new input.
                for _ in range(5):
                    await asyncio.sleep(1.0)
                    if not worker.process.is_alive:
                        _log.warning(
                            "oversight redirect aborted for %s: process died after escape",
                            worker.name,
                        )
                        return False
                    if worker.state != WorkerState.BUZZING:
                        break
                # Final safety: verify Claude is still alive before sending
                if not worker.process.is_alive:
                    _log.warning(
                        "oversight redirect aborted for %s: process not alive",
                        worker.name,
                    )
                    return False
                # Send as a single-line message — no embedded newlines that
                # bash could interpret as separate commands if Claude exits.
                clean_msg = result.message.replace("\n", " ").strip()
                await worker.process.send_keys(clean_msg)
                _log.info(
                    "oversight redirected %s: %s",
                    worker.name,
                    clean_msg[:80],
                )
                return True

        elif result.action == "flag_human":
            self._emit(
                "oversight_alert",
                worker,
                result.signal,
                result,
            )
            _log.info(
                "oversight flagged %s for human review: %s",
                worker.name,
                result.reasoning[:80],
            )
            return True

        return False
