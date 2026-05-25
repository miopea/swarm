"""PtyBridge — WebSocket-to-PTY bridge for the web terminal.

Each WebSocket connects to a specific worker's process, receiving output
in real time and forwarding input directly.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import TYPE_CHECKING

from aiohttp import web

from swarm.logging import get_logger
from swarm.pty.process import ProcessError

if TYPE_CHECKING:
    from swarm.pty.process import WorkerProcess

_log = get_logger("pty.bridge")

_MAX_TERMINAL_SESSIONS = 20
# Maximum bytes accepted in a single WebSocket binary message (input).
# Larger pastes are chunked with yielding between chunks to avoid
# monopolizing the event loop and starving other workers.
_MAX_INPUT_MSG_BYTES = 128 * 1024  # 128 KiB hard cap
_INPUT_CHUNK_SIZE = 16384  # bytes per chunk sent to PTY (16 KiB)
_INPUT_CHUNK_DELAY = 0.002  # seconds between chunks
_INITIAL_VIEW_TIMEOUT = 3.0  # seconds


def _check_auth(request: web.Request) -> web.Response | None:
    """Return a 403 Response if origin check fails, or None if OK.

    Token auth is handled after ws.prepare() via first-message auth.
    """
    from swarm.server.api import check_origin_or_error

    return check_origin_or_error(request)


async def _send_input_chunked(raw: bytes, proc: WorkerProcess) -> None:
    """Send input to PTY, chunking large pastes to avoid starving the event loop."""
    if len(raw) > _MAX_INPUT_MSG_BYTES:
        _log.warning(
            "input too large (%d bytes), truncating to %d",
            len(raw),
            _MAX_INPUT_MSG_BYTES,
        )
        raw = raw[:_MAX_INPUT_MSG_BYTES]
    if len(raw) > _INPUT_CHUNK_SIZE:
        for offset in range(0, len(raw), _INPUT_CHUNK_SIZE):
            chunk = raw[offset : offset + _INPUT_CHUNK_SIZE]
            await proc.send_keys(chunk.decode("utf-8", errors="replace"), enter=False)
            await asyncio.sleep(_INPUT_CHUNK_DELAY)
            proc.mark_user_input()
    else:
        await proc.send_keys(raw.decode("utf-8", errors="replace"), enter=False)


async def _handle_ws_message(
    msg: web.WSMessage,
    ws: web.WebSocketResponse | None,
    proc: WorkerProcess,
) -> bool:
    """Process a single WS message.  Returns False to break the loop."""

    if msg.type == web.WSMsgType.BINARY:
        try:
            proc.mark_user_input()
            # term-trace: record input bytes for the 30s rollup so silent
            # "typed but nothing reached the PTY" gaps are visible. Logged
            # as a counter, not per-keystroke — one entry every 30s keeps
            # the log quiet while still catching the symptom.
            proc.record_input_bytes(len(msg.data))
            await _send_input_chunked(msg.data, proc)
        except ProcessError as exc:
            _log.warning(
                "[term-trace] %s input send failed (%d bytes) — %s",
                proc.name,
                len(msg.data),
                exc,
            )
            return False
    elif msg.type == web.WSMsgType.TEXT:
        try:
            payload = json.loads(msg.data)
            if payload.get("action") == "resize" or "cols" in payload:
                cols = max(1, min(500, int(payload.get("cols") or 80)))
                rows = max(1, min(500, int(payload.get("rows") or 24)))
                await proc.resize(cols, rows)
            elif payload.get("action") == "meta" and ws is not None:
                await _send_meta(ws, proc)
        except (ValueError, KeyError, ProcessError):
            pass
    elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
        return False
    return True


async def _send_initial_view(
    ws: web.WebSocketResponse,
    proc: WorkerProcess,
    *,
    terminal_cfg,
) -> None:
    """Atomically subscribe to the PTY stream and send the snapshot.

    Subscription MUST happen in the same synchronous step that captures
    the snapshot — otherwise output emitted between capture and
    subscribe is written to the ring buffer but never enqueued for
    this WS, producing a "terminal shows the snapshot but no live
    updates" lock-up that only a page reload clears.

    Previously this function subscribed AFTER two async sends, which
    left a ~milliseconds-to-seconds window during which any PTY output
    was silently lost to this client.  If the worker was idle past
    that window there was no new output to trigger a catch-up and the
    terminal appeared frozen.

    The fix uses ``subscribe_and_snapshot`` — a purely synchronous
    sequence that takes the ring buffer snapshot and registers the WS
    subscriber with no ``await`` between them, making it impossible
    for ``feed_output`` to interleave.
    """
    # If replay scrollback is requested and the local buffer is empty
    # (e.g. right after a daemon restart), pre-populate it from the
    # holder BEFORE the atomic subscribe step.  This write happens on
    # the same event loop, so feed_output can still interleave — but
    # the atomic subscribe_and_snapshot call below closes the window.
    if terminal_cfg.replay_scrollback and not proc.buffer.snapshot():
        try:
            await proc.get_replay_snapshot()
        except Exception:
            _log.debug("holder replay snapshot failed; continuing", exc_info=True)

    snapshot = proc.subscribe_and_snapshot(ws) if terminal_cfg.replay_scrollback else b""
    if not terminal_cfg.replay_scrollback:
        # Still need to subscribe even when replay is disabled.
        proc.subscribe_ws(ws)

    if snapshot:
        await ws.send_bytes(snapshot)
    await _send_meta(ws, proc)


async def _send_initial_view_best_effort(
    ws: web.WebSocketResponse,
    proc: WorkerProcess,
    *,
    terminal_cfg,
) -> None:
    """Send initial replay, but fall back to live-only attach if it stalls."""
    try:
        await asyncio.wait_for(
            _send_initial_view(ws, proc, terminal_cfg=terminal_cfg),
            timeout=_INITIAL_VIEW_TIMEOUT,
        )
    except TimeoutError:
        _log.warning(
            "terminal initial view timed out; falling back to live attach: worker=%s",
            proc.name,
        )
        # Ensure we're subscribed BEFORE any further async calls so we
        # don't lose output to the same race that took down the main
        # path.  subscribe_ws is an idempotent set-add — safe to call
        # even if the main path already subscribed via
        # subscribe_and_snapshot before it was cancelled.
        proc.subscribe_ws(ws)
        await _send_meta(ws, proc)
    except Exception:
        _log.warning(
            "terminal initial view failed; falling back to live attach: worker=%s",
            proc.name,
            exc_info=True,
        )
        proc.subscribe_ws(ws)
        await _send_meta(ws, proc)


async def _send_meta(ws: web.WebSocketResponse, proc: WorkerProcess) -> None:
    """Send lightweight terminal metadata for debug overlays."""
    try:
        await ws.send_str(json.dumps({"meta": "term", "alt": proc.buffer.in_alternate_screen}))
    except Exception:
        _log.debug("Failed to send terminal metadata", exc_info=True)


def _validate_terminal_request(
    request: web.Request,
) -> tuple[object, object, set[str]] | web.Response:
    """Validate auth, concurrency, and worker.  Returns (daemon, worker, sessions) or Response."""
    from swarm.server.helpers import get_daemon as _get_daemon

    auth_err = _check_auth(request)
    if auth_err is not None:
        return auth_err

    daemon = _get_daemon(request)

    sessions: set = request.app.setdefault("_terminal_sessions", set())
    if len(sessions) >= _MAX_TERMINAL_SESSIONS:
        return web.json_response({"error": "Too many terminal sessions"}, status=503)

    worker_name = request.query.get("worker", "")
    if not worker_name:
        return web.json_response({"error": "Missing 'worker' query parameter"}, status=400)

    worker = daemon.get_worker(worker_name)
    if not worker:
        return web.json_response({"error": f"Worker '{worker_name}' not found"}, status=404)

    return daemon, worker, sessions


async def handle_terminal_ws(request: web.Request) -> web.WebSocketResponse:
    """WebSocket endpoint for interactive terminal access to a worker.

    Sends a rendered screen snapshot for immediate content, then subscribes
    to the live PTY output stream.
    """
    result = _validate_terminal_request(request)
    if isinstance(result, web.Response):
        return result
    daemon, worker, sessions = result

    session_key = f"pty-{worker.name}-{uuid.uuid4().hex[:12]}"
    sessions.add(session_key)

    proc = worker.process
    if not proc:
        sessions.discard(session_key)
        return web.json_response({"error": "Worker has no active process"}, status=503)

    ws = web.WebSocketResponse(heartbeat=20.0)
    await ws.prepare(request)

    # Authenticate via first-message or deprecated query-param token.
    from swarm.server.api import get_api_password, ws_authenticate

    if not await ws_authenticate(ws, request, get_api_password(daemon)):
        # ws_authenticate now records the failure internally only when
        # the token was actually wrong (not on protocol-level timeout
        # / malformed message).
        sessions.discard(session_key)
        return ws

    daemon.hub.terminal_ws_clients.add(ws)
    proc.set_terminal_active(True)
    attach_t = time.monotonic()
    _log.warning(
        "[term-trace] terminal attach: worker=%s ws_id=%d session=%s",
        worker.name,
        id(ws),
        session_key,
    )

    try:
        try:
            cols = request.query.get("cols")
            rows = request.query.get("rows")
            if cols and rows:
                c = max(1, min(500, int(cols)))
                r = max(1, min(500, int(rows)))
                await proc.resize(c, r)
        except (ValueError, ProcessError):
            pass
        await _send_initial_view_best_effort(
            ws,
            proc,
            terminal_cfg=daemon.config.terminal,
        )
        _log.warning(
            "[term-trace] terminal initial view sent: worker=%s ws_id=%d elapsed=%.2fs",
            worker.name,
            id(ws),
            time.monotonic() - attach_t,
        )

        async for msg in ws:
            if not await _handle_ws_message(msg, ws, proc):
                break
    finally:
        proc.unsubscribe_ws(ws)
        if not proc.has_ws_subscribers:
            proc.set_terminal_active(False)
        daemon.hub.terminal_ws_clients.discard(ws)
        sessions.discard(session_key)
        _log.warning(
            "[term-trace] terminal detached: worker=%s ws_id=%d session_alive=%.2fs ws.closed=%s",
            worker.name,
            id(ws),
            time.monotonic() - attach_t,
            ws.closed,
        )
        if not ws.closed:
            await ws.close()

    return ws
