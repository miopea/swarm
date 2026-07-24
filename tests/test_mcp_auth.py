"""Auth gate on the MCP HTTP endpoints.

The ``/mcp`` Streamable-HTTP surface can dispatch tasks into worker PTYs
(RCE-capable), so it must be authenticated whenever the daemon carries an
explicit password (the "exposed" configuration). Local workers authenticate
via a dedicated MCP bearer token injected into their ``.mcp.json``.

Regression guard for the 2026-07 finding: ``/mcp`` was exempt from the
session-auth middleware under a localhost-only assumption and became an
anonymous-RCE hole once the daemon went on a public tunnel.
"""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from swarm.auth import mcp_token
from swarm.server.api import create_app
from tests.conftest import make_daemon

_TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
_ACCEPT = {"Accept": "application/json, text/event-stream"}
# The test client connects on loopback, which is now a trusted-local bypass. To
# exercise the PUBLIC auth gate, simulate a tunnel-forwarded request by adding a
# forwarding header (cloudflared always stamps X-Forwarded-For).
_REMOTE = {"Accept": "application/json, text/event-stream", "X-Forwarded-For": "203.0.113.9"}


@pytest.fixture(autouse=True)
def _fixed_token(monkeypatch):
    """Pin the MCP token so verification is deterministic and avoid the DB."""
    monkeypatch.setattr(mcp_token, "_cached", "test-mcp-token")
    yield


@pytest.fixture
def daemon(monkeypatch):
    return make_daemon(monkeypatch=monkeypatch)


async def _client(daemon) -> TestClient:
    app = create_app(daemon, enable_web=False)
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


# ---------------------------------------------------------------------------
# Token verification unit
# ---------------------------------------------------------------------------


def test_verify_mcp_token_matches_and_rejects():
    assert mcp_token.verify_mcp_token("test-mcp-token") is True
    assert mcp_token.verify_mcp_token("wrong") is False
    assert mcp_token.verify_mcp_token("") is False


def test_is_loopback_request():
    from types import SimpleNamespace

    from swarm.server.api import is_loopback_request

    def req(remote, headers=None):
        # is_loopback_request only reads .remote and .headers.
        return SimpleNamespace(remote=remote, headers=headers or {})

    # Genuine local: loopback peer, no forwarding headers.
    assert is_loopback_request(req("127.0.0.1")) is True
    assert is_loopback_request(req("::1")) is True
    # Tunnel/proxy: loopback peer but forwarding headers present → NOT local.
    assert is_loopback_request(req("127.0.0.1", {"X-Forwarded-For": "1.2.3.4"})) is False
    assert is_loopback_request(req("127.0.0.1", {"CF-Ray": "abc123"})) is False
    # LAN-direct: non-loopback peer → NOT local.
    assert is_loopback_request(req("10.0.0.5")) is False


# ---------------------------------------------------------------------------
# Middleware enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_open_when_no_password(daemon):
    """Backward-compat: a password-less (local, unexposed) install keeps /mcp
    open — the session middleware no-ops entirely without a password."""
    assert daemon.config.api_password in (None, "")
    c = await _client(daemon)
    try:
        resp = await c.post("/mcp", json=_TOOLS_LIST, headers=_ACCEPT)
        assert resp.status == 200
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_mcp_local_loopback_bypasses_token(daemon):
    """A genuine same-machine caller (no forwarding header) is trusted and
    reaches /mcp without any token, even with a password set — the token gate
    exists only for the public tunnel surface."""
    daemon.config.api_password = "dashboard-pw"
    c = await _client(daemon)
    try:
        resp = await c.post("/mcp", json=_TOOLS_LIST, headers=_ACCEPT)  # no X-Forwarded-For
        assert resp.status == 200
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_mcp_blocked_without_token_when_password_set(daemon):
    daemon.config.api_password = "dashboard-pw"
    c = await _client(daemon)
    try:
        resp = await c.post("/mcp", json=_TOOLS_LIST, headers=_REMOTE)
        assert resp.status == 401
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_mcp_allowed_with_bearer_token(daemon):
    daemon.config.api_password = "dashboard-pw"
    c = await _client(daemon)
    try:
        resp = await c.post(
            "/mcp",
            json=_TOOLS_LIST,
            headers={**_REMOTE, "Authorization": "Bearer test-mcp-token"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert "result" in body and "tools" in body["result"]
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_mcp_allowed_with_query_token(daemon):
    """SSE GETs and header-less clients can pass the token as ?token=."""
    daemon.config.api_password = "dashboard-pw"
    c = await _client(daemon)
    try:
        resp = await c.post("/mcp?token=test-mcp-token", json=_TOOLS_LIST, headers=_REMOTE)
        assert resp.status == 200
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_mcp_blocked_with_wrong_token(daemon):
    daemon.config.api_password = "dashboard-pw"
    c = await _client(daemon)
    try:
        resp = await c.post(
            "/mcp",
            json=_TOOLS_LIST,
            headers={**_REMOTE, "Authorization": "Bearer nope"},
        )
        assert resp.status == 401
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_401_advertises_oauth_resource_metadata(daemon):
    """The 401 carries a WWW-Authenticate pointing at the OAuth resource
    metadata so the MCP connector can discover the authorization server and
    start the auth-code flow."""
    daemon.config.api_password = "dashboard-pw"
    c = await _client(daemon)
    try:
        resp = await c.post("/mcp", json=_TOOLS_LIST, headers=_REMOTE)
        assert resp.status == 401
        www = resp.headers.get("WWW-Authenticate", "")
        assert www.startswith("Bearer ")
        assert "/.well-known/oauth-protected-resource" in www
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Worker .mcp.json injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_mcp_config_carries_auth_header(daemon, tmp_path):
    from swarm.worker.worker import Worker
    from tests.fakes.process import FakeWorkerProcess

    wdir = tmp_path / "proj"
    wdir.mkdir()
    daemon.workers = [Worker(name="w1", path=str(wdir), process=FakeWorkerProcess(name="w1"))]
    daemon.config.port = 9090

    daemon._write_worker_mcp_configs()

    written = json.loads((wdir / ".mcp.json").read_text())
    server = written["mcpServers"]["swarm"]
    assert server["headers"]["Authorization"] == "Bearer test-mcp-token"
    assert "?worker=w1" in server["url"]
