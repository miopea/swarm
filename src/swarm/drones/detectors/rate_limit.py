"""RateLimitDetector — spot provider rate-limit messages in PTY output.

Extracted from :class:`~swarm.drones.state_tracker.WorkerStateTracker`
(Phase 1 of ``docs/specs/state-tracker-refactor.md``).  Owns the
per-worker last-seen timestamps so the debounce window is
detector-local rather than smeared across WorkerStateTracker.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from swarm.drones.log import LogCategory, SystemAction

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.drones.log import DroneLog
    from swarm.worker.worker import Worker

# Suppress duplicate alerts for the same worker within this window.
_DEBOUNCE_SECONDS = 60.0


class RateLimitDetector:
    """Scan PTY output for rate-limit messages and emit an alert.

    The matching line (truncated to 120 chars) is logged as a
    notification-worthy buzz entry and forwarded as a ``rate_limit``
    event for downstream watchers.
    """

    def __init__(self, log: DroneLog, emit: Callable[..., None]) -> None:
        self._log = log
        self._emit = emit
        self._rate_limit_seen: dict[str, float] = {}

    def check(self, worker: Worker, content: str) -> None:
        from swarm.providers.claude import _RE_RATE_LIMIT

        m = _RE_RATE_LIMIT.search(content)
        if not m:
            return
        # Debounce: don't spam for the same worker within the window.
        now = time.time()
        last = self._rate_limit_seen.get(worker.name, 0.0)
        if now - last < _DEBOUNCE_SECONDS:
            return
        self._rate_limit_seen[worker.name] = now
        # Extract the matching line for context
        line_start = content.rfind("\n", 0, m.start()) + 1
        line_end = content.find("\n", m.end())
        if line_end == -1:
            line_end = len(content)
        msg = content[line_start:line_end].strip()[:120]
        self._log.add(
            SystemAction.QUEEN_BLOCKED,
            worker.name,
            f"rate limit: {msg}",
            category=LogCategory.WORKER,
            is_notification=True,
        )
        self._emit("rate_limit", worker, msg)

    def last_seen(self, name: str) -> float:
        """Most recent rate-limit timestamp for *name*, or 0.0 if never seen.

        ``poll_dispatcher`` reads this to gate state-tick processing for
        recently-rate-limited workers.
        """
        return self._rate_limit_seen.get(name, 0.0)

    def forget(self, name: str) -> None:
        """Drop tracking state for a worker (dead-worker cleanup hook)."""
        self._rate_limit_seen.pop(name, None)
