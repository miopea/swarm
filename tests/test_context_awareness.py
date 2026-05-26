"""Tests for context window awareness."""

from __future__ import annotations

from swarm.notify.bus import EventType, NotificationBus, Severity
from swarm.worker.usage import estimate_context_usage
from swarm.worker.worker import TokenUsage, Worker


class TestEstimateContextUsage:
    def test_zero_tokens(self) -> None:
        usage = TokenUsage()
        assert estimate_context_usage(usage) == 0.0

    def test_last_turn_half_context(self) -> None:
        usage = TokenUsage(input_tokens=2_000_000, last_turn_input_tokens=500_000)
        pct = estimate_context_usage(usage, "claude")
        assert 0.49 < pct < 0.51

    def test_last_turn_full_context(self) -> None:
        usage = TokenUsage(input_tokens=3_000_000, last_turn_input_tokens=1_000_000)
        pct = estimate_context_usage(usage, "claude")
        assert pct == 1.0

    def test_last_turn_over_context_capped(self) -> None:
        usage = TokenUsage(input_tokens=5_000_000, last_turn_input_tokens=2_000_000)
        pct = estimate_context_usage(usage, "claude")
        assert pct == 1.0

    def test_falls_back_to_cumulative_when_no_last_turn(self) -> None:
        usage = TokenUsage(input_tokens=500_000)
        pct = estimate_context_usage(usage, "claude")
        assert 0.49 < pct < 0.51

    def test_codex_smaller_window(self) -> None:
        usage = TokenUsage(last_turn_input_tokens=100_000)
        pct = estimate_context_usage(usage, "codex")
        assert 0.49 < pct < 0.51

    def test_unknown_provider_uses_claude_default(self) -> None:
        usage = TokenUsage(last_turn_input_tokens=500_000)
        pct = estimate_context_usage(usage, "unknown_provider")
        assert 0.49 < pct < 0.51

    def test_last_turn_preferred_over_cumulative(self) -> None:
        """Cumulative input_tokens is 5M (would be 500% of window).
        Last turn is 200k (20% of window). Should use last turn."""
        usage = TokenUsage(input_tokens=5_000_000, last_turn_input_tokens=200_000)
        pct = estimate_context_usage(usage, "claude")
        assert 0.19 < pct < 0.21


class TestWorkerContextPct:
    def test_default_zero(self) -> None:
        w = Worker(name="api", path="/tmp/api")
        assert w.context_pct == 0.0

    def test_in_api_dict(self) -> None:
        w = Worker(name="api", path="/tmp/api")
        w.context_pct = 0.75
        d = w.to_api_dict()
        assert d["context_pct"] == 0.75


class TestContextPressureNotification:
    def test_emit_context_pressure_warning(self) -> None:
        bus = NotificationBus(debounce_seconds=0)
        received = []
        bus.add_backend(received.append)

        bus.emit_context_pressure("api", 0.72, "warning")
        assert len(received) == 1
        assert received[0].event_type == EventType.CONTEXT_PRESSURE
        assert received[0].severity == Severity.WARNING
        assert "72%" in received[0].message

    def test_emit_context_pressure_critical(self) -> None:
        bus = NotificationBus(debounce_seconds=0)
        received = []
        bus.add_backend(received.append)

        bus.emit_context_pressure("api", 0.95, "critical")
        assert len(received) == 1
        assert received[0].severity == Severity.URGENT

    # ``TestContextErrorCompactGuard`` migrated to
    # ``tests/drones/detectors/test_context_recovery.py`` as part of
    # Phase 2 of ``docs/specs/state-tracker-refactor.md`` — the
    # logic now lives in ContextRecoveryDetector.
