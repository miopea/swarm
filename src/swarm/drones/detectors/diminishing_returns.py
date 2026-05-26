"""DiminishingReturnsDetector — flag BUZZING workers burning tokens without progress.

Extracted from :class:`~swarm.drones.state_tracker.WorkerStateTracker`
(Phase 1 of ``docs/specs/state-tracker-refactor.md``). Triggers when
``_DIMINISHING_STREAK`` consecutive new turns each add fewer than
``_DIMINISHING_DELTA`` tokens — a typical signature of a worker stuck
in a long-running tool loop that's not making forward progress.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from swarm.drones.log import LogCategory, SystemAction

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.drones.log import DroneLog
    from swarm.worker.worker import Worker


# Token-delta floor: a new turn adding fewer than this many input
# tokens counts as "no progress" for the streak counter.
_DIMINISHING_DELTA = 500

# Consecutive low-delta turns before we escalate to the operator.
_DIMINISHING_STREAK = 3


class DiminishingReturnsDetector:
    """Watch BUZZING workers for stalling token growth.

    The streak counter lives on the Worker dataclass
    (``Worker._low_delta_streak``) so it survives detector lifetimes
    if the tracker is recreated.  Suspends self-spam by resetting the
    counter every time it escalates.
    """

    def __init__(self, log: DroneLog, emit: Callable[..., None]) -> None:
        self._log = log
        self._emit = emit

    def check(self, worker: Worker) -> None:
        from swarm.worker.worker import WorkerState

        if worker.state != WorkerState.BUZZING:
            # Reset streak on state change
            if worker._low_delta_streak > 0:
                worker._low_delta_streak = 0
            return

        current = worker.usage.last_turn_input_tokens
        prev = worker._prev_input_tokens

        # Need a baseline before we can compute deltas
        if prev == 0 or current == 0:
            worker._prev_input_tokens = current
            return

        # Only evaluate delta on a new turn — last_turn_input_tokens only
        # changes when a new assistant message is written to the session
        # JSONL. Polls run faster than turns (seconds vs tens of seconds),
        # so a stationary value means "same turn still in progress", not
        # "no progress". Treat it as an indeterminate poll and skip.
        if current == prev:
            return

        worker._prev_input_tokens = current
        delta = current - prev
        if delta < _DIMINISHING_DELTA:
            # Skip if sub-agent is active (parent idle while child works)
            proc = worker.process
            if proc:
                tail = proc.get_content(10)
                from swarm.providers.claude import _RE_SUBAGENT_ACTIVE

                if _RE_SUBAGENT_ACTIVE.search(tail):
                    worker._low_delta_streak = 0
                    return
            worker._low_delta_streak += 1
        else:
            worker._low_delta_streak = 0

        if worker._low_delta_streak >= _DIMINISHING_STREAK:
            worker._low_delta_streak = 0  # reset to avoid spam
            self._log.add(
                SystemAction.QUEEN_BLOCKED,
                worker.name,
                f"diminishing returns — {_DIMINISHING_STREAK} consecutive"
                f" low-delta turns (delta={delta} tokens)",
                category=LogCategory.DRONE,
            )
            self._emit(
                "escalate",
                worker,
                "diminishing returns — context growing but output stalled",
            )
