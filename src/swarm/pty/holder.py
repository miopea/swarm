"""PTY Holder Sidecar — owns PTY master FDs so workers survive daemon restarts.

Architecture::

    swarm daemon  <-->  Unix socket  <-->  pty-holder  <-->  PTY FDs  <-->  claude processes

The holder is a standalone process (double-forked daemon) that:
- Creates PTYs and forks child processes on request
- Holds PTY master FDs open (workers survive daemon death)
- Reads from each PTY master, streams output over the socket
- Accepts input commands from the daemon (write, resize, signal, kill)
- Buffers output while daemon is disconnected (ring buffer persists)

Protocol: JSON lines over Unix domain socket.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import fcntl
import functools
import hashlib
import json
import os
import shlex
import signal
import struct
import sys
import termios
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from swarm.logging import get_logger
from swarm.pty.buffer import RingBuffer

_log = get_logger("pty.holder")

_SWARM_DIR = Path.home() / ".swarm"
DEFAULT_SOCKET_PATH = _SWARM_DIR / "holder.sock"
DEFAULT_PID_PATH = _SWARM_DIR / "holder.pid"


# Source hash of holder.py captured at module import time. The holder is a
# double-forked persistent sidecar — daemon reloads (os.execv) replace the
# daemon but leave the holder running with whatever bytecode it was spawned
# with. When ``holder.py`` changes on disk, the running holder keeps serving
# the old behavior forever unless explicitly bounced. This hash + the
# ``version`` command let the daemon detect that skew and warn the operator
# on reconnect. See ``pool.ProcessPool._check_holder_version``.
def _hash_source(path: Path) -> str:
    """Return sha256 of *path*, or empty string if unreadable."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


_SOURCE_PATH: Path = Path(__file__).resolve()
_SOURCE_HASH_AT_IMPORT: str = _hash_source(_SOURCE_PATH)


def holder_source_hash_at_import() -> str:
    """Return the sha256 of ``holder.py`` captured at module import time.

    This is what a running holder process will keep returning from the
    ``version`` command for its entire lifetime, regardless of subsequent
    edits to the source file on disk.
    """
    return _SOURCE_HASH_AT_IMPORT


def holder_current_source_hash() -> str:
    """Return the sha256 of ``holder.py`` as it sits on disk right now.

    The daemon process (which may be newer than the holder) calls this to
    learn what the holder *should* be running; it then compares against
    the value the live holder reports via the ``version`` command.
    """
    return _hash_source(_SOURCE_PATH)


_READ_SIZE = 4096
_DEFAULT_COLS = 200
_DEFAULT_ROWS = 50
_REAP_INTERVAL = 1.0  # seconds between child-reap sweeps
# Drop threshold for per-client pending writes. Bumped from 1 MB on
# 2026-04-21 after tracing why every daemon reload wedged the dashboard:
#
#   1. Daemon reloads and re-connects to the holder.
#   2. ``ProcessPool.discover()`` fires ``_send_cmd("snapshot", worker=X)``.
#   3. Holder writes the ~1.3 MB reply (1 MB raw ring buffer × ~1.33
#      base64 overhead) into the client's socket buffer.
#   4. While the reply is still draining, a PTY readable event fires
#      ``_broadcast``, which writes more bytes to the SAME pending buffer.
#   5. ``get_write_buffer_size()`` returns ~1.18 MB, exceeds the old 1 MB
#      threshold, and the holder drops the daemon as a "slow client".
#   6. Dashboard shows frozen terminals — output stopped flowing.
#
# The symptom matched the user's long-standing "terminal locks after
# reload, needs 2-3 reloads" pattern. 8 MB gives ~6x headroom over a
# single snapshot reply while still catching truly dead clients (an
# 8 MB backlog is tens of seconds of data at typical PTY output rates).
_MAX_WRITE_BUFFER = 8 * 1024 * 1024  # 8 MB
_KILL_GRACE_SECONDS = 0.5  # SIGTERM→SIGKILL grace period

# Path used by ``restart_in_place`` to hand off worker state from the
# departing holder to the new one. Lives next to the socket so a tenant
# with a custom socket directory keeps it together.
DEFAULT_HANDOFF_PATH = _SWARM_DIR / "holder-handoff.json"


def _make_fd_inheritable(fd: int) -> None:
    """Clear ``FD_CLOEXEC`` on *fd* so it survives ``os.execv``.

    Python sets ``FD_CLOEXEC`` by default (PEP 446) on all FDs from
    ``os.openpty``, which closes them across exec. The graceful holder
    restart path needs PTY masters to survive the handoff so the worker
    child processes (Claude Code sessions) keep their slave end open.
    """
    flags = fcntl.fcntl(fd, fcntl.F_GETFD)
    fcntl.fcntl(fd, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)


class HolderError(Exception):
    """Raised when a holder operation fails."""


@dataclass
class HeldWorker:
    """A worker process owned by the holder."""

    name: str
    pid: int
    master_fd: int
    cwd: str
    command: list[str]
    cols: int = _DEFAULT_COLS
    rows: int = _DEFAULT_ROWS
    buffer: RingBuffer = field(default_factory=RingBuffer, repr=False)
    exit_code: int | None = None
    _reaped: bool = False

    @property
    def alive(self) -> bool:
        """Check whether the child process is still running.

        Idempotent: once the child has been reaped (via ``waitpid``),
        subsequent calls return ``False`` immediately without calling
        ``waitpid`` again.  This prevents a TOCTOU race where two
        concurrent callers both reach ``waitpid`` before either sets
        ``exit_code``, causing the second to see ``(0, 0)`` and
        incorrectly return ``True``.
        """
        if self._reaped:
            return False
        if self.exit_code is not None:
            return False
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid != 0:
                self.exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
                self._reaped = True
                return False
        except ChildProcessError:
            self.exit_code = -1
            self._reaped = True
            return False
        return True


@functools.lru_cache(maxsize=1)
def _resolve_user_path() -> str:
    """Build a PATH that includes common tool manager bin dirs.

    The holder is double-forked and may not inherit the user's full
    interactive-shell PATH (nvm, cargo, etc.).  Scan for well-known
    locations and prepend any that exist.

    Result is cached — the filesystem scan only runs once per process.
    """
    home = Path.home()
    extra_dirs: list[str] = []

    # nvm — pick the highest installed node version
    nvm_dir = home / ".nvm" / "versions" / "node"
    if nvm_dir.is_dir():
        versions = sorted(nvm_dir.iterdir(), reverse=True)
        for v in versions:
            bin_dir = v / "bin"
            if bin_dir.is_dir():
                extra_dirs.append(str(bin_dir))
                break

    # Other common tool managers
    for candidate in [
        home / ".cargo" / "bin",
        home / ".local" / "bin",
        home / ".deno" / "bin",
        Path("/usr/local/bin"),
    ]:
        if candidate.is_dir():
            extra_dirs.append(str(candidate))

    current = os.environ.get("PATH", "")
    current_set = set(current.split(":"))
    new_parts = [d for d in extra_dirs if d not in current_set]
    if new_parts:
        return ":".join(new_parts) + ":" + current
    return current


def _set_pty_size(fd: int, rows: int, cols: int) -> None:
    """Set the window size on a PTY file descriptor."""
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _make_nonblocking(fd: int) -> None:
    """Set a file descriptor to non-blocking mode."""
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


class PtyHolder:
    """Holds PTY master FDs for worker processes.

    Designed to run as a standalone sidecar process. The daemon connects
    over a Unix domain socket and issues commands.
    """

    def __init__(
        self,
        socket_path: str | Path | None = None,
        max_workers: int = 20,
    ) -> None:
        self.socket_path = Path(socket_path) if socket_path else DEFAULT_SOCKET_PATH
        self.max_workers = max_workers
        self.workers: dict[str, HeldWorker] = {}
        self._server: asyncio.AbstractServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: set[asyncio.StreamWriter] = set()
        self._running = False
        # Command-routing dispatch lives in PtyCommandHandler (task #516).
        # PtyHolder retains only PTY lifecycle concerns; the dispatcher
        # routes wire-protocol messages back through ``self.holder.*``.
        from swarm.pty.command_handler import PtyCommandHandler

        self._cmds = PtyCommandHandler(self)

    def _check_capacity(self, name: str) -> None:
        """Raise if the holder is at max capacity for new workers."""
        alive_count = sum(1 for w in self.workers.values() if w.alive)
        if alive_count >= self.max_workers and name not in self.workers:
            raise HolderError(
                f"Max workers limit ({self.max_workers}) reached — cannot spawn '{name}'"
            )

    def spawn_worker(
        self,
        name: str,
        cwd: str,
        command: list[str] | None = None,
        cols: int = _DEFAULT_COLS,
        rows: int = _DEFAULT_ROWS,
        shell_wrap: bool = False,
    ) -> HeldWorker:
        """Create a PTY, fork a child process, and register the worker.

        This is synchronous — called from within the holder's event loop
        via a command handler, but the actual fork/pty work is synchronous.
        """
        self._check_capacity(name)

        if name in self.workers:
            old = self.workers[name]
            if old.alive:
                raise HolderError(f"Worker '{name}' already exists and is alive")
            # Clean up dead worker
            self._cleanup_worker(name)

        if not command:
            from swarm.providers import get_provider

            command = get_provider().worker_command()
        master_fd, slave_fd = os.openpty()
        try:
            _set_pty_size(slave_fd, rows, cols)
            pid = os.fork()
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        if pid == 0:
            # Child process
            try:
                os.close(master_fd)
                os.setsid()
                # Set slave as controlling terminal
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
                os.dup2(slave_fd, 0)
                os.dup2(slave_fd, 1)
                os.dup2(slave_fd, 2)
                if slave_fd > 2:
                    os.close(slave_fd)
                os.chdir(cwd)
                env = os.environ.copy()
                env["TERM"] = "xterm-256color"
                env["PATH"] = _resolve_user_path()
                env["SWARM_MANAGED"] = "1"
                env["SWARM_WORKER_NAME"] = name
                # Claude Code defaults to the alternate screen buffer, which
                # xterm.js renders without scrollback. Disable it so output
                # flows into the main buffer (and into xterm.js's 5000-line
                # scrollback). Upstream: anthropics/claude-code#42670.
                if command and command[0] == "claude":
                    env["CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN"] = "1"
                if shell_wrap:
                    # Wrap CLI tools in a login shell so the user drops
                    # to an interactive prompt when the tool exits (/exit).
                    shell_cmd = " ".join(shlex.quote(c) for c in command)
                    os.execvpe(
                        "bash",
                        ["bash", "--login", "-c", f"{shell_cmd}; exec bash --login"],
                        env,
                    )
                else:
                    os.execvpe(command[0], command, env)
            except Exception:
                import traceback

                traceback.print_exc()
                os._exit(1)
        else:
            # Parent
            os.close(slave_fd)
            _make_nonblocking(master_fd)

            worker = HeldWorker(
                name=name,
                pid=pid,
                master_fd=master_fd,
                cwd=cwd,
                command=command,
                cols=cols,
                rows=rows,
            )
            self.workers[name] = worker

            # Register reader for this worker's PTY output
            if self._loop:
                self._loop.add_reader(master_fd, self._on_pty_readable, name)

            _log.info("spawned worker %s: pid=%d, fd=%d", name, pid, master_fd)
            return worker

    def _on_pty_readable(self, name: str) -> None:
        """Called when a worker's PTY master has data."""
        worker = self.workers.get(name)
        if not worker:
            return
        try:
            data = os.read(worker.master_fd, _READ_SIZE)
        except OSError as e:
            if e.errno in (errno.EIO, errno.EBADF):
                # PTY closed — child exited.  Broadcast death and clean up
                # the FD + reader but keep the worker in the dict so pool
                # can discover it as dead (needed for reconnect after restart).
                worker.alive  # triggers waitpid to reap zombie
                self._broadcast_death(name, worker.exit_code)
                self._release_fd(worker)
                return
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                # Spurious readable wakeup — nothing to read yet. Retry on the
                # next callback; raising here would re-fire the still-registered
                # reader in a tight loop.
                return
            # Unexpected read error: stop this reader to avoid that tight
            # re-fire loop, log it, and leave the worker for higher layers to
            # reconcile. Mirrors the EOF path below (remove reader, no close).
            _log.warning("unexpected read error on worker %s: %s", name, e, exc_info=True)
            if self._loop:
                try:
                    self._loop.remove_reader(worker.master_fd)
                except (ValueError, OSError):
                    pass
            return
        if not data:
            # EOF — remove reader but leave worker in dict for kill_worker
            if self._loop:
                try:
                    self._loop.remove_reader(worker.master_fd)
                except (ValueError, OSError):
                    pass
            return
        worker.buffer.write(data)
        # Stream to connected clients
        self._broadcast_output(name, data)

    def _broadcast(self, encoded: bytes) -> None:
        """Send encoded message to all connected clients.

        Drops clients that have disconnected or whose write buffer
        exceeds ``_MAX_WRITE_BUFFER`` (backpressure).

        Iterates a snapshot (``set(self._clients)``) so concurrent
        ``_handle_client`` disconnects cannot corrupt iteration.
        Uses ``set.discard`` for idempotent removal.
        """
        dead: list[asyncio.StreamWriter] = []
        for writer in set(self._clients):
            try:
                transport = writer.transport
                if transport is None:
                    dead.append(writer)
                    continue
                buf_size = transport.get_write_buffer_size()
                if buf_size > _MAX_WRITE_BUFFER:
                    _log.warning("dropping slow client (buffer %d bytes)", buf_size)
                    dead.append(writer)
                    continue
                if buf_size > _MAX_WRITE_BUFFER // 2:
                    _log.info("client write buffer elevated (%d bytes)", buf_size)
                writer.write(encoded)
            except (ConnectionError, OSError, AttributeError):
                dead.append(writer)
        if dead:
            self._clients -= set(dead)
            _log.debug(
                "removed %d dead client(s) during broadcast (%d remaining)",
                len(dead),
                len(self._clients),
            )

    def _broadcast_output(self, name: str, data: bytes) -> None:
        """Send output data to all connected daemon clients."""
        msg = (
            json.dumps(
                {
                    "output": name,
                    "data": base64.b64encode(data).decode(),
                }
            )
            + "\n"
        )
        self._broadcast(msg.encode())

    def _broadcast_death(self, name: str, exit_code: int | None) -> None:
        """Notify connected clients that a worker process has died."""
        msg = json.dumps({"died": name, "exit_code": exit_code}) + "\n"
        self._broadcast(msg.encode())

    def _release_fd(self, worker: HeldWorker) -> None:
        """Remove the reader and close the master FD without removing the worker."""
        if self._loop:
            try:
                self._loop.remove_reader(worker.master_fd)
            except (ValueError, OSError):
                pass
        try:
            os.close(worker.master_fd)
        except OSError:
            pass

    def _cleanup_worker(self, name: str) -> None:
        """Clean up a worker's resources and remove it from the dict."""
        worker = self.workers.pop(name, None)
        if not worker:
            return
        self._release_fd(worker)

    def kill_worker(self, name: str) -> bool:
        """Kill a worker process and clean up.

        Sends SIGTERM first, then polls for up to ``_KILL_GRACE_SECONDS``
        to allow graceful shutdown before escalating to SIGKILL.

        Uses blocking ``time.sleep`` intentionally — yielding via
        ``asyncio.sleep`` would let the reap loop broadcast a "died"
        message before the kill response is sent, breaking the protocol.
        The total blocking time is bounded by ``_KILL_GRACE_SECONDS``.
        """
        worker = self.workers.get(name)
        if not worker:
            return False
        if worker.alive:
            try:
                os.killpg(os.getpgid(worker.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            # Give the process a chance to exit gracefully
            deadline = time.monotonic() + _KILL_GRACE_SECONDS
            while time.monotonic() < deadline:
                try:
                    pid, _ = os.waitpid(worker.pid, os.WNOHANG)
                    if pid != 0:
                        break  # Process exited cleanly
                except ChildProcessError:
                    break  # Already reaped
                time.sleep(0.05)
            else:
                # Grace period expired — force kill
                try:
                    os.kill(worker.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    os.waitpid(worker.pid, os.WNOHANG)
                except ChildProcessError:
                    pass
        self._cleanup_worker(name)
        return True

    def write_to_worker(self, name: str, data: bytes) -> bool:
        """Write data to a worker's PTY master, handling short writes.

        Sets the FD non-blocking to avoid stalling the event loop when the
        PTY buffer is full (e.g. during large pastes).  If the PTY cannot
        accept data immediately, returns False instead of blocking.
        """
        worker = self.workers.get(name)
        if not worker or not worker.alive:
            return False
        try:
            fd = worker.master_fd
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            try:
                view = memoryview(data)
                while len(view) > 0:
                    try:
                        written = os.write(fd, view)
                    except BlockingIOError:
                        # PTY buffer full — return False so caller can retry
                        return False
                    if written <= 0:
                        return False
                    view = view[written:]
                return True
            finally:
                fcntl.fcntl(fd, fcntl.F_SETFL, flags)
        except OSError as e:
            # Normal backpressure (PTY buffer full) is handled above via
            # BlockingIOError; reaching here is a genuine write failure.
            _log.warning("write to worker %s failed: %s", name, e, exc_info=True)
            return False

    def resize_worker(self, name: str, cols: int, rows: int) -> bool:
        """Resize a worker's PTY."""
        worker = self.workers.get(name)
        if not worker:
            return False
        try:
            _set_pty_size(worker.master_fd, rows, cols)
            worker.cols = cols
            worker.rows = rows
            if worker.alive:
                os.killpg(os.getpgid(worker.pid), signal.SIGWINCH)
            return True
        except (OSError, ProcessLookupError) as e:
            _log.warning("resize of worker %s failed: %s", name, e, exc_info=True)
            return False

    def signal_worker(self, name: str, sig: int) -> bool:
        """Send a signal to a worker's process group."""
        worker = self.workers.get(name)
        if not worker or not worker.alive:
            return False
        try:
            os.killpg(os.getpgid(worker.pid), sig)
            return True
        except OSError as e:
            _log.warning("signal %d to worker %s failed: %s", sig, name, e, exc_info=True)
            return False

    def list_workers(self) -> list[dict[str, object]]:
        """Return metadata for all held workers."""
        result = []
        for w in self.workers.values():
            result.append(
                {
                    "name": w.name,
                    "pid": w.pid,
                    "alive": w.alive,
                    "exit_code": w.exit_code,
                    "cwd": w.cwd,
                    "command": w.command,
                    "cols": w.cols,
                    "rows": w.rows,
                }
            )
        return result

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a connected daemon client."""
        self._clients.add(writer)
        _log.info("client connected (%d total)", len(self._clients))
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                    response = self._handle_command(msg)
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()
                except json.JSONDecodeError:
                    err = {"error": "invalid JSON"}
                    writer.write(json.dumps(err).encode() + b"\n")
                    await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self._clients.discard(writer)
            _log.info("client disconnected (%d remaining)", len(self._clients))
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                _log.debug("Error closing client writer", exc_info=True)

    def _handle_command(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a command and echo the request's ``id`` in the response."""
        cmd_id = msg.get("id")
        response = self._cmds.dispatch(msg)
        if cmd_id is not None:
            response["id"] = cmd_id
        return response

    def inherit_workers(self, state_path: Path) -> int:
        """Repopulate ``self.workers`` from a handoff state file.

        Called from the ``__main__`` entry point with ``--inherit``.
        The PTY master FDs referenced in the state file must already be
        open in this process (they survived the parent holder's execv
        because FD_CLOEXEC was cleared). Each entry's ``master_fd`` is
        validated with ``fstat`` before reuse so we don't mistakenly
        register a closed or remapped descriptor.

        Returns the count of workers successfully restored. The caller
        is responsible for unlinking *state_path* after restoration.
        """
        if not state_path.exists():
            _log.warning("inherit: state file missing: %s", state_path)
            return 0
        try:
            data = json.loads(state_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("inherit: failed to read state %s: %s", state_path, exc)
            return 0

        restored = 0
        for entry in data.get("workers", []):
            try:
                fd = int(entry["master_fd"])
                # Verify the FD is actually open + a character device (PTY).
                os.fstat(fd)
                w = HeldWorker(
                    name=str(entry["name"]),
                    pid=int(entry["pid"]),
                    master_fd=fd,
                    cwd=str(entry.get("cwd", "/tmp")),
                    command=list(entry.get("command", [])),
                    cols=int(entry.get("cols", _DEFAULT_COLS)),
                    rows=int(entry.get("rows", _DEFAULT_ROWS)),
                )
                buf = base64.b64decode(entry.get("buffer", ""))
                if buf:
                    w.buffer.write(buf)
                self.workers[w.name] = w
                restored += 1
                _log.info(
                    "inherited worker %s (pid=%d, fd=%d, buffer=%d bytes)",
                    w.name,
                    w.pid,
                    w.master_fd,
                    len(buf),
                )
            except (KeyError, ValueError, OSError) as exc:
                _log.warning("inherit: skipping malformed entry %r: %s", entry, exc)
                continue
        _log.warning("inherit: restored %d worker(s) from %s", restored, state_path)
        return restored

    def _shutdown_all(self) -> None:
        """Kill all workers and stop the holder."""
        for name in list(self.workers):
            self.kill_worker(name)
        self._running = False
        if self._server:
            self._server.close()

    def _kill_all_workers(self) -> None:
        """Send SIGTERM to all live worker process groups."""
        for worker in list(self.workers.values()):
            if worker.alive:
                try:
                    os.killpg(os.getpgid(worker.pid), signal.SIGTERM)
                except OSError:
                    pass

    async def serve(self) -> None:
        """Start the Unix socket server and run until stopped."""
        self._loop = asyncio.get_running_loop()
        self._running = True

        # Register signal handlers so worker processes aren't orphaned on kill
        for sig in (signal.SIGTERM, signal.SIGINT):
            self._loop.add_signal_handler(sig, self._handle_shutdown_signal)

        # Ensure socket dir exists
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        # Remove stale socket
        if self.socket_path.exists():
            self.socket_path.unlink()

        # Inherited workers (from --inherit handoff) need their PTY
        # readers re-registered on the new event loop. spawn_worker does
        # this for newly-spawned processes; for inherited ones we wire
        # them up here, after the loop is bound.
        for w in self.workers.values():
            try:
                self._loop.add_reader(w.master_fd, self._on_pty_readable, w.name)
            except OSError as exc:
                _log.warning(
                    "failed to re-register reader for inherited worker %s (fd=%d): %s",
                    w.name,
                    w.master_fd,
                    exc,
                )

        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.socket_path),
        )
        # Restrict socket permissions
        os.chmod(str(self.socket_path), 0o700)

        _log.info("holder listening on %s", self.socket_path)

        try:
            while self._running:
                await asyncio.sleep(_REAP_INTERVAL)
                # Reap dead children
                self._reap_children()
        except asyncio.CancelledError:
            pass
        finally:
            self._kill_all_workers()
            self._server.close()
            await self._server.wait_closed()
            if self.socket_path.exists():
                self.socket_path.unlink()
            _log.info("holder stopped")

    def _handle_shutdown_signal(self) -> None:
        """Handle SIGTERM/SIGINT by stopping the serve loop."""
        _log.info("received shutdown signal, stopping holder")
        self._running = False

    def _reap_children(self) -> None:
        """Check for dead child processes and update exit codes."""
        for worker in list(self.workers.values()):
            if worker.exit_code is None:
                was_alive = worker.alive  # triggers waitpid via property
                if not was_alive and worker.exit_code is not None:
                    self._broadcast_death(worker.name, worker.exit_code)
                    self._release_fd(worker)


def start_holder_daemon(socket_path: str | Path | None = None) -> int:
    """Double-fork to start the holder as a background daemon.

    Returns the holder daemon's PID (read from the PID file).
    """
    socket_path = Path(socket_path) if socket_path else DEFAULT_SOCKET_PATH
    pid_path = socket_path.with_suffix(".pid")

    # First fork
    pid = os.fork()
    if pid > 0:
        # Parent: return immediately — the caller (pool) has its own async
        # wait loop for the socket, so blocking here just stalls the event loop.
        return pid

    # First child: create new session
    os.setsid()

    # Second fork
    pid = os.fork()
    if pid > 0:
        # Write PID file
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(pid))
        os._exit(0)

    # Daemon process
    # Redirect stdio
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.close(devnull)

    holder = PtyHolder(socket_path)
    try:
        asyncio.run(holder.serve())
    finally:
        pid_path.unlink(missing_ok=True)
    sys.exit(0)


def _main_inherit() -> None:
    """Entry point invoked by ``restart_in_place`` via ``os.execv``.

    Parses ``--socket`` and ``--inherit`` args, restores worker state
    from the handoff file, then runs ``serve()`` like a normal startup.
    Stays in the foreground (no double-fork) — the parent holder already
    detached from any controlling terminal.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="swarm.pty.holder")
    parser.add_argument(
        "--socket", default=str(DEFAULT_SOCKET_PATH), help="Unix socket path the holder listens on"
    )
    parser.add_argument(
        "--inherit", default="", help="Path to handoff state file from the previous holder"
    )
    args = parser.parse_args()

    socket_path = Path(args.socket)
    pid_path = socket_path.with_suffix(".pid")

    # The execv preserved the same PID, but the .pid file may have been
    # written by the old holder daemon. Refresh it so external killers
    # land on this process.
    try:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()))
    except OSError as exc:
        _log.warning("failed to refresh pid file %s: %s", pid_path, exc)

    holder = PtyHolder(socket_path)

    if args.inherit:
        state_path = Path(args.inherit)
        try:
            holder.inherit_workers(state_path)
        finally:
            try:
                state_path.unlink(missing_ok=True)
            except OSError:
                pass

    try:
        asyncio.run(holder.serve())
    finally:
        pid_path.unlink(missing_ok=True)


if __name__ == "__main__":
    _main_inherit()
