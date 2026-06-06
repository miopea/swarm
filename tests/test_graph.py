"""Tests for Microsoft Graph OAuth token manager."""

import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from swarm.auth.graph import GraphTokenManager, _pkce_challenge, generate_pkce_verifier


@pytest.fixture()
def token_file(tmp_path, monkeypatch):
    """Redirect token storage to a temp directory."""
    p = tmp_path / "graph_tokens.json"
    monkeypatch.setattr("swarm.auth.graph._TOKEN_PATH", p)
    return p


class TestPKCE:
    def test_verifier_length(self):
        v = generate_pkce_verifier()
        assert len(v) == 43

    def test_verifier_is_url_safe(self):
        v = generate_pkce_verifier()
        assert all(c.isalnum() or c in "-_" for c in v)

    def test_challenge_is_base64url(self):
        v = generate_pkce_verifier()
        c = _pkce_challenge(v)
        assert "=" not in c  # no padding
        assert all(ch.isalnum() or ch in "-_" for ch in c)

    def test_challenge_deterministic(self):
        v = "test-verifier-12345678901234567890123"
        assert _pkce_challenge(v) == _pkce_challenge(v)


class TestTokenManagerLoad:
    def test_load_no_file(self, token_file):
        mgr = GraphTokenManager("client-id")
        assert not mgr.is_connected()
        assert mgr._access_token is None

    def test_load_existing_tokens(self, token_file):
        token_file.write_text(
            json.dumps(
                {
                    "access_token": "at123",
                    "refresh_token": "rt456",
                    "expires_at": time.time() + 3600,
                }
            )
        )
        mgr = GraphTokenManager("client-id")
        assert mgr.is_connected()
        assert mgr._access_token == "at123"
        assert mgr._refresh_token == "rt456"

    def test_load_corrupt_file(self, token_file):
        token_file.write_text("not json {{{")
        mgr = GraphTokenManager("client-id")
        assert not mgr.is_connected()


class TestTokenManagerSave:
    def test_save_creates_file(self, token_file):
        mgr = GraphTokenManager("client-id")
        mgr._access_token = "at"
        mgr._refresh_token = "rt"
        mgr._expires_at = 9999999999.0
        mgr._save()
        assert token_file.exists()
        data = json.loads(token_file.read_text())
        assert data["access_token"] == "at"
        assert data["refresh_token"] == "rt"

    def test_roundtrip(self, token_file):
        mgr1 = GraphTokenManager("cid")
        mgr1._access_token = "a"
        mgr1._refresh_token = "r"
        mgr1._expires_at = 12345.0
        mgr1._save()

        mgr2 = GraphTokenManager("cid")
        assert mgr2._access_token == "a"
        assert mgr2._refresh_token == "r"
        assert mgr2._expires_at == 12345.0


class TestDisconnect:
    def test_disconnect_clears_state(self, token_file):
        token_file.write_text(
            json.dumps({"access_token": "a", "refresh_token": "r", "expires_at": 99999})
        )
        mgr = GraphTokenManager("cid")
        assert mgr.is_connected()

        mgr.disconnect()
        assert not mgr.is_connected()
        assert not token_file.exists()

    def test_disconnect_no_file(self, token_file):
        mgr = GraphTokenManager("cid")
        mgr.disconnect()  # should not raise
        assert not mgr.is_connected()


class TestGetAuthUrl:
    def test_url_contains_required_params(self, token_file):
        mgr = GraphTokenManager("my-client-id", "my-tenant")
        url = mgr.get_auth_url("state123", "verifier123")
        assert "my-client-id" in url
        assert "my-tenant" in url
        assert "state123" in url
        assert "code_challenge=" in url
        assert "S256" in url
        assert "Mail.Read" in url


class TestExchangeCode:
    @pytest.mark.asyncio()
    async def test_exchange_code_success(self, token_file):
        mgr = GraphTokenManager("cid")

        async def fake_token_request(url, data):
            mgr._access_token = "new_at"
            mgr._refresh_token = "new_rt"
            mgr._expires_at = time.time() + 3600
            mgr._save()
            return True

        with patch.object(mgr, "_token_request", side_effect=fake_token_request):
            result = await mgr.exchange_code("code123", "verifier123")

        assert result is True
        assert mgr._access_token == "new_at"
        assert mgr._refresh_token == "new_rt"
        assert mgr.is_connected()

    @pytest.mark.asyncio()
    async def test_exchange_code_failure(self, token_file):
        mgr = GraphTokenManager("cid")

        with patch.object(mgr, "_token_request", new_callable=AsyncMock, return_value=False):
            result = await mgr.exchange_code("bad-code", "verifier")

        assert result is False
        assert not mgr.is_connected()


class TestGetToken:
    @pytest.mark.asyncio()
    async def test_returns_cached_when_valid(self, token_file):
        mgr = GraphTokenManager("cid")
        mgr._access_token = "cached"
        mgr._refresh_token = "rt"
        mgr._expires_at = time.time() + 3600

        token = await mgr.get_token()
        assert token == "cached"

    @pytest.mark.asyncio()
    async def test_returns_none_when_not_connected(self, token_file):
        mgr = GraphTokenManager("cid")
        token = await mgr.get_token()
        assert token is None

    @pytest.mark.asyncio()
    async def test_refreshes_when_expired(self, token_file):
        mgr = GraphTokenManager("cid")
        mgr._refresh_token = "rt"
        mgr._expires_at = time.time() - 100  # expired

        async def fake_refresh(url, data):
            mgr._access_token = "refreshed"
            mgr._refresh_token = "new_rt"
            mgr._expires_at = time.time() + 3600
            return True

        with patch.object(mgr, "_token_request", side_effect=fake_refresh):
            token = await mgr.get_token()

        assert token == "refreshed"

    @pytest.mark.asyncio()
    async def test_returns_none_when_refresh_fails(self, token_file):
        mgr = GraphTokenManager("cid")
        mgr._refresh_token = "rt"
        mgr._expires_at = time.time() - 100  # expired

        with patch.object(mgr, "_token_request", new_callable=AsyncMock, return_value=False):
            token = await mgr.get_token()

        assert token is None


class TestConcurrentRefresh:
    @pytest.mark.asyncio
    async def test_concurrent_get_token_refreshes_once(self, token_file):
        """#auth-audit E: two concurrent get_token() on an expired token must
        refresh exactly once (lock + re-check) — a rotated refresh token would
        otherwise invalidate the loser."""
        import asyncio

        mgr = GraphTokenManager("client-id")
        mgr._refresh_token = "rt"
        calls = {"n": 0}

        async def fake_refresh() -> bool:
            calls["n"] += 1
            await asyncio.sleep(0)  # force the two callers to interleave
            mgr._access_token = "fresh-at"
            mgr._expires_at = time.time() + 3600
            return True

        mgr._refresh = fake_refresh  # type: ignore[method-assign]
        results = await asyncio.gather(mgr.get_token(), mgr.get_token())
        assert results == ["fresh-at", "fresh-at"]
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_get_token_returns_none_on_refresh_failure(self, token_file):
        mgr = GraphTokenManager("client-id")
        mgr._refresh_token = "rt"
        mgr._refresh = AsyncMock(return_value=False)  # type: ignore[method-assign]
        assert await mgr.get_token() is None
