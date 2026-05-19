"""compute_backoff — pure function for adaptive poll interval calculation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from swarm.worker.worker import WorkerState

if TYPE_CHECKING:
    from swarm.config import DroneConfig


def compute_backoff(
    *,
    workers: list,
    config: DroneConfig,
    idle_streak: int,
    base_interval: float,
    max_interval: float,
    pressure_level: str,
    focused_workers: set[str],
    focus_interval: float,
) -> float:
    """Compute poll interval based on worker states and idle streak.

    Uses explicit config overrides (poll_interval_buzzing, etc.) if set,
    otherwise derives from *base_interval* with sensible ratios:
    WAITING = 1x, BUZZING = 3x, RESTING = 3x.

    This is a pure function extracted from
    :class:`~swarm.drones.pilot.DronePilot._compute_backoff` for testability.
    """
    cfg = config
    base = base_interval
    states = {w.state for w in workers}

    # No workers at all -> long backoff, nothing to poll
    if not workers:
        return min(60.0, max_interval)

    waiting = WorkerState.WAITING in states
    if waiting:
        state_base = cfg.poll_interval_waiting or base
    elif WorkerState.BUZZING in states:
        state_base = cfg.poll_interval_buzzing or base * 3
    else:
        state_base = cfg.poll_interval_resting or base * 3

    # A WAITING worker is the one most likely to resume imminently — it
    # was just answered / unblocked — so it must NOT accumulate the idle
    # backoff a truly-idle RESTING worker gets. Poll it at the tight base
    # cadence so WAITING→BUZZING is observed in ~base seconds instead of
    # up to max_idle_interval (the 30–60s "stuck on waiting" lag).
    if waiting:
        backoff = min(state_base, max_interval)
    else:
        backoff = min(
            state_base * (2 ** min(idle_streak, 3)),
            max_interval,
        )
    # Cap backoff when user is actively viewing a worker that needs
    # quick response (WAITING/RESTING).  BUZZING workers don't benefit
    # from fast polling.
    focused = focused_workers & {w.name for w in workers}
    if focused:
        focused_states = {w.state for w in workers if w.name in focused}
        if focused_states & {WorkerState.WAITING, WorkerState.RESTING}:
            backoff = min(backoff, focus_interval)
    # Reduce polling overhead during high memory pressure — but never at
    # the cost of WAITING responsiveness (a WAITING worker is transient
    # and about to resume; doubling its interval reintroduces the lag
    # this function is meant to remove).
    if pressure_level in ("high", "critical") and not waiting:
        backoff = min(backoff * 2, max_interval)
    return backoff
