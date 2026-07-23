"""OAuth 2.0 authorization-server flow for the MCP connector.

Covers discovery metadata, Dynamic Client Registration, the authorize→token
auth-code+PKCE exchange, refresh, and that an issued access token authenticates
to /mcp. Regression guard for the Claude Desktop "Connect" flow.
"""

from __future__ import annotations

import base64
import hashlib

import pytest
from aiohttp.test_utils import TestClient, TestServer

from swarm.auth import oauth_server as oauth
from swarm.auth.session import _COOKIE_NAME, create_session_cookie
from swarm.server.api import create_app
from tests.conftest import make_daemon

_PW = "dashboard-pw"
_REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _pkce() -> tuple[str, str]:
    verifier = _b64u(b"verifier-" + b"x" * 40)
    challenge = _b64u(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


@pytest.fixture(autouse=True)
def _fixed_signing(monkeypatch):
    """Pin the OAuth signing key (deterministic, no DB) and clear code state."""
    monkeypatch.setattr(oauth, "_signing_key", b"k" * 32)
    oauth._auth_codes.clear()
    yield
    oauth._auth_codes.clear()


@pytest.fixture
def daemon(monkeypatch):
    d = make_daemon(monkeypatch=monkeypatch)
    d.config.api_password = _PW
    return d


async def _client(daemon) -> TestClient:
    c = TestClient(TestServer(create_app(daemon, enable_web=False)))
    await c.start_server()
    return c


def _session_headers() -> dict[str, str]:
    cookie, _ = create_session_cookie(_PW)
    return {"Cookie": f"{_COOKIE_NAME}={cookie}"}


# ---------------------------------------------------------------------------
# oauth_server unit
# ---------------------------------------------------------------------------


def test_pkce_verify():
    verifier, challenge = _pkce()
    assert oauth.verify_pkce(verifier, challenge) is True
    assert oauth.verify_pkce("wrong", challenge) is False


def test_client_secret_roundtrip():
    cid, secret = oauth.get_static_client()
    assert oauth.verify_client_secret(cid, secret) is True
    assert oauth.verify_client_secret(cid, "bad") is False


def test_access_token_roundtrip():
    tok = oauth.mint_access_token("swarm-mcp")
    payload = oauth.verify_access_token(tok)
    assert payload is not None and payload["cid"] == "swarm-mcp"
    # A refresh token must not validate as an access token.
    assert oauth.verify_access_token(oauth.mint_refresh_token("swarm-mcp")) is None


def test_redirect_allowlist():
    assert oauth.is_allowed_redirect("https://claude.ai/cb") is True
    assert oauth.is_allowed_redirect("https://foo.anthropic.com/cb") is True
    assert oauth.is_allowed_redirect("http://localhost:1234/cb") is True
    assert oauth.is_allowed_redirect("https://evil.com/cb") is False
    assert oauth.is_allowed_redirect("http://claude.ai/cb") is False  # non-loopback must be https


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_metadata(daemon):
    c = await _client(daemon)
    try:
        r1 = await c.get("/.well-known/oauth-protected-resource")
        assert r1.status == 200
        pr = await r1.json()
        assert pr["authorization_servers"] and pr["resource"]

        r2 = await c.get("/.well-known/oauth-authorization-server")
        assert r2.status == 200
        md = await r2.json()
        assert md["authorization_endpoint"].endswith("/oauth/authorize")
        assert md["token_endpoint"].endswith("/oauth/token")
        assert "S256" in md["code_challenge_methods_supported"]
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_dynamic_client_registration(daemon):
    c = await _client(daemon)
    try:
        r = await c.post("/oauth/register", json={"redirect_uris": [_REDIRECT]})
        assert r.status == 201
        reg = await r.json()
        assert reg["client_id"] and reg["client_secret"]
        assert oauth.verify_client_secret(reg["client_id"], reg["client_secret"])

        bad = await c.post("/oauth/register", json={"redirect_uris": ["https://evil.com/cb"]})
        assert bad.status == 400
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Authorize
# ---------------------------------------------------------------------------


def _authorize_query(challenge: str) -> dict[str, str]:
    return {
        "response_type": "code",
        "client_id": "swarm-mcp",
        "redirect_uri": _REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
    }


@pytest.mark.asyncio
async def test_authorize_requires_login(daemon):
    _, challenge = _pkce()
    c = await _client(daemon)
    try:
        r = await c.get(
            "/oauth/authorize", params=_authorize_query(challenge), allow_redirects=False
        )
        assert r.status == 302
        assert r.headers["Location"].startswith("/login?next=")
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_authorize_rejects_bad_redirect(daemon):
    _, challenge = _pkce()
    q = _authorize_query(challenge)
    q["redirect_uri"] = "https://evil.com/cb"
    c = await _client(daemon)
    try:
        r = await c.get(
            "/oauth/authorize", params=q, headers=_session_headers(), allow_redirects=False
        )
        assert r.status == 400
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_authorize_renders_consent_when_logged_in(daemon):
    """A logged-in operator gets an explicit Approve/Deny page — not a silent
    auto-issued code."""
    _, challenge = _pkce()
    c = await _client(daemon)
    try:
        r = await c.get(
            "/oauth/authorize",
            params=_authorize_query(challenge),
            headers=_session_headers(),
            allow_redirects=False,
        )
        assert r.status == 200
        html = await r.text()
        assert "Approve" in html and "Deny" in html
        assert 'name="consent_token"' in html
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Full auth-code + PKCE → token → /mcp, and refresh
# ---------------------------------------------------------------------------


async def _consent_token(c: TestClient, challenge: str) -> str:
    import re

    r = await c.get(
        "/oauth/authorize",
        params=_authorize_query(challenge),
        headers=_session_headers(),
        allow_redirects=False,
    )
    html = await r.text()
    return re.search(r'name="consent_token" value="([^"]+)"', html).group(1)


async def _obtain_code(c: TestClient, challenge: str) -> str:
    from urllib.parse import parse_qs, urlparse

    token = await _consent_token(c, challenge)
    r = await c.post(
        "/oauth/consent",
        data={"consent_token": token, "decision": "approve"},
        headers=_session_headers(),
        allow_redirects=False,
    )
    return parse_qs(urlparse(r.headers["Location"]).query)["code"][0]


@pytest.mark.asyncio
async def test_consent_deny_redirects_with_error(daemon):
    _, challenge = _pkce()
    c = await _client(daemon)
    try:
        token = await _consent_token(c, challenge)
        r = await c.post(
            "/oauth/consent",
            data={"consent_token": token, "decision": "deny"},
            headers=_session_headers(),
            allow_redirects=False,
        )
        assert r.status == 302
        loc = r.headers["Location"]
        assert loc.startswith(_REDIRECT)
        assert "error=access_denied" in loc and "code=" not in loc
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_consent_rejects_forged_token(daemon):
    """A consent POST with a bogus (unsigned) token is refused — the signed
    token is what makes the Approve trustworthy."""
    c = await _client(daemon)
    try:
        r = await c.post(
            "/oauth/consent",
            data={"consent_token": "forged.token", "decision": "approve"},
            headers=_session_headers(),
            allow_redirects=False,
        )
        assert r.status == 400
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_token_exchange_and_mcp_access(daemon):
    verifier, challenge = _pkce()
    c = await _client(daemon)
    try:
        code = await _obtain_code(c, challenge)
        tok = await c.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _REDIRECT,
                "client_id": "swarm-mcp",
                "code_verifier": verifier,
            },
        )
        assert tok.status == 200
        body = await tok.json()
        access, refresh = body["access_token"], body["refresh_token"]
        assert body["token_type"] == "Bearer"

        # The access token authenticates to /mcp.
        mcp = await c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
        )
        assert mcp.status == 200

        # Refresh yields a fresh access token.
        rr = await c.post(
            "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh}
        )
        assert rr.status == 200
        assert (await rr.json())["access_token"]
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_token_rejects_bad_pkce_verifier(daemon):
    _, challenge = _pkce()
    c = await _client(daemon)
    try:
        code = await _obtain_code(c, challenge)
        tok = await c.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _REDIRECT,
                "client_id": "swarm-mcp",
                "code_verifier": "not-the-verifier",
            },
        )
        assert tok.status == 400
        assert (await tok.json())["error"] == "invalid_grant"
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_code_is_single_use(daemon):
    verifier, challenge = _pkce()
    c = await _client(daemon)
    try:
        code = await _obtain_code(c, challenge)
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _REDIRECT,
            "client_id": "swarm-mcp",
            "code_verifier": verifier,
        }
        first = await c.post("/oauth/token", data=form)
        assert first.status == 200
        second = await c.post("/oauth/token", data=form)
        assert second.status == 400  # code already consumed
    finally:
        await c.close()
