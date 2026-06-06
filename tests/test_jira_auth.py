"""Tests for Jira OAuth 2.0 (3LO) token manager."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from swarm.auth.jira import _TOKEN_PATH, JiraTokenManager


@pytest.fixture()
def _clean_tokens():
    """Remove token file before and after each test."""
    if _TOKEN_PATH.exists():
        _TOKEN_PATH.unlink()
    yield
    if _TOKEN_PATH.exists():
        _TOKEN_PATH.unlink()


@pytest.mark.usefixtures("_clean_tokens")
class TestJiraTokenManager:
    def test_not_connected_initially(self) -> None:
        mgr = JiraTokenManager("cid", "csecret")
        assert mgr.is_connected() is False

    def test_cloud_id_empty_initially(self) -> None:
        mgr = JiraTokenManager("cid", "csecret")
        assert mgr.cloud_id == ""
        assert mgr.api_base_url == ""

    def test_api_base_url_with_cloud_id(self) -> None:
        mgr = JiraTokenManager("cid", "csecret")
        mgr._cloud_id = "abc-123"
        assert mgr.api_base_url == "https://api.atlassian.com/ex/jira/abc-123"

    def test_get_auth_url_format(self) -> None:
        mgr = JiraTokenManager("my-client-id", "secret", port=9090)
        url = mgr.get_auth_url("test-state")
        assert "auth.atlassian.com/authorize" in url
        assert "client_id=my-client-id" in url
        assert "state=test-state" in url
        assert "audience=api.atlassian.com" in url
        assert "prompt=consent" in url
        assert "response_type=code" in url
        assert "offline_access" in url

    def test_save_and_load(self, tmp_path: Path) -> None:
        with patch("swarm.auth.jira._TOKEN_PATH", tmp_path / "tokens.json"):
            mgr = JiraTokenManager("cid", "csecret")
            mgr._access_token = "at"
            mgr._refresh_token = "rt"
            mgr._expires_at = 9999.0
            mgr._cloud_id = "cloud-1"
            mgr._save()

            mgr2 = JiraTokenManager("cid", "csecret")
            assert mgr2._access_token == "at"
            assert mgr2._refresh_token == "rt"
            assert mgr2._expires_at == 9999.0
            assert mgr2._cloud_id == "cloud-1"
            assert mgr2.is_connected() is True

    def test_save_permissions(self, tmp_path: Path) -> None:
        token_path = tmp_path / "tokens.json"
        with patch("swarm.auth.jira._TOKEN_PATH", token_path):
            mgr = JiraTokenManager("cid", "csecret")
            mgr._access_token = "at"
            mgr._refresh_token = "rt"
            mgr._save()

            mode = os.stat(token_path).st_mode
            assert stat.S_IMODE(mode) == 0o600

    def test_disconnect_removes_file(self, tmp_path: Path) -> None:
        token_path = tmp_path / "tokens.json"
        with patch("swarm.auth.jira._TOKEN_PATH", token_path):
            mgr = JiraTokenManager("cid", "csecret")
            mgr._access_token = "at"
            mgr._refresh_token = "rt"
            mgr._cloud_id = "cloud-1"
            mgr._save()
            assert token_path.exists()

            mgr.disconnect()
            assert not token_path.exists()
            assert mgr.is_connected() is False
            assert mgr.cloud_id == ""
            assert mgr._access_token is None

    @pytest.mark.asyncio
    async def test_get_token_no_refresh(self) -> None:
        mgr = JiraTokenManager("cid", "csecret")
        token = await mgr.get_token()
        assert token is None

    @pytest.mark.asyncio
    async def test_get_token_cached(self) -> None:
        mgr = JiraTokenManager("cid", "csecret")
        mgr._refresh_token = "rt"
        mgr._access_token = "cached-at"
        import time

        mgr._expires_at = time.time() + 3600
        token = await mgr.get_token()
        assert token == "cached-at"

    @pytest.mark.asyncio
    async def test_concurrent_get_token_refreshes_once(self) -> None:
        """#auth-audit E: two concurrent get_token() calls on an expired token
        must refresh exactly ONCE (lock + re-check), not twice — Atlassian
        rotates refresh tokens, so a double-refresh can invalidate the token."""
        import asyncio
        import time

        mgr = JiraTokenManager("cid", "csecret")
        mgr._refresh_token = "rt"
        calls = {"n": 0}

        async def fake_refresh() -> bool:
            calls["n"] += 1
            await asyncio.sleep(0)  # force interleave so both callers race
            mgr._access_token = "fresh-at"
            mgr._expires_at = time.time() + 3600
            return True

        mgr._refresh = fake_refresh  # type: ignore[method-assign]
        results = await asyncio.gather(mgr.get_token(), mgr.get_token())
        assert results == ["fresh-at", "fresh-at"]
        assert calls["n"] == 1  # second caller re-checked under the lock, didn't re-refresh

    @pytest.mark.asyncio
    async def test_exchange_code_success(self, tmp_path: Path) -> None:
        token_path = tmp_path / "tokens.json"
        with patch("swarm.auth.jira._TOKEN_PATH", token_path):
            mgr = JiraTokenManager("cid", "csecret")

            # Mock _token_request to succeed
            mgr._token_request = AsyncMock(return_value=True)  # type: ignore[method-assign]
            mgr._access_token = "new-at"
            mgr._refresh_token = "new-rt"

            # Mock _discover_cloud_id
            mgr._discover_cloud_id = AsyncMock()  # type: ignore[method-assign]
            mgr._cloud_id = "cloud-abc"

            ok = await mgr.exchange_code("auth-code")

        assert ok is True
        mgr._token_request.assert_called_once()
        mgr._discover_cloud_id.assert_called_once()
        assert mgr.cloud_id == "cloud-abc"

    def test_save_persists_credentials(self, tmp_path: Path) -> None:
        """_save() writes client_id and client_secret to the token file."""
        token_path = tmp_path / "tokens.json"
        with patch("swarm.auth.jira._TOKEN_PATH", token_path):
            mgr = JiraTokenManager("my-id", "my-secret")
            mgr._access_token = "at"
            mgr._refresh_token = "rt"
            mgr._save()

            import json

            data = json.loads(token_path.read_text())
            assert data["client_id"] == "my-id"
            assert data["client_secret"] == "my-secret"

    def test_stored_credentials(self, tmp_path: Path) -> None:
        """stored_credentials() recovers client_id/client_secret from token file."""
        token_path = tmp_path / "tokens.json"
        with patch("swarm.auth.jira._TOKEN_PATH", token_path):
            mgr = JiraTokenManager("saved-id", "saved-secret")
            mgr._access_token = "at"
            mgr._refresh_token = "rt"
            mgr._save()

            cid, csecret = JiraTokenManager.stored_credentials()
            assert cid == "saved-id"
            assert csecret == "saved-secret"

    def test_stored_credentials_missing_file(self, tmp_path: Path) -> None:
        """stored_credentials() returns empty strings when no file exists."""
        with patch("swarm.auth.jira._TOKEN_PATH", tmp_path / "nope.json"):
            cid, csecret = JiraTokenManager.stored_credentials()
            assert cid == ""
            assert csecret == ""

    def test_account_id_empty_initially(self) -> None:
        mgr = JiraTokenManager("cid", "csecret")
        assert mgr.account_id == ""

    def test_account_id_save_and_load(self, tmp_path: Path) -> None:
        """account_id is persisted and restored across instances."""
        token_path = tmp_path / "tokens.json"
        with patch("swarm.auth.jira._TOKEN_PATH", token_path):
            mgr = JiraTokenManager("cid", "csecret")
            mgr._access_token = "at"
            mgr._refresh_token = "rt"
            mgr._account_id = "user-abc-123"
            mgr._save()

            mgr2 = JiraTokenManager("cid", "csecret")
            assert mgr2.account_id == "user-abc-123"

    def test_disconnect_clears_account_id(self, tmp_path: Path) -> None:
        token_path = tmp_path / "tokens.json"
        with patch("swarm.auth.jira._TOKEN_PATH", token_path):
            mgr = JiraTokenManager("cid", "csecret")
            mgr._account_id = "user-abc"
            mgr._access_token = "at"
            mgr._refresh_token = "rt"
            mgr._save()
            mgr.disconnect()
            assert mgr.account_id == ""

    @pytest.mark.asyncio
    async def test_refresh_discovers_account_id_when_missing(self, tmp_path: Path) -> None:
        """Token refresh should retry account_id discovery if still empty."""
        token_path = tmp_path / "tokens.json"
        with patch("swarm.auth.jira._TOKEN_PATH", token_path):
            mgr = JiraTokenManager("cid", "csecret")
            mgr._refresh_token = "rt"
            mgr._access_token = "old-at"
            mgr._cloud_id = "cloud-1"
            mgr._account_id = ""  # simulate initial discovery failure
            mgr._expires_at = 0.0  # expired — will trigger refresh
            mgr._save()

            # Mock _token_request to succeed (refresh gives new access token)
            async def fake_token_request(data: dict[str, str]) -> bool:
                mgr._access_token = "new-at"
                mgr._expires_at = 9999999999.0
                return True

            mgr._token_request = fake_token_request  # type: ignore[method-assign]

            # Mock _discover_account_id to set the account_id
            async def fake_discover() -> None:
                mgr._account_id = "discovered-user-123"

            mgr._discover_account_id = fake_discover  # type: ignore[method-assign]

            token = await mgr.get_token()

            assert token == "new-at"
            assert mgr._account_id == "discovered-user-123"

    @pytest.mark.asyncio
    async def test_refresh_skips_discovery_when_account_id_present(self, tmp_path: Path) -> None:
        """Token refresh should NOT re-discover account_id if already set."""
        token_path = tmp_path / "tokens.json"
        with patch("swarm.auth.jira._TOKEN_PATH", token_path):
            mgr = JiraTokenManager("cid", "csecret")
            mgr._refresh_token = "rt"
            mgr._access_token = "old-at"
            mgr._cloud_id = "cloud-1"
            mgr._account_id = "already-known"
            mgr._expires_at = 0.0  # expired — will trigger refresh
            mgr._save()

            async def fake_token_request(data: dict[str, str]) -> bool:
                mgr._access_token = "new-at"
                mgr._expires_at = 9999999999.0
                return True

            mgr._token_request = fake_token_request  # type: ignore[method-assign]
            mgr._discover_account_id = AsyncMock()  # type: ignore[method-assign]

            token = await mgr.get_token()

            assert token == "new-at"
            assert mgr._account_id == "already-known"
            mgr._discover_account_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_exchange_code_failure(self) -> None:
        mgr = JiraTokenManager("cid", "csecret")
        mgr._token_request = AsyncMock(return_value=False)  # type: ignore[method-assign]
        mgr.last_error = "invalid_grant"

        ok = await mgr.exchange_code("bad-code")

        assert ok is False
        assert "invalid_grant" in mgr.last_error
