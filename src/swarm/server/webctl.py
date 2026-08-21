"""Web dashboard process control — start/stop/status (subprocess + embedded)."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
from typing import TYPE_CHECKING

from swarm.logging import get_logger
from swarm.paths import state_dir

if TYPE_CHECKING:
    from aiohttp import web

    from swarm.server.daemon import SwarmDaemon

_log = get_logger("server.webctl")

_PID_DIR = state_dir()
_WEB_PID_FILE = _PID_DIR / "web.pid"
_WEB_LOG_FILE = _PID_DIR / "web.log"

# --- Embedded web server (shares state with daemon) ---

_embedded_lock = threading.Lock()
_embedded_thread: threading.Thread | None = None
_embedded_loop: asyncio.AbstractEventLoop | None = None
_embedded_runner: web.AppRunner | None = None


def web_start_embedded(
    daemon: SwarmDaemon, host: str = "localhost", port: int = 9090
) -> tuple[bool, str]:
    """Start the web server in-process, sharing state with the caller."""
    global _embedded_thread, _embedded_loop, _embedded_runner

    with _embedded_lock:
        if _embedded_thread is not None and _embedded_thread.is_alive():
            return False, f"Already running — http://{host}:{port}"

        from aiohttp import web as aio_web

        from swarm.server.api import create_app

        app = create_app(daemon)

        def _run() -> None:
            global _embedded_loop, _embedded_runner
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            _embedded_loop = loop
            runner = aio_web.AppRunner(app)
            _embedded_runner = runner
            loop.run_until_complete(runner.setup())
            site = aio_web.TCPSite(runner, host, port)
            loop.run_until_complete(site.start())
            _log.info("embedded web server listening on http://%s:%d", host, port)
            try:
                loop.run_forever()
            finally:
                loop.run_until_complete(runner.cleanup())
                loop.close()

        _embedded_thread = threading.Thread(target=_run, daemon=True, name="swarm-web")
        _embedded_thread.start()

    return True, f"Started — http://{host}:{port}"


def web_stop_embedded() -> tuple[bool, str]:
    """Stop the embedded web server."""
    global _embedded_thread, _embedded_loop, _embedded_runner

    with _embedded_lock:
        if _embedded_loop and not _embedded_loop.is_closed():
            _embedded_loop.call_soon_threadsafe(_embedded_loop.stop)
            if _embedded_thread:
                _embedded_thread.join(timeout=5)
            _embedded_thread = None
            _embedded_loop = None
            _embedded_runner = None
            return True, "Stopped"
        return False, "Not running"


def web_is_running_embedded() -> bool:
    """Check if the embedded web server thread is alive."""
    return _embedded_thread is not None and _embedded_thread.is_alive()


# --- Subprocess web server (standalone mode) ---


def web_is_running() -> int | None:
    """Return the PID if the subprocess web dashboard is running, else None."""
    if not _WEB_PID_FILE.exists():
        return None
    try:
        pid = int(_WEB_PID_FILE.read_text().strip())
        os.kill(pid, 0)  # Check if process exists
        return pid
    except (ProcessLookupError, ValueError, OSError):
        _WEB_PID_FILE.unlink(missing_ok=True)
        return None


def web_start(
    host: str = "localhost",
    port: int = 9090,
    config_path: str | None = None,
    session: str | None = None,
) -> tuple[bool, str]:
    """Start the web dashboard as a background subprocess.

    Returns (success, message).
    """
    pid = web_is_running()
    if pid is not None:
        return False, f"Already running (PID {pid}) — http://{host}:{port}"

    _PID_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, "-m", "swarm", "serve", "--host", host, "--port", str(port)]
    if config_path:
        cmd.extend(["-c", config_path])
    if session:
        cmd.extend(["-s", session])

    with open(_WEB_LOG_FILE, "a") as log_file:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
    _WEB_PID_FILE.write_text(str(proc.pid))

    return True, f"Started (PID {proc.pid}) — http://{host}:{port}"


def web_stop() -> tuple[bool, str]:
    """Stop the background subprocess web dashboard.

    Returns (success, message).
    """
    if not _WEB_PID_FILE.exists():
        return False, "Not running"

    try:
        pid = int(_WEB_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        _WEB_PID_FILE.unlink(missing_ok=True)
        return False, "Invalid PID file (cleaned up)"

    try:
        os.kill(pid, signal.SIGTERM)
        msg = f"Stopped (PID {pid})"
    except ProcessLookupError:
        msg = f"Process {pid} already gone"

    _WEB_PID_FILE.unlink(missing_ok=True)
    return True, msg
