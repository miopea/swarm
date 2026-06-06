"""Atlassian Jira OAuth 2.0 (3LO) token manager."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import aiohttp

from swarm.auth._oauth import apply_token_response, parse_token_error

_TOKEN_PATH = Path.home() / ".swarm" / "jira_tokens.json"
_AUTH_URL = "https://auth.atlassian.com/authorize"
_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
_SCOPE = "read:jira-work write:jira-work offline_access"
_log = logging.getLogger("swarm.auth.jira")


class JiraTokenManager:
    """Manages Atlassian Jira OAuth tokens with automatic refresh."""

    def __init__(
        self, client_id: str, client_secret: str, port: int = 9090, domain: str = ""
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        if domain:
            self.redirect_uri = f"https://{domain}/auth/jira/callback"
        else:
            self.redirect_uri = f"http://localhost:{port}/auth/jira/callback"
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0
        self._cloud_id: str = ""
        self._site_url: str = ""
        self._account_id: str = ""
        self.last_error: str = ""
        # Serialise token refresh: two concurrent get_token() callers must not
        # both refresh — Atlassian rotates the refresh token, so the second
        # refresh would use a now-invalidated token and break auth.
        self._refresh_lock = asyncio.Lock()
        self._load()

    # --- Public API ---

    def is_connected(self) -> bool:
        """True if a refresh token is available (may need refresh)."""
        return bool(self._refresh_token)

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def cloud_id(self) -> str:
        return self._cloud_id

    @property
    def api_base_url(self) -> str:
        """Jira site base URL via Atlassian cloud gateway (no /rest/api/3 suffix)."""
        if not self._cloud_id:
            return ""
        return f"https://api.atlassian.com/ex/jira/{self._cloud_id}"

    def get_auth_url(self, state: str) -> str:
        """Build the Atlassian OAuth authorize URL."""
        scope = _SCOPE.replace(" ", "%20")
        params = (
            f"audience=api.atlassian.com"
            f"&client_id={self.client_id}"
            f"&scope={scope}"
            f"&redirect_uri={self.redirect_uri}"
            f"&state={state}"
            f"&response_type=code"
            f"&prompt=consent"
        )
        return f"{_AUTH_URL}?{params}"

    async def exchange_code(self, code: str) -> bool:
        """Exchange authorization code for tokens. Returns True on success."""
        data = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
        }
        ok = await self._token_request(data)
        if ok:
            await self._discover_cloud_id()
        return ok

    async def get_token(self) -> str | None:
        """Return a valid access token, refreshing if needed."""
        if not self._refresh_token:
            return None
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        async with self._refresh_lock:
            # Re-check under the lock: a concurrent caller may have just
            # refreshed while we waited, so we don't refresh a second time.
            if self._access_token and time.time() < self._expires_at - 60:
                return self._access_token
            if await self._refresh():
                return self._access_token
        return None

    def disconnect(self) -> None:
        """Remove stored tokens from DB and file."""
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0.0
        self._cloud_id = ""
        self._account_id = ""
        try:
            from swarm.db.secrets import save_secret

            save_secret("jira_tokens", {})
        except Exception:
            _log.warning("failed to clear Jira tokens from the secret store", exc_info=True)
        if _TOKEN_PATH.exists():
            _TOKEN_PATH.unlink()

    # --- Internal ---

    async def _discover_cloud_id(self) -> None:
        """Fetch accessible resources to determine the Jira Cloud site ID."""
        if not self._access_token:
            return
        headers = {"Authorization": f"Bearer {self._access_token}"}
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    _RESOURCES_URL,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        _log.warning(
                            "cloud_id discovery failed (%s): %s",
                            resp.status,
                            body[:300],
                        )
                        return
                    resources = await resp.json()
        except Exception as exc:
            _log.warning("cloud_id discovery error: %s", exc)
            return

        if not resources or not isinstance(resources, list):
            _log.warning("cloud_id discovery: no accessible resources returned")
            return

        # Log all available sites for debugging
        for i, r in enumerate(resources):
            _log.info(
                "Jira accessible resource [%d]: id=%s name=%s url=%s scopes=%s",
                i,
                r.get("id", "?")[:12],
                r.get("name", "?"),
                r.get("url", "?"),
                r.get("scopes", []),
            )

        self._cloud_id = resources[0].get("id", "")
        self._site_url = resources[0].get("url", "")
        self._save()
        _log.info(
            "Jira cloud_id selected: %s (%s)",
            self._cloud_id[:12],
            self._site_url,
        )

        # Discover the authenticated user's account ID for issue assignment
        await self._discover_account_id()

    async def _discover_account_id(self) -> None:
        """Fetch the authenticated user's accountId from Jira."""
        if not self._access_token or not self._cloud_id:
            return
        url = f"https://api.atlassian.com/ex/jira/{self._cloud_id}/rest/api/3/myself"
        headers = {"Authorization": f"Bearer {self._access_token}"}
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        _log.warning("account_id discovery failed: %d", resp.status)
                        return
                    data = await resp.json()
        except Exception as exc:
            _log.warning("account_id discovery error: %s", exc)
            return

        self._account_id = data.get("accountId", "")
        if self._account_id:
            self._save()
            _log.info("Jira account_id discovered: %s", self._account_id[:12])

    async def _refresh(self) -> bool:
        """Use refresh_token to get a new access_token."""
        if not self._refresh_token:
            return False
        data = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self._refresh_token,
        }
        ok = await self._token_request(data)
        if ok and self._cloud_id and not self._account_id:
            await self._discover_account_id()
        return ok

    async def _token_request(self, data: dict[str, str]) -> bool:
        """POST to Atlassian token endpoint (JSON body). Returns True on success."""
        self.last_error = ""
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    _TOKEN_URL,
                    json=data,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        self.last_error = parse_token_error(await resp.text())
                        _log.warning(
                            "Jira token request failed (%s): %s",
                            resp.status,
                            self.last_error,
                        )
                        return False
                    body = await resp.json()
        except Exception as exc:
            self.last_error = str(exc)
            _log.warning("Jira token request exception: %s", exc)
            return False

        parsed = apply_token_response(body, prev_refresh=self._refresh_token)
        if parsed is None:
            self.last_error = "token response missing access_token"
            _log.warning("Jira token response had no access_token")
            return False
        self._access_token, self._refresh_token, self._expires_at = parsed
        self._save()
        return True

    @staticmethod
    def stored_credentials() -> tuple[str, str]:
        """Read (client_id, client_secret) from DB or token file."""
        from swarm.db.secrets import load_secret

        raw = load_secret("jira_tokens")
        if raw is None and _TOKEN_PATH.exists():
            try:
                raw = json.loads(_TOKEN_PATH.read_text())
            except Exception:
                return ("", "")
        if raw:
            return (raw.get("client_id", ""), raw.get("client_secret", ""))
        return ("", "")

    def _load(self) -> None:
        """Load tokens from DB, fall back to file."""
        raw = None
        if _TOKEN_PATH == Path.home() / ".swarm" / "jira_tokens.json":
            from swarm.db.secrets import load_secret

            raw = load_secret("jira_tokens")
        if raw is None and _TOKEN_PATH.exists():
            try:
                raw = json.loads(_TOKEN_PATH.read_text())
            except Exception:
                _log.warning("Failed to load Jira auth tokens", exc_info=True)
                return
        if not raw:
            return
        self._access_token = raw.get("access_token")
        self._refresh_token = raw.get("refresh_token")
        self._expires_at = raw.get("expires_at", 0.0)
        self._cloud_id = raw.get("cloud_id", "")
        self._site_url = raw.get("site_url", "")
        self._account_id = raw.get("account_id", "")
        if self._refresh_token:
            _log.info(
                "Jira OAuth tokens loaded (cloud_id=%s)",
                self._cloud_id[:8] if self._cloud_id else "none",
            )

    def _save(self) -> None:
        """Write tokens to DB, fall back to file."""
        data = {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "expires_at": self._expires_at,
            "cloud_id": self._cloud_id,
            "site_url": self._site_url,
            "account_id": self._account_id,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        if _TOKEN_PATH == Path.home() / ".swarm" / "jira_tokens.json":
            from swarm.db.secrets import save_secret

            save_secret("jira_tokens", data)
            return
        _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(data).encode()
        fd = os.open(str(_TOKEN_PATH), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, content)
        finally:
            os.close(fd)
