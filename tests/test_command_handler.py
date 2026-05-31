"""Unit tests for PtyCommandHandler — the holder's JSON command dispatcher.

Previously exercised only indirectly through the socket protocol in
test_holder.py. These test the dispatch + per-command validation in
isolation with a fake holder (no real PTYs / sockets / forks).

``shutdown`` and ``restart_in_place`` are intentionally NOT dispatched here:
the former tears the holder down and the latter calls ``os.execv``.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from swarm.pty.command_handler import PtyCommandHandler


@pytest.fixture
def holder():
    h = MagicMock()
    h.workers = {}
    return h


@pytest.fixture
def handler(holder):
    return PtyCommandHandler(holder)


class TestDispatch:
    def test_unknown_command(self, handler):
        resp = handler.dispatch({"cmd": "frobnicate"})
        assert resp["ok"] is False
        assert "unknown command" in resp["error"]

    def test_missing_cmd_key(self, handler):
        resp = handler.dispatch({})
        assert resp["ok"] is False

    def test_all_registered_commands_route(self, handler):
        # Every key resolves to a handler (none falls through to "unknown").
        # We don't invoke them here — just assert the registry is wired.
        expected = {
            "ping",
            "version",
            "spawn",
            "list",
            "write",
            "signal",
            "resize",
            "kill",
            "snapshot",
            "shutdown",
            "restart_in_place",
        }
        assert expected <= set(PtyCommandHandler._CMD_HANDLERS)


class TestPingVersion:
    def test_ping(self, handler):
        assert handler.dispatch({"cmd": "ping"}) == {"pong": True}

    def test_version(self, handler):
        resp = handler.dispatch({"cmd": "version"})
        assert resp["ok"] is True
        assert isinstance(resp["source_hash"], str)
        assert isinstance(resp["pid"], int)


class TestSpawn:
    def test_rejects_invalid_name(self, handler):
        resp = handler.dispatch({"cmd": "spawn", "name": "bad name!", "cwd": "/tmp"})
        assert resp["ok"] is False
        assert "invalid worker name" in resp["error"]

    def test_rejects_relative_cwd(self, handler):
        resp = handler.dispatch({"cmd": "spawn", "name": "api", "cwd": "relative/path"})
        assert resp["ok"] is False
        assert "absolute" in resp["error"]

    def test_rejects_invalid_dimensions(self, handler):
        resp = handler.dispatch({"cmd": "spawn", "name": "api", "cwd": "/tmp", "cols": "wide"})
        assert resp["ok"] is False
        assert "cols/rows" in resp["error"]

    def test_success_calls_holder(self, handler, holder):
        holder.spawn_worker.return_value = SimpleNamespace(name="api", pid=4321)
        resp = handler.dispatch({"cmd": "spawn", "name": "api", "cwd": "/tmp"})
        assert resp == {"ok": True, "name": "api", "pid": 4321}
        holder.spawn_worker.assert_called_once()


class TestWrite:
    def test_rejects_invalid_base64(self, handler):
        resp = handler.dispatch({"cmd": "write", "name": "api", "data": "!!!not base64!!!"})
        assert resp["ok"] is False
        assert "base64" in resp["error"]

    def test_success_decodes_and_writes(self, handler, holder):
        holder.write_to_worker.return_value = True
        payload = base64.b64encode(b"echo hi\n").decode()
        resp = handler.dispatch({"cmd": "write", "name": "api", "data": payload})
        assert resp == {"ok": True}
        holder.write_to_worker.assert_called_once_with("api", b"echo hi\n")


class TestSignal:
    def test_rejects_disallowed_signal(self, handler):
        resp = handler.dispatch({"cmd": "signal", "name": "api", "sig": "SIGSEGV"})
        assert resp["ok"] is False
        assert "not allowed" in resp["error"]

    def test_allowed_signal_calls_holder(self, handler, holder):
        holder.signal_worker.return_value = True
        resp = handler.dispatch({"cmd": "signal", "name": "api", "sig": "SIGINT"})
        assert resp == {"ok": True}
        holder.signal_worker.assert_called_once()


class TestResize:
    def test_rejects_invalid_dimensions(self, handler):
        resp = handler.dispatch({"cmd": "resize", "name": "api", "rows": "tall"})
        assert resp["ok"] is False
        assert "cols/rows" in resp["error"]

    def test_success_calls_holder(self, handler, holder):
        holder.resize_worker.return_value = True
        resp = handler.dispatch({"cmd": "resize", "name": "api", "cols": 100, "rows": 40})
        assert resp == {"ok": True}
        holder.resize_worker.assert_called_once_with("api", 100, 40)


class TestKillSnapshot:
    def test_kill_calls_holder(self, handler, holder):
        holder.kill_worker.return_value = True
        assert handler.dispatch({"cmd": "kill", "name": "api"}) == {"ok": True}
        holder.kill_worker.assert_called_once_with("api")

    def test_snapshot_worker_not_found(self, handler):
        resp = handler.dispatch({"cmd": "snapshot", "name": "ghost"})
        assert resp["ok"] is False
        assert "not found" in resp["error"]

    def test_snapshot_success_returns_base64(self, handler, holder):
        fake_buffer = SimpleNamespace(snapshot=lambda: b"screen")
        holder.workers = {"api": SimpleNamespace(buffer=fake_buffer)}
        resp = handler.dispatch({"cmd": "snapshot", "name": "api"})
        assert resp["ok"] is True
        assert base64.b64decode(resp["data"]) == b"screen"
