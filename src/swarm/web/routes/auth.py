"""OAuth routes: Microsoft Graph and Jira login/callback/status/disconnect."""

from __future__ import annotations

from aiohttp import web

from swarm.server.daemon import console_log
from swarm.server.helpers import get_daemon


async def handle_graph_login(request: web.Request) -> web.Response:
    """Start OAuth flow: generate PKCE, redirect to Microsoft."""
    import secrets as _secrets

    d = get_daemon(request)
    if not d.graph_mgr:
        return web.Response(text="Graph not configured — set client_id in Config", status=400)

    from swarm.auth.graph import generate_pkce_verifier

    state = _secrets.token_urlsafe(16)
    verifier = generate_pkce_verifier()
    d._graph_auth_pending[state] = verifier

    auth_url = d.graph_mgr.get_auth_url(state, verifier)
    raise web.HTTPFound(auth_url)


async def handle_graph_callback(request: web.Request) -> web.Response:
    """OAuth callback -- exchange code for tokens, redirect to /config."""
    d = get_daemon(request)
    code = request.query.get("code", "")
    state = request.query.get("state", "")
    error = request.query.get("error", "")

    if error:
        return web.Response(
            text=f"Microsoft auth error: {error} — {request.query.get('error_description', '')}",
            status=400,
        )

    if not code or not state:
        return web.Response(text="Missing code or state parameter", status=400)

    verifier = d._graph_auth_pending.pop(state, None)
    if not verifier:
        return web.Response(
            text="Auth state expired (server was likely restarted). "
            'Go back to Config → Integrations and click "Connect Microsoft Account" again.',
            status=400,
        )

    if not d.graph_mgr:
        return web.Response(text="Graph not configured", status=400)

    ok = await d.graph_mgr.exchange_code(code, verifier)
    if not ok:
        detail = d.graph_mgr.last_error or "unknown error"
        console_log(f"Graph token exchange failed: {detail}", level="error")
        return web.Response(
            text=f"Token exchange failed: {detail}",
            content_type="text/plain",
            status=400,
        )

    console_log("Microsoft Graph connected")
    raise web.HTTPFound("/config")


async def handle_graph_status(request: web.Request) -> web.Response:
    """Return Graph connection status as JSON."""
    d = get_daemon(request)
    if not d.graph_mgr:
        return web.json_response({"connected": False, "configured": False})
    return web.json_response({"connected": d.graph_mgr.is_connected(), "configured": True})


async def handle_graph_disconnect(request: web.Request) -> web.Response:
    """Disconnect Microsoft Graph (delete tokens)."""
    d = get_daemon(request)
    if d.graph_mgr:
        d.graph_mgr.disconnect()
        console_log("Microsoft Graph disconnected")
    return web.json_response({"status": "disconnected"})


async def handle_jira_login(request: web.Request) -> web.Response:
    """Start Jira OAuth flow: redirect to Atlassian."""
    import secrets as _secrets

    d = get_daemon(request)
    if not d.jira_mgr:
        return web.Response(text="Jira OAuth not configured — set client_id in Config", status=400)

    state = _secrets.token_urlsafe(16)
    d._jira_auth_pending[state] = state  # CSRF token

    auth_url = d.jira_mgr.get_auth_url(state)
    raise web.HTTPFound(auth_url)


async def handle_jira_callback(request: web.Request) -> web.Response:
    """Jira OAuth callback -- exchange code for tokens, redirect to /config."""
    d = get_daemon(request)
    code = request.query.get("code", "")
    state = request.query.get("state", "")
    error = request.query.get("error", "")

    if error:
        return web.Response(
            text=f"Jira auth error: {error} — {request.query.get('error_description', '')}",
            status=400,
        )

    if not code or not state:
        return web.Response(text="Missing code or state parameter", status=400)

    stored = d._jira_auth_pending.pop(state, None)
    if not stored:
        return web.Response(
            text="Auth state expired (server was likely restarted). "
            'Go back to Config → Integrations and click "Connect Jira" again.',
            status=400,
        )

    if not d.jira_mgr:
        return web.Response(text="Jira OAuth not configured", status=400)

    ok = await d.jira_mgr.exchange_code(code)
    if not ok:
        detail = d.jira_mgr.last_error or "unknown error"
        console_log(f"Jira token exchange failed: {detail}", level="error")
        return web.Response(
            text=f"Token exchange failed: {detail}",
            content_type="text/plain",
            status=400,
        )

    # Persist cloud_id into config
    if d.jira_mgr.cloud_id:
        d.config.jira.cloud_id = d.jira_mgr.cloud_id

    console_log("Jira OAuth connected")
    raise web.HTTPFound("/config")


async def handle_jira_auth_status(request: web.Request) -> web.Response:
    """Return Jira OAuth connection status as JSON."""
    d = get_daemon(request)
    if not d.jira_mgr:
        return web.json_response(
            {
                "connected": False,
                "configured": False,
            }
        )
    # `enabled` is reported alongside `connected` because they are INDEPENDENT and the
    # UI showed only the first. A user authorised OAuth, saw a green "Connected — OAuth
    # active" banner, and then every action failed with "Jira integration not enabled" —
    # which is a different flag (the integration's enabled checkbox) that every
    # /api/jira/* route gates on. The banner could not say so because this payload never
    # carried it, so the screen contradicted itself with no way to tell where to look.
    jira = getattr(d, "jira", None)
    return web.json_response(
        {
            "connected": d.jira_mgr.is_connected(),
            "configured": True,
            "cloud_id": d.jira_mgr.cloud_id,
            "enabled": bool(getattr(jira, "enabled", False)),
        }
    )


async def handle_jira_disconnect(request: web.Request) -> web.Response:
    """Disconnect Jira OAuth (delete tokens)."""
    d = get_daemon(request)
    if d.jira_mgr:
        d.jira_mgr.disconnect()
        console_log("Jira OAuth disconnected")
    return web.json_response({"status": "disconnected"})


def register(app: web.Application) -> None:
    """Register auth routes."""
    # Microsoft Graph OAuth
    app.router.add_get("/auth/graph/login", handle_graph_login)
    app.router.add_get("/auth/graph/callback", handle_graph_callback)
    app.router.add_get("/auth/graph/status", handle_graph_status)
    app.router.add_post("/auth/graph/disconnect", handle_graph_disconnect)
    # Jira OAuth
    app.router.add_get("/auth/jira/login", handle_jira_login)
    app.router.add_get("/auth/jira/callback", handle_jira_callback)
    app.router.add_get("/auth/jira/status", handle_jira_auth_status)
    app.router.add_post("/auth/jira/disconnect", handle_jira_disconnect)
