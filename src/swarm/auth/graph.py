"""Microsoft Graph OAuth token manager (PKCE + optional client secret)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path

import aiohttp

from swarm.auth._oauth import apply_token_response, parse_token_error

_TOKEN_PATH = Path.home() / ".swarm" / "graph_tokens.json"
_AUTH_BASE = "https://login.microsoftonline.com"
_SCOPE = "Mail.ReadWrite offline_access"
_log = logging.getLogger(__name__)


class GraphTokenManager:
    """Manages Microsoft Graph OAuth tokens with automatic refresh."""

    def __init__(
        self,
        client_id: str,
        tenant_id: str = "common",
        port: int = 9090,
        domain: str = "",
        client_secret: str = "",
    ) -> None:
        self.client_id = client_id
        self.tenant_id = tenant_id
        self.client_secret = client_secret
        if domain:
            self.redirect_uri = f"https://{domain}/auth/graph/callback"
        else:
            self.redirect_uri = f"http://localhost:{port}/auth/graph/callback"
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0
        self.last_error: str = ""
        # Serialise refresh so two concurrent get_token() callers don't both
        # refresh (a rotated refresh token would invalidate the loser).
        self._refresh_lock = asyncio.Lock()
        self._load()

    # --- Public API ---

    def is_connected(self) -> bool:
        """True if a refresh token is available (may need refresh)."""
        return bool(self._refresh_token)

    def get_auth_url(self, state: str, code_verifier: str) -> str:
        """Build the Microsoft OAuth authorize URL with PKCE challenge."""
        challenge = _pkce_challenge(code_verifier)
        params = (
            f"client_id={self.client_id}"
            f"&response_type=code"
            f"&redirect_uri={self.redirect_uri}"
            f"&response_mode=query"
            f"&scope={_SCOPE.replace(' ', '%20')}"
            f"&state={state}"
            f"&code_challenge={challenge}"
            f"&code_challenge_method=S256"
        )
        return f"{_AUTH_BASE}/{self.tenant_id}/oauth2/v2.0/authorize?{params}"

    async def exchange_code(self, code: str, code_verifier: str) -> bool:
        """Exchange authorization code for tokens. Returns True on success."""
        url = f"{_AUTH_BASE}/{self.tenant_id}/oauth2/v2.0/token"
        data = {
            "client_id": self.client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": code_verifier,
            "scope": _SCOPE,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret
        return await self._token_request(url, data)

    async def get_token(self) -> str | None:
        """Return a valid access token, refreshing if needed."""
        if not self._refresh_token:
            return None
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        async with self._refresh_lock:
            # Re-check under the lock — a concurrent caller may have refreshed.
            if self._access_token and time.time() < self._expires_at - 60:
                return self._access_token
            if await self._refresh():
                return self._access_token
        return None

    async def create_reply_draft(
        self, message_id: str, body_html: str, *, reply_all: bool = True
    ) -> bool:
        """Create a draft reply (or reply-all) to an existing message via Graph API.

        Uses the ``createReply`` / ``createReplyAll`` endpoint which creates a
        draft in the user's Drafts folder without sending it.  The user can
        review and send manually from Outlook.
        """
        token = await self.get_token()
        if not token:
            _log.warning("create_reply_draft: no valid token")
            return False

        from urllib.parse import quote

        encoded = quote(message_id, safe="")
        action = "createReplyAll" if reply_all else "createReply"
        url = f"https://graph.microsoft.com/v1.0/me/messages/{encoded}/{action}"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = {"comment": body_html}

        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status in (200, 201):
                        _log.info("Draft reply created for message %s", message_id[:30])
                        return True
                    err = await resp.text()
                    _log.warning("create_reply_draft failed (%s): %s", resp.status, err[:200])
                    return False
        except Exception as exc:
            _log.warning("create_reply_draft exception: %s", exc)
            return False

    async def create_draft(
        self,
        to: list[str],
        subject: str,
        body: str,
        *,
        cc: list[str] | None = None,
        body_type: str = "text",
    ) -> dict[str, str] | None:
        """Create a new email draft in the user's Drafts folder via Graph API.

        Uses ``POST /me/messages`` which creates a draft that the user must
        explicitly send from Outlook — we never send on the user's behalf.
        ``body_type`` is ``"text"`` (default) or ``"html"``.

        Returns ``{"id": "...", "web_link": "https://outlook.office.com/..."}``
        on success, or ``None`` on failure (no valid token, Graph error, etc.).
        """
        token = await self.get_token()
        if not token:
            _log.warning("create_draft: no valid token")
            return None

        url = "https://graph.microsoft.com/v1.0/me/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        content_type = "html" if body_type.lower() == "html" else "text"
        payload: dict[str, object] = {
            "subject": subject,
            "body": {"contentType": content_type, "content": body},
            "toRecipients": [{"emailAddress": {"address": addr}} for addr in to],
        }
        if cc:
            payload["ccRecipients"] = [{"emailAddress": {"address": addr}} for addr in cc]

        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status not in (200, 201):
                        err = await resp.text()
                        _log.warning("create_draft failed (%s): %s", resp.status, err[:200])
                        return None
                    data = await resp.json()
                    return {
                        "id": data.get("id", ""),
                        "web_link": data.get("webLink", ""),
                    }
        except Exception as exc:
            _log.warning("create_draft exception: %s", exc)
            return None

    async def resolve_message_id(self, internet_msg_id: str) -> str | None:
        """Resolve an RFC 822 Message-ID to a Graph message ID.

        Queries ``/me/messages?$filter=internetMessageId eq '...'`` and returns
        the Graph ``id`` of the first match, or ``None``.
        """
        token = await self.get_token()
        if not token:
            return None

        from urllib.parse import quote

        # Graph $filter requires the value in single quotes
        escaped = internet_msg_id.replace("'", "''")
        url = (
            f"https://graph.microsoft.com/v1.0/me/messages"
            f"?$filter=internetMessageId eq '{quote(escaped, safe='')}'"
            f"&$select=id"
        )
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        _log.warning("resolve_message_id failed (%s)", resp.status)
                        return None
                    data = await resp.json()
                    values = data.get("value", [])
                    if values:
                        return values[0].get("id")
                    return None
        except Exception as exc:
            _log.warning("resolve_message_id error: %s", exc)
            return None

    def disconnect(self) -> None:
        """Remove stored tokens from DB and file."""
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0.0
        try:
            from swarm.db.secrets import save_secret

            save_secret("graph_tokens", {})
        except Exception:
            _log.warning("failed to clear Graph tokens from the secret store", exc_info=True)
        if _TOKEN_PATH.exists():
            _TOKEN_PATH.unlink()

    # --- Internal ---

    async def _refresh(self) -> bool:
        """Use refresh_token to get a new access_token."""
        if not self._refresh_token:
            return False
        url = f"{_AUTH_BASE}/{self.tenant_id}/oauth2/v2.0/token"
        data = {
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "scope": _SCOPE,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret
        return await self._token_request(url, data)

    async def _token_request(self, url: str, data: dict[str, str]) -> bool:
        """POST to token endpoint, save result. Returns True on success."""
        self.last_error = ""
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    url, data=data, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        self.last_error = parse_token_error(await resp.text())
                        _log.warning(
                            "Graph token request failed (%s): %s", resp.status, self.last_error
                        )
                        return False
                    body = await resp.json()
        except Exception as exc:
            self.last_error = str(exc)
            _log.warning("Graph token request exception: %s", exc)
            return False

        parsed = apply_token_response(body, prev_refresh=self._refresh_token)
        if parsed is None:
            self.last_error = "token response missing access_token"
            _log.warning("Graph token response had no access_token")
            return False
        self._access_token, self._refresh_token, self._expires_at = parsed
        self._save()
        return True

    def _load(self) -> None:
        """Load tokens from DB, fall back to file."""
        from swarm.db.secrets import load_secret

        raw = load_secret("graph_tokens")
        if raw is None and _TOKEN_PATH.exists():
            try:
                raw = json.loads(_TOKEN_PATH.read_text())
            except Exception:
                _log.debug("Failed to load auth tokens", exc_info=True)
                return
        if raw:
            self._access_token = raw.get("access_token")
            self._refresh_token = raw.get("refresh_token")
            self._expires_at = raw.get("expires_at", 0.0)

    def _save(self) -> None:
        """Write tokens to DB, fall back to file."""
        data = {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "expires_at": self._expires_at,
        }
        from swarm.db.secrets import save_secret

        if save_secret("graph_tokens", data):
            return
        _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(data).encode()
        fd = os.open(str(_TOKEN_PATH), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, content)
        finally:
            os.close(fd)


def generate_pkce_verifier() -> str:
    """Generate a random 43-character code verifier for PKCE."""
    return secrets.token_urlsafe(32)


def _pkce_challenge(verifier: str) -> str:
    """SHA256 hash of verifier, base64url-encoded (no padding)."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
