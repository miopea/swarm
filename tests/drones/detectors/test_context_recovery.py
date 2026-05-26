"""Tests for :class:`swarm.drones.detectors.context_recovery.ContextRecoveryDetector`.

Consolidates two pre-refactor test classes that exercised the same
``_check_context_error`` logic from different angles:

* ``tests/test_state_tracker.py::TestContextErrorRecoveryCounter`` — the
  counter-reset semantics on non-BUZZING transitions.
* ``tests/test_context_awareness.py::TestContextErrorCompactGuard`` —
  the six-/compact-in-queue regression and the tightened regex.

Both moved here as part of Phase 2 of ``docs/specs/state-tracker-refactor.md``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from swarm.drones.detectors import ContextRecoveryDetector
from swarm.drones.log import DroneLog
from swarm.worker.worker import Worker, WorkerState


def _make_worker(name: str = "w1", state: WorkerState = WorkerState.RESTING) -> Worker:
    w = Worker(name=name, path=f"/tmp/{name}")
    w.state = state
    return w


def _buzzing_worker(name: str = "api") -> Worker:
    return _make_worker(name=name, state=WorkerState.BUZZING)


def _make_detector() -> tuple[ContextRecoveryDetector, MagicMock, MagicMock]:
    emit = MagicMock()
    decision_executor = MagicMock()
    decision_executor._deferred_actions = []
    detector = ContextRecoveryDetector(
        log=DroneLog(), decision_executor=decision_executor, emit=emit
    )
    return detector, emit, decision_executor


class TestContextErrorRecoveryCounter:
    """Counter resets on non-BUZZING and on no-match."""

    def test_recovery_counter_resets_on_non_buzzing(self) -> None:
        detector, _, _ = _make_detector()
        worker = _make_worker("w1", state=WorkerState.RESTING)
        worker.recovery_attempts = 2
        detector.check(worker, "")
        assert worker.recovery_attempts == 0

    def test_no_error_pattern_no_change(self) -> None:
        detector, _, _ = _make_detector()
        worker = _buzzing_worker("w1")
        worker.recovery_attempts = 0
        detector.check(worker, "ordinary output, no error here")
        assert worker.recovery_attempts == 0


class TestContextErrorCompactGuard:
    """Regression tests for the six-/compact-in-queue bug.

    Prior to the fix, the detector would re-queue a ``/compact`` deferred
    action on every poll while the error text remained in the worker's
    scrollback. Once Claude Code switched its native auto mode on, the
    worker would queue up 6+ ``/compact`` commands in its pending-
    message buffer before executing any of them. The guard here is
    ``worker.compacting`` — if a compact is already in flight, skip
    re-queueing.
    """

    def test_tier1_compact_sets_compacting_flag(self) -> None:
        detector, _, de = _make_detector()
        w = _buzzing_worker()
        detector.check(w, "Error: prompt is too long, retry later")
        assert w.compacting is True
        assert w.recovery_attempts == 1
        assert len(de._deferred_actions) == 1
        assert de._deferred_actions[0][0] == "compact"

    def test_second_poll_does_not_requeue_while_compacting(self) -> None:
        """The bug: on poll 2 (same error still in scrollback, worker
        still BUZZING, compacting flag still True) we must NOT queue
        another compact."""
        detector, _, de = _make_detector()
        w = _buzzing_worker()
        detector.check(w, "Error: prompt is too long")
        assert len(de._deferred_actions) == 1

        # Poll 2 — same conditions
        detector.check(w, "Error: prompt is too long")
        assert len(de._deferred_actions) == 1  # still one, not two
        assert w.recovery_attempts == 1  # didn't advance to 2

    def test_recovery_resumes_after_compacting_clears(self) -> None:
        """When the in-flight compact completes (``compacting = False``)
        but the error is still showing, the tier-2 revive path should
        still be reachable — the guard only prevents double-queue."""
        detector, _, de = _make_detector()
        w = _buzzing_worker()
        detector.check(w, "Error: prompt is too long")
        w.compacting = False  # simulate PostCompact hook clearing it

        detector.check(w, "Error: prompt is too long")
        # recovery_attempts should now advance to 2 → queues "revive"
        assert w.recovery_attempts == 2
        actions = [a[0] for a in de._deferred_actions]
        assert actions == ["compact", "revive"]

    def test_bare_context_window_phrase_no_longer_matches(self) -> None:
        """``"context window"`` on its own (a common English phrase in
        LLM chats) used to trigger tier-1 recovery. The tightened regex
        now requires the full error shapes Claude Code actually emits."""
        detector, _, de = _make_detector()
        w = _buzzing_worker()
        detector.check(w, "The worker was discussing the Claude context window size")
        assert w.compacting is False
        assert w.recovery_attempts == 0
        assert de._deferred_actions == []

    def test_full_error_shapes_still_match(self) -> None:
        """Make sure the tightened regex still fires on the real
        errors: "prompt is too long", "context window exceeded",
        "maximum context length", "token limit exceeded"."""
        for err in (
            "Error: prompt is too long, please retry",
            "context window exceeded — please compact",
            "context window is full",
            "context window limit reached",
            "maximum context length of 200000 tokens",
            "token limit exceeded for this request",
        ):
            detector, _, de = _make_detector()
            w = _buzzing_worker()
            detector.check(w, err)
            assert w.compacting is True, f"regex should match: {err!r}"
            assert len(de._deferred_actions) == 1, f"should queue compact for: {err!r}"
