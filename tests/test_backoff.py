"""Unit tests for compute_backoff — the adaptive poll-interval policy.

Pure function, so no pilot/daemon fixture needed. The load-bearing case
is WAITING: a WAITING worker is about to resume the moment it's answered,
so it must poll at the tight base cadence (not the idle backoff a truly
RESTING worker gets) — otherwise WAITING→BUZZING isn't observed for up
to max_idle_interval (the 30–60s "stuck on waiting" lag).
"""

from __future__ import annotations

from swarm.config import DroneConfig
from swarm.drones.backoff import compute_backoff
from swarm.worker.worker import WorkerState


class _W:
    def __init__(self, name: str, state: WorkerState) -> None:
        self.name = name
        self.state = state


def _call(
    state: WorkerState,
    *,
    idle_streak: int = 0,
    pressure: str = "nominal",
    focused=None,
    cfg=None,
):
    return compute_backoff(
        workers=[_W("budgetbug", state)],
        config=cfg or DroneConfig(),
        idle_streak=idle_streak,
        base_interval=5.0,
        max_interval=30.0,
        pressure_level=pressure,
        focused_workers=focused or set(),
        focus_interval=2.0,
    )


# --- WAITING: the fix ------------------------------------------------------


def test_waiting_ignores_idle_streak():
    # Pre-fix this was min(5 * 2**3, 30) = 30s. Now: flat base cadence.
    assert _call(WorkerState.WAITING, idle_streak=10) == 5.0
    assert _call(WorkerState.WAITING, idle_streak=0) == 5.0


def test_waiting_ignores_memory_pressure_doubling():
    assert _call(WorkerState.WAITING, idle_streak=10, pressure="critical") == 5.0
    assert _call(WorkerState.WAITING, idle_streak=10, pressure="high") == 5.0


def test_waiting_focus_still_speeds_up():
    # Focusing a WAITING worker may only make it faster, never slower.
    assert _call(WorkerState.WAITING, focused={"budgetbug"}) == 2.0


def test_waiting_respects_explicit_poll_interval_waiting():
    cfg = DroneConfig(poll_interval_waiting=3.0)
    assert _call(WorkerState.WAITING, idle_streak=9, cfg=cfg) == 3.0


def test_waiting_wins_when_mixed_with_buzzing():
    backoff = compute_backoff(
        workers=[_W("a", WorkerState.BUZZING), _W("b", WorkerState.WAITING)],
        config=DroneConfig(),
        idle_streak=8,
        base_interval=5.0,
        max_interval=30.0,
        pressure_level="nominal",
        focused_workers=set(),
        focus_interval=2.0,
    )
    assert backoff == 5.0


# --- Regression guards: BUZZING / RESTING backoff unchanged ----------------


def test_resting_still_backs_off_with_idle_streak():
    # base*3=15, *2**3=120, capped at max_idle_interval=30.
    assert _call(WorkerState.RESTING, idle_streak=10) == 30.0
    assert _call(WorkerState.RESTING, idle_streak=0) == 15.0


def test_buzzing_still_backs_off_and_pressure_still_doubles():
    assert _call(WorkerState.BUZZING, idle_streak=0) == 15.0
    # base*3=15, doubled by critical pressure = 30 (still ≤ max).
    assert _call(WorkerState.BUZZING, idle_streak=0, pressure="critical") == 30.0


def test_no_workers_long_backoff():
    assert (
        compute_backoff(
            workers=[],
            config=DroneConfig(),
            idle_streak=0,
            base_interval=5.0,
            max_interval=30.0,
            pressure_level="nominal",
            focused_workers=set(),
            focus_interval=2.0,
        )
        == 30.0
    )
