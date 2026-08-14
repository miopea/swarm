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

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.drones.idle_watcher import IdleWatcher


def _watcher(*, window: float, last_activity: float | None) -> IdleWatcher:
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


def _worker(name: str = "swarm") -> MagicMock:
    w = MagicMock()
    w.name = name
    return w


def test_a_worker_active_seconds_ago_is_not_nudged():
    """The reported case: gaps of 0s, 2s and 30s to the last activity."""
    watcher = _watcher(window=600, last_activity=time.time() - 30)

    reason = watcher._suppression_reason(_worker())

    assert reason is not None
    assert "within 600s window" in reason


def test_the_134_second_gap_is_suppressed_at_600_but_not_at_120():
    """The one real nudge that distinguishes the candidate windows — which is why the
    choice between them is a measurement rather than a preference."""
    recent = time.time() - 134

    assert _watcher(window=600, last_activity=recent)._suppression_reason(_worker()) is not None
    assert _watcher(window=120, last_activity=recent)._suppression_reason(_worker()) is None


def test_a_worker_with_NO_recent_activity_is_still_nudged():
    """POSITIVE CONTROL, and it is the one that decides whether this is safe.

    #225 exists because workers really do drop tasks. A suppression that silenced the
    watcher entirely would recreate that defect and would look identical to the watcher
    having nothing to do — which is exactly the shape this codebase has hit repeatedly.
    """
    watcher = _watcher(window=600, last_activity=time.time() - 3600)

    assert watcher._suppression_reason(_worker()) is None


def test_a_worker_that_has_never_called_mcp_is_still_nudged():
    """None means "no record", NOT "idle forever" — the fail-open direction this fleet
    keeps getting backwards. Here both readings agree: a worker with no MCP activity at
    all is precisely who the watcher exists for, so it is nudged either way. Pinned so a
    later "fail open on None" change does not silently disable the watcher."""
    watcher = _watcher(window=600, last_activity=None)

    assert watcher._suppression_reason(_worker()) is None


def test_a_zero_window_disables_the_suppression_entirely():
    """The off switch, and it must fail toward NUDGING. A zero that suppressed
    everything would be an operator turning the feature off and getting silence."""
    watcher = _watcher(window=0, last_activity=time.time())

    assert watcher._suppression_reason(_worker()) is None


def test_a_raising_lookup_does_not_suppress_and_does_not_crash_the_sweep():
    """A broken activity source must cost the suppression, not the whole sweep — and it
    must fall back to NUDGING, because a lookup that cannot answer is not evidence the
    worker is busy."""
    cfg = MagicMock()
    cfg.idle_nudge_interval_seconds = 180
    cfg.idle_nudge_debounce_seconds = 900
    cfg.idle_nudge_activity_window_seconds = 600
    cfg.assign_operator_engagement_minutes = 0

    def boom(_name: str) -> float:
        raise RuntimeError("activity source unavailable")

    watcher = IdleWatcher(
        drone_config=cfg,
        task_board=MagicMock(),
        drone_log=MagicMock(),
        send_to_worker=AsyncMock(),
        mcp_activity_lookup=boom,
    )

    assert watcher._suppression_reason(_worker()) is None


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
    watcher = _watcher(window=600, last_activity=time.time() - 5)

    reason = watcher._suppression_reason(_worker())

    assert reason and "active" in reason and "ago" in reason


# ---------------------------------------------------------------------------
# Through the SWEEP, not just the predicate
# ---------------------------------------------------------------------------
#
# Everything above tests `_suppression_reason`. The sweep is what actually decides
# whether a worker is poked, and a guard that is correct in isolation while the caller
# ignores it is the defect this codebase hit six times this week. These drive
# `IdleWatcher.sweep` end to end.


def _sweep_watcher(*, last_activity: float | None) -> tuple[IdleWatcher, AsyncMock, MagicMock]:
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
        mcp_activity_lookup=lambda _n: last_activity,
    )
    worker = MagicMock()
    worker.name = "swarm"
    worker.display_state = WorkerState.RESTING
    return watcher, send, drone_log, worker  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_the_sweep_sends_no_nudge_to_a_recently_active_worker():
    """The reported case, end to end: RESTING, holding an ACTIVE task, active 30s ago."""
    watcher, send, drone_log, worker = _sweep_watcher(last_activity=time.time() - 30)

    sent = await watcher.sweep([worker], now=10_000.0)

    assert sent == 0
    send.assert_not_awaited()
    skips = [c.args[2] for c in drone_log.add.call_args_list if len(c.args) >= 3]
    assert any("within 600s window" in str(s) for s in skips), f"skip not logged: {skips}"


@pytest.mark.asyncio
async def test_the_sweep_still_nudges_a_worker_that_has_gone_quiet():
    """POSITIVE CONTROL at the sweep level. Without it, a sweep that nudged nobody would
    pass the test above while disabling the watcher — and would look identical."""
    watcher, send, _drone_log, worker = _sweep_watcher(last_activity=time.time() - 3600)

    sent = await watcher.sweep([worker], now=10_000.0)

    assert sent == 1
    send.assert_awaited_once()
    assert "#1608" in send.await_args.args[1]
