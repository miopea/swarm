"""Tests for :class:`swarm.drones.detectors.rate_limit.RateLimitDetector`.

Moved from ``tests/test_state_tracker.py::TestRateLimitDebounce`` as
part of Phase 1 of ``docs/specs/state-tracker-refactor.md``.  Builds
the detector directly — no WorkerStateTracker overhead.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from swarm.drones.detectors import RateLimitDetector
from swarm.drones.log import DroneLog
from swarm.worker.worker import Worker


def _make_worker(name: str = "w1") -> Worker:
    return Worker(name=name, path=f"/tmp/{name}")


def _make_detector() -> tuple[RateLimitDetector, MagicMock]:
    emit = MagicMock()
    return RateLimitDetector(log=DroneLog(), emit=emit), emit


class TestRateLimitDebounce:
    """``check()`` debounces 60s to avoid spamming the log."""

    def test_no_match_does_nothing(self) -> None:
        detector, emit = _make_detector()
        worker = _make_worker("w1")
        detector.check(worker, "normal output without limit")
        emit.assert_not_called()
        assert detector.last_seen("w1") == 0.0

    def test_match_emits_and_records(self) -> None:
        detector, emit = _make_detector()
        worker = _make_worker("w1")
        # _RE_RATE_LIMIT lives in providers.claude; use its literal phrase.
        content = "You've hit your 5-hour limit. Please wait until 3pm."
        detector.check(worker, content)
        # Should have fired once.
        calls = [c for c in emit.call_args_list if c.args and c.args[0] == "rate_limit"]
        assert len(calls) == 1
        assert detector.last_seen("w1") > 0

    def test_debounce_suppresses_second_match(self) -> None:
        detector, emit = _make_detector()
        worker = _make_worker("w1")
        content = "You've hit your 5-hour limit. Try again at 3pm."
        detector.check(worker, content)
        emit.reset_mock()
        # Immediate second call within debounce → no new emit.
        detector.check(worker, content)
        rate_calls = [c for c in emit.call_args_list if c.args and c.args[0] == "rate_limit"]
        assert rate_calls == []


class TestForgetCleanup:
    def test_forget_clears_last_seen(self) -> None:
        detector, _ = _make_detector()
        worker = _make_worker("w1")
        detector.check(worker, "You've hit your 5-hour limit.")
        assert detector.last_seen("w1") > 0
        detector.forget("w1")
        assert detector.last_seen("w1") == 0.0
