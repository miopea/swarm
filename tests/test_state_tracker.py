"""Tests for :class:`swarm.drones.state_tracker.WorkerStateTracker`.

Focused unit coverage of the tracker's public surface (wake_worker,
mark_*, any_became_active, cleanup_dead_worker) plus the small,
well-scoped private helpers that drive the polling loop's decisions
(_build_safe_pattern, _suggest_approval_pattern, content
fingerprinting, diminishing-returns streak, rate-limit debounce,
context recovery counter). Broader poll-loop integration stays in
``test_pilot.py`` / ``test_context_awareness.py``.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

from swarm.config import DroneConfig
from swarm.drones.detectors import (
    ContextFileTracker,
    ContextPressureCheck,
    ContextRecoveryDetector,
    DiminishingReturnsDetector,
    RateLimitDetector,
    WorkerHealthDetectors,
)
from swarm.drones.log import DroneLog
from swarm.drones.state_tracker import (
    WorkerStateTracker,
    _build_safe_pattern,
)
from swarm.worker.worker import Worker, WorkerState


def _make_worker(name: str = "w1", state: WorkerState = WorkerState.RESTING) -> Worker:
    w = Worker(name=name, path=f"/tmp/{name}")
    w.state = state
    return w


def _make_tracker(
    workers: list[Worker] | None = None,
    *,
    drone_config: DroneConfig | None = None,
    suspended: set[str] | None = None,
    suspended_at: dict[str, float] | None = None,
) -> tuple[WorkerStateTracker, MagicMock]:
    """Build a tracker with minimal but realistic dependencies."""
    workers = workers if workers is not None else []
    log = DroneLog()
    emit = MagicMock()
    decision_executor = MagicMock()
    decision_executor._deferred_actions = []

    def _get_provider(_w: Worker) -> Any:
        prov = MagicMock()
        prov.classify_with_events.return_value = (WorkerState.RESTING, None)
        prov.classify_styled_with_events.return_value = (WorkerState.RESTING, None)
        prov.classify_styled_output.return_value = WorkerState.RESTING
        prov.has_plan_prompt.return_value = False
        prov.is_user_question.return_value = False
        prov.has_choice_prompt.return_value = False
        prov.has_accept_edits_prompt.return_value = False
        prov.get_choice_summary.return_value = ""
        return prov

    detectors = WorkerHealthDetectors(
        context_files=ContextFileTracker(),
        diminishing=DiminishingReturnsDetector(log=log, emit=emit),
        rate_limit=RateLimitDetector(log=log, emit=emit),
        recovery=ContextRecoveryDetector(log=log, decision_executor=decision_executor, emit=emit),
        pressure=ContextPressureCheck(
            log=log,
            decision_executor=decision_executor,
            drone_config=drone_config or DroneConfig(),
        ),
    )
    tracker = WorkerStateTracker(
        workers=workers,
        log=log,
        task_board=None,
        drone_config=drone_config or DroneConfig(),
        get_provider=_get_provider,
        emit=emit,
        decision_executor=decision_executor,
        prev_states={},
        idle_consecutive={},
        escalated={},
        suspended=suspended if suspended is not None else set(),
        suspended_at=suspended_at if suspended_at is not None else {},
        focused_workers=set(),
        revive_history={},
        detectors=detectors,
    )
    return tracker, emit


class TestBuildSafePattern:
    """``_build_safe_pattern`` gates approval pattern suggestions."""

    def test_empty_returns_empty(self) -> None:
        assert _build_safe_pattern([]) == ""

    def test_single_word_command(self) -> None:
        assert _build_safe_pattern(["ls"]) == r"\bls\b"

    def test_two_words_joined(self) -> None:
        assert _build_safe_pattern(["npm", "test"]) == r"\bnpm\ test\b"

    def test_wrapper_uv_run_keeps_three_words(self) -> None:
        # `uv run pytest` should keep all three to specialize the pattern.
        assert _build_safe_pattern(["uv", "run", "pytest"]) == r"\buv\ run\ pytest\b"

    def test_dangerous_root_returns_empty(self) -> None:
        assert _build_safe_pattern(["rm", "-rf", "/tmp/x"]) == ""

    def test_dangerous_root_base_returns_empty(self) -> None:
        # `rm.exe` and similar variants should also be filtered.
        assert _build_safe_pattern(["rm.exe", "anything"]) == ""

    def test_dangerous_second_word_returns_empty(self) -> None:
        # `sudo` in arg position is still dangerous.
        assert _build_safe_pattern(["env", "sudo", "rm"]) == ""


class TestPropertiesAndSetters:
    """The boolean flags the pilot loop reads after each tick."""

    def test_any_became_active_setter_round_trip(self) -> None:
        tracker, _ = _make_tracker()
        assert tracker.any_became_active is False
        tracker.any_became_active = True
        assert tracker.any_became_active is True

    def test_needs_assign_check_setter_round_trip(self) -> None:
        tracker, _ = _make_tracker()
        assert tracker.needs_assign_check is False
        tracker.needs_assign_check = True
        assert tracker.needs_assign_check is True

    def test_mark_operator_and_drone_continue_independent(self) -> None:
        tracker, _ = _make_tracker()
        tracker.mark_operator_continue("alice")
        tracker.mark_drone_continued("bob")
        assert "alice" in tracker._operator_continued
        assert "bob" in tracker._drone_continued
        assert "alice" not in tracker._drone_continued


class TestWakeWorker:
    def test_wake_unsuspended_returns_false(self) -> None:
        tracker, _ = _make_tracker()
        assert tracker.wake_worker("nobody") is False

    def test_wake_clears_suspension_state(self) -> None:
        suspended = {"w1"}
        suspended_at = {"w1": 100.0}
        tracker, _ = _make_tracker(suspended=suspended, suspended_at=suspended_at)
        # Pre-populate the fingerprint/streak that wake clears.
        tracker._content_fingerprints["w1"] = "abc"
        tracker._unchanged_streak["w1"] = 5
        assert tracker.wake_worker("w1") is True
        assert "w1" not in tracker._suspended
        assert "w1" not in tracker._suspended_at
        assert "w1" not in tracker._content_fingerprints
        assert "w1" not in tracker._unchanged_streak

    def test_wake_is_idempotent(self) -> None:
        tracker, _ = _make_tracker(suspended={"w1"})
        assert tracker.wake_worker("w1") is True
        # Second call no-ops because the worker is no longer suspended.
        assert tracker.wake_worker("w1") is False


class TestContentFingerprint:
    """Fingerprinting drives the RESTING short-circuit + suspend path."""

    def test_unchanged_content_grows_streak(self) -> None:
        tracker, _ = _make_tracker()
        for _ in range(4):
            tracker._update_content_fingerprint("w1", "stable content")
        assert tracker._unchanged_streak["w1"] == 3  # 0,1,2,3 = three increments

    def test_changed_content_resets_streak(self) -> None:
        tracker, _ = _make_tracker()
        tracker._update_content_fingerprint("w1", "first")
        tracker._update_content_fingerprint("w1", "first")
        tracker._update_content_fingerprint("w1", "first")
        assert tracker._unchanged_streak["w1"] == 2
        tracker._update_content_fingerprint("w1", "second")
        assert tracker._unchanged_streak["w1"] == 0

    def test_empty_content_fingerprint(self) -> None:
        tracker, _ = _make_tracker()
        tracker._update_content_fingerprint("w1", "")
        # First write seeds the fingerprint; empty string is a valid signature.
        assert tracker._content_fingerprints["w1"] == ""


class TestTrackIdle:
    """Idle counter tracks consecutive RESTING ticks."""

    def test_resting_increments(self) -> None:
        tracker, _ = _make_tracker()
        worker = _make_worker("w1", state=WorkerState.RESTING)
        for _ in range(3):
            tracker._track_idle(worker)
        assert tracker._idle_consecutive["w1"] == 3

    def test_non_resting_clears(self) -> None:
        tracker, _ = _make_tracker()
        worker = _make_worker("w1", state=WorkerState.RESTING)
        tracker._track_idle(worker)
        tracker._track_idle(worker)
        worker.state = WorkerState.BUZZING
        tracker._track_idle(worker)
        assert "w1" not in tracker._idle_consecutive


class TestShouldThrottleSleeping:
    def test_not_throttled_when_not_sleeping(self) -> None:
        tracker, _ = _make_tracker()
        worker = _make_worker("w1", state=WorkerState.RESTING)
        # display_state == state when state_duration is short
        assert tracker._should_throttle_sleeping(worker) is False

    def test_throttled_when_recently_polled(self) -> None:
        cfg = DroneConfig(sleeping_poll_interval=60.0)
        tracker, _ = _make_tracker(drone_config=cfg)
        worker = _make_worker("w1", state=WorkerState.RESTING)
        # Push state_duration past sleeping_threshold so display_state == SLEEPING.
        worker.state_since = time.time() - 1000
        worker.sleeping_threshold = 1.0  # whatever default; override low
        tracker._last_full_poll["w1"] = time.time() - 5.0
        assert tracker._should_throttle_sleeping(worker) is True

    def test_not_throttled_when_focused(self) -> None:
        cfg = DroneConfig(sleeping_poll_interval=60.0)
        tracker, _ = _make_tracker(drone_config=cfg)
        tracker._focused_workers.add("w1")
        worker = _make_worker("w1", state=WorkerState.RESTING)
        worker.state_since = time.time() - 1000
        worker.sleeping_threshold = 1.0
        tracker._last_full_poll["w1"] = time.time() - 5.0
        assert tracker._should_throttle_sleeping(worker) is False


# ``TestContextPressure`` migrated to
# ``tests/drones/detectors/test_context_pressure_check.py`` as part of
# Phase 3 of ``docs/specs/state-tracker-refactor.md`` — the logic now
# lives in ContextPressureCheck.


class TestCleanupDeadWorker:
    def test_clears_all_per_worker_state(self) -> None:
        tracker, _ = _make_tracker(
            suspended={"dead"},
            suspended_at={"dead": 100.0},
        )
        tracker._prev_states["dead"] = WorkerState.BUZZING
        tracker._escalated["dead"] = 0.0
        tracker._idle_consecutive["dead"] = 3
        tracker._content_fingerprints["dead"] = "xyz"
        tracker._unchanged_streak["dead"] = 4
        tracker._revive_history["dead"] = [1.0, 2.0]
        tracker._last_full_poll["dead"] = 100.0
        tracker._waiting_content["dead"] = "prompt"
        tracker._drone_continued.add("dead")

        dw = _make_worker("dead")
        tracker.cleanup_dead_worker(dw)

        for d in (
            tracker._prev_states,
            tracker._escalated,
            tracker._idle_consecutive,
            tracker._content_fingerprints,
            tracker._unchanged_streak,
            tracker._suspended_at,
            tracker._revive_history,
            tracker._last_full_poll,
            tracker._waiting_content,
        ):
            assert "dead" not in d
        assert "dead" not in tracker._suspended
        assert "dead" not in tracker._drone_continued


class TestHasActiveTurnSignal:
    """The stuck-BUZZING safety net must recognise an in-flight dynamic
    workflow (footer tray) as an active turn — otherwise a long workflow run
    would be force-flipped to RESTING after the threshold."""

    def test_workflow_footer_is_active(self) -> None:
        tracker, _ = _make_tracker()
        content = "> \n1 background dynamic workflow · /workflows\n"
        assert tracker._has_active_turn_signal(content) is True

    def test_remote_workflow_footer_is_active(self) -> None:
        tracker, _ = _make_tracker()
        content = "> \n2 remote dynamic workflows · /workflows\n"
        assert tracker._has_active_turn_signal(content) is True

    def test_esc_to_interrupt_is_active(self) -> None:
        tracker, _ = _make_tracker()
        assert tracker._has_active_turn_signal("working\nesc to interrupt\n") is True

    def test_plain_idle_prompt_is_not_active(self) -> None:
        tracker, _ = _make_tracker()
        assert tracker._has_active_turn_signal("Done.\n> \n? for shortcuts\n") is False


class TestWorkerHasActiveTurn:
    """``worker_has_active_turn`` re-reads the LIVE PTY so a worker mid a
    long quiet foreground command isn't seen as idle (2026-06-11 bug)."""

    @staticmethod
    def _worker(content: str, *, alive: bool = True) -> MagicMock:
        w = MagicMock()
        w.process.is_alive = alive
        w.process.get_content.return_value = content
        return w

    def test_live_esc_to_interrupt_is_busy(self) -> None:
        tracker, _ = _make_tracker()
        worker = self._worker("running gh run watch\nesc to interrupt\n")
        assert tracker.worker_has_active_turn(worker) is True

    def test_live_idle_prompt_is_not_busy(self) -> None:
        tracker, _ = _make_tracker()
        worker = self._worker("Done.\n> \n? for shortcuts\n")
        assert tracker.worker_has_active_turn(worker) is False

    def test_dead_process_is_not_busy(self) -> None:
        tracker, _ = _make_tracker()
        worker = self._worker("esc to interrupt\n", alive=False)
        assert tracker.worker_has_active_turn(worker) is False

    def test_no_process_is_not_busy(self) -> None:
        tracker, _ = _make_tracker()
        worker = MagicMock()
        worker.process = None
        assert tracker.worker_has_active_turn(worker) is False

    def test_read_error_is_not_busy(self) -> None:
        tracker, _ = _make_tracker()
        worker = MagicMock()
        worker.process.is_alive = True
        worker.process.get_content.side_effect = OSError("pty gone")
        assert tracker.worker_has_active_turn(worker) is False


class TestBuzzingPromotesOneTask:
    """#405 INV-1: when a worker goes BUZZING, the state tracker must promote
    AT MOST ONE assigned task to IN_PROGRESS. Promoting *every* assigned task
    (the old behaviour) put two tasks IN PROGRESS for one worker — the
    platform #604/#605 violation."""

    def _board_two_assigned(self, worker_name: str):
        from swarm.tasks.board import TaskBoard

        board = TaskBoard()
        t1 = board.create("first")
        t2 = board.create("second")
        for t in (t1, t2):
            board.assign(t.id, worker_name)
        return board, t1, t2

    def test_buzzing_promotes_at_most_one(self) -> None:
        from swarm.tasks.task import TaskStatus

        w = _make_worker("platform", WorkerState.BUZZING)
        tracker, _ = _make_tracker([w])
        board, t1, t2 = self._board_two_assigned("platform")
        tracker.task_board = board

        tracker._handle_state_change(w, WorkerState.RESTING)

        active = [t for t in (t1, t2) if t.status == TaskStatus.ACTIVE]
        assert len(active) == 1  # exactly one IN PROGRESS, never both

    def test_buzzing_skips_when_worker_already_has_active(self) -> None:
        from swarm.tasks.task import TaskStatus

        w = _make_worker("platform", WorkerState.BUZZING)
        tracker, _ = _make_tracker([w])
        board, t1, t2 = self._board_two_assigned("platform")
        board.activate(t1.id)  # t1 already IN PROGRESS
        tracker.task_board = board

        tracker._handle_state_change(w, WorkerState.RESTING)

        # The already-active task stays; the other is NOT also promoted.
        assert t1.status == TaskStatus.ACTIVE
        assert t2.status == TaskStatus.ASSIGNED

    def test_buzzing_promotes_single_assigned(self) -> None:
        from swarm.tasks.task import TaskStatus

        w = _make_worker("platform", WorkerState.BUZZING)
        tracker, _ = _make_tracker([w])
        from swarm.tasks.board import TaskBoard

        board = TaskBoard()
        t = board.create("only")
        board.assign(t.id, "platform")
        tracker.task_board = board

        tracker._handle_state_change(w, WorkerState.RESTING)

        assert t.status == TaskStatus.ACTIVE  # the normal single-task case still works
