"""Tests for the conftest live-DB safeguard's external-daemon detection.

The ``_assert_live_db_untouched`` session finalizer must NOT fire when a
real ``swarm serve`` daemon is running — the daemon legitimately
WAL-checkpoints ``~/.swarm/swarm.db`` every 300s, which changes the file
mtime through no fault of any test. ``_external_daemon_running`` is the
guard that distinguishes "an external writer is present" from "a test
bypassed the sandbox".
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests.conftest import _external_daemon_running


def test_missing_lock_is_not_running(tmp_path: Path) -> None:
    assert _external_daemon_running(tmp_path / "absent.lock") is False


def test_garbage_lock_is_not_running(tmp_path: Path) -> None:
    lock = tmp_path / "daemon.lock"
    lock.write_text("not-a-pid")
    assert _external_daemon_running(lock) is False


def test_dead_pid_is_not_running(tmp_path: Path) -> None:
    lock = tmp_path / "daemon.lock"
    # A PID that is essentially certainly not alive.
    lock.write_text("2147480000")
    assert _external_daemon_running(lock) is False


def test_own_pid_is_not_counted(tmp_path: Path) -> None:
    """The test process itself is never the external daemon."""
    lock = tmp_path / "daemon.lock"
    lock.write_text(str(os.getpid()))
    assert _external_daemon_running(lock) is False


def test_live_external_pid_is_running(tmp_path: Path) -> None:
    proc = subprocess.Popen(["sleep", "30"])
    try:
        lock = tmp_path / "daemon.lock"
        lock.write_text(str(proc.pid))
        assert _external_daemon_running(lock) is True
    finally:
        proc.terminate()
        proc.wait()
