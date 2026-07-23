"""OAuth 2.0 authorization-server endpoints for the MCP connector.

Implements the discovery + authorize + token + dynamic-registration surface
that Claude Desktop's remote-MCP connector drives. The crypto/state lives in
``swarm.auth.oauth_server``; this module is the HTTP shell.

Endpoints (all registered at the domain root so RFC 8414/9728 discovery works):

* ``GET  /.well-known/oauth-protected-resource``  — resource metadata
* ``GET  /.well-known/oauth-authorization-server`` — AS metadata
* ``POST /oauth/register``   — Dynamic Client Registration (RFC 7591)
* ``GET  /oauth/authorize``  — auth-code grant; auto-approves on a valid
                                dashboard session, else bounces via /login
* ``POST /oauth/token``      — code→token exchange (PKCE) and refresh

These paths are exempt from the session-auth gate (``/authorize`` does its own
operator check; the rest are public with client/PKCE auth) and from the CSRF
origin check (they are cross-origin by nature).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

from aiohttp import web

from swarm.auth import oauth_server as oauth
from swarm.logging import get_logger
from swarm.server.helpers import get_daemon, handle_errors

_log = get_logger("routes.oauth")


def public_base_url(daemon: Any, request: web.Request) -> str:
    """Best-effort public origin (no trailing slash) for issuer/endpoint URLs.

    Prefers an explicitly configured domain, then the active tunnel URL, then
    the request's forwarded/host headers. This must match the host Claude
    reaches Swarm at, or discovery URLs won't resolve.
    """
    domain = (getattr(daemon.config, "domain", "") or "").strip()
    if domain:
        host = domain.split("://", 1)[-1].rstrip("/")
        return f"https://{host}"
    tunnel = getattr(daemon, "tunnel", None)
    turl = (getattr(tunnel, "url", "") or "").strip() if tunnel else ""
    if turl:
        return turl.rstrip("/")
    proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() or request.scheme
    return f"{proto}://{request.host}"


# ---------------------------------------------------------------------------
# Discovery metadata
# ---------------------------------------------------------------------------
@handle_errors
async def handle_protected_resource_metadata(request: web.Request) -> web.Response:
    base = public_base_url(get_daemon(request), request)
    return web.json_response(
        {
            "resource": base,
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
            "scopes_supported": [oauth.SCOPE],
            "resource_documentation": f"{base}/config",
        }
    )


@handle_errors
async def handle_authorization_server_metadata(request: web.Request) -> web.Response:
    base = public_base_url(get_daemon(request), request)
    return web.json_response(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post",
                "client_secret_basic",
                "none",
            ],
            "scopes_supported": [oauth.SCOPE],
        }
    )


# ---------------------------------------------------------------------------
# Dynamic Client Registration (RFC 7591)
# ---------------------------------------------------------------------------
@handle_errors
async def handle_register(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}

    # If the client declares redirect_uris, reject up-front any we won't
    # honour at /authorize — a clearer failure than a later redirect reject.
    redirect_uris = body.get("redirect_uris") if isinstance(body, dict) else None
    if isinstance(redirect_uris, list):
        for uri in redirect_uris:
            if not oauth.is_allowed_redirect(str(uri)):
                _log.warning("DCR rejected disallowed redirect_uri=%r", uri)
                return web.json_response(
                    {
                        "error": "invalid_redirect_uri",
                        "error_description": f"redirect_uri not allowed: {uri}",
                    },
                    status=400,
                )

    reg = oauth.register_client()
    if isinstance(redirect_uris, list):
        reg["redirect_uris"] = redirect_uris
    _log.info("OAuth client registered: %s", reg["client_id"])
    return web.json_response(reg, status=201)


# ---------------------------------------------------------------------------
# Authorization endpoint
# ---------------------------------------------------------------------------
def _error_redirect(redirect_uri: str, error: str, state: str, desc: str = "") -> web.Response:
    params = {"error": error}
    if desc:
        params["error_description"] = desc
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    raise web.HTTPFound(f"{redirect_uri}{sep}{urlencode(params)}")


def _operator_authenticated(request: web.Request) -> bool:
    from swarm.auth.session import _COOKIE_NAME, verify_session_cookie
    from swarm.server.api import get_api_password, has_explicit_password

    daemon = get_daemon(request)
    # No dashboard password ⇒ local/unexposed install ⇒ trusted operator.
    if not has_explicit_password(daemon):
        return True
    password = get_api_password(daemon)
    return verify_session_cookie(request.cookies.get(_COOKIE_NAME, ""), password)


@handle_errors
async def handle_authorize(request: web.Request) -> web.Response:
    q = request.query
    response_type = q.get("response_type", "")
    client_id = q.get("client_id", "")
    redirect_uri = q.get("redirect_uri", "")
    state = q.get("state", "")
    scope = q.get("scope", "") or oauth.SCOPE
    code_challenge = q.get("code_challenge", "")
    code_challenge_method = q.get("code_challenge_method", "")

    # redirect_uri is validated FIRST — everything else may error back to it,
    # but only if it's a host we trust (else we'd be an open redirector).
    if not oauth.is_allowed_redirect(redirect_uri):
        _log.warning("authorize rejected disallowed redirect_uri=%r", redirect_uri)
        return web.Response(
            status=400,
            text=(
                "Invalid or disallowed redirect_uri. If this is a legitimate "
                "client, add its host to the oauth_allowed_redirect_hosts secret."
            ),
        )
    if response_type != "code":
        return _error_redirect(redirect_uri, "unsupported_response_type", state)
    if not client_id:
        return _error_redirect(redirect_uri, "invalid_request", state, "client_id required")
    if not code_challenge or code_challenge_method != "S256":
        return _error_redirect(redirect_uri, "invalid_request", state, "S256 PKCE required")

    # Operator gate — auto-approve when logged into the dashboard, else login.
    if not _operator_authenticated(request):
        next_url = f"/oauth/authorize?{urlencode(dict(q))}"
        raise web.HTTPFound(f"/login?next={quote(next_url, safe='')}")

    code = oauth.issue_code(client_id, redirect_uri, code_challenge, scope)
    _log.info("OAuth code issued for client=%s", client_id)
    params = {"code": code}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    raise web.HTTPFound(f"{redirect_uri}{sep}{urlencode(params)}")


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------
def _client_secret_from(request: web.Request, form: dict[str, Any]) -> str:
    secret = str(form.get("client_secret", ""))
    if secret:
        return secret
    # HTTP Basic (client_secret_basic)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        import base64

        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8", "replace")
            if ":" in decoded:
                return decoded.split(":", 1)[1]
        except Exception:
            return ""
    return ""


def _token_response(client_id: str, scope: str, with_refresh: bool = True) -> web.Response:
    body: dict[str, Any] = {
        "access_token": oauth.mint_access_token(client_id, scope),
        "token_type": "Bearer",
        "expires_in": oauth.ACCESS_TOKEN_TTL,
        "scope": scope,
    }
    if with_refresh:
        body["refresh_token"] = oauth.mint_refresh_token(client_id, scope)
    # OAuth spec: token responses must not be cached.
    return web.json_response(body, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _token_error(error: str, desc: str = "", status: int = 400) -> web.Response:
    payload = {"error": error}
    if desc:
        payload["error_description"] = desc
    return web.json_response(payload, status=status, headers={"Cache-Control": "no-store"})


@handle_errors
async def handle_token(request: web.Request) -> web.Response:
    form = dict(await request.post())
    grant_type = str(form.get("grant_type", ""))

    if grant_type == "authorization_code":
        code = str(form.get("code", ""))
        redirect_uri = str(form.get("redirect_uri", ""))
        client_id = str(form.get("client_id", ""))
        code_verifier = str(form.get("code_verifier", ""))

        data = oauth.consume_code(code)
        if data is None:
            return _token_error("invalid_grant", "code invalid or expired")
        if client_id and data["client_id"] != client_id:
            return _token_error("invalid_grant", "client_id mismatch")
        if data["redirect_uri"] != redirect_uri:
            return _token_error("invalid_grant", "redirect_uri mismatch")
        if not oauth.verify_pkce(code_verifier, data["code_challenge"]):
            return _token_error("invalid_grant", "PKCE verification failed")

        # If a client_secret is presented it must be valid (confidential
        # client); public clients rely on PKCE and may omit it.
        secret = _client_secret_from(request, form)
        cid = client_id or data["client_id"]
        if secret and not oauth.verify_client_secret(cid, secret):
            return _token_error("invalid_client", "bad client credentials", status=401)

        return _token_response(cid, data["scope"])

    if grant_type == "refresh_token":
        refresh = str(form.get("refresh_token", ""))
        payload = oauth.verify_refresh_token(refresh)
        if payload is None:
            return _token_error("invalid_grant", "refresh token invalid or expired")
        return _token_response(str(payload["cid"]), str(payload.get("scp", oauth.SCOPE)))

    return _token_error("unsupported_grant_type", f"unsupported grant_type: {grant_type}")


# ---------------------------------------------------------------------------
# Operator-facing connection management (settings page)
# These live under /api/ so the session-auth + CSRF middleware gate them to a
# logged-in operator, unlike the public /oauth/* surface above.
# ---------------------------------------------------------------------------
def _connection_info(daemon: Any, request: web.Request) -> dict[str, Any]:
    from swarm.auth.mcp_token import get_or_create_mcp_token

    base = public_base_url(daemon, request)
    client_id, client_secret = oauth.get_static_client()
    return {
        "url": f"{base}/mcp",
        "token": get_or_create_mcp_token(),
        "client_id": client_id,
        "client_secret": client_secret,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
    }


@handle_errors
async def handle_connection_info(request: web.Request) -> web.Response:
    daemon = get_daemon(request)
    return web.json_response(_connection_info(daemon, request))


@handle_errors
async def handle_rotate_token(request: web.Request) -> web.Response:
    from swarm.auth.mcp_token import rotate_mcp_token

    daemon = get_daemon(request)
    token = rotate_mcp_token()
    # Push the new token into local workers' .mcp.json so they keep authing.
    try:
        daemon._write_worker_mcp_configs()
    except Exception:
        _log.warning("failed to rewrite worker .mcp.json after token rotate", exc_info=True)
    _log.info("MCP static token rotated by operator")
    return web.json_response({"token": token})


@handle_errors
async def handle_rotate_oauth(request: web.Request) -> web.Response:
    oauth.rotate_signing_key()
    _log.info("OAuth signing key rotated by operator (all OAuth tokens revoked)")
    client_id, client_secret = oauth.get_static_client()
    return web.json_response({"client_id": client_id, "client_secret": client_secret})


def register(app: web.Application) -> None:
    app.router.add_get("/.well-known/oauth-protected-resource", handle_protected_resource_metadata)
    app.router.add_get(
        "/.well-known/oauth-authorization-server", handle_authorization_server_metadata
    )
    app.router.add_post("/oauth/register", handle_register)
    app.router.add_get("/oauth/authorize", handle_authorize)
    app.router.add_post("/oauth/token", handle_token)
    # Operator-only management (settings page)
    app.router.add_get("/api/mcp/connection", handle_connection_info)
    app.router.add_post("/api/mcp/token/rotate", handle_rotate_token)
    app.router.add_post("/api/mcp/oauth/rotate", handle_rotate_oauth)
