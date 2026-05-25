"""BackgroundLoopRunner — lifecycle owner for the daemon's periodic loops.

Extracted from :class:`~swarm.server.daemon.SwarmDaemon` (task — audit
2026-05-25). Before this module each background coroutine was wired
inline: a ``self._foo_task = asyncio.create_task(self._foo_loop())``
line in ``start()`` and a matching entry in the cancellation tuple in
``_cancel_timers``. The two lists drifted: adding a loop meant editing
both sites, and a missed cancellation handle would leak the task across
``os.execv`` reloads.

The runner centralises the lifecycle:

* :meth:`register` collects ``(name, factory, enabled)`` tuples.
* :meth:`start_all` materialises an ``asyncio.Task`` per registered
  loop whose ``enabled`` flag is true and that isn't already running.
  Idempotent — re-running ``start_all`` after a partial start is a
  no-op for entries that already have a live task.
* :meth:`start` starts a single named loop. The use case is the
  late-enable path (e.g. operator flips ``resources.enabled`` in the
  dashboard mid-run — ``reload_config`` calls
  ``loop_runner.start("resource")`` instead of repeating the
  ``create_task`` boilerplate).
* :meth:`cancel_all` cancels every live task and awaits their
  completion under ``gather(return_exceptions=True)`` so shutdown
  never raises on a worker that already errored out.

Loop *bodies* stay on the daemon. They're tightly coupled to daemon
state — moving them here would require plumbing ~25 closures into the
runner constructor and split one god class into two. The win that
matters is separating lifecycle plumbing from business logic; that is
what this module does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from swarm.logging import get_logger

_log = get_logger("server.loop_runner")


@dataclass(frozen=True)
class _Registration:
    name: str
    factory: Callable[[], Awaitable[None]]
    enabled: bool


class BackgroundLoopRunner:
    """Owns the lifecycle of the daemon's long-running periodic tasks."""

    def __init__(self) -> None:
        self._registered: list[_Registration] = []
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def register(
        self,
        name: str,
        factory: Callable[[], Awaitable[None]],
        *,
        enabled: bool = True,
    ) -> None:
        """Record a loop. ``factory`` returns the coroutine to schedule.

        Registration is order-preserving; :meth:`start_all` materialises
        tasks in registration order so loops that depend on side-effects
        of earlier loops (rare, but the heartbeat reads state the usage
        loop primes) start in the deterministic order.
        """
        if any(r.name == name for r in self._registered):
            raise ValueError(f"loop {name!r} already registered")
        self._registered.append(_Registration(name=name, factory=factory, enabled=enabled))

    def start_all(self) -> None:
        """Start every enabled loop that isn't already running."""
        for reg in self._registered:
            self._start_one(reg)

    def start(self, name: str) -> bool:
        """Start a single registered loop by name.

        Returns ``True`` if a new task was created; ``False`` if the
        loop was disabled at registration time, already running, or
        unknown.
        """
        for reg in self._registered:
            if reg.name == name:
                return self._start_one(reg)
        _log.warning("BackgroundLoopRunner: start(%r) — unknown loop", name)
        return False

    def _start_one(self, reg: _Registration) -> bool:
        if not reg.enabled:
            return False
        existing = self._tasks.get(reg.name)
        if existing is not None and not existing.done():
            return False
        task = asyncio.create_task(reg.factory(), name=f"loop:{reg.name}")
        self._tasks[reg.name] = task
        return True

    def get(self, name: str) -> asyncio.Task[None] | None:
        """Return the live task for a loop, or ``None`` if not running."""
        return self._tasks.get(name)

    def names(self) -> list[str]:
        """Return the names of all registered loops, in registration order."""
        return [r.name for r in self._registered]

    async def cancel_all(self) -> None:
        """Cancel every registered task and await their completion.

        ``cancel`` on an already-done task is a no-op, so we don't
        bother filtering — preserving the original daemon shape and
        keeping the path test-mock friendly.
        """
        tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        # Only ``asyncio.Task`` instances are gather-safe; a test mock
        # passed via the back-compat setter isn't and would crash the
        # await.  Filter to real tasks for the wait.
        real = [t for t in tasks if isinstance(t, asyncio.Task)]
        if real:
            # return_exceptions=True so a CancelledError or a worker
            # exception doesn't propagate out of shutdown and abort
            # the remaining stop() sequence.
            await asyncio.gather(*real, return_exceptions=True)
        self._tasks.clear()
