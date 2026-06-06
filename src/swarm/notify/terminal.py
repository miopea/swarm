"""Terminal notification backend — rings the terminal bell."""

from __future__ import annotations

import sys

from swarm.notify.bus import NotifyEvent, Severity


def terminal_bell_backend(event: NotifyEvent) -> None:
    """Ring the terminal bell for warnings and urgent events."""
    if event.severity in (Severity.WARNING, Severity.URGENT):
        try:
            sys.stderr.write("\a")
            sys.stderr.flush()
        except BrokenPipeError:
            pass
