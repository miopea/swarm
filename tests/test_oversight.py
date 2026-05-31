"""Tests for Queen oversight signals (Phase 4)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.config import OversightConfig
from swarm.queen.oversight import (
    OversightMonitor,
    OversightSignal,
    Severity,
    SignalType,
)
from swarm.worker.worker import Worker, WorkerState


def _make_worker(
    name: str = "w1",
    state: WorkerState = WorkerState.BUZZING,
    state_since: float | None = None,
) -> Worker:
    w = Worker(name=name, path="/tmp/test")
    w.state = state
    if state_since is not None:
        w.state_since = state_since
    return w


def _make_task(task_id: str = "t1", title: str = "Fix the bug") -> MagicMock:
    task = MagicMock()
    task.id = task_id
    task.title = title
    task.description = "Fix the authentication bug"
    return task


# --- OversightConfig ---


class TestOversightConfig:
    def test_defaults(self) -> None:
        cfg = OversightConfig()
        assert cfg.enabled is True
        assert cfg.buzzing_threshold_minutes == 15.0
        assert cfg.drift_check_interval_minutes == 10.0
        assert cfg.max_calls_per_hour == 6

    def test_custom(self) -> None:
        cfg = OversightConfig(
            enabled=False,
            buzzing_threshold_minutes=20.0,
            max_calls_per_hour=10,
        )
        assert cfg.enabled is False
        assert cfg.buzzing_threshold_minutes == 20.0
        assert cfg.max_calls_per_hour == 10


# --- OversightMonitor ---


class TestOversightMonitor:
    def test_disabled(self) -> None:
        monitor = OversightMonitor(OversightConfig(enabled=False))
        assert monitor.enabled is False
        w = _make_worker(state_since=time.time() - 3600)
        signals = monitor.collect_signals([w], None)
        assert signals == []

    def test_prolonged_buzzing_below_threshold(self) -> None:
        monitor = OversightMonitor(OversightConfig(buzzing_threshold_minutes=15.0))
        # Worker buzzing for 5 minutes — below threshold
        w = _make_worker(state_since=time.time() - 300)
        sig = monitor.check_prolonged_buzzing(w, None)
        assert sig is None

    def test_prolonged_buzzing_above_threshold(self) -> None:
        monitor = OversightMonitor(OversightConfig(buzzing_threshold_minutes=15.0))
        # Worker buzzing for 20 minutes — above threshold
        w = _make_worker(state_since=time.time() - 1200)
        sig = monitor.check_prolonged_buzzing(w, None)
        assert sig is not None
        assert sig.signal_type == SignalType.PROLONGED_BUZZING
        assert sig.worker_name == "w1"
        assert "20 minutes" in sig.description

    def test_prolonged_buzzing_with_task(self) -> None:
        monitor = OversightMonitor(OversightConfig(buzzing_threshold_minutes=15.0))
        w = _make_worker(state_since=time.time() - 1200)
        task = _make_task()
        sig = monitor.check_prolonged_buzzing(w, task)
        assert sig is not None
        assert "Fix the bug" in sig.description
        assert sig.task_id == "t1"

    def test_prolonged_buzzing_only_fires_once(self) -> None:
        monitor = OversightMonitor(OversightConfig(buzzing_threshold_minutes=15.0))
        w = _make_worker(state_since=time.time() - 1200)
        sig1 = monitor.check_prolonged_buzzing(w, None)
        sig2 = monitor.check_prolonged_buzzing(w, None)
        assert sig1 is not None
        assert sig2 is None  # Already notified

    def test_prolonged_buzzing_resets_on_state_change(self) -> None:
        monitor = OversightMonitor(OversightConfig(buzzing_threshold_minutes=15.0))
        w = _make_worker(state_since=time.time() - 1200)
        sig1 = monitor.check_prolonged_buzzing(w, None)
        assert sig1 is not None

        # Worker transitions to RESTING
        w.state = WorkerState.RESTING
        sig2 = monitor.check_prolonged_buzzing(w, None)
        assert sig2 is None  # Not buzzing

        # Worker goes back to BUZZING
        w.state = WorkerState.BUZZING
        w.state_since = time.time() - 1200
        sig3 = monitor.check_prolonged_buzzing(w, None)
        assert sig3 is not None  # Flag was cleared

    def test_prolonged_buzzing_non_buzzing_state(self) -> None:
        monitor = OversightMonitor(OversightConfig())
        w = _make_worker(state=WorkerState.RESTING, state_since=time.time() - 3600)
        sig = monitor.check_prolonged_buzzing(w, None)
        assert sig is None

    def test_prolonged_buzzing_suppressed_when_tool_active(self) -> None:
        """A long-running tool (e.g. an in-flight dynamic workflow) holds the
        worker in BUZZING by design — the signal must be suppressed even well
        past the threshold, and the notify flag must stay clear."""
        monitor = OversightMonitor(OversightConfig(buzzing_threshold_minutes=15.0))
        w = _make_worker(state_since=time.time() - 1200)  # 20 min
        sig = monitor.check_prolonged_buzzing(w, None, tool_active=True)
        assert sig is None
        # Flag untouched: once the tool finishes a genuine stall can still fire.
        sig2 = monitor.check_prolonged_buzzing(w, None, tool_active=False)
        assert sig2 is not None

    def test_collect_signals_suppresses_prolonged_buzzing_for_workflow(self) -> None:
        """collect_signals routes the per-worker is_long_running predicate into
        the prolonged-buzzing check, suppressing the signal for a workflow."""
        from swarm.providers.claude import ClaudeProvider

        provider = ClaudeProvider()
        monitor = OversightMonitor(OversightConfig(buzzing_threshold_minutes=15.0))
        w = _make_worker(state_since=time.time() - 1200)  # 20 min, above threshold
        outputs = {"w1": "> \n1 background dynamic workflow · /workflows\n"}
        signals = monitor.collect_signals(
            [w],
            None,
            worker_outputs=outputs,
            is_long_running=lambda worker, out: provider.is_long_running_tool_active(out),
        )
        assert all(s.signal_type != SignalType.PROLONGED_BUZZING for s in signals)

    def test_collect_signals_fires_prolonged_buzzing_without_workflow(self) -> None:
        """Genuinely stuck worker (no workflow indicator) still fires."""
        from swarm.providers.claude import ClaudeProvider

        provider = ClaudeProvider()
        monitor = OversightMonitor(OversightConfig(buzzing_threshold_minutes=15.0))
        w = _make_worker(state_since=time.time() - 1200)
        outputs = {"w1": "Working on it...\nesc to interrupt\n"}
        signals = monitor.collect_signals(
            [w],
            None,
            worker_outputs=outputs,
            is_long_running=lambda worker, out: provider.is_long_running_tool_active(out),
        )
        assert any(s.signal_type == SignalType.PROLONGED_BUZZING for s in signals)

    def test_task_drift_no_task(self) -> None:
        monitor = OversightMonitor(OversightConfig())
        w = _make_worker()
        sig = monitor.check_task_drift(w, None, "some output")
        assert sig is None

    def test_task_drift_no_output(self) -> None:
        monitor = OversightMonitor(OversightConfig())
        w = _make_worker()
        task = _make_task()
        sig = monitor.check_task_drift(w, task, "")
        assert sig is None

    def test_task_drift_interval_throttle(self) -> None:
        monitor = OversightMonitor(OversightConfig(drift_check_interval_minutes=10.0))
        w = _make_worker()
        task = _make_task()

        sig1 = monitor.check_task_drift(w, task, "output")
        assert sig1 is not None

        # Second check within interval — throttled
        sig2 = monitor.check_task_drift(w, task, "output")
        assert sig2 is None

    def test_task_drift_fires_after_interval(self) -> None:
        monitor = OversightMonitor(OversightConfig(drift_check_interval_minutes=10.0))
        w = _make_worker()
        task = _make_task()

        sig1 = monitor.check_task_drift(w, task, "output")
        assert sig1 is not None

        # Backdate the last check
        monitor._last_drift_check[w.name] = time.time() - 700
        sig2 = monitor.check_task_drift(w, task, "output")
        assert sig2 is not None

    def test_task_drift_wrong_state(self) -> None:
        monitor = OversightMonitor(OversightConfig())
        w = _make_worker(state=WorkerState.WAITING)
        task = _make_task()
        sig = monitor.check_task_drift(w, task, "output")
        assert sig is None

    def test_collect_signals_multiple(self) -> None:
        monitor = OversightMonitor(OversightConfig(buzzing_threshold_minutes=15.0))
        w1 = _make_worker(name="w1", state_since=time.time() - 1200)
        w2 = _make_worker(
            name="w2",
            state=WorkerState.RESTING,
            state_since=time.time() - 60,
        )
        signals = monitor.collect_signals([w1, w2], None)
        assert len(signals) == 1
        assert signals[0].worker_name == "w1"

    def test_collect_signals_with_task_board(self) -> None:
        monitor = OversightMonitor(OversightConfig(buzzing_threshold_minutes=15.0))
        w = _make_worker(state_since=time.time() - 1200)
        task = _make_task()
        board = MagicMock()
        board.active_tasks_for_worker.return_value = [task]

        signals = monitor.collect_signals([w], board, worker_outputs={"w1": "output"})
        # Should have buzzing signal (drift may or may not fire)
        buzzing = [s for s in signals if s.signal_type == SignalType.PROLONGED_BUZZING]
        assert len(buzzing) == 1
        assert buzzing[0].task_id == "t1"


# --- Rate Limiting ---


class TestRateLimiting:
    def test_within_limit(self) -> None:
        monitor = OversightMonitor(OversightConfig(max_calls_per_hour=6))
        assert monitor._within_rate_limit() is True

    def test_at_limit(self) -> None:
        monitor = OversightMonitor(OversightConfig(max_calls_per_hour=3))
        now = time.time()
        monitor._call_timestamps = [now - 100, now - 50, now - 10]
        assert monitor._within_rate_limit() is False

    def test_old_calls_expire(self) -> None:
        monitor = OversightMonitor(OversightConfig(max_calls_per_hour=3))
        old = time.time() - 4000  # > 1 hour ago
        monitor._call_timestamps = [old, old, old]
        assert monitor._within_rate_limit() is True


# --- Evaluate Signal ---


class TestEvaluateSignal:
    @pytest.mark.asyncio
    async def test_rate_limited(self) -> None:
        monitor = OversightMonitor(OversightConfig(max_calls_per_hour=1))
        monitor._call_timestamps = [time.time()]
        queen = MagicMock()
        signal = OversightSignal(
            signal_type=SignalType.PROLONGED_BUZZING,
            worker_name="w1",
            description="test",
        )
        result = await monitor.evaluate_signal(signal, queen, "output")
        assert result is None
        queen.ask.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_evaluation(self) -> None:
        monitor = OversightMonitor(OversightConfig())
        queen = AsyncMock()
        queen.ask.return_value = {
            "severity": "minor",
            "action": "note",
            "message": "Focus on the task",
            "reasoning": "Worker is making progress but slowly",
            "confidence": 0.82,
        }

        signal = OversightSignal(
            signal_type=SignalType.PROLONGED_BUZZING,
            worker_name="w1",
            description="Buzzing for 20 min",
        )
        result = await monitor.evaluate_signal(signal, queen, "output")
        assert result is not None
        assert result.severity == Severity.MINOR
        assert result.action == "note"
        assert result.message == "Focus on the task"
        assert result.confidence == 0.82

    @pytest.mark.asyncio
    async def test_critical_severity(self) -> None:
        monitor = OversightMonitor(OversightConfig())
        queen = AsyncMock()
        queen.ask.return_value = {
            "severity": "critical",
            "action": "flag_human",
            "message": "Worker deleting production data",
            "reasoning": "Destructive operation detected",
            "confidence": 0.94,
        }

        signal = OversightSignal(
            signal_type=SignalType.TASK_DRIFT,
            worker_name="w1",
            description="Drift detected",
            task_id="t1",
        )
        result = await monitor.evaluate_signal(signal, queen, "rm -rf /", task_info="Fix login bug")
        assert result is not None
        assert result.severity == Severity.CRITICAL
        assert result.action == "flag_human"

    @pytest.mark.asyncio
    async def test_queen_error(self) -> None:
        monitor = OversightMonitor(OversightConfig())
        queen = AsyncMock()
        queen.ask.return_value = {"error": "Rate limited"}

        signal = OversightSignal(
            signal_type=SignalType.PROLONGED_BUZZING,
            worker_name="w1",
            description="test",
        )
        result = await monitor.evaluate_signal(signal, queen, "output")
        assert result is None

    @pytest.mark.asyncio
    async def test_queen_exception(self) -> None:
        monitor = OversightMonitor(OversightConfig())
        queen = AsyncMock()
        queen.ask.side_effect = RuntimeError("connection failed")

        signal = OversightSignal(
            signal_type=SignalType.PROLONGED_BUZZING,
            worker_name="w1",
            description="test",
        )
        result = await monitor.evaluate_signal(signal, queen, "output")
        assert result is None

    @pytest.mark.asyncio
    async def test_unknown_severity_defaults_to_minor(self) -> None:
        monitor = OversightMonitor(OversightConfig())
        queen = AsyncMock()
        queen.ask.return_value = {
            "severity": "unknown_level",
            "action": "note",
            "message": "test",
            "reasoning": "test",
            "confidence": 0.5,
        }

        signal = OversightSignal(
            signal_type=SignalType.PROLONGED_BUZZING,
            worker_name="w1",
            description="test",
        )
        result = await monitor.evaluate_signal(signal, queen, "output")
        assert result is not None
        assert result.severity == Severity.MINOR

    @pytest.mark.asyncio
    async def test_redirect_with_cited_contradiction_preserved(self) -> None:
        """Task #340: a redirect that cites a contradicted task line stays a redirect."""
        monitor = OversightMonitor(OversightConfig())
        queen = AsyncMock()
        queen.ask.return_value = {
            "severity": "major",
            "action": "redirect",
            "message": "Stop the wrong work",
            "reasoning": "Worker is editing the production DB",
            "confidence": 0.91,
            "cited_contradiction": "Do NOT touch production data",
        }
        signal = OversightSignal(
            signal_type=SignalType.TASK_DRIFT,
            worker_name="w1",
            description="drift",
            task_id="t1",
        )
        result = await monitor.evaluate_signal(signal, queen, "DROP TABLE users")
        assert result is not None
        assert result.action == "redirect"
        assert result.severity == Severity.MAJOR
        assert result.cited_contradiction == "Do NOT touch production data"

    @pytest.mark.asyncio
    async def test_redirect_without_contradiction_downgraded_to_note(self) -> None:
        """Task #340: redirect with no cited contradiction downgrades to note.

        Models the budgetbug incident — surface-keyword divergence
        ("maintenance" router vs "backups" task) is not drift, and the
        Queen returned no quoted contradiction. Must not interrupt the
        worker.
        """
        monitor = OversightMonitor(OversightConfig())
        queen = AsyncMock()
        queen.ask.return_value = {
            "severity": "major",
            "action": "redirect",
            "message": "You're off-topic",
            "reasoning": "Topical mismatch — task says backups, you're shipping maintenance",
            "confidence": 0.74,
            # No cited_contradiction field — this is the bug class.
        }
        signal = OversightSignal(
            signal_type=SignalType.TASK_DRIFT,
            worker_name="w1",
            description="periodic drift check",
            task_id="t1",
        )
        result = await monitor.evaluate_signal(
            signal,
            queen,
            "shipping admin maintenance router for backups",
            task_info="Verify database backup functionality",
        )
        assert result is not None
        assert result.action == "note"
        assert result.severity == Severity.MINOR
        assert result.cited_contradiction == ""

    @pytest.mark.asyncio
    async def test_interventions_tracked(self) -> None:
        monitor = OversightMonitor(OversightConfig())
        queen = AsyncMock()
        queen.ask.return_value = {
            "severity": "major",
            "action": "redirect",
            "message": "Refocus on task",
            "reasoning": "Off track",
            "confidence": 0.87,
            "cited_contradiction": "task says X, you're doing Y",
        }

        signal = OversightSignal(
            signal_type=SignalType.TASK_DRIFT,
            worker_name="w1",
            description="Drift",
        )
        await monitor.evaluate_signal(signal, queen, "output")
        assert len(monitor._interventions) == 1
        assert monitor._interventions[0]["worker"] == "w1"
        assert monitor._interventions[0]["severity"] == "major"


# --- Status / Reset ---


class TestOversightStatus:
    def test_get_status(self) -> None:
        monitor = OversightMonitor(OversightConfig())
        status = monitor.get_status()
        assert status["enabled"] is True
        assert status["calls_this_hour"] == 0
        assert status["max_calls_per_hour"] == 6
        assert status["buzzing_notified"] == []
        assert status["recent_interventions"] == []

    def test_reset_worker(self) -> None:
        monitor = OversightMonitor(OversightConfig())
        monitor._buzzing_notified.add("w1")
        monitor._last_drift_check["w1"] = time.time()

        monitor.reset_worker("w1")
        assert "w1" not in monitor._buzzing_notified
        assert "w1" not in monitor._last_drift_check


# --- Config Integration ---


class TestConfigIntegration:
    def test_queen_config_has_oversight(self) -> None:
        from swarm.config import QueenConfig

        cfg = QueenConfig()
        assert cfg.oversight.enabled is True
        assert cfg.oversight.buzzing_threshold_minutes == 15.0

    def test_hive_config_has_oversight(self) -> None:
        from swarm.config import HiveConfig

        cfg = HiveConfig()
        assert cfg.queen.oversight.enabled is True

    def test_config_validation(self) -> None:
        from swarm.config import HiveConfig, OversightConfig, QueenConfig

        cfg = HiveConfig(
            queen=QueenConfig(oversight=OversightConfig(buzzing_threshold_minutes=-1.0))
        )
        errors = cfg.validate()
        assert any("buzzing_threshold_minutes" in e for e in errors)

    def test_config_serialization_roundtrip(self) -> None:
        from swarm.config import HiveConfig, serialize_config

        cfg = HiveConfig()
        data = serialize_config(cfg)
        assert "oversight" in data["queen"]
        assert data["queen"]["oversight"]["enabled"] is True
        assert data["queen"]["oversight"]["buzzing_threshold_minutes"] == 15.0


class TestCheckResourcePressure:
    """OversightMonitor.check_resource_pressure — fires only on HIGH/CRITICAL
    memory pressure that has persisted for more than two minutes."""

    def _monitor(self) -> OversightMonitor:
        return OversightMonitor(OversightConfig(enabled=True))

    def test_fires_on_high_pressure_when_persistent(self) -> None:
        sig = self._monitor().check_resource_pressure("high", 180.0)
        assert sig is not None
        assert sig.signal_type == SignalType.RESOURCE_PRESSURE
        assert sig.worker_name == ""
        assert "high" in sig.description

    def test_fires_on_critical_pressure_when_persistent(self) -> None:
        sig = self._monitor().check_resource_pressure("critical", 300.0)
        assert sig is not None
        assert sig.signal_type == SignalType.RESOURCE_PRESSURE
        assert "critical" in sig.description

    def test_ignores_brief_pressure(self) -> None:
        assert self._monitor().check_resource_pressure("critical", 119.0) is None

    def test_at_threshold_boundary_does_not_fire(self) -> None:
        # Strictly less-than 120s does not fire; exactly 120s is not "< 120".
        assert self._monitor().check_resource_pressure("high", 119.9) is None
        assert self._monitor().check_resource_pressure("high", 120.0) is not None

    def test_ignores_low_and_unknown_pressure_levels(self) -> None:
        m = self._monitor()
        assert m.check_resource_pressure("low", 600.0) is None
        assert m.check_resource_pressure("medium", 600.0) is None
        assert m.check_resource_pressure("", 600.0) is None
