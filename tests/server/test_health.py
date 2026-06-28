"""Tests for server/health.py — daemon self-health sweep."""

from __future__ import annotations

from unittest.mock import MagicMock

from swarm.notify.bus import EventType, Severity
from swarm.server.health import DiskUsage, HealthSweep


def make_sweep(
    *,
    free_bytes: int = 100 * 2**30,
    total_bytes: int = 200 * 2**30,
    integrity_ok: bool = True,
):
    bus = MagicMock()
    db = MagicMock()
    db.integrity_check.return_value = integrity_ok
    usage = DiskUsage(total=total_bytes, free=free_bytes)
    sweep = HealthSweep(
        db=db,
        notify=lambda: bus,
        disk_usage_fn=lambda: usage,
    )
    return sweep, bus, db


def emitted_types(bus) -> list[EventType]:
    return [call.args[0].event_type for call in bus.emit.call_args_list]


class TestDiskCheck:
    def test_healthy_disk_no_alert(self):
        sweep, bus, _ = make_sweep(free_bytes=100 * 2**30)
        sweep.check_disk()
        bus.emit.assert_not_called()

    def test_low_disk_alerts(self):
        # 1 GiB free of 200 GiB — both pct and absolute thresholds breached
        sweep, bus, _ = make_sweep(free_bytes=2**30)
        sweep.check_disk()
        assert emitted_types(bus) == [EventType.DAEMON_HEALTH]
        event = bus.emit.call_args.args[0]
        assert event.severity == Severity.URGENT

    def test_low_disk_alerts_once_until_cleared(self):
        sweep, bus, _ = make_sweep(free_bytes=2**30)
        sweep.check_disk()
        sweep.check_disk()
        assert bus.emit.call_count == 1
        # Recovers → next breach alerts again
        sweep._disk_usage_fn = lambda: DiskUsage(total=200 * 2**30, free=100 * 2**30)
        sweep.check_disk()
        sweep._disk_usage_fn = lambda: DiskUsage(total=200 * 2**30, free=2**30)
        sweep.check_disk()
        assert bus.emit.call_count == 2


class TestIntegrityCheck:
    def test_ok_no_alert(self):
        sweep, bus, db = make_sweep(integrity_ok=True)
        sweep.check_integrity()
        bus.emit.assert_not_called()
        db.integrity_check.assert_called_once()

    def test_failure_alerts_urgent(self):
        sweep, bus, _ = make_sweep(integrity_ok=False)
        sweep.check_integrity()
        assert emitted_types(bus) == [EventType.DAEMON_HEALTH]
        assert bus.emit.call_args.args[0].severity == Severity.URGENT

    def test_failure_alerts_once_per_streak(self):
        sweep, bus, db = make_sweep(integrity_ok=False)
        sweep.check_integrity()
        sweep.check_integrity()
        assert bus.emit.call_count == 1
        db.integrity_check.return_value = True
        sweep.check_integrity()
        db.integrity_check.return_value = False
        sweep.check_integrity()
        assert bus.emit.call_count == 2

    def test_integrity_exception_does_not_raise(self):
        sweep, bus, db = make_sweep()
        db.integrity_check.side_effect = RuntimeError("locked")
        sweep.check_integrity()  # must not raise
        bus.emit.assert_not_called()
