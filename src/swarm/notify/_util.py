"""Shared helpers for notification backends."""

from __future__ import annotations

import threading
from collections.abc import Callable

from swarm.logging import get_logger

_log = get_logger("notify.util")


def run_detached(fn: Callable[[], None], *, name: str) -> None:
    """Run *fn* on a daemon thread, fire-and-forget.

    The notification bus dispatches backends synchronously on the daemon's
    async event loop (``bus.emit`` is not a coroutine). Backends that do
    blocking network I/O (SMTP, HTTP) must therefore not run inline — a slow
    or hung server would freeze the whole daemon for the backend's timeout.
    Offloading to a daemon thread keeps ``emit`` non-blocking regardless of the
    caller's context (the desktop backend already uses the same pattern for its
    subprocess reaper). ``fn`` is expected to handle its own errors; a failure
    to even start the thread is logged rather than propagated to the emit path.
    """
    try:
        threading.Thread(target=fn, daemon=True, name=name).start()
    except Exception:
        _log.warning("failed to start %s notification thread", name, exc_info=True)
