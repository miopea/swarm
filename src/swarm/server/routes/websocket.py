"""WebSocket routes — main WS and terminal WS."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from swarm.auth.password import verify_password
from swarm.logging import get_logger
from swarm.server.api import check_origin_or_error, get_api_password, get_client_ip
from swarm.server.helpers import get_daemon

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("server.routes.websocket")

_MAX_WS_PER_IP = 10
_WS_AUTH_MAX_FAILURES = 5
_WS_AUTH_LOCKOUT_SECONDS = 300  # 5 minutes
_ws_auth_failures: dict[str, list[float]] = {}


def register(app: web.Application) -> None:
    # Serve uploaded files
    uploads_dir = Path.home() / ".swarm" / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    app.router.add_static("/uploads/", uploads_dir)

    app.router.add_get("/ws", handle_websocket)

    from swarm.pty.bridge import handle_terminal_ws

    app.router.add_get("/ws/terminal", handle_terminal_ws)


def _is_ws_auth_locked(ip: str) -> bool:
    """Return True if IP has exceeded max failed WS auth attempts."""
    now = time.time()
    cutoff = now - _WS_AUTH_LOCKOUT_SECONDS
    timestamps = _ws_auth_failures.get(ip)
    if not timestamps:
        return False
    recent = [t for t in timestamps if t > cutoff]
    if recent:
        _ws_auth_failures[ip] = recent
    else:
        _ws_auth_failures.pop(ip, None)
        return False
    return len(recent) >= _WS_AUTH_MAX_FAILURES


def record_ws_auth_failure(ip: str) -> None:
    """Record a failed WS auth attempt for rate limiting."""
    _ws_auth_failures.setdefault(ip, []).append(time.time())
    cutoff = time.time() - _WS_AUTH_LOCKOUT_SECONDS
    if len(_ws_auth_failures) > 50:
        stale = [k for k, v in _ws_auth_failures.items() if not v or v[-1] < cutoff]
        for k in stale:
            _ws_auth_failures.pop(k, None)


def _ws_decrement(ws_ip_counts: dict[str, int], ip: str) -> None:
    """Decrement per-IP WebSocket connection count."""
    count = ws_ip_counts.get(ip, 0)
    if count > 1:
        ws_ip_counts[ip] = count - 1
    else:
        ws_ip_counts.pop(ip, None)


async def ws_authenticate(ws: web.WebSocketResponse, request: web.Request, password: str) -> bool:
    """Authenticate a WebSocket via first-message auth or deprecated query param.

    Returns True on success, False on failure.  When the failure is an
    *actual wrong token* (deliberate bad credential), the caller's IP
    is recorded toward the per-IP lockout via ``record_ws_auth_failure``.
    Protocol-level failures — auth-message timeout, non-text frame,
    malformed JSON, missing ``type`` field — do NOT count toward the
    lockout: they're transient transport issues (slow tunnel, lost
    message) and shouldn't lock an operator out for 5 minutes.

    Reported through Cloudflare tunnel: dashboard's main /ws kept
    failing handshake after a brief tunnel hiccup, while /ws/terminal
    (which doesn't go through the lockout) stayed working.  Root
    cause: ws_authenticate counted timeouts as wrong-token failures,
    so 5 transient transport blips locked the operator out of the
    main app for 5 minutes.
    """
    query_token = request.query.get("token", "")
    if query_token:
        _log.warning("WS auth via ?token= query param is deprecated — use first-message auth")
        if verify_password(query_token, password):
            return True
        await ws.close(code=4001, message=b"Unauthorized")
        # Wrong query-param token = real auth failure.
        from swarm.server.api import get_client_ip

        ip = get_client_ip(request)
        _log.warning(
            "WS auth FAIL (wrong-token, query-param): path=%s ip=%s — lockout counter incremented",
            request.path,
            ip,
        )
        record_ws_auth_failure(ip)
        return False

    try:
        msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
    except TimeoutError:
        # Protocol-level: client never sent the auth frame in time.
        # Likely tunnel hiccup.  Don't count toward lockout.
        await ws.close(code=4001, message=b"Auth timeout")
        return False

    if msg.type != web.WSMsgType.TEXT:
        # Protocol-level: malformed connection.  Don't count.
        await ws.close(code=4001, message=b"Expected auth message")
        return False

    try:
        auth = json.loads(msg.data)
    except json.JSONDecodeError:
        # Protocol-level: malformed body.  Don't count.
        await ws.close(code=4001, message=b"Invalid auth message")
        return False

    if auth.get("type") != "auth" or not verify_password(auth.get("token", ""), password):
        # This is a real auth failure: client sent a well-formed auth
        # frame but the token didn't match.  Lockout-eligible.
        await ws.close(code=4001, message=b"Unauthorized")
        from swarm.server.api import get_client_ip

        ip = get_client_ip(request)
        token_value = auth.get("token", "")
        token_summary = (
            "<empty>" if not token_value else f"len={len(token_value)} prefix={token_value[:8]!r}"
        )
        _log.warning(
            "WS auth FAIL (wrong-token, first-message): path=%s ip=%s "
            "msg_type=%r token=%s — lockout counter incremented",
            request.path,
            ip,
            auth.get("type"),
            token_summary,
        )
        record_ws_auth_failure(ip)
        return False

    return True


def _check_ws_access(request: web.Request) -> web.Response | None:
    """Validate origin and rate limit for WebSocket upgrade.

    Every reject path now emits a WARNING-level log with the offending
    IP and the reason — pre-fix the handler returned 403/429
    silently, leaving operators with a "WebSocket connection ...
    failed:" browser console message and zero server-side context.
    The auth-lockout fix in 2026.5.5.3 closed one rejection path;
    this logging makes the remaining ones (origin mismatch, per-IP
    cap) diagnosable on the next reproduction.
    """
    if (resp := check_origin_or_error(request)) is not None:
        return resp

    ip = get_client_ip(request)
    if _is_ws_auth_locked(ip):
        _log.warning(
            "WS reject: ip=%s is in 5-minute auth-lockout window — "
            "5+ wrong-token failures recorded recently",
            ip,
        )
        return web.Response(status=429, text="Too many failed auth attempts")
    ws_ip_counts: dict[str, int] = request.app.setdefault("_ws_ip_counts", {})
    current = ws_ip_counts.get(ip, 0)
    if current >= _MAX_WS_PER_IP:
        _log.warning(
            "WS reject: ip=%s has %d open /ws connections (cap=%d) — "
            "either the browser is holding stale tabs or a counter leaked",
            ip,
            current,
            _MAX_WS_PER_IP,
        )
        return web.Response(status=429, text="Too many WebSocket connections")
    return None


async def handle_websocket(request: web.Request) -> web.WebSocketResponse:
    rejection = _check_ws_access(request)
    if rejection is not None:
        return rejection

    d = get_daemon(request)

    ip = get_client_ip(request)
    ws_ip_counts: dict[str, int] = request.app.setdefault("_ws_ip_counts", {})
    ws_ip_counts[ip] = ws_ip_counts.get(ip, 0) + 1

    # Wrap everything that follows in a single outer try/finally so the
    # IP counter is ALWAYS decremented, regardless of which path exits
    # (auth fail, ws.prepare() raising mid-handshake, exception in the
    # main loop, etc.).  Previously an exception between the increment
    # and the inner try block — e.g. ws.prepare() being cancelled on a
    # hung client connection — leaked the counter permanently.  After
    # enough leaks per IP the rate limiter (_MAX_WS_PER_IP) rejects
    # every subsequent connection with 429 "Too many WebSocket
    # connections" and the dashboard cannot reconnect until the daemon
    # is restarted.
    ws = web.WebSocketResponse(heartbeat=20.0)
    try:
        await ws.prepare(request)

        if not await ws_authenticate(ws, request, get_api_password(d)):
            # ws_authenticate now records the failure internally only
            # when the token was actually wrong (not on protocol-level
            # timeout / malformed message).  See its docstring.
            return ws

        _log.info("WebSocket client connected")

        d.hub.ws_clients.add(ws)
        try:
            pending_proposals = d.proposal_store.pending
            init_payload: dict[str, object] = {
                "type": "init",
                "workers": [{"name": w.name, "state": w.display_state.value} for w in d.workers],
                "drones_enabled": d.pilot.enabled if d.pilot else False,
                "proposals": [d.proposal_dict(p) for p in pending_proposals],
                "proposal_count": len(pending_proposals),
                "queen_queue": d.queen_queue.status(),
                "test_mode": hasattr(d, "_test_log"),
                "test_run_id": d._test_log.run_id if hasattr(d, "_test_log") else None,
            }
            if getattr(d, "_update_result", None) is not None:
                from swarm.update import update_result_to_dict

                init_payload["update"] = update_result_to_dict(d._update_result)
            await ws.send_json(init_payload)

            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        await ws.send_json({"type": "error", "message": "invalid JSON"})
                        continue
                    try:
                        await _handle_ws_command(d, ws, data)
                    except Exception:
                        _log.exception("error handling WS command: %s", data.get("command", ""))
                        await ws.send_json({"type": "error", "message": "internal error"})
                elif msg.type == web.WSMsgType.ERROR:
                    _log.warning("WebSocket error: %s", ws.exception())
        finally:
            d.hub.ws_clients.discard(ws)
            _log.info("WebSocket client disconnected")
    finally:
        _ws_decrement(ws_ip_counts, ip)

    return ws


_ALLOWED_WS_COMMANDS = {"refresh", "toggle_drones", "focus"}


async def _handle_ws_command(
    d: SwarmDaemon, ws: web.WebSocketResponse, data: dict[str, Any]
) -> None:
    """Handle a command received over WebSocket."""
    cmd = data.get("command", "")

    if cmd not in _ALLOWED_WS_COMMANDS:
        await ws.send_json({"type": "error", "message": f"unknown command: {cmd}"})
        return

    if cmd == "refresh":
        await ws.send_json(
            {
                "type": "state",
                "workers": [
                    {
                        "name": w.name,
                        "state": w.display_state.value,
                        "state_duration": round(w.state_duration, 1),
                    }
                    for w in d.workers
                ],
            }
        )
    elif cmd == "toggle_drones":
        if d.pilot:
            new_state = d.toggle_drones()
            await ws.send_json({"type": "drones_toggled", "enabled": new_state})
    elif cmd == "focus":
        worker_name = data.get("worker", "")
        if d.pilot:
            d.pilot.set_focused_workers({worker_name} if worker_name else set())
