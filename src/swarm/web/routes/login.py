"""Login, logout, and WebAuthn passkey routes."""

from __future__ import annotations

import json
import secrets
import time
from collections import deque
from typing import Any

import aiohttp_jinja2
from aiohttp import web

from swarm.auth.passkeys import PasskeyStore
from swarm.auth.password import verify_password
from swarm.auth.session import _COOKIE_NAME, create_session_cookie, verify_session_cookie
from swarm.auth.webauthn import (
    credential_id_to_base64url,
    generate_authentication_options,
    generate_registration_options,
    verify_authentication,
    verify_registration,
)
from swarm.server.helpers import get_daemon, json_error

_LOGIN_MAX_FAILURES = 5
_LOGIN_LOCKOUT_SECONDS = 900  # 15 minutes


def _safe_next(raw: str) -> str:
    """Sanitize a post-login redirect target to a same-site path.

    Only local absolute paths are honoured (``/oauth/authorize?...`` for the
    MCP OAuth flow). Anything protocol-relative or absolute-URL is dropped to
    prevent an open redirect.
    """
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return ""


# IP -> deque of failure timestamps
_login_failures: dict[str, deque[float]] = {}


def _get_rp_id(request: web.Request) -> str:
    """Resolve WebAuthn Relying Party ID from config or request host."""
    daemon = get_daemon(request)
    if daemon.config.domain:
        return daemon.config.domain
    host = request.host or "localhost"
    return host.split(":")[0]


def _get_expected_origin(request: web.Request) -> list[str]:
    """Build list of acceptable origins for WebAuthn verification."""
    origins = []
    host = request.host or "localhost"
    # Support both http and https for the request host
    origins.append(f"https://{host}")
    origins.append(f"http://{host}")
    # Also accept the bare hostname without port
    hostname = host.split(":")[0]
    if hostname != host:
        origins.append(f"https://{hostname}")
        origins.append(f"http://{hostname}")
    return origins


def _get_password(request: web.Request) -> str:
    """Get the effective API password."""
    from swarm.server.api import get_api_password

    return get_api_password(get_daemon(request))


def _get_client_ip(request: web.Request) -> str:
    from swarm.server.api import get_client_ip

    return get_client_ip(request)


def _is_login_locked(ip: str) -> bool:
    """Check if an IP is locked out from login attempts."""
    timestamps = _login_failures.get(ip)
    if not timestamps:
        return False
    now = time.time()
    cutoff = now - _LOGIN_LOCKOUT_SECONDS
    while timestamps and timestamps[0] < cutoff:
        timestamps.popleft()
    return len(timestamps) >= _LOGIN_MAX_FAILURES


def _record_login_failure(ip: str) -> None:
    if ip not in _login_failures:
        _login_failures[ip] = deque()
    _login_failures[ip].append(time.time())


def _clear_login_failures(ip: str) -> None:
    _login_failures.pop(ip, None)


def _set_session_cookie(response: web.Response, password: str, secure: bool) -> None:
    """Set the session cookie on a response."""
    value, max_age = create_session_cookie(password)
    response.set_cookie(
        _COOKIE_NAME,
        value,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="Lax",
        path="/",
    )


def _passkey_store(request: web.Request) -> PasskeyStore:
    """Get or create the PasskeyStore from app state."""
    store = request.app.get("passkey_store")
    if store is None:
        store = PasskeyStore()
        request.app["passkey_store"] = store
    return store


# -----------------------------------------------------------------------
# Login / Logout page routes
# -----------------------------------------------------------------------


@aiohttp_jinja2.template("login.html")
async def handle_login_page(request: web.Request) -> dict[str, Any]:
    """Render the login page."""
    from swarm.server.api import has_explicit_password

    # No password configured → login page is meaningless, go to dashboard
    daemon = get_daemon(request)
    if not has_explicit_password(daemon):
        raise web.HTTPFound("/")

    nonce = secrets.token_urlsafe(16)
    request["csp_nonce"] = nonce
    password = _get_password(request)
    next_url = _safe_next(request.query.get("next", ""))

    # If already authenticated, redirect to the requested target (or dashboard)
    cookie = request.cookies.get(_COOKIE_NAME, "")
    if verify_session_cookie(cookie, password):
        raise web.HTTPFound(next_url or "/")

    store = _passkey_store(request)
    has_passkeys = store.has_any()
    error = request.query.get("error", "")
    return {
        "has_passkeys": has_passkeys,
        "error": error,
        "csp_nonce": nonce,
        "rp_id": _get_rp_id(request),
        "next": next_url,
    }


async def handle_login_post(request: web.Request) -> web.Response:
    """Handle password login form submission."""
    ip = _get_client_ip(request)
    if _is_login_locked(ip):
        raise web.HTTPFound("/login?error=Too+many+attempts.+Try+again+in+15+minutes.")

    data = await request.post()
    submitted = str(data.get("password", ""))
    password = _get_password(request)
    next_url = _safe_next(str(data.get("next", "")))

    if not verify_password(submitted, password):
        _record_login_failure(ip)
        err = "/login?error=Invalid+password"
        if next_url:
            from urllib.parse import quote

            err += f"&next={quote(next_url, safe='')}"
        raise web.HTTPFound(err)

    _clear_login_failures(ip)
    response = web.HTTPFound(next_url or "/")
    _set_session_cookie(response, password, request.secure)
    raise response


async def handle_logout(request: web.Request) -> web.Response:
    """Clear session cookie and redirect to login."""
    response = web.HTTPFound("/login")
    response.del_cookie(_COOKIE_NAME, path="/")
    raise response


# -----------------------------------------------------------------------
# WebAuthn registration (requires existing session)
# -----------------------------------------------------------------------


async def handle_webauthn_register_options(request: web.Request) -> web.Response:
    """Generate registration challenge. Requires authenticated session."""
    password = _get_password(request)
    cookie = request.cookies.get(_COOKIE_NAME, "")
    if not verify_session_cookie(cookie, password):
        return json_error("Unauthorized", 401)

    store = _passkey_store(request)
    rp_id = _get_rp_id(request)
    options_json, token = generate_registration_options(
        rp_id=rp_id,
        password=password,
        existing_credentials=store.get_all(),
    )
    # Store the challenge token in a short-lived cookie
    resp = web.Response(
        text=json.dumps({"options": json.loads(options_json), "token": token}),
        content_type="application/json",
    )
    return resp


async def handle_webauthn_register_verify(request: web.Request) -> web.Response:
    """Verify attestation and store credential."""
    password = _get_password(request)
    cookie = request.cookies.get(_COOKIE_NAME, "")
    if not verify_session_cookie(cookie, password):
        return json_error("Unauthorized", 401)

    body = await request.json()
    token = body.get("token", "")
    credential_response = body.get("credential", {})
    device_name = str(body.get("device_name", "")).strip() or "Unknown device"

    rp_id = _get_rp_id(request)
    try:
        cred = verify_registration(rp_id, token, credential_response, _get_expected_origin(request))
    except Exception as e:
        return json_error(f"Registration failed: {e}")

    cred.device_name = device_name
    store = _passkey_store(request)
    store.add(cred)
    return web.json_response({"status": "ok", "device_name": device_name})


# -----------------------------------------------------------------------
# WebAuthn authentication (login flow)
# -----------------------------------------------------------------------


async def handle_webauthn_login_options(request: web.Request) -> web.Response:
    """Generate authentication challenge for passkey login."""
    store = _passkey_store(request)
    creds = store.get_all()
    if not creds:
        return json_error("No passkeys registered")

    rp_id = _get_rp_id(request)
    options_json, token = generate_authentication_options(rp_id, creds)
    return web.json_response({"options": json.loads(options_json), "token": token})


async def handle_webauthn_login_verify(request: web.Request) -> web.Response:
    """Verify assertion and set session cookie."""
    ip = _get_client_ip(request)
    if _is_login_locked(ip):
        return json_error("Too many attempts. Try again in 15 minutes.", 429)

    body = await request.json()
    token = body.get("token", "")
    credential_response = body.get("credential", {})

    store = _passkey_store(request)
    creds = store.get_all()
    rp_id = _get_rp_id(request)

    # Find the credential that matches the response
    raw_id = credential_response.get("rawId") or credential_response.get("id", "")
    from webauthn.helpers import base64url_to_bytes

    try:
        response_cred_id = base64url_to_bytes(raw_id)
    except Exception:
        _record_login_failure(ip)
        return json_error("Invalid credential")

    matched = None
    for c in creds:
        if c.credential_id == response_cred_id:
            matched = c
            break

    if not matched:
        _record_login_failure(ip)
        return json_error("Unknown credential")

    try:
        new_count = verify_authentication(
            rp_id, token, matched, credential_response, _get_expected_origin(request)
        )
    except Exception as e:
        _record_login_failure(ip)
        return json_error(f"Authentication failed: {e}")

    store.update_sign_count(matched.credential_id, new_count)
    _clear_login_failures(ip)

    password = _get_password(request)
    next_url = _safe_next(str(body.get("next", "")))
    resp = web.json_response({"status": "ok", "redirect": next_url or "/"})
    _set_session_cookie(resp, password, request.secure)
    return resp


# -----------------------------------------------------------------------
# Passkey management (authenticated)
# -----------------------------------------------------------------------


async def handle_passkey_list(request: web.Request) -> web.Response:
    """List registered passkeys (for config page)."""
    store = _passkey_store(request)
    creds = store.get_all()
    return web.json_response(
        [
            {
                "credential_id": credential_id_to_base64url(c.credential_id),
                "device_name": c.device_name,
                "registered_at": c.registered_at,
            }
            for c in creds
        ]
    )


async def handle_passkey_delete(request: web.Request) -> web.Response:
    """Delete a registered passkey."""
    body = await request.json()
    cred_id_b64 = body.get("credential_id", "")
    if not cred_id_b64:
        return json_error("credential_id required")
    from webauthn.helpers import base64url_to_bytes

    try:
        cred_id = base64url_to_bytes(cred_id_b64)
    except Exception:
        return json_error("Invalid credential_id")

    store = _passkey_store(request)
    store.remove(cred_id)
    return web.json_response({"status": "ok"})


# -----------------------------------------------------------------------
# Password change (authenticated)
# -----------------------------------------------------------------------


async def handle_change_password(request: web.Request) -> web.Response:
    """Change the API password. Requires current session + current password."""
    password = _get_password(request)
    cookie = request.cookies.get(_COOKIE_NAME, "")
    if not verify_session_cookie(cookie, password):
        return json_error("Unauthorized", 401)

    body = await request.json()
    current = str(body.get("current_password", ""))
    new_pw = str(body.get("new_password", ""))

    if not new_pw or len(new_pw) < 8:
        return json_error("New password must be at least 8 characters")

    # Verify current password
    if not verify_password(current, password):
        return json_error("Current password is incorrect")

    # Hash and save new password
    from swarm.auth.password import hash_password
    from swarm.config.serialization import save_config

    daemon = get_daemon(request)
    new_hash = hash_password(new_pw)
    daemon.config.api_password = new_hash
    save_config(daemon.config)

    # Set new session cookie (old ones auto-invalidate since signing key changed)
    resp = web.json_response({"status": "ok"})
    _set_session_cookie(resp, new_hash, request.secure)
    return resp


# -----------------------------------------------------------------------
# Route registration
# -----------------------------------------------------------------------


def register(app: web.Application) -> None:
    """Register login/logout and WebAuthn routes."""
    app.router.add_get("/login", handle_login_page)
    app.router.add_post("/login", handle_login_post)
    app.router.add_post("/logout", handle_logout)
    # WebAuthn registration (requires session)
    app.router.add_post("/auth/webauthn/register/options", handle_webauthn_register_options)
    app.router.add_post("/auth/webauthn/register/verify", handle_webauthn_register_verify)
    # WebAuthn login (no session required)
    app.router.add_post("/auth/webauthn/login/options", handle_webauthn_login_options)
    app.router.add_post("/auth/webauthn/login/verify", handle_webauthn_login_verify)
    # Passkey management (requires session, checked by middleware)
    app.router.add_get("/auth/passkeys", handle_passkey_list)
    app.router.add_post("/auth/passkeys/delete", handle_passkey_delete)
    # Password change
    app.router.add_post("/auth/password/change", handle_change_password)
