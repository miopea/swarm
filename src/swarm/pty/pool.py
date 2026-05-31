"""ProcessPool — manages connection to the pty-holder and WorkerProcess instances.

The pool is the daemon's single point of contact for all worker process
operations. It connects to the holder sidecar, spawns workers, and
routes output from the holder to the appropriate WorkerProcess instances.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from swarm.logging import get_logger
from swarm.pty.holder import (
    DEFAULT_SOCKET_PATH,
    holder_current_source_hash,
    start_holder_daemon,
)
from swarm.pty.process import ProcessError, WorkerProcess

_log = get_logger("pty.pool")

_CONNECT_TIMEOUT = 5.0
_HOLDER_START_TIMEOUT = 5.0
_HOLDER_SOCKET_CHECK_DELAY = 0.1  # seconds between socket existence checks
_STREAM_READER_LIMIT = 2 * 1024 * 1024  # 2MB — covers max snapshot (1MB raw → ~1.4MB base64)


class ProcessPool:
    """Manages the connection to the pty-holder and all WorkerProcess instances."""

    def __init__(self, socket_path: str | Path | None = None) -> None:
        self.socket_path = Path(socket_path) if socket_path else DEFAULT_SOCKET_PATH
        self._workers: dict[str, WorkerProcess] = {}
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._connected = False
        # Pending command responses: maps a counter to a future
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._cmd_counter = 0
        self._cmd_lock = asyncio.Lock()
        # Output messages that arrived from the holder for workers the pool
        # hasn't discovered yet. During the post-restart discovery window the
        # read loop is live (processing command responses) but _workers is
        # still being populated one worker at a time; without this buffer,
        # any PTY output that crossed the socket in that window would be
        # dropped silently — producing the "terminal shows stale snapshot,
        # needs a second reload" reload-race bug. See ``discover`` for how
        # this buffer is drained relative to the holder snapshot.
        self._pending_output: dict[str, list[bytes]] = {}
        # Holder version skew: filled on connect by _check_holder_version. The
        # daemon surfaces this via /api/health so the dashboard can warn the
        # operator when holder.py changes need an explicit holder bounce
        # (Reload only restarts the daemon — it can't re-exec the holder).
        self.holder_drift: dict[str, Any] = {
            "checked": False,
            "drift": False,
            "holder_hash": "",
            "daemon_hash": "",
            "holder_pid": 0,
            "unknown": False,
        }

    @property
    def is_connected(self) -> bool:
        """Whether the pool has an active connection to the pty-holder."""
        return self._connected

    async def connect(self) -> None:
        """Connect to the pty-holder's Unix socket."""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path), limit=_STREAM_READER_LIMIT),
                timeout=_CONNECT_TIMEOUT,
            )
        except (TimeoutError, ConnectionRefusedError, FileNotFoundError) as e:
            raise ProcessError(f"Cannot connect to holder at {self.socket_path}: {e}") from e

        self._connected = True
        self._read_task = asyncio.create_task(self._read_loop())
        # Update _send_cmd on all existing processes after reconnect
        for proc in self._workers.values():
            proc.bind_send_cmd(self._send_cmd)
        _log.warning(
            "[term-trace] connected to holder at %s — existing workers: %s",
            self.socket_path,
            list(self._workers.keys()),
        )

    async def ensure_holder(self) -> None:
        """Start the holder if not running, then connect."""
        if self._connected:
            if await self._ping():
                return
            self._connected = False

        # Try connecting to existing holder
        if self.socket_path.exists():
            if await self._try_connect():
                return

        # Start a new holder and wait for it
        _log.info("starting holder daemon")
        start_holder_daemon(self.socket_path)

        for _ in range(int(_HOLDER_START_TIMEOUT / _HOLDER_SOCKET_CHECK_DELAY)):
            await asyncio.sleep(_HOLDER_SOCKET_CHECK_DELAY)
            if self.socket_path.exists() and await self._try_connect():
                return

        raise ProcessError("Failed to start holder daemon")

    async def _ping(self) -> bool:
        """Send a ping and return True if holder responds."""
        try:
            resp = await self._send_cmd({"cmd": "ping"})
            return bool(resp.get("pong"))
        except (ProcessError, OSError):
            return False

    async def _check_holder_version(self) -> None:
        """Detect bytecode skew between the running holder and holder.py.

        Because the holder is a double-forked persistent sidecar, a daemon
        reload (os.execv) can't refresh its bytecode — only an explicit
        holder bounce does. Fixes like the 2026-04-21 _MAX_WRITE_BUFFER
        raise (commit 0df45be) silently fail to take effect until the
        operator knows to kill + respawn. This check makes that invisible
        failure loud: on every successful connect, compare the holder's
        import-time source hash to ``holder.py`` on disk. Drift → log a
        loud warning with the exact kill instructions; dashboard reads
        ``holder_drift`` via /api/health.

        Failures (old holder that doesn't know the ``version`` cmd, IO
        errors, missing file) mark ``unknown=True`` without asserting
        drift, so the check itself never breaks the connection.
        """
        daemon_hash = holder_current_source_hash()
        try:
            resp = await self._send_cmd({"cmd": "version"})
        except (ProcessError, OSError):
            self.holder_drift = {
                "checked": True,
                "drift": False,
                "holder_hash": "",
                "daemon_hash": daemon_hash,
                "holder_pid": 0,
                "unknown": True,
            }
            return
        holder_hash = str(resp.get("source_hash") or "")
        try:
            holder_pid = int(resp.get("pid") or 0)
        except (TypeError, ValueError):
            holder_pid = 0
        drift = bool(daemon_hash) and bool(holder_hash) and holder_hash != daemon_hash
        self.holder_drift = {
            "checked": True,
            "drift": drift,
            "holder_hash": holder_hash,
            "daemon_hash": daemon_hash,
            "holder_pid": holder_pid,
            "unknown": not holder_hash,
        }
        if drift:
            pid_path = self.socket_path.with_suffix(".pid")
            _log.warning(
                "[holder-drift] running holder (pid=%s) is older than "
                "holder.py on disk. Reload (os.execv) WILL NOT pick this up — "
                "kill %s && rm -f %s %s && restart swarm to adopt the new code.",
                holder_pid,
                holder_pid or "<pid>",
                self.socket_path,
                pid_path,
            )

    async def _try_connect(self) -> bool:
        """Try to connect and ping the holder. Returns True on success."""
        try:
            await self.connect()
            if await self._ping():
                await self._check_holder_version()
                return True
            # Ping failed — clean up the connection we just opened
            await self._disconnect()
        except (ProcessError, OSError):
            _log.info("stale holder socket — starting fresh")
            await self._disconnect()
        self._connected = False
        return False

    async def spawn(
        self,
        name: str,
        cwd: str,
        command: list[str] | None = None,
        cols: int = 200,
        rows: int = 50,
        shell_wrap: bool = False,
    ) -> WorkerProcess:
        """Spawn a new worker via the holder."""
        if not self._connected:
            raise ProcessError("Not connected to holder")

        resp = await self._send_cmd(
            {
                "cmd": "spawn",
                "name": name,
                "cwd": cwd,
                "command": command,
                "cols": cols,
                "rows": rows,
                "shell_wrap": shell_wrap,
            }
        )
        if not resp.get("ok"):
            raise ProcessError(f"Spawn failed: {resp.get('error', 'unknown')}")

        pid = resp.get("pid")
        if pid is None:
            raise ProcessError("Spawn response missing 'pid'")

        proc = WorkerProcess(name=name, cwd=cwd, cols=cols, rows=rows)
        proc.pid = pid
        proc.is_alive = True
        proc.bind_send_cmd(self._send_cmd)
        self._workers[name] = proc
        _log.info("spawned worker %s (pid=%d)", name, proc.pid)
        return proc

    def get(self, name: str) -> WorkerProcess | None:
        """Get a worker by name."""
        return self._workers.get(name)

    def get_all(self) -> list[WorkerProcess]:
        """Get all worker processes."""
        return list(self._workers.values())

    async def kill(self, name: str) -> None:
        """Kill a worker and remove it from the pool."""
        proc = self._workers.get(name)
        if proc:
            await proc.kill()
            del self._workers[name]

    async def kill_all(self) -> None:
        """Kill all workers."""
        for name in list(self._workers):
            await self.kill(name)

    async def revive(
        self,
        name: str,
        cwd: str | None = None,
        command: list[str] | None = None,
        shell_wrap: bool = False,
    ) -> WorkerProcess | None:
        """Revive a dead worker by killing the old one and respawning."""
        old = self._workers.get(name)
        if old:
            cwd = cwd or old.cwd
            await self.kill(name)
        if not cwd:
            return None
        # Spawn fresh — caller provides the provider-specific command
        return await self.spawn(name, cwd, command=command, shell_wrap=shell_wrap)

    async def discover(self) -> list[WorkerProcess]:
        """Reconnect to existing workers in the holder.

        Used after daemon restart to resume tracking existing workers.
        """
        if not self._connected:
            raise ProcessError("Not connected to holder")

        resp = await self._send_cmd({"cmd": "list"})
        workers_data = resp.get("workers", [])

        for w in workers_data:
            name = w.get("name")
            pid = w.get("pid")
            if not name or pid is None:
                _log.warning("skipping malformed worker entry from holder: %s", w)
                continue
            if name in self._workers:
                existing = self._workers[name]
                if existing.pid != pid:
                    # PID changed (holder restarted) — replace the stale process
                    _log.warning(
                        "worker %s PID changed %s → %s, replacing process",
                        name,
                        existing.pid,
                        pid,
                    )
                else:
                    # Same PID — update liveness in place
                    existing.is_alive = w.get("alive", False)
                    existing.exit_code = w.get("exit_code")
                    continue
            # Create WorkerProcess for existing holder worker
            proc = WorkerProcess(
                name=name,
                cwd=w.get("cwd", "/tmp"),
                cols=int(w.get("cols", 200)),
                rows=int(w.get("rows", 50)),
            )
            proc.pid = pid
            proc.is_alive = w.get("alive", False)
            proc.exit_code = w.get("exit_code")
            proc.bind_send_cmd(self._send_cmd)

            # Fetch the holder's buffer snapshot.  The holder processes
            # commands and output broadcasts serially on a single socket,
            # and our read loop dispatches messages in arrival order.  That
            # means any "output" messages for this worker that the read
            # loop has already pushed into ``_pending_output`` BEFORE this
            # await resolves were emitted by the holder before it produced
            # the snapshot response — so they're already inside the
            # snapshot bytes and replaying them would duplicate output.
            # Anything that arrives AFTER the await returns is
            # post-snapshot and has to be applied by hand.
            snap_resp = await self._send_cmd({"cmd": "snapshot", "name": name})
            # Discard the pre-snapshot pending chunks — they're in the snapshot.
            pre_snapshot_dropped = len(self._pending_output.pop(name, []))
            if snap_resp.get("ok"):
                try:
                    data = base64.b64decode(snap_resp["data"])
                    proc.buffer.write(data)
                except Exception:
                    _log.warning("failed to decode snapshot for %s", name, exc_info=True)

            # Register proc BEFORE yielding so any subsequent output message
            # routes directly to proc.feed_output instead of going back into
            # _pending_output.
            self._workers[name] = proc
            if pre_snapshot_dropped:
                _log.info(
                    "discovered worker %s (pid=%d, alive=%s) — "
                    "dropped %d pre-snapshot output chunk(s) already in snapshot",
                    name,
                    proc.pid,
                    proc.is_alive,
                    pre_snapshot_dropped,
                )
            else:
                _log.info("discovered worker %s (pid=%d, alive=%s)", name, proc.pid, proc.is_alive)

        # Final sweep: any worker that still has pending output here was
        # seen by the holder broadcast but not returned by the `list`
        # command (possible race if a worker spawned between `list` and
        # the current moment). Preserve the buffer — it'll be drained
        # when discover() is called again or dropped if the worker
        # really never appears.
        leftover = {k: len(v) for k, v in self._pending_output.items() if v}
        if leftover:
            _log.warning(
                "post-discover: pending output remains for %s — retained for next discover()",
                leftover,
            )

        return list(self._workers.values())

    async def disconnect(self) -> None:
        """Public disconnect — close the socket connection to the holder."""
        await self._disconnect()

    async def shutdown_holder(self) -> None:
        """Tell the holder to shut down (kills all workers)."""
        if self._connected:
            try:
                await self._send_cmd({"cmd": "shutdown"})
            except (ProcessError, OSError):
                pass
        self._workers.clear()
        await self._disconnect()

    async def restart_holder_in_place(self, *, reconnect_timeout: float = 10.0) -> bool:
        """Ask the holder to re-exec itself in place, preserving worker FDs.

        Used to deploy holder code changes (e.g., the 8 MB write-buffer
        threshold from 2026-04-21) without taking down the worker child
        processes. The holder serializes its worker registry + ring
        buffers to a handoff file, clears ``FD_CLOEXEC`` on each PTY
        master, and ``os.execv``s into a fresh ``swarm.pty.holder``
        invocation that reads the handoff and resumes.

        Workflow on the daemon side:
          1. Send ``restart_in_place`` — the response NEVER arrives
             because the holder execs before flushing it. We treat a
             timeout/disconnect during this command as success.
          2. Wait for the socket to come back (the new holder rebinds).
          3. Reconnect and re-discover.

        Returns ``True`` if reconnect succeeded, ``False`` otherwise.
        Caller should still treat ``False`` as "needs operator action"
        — the holder may or may not be running, and worker liveness
        depends on whether the execv succeeded.
        """
        import asyncio
        import time as _time

        if not self._connected:
            try:
                await self.connect()
            except ProcessError:
                _log.warning("restart_holder_in_place: not connected and reconnect failed")
                return False

        _log.warning("restart_holder_in_place: sending command")
        # The command never returns because the holder execs before
        # flushing the response. Catch the timeout/disconnect that
        # follows and treat them as expected.
        try:
            await asyncio.wait_for(
                self._send_cmd({"cmd": "restart_in_place"}),
                timeout=2.0,
            )
        except (TimeoutError, ProcessError, OSError):
            pass

        # Drop our side of the socket — the new holder needs a clean slate.
        try:
            await self._disconnect()
        except Exception:
            _log.debug("disconnect during restart_holder_in_place", exc_info=True)

        # Poll until the new holder rebinds the socket. The handoff is
        # synchronous-ish: write state file → execv → new process binds.
        # Empirically <500ms but we give it 10s before declaring failure.
        deadline = _time.monotonic() + reconnect_timeout
        last_err: Exception | None = None
        while _time.monotonic() < deadline:
            try:
                await self.connect()
                _log.warning("restart_holder_in_place: reconnected to new holder")
                return True
            except (ProcessError, OSError) as exc:
                last_err = exc
                await asyncio.sleep(0.2)
        _log.error(
            "restart_holder_in_place: reconnect timed out after %.1fs (last error: %s)",
            reconnect_timeout,
            last_err,
        )
        return False

    async def _disconnect(self) -> None:
        """Close the socket connection.

        Acquires ``_cmd_lock`` before failing pending futures so that a
        concurrent ``_send_cmd`` (between ``drain()`` and ``wait_for()``)
        cannot see a cleared ``_pending`` dict.
        """
        self._connected = False
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            self._read_task = None
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                _log.debug("Error closing pool writer", exc_info=True)
            self._writer = None
        self._reader = None
        # Fail all pending futures under lock to avoid racing with _send_cmd
        async with self._cmd_lock:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ProcessError("Disconnected"))
            self._pending.clear()

    async def _send_cmd(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Send a command and wait for the response.

        Uses a lock to serialize the write + future registration, but
        releases before drain() to avoid blocking other callers during
        slow socket writes.
        """
        if not self._writer or not self._connected:
            raise ProcessError("Not connected to holder")

        async with self._cmd_lock:
            self._cmd_counter += 1
            cmd_id = self._cmd_counter
            fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
            self._pending[cmd_id] = fut

            msg_with_id = {**msg, "id": cmd_id}
            self._writer.write(json.dumps(msg_with_id).encode() + b"\n")

        # Drain outside the lock — other commands can queue while we wait
        try:
            await self._writer.drain()
        except (ConnectionError, OSError):
            self._pending.pop(cmd_id, None)
            raise ProcessError("Connection lost during drain")

        try:
            return await asyncio.wait_for(fut, timeout=10.0)
        except TimeoutError:
            raise ProcessError(f"Command timed out: {msg.get('cmd')}")
        finally:
            self._pending.pop(cmd_id, None)

    def _dispatch_message(self, msg: dict[str, Any]) -> None:
        """Route a single holder message to the appropriate handler."""
        if "output" in msg:
            name = msg["output"]
            proc = self._workers.get(name)
            try:
                data = base64.b64decode(msg.get("data", ""))
            except Exception:
                _log.warning("failed to decode output for %s", name, exc_info=True)
                return
            if proc:
                proc.feed_output(data)
            else:
                # Worker not yet discovered — buffer the bytes so discover()
                # can decide what to do with them relative to the snapshot.
                self._pending_output.setdefault(name, []).append(data)
        elif "died" in msg:
            name = msg["died"]
            proc = self._workers.get(name)
            if proc:
                proc.is_alive = False
                proc.exit_code = msg.get("exit_code")
                proc.cleanup_ws()
                _log.info("worker %s died (exit_code=%s)", name, msg.get("exit_code"))
        else:
            cmd_id = msg.get("id")
            if cmd_id is not None and cmd_id in self._pending:
                fut = self._pending.pop(cmd_id)
                if not fut.done():
                    fut.set_result(msg)
            else:
                _log.warning("unmatched holder message (no pending id=%s)", cmd_id)

    async def _read_loop(self) -> None:
        """Read messages from the holder, dispatching responses and output."""
        consecutive_bad = 0
        try:
            while self._connected and self._reader:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    consecutive_bad += 1
                    _log.warning(
                        "corrupt JSON from holder (%d consecutive): %s",
                        consecutive_bad,
                        line[:200],
                    )
                    if consecutive_bad >= 5:
                        _log.warning("5+ consecutive bad messages — reconnecting")
                        break
                    continue
                consecutive_bad = 0
                self._dispatch_message(msg)
        except (asyncio.CancelledError, ConnectionError, OSError, ValueError):
            pass
        finally:
            self._connected = False
