"""REST + WebSocket API server for the swarm daemon.

This module contains the ``create_app`` factory, middleware, and shared
auth/rate-limit helpers.  Route handlers live in ``swarm.server.routes.*``.
"""

from __future__ import annotations

import os
import secrets
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from aiohttp import web

from swarm.auth.password import verify_password
from swarm.logging import get_logger
from swarm.server.helpers import get_daemon, json_error

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("server.api")

_RATE_LIMIT_REQUESTS = 60  # per minute
_RATE_LIMIT_WINDOW = 60  # seconds
_RATE_LIMIT_CLEANUP_INTERVAL = 300  # evict stale IPs every 5 minutes
_RATE_LIMIT_MAX_IPS = 10_000  # absolute cap on tracked IPs
_rate_limit_last_cleanup: float = 0.0

# Paths that require authentication for mutating methods
_CONFIG_AUTH_PREFIX = "/api/config"

# Auto-generated token for sessions where no api_password is configured.
# Generated once per process; logged on startup so the operator can see it.
_auto_token: str = secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# Shared auth / origin helpers (used by routes)
# ---------------------------------------------------------------------------


def get_client_ip(request: web.Request) -> str:
    """Get client IP, respecting X-Forwarded-For only when trust_proxy is enabled."""
    daemon = get_daemon(request)
    if daemon.config.trust_proxy:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            if len(parts) >= 2:
                return parts[-2]
            if parts:
                return parts[0]
    return request.remote or "unknown"


def get_api_password(daemon: SwarmDaemon) -> str:
    """Get API password from config, environment, or auto-generated token."""
    return os.environ.get("SWARM_API_PASSWORD") or daemon.config.api_password or _auto_token


def has_explicit_password(daemon: SwarmDaemon) -> bool:
    """Return True if an explicit password is configured (not the auto-token)."""
    return bool(os.environ.get("SWARM_API_PASSWORD") or daemon.config.api_password)


def is_same_origin(request: web.Request, origin: str) -> bool:
    """Check if the Origin header matches the request host, domain, or tunnel URL."""
    if not origin:
        return True
    req_host = request.host.split(":")[0] if request.host else ""
    parsed = urlparse(origin)
    origin_host = parsed.hostname or ""
    if origin_host in ("localhost", "127.0.0.1") or origin_host == req_host:
        return True
    daemon = get_daemon(request)
    # Check configured domain (for reverse proxy setups)
    if daemon.config.domain and origin_host == daemon.config.domain:
        return True
    tunnel_url = daemon.tunnel.url
    if tunnel_url:
        tunnel_parsed = urlparse(tunnel_url)
        if origin_host == (tunnel_parsed.hostname or ""):
            return True
    return False


def check_origin_or_error(request: web.Request) -> web.Response | None:
    """Validate the Origin header against host / domain / tunnel.url.

    Returns ``None`` on success (header missing OR same-origin), or a
    403 ``web.Response`` on failure.  Every reject is logged at WARNING
    level with the offending origin, the request host, and the request
    path, so origin-mismatch failures are diagnosable from server logs
    alone.

    Phase E of the duplication-cluster sweep collapsed three near-
    identical inline copies of this check (``_csrf_middleware`` in
    ``server.api``, ``_check_auth`` in ``pty.bridge``, ``_check_ws_access``
    in ``server.routes.websocket``) onto this single helper.  Pre-Phase-E
    only the WS site logged on reject — the CSRF middleware and pty
    bridge silently returned 403 with no server-side anchor, so a
    misconfigured reverse proxy looked exactly like a client bug.
    """
    origin = request.headers.get("Origin", "")
    if origin and not is_same_origin(request, origin):
        _log.warning(
            "origin reject: origin=%r host=%r path=%s",
            origin,
            request.host,
            request.path,
        )
        return web.Response(status=403, text="Origin rejected")
    return None


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


@web.middleware
async def _config_auth_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Require Bearer token or valid session cookie for mutating config endpoints."""
    if request.path.startswith(_CONFIG_AUTH_PREFIX) and request.method in ("PUT", "POST", "DELETE"):
        daemon = get_daemon(request)
        password = get_api_password(daemon)

        # Accept valid session cookie (set by login page)
        from swarm.auth.session import _COOKIE_NAME, verify_session_cookie

        cookie = request.cookies.get(_COOKIE_NAME, "")
        if verify_session_cookie(cookie, password):
            return await handler(request)

        # Fall back to Bearer token (API / programmatic access)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not verify_password(auth[7:], password):
            return json_error("Unauthorized", 401)
    return await handler(request)


@web.middleware
async def _csrf_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Reject cross-origin mutating requests.

    ``/share-receive`` is exempt from the origin check because the OS
    share sheet (iOS / Android Web Share Target) initiates the POST,
    not a page — Origin lands as ``null``. The session cookie still
    travels with the PWA so the session-auth middleware still gates
    access; we trust the cookie as the auth signal.
    """
    if request.method in ("POST", "PUT", "DELETE"):
        if request.path != "/share-receive":
            if (resp := check_origin_or_error(request)) is not None:
                return resp
        if (
            request.path.startswith("/api/") or request.path.startswith("/action/")
        ) and not request.headers.get("X-Requested-With"):
            return web.Response(status=403, text="Missing X-Requested-With header")
    return await handler(request)


@web.middleware
async def _security_headers_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Add security headers to all responses."""
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    nonce = request.get("csp_nonce")
    if nonce:
        script_src = f"'self' 'nonce-{nonce}' https://unpkg.com https://cdn.jsdelivr.net"
    else:
        script_src = "'self' https://unpkg.com https://cdn.jsdelivr.net"
    response.headers.setdefault(
        "Content-Security-Policy",
        f"default-src 'self'; "
        f"script-src {script_src}; "
        f"style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        f"img-src 'self' data: blob:; "
        f"font-src 'self' data:; "
        f"connect-src 'self' ws: wss: https://cdn.jsdelivr.net https://unpkg.com; "
        f"frame-ancestors 'self'",
    )
    if request.secure:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    # Cache headers for static assets
    path = request.path
    if path.startswith("/static/") or path.startswith("/uploads/"):
        response.headers.setdefault("Cache-Control", "public, max-age=300")
    elif not path.startswith("/api/") and not path.startswith("/ws"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@web.middleware
async def _rate_limit_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Simple in-memory rate limiter: N requests/minute per client IP."""
    if request.method == "GET" or request.path in ("/ws", "/ws/terminal"):
        return await handler(request)

    ip = get_client_ip(request)
    now = time.time()
    rate_limits: dict[str, deque[float]] = request.app["rate_limits"]
    timestamps = rate_limits[ip]
    cutoff = now - _RATE_LIMIT_WINDOW
    while timestamps and timestamps[0] <= cutoff:
        timestamps.popleft()

    remaining = _RATE_LIMIT_REQUESTS - len(timestamps)
    first_ts = timestamps[0] if timestamps else now
    reset_at = int(first_ts + _RATE_LIMIT_WINDOW)

    if remaining <= 0:
        resp = json_error("Rate limit exceeded. Try again later.", 429)
        resp.headers["X-RateLimit-Limit"] = str(_RATE_LIMIT_REQUESTS)
        resp.headers["X-RateLimit-Remaining"] = "0"
        resp.headers["X-RateLimit-Reset"] = str(reset_at)
        resp.headers["Retry-After"] = str(max(1, reset_at - int(now)))
        return resp

    timestamps.append(now)

    global _rate_limit_last_cleanup
    if now - _rate_limit_last_cleanup > _RATE_LIMIT_CLEANUP_INTERVAL:
        _rate_limit_last_cleanup = now
        stale = [k for k, v in rate_limits.items() if not v or v[-1] < cutoff]
        for k in stale:
            del rate_limits[k]
        if len(rate_limits) > _RATE_LIMIT_MAX_IPS:
            import heapq

            excess = len(rate_limits) - _RATE_LIMIT_MAX_IPS
            oldest = heapq.nsmallest(
                excess,
                rate_limits,
                key=lambda k: rate_limits[k][-1] if rate_limits[k] else 0,
            )
            for k in oldest:
                rate_limits.pop(k, None)

    resp = await handler(request)
    resp.headers["X-RateLimit-Limit"] = str(_RATE_LIMIT_REQUESTS)
    resp.headers["X-RateLimit-Remaining"] = str(max(0, remaining - 1))
    resp.headers["X-RateLimit-Reset"] = str(reset_at)
    return resp


# ---------------------------------------------------------------------------
# Request ID middleware — attach + echo X-Request-ID on every request
# ---------------------------------------------------------------------------


@web.middleware
async def _request_id_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Attach a request ID, time the request, and log structured metrics."""
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request["request_id"] = rid
    start = time.monotonic()
    response = await handler(request)
    elapsed_ms = (time.monotonic() - start) * 1000
    response.headers["X-Request-ID"] = rid
    response.headers["X-Response-Time"] = f"{elapsed_ms:.1f}ms"
    # Structured access log for API/action routes (skip static/ws noise)
    path = request.path
    if not path.startswith("/static/") and path not in ("/ws", "/ws/terminal"):
        _log.debug(
            "request %s %s %d %.1fms",
            request.method,
            path,
            response.status,
            elapsed_ms,
            extra={
                "request_id": rid,
                "method": request.method,
                "path": path,
                "status": response.status,
                "latency_ms": round(elapsed_ms, 1),
            },
        )
    return response


# ---------------------------------------------------------------------------
# Session auth middleware — gates all routes except login/static/OAuth
# ---------------------------------------------------------------------------
_SESSION_IDLE_TIMEOUT = 24 * 3600  # 24 hours
_session_last_active: dict[str, float] = {}  # cookie → last request timestamp

# Paths exempt from session auth (no login required)
_SESSION_AUTH_EXEMPT: set[str] = {
    "/login",
    "/logout",
    "/ready",
    "/sw.js",
    "/manifest.json",
    "/bee-icon.svg",
    "/offline.html",
    "/favicon.ico",
    "/auth/webauthn/login/options",
    "/auth/webauthn/login/verify",
    "/api/tasks/cross",  # local-only hook ingestion — CSRF middleware still applies
    "/api/hooks/approval",  # PreToolUse hook — local Claude Code process
    "/api/hooks/session-end",  # SessionEnd hook — local Claude Code process
    "/api/hooks/event",  # lifecycle event hooks — local Claude Code process
    "/ws",  # WebSocket — has its own first-message auth
    "/ws/terminal",  # terminal WS — has its own first-message auth
    # NOTE: /mcp is intentionally NOT exempt — it can dispatch tasks into
    # worker PTYs (RCE-capable), so it is gated by a dedicated MCP bearer
    # token (see the /mcp branch in _session_auth_middleware). It was exempt
    # under a localhost-only assumption that broke once the daemon went on a
    # public tunnel.
}

# MCP HTTP endpoints — gated by a dedicated bearer token instead of the
# dashboard session (they can trigger code execution in worker PTYs).
_MCP_PATHS: tuple[str, ...] = ("/mcp", "/mcp/sse", "/mcp/message")
_SESSION_AUTH_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/static/",
    "/auth/graph/callback",
    "/auth/jira/callback",
    "/.well-known/",  # MCP SDK probes OAuth discovery — must get 404, not 401
)


@web.middleware
async def _session_auth_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Require a valid session cookie or Bearer token for all routes.

    Skipped entirely when no explicit api_password is configured — preserves
    backward-compatible open access for local/unprotected installs.
    """
    daemon = get_daemon(request)

    # No explicit password → no session gate (backward-compatible open access)
    if not has_explicit_password(daemon):
        return await handler(request)

    path = request.path

    # Exempt paths
    if path in _SESSION_AUTH_EXEMPT or path.startswith(_SESSION_AUTH_EXEMPT_PREFIXES):
        return await handler(request)

    password = get_api_password(daemon)

    # Check session cookie
    from swarm.auth.session import _COOKIE_NAME, verify_session_cookie

    cookie = request.cookies.get(_COOKIE_NAME, "")
    if verify_session_cookie(cookie, password):
        now = time.time()
        last = _session_last_active.get(cookie, now)
        if now - last > _SESSION_IDLE_TIMEOUT:
            _session_last_active.pop(cookie, None)
            # Session expired due to inactivity
            accept = request.headers.get("Accept", "")
            if "text/html" in accept:
                raise web.HTTPFound("/login")
            return json_error("Session expired (idle timeout)", 401)
        _session_last_active[cookie] = now
        return await handler(request)

    # Check Bearer token (API / programmatic access)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer ") and verify_password(auth_header[7:], password):
        return await handler(request)

    # MCP endpoints additionally accept a dedicated MCP bearer token (separate
    # from the dashboard credential) so external MCP clients — and local
    # workers via their injected .mcp.json header — authenticate without ever
    # holding the dashboard password. Token may arrive as a Bearer header or a
    # ?token= query param (the latter for SSE GETs that can't set headers).
    if path in _MCP_PATHS:
        from swarm.auth.mcp_token import verify_mcp_token

        mcp_tok = (
            auth_header[7:]
            if auth_header.startswith("Bearer ")
            else request.rel_url.query.get("token", "")
        )
        if verify_mcp_token(mcp_tok):
            return await handler(request)

    # Not authenticated — redirect browsers, 401 for API.
    # NOTE: no WWW-Authenticate header on the 401 — Claude Code's MCP client
    # starts an OAuth discovery dance when the server advertises auth, and we
    # want a plain, quiet 401 that simply blocks the caller.
    accept = request.headers.get("Accept", "")
    if "text/html" in accept:
        raise web.HTTPFound("/login")
    return json_error("Unauthorized", 401)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(daemon: SwarmDaemon, enable_web: bool = True) -> web.Application:
    """Create the aiohttp application with all routes."""
    app = web.Application(
        client_max_size=20 * 1024 * 1024,  # 20 MB for file uploads
        middlewares=[
            _request_id_middleware,
            _session_auth_middleware,
            _security_headers_middleware,
            _csrf_middleware,
            _rate_limit_middleware,
            _config_auth_middleware,
        ],
    )
    app["daemon"] = daemon
    app["rate_limits"] = defaultdict(deque)  # ip -> deque of timestamps

    # Web dashboard routes (before API to allow / to serve dashboard)
    if enable_web:
        from swarm.web.app import setup_web_routes

        setup_web_routes(app)

    # Register all API + WebSocket routes from domain modules
    from swarm.server.routes import register_all

    register_all(app)

    return app


# ---------------------------------------------------------------------------
# Re-exports for backward compatibility (tests, bridge.py)
# ---------------------------------------------------------------------------
from swarm.server.routes.websocket import (  # noqa: E402, F401
    _WS_AUTH_LOCKOUT_SECONDS,
    _WS_AUTH_MAX_FAILURES,
    _handle_ws_command,
    _is_ws_auth_locked,
    _ws_auth_failures,
    record_ws_auth_failure,
    ws_authenticate,
)
