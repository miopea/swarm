"""#1610 — the idle nudge fires on display-state idleness, not on absence of progress.

MEASURED ON MYSELF, 2026-08-14. swarm received four AUTO_NUDGEs in 41 minutes, each
"idle with active task(s): #1608", while pushing de3870a, a184cfa and be122e2 between
them. The task was ACTIVE and correctly asserted throughout, and the 900s debounce was
respected — so this was not a debounce failure. The watcher was working exactly as
written; what it lacked was any notion of progress.

Replayed against those four real nudges, gaps to last activity were 0s, 134s, 2s and 30s:

    window  120s -> would have suppressed 3/4
    window  300s -> would have suppressed 4/4
    window  600s -> would have suppressed 4/4

WHY 600 AND NOT 900. The ceiling is the real constraint: the window must stay BELOW
`idle_nudge_debounce_seconds` (900). At or above it, any worker that acts once per
debounce window is never nudged and the suppression becomes indistinguishable from
switching the watcher off — a guard that cannot fire is the failure this codebase has
hit six times this week. Time-weighted over 12h across four workers, 600s leaves
12-44% of an active worker's elapsed time still nudge-eligible: real suppression, not a
blanket.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.drones.idle_watcher import IdleWatcher


def _watcher(*, window: float, last_activity: float | None = None) -> IdleWatcher:
    cfg = MagicMock()
    cfg.idle_nudge_interval_seconds = 180
    cfg.idle_nudge_debounce_seconds = 900
    cfg.idle_nudge_max_repeats = 3
    cfg.idle_nudge_activity_window_seconds = window
    cfg.assign_operator_engagement_minutes = 0
    return IdleWatcher(
        drone_config=cfg,
        task_board=MagicMock(),
        drone_log=MagicMock(),
        send_to_worker=AsyncMock(),
        mcp_activity_lookup=lambda _name: last_activity,
    )


def _worker(name: str = "swarm", *, resting_for: float = 30.0) -> MagicMock:
    w = MagicMock()
    w.name = name
    w.state_duration = resting_for
    return w


def test_a_worker_active_seconds_ago_is_not_nudged():
    """The reported case: gaps of 0s, 2s and 30s to the last activity."""
    watcher = _watcher(window=600)

    reason = watcher._suppression_reason(_worker(resting_for=30))

    assert reason is not None
    assert "within 600s window" in reason


def test_the_134_second_gap_is_suppressed_at_600_but_not_at_120():
    """The one real nudge that distinguishes the candidate windows — which is why the
    choice between them is a measurement rather than a preference."""
    w = _worker(resting_for=134)

    assert _watcher(window=600)._suppression_reason(w) is not None
    assert _watcher(window=120)._suppression_reason(w) is None


def test_a_worker_with_NO_recent_activity_is_still_nudged():
    """POSITIVE CONTROL, and it is the one that decides whether this is safe.

    #225 exists because workers really do drop tasks. A suppression that silenced the
    watcher entirely would recreate that defect and would look identical to the watcher
    having nothing to do — which is exactly the shape this codebase has hit repeatedly.
    """
    watcher = _watcher(window=600)

    assert watcher._suppression_reason(_worker(resting_for=3600)) is None


def test_a_worker_that_has_never_called_mcp_is_still_nudged():
    """None means "no record", NOT "idle forever" — the fail-open direction this fleet
    keeps getting backwards. Here both readings agree: a worker with no MCP activity at
    all is precisely who the watcher exists for, so it is nudged either way. Pinned so a
    later "fail open on None" change does not silently disable the watcher."""
    watcher = _watcher(window=600)

    assert watcher._suppression_reason(_worker(resting_for=1500)) is None


def test_a_zero_window_disables_the_suppression_entirely():
    """The off switch, and it must fail toward NUDGING. A zero that suppressed
    everything would be an operator turning the feature off and getting silence."""
    watcher = _watcher(window=0)

    assert watcher._suppression_reason(_worker(resting_for=1)) is None


def test_an_unreadable_state_duration_does_not_suppress():
    """A state_duration that cannot be read is not evidence the worker is busy, so it
    falls back to NUDGING — the safe direction."""
    w = _worker()
    w.state_duration = "not a number"

    assert _watcher(window=600)._suppression_reason(w) is None


@pytest.mark.parametrize("window", [600.0, 899.0])
def test_the_window_stays_below_the_debounce(window: float):
    """THE CEILING, pinned as a property rather than left in a comment.

    At or above `idle_nudge_debounce_seconds`, a worker acting once per debounce window
    is never nudged and the suppression is indistinguishable from disabling the watcher.
    The default must respect this, and so must any operator value a reviewer copies from
    this test.
    """
    from swarm.config.models import DroneConfig

    default = DroneConfig()
    assert default.idle_nudge_activity_window_seconds < default.idle_nudge_debounce_seconds
    assert window < default.idle_nudge_debounce_seconds


def test_the_suppression_is_reported_not_silent():
    """Every other suppression here announces itself as AUTO_NUDGE_SKIPPED with a reason.
    A skip that logged nothing would make the ABSENCE of a nudge the only evidence — and
    absence of a signal is not absence of a problem."""
    watcher = _watcher(window=600)

    reason = watcher._suppression_reason(_worker(resting_for=5))

    assert reason and "finished a turn" in reason and "ago" in reason


# ---------------------------------------------------------------------------
# Through the SWEEP, not just the predicate
# ---------------------------------------------------------------------------
#
# Everything above tests `_suppression_reason`. The sweep is what actually decides
# whether a worker is poked, and a guard that is correct in isolation while the caller
# ignores it is the defect this codebase hit six times this week. These drive
# `IdleWatcher.sweep` end to end.


def _sweep_watcher(*, resting_for: float) -> tuple[IdleWatcher, AsyncMock, MagicMock]:
    from swarm.worker.worker import WorkerState

    cfg = MagicMock()
    cfg.idle_nudge_interval_seconds = 180
    cfg.idle_nudge_debounce_seconds = 900
    cfg.idle_nudge_max_repeats = 0
    cfg.idle_nudge_activity_window_seconds = 600
    cfg.assign_operator_engagement_minutes = 0

    task = MagicMock()
    task.number = 1608
    task.id = "t-1608"
    task.assigned_worker = "swarm"
    task.is_on_hold = False
    task.status.value = "active"
    board = MagicMock()
    board.assigned_or_active_tasks = [task]

    send = AsyncMock()
    drone_log = MagicMock()
    watcher = IdleWatcher(
        drone_config=cfg,
        task_board=board,
        drone_log=drone_log,
        send_to_worker=send,
    )
    worker = MagicMock()
    worker.name = "swarm"
    worker.display_state = WorkerState.RESTING
    worker.state_duration = resting_for
    return watcher, send, drone_log, worker  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_the_sweep_sends_no_nudge_to_a_recently_active_worker():
    """The reported case, end to end: RESTING, holding an ACTIVE task, active 30s ago."""
    watcher, send, drone_log, worker = _sweep_watcher(resting_for=30)

    sent = await watcher.sweep([worker], now=10_000.0)

    assert sent == 0
    send.assert_not_awaited()
    skips = [c.args[2] for c in drone_log.add.call_args_list if len(c.args) >= 3]
    assert any("within 600s window" in str(s) for s in skips), f"skip not logged: {skips}"


@pytest.mark.asyncio
async def test_the_sweep_still_nudges_a_worker_that_has_gone_quiet():
    """POSITIVE CONTROL at the sweep level. Without it, a sweep that nudged nobody would
    pass the test above while disabling the watcher — and would look identical."""
    watcher, send, _drone_log, worker = _sweep_watcher(resting_for=3600)

    sent = await watcher.sweep([worker], now=10_000.0)

    assert sent == 1
    send.assert_awaited_once()
    assert "#1608" in send.await_args.args[1]


# ---------------------------------------------------------------------------
# #1615 — replayed against the EIGHT REAL NUDGES this worker received
# ---------------------------------------------------------------------------
#
# Measured state_duration at each: 61, 750, 49, 30, 48, 152, 532, 558 seconds.
# Under #1610's MCP signal the gaps were 783/2098/864/86/463/3334/4322/651 — it would
# have suppressed 2 of 8. state_duration suppresses 7 of 8, and the one it lets through
# is a genuine 12.5-minute pause with no turn completed, which is a fair nudge.

REAL_NUDGE_STATE_DURATIONS = [61, 750, 49, 30, 48, 152, 532, 558]


def test_the_signal_suppresses_seven_of_the_eight_real_nudges():
    """The before/after AC, asserted rather than described. If a change drops this
    below 7 the signal got worse, and the number says so."""
    watcher = _watcher(window=600)

    suppressed = sum(
        1
        for d in REAL_NUDGE_STATE_DURATIONS
        if watcher._suppression_reason(_worker(resting_for=d)) is not None
    )

    assert suppressed == 7, f"expected 7/8 suppressed, got {suppressed}/8"


def test_the_one_it_lets_through_is_a_genuine_long_pause():
    """750s with no turn completed. Suppressing that would mean never nudging a worker
    that stopped mid-session, which is exactly what #225 exists to catch."""
    assert _watcher(window=600)._suppression_reason(_worker(resting_for=750)) is None


def test_a_slept_worker_is_never_suppressed():
    """SLEEPING is RESTING past `sleeping_threshold` (1200s), so it always exceeds this
    window. Pinned so a future window raised above 1200 does not silently make slept
    workers unnudgeable — the guard-that-cannot-fire shape."""
    from swarm.config.models import DroneConfig

    default = DroneConfig()
    assert default.idle_nudge_activity_window_seconds < default.sleeping_threshold
    assert _watcher(window=600)._suppression_reason(_worker(resting_for=1200)) is None
