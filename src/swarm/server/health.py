"""Daemon self-health sweep — disk space and DB integrity alerting.

A long-running daemon can degrade silently: the disk fills until SQLite
writes start failing, or the database corrupts and every feature breaks
at once. Both conditions are cheap to detect and catastrophic to miss,
so this sweep checks them periodically and pushes URGENT notifications
through the existing bus (desktop/email/webhook backends).

Alerts are sticky per condition: one notification when a condition
trips, silence while it persists, re-armed when it clears — the bus's
5-second debounce alone would re-fire on every sweep.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from swarm.logging import get_logger
from swarm.notify.bus import EventType, NotifyEvent, Severity
from swarm.paths import state_dir

if TYPE_CHECKING:
    from swarm.db.core import SwarmDB
    from swarm.notify.bus import NotificationBus

_log = get_logger("server.health")

# Disk thresholds — alert when BOTH free% and absolute-free fall below.
# A 10 TB array at 4% free still has 400 GB; a 20 GB VPS at 12% has 2.4 GB.
# Requiring both avoids false alarms on either extreme.
_DISK_MIN_FREE_PCT = 0.10
_DISK_MIN_FREE_BYTES = 5 * 2**30  # 5 GiB

_DISK_INTERVAL_SECONDS = 600.0  # 10 minutes
_INTEGRITY_INTERVAL_SECONDS = 86_400.0  # daily


@dataclass(frozen=True)
class DiskUsage:
    total: int
    free: int


def _swarm_disk_usage() -> DiskUsage:
    usage = shutil.disk_usage(state_dir())
    return DiskUsage(total=usage.total, free=usage.free)


class HealthSweep:
    """Periodic disk-space + DB-integrity checks with sticky alerting."""

    def __init__(
        self,
        *,
        db: SwarmDB,
        notify: Callable[[], NotificationBus | None],
        disk_usage_fn: Callable[[], DiskUsage] = _swarm_disk_usage,
    ) -> None:
        self._db = db
        self._notify = notify
        self._disk_usage_fn = disk_usage_fn
        self._disk_alerted = False
        self._integrity_alerted = False

    def _emit(self, title: str, message: str) -> None:
        bus = self._notify()
        if bus is None:
            return
        bus.emit(
            NotifyEvent(
                event_type=EventType.DAEMON_HEALTH,
                title=title,
                message=message,
                severity=Severity.URGENT,
            )
        )

    def check_disk(self) -> None:
        """Alert once when free space falls below both thresholds."""
        try:
            usage = self._disk_usage_fn()
        except OSError:
            _log.warning("disk usage check failed", exc_info=True)
            return
        if usage.total <= 0:
            return
        free_pct = usage.free / usage.total
        low = free_pct < _DISK_MIN_FREE_PCT and usage.free < _DISK_MIN_FREE_BYTES
        if low and not self._disk_alerted:
            self._disk_alerted = True
            free_gb = usage.free / 2**30
            _log.warning("disk space low: %.1f GiB free (%.0f%%)", free_gb, free_pct * 100)
            self._emit(
                "Disk space low",
                f"{state_dir()} volume has {free_gb:.1f} GiB free ({free_pct:.0%}). "
                "SQLite writes and PTY logs will start failing if it fills.",
            )
        elif not low:
            self._disk_alerted = False

    def check_integrity(self) -> None:
        """Alert once per failure streak of PRAGMA integrity_check."""
        try:
            ok = self._db.integrity_check()
        except Exception:
            _log.warning("integrity check could not run", exc_info=True)
            return
        if not ok and not self._integrity_alerted:
            self._integrity_alerted = True
            _log.error("swarm.db FAILED integrity check")
            self._emit(
                "Database integrity check FAILED",
                "swarm.db failed PRAGMA integrity_check. Stop the daemon and "
                "restore a backup: swarm db restore",
            )
        elif ok:
            self._integrity_alerted = False

    async def sweep_loop(self) -> None:
        """Disk every 10 minutes; integrity daily (first pass shortly after start)."""
        next_integrity = 0.0
        try:
            while True:
                await asyncio.sleep(_DISK_INTERVAL_SECONDS)
                self.check_disk()
                loop_now = asyncio.get_running_loop().time()
                if loop_now >= next_integrity:
                    next_integrity = loop_now + _INTEGRITY_INTERVAL_SECONDS
                    self.check_integrity()
        except asyncio.CancelledError:
            return
        except BaseException:
            _log.warning("health sweep loop crashed", exc_info=True)
