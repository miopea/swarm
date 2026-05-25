"""Tests for drones/pilot.py — async polling loop and decision engine."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from swarm.config import DroneConfig
from swarm.drones.log import DroneLog, SystemAction
from swarm.drones.pilot import DronePilot
from swarm.drones.poll_dispatcher import PollDispatcher
from swarm.drones.rules import decide
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import TaskStatus
from swarm.worker.worker import WorkerState
from tests.conftest import make_worker as _make_worker


def _set_workers_content(
    workers: list, *, content: str = "esc to interrupt", command: str = "claude"
) -> None:
    """Configure all workers' fake processes for polling."""
    for w in workers:
        if w.process:
            w.process.set_content(content)
            w.process._child_foreground_command = command


@pytest.fixture
def pilot_setup(monkeypatch):
    """Set up a DronePilot with fake processes."""
    workers = [_make_worker("api"), _make_worker("web")]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())

    # Default: BUZZING content
    _set_workers_content(workers, content="esc to interrupt", command="claude")

    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

    return pilot, workers, log


@pytest.mark.asyncio
async def test_poll_once_buzzing(pilot_setup):
    """Workers in BUZZING state should not generate any actions."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True
    await pilot.poll_once()
    assert len(log.entries) == 0


@pytest.mark.asyncio
async def test_poll_once_detects_waiting(pilot_setup):
    """poll_once should detect WAITING state from empty prompt content."""
    pilot, workers, log = pilot_setup
    _set_workers_content(workers, content="> ", command="claude")
    pilot.enabled = True
    # BUZZING -> WAITING requires 3 confirmations (hysteresis)
    await pilot.poll_once()
    await pilot.poll_once()
    await pilot.poll_once()
    # After three polls (hysteresis), workers with empty prompts should be WAITING
    waiting = [w for w in workers if w.state == WorkerState.WAITING]
    assert len(waiting) > 0, "Expected at least one worker to transition to WAITING"


@pytest.mark.asyncio
async def test_poll_once_marks_dead_workers_stung(pilot_setup):
    """Dead processes should cause workers to transition to STUNG."""
    pilot, workers, log = pilot_setup
    # Kill all processes
    for w in workers:
        w.process._alive = False

    changes = []
    pilot.on_state_changed(lambda w: changes.append(w.name))

    await pilot.poll_once()
    # Workers should be STUNG, not removed (30s reap timeout gives user time to revive)
    assert len(workers) == 2
    assert all(w.state == WorkerState.STUNG for w in workers)
    assert len(changes) > 0


@pytest.mark.asyncio
async def test_poll_once_state_change_callback(pilot_setup):
    """State changes should fire the on_state_changed callback."""
    pilot, workers, log = pilot_setup

    # Kill processes → triggers STUNG transition
    for w in workers:
        w.process._alive = False

    state_changes = []
    pilot.on_state_changed(lambda w: state_changes.append(w.name))

    await pilot.poll_once()
    assert len(state_changes) > 0


@pytest.mark.asyncio
async def test_cleanup_dead_workers_removes_last_full_poll(pilot_setup):
    """_cleanup_dead_workers should remove _last_full_poll entries for dead workers."""
    pilot, workers, log = pilot_setup
    dead_name = workers[0].name
    alive_name = workers[1].name
    # Seed _last_full_poll for all workers
    for w in workers:
        pilot._last_full_poll[w.name] = time.time()

    # Mark first worker as dead
    dead = [workers[0]]
    pilot._cleanup_dead_workers(dead)

    assert dead_name not in pilot._last_full_poll
    assert alive_name in pilot._last_full_poll


def test_cleanup_stale_proposed_completions(pilot_setup):
    """_cleanup_stale_proposed_completions should remove entries older than max age."""
    pilot, workers, log = pilot_setup
    now = time.time()

    # Add a fresh entry and a stale entry
    pilot._proposed_completions["fresh-task"] = now
    pilot._proposed_completions["stale-task"] = now - 7200  # 2 hours old

    pilot._cleanup_stale_proposed_completions()

    assert "fresh-task" in pilot._proposed_completions
    assert "stale-task" not in pilot._proposed_completions


@pytest.mark.asyncio
async def test_poll_loop_error_counter(pilot_setup, monkeypatch):
    """Consecutive poll errors should increment counter and emit event at threshold."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True
    pilot._running = True

    events: list[int] = []
    pilot.on("poll_errors_exceeded", lambda count: events.append(count))

    # Make _poll_once_locked raise on every call
    call_count = 0

    async def _failing_poll():
        nonlocal call_count
        call_count += 1
        raise RuntimeError("test error")

    monkeypatch.setattr(pilot, "_poll_once_locked", _failing_poll)

    # Run a few iterations of the loop manually
    for _ in range(6):
        try:
            pilot._had_substantive_action = False
            pilot._any_became_active = False
            async with pilot._poll_lock:
                await pilot._poll_once_locked()
        except Exception:
            pilot._handle_poll_error()

    assert pilot._consecutive_errors == 6
    assert len(events) == 1  # emitted once at threshold=5
    assert events[0] == 5


@pytest.mark.asyncio
async def test_poll_loop_error_counter_resets(pilot_setup):
    """Successful poll cycle should reset the error counter."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True
    pilot._consecutive_errors = 3

    _set_workers_content(workers, content="esc to interrupt", command="claude")
    await pilot.poll_once()

    assert pilot._consecutive_errors == 0


@pytest.mark.asyncio
async def test_revive_on_stung(pilot_setup, monkeypatch):
    """STUNG workers with revives remaining should be revived."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Kill processes → triggers STUNG on first poll
    for w in workers:
        w.process._alive = False

    await pilot.poll_once()  # transitions to STUNG
    assert all(w.state == WorkerState.STUNG for w in workers)

    await pilot.poll_once()  # STUNG → decide → REVIVE
    revives = [e for e in log.entries if e.action == SystemAction.REVIVED]
    assert len(revives) > 0


@pytest.mark.asyncio
async def test_escalate_on_crash_loop(pilot_setup, monkeypatch):
    """Workers that exhaust revive attempts should be escalated."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Set workers to already have max revives
    for w in workers:
        w.revive_count = 3
        w.process._alive = False

    escalations = []
    pilot.on_escalate(lambda w, r: escalations.append((w.name, r)))

    await pilot.poll_once()  # transitions to STUNG
    await pilot.poll_once()  # STUNG + exhausted revives → ESCALATE
    escalates = [e for e in log.entries if e.action == SystemAction.ESCALATED]
    assert len(escalates) > 0


@pytest.mark.asyncio
async def test_revive_loop_escalates(pilot_setup, monkeypatch):
    """Workers revived too many times in a short window should escalate instead."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True
    pilot._revive_loop_max = 3
    pilot._revive_loop_window = 60.0

    w = workers[0]
    # Seed revive history: 3 revives in the last 30 seconds
    now = time.monotonic()
    pilot._revive_history[w.name] = [now - 30, now - 20, now - 10]

    # Kill process → STUNG
    w.process._alive = False
    await pilot.poll_once()  # transitions to STUNG
    assert w.state == WorkerState.STUNG

    escalations = []
    pilot.on_escalate(lambda w, r: escalations.append((w.name, r)))

    await pilot.poll_once()  # STUNG → should escalate (revive loop), NOT revive

    # Should NOT have revived
    revives = [e for e in log.entries if e.action == SystemAction.REVIVED]
    assert len([r for r in revives if r.worker_name == w.name]) == 0

    # Should have escalated
    assert len(escalations) > 0
    assert "revive loop" in escalations[0][1].lower()


@pytest.mark.asyncio
async def test_revive_loop_allows_after_window(pilot_setup, monkeypatch):
    """Old revives outside the window should not block new revives."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True
    pilot._revive_loop_max = 3
    pilot._revive_loop_window = 60.0

    w = workers[0]
    # Seed revive history: 3 revives, but all older than 60 seconds
    now = time.monotonic()
    pilot._revive_history[w.name] = [now - 120, now - 100, now - 80]

    # Kill process → STUNG
    w.process._alive = False
    await pilot.poll_once()  # transitions to STUNG
    assert w.state == WorkerState.STUNG

    await pilot.poll_once()  # STUNG → should revive (old history expired)

    revives = [e for e in log.entries if e.action == SystemAction.REVIVED]
    assert len([r for r in revives if r.worker_name == w.name]) > 0


@pytest.mark.asyncio
async def test_revive_loop_cleanup_on_dead_worker(pilot_setup, monkeypatch):
    """Revive history should be cleaned up when dead workers are reaped."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    w = workers[0]
    pilot._revive_history[w.name] = [time.monotonic()]

    # Simulate dead worker cleanup
    pilot._cleanup_dead_workers([w])

    assert w.name not in pilot._revive_history


@pytest.mark.asyncio
async def test_shell_fallback_stays_resting(pilot_setup):
    """When the CLI exits but the wrapper shell is alive, worker should be RESTING, not STUNG."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Simulate: CLI exited, shell fallback (bash is foreground, but process is alive)
    _set_workers_content(workers, content="$ ", command="bash")
    # Process is still alive (wrapper shell)
    for w in workers:
        w.process._alive = True

    # BUZZING→RESTING requires 3 confirmations (hysteresis)
    await pilot.poll_once()
    await pilot.poll_once()
    await pilot.poll_once()

    # Workers should be RESTING (not STUNG) because the process is alive
    for w in workers:
        assert w.state == WorkerState.RESTING, (
            f"Worker {w.name} should be RESTING after shell fallback, got {w.state}"
        )


@pytest.mark.asyncio
async def test_toggle(pilot_setup):
    """toggle() should flip enabled state but keep poll loop alive."""
    pilot, _, _ = pilot_setup
    assert not pilot.enabled
    result = pilot.toggle()
    assert result is True
    assert pilot.enabled
    result = pilot.toggle()
    assert result is False
    assert not pilot.enabled
    # Poll loop should still be running for state detection
    assert pilot._task is not None and not pilot._task.done()
    # Clean up
    pilot.stop()


@pytest.mark.asyncio
async def test_continue_on_choice_prompt(pilot_setup):
    """Choice prompts should trigger CONTINUE decision."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Put workers in WAITING state first (choice prompt = actionable)
    for w in workers:
        w.state = WorkerState.WAITING

    content = """> 1. Always allow
  2. Yes
  3. No
Enter to select · ↑/↓ to navigate"""
    _set_workers_content(workers, content=content, command="claude")

    await pilot.poll_once()
    continued = [e for e in log.entries if e.action == SystemAction.CONTINUED]
    assert len(continued) > 0


@pytest.mark.asyncio
async def test_escalated_set_is_per_pilot():
    """Each DronePilot should have its own _escalated set."""
    w1 = [_make_worker("api")]
    w2 = [_make_worker("web")]
    p1 = DronePilot(w1, DroneLog(), drone_config=DroneConfig())
    p2 = DronePilot(w2, DroneLog(), drone_config=DroneConfig())
    assert p1._escalated is not p2._escalated


# ── Adaptive polling / backoff ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_once_returns_false_when_idle(pilot_setup):
    """poll_once should return False when all workers are BUZZING (no action)."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True
    result = await pilot.poll_once()
    assert result is False


@pytest.mark.asyncio
async def test_poll_once_returns_true_on_action(pilot_setup):
    """poll_once should return True when an action is taken (e.g. revive)."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    for w in workers:
        w.process._alive = False

    await pilot.poll_once()  # transitions to STUNG
    result = await pilot.poll_once()  # STUNG → REVIVE action
    assert result is True


@pytest.mark.asyncio
async def test_adaptive_backoff(pilot_setup):
    """Idle streak should grow and backoff interval should increase."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    assert pilot._idle_streak == 0

    # All workers BUZZING, no action taken → idle streak should grow
    had_action = await pilot.poll_once()
    assert had_action is False

    # Apply _loop's idle-streak logic (same as pilot._loop)
    if had_action:
        pilot._idle_streak = 0
    else:
        pilot._idle_streak += 1

    assert pilot._idle_streak == 1

    # Second idle poll
    had_action = await pilot.poll_once()
    assert had_action is False
    if had_action:
        pilot._idle_streak = 0
    else:
        pilot._idle_streak += 1

    assert pilot._idle_streak == 2

    # Verify backoff formula: base * 2^min(streak, 3), capped at max
    def expected_backoff(streak):
        return min(
            pilot._base_interval * (2 ** min(streak, 3)),
            pilot._max_interval,
        )

    assert expected_backoff(1) == 2.0  # 1.0 * 2^1
    assert expected_backoff(2) == 4.0  # 1.0 * 2^2
    assert expected_backoff(3) == 8.0  # 1.0 * 2^3
    assert expected_backoff(5) == 8.0  # capped at 2^3 = 8.0


@pytest.mark.asyncio
async def test_adaptive_backoff_resets_on_action(pilot_setup):
    """Idle streak should reset to 0 when an action is taken."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Build up idle streak via actual no-action polls
    for _ in range(3):
        had_action = await pilot.poll_once()
        assert had_action is False
        pilot._idle_streak += 1  # mirrors _loop logic

    assert pilot._idle_streak == 3

    # Force an action (revive via dead process → STUNG → revive)
    for w in workers:
        w.process._alive = False

    await pilot.poll_once()  # transitions to STUNG
    had_action = await pilot.poll_once()  # STUNG → REVIVE
    assert had_action is True

    # Apply _loop's reset logic
    if had_action:
        pilot._idle_streak = 0

    assert pilot._idle_streak == 0


# ── Escalation does NOT reset backoff ─────────────────────────────────


@pytest.mark.asyncio
async def test_escalation_does_not_reset_idle_streak(pilot_setup):
    """Escalation-only actions should NOT reset idle_streak (backoff should grow)."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Build up idle streak
    for _ in range(3):
        had_action = await pilot.poll_once()
        assert had_action is False
        pilot._idle_streak += 1

    assert pilot._idle_streak == 3

    # Make workers STUNG with exhausted revives → ESCALATE decision
    for w in workers:
        w.revive_count = 3
        w.process._alive = False

    await pilot.poll_once()  # transitions to STUNG
    had_action = await pilot.poll_once()  # STUNG + exhausted → ESCALATE
    assert had_action is True  # escalation still counts as had_action

    # But _had_substantive_action should be False (escalation only)
    assert pilot._had_substantive_action is False


@pytest.mark.asyncio
async def test_substantive_action_resets_idle_streak(pilot_setup):
    """CONTINUE and REVIVE actions should reset idle_streak."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # STUNG with revives remaining → REVIVE (substantive)
    for w in workers:
        w.process._alive = False

    await pilot.poll_once()  # transitions to STUNG
    had_action = await pilot.poll_once()  # STUNG → REVIVE
    assert had_action is True
    assert pilot._had_substantive_action is True  # REVIVE is substantive


# ── Skip-decide optimization for escalated workers ────────────────────


@pytest.mark.asyncio
async def test_skip_decide_for_escalated_unchanged_worker(pilot_setup):
    """Already-escalated workers with no state change should skip decide()."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Pre-escalate a worker (use current monotonic time so it doesn't expire)
    import time as _time

    pilot._escalated[workers[0].name] = _time.monotonic()
    # Set prev_state to match current state (no change)
    pilot._prev_states[workers[0].name] = WorkerState.BUZZING

    await pilot.poll_once()

    # The escalated worker should not have any decide-driven log entries
    assert workers[0].name in pilot._escalated  # still escalated


@pytest.mark.asyncio
async def test_escalated_worker_reevaluated_on_state_change(pilot_setup, monkeypatch):
    """When an escalated worker changes state, decide() should run."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Pre-escalate a worker and put it in WAITING state
    import time as _time

    workers[0].state = WorkerState.WAITING
    pilot._escalated[workers[0].name] = _time.monotonic()
    pilot._prev_states[workers[0].name] = WorkerState.WAITING

    # Default mock returns BUZZING content → actual state change
    await pilot.poll_once()

    # BUZZING branch in decide() clears escalation
    assert workers[0].name not in pilot._escalated


# ── Loop termination: empty hive ────────────────────────────────────────


@pytest.mark.asyncio
async def test_loop_exits_on_empty_hive(monkeypatch):
    """_loop should exit and emit hive_empty when all workers are gone."""
    workers = [_make_worker("api")]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=0.01, pool=None, drone_config=DroneConfig())

    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())
    # Set reap timeout to 0 so dead workers are removed immediately after STUNG
    workers[0].stung_reap_timeout = 0.0

    # Kill all processes → STUNG → reaped (0s timeout)
    workers[0].process._alive = False

    events: list[str] = []
    pilot.on_hive_empty(lambda: events.append("hive_empty"))

    pilot.enabled = True
    pilot._running = True
    # Run _loop — should exit after workers are reaped
    await asyncio.wait_for(pilot._loop(), timeout=2.0)

    assert not pilot.enabled
    assert not pilot._running
    assert "hive_empty" in events
    assert len(workers) == 0


# ── Loop termination: hive complete ─────────────────────────────────────


@pytest.mark.asyncio
async def test_hive_complete_emitted(monkeypatch):
    """hive_complete should fire after 3 consecutive all-done polls."""
    workers = [_make_worker("api", state=WorkerState.RESTING)]
    log = DroneLog()

    board = TaskBoard()
    task = board.create("Test task")
    board.assign(task.id, "api")
    board.complete(task.id)

    pilot = DronePilot(
        workers,
        log,
        interval=0.01,
        pool=None,
        drone_config=DroneConfig(auto_stop_on_complete=True),
        task_board=board,
    )

    # Workers are RESTING and all tasks complete.
    # Use idle prompt with suggestion text (classifies as RESTING, not WAITING).
    idle_content = '> Try "how does foo work"\n? for shortcuts'
    workers[0].process.set_content(idle_content)
    workers[0].process._child_foreground_command = "claude"

    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

    events: list[str] = []
    pilot.on_hive_complete(lambda: events.append("hive_complete"))

    pilot.enabled = True
    pilot._running = True
    pilot.mark_completion_seen()  # simulate a task completed this session
    await asyncio.wait_for(pilot._loop(), timeout=2.0)

    assert "hive_complete" in events


@pytest.mark.asyncio
async def test_hive_complete_not_emitted_when_disabled(monkeypatch):
    """auto_stop_on_complete=False should prevent hive_complete."""
    workers = [_make_worker("api", state=WorkerState.RESTING)]
    log = DroneLog()

    board = TaskBoard()
    task = board.create("Test task")
    board.assign(task.id, "api")
    board.complete(task.id)

    pilot = DronePilot(
        workers,
        log,
        interval=0.01,
        pool=None,
        drone_config=DroneConfig(auto_stop_on_complete=False),
        task_board=board,
    )

    # Use idle prompt with suggestion text (classifies as RESTING, not WAITING)
    idle_content = '> Try "how does foo work"\n? for shortcuts'
    workers[0].process.set_content(idle_content)
    workers[0].process._child_foreground_command = "claude"

    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

    events: list[str] = []
    pilot.on_hive_complete(lambda: events.append("hive_complete"))

    pilot.enabled = True
    pilot.mark_completion_seen()  # even with a completion, disabled config blocks it
    # Run a few poll cycles manually (not _loop, since it wouldn't terminate)
    for _ in range(5):
        await pilot.poll_once()

    assert "hive_complete" not in events


@pytest.mark.asyncio
async def test_hive_complete_sets_running_false(monkeypatch):
    """hive_complete should set _running=False so watchdog doesn't restart."""
    workers = [_make_worker("api", state=WorkerState.RESTING)]
    log = DroneLog()

    board = TaskBoard()
    task = board.create("Test task")
    board.assign(task.id, "api")
    board.complete(task.id)

    pilot = DronePilot(
        workers,
        log,
        interval=0.01,
        pool=None,
        drone_config=DroneConfig(auto_stop_on_complete=True),
        task_board=board,
    )

    idle_content = '> Try "how does foo work"\n? for shortcuts'
    workers[0].process.set_content(idle_content)
    workers[0].process._child_foreground_command = "claude"

    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

    pilot.enabled = True
    pilot._running = True
    pilot.mark_completion_seen()  # simulate a task completed this session
    await asyncio.wait_for(pilot._loop(), timeout=2.0)

    assert not pilot._running, "_running should be False after hive_complete"
    assert not pilot.needs_restart(), "watchdog should not restart after hive_complete"


@pytest.mark.asyncio
async def test_hive_complete_not_triggered_on_empty_board(monkeypatch):
    """Empty task board should NOT trigger hive_complete (no tasks ever created)."""
    workers = [_make_worker("api", state=WorkerState.RESTING)]
    log = DroneLog()

    board = TaskBoard()  # empty — no tasks created

    pilot = DronePilot(
        workers,
        log,
        interval=0.01,
        pool=None,
        drone_config=DroneConfig(auto_stop_on_complete=True),
        task_board=board,
    )

    idle_content = '> Try "how does foo work"\n? for shortcuts'
    workers[0].process.set_content(idle_content)
    workers[0].process._child_foreground_command = "claude"

    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

    events: list[str] = []
    pilot.on_hive_complete(lambda: events.append("hive_complete"))

    pilot.enabled = True
    pilot._running = True
    # _loop() would run forever (no hive_complete exit) — use timeout to prove it
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(pilot._loop(), timeout=0.15)

    assert "hive_complete" not in events, "empty board must not trigger hive_complete"
    assert pilot.enabled, "pilot should remain enabled with empty board"


@pytest.mark.asyncio
async def test_hive_complete_not_triggered_on_stale_completions(monkeypatch):
    """Completed tasks from a previous session should NOT trigger hive_complete."""
    workers = [_make_worker("api", state=WorkerState.RESTING)]
    log = DroneLog()

    # Board with all-completed tasks (as if loaded from persistent store)
    board = TaskBoard()
    task = board.create("Old task")
    board.assign(task.id, "api")
    board.complete(task.id)

    pilot = DronePilot(
        workers,
        log,
        interval=0.01,
        pool=None,
        drone_config=DroneConfig(auto_stop_on_complete=True),
        task_board=board,
    )
    # _saw_completion defaults to False — no task was completed this session

    idle_content = '> Try "how does foo work"\n? for shortcuts'
    workers[0].process.set_content(idle_content)
    workers[0].process._child_foreground_command = "claude"

    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

    events: list[str] = []
    pilot.on_hive_complete(lambda: events.append("hive_complete"))

    pilot.enabled = True
    pilot._running = True
    # _loop() would run forever (stale completions don't trigger auto-stop)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(pilot._loop(), timeout=0.15)

    assert "hive_complete" not in events, "stale completions must not trigger hive_complete"
    assert pilot.enabled, "pilot should remain enabled with stale completions"


@pytest.mark.asyncio
async def test_loop_cancelled_no_error(monkeypatch):
    """Cancelling the loop (Ctrl+C shutdown) should not log ERROR."""
    import logging

    workers = [_make_worker("api")]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=0.1, pool=None, drone_config=DroneConfig())

    workers[0].process.set_content("esc to interrupt")
    workers[0].process._child_foreground_command = "claude"

    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

    pilot.enabled = True
    pilot._running = True
    task = asyncio.create_task(pilot._loop())

    # Let it start one cycle then cancel
    await asyncio.sleep(0.05)
    task.cancel()

    errors: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda r: errors.append(r.getMessage()) if r.levelno >= logging.ERROR else None
    logger = logging.getLogger("swarm.drones.pilot")
    logger.addHandler(handler)
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        logger.removeHandler(handler)

    assert not errors, f"CancelledError should not produce ERROR logs: {errors}"


@pytest.mark.asyncio
async def test_wait_directive_no_warning():
    """Queen 'wait' directive should not produce a warning."""
    import logging

    workers = [_make_worker("api")]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())

    warnings: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda r: (
        warnings.append(r.getMessage()) if r.levelno >= logging.WARNING else None
    )
    logger = logging.getLogger("swarm.drones.pilot")
    logger.addHandler(handler)

    try:
        result = await pilot._execute_directives(
            [{"worker": "api", "action": "wait", "reason": "worker is busy"}]
        )
    finally:
        logger.removeHandler(handler)

    # "wait" is a no-op — should not count as an executed directive
    assert result is False
    assert not warnings, f"'wait' directive should not produce warnings: {warnings}"


@pytest.mark.asyncio
async def test_on_loop_done_normal_exit_not_warning():
    """Normal loop exit (hive_complete) should not log WARNING."""
    import logging

    warnings: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda r: (
        warnings.append(r.getMessage()) if r.levelno >= logging.WARNING else None
    )
    logger = logging.getLogger("swarm.drones.pilot")
    logger.addHandler(handler)

    try:
        # Simulate a normally-exited task
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        PollDispatcher._on_loop_done(task)
    finally:
        logger.removeHandler(handler)

    assert not warnings, f"Normal exit should not produce WARNING logs: {warnings}"


# ── Circuit breaker ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_circuit_breaker_trips(monkeypatch):
    """Worker with N consecutive poll failures should be treated as dead."""
    workers = [_make_worker("api"), _make_worker("web")]
    log = DroneLog()
    max_failures = 3
    pilot = DronePilot(
        workers,
        log,
        interval=1.0,
        pool=None,
        drone_config=DroneConfig(max_poll_failures=max_failures),
    )

    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

    # Make "api" process throw on get_content, "web" works normally
    workers[1].process.set_content("esc to interrupt")
    workers[1].process._child_foreground_command = "claude"

    def failing_get_content(lines=35):
        raise OSError("simulated failure")

    def failing_get_styled_content(lines=35):
        raise OSError("simulated failure")

    workers[0].process.get_content = failing_get_content
    workers[0].process.get_styled_content = failing_get_styled_content

    changes: list[int] = []
    pilot.on_workers_changed(lambda: changes.append(1))

    # Poll N-1 times: should NOT remove the worker yet
    for _ in range(max_failures - 1):
        await pilot.poll_once()

    assert len(workers) == 2  # both still alive
    count, _ = pilot._poll_failures.get("api", (0, 0.0))
    assert count == max_failures - 1

    # One more poll: circuit breaker trips
    await pilot.poll_once()

    assert len(workers) == 1
    assert workers[0].name == "web"
    assert len(changes) == 1  # workers_changed fired once
    # Failure counter cleaned up
    assert "api" not in pilot._poll_failures


@pytest.mark.asyncio
async def test_circuit_breaker_resets_on_success(pilot_setup):
    """Successful poll should reset the failure counter for a worker."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Seed some failures
    import time as _time

    pilot._poll_failures["api"] = (3, _time.monotonic())

    await pilot.poll_once()

    # After successful poll, counter should be cleared
    assert "api" not in pilot._poll_failures


# ── Dead worker task redistribution ─────────────────────────────────────


@pytest.mark.asyncio
async def test_dead_worker_unassigns_tasks(monkeypatch):
    """When a dead worker is removed, its assigned tasks should be unassigned."""
    workers = [_make_worker("api")]
    log = DroneLog()

    board = TaskBoard()
    task = board.create("Build API")
    board.assign(task.id, "api")
    assert task.status == TaskStatus.ASSIGNED
    assert task.assigned_worker == "api"

    pilot = DronePilot(
        workers,
        log,
        interval=1.0,
        pool=None,
        drone_config=DroneConfig(),
        task_board=board,
    )

    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())
    # Set reap timeout to 0 so dead workers are removed immediately after STUNG
    workers[0].stung_reap_timeout = 0.0

    # Kill the process → STUNG → reaped (0s timeout)
    workers[0].process._alive = False

    await pilot.poll_once()  # transitions to STUNG
    await pilot.poll_once()  # reaped (0s timeout)

    # Worker removed
    assert len(workers) == 0
    # Task should be back to PENDING
    assert task.status == TaskStatus.UNASSIGNED
    assert task.assigned_worker is None


@pytest.mark.asyncio
async def test_circuit_breaker_dead_worker_unassigns_tasks(monkeypatch):
    """Circuit-breaker-killed worker's tasks should also be unassigned."""
    workers = [_make_worker("api")]
    log = DroneLog()

    board = TaskBoard()
    task = board.create("Build API")
    board.assign(task.id, "api")

    pilot = DronePilot(
        workers,
        log,
        interval=1.0,
        pool=None,
        drone_config=DroneConfig(max_poll_failures=2),
        task_board=board,
    )

    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

    # Make process throw on get_content / get_styled_content
    def failing_get_content(lines=35):
        raise OSError("boom")

    workers[0].process.get_content = failing_get_content
    workers[0].process.get_styled_content = failing_get_content

    # 2 failures → circuit breaker trips
    await pilot.poll_once()
    await pilot.poll_once()

    assert len(workers) == 0
    assert task.status == TaskStatus.UNASSIGNED
    assert task.assigned_worker is None


class TestTaskCompletionReproposal:
    """Completion re-proposal after cooldown when Queen initially says 'not done'."""

    def _make_pilot_with_board(self):
        workers = [_make_worker("api", state=WorkerState.RESTING, resting_since=0)]
        log = DroneLog()
        board = TaskBoard()
        pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())
        pilot.task_board = board
        pilot.enabled = True
        # Shorten cooldown for tests
        pilot._COMPLETION_REPROPOSE_COOLDOWN = 60
        return pilot, workers, board, log

    def test_first_proposal_fires(self):
        """First idle check should emit task_done."""
        pilot, workers, board, log = self._make_pilot_with_board()

        workers[0].state_since = time.time() - 120  # idle for 2 min

        task = board.create("Fix bug")
        board.assign(task.id, "api")

        events = []
        pilot.on("task_done", lambda w, t, r: events.append((w.name, t.id)))

        pilot._check_task_completions()
        assert len(events) == 1
        assert events[0] == ("api", task.id)

    def test_second_check_within_cooldown_skips(self):
        """Within cooldown, same task should not be re-proposed."""
        pilot, workers, board, log = self._make_pilot_with_board()

        workers[0].state_since = time.time() - 120

        task = board.create("Fix bug")
        board.assign(task.id, "api")

        events = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        pilot._check_task_completions()
        pilot._check_task_completions()  # within cooldown
        assert len(events) == 1  # only fired once

    def test_reproposal_after_cooldown(self):
        """After cooldown expires, task should be re-proposed."""
        pilot, workers, board, log = self._make_pilot_with_board()

        workers[0].state_since = time.time() - 120

        task = board.create("Fix bug")
        board.assign(task.id, "api")

        events = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        pilot._check_task_completions()
        assert len(events) == 1

        # Simulate cooldown expiry by backdating the timestamp
        pilot._proposed_completions[task.id] = time.time() - 120  # 2 min ago, > 60s cooldown

        pilot._check_task_completions()
        assert len(events) == 2  # fired again

    def test_clear_proposed_allows_immediate_reproposal(self):
        """clear_proposed_completion should allow immediate re-proposal."""
        pilot, workers, board, log = self._make_pilot_with_board()

        workers[0].state_since = time.time() - 120

        task = board.create("Fix bug")
        board.assign(task.id, "api")

        events = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        pilot._check_task_completions()
        assert len(events) == 1

        pilot.clear_proposed_completion(task.id)

        pilot._check_task_completions()
        assert len(events) == 2

    def test_high_conf_not_done_extends_cooldown(self):
        """After Queen verdict done=False conf>=0.8, the re-propose cooldown
        must extend to _HIGH_CONF_NOT_DONE_BACKOFF so we stop burning LLM
        calls re-asking about unchanged state (task #254 — audit spec Task A).
        """
        pilot, workers, board, log = self._make_pilot_with_board()

        workers[0].state_since = time.time() - 120
        task = board.create("Fix bug")
        board.assign(task.id, "api")

        events = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        # First proposal fires normally.
        pilot._check_task_completions()
        assert len(events) == 1

        # Queen returns done=False with high confidence.
        pilot.record_completion_verdict(task.id, done=False, confidence=0.95)

        # Simulate normal (short) cooldown expiring.
        pilot._proposed_completions[task.id] = time.time() - 120  # > 60s cooldown
        # But the high-conf-not-done backoff is 30 min — should still skip.
        pilot._check_task_completions()
        assert len(events) == 1, "high-conf-not-done should suppress re-proposal"

    def test_low_conf_not_done_does_not_extend_cooldown(self):
        """When Queen's confidence is below the threshold, fall back to the
        normal short cooldown — the verdict is too weak to trust."""
        pilot, workers, board, log = self._make_pilot_with_board()

        workers[0].state_since = time.time() - 120
        task = board.create("Fix bug")
        board.assign(task.id, "api")

        events = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        pilot._check_task_completions()
        assert len(events) == 1

        # Low-confidence not-done — e.g. Queen uncertain, could still be done soon.
        pilot.record_completion_verdict(task.id, done=False, confidence=0.3)
        pilot._proposed_completions[task.id] = time.time() - 120  # past short cooldown
        pilot._check_task_completions()
        assert len(events) == 2, "low-conf verdict should NOT extend the cooldown"

    def test_done_true_verdict_clears_backoff(self):
        """A done=True verdict must clear the backoff entry so completion
        can proceed through the proposal path on the next check."""
        pilot, workers, board, log = self._make_pilot_with_board()

        workers[0].state_since = time.time() - 120
        task = board.create("Fix bug")
        board.assign(task.id, "api")

        events = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        pilot._check_task_completions()
        pilot.record_completion_verdict(task.id, done=False, confidence=0.95)
        assert task.id in pilot._task_lifecycle._completion_verdicts

        # Queen now confirms the task IS done.
        pilot.record_completion_verdict(task.id, done=True, confidence=0.95)
        assert task.id not in pilot._task_lifecycle._completion_verdicts

    def test_high_conf_backoff_expires_eventually(self):
        """After _HIGH_CONF_NOT_DONE_BACKOFF seconds, re-proposal resumes."""
        pilot, workers, board, log = self._make_pilot_with_board()
        workers[0].state_since = time.time() - 120
        task = board.create("Fix bug")
        board.assign(task.id, "api")

        events = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        pilot._check_task_completions()
        pilot.record_completion_verdict(task.id, done=False, confidence=0.95)

        # Backdate the verdict past the 30-min backoff window.
        lifecycle = pilot._task_lifecycle
        ts, done, conf = lifecycle._completion_verdicts[task.id]
        lifecycle._completion_verdicts[task.id] = (
            ts - lifecycle._HIGH_CONF_NOT_DONE_BACKOFF - 10,
            done,
            conf,
        )
        # Also backdate the short cooldown so only the verdict backoff matters.
        pilot._proposed_completions[task.id] = time.time() - 120

        pilot._check_task_completions()
        assert len(events) == 2, "backoff should expire; re-proposal should fire"


# ── _auto_assign_tasks ──────────────────────────────────────────────────


class TestAutoAssignTasks:
    """Tests for the _auto_assign_tasks Queen-driven assignment flow."""

    def _make_pilot_with_queen(self, monkeypatch, workers=None, tasks=None):
        """Helper: build pilot with a mocked Queen and populated task board."""
        if workers is None:
            workers = [_make_worker("api", state=WorkerState.RESTING)]
        log = DroneLog()
        board = TaskBoard()
        for t in tasks or []:
            board.add(t)

        queen = AsyncMock()
        queen.can_call = True
        queen.enabled = True
        queen.min_confidence = 0.7

        pilot = DronePilot(
            workers,
            log,
            interval=1.0,
            pool=None,
            drone_config=DroneConfig(),
            task_board=board,
            queen=queen,
        )
        pilot.enabled = True

        # Mock build_hive_context
        monkeypatch.setattr("swarm.queen.context.build_hive_context", lambda *a, **kw: "ctx")
        return pilot, workers, board, queen, log

    @pytest.mark.asyncio
    async def test_auto_assign_no_queen(self, monkeypatch):
        """Returns False when queen is None."""
        pilot, _, board, _, _ = self._make_pilot_with_queen(monkeypatch)
        pilot.queen = None
        result = await pilot._auto_assign_tasks()
        assert result is False

    @pytest.mark.asyncio
    async def test_auto_assign_queen_cannot_call(self, monkeypatch):
        """Returns False when queen.can_call is False."""
        pilot, _, board, queen, _ = self._make_pilot_with_queen(monkeypatch)
        queen.can_call = False
        result = await pilot._auto_assign_tasks()
        assert result is False

    @pytest.mark.asyncio
    async def test_auto_assign_no_available_tasks(self, monkeypatch):
        """Returns False when no available tasks exist."""
        pilot, _, board, queen, _ = self._make_pilot_with_queen(monkeypatch)
        # Board is empty — no tasks
        result = await pilot._auto_assign_tasks()
        assert result is False

    @pytest.mark.asyncio
    async def test_auto_assign_no_idle_workers(self, monkeypatch):
        """Returns False when all workers are BUZZING (none idle)."""
        from swarm.tasks.task import SwarmTask

        workers = [_make_worker("api", state=WorkerState.BUZZING)]
        task = SwarmTask(title="Build API", description="Build the REST API")
        pilot, _, board, queen, _ = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )
        result = await pilot._auto_assign_tasks()
        assert result is False

    @pytest.mark.asyncio
    async def test_auto_assign_skips_worker_with_active_task(self, monkeypatch):
        """Workers with already-assigned tasks should not get new assignments."""
        from swarm.tasks.task import SwarmTask

        task1 = SwarmTask(title="Existing task")
        task2 = SwarmTask(title="New task")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, _ = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task1, task2]
        )
        # Assign task1 to api so it has an active task
        board.assign(task1.id, "api")

        result = await pilot._auto_assign_tasks()
        assert result is False
        queen.assign_tasks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_assign_success_emits_proposal(self, monkeypatch):
        """Successful Queen assignment should emit a proposal event when auto-approve is off."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API", description="REST API")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, log = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )
        pilot.drone_config = DroneConfig(auto_approve_assignments=False)

        queen.assign_tasks.return_value = [
            {
                "worker": "api",
                "task_id": task.id,
                "message": "Build the REST API",
                "reasoning": "Best match",
                "confidence": 0.9,
            }
        ]

        proposals = []
        pilot.on_proposal(lambda p: proposals.append(p))

        result = await pilot._auto_assign_tasks()
        assert result is True
        assert len(proposals) == 1
        assert proposals[0].worker_name == "api"
        assert proposals[0].task_id == task.id

    @pytest.mark.asyncio
    async def test_auto_assign_skips_invalid_assignment(self, monkeypatch):
        """Assignments referencing unknown workers or tasks should be skipped."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, _ = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )

        queen.assign_tasks.return_value = [
            {
                "worker": "nonexistent",
                "task_id": task.id,
                "message": "Do stuff",
            }
        ]

        proposals = []
        pilot.on_proposal(lambda p: proposals.append(p))

        result = await pilot._auto_assign_tasks()
        assert result is False
        assert len(proposals) == 0

    @pytest.mark.asyncio
    async def test_auto_assign_skips_non_dict_entries(self, monkeypatch):
        """Non-dict assignment entries from Queen should be skipped."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, _ = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )

        queen.assign_tasks.return_value = ["not-a-dict", 42, None]

        proposals = []
        pilot.on_proposal(lambda p: proposals.append(p))

        result = await pilot._auto_assign_tasks()
        assert result is False
        assert len(proposals) == 0

    @pytest.mark.asyncio
    async def test_auto_assign_queen_error_returns_false(self, monkeypatch):
        """Queen raising an exception should not crash; returns False."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, _ = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )

        queen.assign_tasks.side_effect = RuntimeError("Queen crashed")

        result = await pilot._auto_assign_tasks()
        assert result is False

    @pytest.mark.asyncio
    async def test_auto_assign_includes_worker_with_completed_task(self, monkeypatch):
        """Workers whose only tasks are COMPLETED should be considered idle."""
        from swarm.tasks.task import SwarmTask

        task1 = SwarmTask(title="Old task")
        task2 = SwarmTask(title="New task")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, _ = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task1, task2]
        )
        # Complete task1 — it remains assigned_worker="api" but status=COMPLETED
        board.assign(task1.id, "api")
        board.complete(task1.id)

        queen.assign_tasks.return_value = [
            {
                "worker": "api",
                "task_id": task2.id,
                "message": "Do the new task",
                "confidence": 0.9,
            }
        ]

        assigned = []
        pilot.on_task_assigned(lambda w, t, m="": assigned.append((w.name, t.id)))

        result = await pilot._auto_assign_tasks()
        assert result is True
        assert len(assigned) == 1
        assert assigned[0] == ("api", task2.id)

    @pytest.mark.asyncio
    async def test_auto_assign_skips_worker_with_pending_proposal(self, monkeypatch):
        """Workers with pending proposals should be excluded from auto-assign."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        pilot, _, board, queen, _ = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )
        # Per-worker proposal check returns True for "api"
        pilot.set_pending_proposals_for_worker(lambda name: name == "api")

        result = await pilot._auto_assign_tasks()
        assert result is False
        queen.assign_tasks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_assign_allows_other_workers_when_one_has_proposal(self, monkeypatch):
        """Per-worker proposal check should only block the specific worker."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        workers = [
            _make_worker("api", state=WorkerState.RESTING),
            _make_worker("web", state=WorkerState.RESTING),
        ]
        pilot, _, board, queen, _ = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )
        # "api" has a pending proposal, "web" does not
        pilot.set_pending_proposals_for_worker(lambda name: name == "api")

        queen.assign_tasks.return_value = [
            {
                "worker": "web",
                "task_id": task.id,
                "message": "Do it",
                "confidence": 0.9,
            }
        ]

        assigned = []
        pilot.on_task_assigned(lambda w, t, m="": assigned.append(w.name))

        result = await pilot._auto_assign_tasks()
        assert result is True
        # Queen should only have been called with "web" (api filtered out)
        call_args = queen.assign_tasks.call_args
        assert "api" not in call_args[0][0]  # first positional arg = idle worker names
        assert "web" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_auto_assign_skips_already_assigned_task(self, monkeypatch):
        """Task already assigned (not available) should be skipped even if Queen returns it."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        workers = [
            _make_worker("api", state=WorkerState.RESTING),
            _make_worker("web", state=WorkerState.RESTING),
        ]
        pilot, _, board, queen, _ = self._make_pilot_with_queen(
            monkeypatch, workers=workers, tasks=[task]
        )
        # Assign task to "web" so it is no longer available
        board.assign(task.id, "web")

        # Queen proposes assigning it to "api" — should be skipped
        queen.assign_tasks.return_value = [
            {
                "worker": "api",
                "task_id": task.id,
                "message": "Do it",
            }
        ]

        proposals = []
        pilot.on_proposal(lambda p: proposals.append(p))

        result = await pilot._auto_assign_tasks()
        assert result is False
        assert len(proposals) == 0


# ── Project affinity routing (task #341) ─────────────────────────────────


class TestAutoAssignAffinityRouting:
    """Tests for the deterministic project-affinity gate that bypasses Queen.

    Models the bug from task #341: an assigner that latches onto generic
    keywords ("admin", "database") routes a task to the wrong worker
    because it lacks project-affinity scoping.
    """

    def _build_pilot(self, monkeypatch, workers, tasks):
        from swarm.tasks.board import TaskBoard

        log = DroneLog()
        board = TaskBoard()
        for t in tasks:
            board.add(t)
        queen = AsyncMock()
        queen.can_call = True
        queen.enabled = True
        queen.min_confidence = 0.7
        pilot = DronePilot(
            workers,
            log,
            interval=1.0,
            pool=None,
            drone_config=DroneConfig(),
            task_board=board,
            queen=queen,
        )
        pilot.enabled = True
        monkeypatch.setattr("swarm.queen.context.build_hive_context", lambda *a, **kw: "ctx")
        return pilot, board, queen, log

    @pytest.mark.asyncio
    async def test_explicit_project_name_pins_to_owner(self, monkeypatch):
        """A task that names a project routes deterministically to its worker."""
        from swarm.tasks.task import SwarmTask
        from swarm.worker.worker import Worker
        from tests.fakes.process import FakeWorkerProcess

        # Repos: budgetbug, rcg-platform — workers: budgetbug, platform
        workers = [
            Worker(
                name="budgetbug",
                path="/home/op/projects/personal/budgetbug",
                process=FakeWorkerProcess(name="budgetbug"),
                state=WorkerState.RESTING,
            ),
            Worker(
                name="platform",
                path="/home/op/projects/rcg-platform",
                process=FakeWorkerProcess(name="platform"),
                state=WorkerState.RESTING,
            ),
        ]
        task = SwarmTask(
            title="budgetbug: ship v1.2.0",
            description="Verify budgetbug deploy succeeded.",
        )
        pilot, _, queen, log = self._build_pilot(monkeypatch, workers, [task])

        assigned: list[tuple[str, str]] = []
        pilot.on_task_assigned(lambda w, t, m="": assigned.append((w.name, t.id)))

        result = await pilot._auto_assign_tasks()
        assert result is True
        # Queen MUST NOT be called — affinity routed deterministically.
        queen.assign_tasks.assert_not_awaited()
        assert assigned == [("budgetbug", task.id)]

    @pytest.mark.asyncio
    async def test_operator_engagement_overrides_generic_keywords(self, monkeypatch):
        """Models the task #341 incident exactly.

        Task: 'Verify database backup functionality and diagnose why admin
        shows no backups' — generic keywords. Operator was driving budgetbug
        interactively. The assigner must NOT route to admin or platform.
        """
        from swarm.tasks.task import SwarmTask
        from swarm.worker.worker import Worker
        from tests.fakes.process import FakeWorkerProcess

        budgetbug_proc = FakeWorkerProcess(name="budgetbug")
        budgetbug_proc.mark_user_input()  # operator just typed in budgetbug PTY

        workers = [
            Worker(
                name="budgetbug",
                path="/home/op/projects/personal/budgetbug",
                process=budgetbug_proc,
                state=WorkerState.RESTING,
            ),
            Worker(
                name="admin",
                path="/home/op/projects/rcg-admin",
                process=FakeWorkerProcess(name="admin"),
                state=WorkerState.RESTING,
            ),
            Worker(
                name="platform",
                path="/home/op/projects/rcg-platform",
                process=FakeWorkerProcess(name="platform"),
                state=WorkerState.RESTING,
            ),
        ]
        task = SwarmTask(
            title="Verify database backup functionality",
            description="Diagnose why admin shows no backups.",
        )
        pilot, _, queen, log = self._build_pilot(monkeypatch, workers, [task])

        assigned: list[tuple[str, str]] = []
        pilot.on_task_assigned(lambda w, t, m="": assigned.append((w.name, t.id)))

        result = await pilot._auto_assign_tasks()
        assert result is True
        queen.assign_tasks.assert_not_awaited()
        # Engagement signal pins to budgetbug despite "admin" keyword
        assert assigned == [("budgetbug", task.id)]

    @pytest.mark.asyncio
    async def test_no_signal_falls_back_to_queen(self, monkeypatch):
        """Generic task with no engagement still goes through the Queen."""
        from swarm.tasks.task import SwarmTask
        from swarm.worker.worker import Worker
        from tests.fakes.process import FakeWorkerProcess

        workers = [
            Worker(
                name="api",
                path="/tmp/api",
                process=FakeWorkerProcess(name="api"),
                state=WorkerState.RESTING,
            ),
            Worker(
                name="web",
                path="/tmp/web",
                process=FakeWorkerProcess(name="web"),
                state=WorkerState.RESTING,
            ),
        ]
        task = SwarmTask(title="Refactor utility module", description="Clean up helpers.")
        pilot, _, queen, _ = self._build_pilot(monkeypatch, workers, [task])
        queen.assign_tasks.return_value = [
            {
                "worker": "api",
                "task_id": task.id,
                "message": "Refactor",
                "confidence": 0.92,
            }
        ]
        assigned: list[str] = []
        pilot.on_task_assigned(lambda w, t, m="": assigned.append(w.name))

        result = await pilot._auto_assign_tasks()
        assert result is True
        # Queen was called and Queen's pick was used
        queen.assign_tasks.assert_awaited_once()
        assert assigned == ["api"]

    @pytest.mark.asyncio
    async def test_low_confidence_low_affinity_parks_in_backlog(self, monkeypatch):
        """Both affinity and Queen confidence below floor → backlog (no emit)."""
        from swarm.tasks.task import SwarmTask
        from swarm.worker.worker import Worker
        from tests.fakes.process import FakeWorkerProcess

        workers = [
            Worker(
                name="api",
                path="/tmp/api",
                process=FakeWorkerProcess(name="api"),
                state=WorkerState.RESTING,
            ),
            Worker(
                name="web",
                path="/tmp/web",
                process=FakeWorkerProcess(name="web"),
                state=WorkerState.RESTING,
            ),
        ]
        task = SwarmTask(
            title="Vague chore",
            description="Some kind of cleanup.",
        )
        pilot, _, queen, log = self._build_pilot(monkeypatch, workers, [task])
        # Queen returns a low-confidence pick AND no worker has affinity
        queen.assign_tasks.return_value = [
            {
                "worker": "api",
                "task_id": task.id,
                "message": "Do it",
                "confidence": 0.30,  # below default floor 0.5
            }
        ]
        assigned: list[str] = []
        proposals: list[object] = []
        pilot.on_task_assigned(lambda w, t, m="": assigned.append(w.name))
        pilot.on_proposal(lambda p: proposals.append(p))

        await pilot._auto_assign_tasks()
        # No assignment emitted, no proposal — task stays in backlog
        assert assigned == []
        assert proposals == []
        # Buzz-log records the skip
        assert any(e.action == SystemAction.AUTO_ASSIGN_BACKLOG_SKIPPED for e in log.entries)

    @pytest.mark.asyncio
    async def test_queen_picks_wrong_worker_affinity_overrides_to_backlog(self, monkeypatch):
        """If Queen picks a worker but a different worker has higher affinity, park in backlog."""
        from swarm.tasks.task import SwarmTask
        from swarm.worker.worker import Worker
        from tests.fakes.process import FakeWorkerProcess

        workers = [
            Worker(
                name="budgetbug",
                path="/home/op/projects/personal/budgetbug",
                process=FakeWorkerProcess(name="budgetbug"),
                state=WorkerState.RESTING,
            ),
            Worker(
                name="platform",
                path="/home/op/projects/rcg-platform",
                process=FakeWorkerProcess(name="platform"),
                state=WorkerState.RESTING,
            ),
        ]
        # Unambiguous budgetbug task — but engagement signal absent and the
        # task title alone routes through the deterministic phase. Force the
        # Queen path by giving the title a budgetbug-affinity-blocked shape.
        task = SwarmTask(
            title="budgetbug: ship release",
            description="Cut budgetbug v1.2.0.",
        )
        pilot, _, queen, log = self._build_pilot(monkeypatch, workers, [task])
        # Pre-occupy budgetbug so the deterministic phase can't pin it,
        # forcing Phase 2 (Queen) — Queen then picks platform (wrong).
        from swarm.tasks.task import SwarmTask as _Task

        decoy = _Task(title="placeholder")
        pilot.task_board.add(decoy)
        pilot.task_board.assign(decoy.id, "budgetbug")
        queen.assign_tasks.return_value = [
            {
                "worker": "platform",
                "task_id": task.id,
                "message": "Cut release",
                "confidence": 0.92,
            }
        ]
        assigned: list[str] = []
        pilot.on_task_assigned(lambda w, t, m="": assigned.append(w.name))

        await pilot._auto_assign_tasks()
        # Queen picked platform — but budgetbug had higher affinity. Since
        # budgetbug isn't actually idle, the override parks the task.
        assert assigned == []
        assert any(e.action == SystemAction.AUTO_ASSIGN_BACKLOG_SKIPPED for e in log.entries)


# ── Circuit breaker recovery ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_circuit_breaker_recovery_on_successful_poll(monkeypatch):
    """A successful poll after failures should clear the failure counter (not trip)."""
    workers = [_make_worker("api")]
    log = DroneLog()
    pilot = DronePilot(
        workers,
        log,
        interval=1.0,
        pool=None,
        drone_config=DroneConfig(max_poll_failures=5),
    )
    pilot.enabled = True

    # Set BUZZING content
    workers[0].process.set_content("esc to interrupt")
    workers[0].process._child_foreground_command = "claude"

    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

    # Seed failures just below threshold
    import time as _time2

    pilot._poll_failures["api"] = (4, _time2.monotonic())

    # Now poll succeeds
    await pilot.poll_once()

    # Worker should still be alive
    assert len(workers) == 1
    # Failure counter should be reset
    assert "api" not in pilot._poll_failures


# ── _check_task_completions edge cases ───────────────────────────────────


class TestAutoCompleteMinIdleConfig:
    """auto_complete_min_idle should be configurable from DroneConfig."""

    def test_default_value(self):
        """Default auto_complete_min_idle is 45s."""
        pilot = DronePilot([], DroneLog(), drone_config=DroneConfig())
        assert pilot._auto_complete_min_idle == 45.0

    def test_config_override(self):
        """DroneConfig.auto_complete_min_idle flows to pilot instance attribute."""
        cfg = DroneConfig(auto_complete_min_idle=10.0)
        pilot = DronePilot([], DroneLog(), drone_config=cfg)
        assert pilot._auto_complete_min_idle == 10.0

    def test_completion_uses_config_value(self):
        """_check_task_completions should respect the configured threshold."""

        workers = [_make_worker("api", state=WorkerState.RESTING, resting_since=0)]
        log = DroneLog()
        board = TaskBoard()
        # Set low threshold (15s)
        cfg = DroneConfig(auto_complete_min_idle=15.0)
        pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=cfg)
        pilot.task_board = board
        pilot.enabled = True

        # Worker idle for 20s (above 15s threshold, below default 45s)
        workers[0].state_since = time.time() - 20

        task = board.create("Fix bug")
        board.assign(task.id, "api")

        events: list[str] = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        pilot._check_task_completions()
        assert len(events) == 1  # triggered at 20s with 15s threshold

    def test_completion_blocked_below_threshold(self):
        """Worker idle below configured threshold should not propose completion."""

        workers = [_make_worker("api", state=WorkerState.RESTING, resting_since=0)]
        log = DroneLog()
        board = TaskBoard()
        cfg = DroneConfig(auto_complete_min_idle=60.0)
        pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=cfg)
        pilot.task_board = board
        pilot.enabled = True

        # Worker idle for 45s (above default 45s, but below configured 60s)
        workers[0].state_since = time.time() - 45

        task = board.create("Fix bug")
        board.assign(task.id, "api")

        events: list[str] = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        pilot._check_task_completions()
        assert len(events) == 0  # blocked by 60s threshold


class TestCheckTaskCompletionsEdgeCases:
    """Additional edge cases for _check_task_completions."""

    def _make_pilot_with_board(self):
        workers = [_make_worker("api", state=WorkerState.RESTING, resting_since=0)]
        log = DroneLog()
        board = TaskBoard()
        pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())
        pilot.task_board = board
        pilot.enabled = True
        pilot._COMPLETION_REPROPOSE_COOLDOWN = 60
        return pilot, workers, board, log

    def test_no_task_board_returns_false(self):
        """_check_task_completions should return False when task_board is None."""
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        log = DroneLog()
        pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())
        pilot.task_board = None
        result = pilot._check_task_completions()
        assert result is False

    def test_worker_not_resting_skipped(self):
        """Workers that are not RESTING should be skipped."""
        pilot, workers, board, log = self._make_pilot_with_board()

        workers[0].state = WorkerState.BUZZING
        workers[0].state_since = time.time() - 120

        task = board.create("Fix bug")
        board.assign(task.id, "api")

        events = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        result = pilot._check_task_completions()
        assert result is False
        assert len(events) == 0

    def test_worker_idle_too_short_skipped(self):
        """Workers idle for less than the minimum should be skipped."""
        pilot, workers, board, log = self._make_pilot_with_board()

        # Idle for only 10 seconds (below 45s threshold)
        workers[0].state_since = time.time() - 10

        task = board.create("Fix bug")
        board.assign(task.id, "api")

        events = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        result = pilot._check_task_completions()
        assert result is False
        assert len(events) == 0

    def test_completed_tasks_not_proposed(self):
        """Tasks already completed should not be proposed."""
        pilot, workers, board, log = self._make_pilot_with_board()

        workers[0].state_since = time.time() - 120

        task = board.create("Fix bug")
        board.assign(task.id, "api")
        board.complete(task.id)

        events = []
        pilot.on("task_done", lambda w, t, r: events.append(t.id))

        result = pilot._check_task_completions()
        assert result is False
        assert len(events) == 0


# ── poll_once coordination triggers ──────────────────────────────────
# Removed — _coordination_cycle deleted in task #253 spec B.  See
# docs/specs/headless-queen-architecture.md.


# ── poll_once integration with _auto_assign_tasks ────────────────────────


@pytest.mark.asyncio
async def test_poll_once_calls_auto_assign(monkeypatch):
    """poll_once should invoke _auto_assign_tasks when enabled with queen and board."""
    from swarm.tasks.task import SwarmTask

    workers = [_make_worker("api", state=WorkerState.RESTING)]
    log = DroneLog()
    board = TaskBoard()
    task = SwarmTask(title="Build API")
    board.add(task)

    queen = AsyncMock()
    queen.can_call = True
    queen.enabled = True
    queen.min_confidence = 0.7
    queen.assign_tasks.return_value = [
        {
            "worker": "api",
            "task_id": task.id,
            "message": "Do it",
            "confidence": 0.9,
        }
    ]

    pilot = DronePilot(
        workers,
        log,
        interval=1.0,
        pool=None,
        drone_config=DroneConfig(),
        task_board=board,
        queen=queen,
    )
    pilot.enabled = True

    # RESTING content
    idle_content = '> Try "how does foo work"\n? for shortcuts'
    workers[0].process.set_content(idle_content)
    workers[0].process._child_foreground_command = "claude"

    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())
    monkeypatch.setattr("swarm.queen.context.build_hive_context", lambda *a, **kw: "ctx")

    assigned = []
    pilot.on_task_assigned(lambda w, t, m="": assigned.append((w.name, t.id)))

    result = await pilot.poll_once()

    assert result is True
    assert len(assigned) == 1  # auto-approved (confidence 0.9 >= 0.7)
    queen.assign_tasks.assert_awaited_once()


# ── Display-state transition emits state_changed ─────────────────────────


@pytest.mark.asyncio
async def test_display_state_transition_emits_state_changed(monkeypatch):
    """RESTING→SLEEPING display_state transition should emit state_changed."""

    workers = [_make_worker("api", state=WorkerState.RESTING, resting_since=time.time() - 1500)]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())
    pilot.enabled = True

    assert workers[0].display_state == WorkerState.SLEEPING

    idle_content = '> Try "how does foo work"\n? for shortcuts'
    workers[0].process.set_content(idle_content)
    workers[0].process._child_foreground_command = "claude"

    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

    state_changes: list[str] = []
    pilot.on_state_changed(lambda w: state_changes.append(w.name))

    await pilot.poll_once()

    # state_changed should have been emitted from the display_state divergence path
    assert "api" in state_changes


# ── Focus backoff cap ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_focus_caps_backoff(pilot_setup):
    """Setting _focused_workers should cap backoff at _focus_interval for idle workers."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Transition the focused worker to RESTING (3 confirmations required)
    workers[0].update_state(WorkerState.RESTING)
    workers[0].update_state(WorkerState.RESTING)
    workers[0].update_state(WorkerState.RESTING)
    assert workers[0].state == WorkerState.RESTING

    # Build up idle streak to get high backoff
    pilot._idle_streak = 5

    # Without focus, backoff should be high
    normal_backoff = pilot._compute_backoff()
    assert normal_backoff > pilot._focus_interval

    # Set focus on the RESTING worker
    pilot.set_focused_workers({workers[0].name})

    # Backoff should be capped at _focus_interval
    capped_backoff = pilot._compute_backoff()
    assert capped_backoff == pilot._focus_interval


@pytest.mark.asyncio
async def test_focus_no_effect_when_worker_not_tracked(pilot_setup):
    """Focus on unknown worker should not cap backoff."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    pilot._idle_streak = 5
    pilot.set_focused_workers({"nonexistent"})

    backoff = min(
        pilot._base_interval * (2 ** min(pilot._idle_streak, 3)),
        pilot._max_interval,
    )
    # No intersection with workers → focus cap should not apply
    worker_names = {w.name for w in workers}
    assert not (pilot._focused_workers & worker_names)
    assert backoff > pilot._focus_interval


@pytest.mark.asyncio
async def test_focus_no_cap_when_workers_buzzing(pilot_setup):
    """Focus on a BUZZING worker should NOT cap backoff — fast poll is wasted."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Workers default to BUZZING in the fixture
    assert all(w.state == WorkerState.BUZZING for w in workers)

    pilot._idle_streak = 5
    pilot.set_focused_workers({workers[0].name})

    backoff = pilot._compute_backoff()
    # BUZZING + focus should NOT be capped at _focus_interval
    assert backoff > pilot._focus_interval


@pytest.mark.asyncio
async def test_focus_caps_when_worker_resting(pilot_setup):
    """Focus on a RESTING worker should cap backoff at _focus_interval."""
    pilot, workers, log = pilot_setup
    pilot.enabled = True

    # Transition the focused worker to RESTING (needs 3 confirmations)
    workers[0].update_state(WorkerState.RESTING)
    workers[0].update_state(WorkerState.RESTING)
    workers[0].update_state(WorkerState.RESTING)
    assert workers[0].state == WorkerState.RESTING

    pilot._idle_streak = 5
    pilot.set_focused_workers({workers[0].name})

    backoff = pilot._compute_backoff()
    assert backoff == pilot._focus_interval


# ── Auto-approve assignments ─────────────────────────────────────────────


class TestAutoApproveAssignments:
    """Tests for auto-approve when confidence is above threshold."""

    def _make_pilot_with_queen(self, monkeypatch, workers=None, tasks=None, auto_approve=True):
        if workers is None:
            workers = [_make_worker("api", state=WorkerState.RESTING)]
        log = DroneLog()
        board = TaskBoard()
        for t in tasks or []:
            board.add(t)

        queen = AsyncMock()
        queen.can_call = True
        queen.enabled = True
        queen.min_confidence = 0.7

        pilot = DronePilot(
            workers,
            log,
            interval=1.0,
            pool=None,
            drone_config=DroneConfig(auto_approve_assignments=auto_approve),
            task_board=board,
            queen=queen,
        )
        pilot.enabled = True
        monkeypatch.setattr("swarm.queen.context.build_hive_context", lambda *a, **kw: "ctx")
        return pilot, workers, board, queen, log

    @pytest.mark.asyncio
    async def test_high_confidence_auto_approves(self, monkeypatch):
        """Assignments with confidence >= min_confidence should auto-approve."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        pilot, workers, board, queen, log = self._make_pilot_with_queen(monkeypatch, tasks=[task])

        queen.assign_tasks.return_value = [
            {"worker": "api", "task_id": task.id, "message": "Do it", "confidence": 0.9}
        ]

        assigned = []
        pilot.on_task_assigned(lambda w, t, m="": assigned.append((w.name, t.id)))

        proposals = []
        pilot.on_proposal(lambda p: proposals.append(p))

        result = await pilot._auto_assign_tasks()
        assert result is True
        assert len(assigned) == 1
        assert assigned[0] == ("api", task.id)
        assert len(proposals) == 0  # bypassed proposal system

    @pytest.mark.asyncio
    async def test_low_confidence_creates_proposal(self, monkeypatch):
        """Assignments below threshold should create proposals as before."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        pilot, workers, board, queen, log = self._make_pilot_with_queen(monkeypatch, tasks=[task])

        queen.assign_tasks.return_value = [
            {"worker": "api", "task_id": task.id, "message": "Do it", "confidence": 0.5}
        ]

        assigned = []
        pilot.on_task_assigned(lambda w, t, m="": assigned.append((w.name, t.id)))

        proposals = []
        pilot.on_proposal(lambda p: proposals.append(p))

        result = await pilot._auto_assign_tasks()
        assert result is True
        assert len(assigned) == 0  # not auto-approved
        assert len(proposals) == 1  # went through proposal system

    @pytest.mark.asyncio
    async def test_auto_approve_disabled_always_proposes(self, monkeypatch):
        """With auto_approve_assignments=False, all go through proposals."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        pilot, workers, board, queen, log = self._make_pilot_with_queen(
            monkeypatch, tasks=[task], auto_approve=False
        )

        queen.assign_tasks.return_value = [
            {"worker": "api", "task_id": task.id, "message": "Do it", "confidence": 0.95}
        ]

        assigned = []
        pilot.on_task_assigned(lambda w, t, m="": assigned.append((w.name, t.id)))

        proposals = []
        pilot.on_proposal(lambda p: proposals.append(p))

        result = await pilot._auto_assign_tasks()
        assert result is True
        assert len(assigned) == 0
        assert len(proposals) == 1

    @pytest.mark.asyncio
    async def test_auto_approve_resets_idle_counter(self, monkeypatch):
        """Auto-approved assignment should reset the worker's idle counter."""
        from swarm.tasks.task import SwarmTask

        task = SwarmTask(title="Build API")
        pilot, workers, board, queen, log = self._make_pilot_with_queen(monkeypatch, tasks=[task])
        pilot._idle_consecutive["api"] = 5

        queen.assign_tasks.return_value = [
            {"worker": "api", "task_id": task.id, "message": "Do it", "confidence": 0.9}
        ]

        await pilot._auto_assign_tasks()
        assert pilot._idle_consecutive.get("api", 0) == 0


# ── Idle-consecutive tracking ────────────────────────────────────────────


class TestIdleConsecutiveTracking:
    """Tests for per-worker idle consecutive poll tracking."""

    @pytest.mark.asyncio
    async def test_idle_counter_increments(self, pilot_setup):
        """RESTING workers should have their idle counter incremented."""
        pilot, workers, log = pilot_setup
        pilot.enabled = True

        # Make workers RESTING
        idle_content = '> Try "how does foo work"\n? for shortcuts'
        _set_workers_content(workers, content=idle_content, command="claude")

        await pilot.poll_once()
        await pilot.poll_once()
        await pilot.poll_once()

        # Workers should be RESTING after 3 polls (hysteresis)
        resting = [w for w in workers if w.state == WorkerState.RESTING]
        for w in resting:
            assert pilot._idle_consecutive.get(w.name, 0) >= 1

    @pytest.mark.asyncio
    async def test_idle_counter_resets_on_buzzing(self, pilot_setup):
        """Counter should reset when worker goes back to BUZZING."""
        pilot, workers, log = pilot_setup
        pilot.enabled = True
        pilot._idle_consecutive["api"] = 5

        # Workers are BUZZING (default mock returns "esc to interrupt")
        await pilot.poll_once()

        assert pilot._idle_consecutive.get("api", 0) == 0


# ── Idle-consecutive tracking (continued) ────────────────────────────────


class TestIdleConsecutiveTrackingContinued:
    """Additional tests for per-worker idle consecutive poll tracking."""

    @pytest.mark.asyncio
    async def test_needs_assign_check_on_resting_transition(self, monkeypatch):
        """BUZZING→RESTING transition should set _needs_assign_check."""
        workers = [_make_worker("api", state=WorkerState.BUZZING)]
        log = DroneLog()
        pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())
        pilot.enabled = True

        idle_content = '> Try "how does foo work"\n? for shortcuts'
        workers[0].process.set_content(idle_content)
        workers[0].process._child_foreground_command = "claude"

        monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

        # Need 3 polls for hysteresis to confirm RESTING
        await pilot.poll_once()
        await pilot.poll_once()
        await pilot.poll_once()

        # After the transition, the flag should have been set (then cleared by periodic tasks)
        # We check workers are now RESTING to confirm the transition happened
        assert workers[0].state == WorkerState.RESTING


# ── Coordination skip when hive state unchanged ──────────────────────


@pytest.fixture
def coordination_setup(pilot_setup):
    """Mixed-state worker set for testing capture_worker_outputs (retained)."""
    pilot, workers, log = pilot_setup
    workers[1].state = WorkerState.RESTING
    return pilot, workers, None


# ── capture_worker_outputs (retained after coord cycle removal) ──────


def test_capture_worker_outputs_skips_sleeping(coordination_setup):
    """SLEEPING workers are excluded from capture — their PTY is stale."""
    pilot, workers, _ = coordination_setup
    workers[1].state_since = time.time() - 1500  # 25 min ago → SLEEPING
    workers[1].state = WorkerState.SLEEPING
    outputs = pilot._coordination.capture_worker_outputs()
    assert workers[1].name not in outputs
    assert workers[0].name in outputs


def test_capture_worker_outputs_line_counts_vary_by_state(coordination_setup):
    """RESTING gets 15 lines, other active states get 60 — cheaper snapshot."""
    pilot, workers, _ = coordination_setup

    line_counts: dict[str, int] = {}
    original_0 = workers[0].process.get_content
    original_1 = workers[1].process.get_content

    def track_0(lines=35):
        line_counts[workers[0].name] = lines
        return original_0(lines)

    def track_1(lines=35):
        line_counts[workers[1].name] = lines
        return original_1(lines)

    workers[0].process.get_content = track_0
    workers[1].process.get_content = track_1

    pilot._coordination.capture_worker_outputs()

    assert line_counts.get(workers[1].name) == 15  # RESTING
    assert line_counts.get(workers[0].name) == 60  # BUZZING


# ── Sleeping worker poll throttling ───────────────────────────────────


@pytest.mark.asyncio
async def test_sleeping_worker_poll_throttled(monkeypatch):
    """Sleeping workers should skip expensive classify between full polls."""
    workers = [_make_worker("sleepy", state=WorkerState.RESTING)]
    # Make it sleeping (idle > 20 min)
    workers[0].state_since = time.time() - 1500

    log = DroneLog()
    config = DroneConfig(sleeping_poll_interval=30.0)
    pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=config)

    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

    workers[0].process.set_content("> idle")
    workers[0].process._child_foreground_command = "claude"

    # Track get_styled_content calls (pilot now uses styled path)
    call_count = [0]
    original_get_styled_content = workers[0].process.get_styled_content

    def counting_get_styled_content(lines=35):
        call_count[0] += 1
        return original_get_styled_content(lines)

    workers[0].process.get_styled_content = counting_get_styled_content

    # Initialize deferred actions list (normally done by _poll_once_locked)
    pilot._deferred_actions = []

    # First poll — should do a full poll (no previous timestamp)
    dead: list = []
    pilot._poll_single_worker(workers[0], dead)
    assert call_count[0] >= 1
    first_count = call_count[0]

    # Immediately poll again — throttled path does lightweight re-check
    pilot._poll_single_worker(workers[0], dead)
    assert call_count[0] > first_count  # lightweight check still reads content


@pytest.mark.asyncio
async def test_sleeping_worker_not_throttled_when_focused(monkeypatch):
    """Sleeping workers that are focused should not be throttled."""
    workers = [_make_worker("sleepy", state=WorkerState.RESTING)]
    workers[0].state_since = time.time() - 1500

    log = DroneLog()
    config = DroneConfig(sleeping_poll_interval=30.0)
    pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=config)
    pilot.set_focused_workers({"sleepy"})

    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

    workers[0].process.set_content("> idle")
    workers[0].process._child_foreground_command = "claude"

    # First poll — full
    dead: list = []
    pilot._poll_single_worker(workers[0], dead)

    # Second poll immediately — should still be full (focused overrides throttle)
    # Verify by checking _last_full_poll gets updated
    pilot._last_full_poll[workers[0].name] = time.time() - 1  # just set it
    pilot._poll_single_worker(workers[0], dead)
    # Should not be throttled (focused), so _last_full_poll should be recent
    assert time.time() - pilot._last_full_poll.get("sleepy", 0) < 2


@pytest.mark.asyncio
async def test_sleeping_throttle_rechecks_state(monkeypatch):
    """Sleeping throttle should do a lightweight re-check and break out if state changes."""
    workers = [_make_worker("sleepy", state=WorkerState.RESTING)]
    workers[0].state_since = time.time() - 1500  # sleeping

    log = DroneLog()
    config = DroneConfig(sleeping_poll_interval=30.0)
    pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=config)
    pilot.enabled = True

    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

    workers[0].process.set_content("> idle")
    workers[0].process._child_foreground_command = "claude"

    # Initialize deferred actions list (normally done by _poll_once_locked)
    pilot._deferred_actions = []

    # First poll — full
    dead: list = []
    pilot._poll_single_worker(workers[0], dead)

    # Now change content to something that classifies as WAITING
    workers[0].process.set_content(">> accept edits on (shift+tab to cycle)")
    # Second poll — lightweight check sees accept-edits -> falls through to full poll
    result = pilot._poll_single_worker(workers[0], dead)
    # The throttle should have fallen through (returned None from _poll_sleeping_throttled)
    # so full classify + decide happened
    assert result is not None  # got a real result, not early-return


# --- Worker suspension tests ---


@pytest.mark.asyncio
async def test_sleeping_worker_suspended_after_unchanged_polls(pilot_setup, monkeypatch):
    """A SLEEPING worker should be suspended after 3 unchanged polls."""
    pilot, workers, log = pilot_setup
    w = workers[0]
    w.state = WorkerState.RESTING
    w.state_since = time.time() - 1500  # idle 25 min → SLEEPING display_state
    # Seed last_full_poll so throttling kicks in immediately
    pilot._last_full_poll[w.name] = time.time()

    w.process.set_content("idle prompt")
    w.process._child_foreground_command = "claude"

    # Seed the provider cache with a mock that always returns RESTING
    from unittest.mock import MagicMock

    mock_provider = MagicMock()
    mock_provider.classify_output = MagicMock(return_value=WorkerState.RESTING)
    pilot._provider_cache[w.provider_name] = mock_provider
    pilot.enabled = False  # disable decision engine

    # Polls 1-3 build unchanged streak; poll 4 triggers suspension
    for _ in range(5):
        await pilot.poll_once()

    assert w.name in pilot._suspended
    assert w.name in pilot._suspended_at


@pytest.mark.asyncio
async def test_suspended_worker_skipped_in_poll(pilot_setup, monkeypatch):
    """A suspended worker should be skipped entirely in _poll_once_locked."""
    pilot, workers, _log = pilot_setup
    w = workers[0]

    # Manually suspend the worker
    pilot._suspended.add(w.name)
    pilot._suspended_at[w.name] = time.time()

    # Track get_content calls for the suspended worker
    call_count = [0]
    original_get_content = w.process.get_content

    def counting_get_content(lines=35):
        call_count[0] += 1
        return original_get_content(lines)

    w.process.get_content = counting_get_content

    pilot.enabled = False

    await pilot.poll_once()

    # Suspended worker's get_content should not have been called
    assert call_count[0] == 0, "suspended worker should not be polled"


@pytest.mark.asyncio
async def test_safety_net_polls_suspended_worker(pilot_setup, monkeypatch):
    """After safety-net interval, a suspended worker should be polled again."""
    pilot, workers, _log = pilot_setup
    w = workers[0]

    # Suspend with a timestamp far in the past
    pilot._suspended.add(w.name)
    pilot._suspended_at[w.name] = time.time() - 120  # 120s ago, past 60s safety-net
    pilot._suspend_safety_interval = 60.0

    # Track get_styled_content calls for the suspended worker (pilot uses styled path)
    call_count = [0]
    original_get_styled_content = w.process.get_styled_content

    def counting_get_styled_content(lines=35):
        call_count[0] += 1
        return original_get_styled_content(lines)

    w.process.get_styled_content = counting_get_styled_content

    pilot.enabled = False

    await pilot.poll_once()

    # The suspended worker should have been polled (safety-net elapsed)
    assert call_count[0] > 0


@pytest.mark.asyncio
async def test_focus_wakes_suspended_worker(pilot_setup):
    """Focusing a suspended worker should wake it."""
    pilot, workers, _log = pilot_setup
    w = workers[0]

    pilot._suspended.add(w.name)
    pilot._suspended_at[w.name] = time.time()

    pilot.set_focused_workers({w.name})

    assert w.name not in pilot._suspended
    assert w.name not in pilot._suspended_at


@pytest.mark.asyncio
async def test_state_change_wakes_suspended_worker(pilot_setup, monkeypatch):
    """A real state transition should wake a suspended worker."""
    pilot, workers, _log = pilot_setup
    w = workers[0]
    w.state = WorkerState.RESTING

    pilot._suspended.add(w.name)
    pilot._suspended_at[w.name] = time.time()

    # Simulate a state change
    pilot._handle_state_change(w, WorkerState.BUZZING)

    assert w.name not in pilot._suspended


@pytest.mark.asyncio
async def test_state_transition_emits_diagnostic_buzz_entry(pilot_setup):
    """Task #233: every state transition must leave a STATE_TRANSITION
    entry in the drone log with enough context to diagnose
    mis-classifications without replaying the PTY.

    Regression guard for the "admin RESTING while demonstrably mid-Bash"
    bug. With the entry in place, a bad transition shows up in the
    buzz log the moment it happens, metadata and all.
    """
    from swarm.drones.log import SystemAction

    pilot, workers, log = pilot_setup
    w = workers[0]
    w.state = WorkerState.BUZZING

    pilot._handle_state_change(w, WorkerState.RESTING)

    transitions = [e for e in log.entries if e.action == SystemAction.STATE_TRANSITION]
    assert len(transitions) == 1
    entry = transitions[0]
    assert entry.worker_name == w.name
    assert "RESTING" in entry.detail
    assert "BUZZING" in entry.detail
    # Metadata carries the signals a future debugger needs to figure out
    # whether the classifier was looking at the right PTY state.
    md = entry.metadata
    assert md["from"] == "RESTING"
    assert md["to"] == "BUZZING"
    assert "esc_to_interrupt" in md
    assert "unchanged_streak" in md
    assert "suspended" in md
    assert "pty_delta_bytes" in md


class TestStuckBuzzingSafetyNet:
    """Task #236: force RESTING when BUZZING lingers without active-turn signals."""

    def test_has_active_turn_signal_detects_esc_to_interrupt(self, pilot_setup) -> None:
        pilot, _, _ = pilot_setup
        tail = "some line\n  ⏵⏵ auto mode on · esc to interrupt · ctrl+t\n"
        assert pilot._state_tracker._has_active_turn_signal(tail) is True

    def test_has_active_turn_signal_detects_subagent_spinner(self, pilot_setup) -> None:
        pilot, _, _ = pilot_setup
        # ``↓ N tokens`` is the canonical subagent-active signal the
        # classifier already uses; the safety-net helper shares the
        # same regex.
        tail = "some output\n· Running verification (20m · ↓ 31.5k tokens)\n"
        assert pilot._state_tracker._has_active_turn_signal(tail) is True

    def test_has_active_turn_signal_false_for_idle_prompt(self, pilot_setup) -> None:
        pilot, _, _ = pilot_setup
        # Typical Claude Code idle tail — prompt + hint line, no "esc to
        # interrupt", no monitor, no subagent spinner.
        tail = (
            "  ⎜ ✔ Final task done\n"
            "────────────────────────────────\n"
            "❯\n"
            "────────────────────────────────\n"
            "  ⏵⏵ auto mode on (shift+tab to cycle)\n"
        )
        assert pilot._state_tracker._has_active_turn_signal(tail) is False

    def test_has_active_turn_signal_ignores_stale_subagent_in_scrollback(self, pilot_setup) -> None:
        """The stuck-BUZZING root-cause signature: a subagent indicator
        in the WIDE tail (scrollback) but not the narrow tail. The
        safety net's narrow-tail check must reject it so the worker
        doesn't get re-classified BUZZING forever."""
        pilot, _, _ = pilot_setup
        # 5 "narrow" lines at the bottom are a plain idle prompt; the
        # subagent pattern sits 10 lines above — it would match the
        # wide-tail check the classifier uses but NOT the narrow one.
        stale_pattern = "· Running verification… (20m 35s · ↓ 31.5k tokens · thought for 2s)"
        content = (
            stale_pattern
            + "\n"
            + "\n".join([f"line-{i}" for i in range(10)])
            + "\n❯\n"
            + "  ⏵⏵ auto mode on\n"
        )
        assert pilot._state_tracker._has_active_turn_signal(content) is False

    def test_threshold_constant_is_reasonable(self, pilot_setup) -> None:
        """Guard rail: don't let future tuning drop this below 5
        minutes by accident. Legitimate long-running turns (Playwright
        install, heavy build) regularly exceed 5 minutes."""
        pilot, _, _ = pilot_setup
        assert pilot._state_tracker._STUCK_BUZZING_THRESHOLD >= 300.0


@pytest.mark.asyncio
async def test_dead_worker_cleanup_removes_suspension(pilot_setup, monkeypatch):
    """Cleaning up dead workers should remove suspension state."""
    pilot, workers, _log = pilot_setup
    w = workers[0]

    pilot._suspended.add(w.name)
    pilot._suspended_at[w.name] = time.time()

    pilot._cleanup_dead_workers([w])

    assert w.name not in pilot._suspended
    assert w.name not in pilot._suspended_at


def test_wake_worker_returns_false_if_not_suspended(pilot_setup):
    """wake_worker should return False for a non-suspended worker."""
    pilot, workers, _log = pilot_setup
    assert pilot.wake_worker(workers[0].name) is False


def test_wake_worker_returns_true_and_clears_state(pilot_setup):
    """wake_worker should return True and clear fingerprint/streak data."""
    pilot, workers, _log = pilot_setup
    w = workers[0]

    pilot._suspended.add(w.name)
    pilot._suspended_at[w.name] = time.time()
    pilot._content_fingerprints[w.name] = 12345
    pilot._unchanged_streak[w.name] = 5

    assert pilot.wake_worker(w.name) is True
    assert w.name not in pilot._suspended
    assert w.name not in pilot._content_fingerprints
    assert w.name not in pilot._unchanged_streak


def test_diagnostics_includes_suspension_info(pilot_setup):
    """get_diagnostics should report suspended worker count and names."""
    pilot, workers, _log = pilot_setup
    pilot._suspended.add("api")
    pilot._suspended.add("web")

    diag = pilot.get_diagnostics()
    assert diag["suspended_count"] == 2
    assert sorted(diag["suspended_workers"]) == ["api", "web"]


# ── Public setter regression tests ───────────────────────────────────────


def test_set_emit_decisions(pilot_setup):
    """set_emit_decisions toggles the _emit_decisions flag."""
    pilot, _, _ = pilot_setup
    assert pilot._emit_decisions is False  # default

    pilot.set_emit_decisions(True)
    assert pilot._emit_decisions is True

    pilot.set_emit_decisions(False)
    assert pilot._emit_decisions is False


def test_set_auto_complete_idle(pilot_setup):
    """set_auto_complete_idle updates the minimum idle threshold."""
    pilot, _, _ = pilot_setup
    original = pilot._auto_complete_min_idle

    pilot.set_auto_complete_idle(10.0)
    assert pilot._auto_complete_min_idle == 10.0

    pilot.set_auto_complete_idle(original)
    assert pilot._auto_complete_min_idle == original


def test_mark_completion_seen(pilot_setup):
    """mark_completion_seen sets the _saw_completion flag."""
    pilot, _, _ = pilot_setup
    assert pilot._saw_completion is False  # default

    pilot.mark_completion_seen()
    assert pilot._saw_completion is True


# ── Terminal-active guard ─────────────────────────────────────────────


class TestTerminalActiveGuard:
    """Automated input is skipped when user has an active web terminal."""

    @pytest.mark.asyncio
    async def test_deferred_continue_skipped_when_user_active(self, monkeypatch):
        """Deferred continue should be skipped when user is typing."""
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        log = DroneLog()
        pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())
        monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

        # Simulate user active in terminal
        workers[0].process.set_terminal_active(True)
        workers[0].process.mark_user_input()

        # Queue a deferred continue
        from swarm.drones.rules import Decision, DroneDecision

        decision = DroneDecision(decision=Decision.CONTINUE, reason="test", source="test")
        pilot._deferred_actions = [
            ("continue", workers[0], decision, workers[0].state, workers[0].process)
        ]

        await pilot._execute_deferred_actions()
        # The continue should have been skipped
        assert len(workers[0].process.keys_sent) == 0

    @pytest.mark.asyncio
    async def test_queen_continue_skipped_when_user_active(self, monkeypatch):
        """Queen continue directive skipped when user is active."""
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        log = DroneLog()
        pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())
        monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

        workers[0].process.set_terminal_active(True)
        workers[0].process.mark_user_input()

        result = await pilot._directives._handle_continue({"reason": "test"}, workers[0])
        assert result is False
        assert len(workers[0].process.keys_sent) == 0

    @pytest.mark.asyncio
    async def test_queen_continue_blocked_on_waiting_worker(self, monkeypatch):
        """Queen continue blocked when worker is WAITING — bare Enter would submit a choice."""
        workers = [_make_worker("api", state=WorkerState.WAITING)]
        log = DroneLog()
        pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())
        monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

        workers[0].process.set_content("> ")

        result = await pilot._directives._handle_continue({"reason": "test"}, workers[0])
        assert result is False
        assert len(workers[0].process.keys_sent) == 0

    @pytest.mark.asyncio
    async def test_queen_continue_blocked_on_resting_worker(self, monkeypatch):
        """Queen continue blocked when worker is RESTING — bare Enter never safe at a prompt."""
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        log = DroneLog()
        pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())
        monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

        workers[0].process.set_content("> check the buzz log")

        result = await pilot._directives._handle_continue({"reason": "test"}, workers[0])
        assert result is False
        assert len(workers[0].process.keys_sent) == 0

    @pytest.mark.asyncio
    async def test_queen_continue_blocked_on_sleeping_worker(self, monkeypatch):
        """Queen continue blocked when worker is SLEEPING."""
        workers = [_make_worker("api", state=WorkerState.SLEEPING)]
        log = DroneLog()
        pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())
        monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

        workers[0].process.set_content("> ")

        result = await pilot._directives._handle_continue({"reason": "test"}, workers[0])
        assert result is False
        assert len(workers[0].process.keys_sent) == 0

    @pytest.mark.asyncio
    async def test_deferred_continue_blocked_on_resting_worker(self, monkeypatch):
        """Deferred continue blocked when worker is RESTING."""
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        log = DroneLog()
        pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())
        monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

        workers[0].process.set_content('> try "fix lint errors"\n\n? for shortcuts')

        from swarm.drones.rules import Decision, DroneDecision

        decision = DroneDecision(decision=Decision.CONTINUE, reason="test", source="test")
        pilot._deferred_actions = [
            ("continue", workers[0], decision, workers[0].state, workers[0].process)
        ]

        await pilot._execute_deferred_actions()
        assert len(workers[0].process.keys_sent) == 0

    @pytest.mark.asyncio
    async def test_deferred_continue_blocked_on_sleeping_worker(self, monkeypatch):
        """Deferred continue blocked when worker is SLEEPING — state guard catches it."""
        workers = [_make_worker("api", state=WorkerState.SLEEPING)]
        log = DroneLog()
        pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())
        monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

        workers[0].process.set_content("> ")

        from swarm.drones.rules import Decision, DroneDecision

        decision = DroneDecision(decision=Decision.CONTINUE, reason="test", source="test")
        pilot._deferred_actions = [
            ("continue", workers[0], decision, workers[0].state, workers[0].process)
        ]

        await pilot._execute_deferred_actions()
        assert len(workers[0].process.keys_sent) == 0
        blocked = [e for e in log.entries if e.action == SystemAction.QUEEN_BLOCKED]
        assert len(blocked) == 1

    @pytest.mark.asyncio
    async def test_deferred_continue_blocked_on_ctrl_t_hint(self, monkeypatch):
        """Deferred continue blocked when worker shows ctrl+t to hide hint."""
        workers = [_make_worker("api", state=WorkerState.RESTING)]
        log = DroneLog()
        pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())
        monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

        workers[0].process.set_content('> try "how do I log?"\nctrl+t to hide')

        from swarm.drones.rules import Decision, DroneDecision

        decision = DroneDecision(decision=Decision.CONTINUE, reason="test", source="test")
        pilot._deferred_actions = [
            ("continue", workers[0], decision, workers[0].state, workers[0].process)
        ]

        await pilot._execute_deferred_actions()
        assert len(workers[0].process.keys_sent) == 0


# ── Escalation tracking clearance ────────────────────────────────────


class TestEscalationTrackingClearance:
    """_escalated set is cleared when worker leaves WAITING state."""

    def test_escalation_cleared_on_waiting_to_resting(self):
        workers = [_make_worker("api", state=WorkerState.WAITING)]
        log = DroneLog()
        pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())
        pilot._escalated["api"] = 0.0

        # Simulate WAITING → RESTING transition
        workers[0].state = WorkerState.RESTING
        pilot._handle_state_change(workers[0], WorkerState.WAITING)
        assert "api" not in pilot._escalated

    def test_escalation_cleared_on_waiting_to_buzzing(self):
        workers = [_make_worker("api", state=WorkerState.BUZZING)]
        log = DroneLog()
        pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())
        pilot._escalated["api"] = 0.0

        # Simulate WAITING → BUZZING transition
        pilot._handle_state_change(workers[0], WorkerState.WAITING)
        assert "api" not in pilot._escalated


# ── Hive auto-stop with SLEEPING workers ────────────────────────────


def test_hive_complete_condition_accepts_sleeping_workers():
    """Hive completion check should accept SLEEPING workers as idle."""
    workers = [_make_worker("api", state=WorkerState.RESTING)]
    log = DroneLog()
    board = TaskBoard()
    cfg = DroneConfig(auto_stop_on_complete=True)
    pilot = DronePilot(
        workers,
        log,
        interval=1.0,
        pool=None,
        drone_config=cfg,
        task_board=board,
    )
    pilot.enabled = True
    pilot._saw_completion = True

    # Make the worker SLEEPING (RESTING for > 20 min)
    workers[0].state_since = time.time() - 1500
    assert workers[0].display_state == WorkerState.SLEEPING

    # The all-idle check used in the run loop should pass for SLEEPING workers
    all_idle = all(w.display_state in (WorkerState.RESTING, WorkerState.SLEEPING) for w in workers)
    assert all_idle is True

    # Verify WAITING workers do NOT count as idle
    workers[0].state = WorkerState.WAITING
    all_idle = all(w.display_state in (WorkerState.RESTING, WorkerState.SLEEPING) for w in workers)
    assert all_idle is False


# ── A3: Deferred actions stale-state guard ───────────────────────────────


@pytest.mark.asyncio
async def test_deferred_continue_skipped_on_state_change(monkeypatch):
    """Deferred continue should be skipped if worker state changed since decision."""
    workers = [_make_worker("api", state=WorkerState.WAITING)]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())

    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

    proc = workers[0].process
    # Queue a deferred continue with WAITING as decision-time state
    from swarm.drones.rules import Decision, DroneDecision

    decision = DroneDecision(
        decision=Decision.CONTINUE,
        reason="test",
        source="test",
    )
    pilot._deferred_actions.append(("continue", workers[0], decision, WorkerState.WAITING, proc))

    # Change state to BUZZING before execution
    workers[0].state = WorkerState.BUZZING

    await pilot._execute_deferred_actions()

    # send_enter should NOT have been called
    assert "\n" not in proc.keys_sent


@pytest.mark.asyncio
async def test_deferred_continue_uses_decision_time_process(monkeypatch):
    """Deferred continue should use the process from decision time."""
    workers = [_make_worker("api", state=WorkerState.WAITING)]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())

    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())

    from swarm.drones.rules import Decision, DroneDecision
    from tests.fakes.process import FakeWorkerProcess

    original_proc = workers[0].process
    decision = DroneDecision(
        decision=Decision.CONTINUE,
        reason="test",
        source="test",
    )
    pilot._deferred_actions.append(
        ("continue", workers[0], decision, WorkerState.WAITING, original_proc)
    )

    # Replace process on the worker (simulating holder reconnect)
    new_proc = FakeWorkerProcess(name="api")
    workers[0].process = new_proc

    await pilot._execute_deferred_actions()

    # Original process should have received the approval response
    assert "\r" in original_proc.keys_sent
    # New process should NOT
    assert "\r" not in new_proc.keys_sent


@pytest.mark.asyncio
async def test_deferred_revive_skipped_on_state_change(monkeypatch):
    """Deferred revive should be skipped if worker state changed since decision."""
    workers = [_make_worker("api", state=WorkerState.STUNG)]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())

    mock_revive = AsyncMock()
    monkeypatch.setattr("swarm.drones.pilot.revive_worker", mock_revive)

    from swarm.drones.rules import Decision, DroneDecision

    decision = DroneDecision(
        decision=Decision.REVIVE,
        reason="test",
        source="test",
    )
    pilot._deferred_actions.append(
        ("revive", workers[0], decision, WorkerState.STUNG, workers[0].process)
    )

    # Change state before execution
    workers[0].state = WorkerState.BUZZING

    await pilot._execute_deferred_actions()

    # revive_worker should NOT have been called
    mock_revive.assert_not_called()


class TestEventThreading:
    """Verify events flow from classify_with_events() through to decide()."""

    def test_classify_worker_state_returns_events(self, pilot_setup):
        """_classify_worker_state should return (state, events) tuple."""
        pilot, workers, _log = pilot_setup
        w = workers[0]
        _set_workers_content([w], content="esc to interrupt", command="claude")
        state, events = pilot._classify_worker_state(w, "claude", "esc to interrupt")
        assert state == WorkerState.BUZZING
        assert events is not None
        assert isinstance(events, list)

    def test_classify_worker_state_returns_none_on_error(self, pilot_setup, monkeypatch):
        """_classify_worker_state should return None events on exception."""
        pilot, workers, _log = pilot_setup
        w = workers[0]

        def raise_error(*_args, **_kwargs):
            raise RuntimeError("test error")

        monkeypatch.setattr(
            "swarm.providers.claude.ClaudeProvider.classify_with_events", raise_error
        )
        state, events = pilot._classify_worker_state(w, "claude", "content")
        assert events is None

    def test_run_decision_sync_passes_events(self, pilot_setup):
        """_run_decision_sync should pass events through to decide()."""
        from unittest.mock import patch

        from swarm.providers.events import EventType, TerminalEvent

        pilot, workers, _log = pilot_setup
        w = workers[0]
        w.state = WorkerState.WAITING
        pilot.enabled = True

        events = [TerminalEvent(EventType.TOOL_CALL, tool_name="Read")]
        content = """\
Read file
  Read(/home/user/file.py)
> 1. Yes
  2. No
Esc to cancel"""

        with patch("swarm.drones.decision_executor.decide", wraps=decide) as mock_decide:
            pilot._run_decision_sync(w, content, events=events)
            mock_decide.assert_called_once()
            call_kwargs = mock_decide.call_args
            assert call_kwargs.kwargs.get("events") is events

    def test_poll_threads_events_to_decision(self, pilot_setup):
        """Full poll should thread events from classify through to decision."""
        from unittest.mock import patch

        pilot, workers, _log = pilot_setup
        w = workers[0]
        pilot.enabled = True

        # Set up a choice prompt that triggers a decision
        choice_content = """\
Read file
  Read(/home/user/file.py)
> 1. Yes
  2. No
Esc to cancel"""
        _set_workers_content([w], content=choice_content, command="claude")

        with patch("swarm.drones.decision_executor.decide", wraps=decide) as mock_decide:
            pilot._poll_single_worker(w, [])
            if mock_decide.called:
                call_kwargs = mock_decide.call_args
                events_arg = call_kwargs.kwargs.get("events")
                # Events should be a list (from classify_with_events)
                assert events_arg is None or isinstance(events_arg, list)


# ---------------------------------------------------------------------------
# Pressure suspension — soft suspension (no SIGTSTP/SIGCONT)
# ---------------------------------------------------------------------------


def test_suspend_workers_does_not_send_sigtstp(pilot_setup, monkeypatch):
    """_suspend_workers should NOT send SIGTSTP — soft suspension only."""
    pilot, workers, _log = pilot_setup
    signals_sent: list[tuple[str, int]] = []
    monkeypatch.setattr(
        pilot._pressure_mgr,
        "_signal_worker_async",
        lambda name, sig: signals_sent.append((name, sig)),
    )
    pilot._suspend_workers([w.name for w in workers], "HIGH")
    # No signals should have been sent
    assert signals_sent == []
    # But workers should be tracked as suspended
    for w in workers:
        assert w.name in pilot._suspended_for_pressure
        assert w.name in pilot._suspended


def test_resume_pressure_suspended_does_not_send_sigcont(pilot_setup, monkeypatch):
    """_resume_pressure_suspended should NOT send SIGCONT — soft resume only."""
    pilot, workers, _log = pilot_setup
    signals_sent: list[tuple[str, int]] = []
    monkeypatch.setattr(
        pilot._pressure_mgr,
        "_signal_worker_async",
        lambda name, sig: signals_sent.append((name, sig)),
    )
    # First suspend them
    pilot._suspend_workers([w.name for w in workers], "HIGH")
    signals_sent.clear()
    # Now resume
    pilot._resume_pressure_suspended()
    # No SIGCONT should be sent
    assert signals_sent == []
    # Sets should be cleaned up
    assert len(pilot._suspended_for_pressure) == 0
    for w in workers:
        assert w.name not in pilot._suspended


def test_critical_pressure_skips_buzzing_workers(pilot_setup, monkeypatch):
    """CRITICAL pressure should never suspend BUZZING or WAITING workers."""
    pilot, workers, _log = pilot_setup
    monkeypatch.setattr(pilot._pressure_mgr, "_signal_worker_async", lambda name, sig: None)
    # Set workers to BUZZING (the default from pilot_setup)
    for w in workers:
        w.state = WorkerState.BUZZING
    pilot._suspend_on_critical_pressure()
    # No workers should be suspended since they're all BUZZING
    assert len(pilot._suspended_for_pressure) == 0


def test_critical_pressure_suspends_sleeping_and_resting(pilot_setup, monkeypatch):
    """CRITICAL pressure should suspend SLEEPING and RESTING workers."""
    pilot, workers, _log = pilot_setup
    monkeypatch.setattr(pilot._pressure_mgr, "_signal_worker_async", lambda name, sig: None)
    # Make one SLEEPING, one RESTING
    workers[0].state = WorkerState.RESTING
    workers[0].state_since = time.time() - 1500  # old enough for SLEEPING display
    workers[1].state = WorkerState.RESTING
    workers[1].state_since = time.time() - 10  # recent, stays RESTING
    # The most recent worker is exempt, but both are eligible by state
    pilot._suspend_on_critical_pressure()
    # At least one should be suspended (the non-most-recent one)
    assert len(pilot._suspended_for_pressure) >= 1


def test_critical_pressure_skips_waiting_workers(pilot_setup, monkeypatch):
    """CRITICAL pressure should not suspend WAITING workers."""
    pilot, workers, _log = pilot_setup
    monkeypatch.setattr(pilot._pressure_mgr, "_signal_worker_async", lambda name, sig: None)
    for w in workers:
        w.state = WorkerState.WAITING
    pilot._suspend_on_critical_pressure()
    assert len(pilot._suspended_for_pressure) == 0


# --- Oversight result handling ---


@pytest.mark.asyncio
async def test_oversight_note_does_not_send_keys(pilot_setup):
    """Note actions should be log-only — never inject into worker PTY."""
    from swarm.queen.oversight import OversightResult, OversightSignal, Severity, SignalType

    pilot, workers, _log = pilot_setup
    signal = OversightSignal(
        signal_type=SignalType.PROLONGED_BUZZING,
        worker_name="api",
        description="buzzing for 10 min",
    )
    result = OversightResult(
        signal=signal,
        severity=Severity.MINOR,
        action="note",
        message="Worker is fine, just waiting on CI",
        reasoning="gh run watch is expected to take a while",
    )
    acted = await pilot._oversight_handler._handle_oversight_result(result)
    assert acted is True
    # Must NOT have called send_keys
    assert workers[0].process.keys_sent == []


@pytest.mark.asyncio
async def test_oversight_redirect_sends_keys(pilot_setup):
    """Redirect actions should still inject into worker PTY."""
    from swarm.queen.oversight import OversightResult, OversightSignal, Severity, SignalType

    pilot, workers, _log = pilot_setup
    signal = OversightSignal(
        signal_type=SignalType.PROLONGED_BUZZING,
        worker_name="api",
        description="stuck in a loop",
    )
    result = OversightResult(
        signal=signal,
        severity=Severity.MAJOR,
        action="redirect",
        message="Stop looping and move on to the next task",
        reasoning="Worker is repeating the same action",
    )
    # Set worker to RESTING so the escape→wait loop exits immediately
    workers[0].state = WorkerState.RESTING
    acted = await pilot._oversight_handler._handle_oversight_result(result)
    assert acted is True
    # Redirect should send escape first, then the message via send_keys
    assert "<Esc>" in workers[0].process.keys_sent
    assert any("Stop looping" in k for k in workers[0].process.keys_sent)


@pytest.mark.asyncio
async def test_oversight_redirect_skipped_when_operator_engaged(pilot_setup):
    """Task #340: a recent operator keystroke gates the redirect — no PTY injection.

    Models the budgetbug incident where a periodic drift signal interrupted
    the operator mid-deploy. The hard precondition gate must skip the
    redirect and log OVERSIGHT_INTERVENTION_SKIPPED.
    """
    from swarm.config import OversightConfig
    from swarm.queen.oversight import (
        OversightMonitor,
        OversightResult,
        OversightSignal,
        Severity,
        SignalType,
    )

    pilot, workers, log = pilot_setup
    pilot._oversight_handler.set_oversight(
        OversightMonitor(OversightConfig(operator_engagement_minutes=10.0))
    )
    workers[0].state = WorkerState.RESTING
    workers[0].process.mark_user_input()  # operator just typed

    signal = OversightSignal(
        signal_type=SignalType.TASK_DRIFT,
        worker_name="api",
        description="periodic drift check",
    )
    result = OversightResult(
        signal=signal,
        severity=Severity.MAJOR,
        action="redirect",
        message="You're off-topic",
        reasoning="surface-keyword divergence",
        cited_contradiction="Do not ship maintenance code",
    )
    acted = await pilot._oversight_handler._handle_oversight_result(result)
    assert acted is False
    # Must NOT have sent escape or any keys
    assert workers[0].process.keys_sent == []
    # Must log a SKIPPED entry
    assert any(e.action == SystemAction.OVERSIGHT_INTERVENTION_SKIPPED for e in log.entries)


@pytest.mark.asyncio
async def test_oversight_redirect_proceeds_when_operator_idle(pilot_setup):
    """Task #340: gate is per-window — old keystrokes (or none) don't gate."""
    from swarm.config import OversightConfig
    from swarm.queen.oversight import (
        OversightMonitor,
        OversightResult,
        OversightSignal,
        Severity,
        SignalType,
    )

    pilot, workers, _log = pilot_setup
    pilot._oversight_handler.set_oversight(
        OversightMonitor(OversightConfig(operator_engagement_minutes=10.0))
    )
    workers[0].state = WorkerState.RESTING
    # No mark_user_input call — operator hasn't engaged
    signal = OversightSignal(
        signal_type=SignalType.TASK_DRIFT,
        worker_name="api",
        description="periodic drift check",
    )
    result = OversightResult(
        signal=signal,
        severity=Severity.MAJOR,
        action="redirect",
        message="Refocus",
        reasoning="clear contradiction",
        cited_contradiction="Do not touch the auth module",
    )
    acted = await pilot._oversight_handler._handle_oversight_result(result)
    assert acted is True
    assert "<Esc>" in workers[0].process.keys_sent
