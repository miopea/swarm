"""Tests for :class:`swarm.drones.oversight_handler.OversightHandler`.

Unit-level coverage that complements the integration coverage in
``test_oversight.py`` (which exercises ``OversightMonitor``) and
``test_oversight_autopark.py``. The handler is the dispatch layer
between collected signals and worker-side intervention, so these
tests stub the monitor/queen/task_board to isolate the dispatch
behaviour.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from swarm.config import OversightConfig
from swarm.drones.log import DroneLog, LogCategory, SystemAction
from swarm.drones.oversight_handler import OversightHandler
from swarm.queen.oversight import (
    OversightMonitor,
    OversightResult,
    OversightSignal,
    Severity,
    SignalType,
)
from swarm.worker.worker import Worker, WorkerState


def _make_worker(name: str = "w1", state: WorkerState = WorkerState.BUZZING) -> Worker:
    w = Worker(name=name, path=f"/tmp/{name}")
    w.state = state
    return w


def _make_signal(
    *,
    worker_name: str = "w1",
    signal_type: SignalType = SignalType.PROLONGED_BUZZING,
    description: str = "buzzing too long",
    task_id: str = "",
) -> OversightSignal:
    return OversightSignal(
        signal_type=signal_type,
        worker_name=worker_name,
        description=description,
        task_id=task_id,
    )


def _make_result(
    signal: OversightSignal,
    *,
    severity: Severity = Severity.MAJOR,
    action: str = "note",
    message: str = "hello",
    reasoning: str = "because",
) -> OversightResult:
    return OversightResult(
        signal=signal,
        severity=severity,
        action=action,
        message=message,
        reasoning=reasoning,
    )


def _make_handler(
    workers: list[Worker],
    *,
    monitor: OversightMonitor | None = None,
    queen: Any = None,
    task_board: Any = None,
    capture_outputs: dict[str, str] | None = None,
) -> tuple[OversightHandler, MagicMock, DroneLog]:
    log = DroneLog()
    emit = MagicMock()
    captured = capture_outputs or {}
    handler = OversightHandler(
        workers=workers,
        log=log,
        queen=queen,
        task_board=task_board,
        oversight_monitor=monitor,
        emit=emit,
        capture_outputs=lambda: dict(captured),
    )
    return handler, emit, log


class TestOversightCycleGuards:
    """The cycle short-circuits when prerequisites are missing."""

    async def test_no_monitor_returns_false(self) -> None:
        handler, emit, _ = _make_handler([_make_worker()])
        assert await handler.oversight_cycle() is False
        emit.assert_not_called()

    async def test_monitor_disabled_returns_false(self) -> None:
        monitor = OversightMonitor(OversightConfig(enabled=False))
        handler, emit, _ = _make_handler([_make_worker()], monitor=monitor, queen=MagicMock())
        assert await handler.oversight_cycle() is False
        emit.assert_not_called()

    async def test_no_queen_returns_false(self) -> None:
        monitor = OversightMonitor(OversightConfig(enabled=True))
        handler, emit, _ = _make_handler([_make_worker()], monitor=monitor, queen=None)
        assert await handler.oversight_cycle() is False
        emit.assert_not_called()


class TestSetOversight:
    def test_replaces_monitor_reference(self) -> None:
        handler, _, _ = _make_handler([_make_worker()])
        replacement = OversightMonitor(OversightConfig(enabled=True))
        handler.set_oversight(replacement)
        # Internal attr is the load-bearing one; assert via behaviour:
        # with a queen now wired, an enabled monitor produces no early-out.
        # We can't trigger the real collect-signals path without a real
        # board, but the guard now passes the "monitor is None" check.
        assert handler._oversight is replacement


class TestParkProposals:
    """Park proposals fire ``park_proposal`` and log ``PARK_PROPOSED``."""

    async def test_park_proposal_emits_and_logs(self) -> None:
        worker = _make_worker("w-park")
        monitor = MagicMock()
        monitor.enabled = True
        monitor.collect_signals.return_value = []
        monitor.collect_park_proposals.return_value = [
            ("w-park", "task-abc", "blocked on operator")
        ]
        handler, emit, log = _make_handler([worker], monitor=monitor, queen=MagicMock())

        acted = await handler.oversight_cycle()

        assert acted is True
        emit.assert_called_once_with("park_proposal", worker, "task-abc", "blocked on operator")
        park_entries = [e for e in log.entries if e.action == SystemAction.PARK_PROPOSED]
        assert len(park_entries) == 1
        assert park_entries[0].worker_name == "w-park"
        assert park_entries[0].category == LogCategory.QUEEN

    async def test_park_proposal_for_unknown_worker_is_skipped(self) -> None:
        monitor = MagicMock()
        monitor.enabled = True
        monitor.collect_signals.return_value = []
        monitor.collect_park_proposals.return_value = [("ghost", "task-xyz", "vanished")]
        handler, emit, _ = _make_handler([_make_worker("w1")], monitor=monitor, queen=MagicMock())

        acted = await handler.oversight_cycle()

        assert acted is False
        emit.assert_not_called()

    async def test_park_suppresses_signal_for_same_worker(self) -> None:
        worker = _make_worker("w-park")
        sig = _make_signal(worker_name="w-park")
        monitor = MagicMock()
        monitor.enabled = True
        monitor.collect_signals.return_value = [sig]
        monitor.collect_park_proposals.return_value = [
            ("w-park", "task-abc", "blocked on operator")
        ]
        # If the signal weren't suppressed, evaluate_signal would be called.
        monitor.evaluate_signal = AsyncMock(return_value=None)
        handler, _, _ = _make_handler([worker], monitor=monitor, queen=MagicMock())

        await handler.oversight_cycle()

        monitor.evaluate_signal.assert_not_called()


class TestSignalEvaluation:
    """Signals route through ``evaluate_signal`` and on to result handling."""

    async def test_rate_limited_signal_logs_and_continues(self) -> None:
        worker = _make_worker("w1")
        sig = _make_signal()
        monitor = MagicMock()
        monitor.enabled = True
        monitor.collect_signals.return_value = [sig]
        monitor.collect_park_proposals.return_value = []
        monitor.evaluate_signal = AsyncMock(return_value=None)  # rate-limited
        handler, emit, log = _make_handler([worker], monitor=monitor, queen=MagicMock())

        acted = await handler.oversight_cycle()

        assert acted is False
        emit.assert_not_called()
        rate_limited = [e for e in log.entries if e.action == SystemAction.OVERSIGHT_RATE_LIMITED]
        assert len(rate_limited) == 1

    async def test_note_action_logs_intervention_and_returns_true(self) -> None:
        worker = _make_worker("w1")
        sig = _make_signal()
        result = _make_result(sig, action="note", severity=Severity.MAJOR)
        monitor = MagicMock()
        monitor.enabled = True
        monitor.collect_signals.return_value = [sig]
        monitor.collect_park_proposals.return_value = []
        monitor.evaluate_signal = AsyncMock(return_value=result)
        handler, _, log = _make_handler([worker], monitor=monitor, queen=MagicMock())

        acted = await handler.oversight_cycle()

        assert acted is True
        interventions = [e for e in log.entries if e.action == SystemAction.OVERSIGHT_INTERVENTION]
        assert len(interventions) == 1
        assert "note" in interventions[0].detail

    async def test_unknown_worker_in_result_returns_false(self) -> None:
        # Result references a worker we don't have.
        sig = _make_signal(worker_name="ghost")
        result = _make_result(sig, action="note")
        monitor = MagicMock()
        monitor.enabled = True
        monitor.collect_signals.return_value = [sig]
        monitor.collect_park_proposals.return_value = []
        monitor.evaluate_signal = AsyncMock(return_value=result)
        handler, emit, _ = _make_handler([_make_worker("real")], monitor=monitor, queen=MagicMock())

        acted = await handler.oversight_cycle()

        assert acted is False
        emit.assert_not_called()

    async def test_flag_human_emits_oversight_alert(self) -> None:
        worker = _make_worker("w1")
        sig = _make_signal()
        result = _make_result(sig, action="flag_human", severity=Severity.CRITICAL)
        monitor = MagicMock()
        monitor.enabled = True
        monitor.collect_signals.return_value = [sig]
        monitor.collect_park_proposals.return_value = []
        monitor.evaluate_signal = AsyncMock(return_value=result)
        handler, emit, _ = _make_handler([worker], monitor=monitor, queen=MagicMock())

        acted = await handler.oversight_cycle()

        assert acted is True
        emit.assert_called_once()
        args = emit.call_args.args
        assert args[0] == "oversight_alert"
        assert args[1] is worker
        assert args[2] is sig
        assert args[3] is result


class TestRedirectAction:
    """``redirect`` is the only action that touches the worker process."""

    async def test_redirect_skipped_when_operator_recently_engaged(self) -> None:
        worker = _make_worker("w1")
        worker.process = MagicMock()
        worker.process.operator_engaged_within.return_value = True
        worker.process.is_user_active = False
        worker.process.is_alive = True

        sig = _make_signal()
        result = _make_result(
            sig, action="redirect", severity=Severity.MAJOR, message="stop drifting"
        )
        monitor = MagicMock()
        monitor.enabled = True
        monitor.collect_signals.return_value = [sig]
        monitor.collect_park_proposals.return_value = []
        monitor.evaluate_signal = AsyncMock(return_value=result)
        # Real OversightConfig with the engagement window enabled.
        monitor._config = OversightConfig(operator_engagement_minutes=10.0)

        handler, _, log = _make_handler([worker], monitor=monitor, queen=MagicMock())

        acted = await handler.oversight_cycle()

        assert acted is False
        worker.process.send_keys.assert_not_called()
        worker.process.send_escape.assert_not_called()
        skipped = [
            e for e in log.entries if e.action == SystemAction.OVERSIGHT_INTERVENTION_SKIPPED
        ]
        assert len(skipped) == 1
        assert "operator engaged" in skipped[0].detail

    async def test_redirect_aborts_when_process_dies_after_escape(self) -> None:
        worker = _make_worker("w1")
        worker.process = MagicMock()
        worker.process.operator_engaged_within.return_value = False
        worker.process.is_user_active = False
        # Dies right after the escape.
        worker.process.is_alive = False
        worker.process.send_escape = AsyncMock()
        worker.process.send_keys = AsyncMock()

        sig = _make_signal()
        result = _make_result(
            sig, action="redirect", severity=Severity.MAJOR, message="stop drifting"
        )
        monitor = MagicMock()
        monitor.enabled = True
        monitor.collect_signals.return_value = [sig]
        monitor.collect_park_proposals.return_value = []
        monitor.evaluate_signal = AsyncMock(return_value=result)
        monitor._config = OversightConfig(operator_engagement_minutes=0)

        handler, _, _ = _make_handler([worker], monitor=monitor, queen=MagicMock())

        acted = await handler.oversight_cycle()

        assert acted is False
        worker.process.send_escape.assert_awaited_once()
        worker.process.send_keys.assert_not_called()

    async def test_redirect_sends_clean_single_line_message(self) -> None:
        worker = _make_worker("w1", state=WorkerState.RESTING)
        worker.process = MagicMock()
        worker.process.operator_engaged_within.return_value = False
        worker.process.is_user_active = False
        worker.process.is_alive = True
        worker.process.send_escape = AsyncMock()
        worker.process.send_keys = AsyncMock()

        sig = _make_signal()
        result = _make_result(
            sig,
            action="redirect",
            severity=Severity.MAJOR,
            message="line one\nline two\nline three",
        )
        monitor = MagicMock()
        monitor.enabled = True
        monitor.collect_signals.return_value = [sig]
        monitor.collect_park_proposals.return_value = []
        monitor.evaluate_signal = AsyncMock(return_value=result)
        monitor._config = OversightConfig(operator_engagement_minutes=0)

        handler, _, _ = _make_handler([worker], monitor=monitor, queen=MagicMock())

        acted = await handler.oversight_cycle()

        assert acted is True
        worker.process.send_escape.assert_awaited_once()
        worker.process.send_keys.assert_awaited_once()
        sent_msg = worker.process.send_keys.call_args.args[0]
        assert "\n" not in sent_msg
        assert sent_msg == "line one line two line three"
