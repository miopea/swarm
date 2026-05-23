"""WorkerProcess — client-side representation of a worker in the pty-holder.

Each WorkerProcess communicates with the holder over the shared Unix socket.
It maintains a local RingBuffer fed by the holder's output stream, and
manages WebSocket subscribers for the web terminal.
"""

from __future__ import annotations

import asyncio
import base64
import signal
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from swarm.logging import get_logger
from swarm.pty.buffer import RingBuffer
from swarm.pty.terminal import CellStyle

_log = get_logger("pty.process")

# Delay between text and Enter so TUI apps can process input
_INPUT_DRAIN_DELAY = 0.05  # seconds

# Type alias for the pool command sender bound method
_SendCmd = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class ProcessErrorKind:
    """Classification of PTY process errors for smarter recovery."""

    NOT_RUNNING = "not_running"  # Worker process has exited
    SEND_FAILED = "send_failed"  # Failed to send input to PTY
    TIMEOUT = "timeout"  # Operation timed out
    HOLDER_DISCONNECTED = "holder_disconnected"  # PTY holder not reachable
    UNKNOWN = "unknown"


class ProcessError(Exception):
    """Raised when a PTY process operation fails."""

    def __init__(self, message: str, kind: str = ProcessErrorKind.UNKNOWN) -> None:
        super().__init__(message)
        self.kind = kind

    @property
    def is_recoverable(self) -> bool:
        """True if the error may be transient and worth retrying."""
        return self.kind in (ProcessErrorKind.SEND_FAILED, ProcessErrorKind.TIMEOUT)

    @property
    def needs_revive(self) -> bool:
        """True if the worker process needs to be restarted."""
        return self.kind == ProcessErrorKind.NOT_RUNNING


class WorkerProcess:
    """Client-side handle for a worker running in the pty-holder.

    Parameters
    ----------
    name:
        Unique worker name.
    cwd:
        Working directory for the worker process.
    cols, rows:
        Initial terminal dimensions.
    """

    def __init__(
        self,
        name: str,
        cwd: str,
        cols: int = 200,
        rows: int = 50,
    ) -> None:
        self.name = name
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self.buffer = RingBuffer()
        self.pid: int | None = None
        self._alive = False
        self._exit_code: int | None = None
        self._ws_subscribers: set[web.WebSocketResponse] = set()
        # Per-subscriber ordered send queues
        self._ws_queues: dict[int, asyncio.Queue[bytes]] = {}
        self._ws_tasks: dict[int, asyncio.Task[None]] = {}
        self._ws_max_backlog = 500
        self._ws_queue_warned: set[int] = set()  # track high-watermark warnings
        # term-trace counters: rolled up periodically so feed_output stays cheap.
        # ``bytes_in / bytes_out`` cover the PTY→daemon→WS output direction;
        # ``bytes_input`` covers the WS→daemon→PTY keystroke direction so the
        # 30s rollup makes gaps visible in either direction.
        self._trace_bytes_in: int = 0
        self._trace_bytes_out: int = 0
        self._trace_frames_in: int = 0
        self._trace_bytes_input: int = 0
        self._trace_frames_input: int = 0
        self._trace_last_summary: float = time.time()
        # Set by the pool when connected (use bind_send_cmd to update)
        self._send_cmd: _SendCmd | None = None
        # Terminal-active guard: prevents automated input while user is typing
        self._terminal_active: bool = False
        self._last_user_input: float = 0.0

    def bind_send_cmd(self, send_cmd: _SendCmd) -> None:
        """Bind the pool's command sender (public API for pool)."""
        self._send_cmd = send_cmd

    _USER_ACTIVE_WINDOW = 2.0

    @property
    def is_user_active(self) -> bool:
        """True when a user has the web terminal open and recently typed."""
        elapsed = time.time() - self._last_user_input
        return self._terminal_active and elapsed < self._USER_ACTIVE_WINDOW

    @property
    def last_user_input_at(self) -> float:
        """Wall-clock timestamp of the most recent operator keystroke (0 if never)."""
        return self._last_user_input

    def operator_engaged_within(self, window_seconds: float) -> bool:
        """True when the operator typed in this PTY within the last ``window_seconds``.

        Used by oversight to gate `redirect` interventions: a periodic drift
        signal must not interrupt an interactive session. Unlike
        ``is_user_active``, this does NOT require a live web terminal —
        operators can attach intermittently, and recent input within a
        multi-minute window is enough evidence of engagement.
        """
        if window_seconds <= 0 or self._last_user_input == 0.0:
            return False
        return (time.time() - self._last_user_input) < window_seconds

    def mark_user_input(self) -> None:
        """Record that the user just sent input via the web terminal."""
        self._last_user_input = time.time()

    def record_input_bytes(self, size: int) -> None:
        """Bump term-trace input counters by ``size`` bytes.

        Called by the WS→PTY bridge when a BINARY keystroke frame arrives.
        Accumulated totals appear in the 30 s rollup alongside the output
        counters so silent "typed but nothing reached the PTY" failures
        show up as ``input=N`` during windows where output_frames=0.
        """
        if size <= 0:
            return
        self._trace_bytes_input += size
        self._trace_frames_input += 1
        # Typing without echo would otherwise leave the rollup dormant —
        # feed_output is the only other caller and it only fires on PTY
        # output. Running the same rollup here keeps the input-only case
        # visible too.
        self._maybe_emit_trace_summary()

    def _maybe_emit_trace_summary(self) -> None:
        """Emit the 30 s term-trace rollup if the window has elapsed."""
        now = time.time()
        if now - self._trace_last_summary < 30.0:
            return
        if not (self._trace_frames_in or self._trace_frames_input):
            return
        _log.warning(
            "[term-trace] %s 30s window: frames=%d in=%dB out=%dB "
            "input_frames=%d input=%dB subs=%d queues=%d senders=%d",
            self.name,
            self._trace_frames_in,
            self._trace_bytes_in,
            self._trace_bytes_out,
            self._trace_frames_input,
            self._trace_bytes_input,
            len(self._ws_subscribers),
            len(self._ws_queues),
            len(self._ws_tasks),
        )
        self._trace_bytes_in = 0
        self._trace_bytes_out = 0
        self._trace_frames_in = 0
        self._trace_bytes_input = 0
        self._trace_frames_input = 0
        self._trace_last_summary = now

    @property
    def has_ws_subscribers(self) -> bool:
        """Return True if any WebSocket subscribers are connected."""
        return bool(self._ws_subscribers)

    def set_terminal_active(self, active: bool) -> None:
        """Set whether a web terminal session is connected."""
        self._terminal_active = active

    def feed_output(self, data: bytes) -> None:
        """Feed output data from the holder into the local buffer and WS subscribers."""
        self.buffer.write(data)
        self._trace_bytes_in += len(data)
        self._trace_frames_in += 1
        # Prune dead sender tasks to prevent memory leaks
        dead_ids = [ws_id for ws_id, task in self._ws_tasks.items() if task.done()]
        if dead_ids:
            _log.warning(
                "[term-trace] %s feed_output: pruning %d completed sender task(s) "
                "(subscribers=%d, queues=%d)",
                self.name,
                len(dead_ids),
                len(self._ws_subscribers),
                len(self._ws_queues),
            )
        for ws_id in dead_ids:
            self._ws_queues.pop(ws_id, None)
            self._ws_tasks.pop(ws_id, None)
        # Broadcast to WebSocket subscribers
        dead: list[web.WebSocketResponse] = []
        for ws in list(self._ws_subscribers):
            if ws.closed or not self._enqueue_ws(ws, data):
                dead.append(ws)
        if dead:
            _log.warning(
                "[term-trace] %s feed_output: dropping %d ws subscriber(s) "
                "(closed-or-enqueue-failed; live subscribers=%d)",
                self.name,
                len(dead),
                len(self._ws_subscribers) - len(dead),
            )
        for ws in dead:
            self._drop_ws(ws, reason="feed_output enqueue failure or ws.closed")
        self._trace_bytes_out += len(data) * len(self._ws_subscribers)
        self._maybe_emit_trace_summary()

    def _enqueue_ws(self, ws: web.WebSocketResponse, data: bytes) -> bool:
        """Try to enqueue data for a WS subscriber. Returns False to drop."""
        try:
            ws_id = id(ws)
            queue = self._ws_queues.get(ws_id)
            if not queue:
                queue = asyncio.Queue(maxsize=self._ws_max_backlog)
                self._ws_queues[ws_id] = queue
                self._ws_tasks[ws_id] = asyncio.get_running_loop().create_task(
                    self._ws_sender(ws, queue)
                )
            # High-watermark warning (once per subscriber)
            qsize = queue.qsize()
            if qsize > self._ws_max_backlog * 0.8 and ws_id not in self._ws_queue_warned:
                self._ws_queue_warned.add(ws_id)
                _log.warning(
                    "WS queue high watermark for %s: %d/%d",
                    self.name,
                    qsize,
                    self._ws_max_backlog,
                )
            if queue.full():
                if not self._coalesce_queue(queue, data):
                    _log.warning(
                        "dropping stuck WS subscriber for %s (queue %d)",
                        self.name,
                        qsize,
                    )
                    return False
                return True
            queue.put_nowait(data)
            return True
        except RuntimeError:
            return False
        except Exception:
            _log.debug("WS enqueue failed for %s", self.name, exc_info=True)
            return False

    def _coalesce_queue(self, queue: asyncio.Queue[bytes], new_data: bytes) -> bool:
        """Drain a full queue into one merged chunk. Returns True if successful."""
        chunks: list[bytes] = []
        while not queue.empty():
            try:
                chunks.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if chunks:
            queue.put_nowait(b"".join(chunks) + new_data)
            _log.debug("coalesced %d WS chunks for %s", len(chunks) + 1, self.name)
            return True
        return False

    async def _ws_sender(
        self,
        ws: web.WebSocketResponse,
        queue: asyncio.Queue[bytes],
    ) -> None:
        """Send queued WS output in-order to avoid interleaving ANSI streams."""
        exit_reason = "queue closed"
        try:
            while True:
                data = await queue.get()
                if ws.closed:
                    exit_reason = "ws.closed mid-send"
                    break
                await ws.send_bytes(data)
        except asyncio.CancelledError:
            exit_reason = "task cancelled"
            raise
        except Exception as exc:
            exit_reason = f"exception: {type(exc).__name__}: {exc}"
            _log.warning("WS sender failed for %s", self.name, exc_info=True)
        finally:
            _log.warning(
                "[term-trace] %s ws sender exit (id=%d) — %s",
                self.name,
                id(ws),
                exit_reason,
            )
            self._drop_ws(ws, reason=f"sender exit: {exit_reason}")

    def _drop_ws(self, ws: web.WebSocketResponse, *, reason: str = "unspecified") -> None:
        """Remove a WS subscriber and stop its sender task.

        Also schedules a WS close so the client detects the disconnect and
        reconnects.  Without this, the WS stays open (heartbeat pings keep
        it alive) but no output is delivered — the terminal appears frozen.
        """
        ws_id = id(ws)
        was_subscribed = ws in self._ws_subscribers
        self._ws_subscribers.discard(ws)
        task = self._ws_tasks.pop(ws_id, None)
        if task:
            task.cancel()
        self._ws_queues.pop(ws_id, None)
        if was_subscribed or task:
            _log.warning(
                "[term-trace] %s drop_ws (id=%d) — %s (remaining subscribers=%d, ws.closed=%s)",
                self.name,
                ws_id,
                reason,
                len(self._ws_subscribers),
                getattr(ws, "closed", "?"),
            )
        # Close the WS so the client-side onclose fires and triggers reconnect.
        if not ws.closed:
            try:
                asyncio.get_running_loop().create_task(ws.close())
            except RuntimeError:
                pass  # no event loop (shutdown)

    def cleanup_ws(self) -> None:
        """Cancel all WS sender tasks and clear subscriber state.

        Called when the process dies to prevent lingering tasks/queues.
        """
        for ws in list(self._ws_subscribers):
            self._drop_ws(ws, reason="cleanup_ws (worker process died)")

    def get_content(self, lines: int = 35) -> str:
        """Read the last N lines from the local ring buffer (synchronous).

        Used by ``classify_worker_output()`` for state detection.
        Zero subprocess calls — reads from in-process memory.
        """
        return self.buffer.get_lines(lines)

    def get_styled_content(self, lines: int = 35) -> tuple[str, list[tuple[str, list[CellStyle]]]]:
        """Read last N lines with per-character style data.

        Returns ``(text, styled_rows)`` — see
        :meth:`~swarm.pty.buffer.RingBuffer.get_styled_lines`.
        """
        return self.buffer.get_styled_lines(lines)

    def get_foreground_command(self) -> str:
        """Read the foreground command from /proc/{pid}/stat.

        Returns the command name (e.g. 'claude', 'bash') or '' on failure.
        """
        if not self.pid:
            return ""
        try:
            stat = Path(f"/proc/{self.pid}/stat").read_text()
            # Format: "pid (comm) state ..." — extract comm
            start = stat.index("(") + 1
            end = stat.index(")")
            return stat[start:end]
        except (FileNotFoundError, ValueError, OSError):
            return ""

    def get_child_foreground_command(self) -> str:
        """Read the foreground command of the first child process.

        The PTY holder forks a child which runs the actual command.
        The child's children are what we care about for state detection
        (e.g. 'claude' vs 'bash' after claude exits).
        """
        if not self.pid:
            return ""
        try:
            # Find child PIDs
            children_path = Path(f"/proc/{self.pid}/task/{self.pid}/children")
            if children_path.exists():
                children = children_path.read_text().strip().split()
                if children:
                    child_pid = children[0]
                    stat = Path(f"/proc/{child_pid}/stat").read_text()
                    start = stat.index("(") + 1
                    end = stat.index(")")
                    return stat[start:end]
        except (FileNotFoundError, ValueError, OSError):
            pass
        # Fallback to own command
        return self.get_foreground_command()

    async def send_keys(self, text: str, enter: bool = True) -> None:
        """Send text to the worker's PTY.

        Text and Enter are sent as separate writes so that interactive
        TUI apps (e.g. Claude Code's slash-command autocomplete) have
        time to process the input before receiving the carriage return.
        """
        await self._write(text.encode("utf-8"))
        if enter:
            await asyncio.sleep(_INPUT_DRAIN_DELAY)
            await self._write(b"\r")

    async def send_enter(self) -> None:
        """Send Enter (carriage return) to the worker."""
        await self._write(b"\r")

    async def send_interrupt(self) -> None:
        """Send SIGINT to the worker's process group."""
        await self._signal(signal.SIGINT)

    async def send_escape(self) -> None:
        """Send ESC byte to the worker's PTY."""
        await self._write(b"\x1b")

    async def send_arrow_up(self) -> None:
        """Send Up Arrow (ANSI escape) to the worker's PTY."""
        await self._write(b"\x1b[A")

    async def send_arrow_down(self) -> None:
        """Send Down Arrow (ANSI escape) to the worker's PTY."""
        await self._write(b"\x1b[B")

    async def send_arrow_right(self) -> None:
        """Send Right Arrow (ANSI escape) to the worker's PTY."""
        await self._write(b"\x1b[C")

    async def send_arrow_left(self) -> None:
        """Send Left Arrow (ANSI escape) to the worker's PTY."""
        await self._write(b"\x1b[D")

    async def send_sigwinch(self) -> None:
        """Send SIGWINCH to force TUI redraw."""
        await self._signal(signal.SIGWINCH)

    async def resize(self, cols: int, rows: int) -> None:
        """Resize the worker's PTY."""
        if cols == self.cols and rows == self.rows:
            return  # skip no-op resize, avoids SIGWINCH
        if not self._send_cmd:
            raise ProcessError(
                f"worker {self.name!r}: not connected to holder",
                kind=ProcessErrorKind.HOLDER_DISCONNECTED,
            )
        self.cols = cols
        self.rows = rows
        self.buffer.resize(cols, rows)
        await self._send_cmd(
            {
                "cmd": "resize",
                "name": self.name,
                "cols": cols,
                "rows": rows,
            }
        )

    def subscribe_ws(self, ws: web.WebSocketResponse) -> None:
        """Add a WebSocket subscriber for real-time output."""
        was_new = ws not in self._ws_subscribers
        self._ws_subscribers.add(ws)
        if was_new:
            _log.warning(
                "[term-trace] %s subscribe_ws (id=%d) — total subscribers=%d",
                self.name,
                id(ws),
                len(self._ws_subscribers),
            )

    def unsubscribe_ws(self, ws: web.WebSocketResponse) -> None:
        """Remove a WebSocket subscriber."""
        self._drop_ws(ws, reason="unsubscribe_ws (handler exit)")

    def subscribe_and_snapshot(self, ws: web.WebSocketResponse) -> bytes:
        """Add a WebSocket subscriber and return the current buffer snapshot.

        This is done in one atomic step (relative to the event loop) to
        avoid missing or duplicating data between the snapshot and live stream.
        """
        # Both buffer.snapshot() and adding to the set are synchronous.
        # Since feed_output is also called from the same event loop,
        # no data can arrive between these two lines.
        snapshot = self.buffer.snapshot()
        was_new = ws not in self._ws_subscribers
        self._ws_subscribers.add(ws)
        _log.warning(
            "[term-trace] %s subscribe_and_snapshot (id=%d, new=%s, snapshot=%dB) "
            "— total subscribers=%d",
            self.name,
            id(ws),
            was_new,
            len(snapshot),
            len(self._ws_subscribers),
        )
        return snapshot

    async def get_replay_snapshot(self) -> bytes:
        """Return the best available replay snapshot for web terminal attach.

        Prefer the daemon-local buffer for speed. If it is empty after a daemon
        restart, fetch a fresh snapshot from the holder sidecar and repopulate
        the local buffer so reconnecting terminals still get scrollback.
        """
        snapshot = self.buffer.snapshot()
        if snapshot or not self._send_cmd:
            return snapshot
        try:
            resp = await self._send_cmd({"cmd": "snapshot", "name": self.name})
            if not resp.get("ok"):
                return b""
            data = base64.b64decode(resp.get("data", ""))
            if data:
                self.buffer.write(data)
            return data
        except Exception:
            _log.warning("failed to fetch holder snapshot for %s", self.name, exc_info=True)
            return b""

    async def kill(self) -> None:
        """Kill the worker process via the holder."""
        if self._send_cmd:
            await self._send_cmd({"cmd": "kill", "name": self.name})
        self._alive = False

    @property
    def is_alive(self) -> bool:
        return self._alive

    @is_alive.setter
    def is_alive(self, value: bool) -> None:
        self._alive = value

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    @exit_code.setter
    def exit_code(self, value: int | None) -> None:
        self._exit_code = value

    async def _write(self, data: bytes) -> None:
        """Write raw bytes to the worker's PTY via the holder."""
        if not self._send_cmd:
            raise ProcessError(
                f"Worker '{self.name}' not connected to holder",
                kind=ProcessErrorKind.HOLDER_DISCONNECTED,
            )
        resp = await self._send_cmd(
            {
                "cmd": "write",
                "name": self.name,
                "data": base64.b64encode(data).decode(),
            }
        )
        if not resp.get("ok"):
            error = resp.get("error", "unknown")
            kind = (
                ProcessErrorKind.NOT_RUNNING
                if "not found" in error or "not alive" in error
                else ProcessErrorKind.SEND_FAILED
            )
            raise ProcessError(f"Write failed for '{self.name}': {error}", kind=kind)

    async def _signal(self, sig: int) -> None:
        """Send a signal to the worker via the holder."""
        if not self._send_cmd:
            raise ProcessError(
                f"Worker '{self.name}' not connected to holder",
                kind=ProcessErrorKind.HOLDER_DISCONNECTED,
            )
        sig_name = signal.Signals(sig).name
        resp = await self._send_cmd(
            {
                "cmd": "signal",
                "name": self.name,
                "sig": sig_name,
            }
        )
        if not resp.get("ok"):
            raise ProcessError(
                f"Signal {sig_name} failed for '{self.name}': {resp.get('error', 'unknown')}",
                kind=ProcessErrorKind.SEND_FAILED,
            )
