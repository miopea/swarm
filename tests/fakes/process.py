"""Fake WorkerProcess for unit tests.

Provides an in-memory process simulation so tests can exercise
pilot/daemon/manager code without a real PTY holder.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from swarm.pty.buffer import RingBuffer
from swarm.pty.terminal import CellStyle


@dataclass
class FakeWorkerProcess:
    """In-memory WorkerProcess replacement for tests.

    Usage::

        fake = FakeWorkerProcess(name="alice")
        fake.set_content("Some output\\n")
        worker.process = fake
    """

    name: str
    cwd: str = "/tmp"
    cols: int = 200
    rows: int = 50
    pid: int | None = 1234
    buffer: RingBuffer = field(default_factory=RingBuffer, repr=False)
    _alive: bool = True
    _exit_code: int | None = None
    _foreground_command: str = "claude"
    _child_foreground_command: str = "claude"
    keys_sent: list[str] = field(default_factory=list, repr=False)
    # #1451: writes held because a selection prompt was open.
    deferred_keys: list[tuple[str, bool]] = field(default_factory=list, repr=False)
    _killed: bool = False
    _terminal_active: bool = False
    _last_user_input: float = 0.0

    def set_content(self, text: str) -> None:
        """Set the buffer content (convenience for tests)."""
        self.buffer.clear()
        self.buffer.write(text.encode("utf-8"))

    def get_content(self, lines: int = 35) -> str:
        """Read from the buffer, like the real WorkerProcess."""
        return self.buffer.get_lines(lines)

    def get_styled_content(self, lines: int = 35) -> tuple[str, list[tuple[str, list[CellStyle]]]]:
        """Read from the buffer with style data, like the real WorkerProcess."""
        return self.buffer.get_styled_lines(lines)

    def get_foreground_command(self) -> str:
        return self._foreground_command

    def get_child_foreground_command(self) -> str:
        return self._child_foreground_command

    async def async_get_foreground_command(self) -> str:
        return self._foreground_command

    async def async_get_child_foreground_command(self) -> str:
        return self._child_foreground_command

    def feed_output(self, data: bytes) -> None:
        self.buffer.write(data)

    async def send_keys(self, text: str, enter: bool = True, *, automated: bool = False) -> None:
        """Mirror WorkerProcess.send_keys, INCLUDING the #1451 hold.

        The fake models the guard rather than merely tolerating the keyword. A
        fake that accepted ``automated`` and ignored it would let every existing
        test keep passing while the real guard was wired backwards — the fake
        would be asserting that the bug is absent by construction.
        """
        from swarm.pty.prompt_guard import has_open_selection_prompt

        if automated and has_open_selection_prompt(self.buffer.get_lines(120)):
            self.deferred_keys.append((text, enter))
            return
        for qtext, qenter in self.deferred_keys:
            self.keys_sent.append(qtext + ("\n" if qenter else ""))
        self.deferred_keys.clear()
        full = text + ("\n" if enter else "")
        self.keys_sent.append(full)

    async def send_enter(self) -> None:
        self.keys_sent.append("\n")

    async def send_interrupt(self) -> None:
        self.keys_sent.append("<C-c>")

    async def send_escape(self) -> None:
        self.keys_sent.append("<Esc>")

    async def resize(self, cols: int, rows: int) -> None:
        self.cols = cols
        self.rows = rows

    async def kill(self) -> None:
        self._alive = False
        self._killed = True

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

    _USER_ACTIVE_WINDOW = 2.0

    @property
    def is_user_active(self) -> bool:
        import time

        elapsed = time.time() - self._last_user_input
        return self._terminal_active and elapsed < self._USER_ACTIVE_WINDOW

    @property
    def last_user_input_at(self) -> float:
        return self._last_user_input

    def operator_engaged_within(self, window_seconds: float) -> bool:
        import time

        if window_seconds <= 0 or self._last_user_input == 0.0:
            return False
        return (time.time() - self._last_user_input) < window_seconds

    def mark_user_input(self) -> None:
        import time

        self._last_user_input = time.time()

    def record_input_bytes(self, size: int) -> None:
        """Stub for term-trace input counter — real impl in WorkerProcess."""
        self._trace_input_bytes = getattr(self, "_trace_input_bytes", 0) + max(0, size)

    def set_terminal_active(self, active: bool) -> None:
        self._terminal_active = active

    @property
    def has_ws_subscribers(self) -> bool:
        return False

    def subscribe_ws(self, ws: object) -> None:
        pass

    def unsubscribe_ws(self, ws: object) -> None:
        pass

    def subscribe_and_snapshot(self, ws: object) -> bytes:
        """Atomic snapshot + subscribe — mirrors the real WorkerProcess API."""
        return self.buffer.snapshot()

    async def get_replay_snapshot(self) -> bytes:
        return self.buffer.snapshot()
