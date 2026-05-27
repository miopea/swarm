"""Tests for :mod:`swarm.web.routes.auth` — Microsoft Graph + Jira OAuth.

Pre-fill-in: ``auth.py`` sat at **13% coverage** — the OAuth
status/disconnect endpoints were exercised indirectly through the
config dashboard, but the login/callback handlers were unreached.

These tests mock the request + daemon directly (rather than spinning
up an aiohttp test app) since each handler is a thin async function
over ``request.query`` + ``daemon.graph_mgr`` / ``daemon.jira_mgr``.

Coverage gap closed in the 2026-05-27 test-gap fill-in, phase 3.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from swarm.web.routes import auth as auth_mod


def _make_request(daemon: MagicMock, query: dict[str, str] | None = None) -> MagicMock:
    """Build a stand-in for an aiohttp web.Request.

    Routes only touch ``request.query`` and ``request.app`` (the
    daemon back-ref via ``get_daemon``); MagicMock with those two
    attributes is enough.
    """
    request = MagicMock()
    request.query = query or {}
    # auth_mod.get_daemon is patched per-test to return ``daemon``.
    return request


# ---------------------------------------------------------------------------
# Microsoft Graph OAuth
# ---------------------------------------------------------------------------


class TestGraphLogin:
    @pytest.mark.asyncio
    async def test_returns_400_when_graph_unconfigured(self, monkeypatch) -> None:
        daemon = MagicMock()
        daemon.graph_mgr = None
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        request = _make_request(daemon)
        response = await auth_mod.handle_graph_login(request)
        assert response.status == 400
        assert "not configured" in response.text

    @pytest.mark.asyncio
    async def test_happy_path_redirects_and_stores_state(self, monkeypatch) -> None:
        daemon = MagicMock()
        daemon.graph_mgr.get_auth_url.return_value = "https://login.microsoft.example/oauth"
        daemon._graph_auth_pending = {}
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        request = _make_request(daemon)
        with pytest.raises(web.HTTPFound) as exc:
            await auth_mod.handle_graph_login(request)
        assert exc.value.location == "https://login.microsoft.example/oauth"
        # A pending state row was persisted for the upcoming callback
        assert len(daemon._graph_auth_pending) == 1


class TestGraphCallback:
    @pytest.mark.asyncio
    async def test_returns_400_when_query_carries_error(self, monkeypatch) -> None:
        daemon = MagicMock()
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        request = _make_request(daemon, {"error": "access_denied"})
        response = await auth_mod.handle_graph_callback(request)
        assert response.status == 400
        assert "access_denied" in response.text

    @pytest.mark.asyncio
    async def test_missing_code_or_state_returns_400(self, monkeypatch) -> None:
        daemon = MagicMock()
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        request = _make_request(daemon, {"code": ""})  # state missing
        response = await auth_mod.handle_graph_callback(request)
        assert response.status == 400
        assert "Missing code or state" in response.text

    @pytest.mark.asyncio
    async def test_expired_state_returns_400(self, monkeypatch) -> None:
        """A state that's not in ``_graph_auth_pending`` — server restarted."""
        daemon = MagicMock()
        daemon._graph_auth_pending = {}  # No matching state
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        request = _make_request(daemon, {"code": "abc", "state": "stale"})
        response = await auth_mod.handle_graph_callback(request)
        assert response.status == 400
        assert "expired" in response.text.lower()

    @pytest.mark.asyncio
    async def test_graph_unconfigured_after_valid_state_returns_400(self, monkeypatch) -> None:
        daemon = MagicMock()
        daemon._graph_auth_pending = {"s": "v"}
        daemon.graph_mgr = None
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        request = _make_request(daemon, {"code": "abc", "state": "s"})
        response = await auth_mod.handle_graph_callback(request)
        assert response.status == 400
        assert "not configured" in response.text

    @pytest.mark.asyncio
    async def test_exchange_failure_returns_400_with_detail(self, monkeypatch) -> None:
        daemon = MagicMock()
        daemon._graph_auth_pending = {"s": "v"}
        daemon.graph_mgr.exchange_code = AsyncMock(return_value=False)
        daemon.graph_mgr.last_error = "invalid_grant"
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        request = _make_request(daemon, {"code": "abc", "state": "s"})
        response = await auth_mod.handle_graph_callback(request)
        assert response.status == 400
        assert "invalid_grant" in response.text

    @pytest.mark.asyncio
    async def test_happy_path_redirects_to_config(self, monkeypatch) -> None:
        daemon = MagicMock()
        daemon._graph_auth_pending = {"s": "v"}
        daemon.graph_mgr.exchange_code = AsyncMock(return_value=True)
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        request = _make_request(daemon, {"code": "abc", "state": "s"})
        with pytest.raises(web.HTTPFound) as exc:
            await auth_mod.handle_graph_callback(request)
        assert exc.value.location == "/config"


class TestGraphStatusDisconnect:
    @pytest.mark.asyncio
    async def test_status_unconfigured(self, monkeypatch) -> None:
        daemon = MagicMock()
        daemon.graph_mgr = None
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        response = await auth_mod.handle_graph_status(_make_request(daemon))
        assert response.status == 200
        import json

        payload = json.loads(response.text)
        assert payload == {"connected": False, "configured": False}

    @pytest.mark.asyncio
    async def test_status_connected(self, monkeypatch) -> None:
        daemon = MagicMock()
        daemon.graph_mgr.is_connected.return_value = True
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        response = await auth_mod.handle_graph_status(_make_request(daemon))
        import json

        payload = json.loads(response.text)
        assert payload == {"connected": True, "configured": True}

    @pytest.mark.asyncio
    async def test_disconnect_clears_tokens(self, monkeypatch) -> None:
        daemon = MagicMock()
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        response = await auth_mod.handle_graph_disconnect(_make_request(daemon))
        assert response.status == 200
        daemon.graph_mgr.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_noop_when_no_graph_mgr(self, monkeypatch) -> None:
        daemon = MagicMock()
        daemon.graph_mgr = None
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        response = await auth_mod.handle_graph_disconnect(_make_request(daemon))
        assert response.status == 200


# ---------------------------------------------------------------------------
# Jira OAuth (mirror Graph behaviour but with cloud_id persistence)
# ---------------------------------------------------------------------------


class TestJiraLogin:
    @pytest.mark.asyncio
    async def test_returns_400_when_jira_unconfigured(self, monkeypatch) -> None:
        daemon = MagicMock()
        daemon.jira_mgr = None
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        response = await auth_mod.handle_jira_login(_make_request(daemon))
        assert response.status == 400
        assert "not configured" in response.text

    @pytest.mark.asyncio
    async def test_happy_path_redirects(self, monkeypatch) -> None:
        daemon = MagicMock()
        daemon.jira_mgr.get_auth_url.return_value = "https://auth.atlassian.example/oauth"
        daemon._jira_auth_pending = {}
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        with pytest.raises(web.HTTPFound) as exc:
            await auth_mod.handle_jira_login(_make_request(daemon))
        assert exc.value.location == "https://auth.atlassian.example/oauth"
        assert len(daemon._jira_auth_pending) == 1


class TestJiraCallback:
    @pytest.mark.asyncio
    async def test_error_query_returns_400(self, monkeypatch) -> None:
        daemon = MagicMock()
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        response = await auth_mod.handle_jira_callback(
            _make_request(daemon, {"error": "access_denied"})
        )
        assert response.status == 400

    @pytest.mark.asyncio
    async def test_missing_code_returns_400(self, monkeypatch) -> None:
        daemon = MagicMock()
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        response = await auth_mod.handle_jira_callback(_make_request(daemon, {}))
        assert response.status == 400

    @pytest.mark.asyncio
    async def test_expired_state_returns_400(self, monkeypatch) -> None:
        daemon = MagicMock()
        daemon._jira_auth_pending = {}
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        response = await auth_mod.handle_jira_callback(
            _make_request(daemon, {"code": "abc", "state": "missing"})
        )
        assert response.status == 400
        assert "expired" in response.text.lower()

    @pytest.mark.asyncio
    async def test_exchange_failure_returns_400(self, monkeypatch) -> None:
        daemon = MagicMock()
        daemon._jira_auth_pending = {"s": "csrf"}
        daemon.jira_mgr.exchange_code = AsyncMock(return_value=False)
        daemon.jira_mgr.last_error = "invalid_request"
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        response = await auth_mod.handle_jira_callback(
            _make_request(daemon, {"code": "abc", "state": "s"})
        )
        assert response.status == 400
        assert "invalid_request" in response.text

    @pytest.mark.asyncio
    async def test_happy_path_persists_cloud_id_and_redirects(self, monkeypatch) -> None:
        daemon = MagicMock()
        daemon._jira_auth_pending = {"s": "csrf"}
        daemon.jira_mgr.exchange_code = AsyncMock(return_value=True)
        daemon.jira_mgr.cloud_id = "cloud-abc"
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        with pytest.raises(web.HTTPFound) as exc:
            await auth_mod.handle_jira_callback(
                _make_request(daemon, {"code": "abc", "state": "s"})
            )
        assert exc.value.location == "/config"
        # The cloud_id was persisted to config
        assert daemon.config.jira.cloud_id == "cloud-abc"


class TestJiraStatusDisconnect:
    @pytest.mark.asyncio
    async def test_status_unconfigured(self, monkeypatch) -> None:
        daemon = MagicMock()
        daemon.jira_mgr = None
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        response = await auth_mod.handle_jira_auth_status(_make_request(daemon))
        import json

        payload = json.loads(response.text)
        assert payload == {"connected": False, "configured": False}

    @pytest.mark.asyncio
    async def test_status_connected_includes_cloud_id(self, monkeypatch) -> None:
        daemon = MagicMock()
        daemon.jira_mgr.is_connected.return_value = True
        daemon.jira_mgr.cloud_id = "cloud-xyz"
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        response = await auth_mod.handle_jira_auth_status(_make_request(daemon))
        import json

        payload = json.loads(response.text)
        assert payload["connected"] is True
        assert payload["configured"] is True
        assert payload["cloud_id"] == "cloud-xyz"

    @pytest.mark.asyncio
    async def test_disconnect_clears_tokens(self, monkeypatch) -> None:
        daemon = MagicMock()
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        response = await auth_mod.handle_jira_disconnect(_make_request(daemon))
        assert response.status == 200
        daemon.jira_mgr.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_noop_when_no_jira_mgr(self, monkeypatch) -> None:
        daemon = MagicMock()
        daemon.jira_mgr = None
        monkeypatch.setattr(auth_mod, "get_daemon", lambda _r: daemon)
        response = await auth_mod.handle_jira_disconnect(_make_request(daemon))
        assert response.status == 200
