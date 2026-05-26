"""Tests for :class:`swarm.drones.detectors.diminishing_returns.DiminishingReturnsDetector`.

Moved from ``tests/test_state_tracker.py::TestDiminishingReturns`` as
part of Phase 1 of ``docs/specs/state-tracker-refactor.md``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from swarm.drones.detectors import DiminishingReturnsDetector
from swarm.drones.log import DroneLog
from swarm.worker.worker import Worker, WorkerState


def _make_worker(name: str = "w1", state: WorkerState = WorkerState.RESTING) -> Worker:
    w = Worker(name=name, path=f"/tmp/{name}")
    w.state = state
    return w


def _make_detector() -> tuple[DiminishingReturnsDetector, MagicMock]:
    emit = MagicMock()
    return DiminishingReturnsDetector(log=DroneLog(), emit=emit), emit


class TestDiminishingReturns:
    def test_no_baseline_seeds_prev_tokens(self) -> None:
        detector, emit = _make_detector()
        worker = _make_worker("w1", state=WorkerState.BUZZING)
        worker.usage.last_turn_input_tokens = 1000
        detector.check(worker)
        assert worker._prev_input_tokens == 1000
        # No escalation on first observation.
        emit.assert_not_called()

    def test_resets_streak_on_state_change(self) -> None:
        detector, _ = _make_detector()
        worker = _make_worker("w1", state=WorkerState.RESTING)
        worker._low_delta_streak = 2
        detector.check(worker)
        assert worker._low_delta_streak == 0

    def test_low_delta_increments_streak(self) -> None:
        detector, _ = _make_detector()
        worker = _make_worker("w1", state=WorkerState.BUZZING)
        worker._prev_input_tokens = 1000
        worker.usage.last_turn_input_tokens = 1100  # delta 100, below threshold
        worker.process = None  # skip subagent check
        detector.check(worker)
        assert worker._low_delta_streak == 1

    def test_stationary_value_is_ignored(self) -> None:
        # Same value across polls = same turn still in progress, not no-progress.
        detector, emit = _make_detector()
        worker = _make_worker("w1", state=WorkerState.BUZZING)
        worker._prev_input_tokens = 5000
        worker.usage.last_turn_input_tokens = 5000
        detector.check(worker)
        assert worker._low_delta_streak == 0
        emit.assert_not_called()
