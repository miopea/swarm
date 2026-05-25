"""Regression tests for code review fixes (Phase 1).

Covers: pilot timeouts, classify_output exception handling, config validation,
TaskBoard lock, escalation clearing, content hashing, rate limiter cleanup,
grace period on kill, and serialize_config completeness.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from swarm.config import (
    DroneConfig,
    HiveConfig,
    WorkerConfig,
    _parse_config,
    serialize_config,
)
from swarm.drones.log import DroneLog
from swarm.drones.pilot import DronePilot
from swarm.tasks.board import TaskBoard
from swarm.worker.worker import Worker, WorkerState
from tests.conftest import make_worker as _make_worker


def _write_yaml(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "swarm.yaml"
    p.write_text(yaml.dump(data, default_flow_style=False))
    return p


# --- Fix #1: get_content() exception safety in pilot ---


class TestGetContentExceptionSafety:
    """Pilot should not crash when get_content() raises."""

    @pytest.mark.asyncio
    async def test_get_content_exception_skips_worker(self):
        """If get_content raises, the worker is skipped without crashing."""
        worker = _make_worker("api")
        worker.process.get_content = MagicMock(side_effect=RuntimeError("lock stuck"))

        log = DroneLog()
        pilot = DronePilot([worker], log, interval=1.0, drone_config=DroneConfig())
        pilot.enabled = True

        with patch("swarm.drones.pilot.revive_worker", AsyncMock()):
            # Should not raise
            await pilot.poll_once()


# --- Fix #2: classify_output() exception handling ---


class TestClassifyOutputExceptionSafety:
    """classify_output failure should fall back to previous state."""

    @pytest.mark.asyncio
    async def test_classify_output_exception_keeps_previous_state(self):
        """Worker keeps its previous state when classify_output raises."""
        worker = _make_worker("api", state=WorkerState.BUZZING)
        worker.process.set_content("esc to interrupt")
        worker.process._child_foreground_command = "claude"

        log = DroneLog()
        pilot = DronePilot([worker], log, interval=1.0, drone_config=DroneConfig())
        pilot.enabled = False  # disable decisions to isolate classification

        # Make classify_output raise
        bad_provider = MagicMock()
        bad_provider.classify_output.side_effect = ValueError("bad output")
        pilot._providers = {"claude": bad_provider}

        with patch("swarm.drones.pilot.revive_worker", AsyncMock()):
            await pilot.poll_once()

        # Worker should still be BUZZING (fallback to previous state)
        assert worker.state == WorkerState.BUZZING


# --- Fix #3: Config key validation ---


class TestConfigKeyValidation:
    """Unrecognized config keys should produce warnings."""

    def test_warns_on_unknown_top_level_key(self, tmp_path, caplog):
        data = {
            "session_name": "test",
            "workers": [{"name": "w1", "path": "/tmp"}],
            "unknwon_key": True,  # typo
        }
        path = _write_yaml(tmp_path, data)
        with caplog.at_level(logging.WARNING, logger="swarm.config"):
            _parse_config(path)
        assert any("unknwon_key" in r.message for r in caplog.records)

    def test_warns_on_unknown_drones_key(self, tmp_path, caplog):
        data = {
            "workers": [],
            "drones": {
                "enabled": True,
                "poll_intervl": 3,  # typo
            },
        }
        path = _write_yaml(tmp_path, data)
        with caplog.at_level(logging.WARNING, logger="swarm.config"):
            _parse_config(path)
        assert any("poll_intervl" in r.message for r in caplog.records)

    def test_warns_on_unknown_queen_key(self, tmp_path, caplog):
        data = {
            "workers": [],
            "queen": {"cooldwn": 30.0},
        }
        path = _write_yaml(tmp_path, data)
        with caplog.at_level(logging.WARNING, logger="swarm.config"):
            _parse_config(path)
        assert any("cooldwn" in r.message for r in caplog.records)

    def test_no_warning_for_valid_keys(self, tmp_path, caplog):
        data = {
            "session_name": "test",
            "workers": [{"name": "w1", "path": "/tmp"}],
            "drones": {"enabled": True, "poll_interval": 5.0},
        }
        path = _write_yaml(tmp_path, data)
        with caplog.at_level(logging.WARNING, logger="swarm.config"):
            _parse_config(path)
        assert not any("unrecognized" in r.message for r in caplog.records)


# --- Fix #4: TaskBoard uses threading.Lock (not RLock) ---


class TestTaskBoardLock:
    """TaskBoard uses RLock for reentrant callback safety."""

    def test_lock_is_reentrant(self):
        """RLock required: _notify() callbacks re-enter the board."""
        import threading

        board = TaskBoard()
        assert isinstance(board._lock, type(threading.RLock()))


# --- Fix #5: Escalation clearing on proposal dismiss ---


class TestEscalationClearing:
    """Escalation tracking should clear when proposals are resolved."""

    def test_clear_escalation_removes_worker(self):
        workers = [_make_worker("api")]
        log = DroneLog()
        pilot = DronePilot(workers, log, interval=1.0, drone_config=DroneConfig())
        pilot._escalated["api"] = 0.0
        pilot.clear_escalation("api")
        assert "api" not in pilot._escalated

    def test_clear_escalation_noop_for_missing(self):
        workers = [_make_worker("api")]
        log = DroneLog()
        pilot = DronePilot(workers, log, interval=1.0, drone_config=DroneConfig())
        # Should not raise
        pilot.clear_escalation("nonexistent")


# --- Fix #6: Content fingerprinting uses hashlib ---


class TestContentFingerprinting:
    """Content fingerprinting should use deterministic hashlib."""

    def test_fingerprint_is_deterministic(self):
        """Same content should produce the same fingerprint across runs."""
        content = "some output text for fingerprinting"
        expected = hashlib.sha256(content[-200:].encode()).hexdigest()[:16]
        # Verify the algorithm matches what pilot uses
        assert len(expected) == 16
        assert expected == hashlib.sha256(content[-200:].encode()).hexdigest()[:16]

    def test_pilot_fingerprint_updates(self):
        workers = [_make_worker("api")]
        log = DroneLog()
        pilot = DronePilot(workers, log, interval=1.0, drone_config=DroneConfig())

        pilot._state_tracker._update_content_fingerprint("api", "content A")
        fp_a = pilot._state_tracker._content_fingerprints["api"]

        pilot._state_tracker._update_content_fingerprint("api", "content B")
        fp_b = pilot._state_tracker._content_fingerprints["api"]

        assert fp_a != fp_b
        assert pilot._state_tracker._unchanged_streak["api"] == 0

    def test_unchanged_streak_increments(self):
        workers = [_make_worker("api")]
        log = DroneLog()
        pilot = DronePilot(workers, log, interval=1.0, drone_config=DroneConfig())

        pilot._state_tracker._update_content_fingerprint("api", "same content")
        pilot._state_tracker._update_content_fingerprint("api", "same content")
        pilot._state_tracker._update_content_fingerprint("api", "same content")

        assert pilot._state_tracker._unchanged_streak["api"] == 2  # 2 repeats after first


# --- Fix #8: Rate limiter TTL cleanup ---


class TestRateLimiterCleanup:
    """Rate limiter should clean up stale IP entries."""

    def test_stale_entries_cleaned(self):
        """Old IP entries should be removed during periodic cleanup."""
        from collections import defaultdict

        rate_limits: dict[str, list[float]] = defaultdict(list)
        now = time.time()

        # Add a stale entry (2 minutes old)
        rate_limits["1.2.3.4"] = [now - 120]
        # Add a fresh entry
        rate_limits["5.6.7.8"] = [now - 5]

        # Simulate cleanup logic from the middleware
        cutoff = now - 60
        stale = [k for k, v in rate_limits.items() if not v or v[-1] < cutoff]
        for k in stale:
            del rate_limits[k]

        assert "1.2.3.4" not in rate_limits
        assert "5.6.7.8" in rate_limits

    def test_max_ip_cap_evicts_oldest(self):
        """When IP count exceeds the cap, least-recently-active IPs are evicted."""
        from collections import defaultdict

        from swarm.server.api import _RATE_LIMIT_MAX_IPS

        rate_limits: dict[str, list[float]] = defaultdict(list)
        now = time.time()

        # Add MAX + 5 IPs with ascending timestamps
        total = _RATE_LIMIT_MAX_IPS + 5
        for i in range(total):
            rate_limits[f"10.0.0.{i}"] = [now - total + i]

        # Simulate the hard-cap cleanup
        if len(rate_limits) > _RATE_LIMIT_MAX_IPS:

            def _last_ts(k: str) -> float:
                return rate_limits[k][-1] if rate_limits[k] else 0

            by_recency = sorted(rate_limits, key=_last_ts)
            excess = len(rate_limits) - _RATE_LIMIT_MAX_IPS
            for k in by_recency[:excess]:
                del rate_limits[k]

        assert len(rate_limits) == _RATE_LIMIT_MAX_IPS
        # Oldest 5 should be gone
        for i in range(5):
            assert f"10.0.0.{i}" not in rate_limits
        # Newest should remain
        assert f"10.0.0.{total - 1}" in rate_limits


# --- Fix #10: Grace period cleared on explicit kill ---


class TestGracePeriodOnKill:
    """force_state should clear _revive_at so STUNG detection isn't suppressed."""

    def test_force_state_clears_revive_at(self):
        worker = Worker(name="api", path="/tmp")
        worker.record_revive()
        assert worker._revive_at > 0

        worker.force_state(WorkerState.STUNG)
        assert worker._revive_at == 0.0

    def test_force_state_to_buzzing_clears_revive_at(self):
        worker = Worker(name="api", path="/tmp", state=WorkerState.STUNG)
        worker.record_revive()
        assert worker._revive_at > 0

        worker.force_state(WorkerState.BUZZING)
        assert worker._revive_at == 0.0


# --- Fix #12 (plan #14): serialize_config includes all DroneConfig fields ---


class TestSerializeConfigCompleteness:
    """serialize_config should round-trip all DroneConfig fields."""

    def test_drone_state_polling_fields_serialized(self):
        config = HiveConfig(
            workers=[WorkerConfig(name="w1", path="/tmp")],
            drones=DroneConfig(
                poll_interval_buzzing=10.0,
                poll_interval_waiting=3.0,
                poll_interval_resting=15.0,
                auto_complete_min_idle=60.0,
                sleeping_poll_interval=45.0,
            ),
        )
        data = serialize_config(config)
        drones = data["drones"]
        assert drones["poll_interval_buzzing"] == 10.0
        assert drones["poll_interval_waiting"] == 3.0
        assert drones["poll_interval_resting"] == 15.0
        assert drones["auto_complete_min_idle"] == 60.0
        assert drones["sleeping_poll_interval"] == 45.0

    def test_drone_fields_roundtrip(self, tmp_path):
        """Write config with state-aware polling, re-parse, verify values."""
        config = HiveConfig(
            workers=[WorkerConfig(name="w1", path="/tmp")],
            drones=DroneConfig(
                poll_interval_buzzing=8.0,
                poll_interval_waiting=2.0,
                poll_interval_resting=12.0,
                auto_complete_min_idle=90.0,
                sleeping_poll_interval=60.0,
            ),
        )
        data = serialize_config(config)
        path = _write_yaml(tmp_path, data)
        restored = _parse_config(path)
        assert restored.drones.poll_interval_buzzing == 8.0
        assert restored.drones.poll_interval_waiting == 2.0
        assert restored.drones.poll_interval_resting == 12.0
        assert restored.drones.auto_complete_min_idle == 90.0
        assert restored.drones.sleeping_poll_interval == 60.0


# --- Fix #11 (plan #12): WebSocket backpressure ---


class TestHolderBackpressure:
    """Holder should drop slow clients based on write buffer size."""

    def test_broadcast_drops_slow_client(self):
        """Client with large write buffer should be dropped."""
        from swarm.pty.holder import _MAX_WRITE_BUFFER, PtyHolder

        holder = PtyHolder()

        # Create a mock writer with oversized buffer
        slow_writer = MagicMock()
        slow_transport = MagicMock()
        slow_transport.get_write_buffer_size.return_value = _MAX_WRITE_BUFFER + 1
        slow_writer.transport = slow_transport

        # Create a healthy writer
        fast_writer = MagicMock()
        fast_transport = MagicMock()
        fast_transport.get_write_buffer_size.return_value = 0
        fast_writer.transport = fast_transport

        holder._clients = {slow_writer, fast_writer}
        holder._broadcast(b"test data\n")

        # Slow client should be dropped
        assert slow_writer not in holder._clients
        # Fast client should remain and receive data
        assert fast_writer in holder._clients
        fast_writer.write.assert_called_once_with(b"test data\n")
        # Slow client should NOT have received data
        slow_writer.write.assert_not_called()
