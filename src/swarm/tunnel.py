"""Cloudflare Tunnel manager — spawns cloudflared for remote HTTPS access."""

from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import Callable
from enum import Enum
from pathlib import Path

from swarm.logging import get_logger

_log = get_logger("tunnel")

_URL_WAIT_TIMEOUT = 30.0  # seconds
_STOP_TIMEOUT = 5.0  # seconds

_RESTART_MARKER = Path.home() / ".swarm" / "tunnel-restart"

_URL_RE = re.compile(r"https://[a-zA-Z0-9_-]+\.trycloudflare\.com")


class TunnelState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"


class TunnelManager:
    """Manages a cloudflared quick-tunnel subprocess."""

    def __init__(
        self,
        port: int = 9090,
        on_state_change: Callable[[TunnelState, str], None] | None = None,
    ) -> None:
        self.port = port
        self._on_state_change = on_state_change
        self._process: asyncio.subprocess.Process | None = None
        self._state = TunnelState.STOPPED
        self._url: str = ""
        self._error: str = ""
        self._reader_task: asyncio.Task[None] | None = None

    @property
    def state(self) -> TunnelState:
        return self._state

    @property
    def url(self) -> str:
        return self._url

    @property
    def error(self) -> str:
        return self._error

    @property
    def is_running(self) -> bool:
        return self._state == TunnelState.RUNNING

    def _set_state(self, state: TunnelState, detail: str = "") -> None:
        self._state = state
        if self._on_state_change:
            self._on_state_change(state, detail)

    async def start(self) -> str:
        """Start the cloudflared tunnel. Returns the public URL.

        Raises RuntimeError if cloudflared is not installed or fails to start.
        """
        if self._state in (TunnelState.STARTING, TunnelState.RUNNING):
            return self._url

        if not shutil.which("cloudflared"):
            self._error = "cloudflared is not installed"
            self._set_state(TunnelState.ERROR, self._error)
            raise RuntimeError(self._error)

        self._url = ""
        self._error = ""
        self._set_state(TunnelState.STARTING)

        try:
            self._process = await asyncio.create_subprocess_exec(
                "cloudflared",
                "tunnel",
                "--url",
                f"http://localhost:{self.port}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            self._error = str(e)
            self._set_state(TunnelState.ERROR, self._error)
            raise RuntimeError(self._error) from e

        # Parse URL from stderr (cloudflared prints it there)
        url = await self._wait_for_url()
        if not url:
            await self.stop()
            msg = self._error or "Failed to get tunnel URL"
            self._set_state(TunnelState.ERROR, msg)
            raise RuntimeError(msg)

        self._url = url
        self._set_state(TunnelState.RUNNING, url)
        _log.info("tunnel running: %s", url)

        # Start background reader to detect process exit
        self._reader_task = asyncio.create_task(self._watch_process())
        return url

    async def _wait_for_url(self, timeout: float = _URL_WAIT_TIMEOUT) -> str:
        """Read stderr until we find the trycloudflare URL or timeout."""
        if self._process is None or self._process.stderr is None:
            raise RuntimeError("tunnel process not started or stderr unavailable")

        try:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                remaining = deadline - loop.time()
                try:
                    line_bytes = await asyncio.wait_for(
                        self._process.stderr.readline(),
                        timeout=remaining,
                    )
                except TimeoutError:
                    break

                if not line_bytes:
                    # EOF — process likely exited
                    break

                line = line_bytes.decode(errors="replace")
                _log.debug("cloudflared: %s", line.rstrip())

                match = _URL_RE.search(line)
                if match:
                    return match.group(0)

            self._error = "Timed out waiting for tunnel URL"
        except Exception as e:
            self._error = str(e)
        return ""

    async def _watch_process(self) -> None:
        """Drain stderr and detect process exit.

        After URL extraction, cloudflared keeps logging to stderr.  If
        nobody reads the pipe, the 64KB kernel buffer fills up and
        cloudflared blocks — freezing the QUIC connection and killing
        the tunnel.  This task drains stderr continuously until the
        process exits.
        """
        try:
            if not self._process:
                return
            # Drain stderr so cloudflared never blocks on a full pipe
            stderr = self._process.stderr
            if stderr:
                while True:
                    line = await stderr.readline()
                    if not line:
                        break  # EOF — process exited
                    _log.debug("cloudflared: %s", line.decode(errors="replace").rstrip())
            await self._process.wait()
            if self._state == TunnelState.RUNNING:
                _log.warning("cloudflared exited unexpectedly (rc=%s)", self._process.returncode)
                self._state = TunnelState.STOPPED
                self._url = ""
                self._set_state(TunnelState.STOPPED)
        except asyncio.CancelledError:
            return

    async def stop(self) -> None:
        """Stop the cloudflared tunnel."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            self._reader_task = None

        if self._process and self._process.returncode is None:
            try:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=_STOP_TIMEOUT)
                except TimeoutError:
                    self._process.kill()
                    await self._process.wait()
            except ProcessLookupError:
                pass  # Already exited

        self._process = None
        self._url = ""
        was_running = self._state == TunnelState.RUNNING
        self._state = TunnelState.STOPPED
        if was_running:
            self._set_state(TunnelState.STOPPED)
            _log.info("tunnel stopped")

    def save_restart_marker(self) -> None:
        """Write a marker file so the tunnel auto-starts after a daemon restart."""
        if not self.is_running:
            return
        try:
            _RESTART_MARKER.parent.mkdir(parents=True, exist_ok=True)
            _RESTART_MARKER.touch()
            _log.info("tunnel restart marker saved")
        except Exception:
            _log.warning("failed to save tunnel restart marker", exc_info=True)

    @staticmethod
    def consume_restart_marker() -> bool:
        """Return True and delete the marker if it exists."""
        try:
            if _RESTART_MARKER.exists():
                _RESTART_MARKER.unlink()
                return True
        except Exception:
            _log.debug("Failed to check restart marker", exc_info=True)
        return False

    def to_dict(self) -> dict[str, str | bool]:
        """Serialize tunnel state for API responses."""
        return {
            "running": self.is_running,
            "state": self._state.value,
            "url": self._url,
            "error": self._error,
        }
