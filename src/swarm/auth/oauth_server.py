"""Minimal OAuth 2.0 authorization server for the MCP endpoint.

Claude Desktop's remote-MCP connector authenticates via OAuth 2.1
(Authorization Code + PKCE), optionally preceded by Dynamic Client
Registration (RFC 7591). This module implements just enough of an
authorization server for that flow, so the connector's "Connect" button
works natively against a tunneled Swarm daemon.

Design choices (single-operator, local tool — kept deliberately small):

* **Stateless tokens.** Access and refresh tokens are HMAC-signed blobs
  (same construction as the dashboard session cookie), verified without
  storage. No DB migration; tokens survive daemon restarts because the
  signing key is persisted in the ``secrets`` table.
* **Stateless clients.** A client's secret is derived as
  ``HMAC(signing_key, client_id)``, so registered clients need no storage
  either — ``verify_client_secret`` recomputes it. Dynamic registration
  just mints a fresh ``client_id`` and returns the derived secret.
* **Auth codes are in-memory** with a short TTL. Losing them on restart is
  harmless — the connector simply re-runs authorize.
* **Redirect-URI allowlist.** Because ``/authorize`` auto-approves on a
  valid dashboard session (no consent click), an open redirect would let a
  malicious site ride the operator's session and exfiltrate a code. We
  therefore only redirect to known Claude/Anthropic hosts (and localhost);
  rejected redirects are logged so an unexpected host is easy to allowlist.

The issued access token grants full MCP tool access, exactly like the
static MCP token — it is validated at ``/mcp`` alongside it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets as _secrets
import time
from typing import Any
from urllib.parse import urlparse

from swarm.db.secrets import load_secret, save_secret
from swarm.logging import get_logger

_log = get_logger("auth.oauth")

# Secret keys (secrets table)
_SIGNING_KEY_SECRET = "oauth_signing_key"
_REDIRECT_HOSTS_SECRET = "oauth_allowed_redirect_hosts"

# Fixed client shown in the settings page for the manual "Advanced settings"
# (Client ID / Secret) path. Dynamic registration mints additional ones.
STATIC_CLIENT_ID = "swarm-mcp"

ACCESS_TOKEN_TTL = 3600  # 1 hour
REFRESH_TOKEN_TTL = 30 * 24 * 3600  # 30 days
AUTH_CODE_TTL = 300  # 5 minutes
CONSENT_TTL = 600  # 10 minutes — how long a rendered consent page stays valid
SCOPE = "mcp"

# Redirect hosts we auto-approve to. Suffix entries (leading dot) match
# subdomains. Overridable/extendable via the oauth_allowed_redirect_hosts
# secret (a JSON list), for operators whose client uses another callback.
_DEFAULT_REDIRECT_HOSTS: tuple[str, ...] = (
    "claude.ai",
    ".claude.ai",
    "anthropic.com",
    ".anthropic.com",
    "localhost",
    "127.0.0.1",
)

_signing_key: bytes | None = None
# code -> {client_id, redirect_uri, code_challenge, scope, exp}
_auth_codes: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# base64url helpers (no padding — OAuth/JOSE convention)
# ---------------------------------------------------------------------------
def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ---------------------------------------------------------------------------
# Signing key
# ---------------------------------------------------------------------------
def _get_signing_key() -> bytes:
    global _signing_key
    if _signing_key:
        return _signing_key
    stored = load_secret(_SIGNING_KEY_SECRET)
    if isinstance(stored, dict) and isinstance(stored.get("key"), str) and stored["key"]:
        _signing_key = _b64u_decode(stored["key"])
        return _signing_key
    key = _secrets.token_bytes(32)
    save_secret(_SIGNING_KEY_SECRET, {"key": _b64u(key)})
    _signing_key = key
    return key


def rotate_signing_key() -> None:
    """Rotate the OAuth signing key — invalidates all tokens and client secrets."""
    global _signing_key
    key = _secrets.token_bytes(32)
    save_secret(_SIGNING_KEY_SECRET, {"key": _b64u(key)})
    _signing_key = key
    _auth_codes.clear()


# ---------------------------------------------------------------------------
# Signed tokens (access / refresh)
# ---------------------------------------------------------------------------
def _sign(payload: dict[str, Any]) -> str:
    body = _b64u(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64u(hmac.new(_get_signing_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def _unsign(token: str) -> dict[str, Any] | None:
    if not token or token.count(".") != 1:
        return None
    body, sig = token.split(".", 1)
    expected = _b64u(hmac.new(_get_signing_key(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64u_decode(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp < time.time():
        return None
    return payload


def mint_access_token(client_id: str, scope: str = SCOPE) -> str:
    now = int(time.time())
    return _sign(
        {"t": "at", "cid": client_id, "scp": scope, "iat": now, "exp": now + ACCESS_TOKEN_TTL}
    )


def mint_refresh_token(client_id: str, scope: str = SCOPE) -> str:
    now = int(time.time())
    return _sign(
        {"t": "rt", "cid": client_id, "scp": scope, "iat": now, "exp": now + REFRESH_TOKEN_TTL}
    )


def verify_access_token(token: str) -> dict[str, Any] | None:
    """Return the token payload if it is a valid, unexpired access token."""
    payload = _unsign(token)
    if payload and payload.get("t") == "at":
        return payload
    return None


def verify_refresh_token(token: str) -> dict[str, Any] | None:
    payload = _unsign(token)
    if payload and payload.get("t") == "rt":
        return payload
    return None


def mint_consent_token(
    client_id: str, redirect_uri: str, code_challenge: str, scope: str, state: str
) -> str:
    """Sign the pending authorization so the consent POST can't be forged or
    tampered with — the operator's Approve returns this token, and the code is
    issued from its (signed) params rather than re-submitted form fields."""
    now = int(time.time())
    return _sign(
        {
            "t": "cns",
            "cid": client_id,
            "ru": redirect_uri,
            "cc": code_challenge,
            "sc": scope,
            "st": state,
            "exp": now + CONSENT_TTL,
        }
    )


def verify_consent_token(token: str) -> dict[str, Any] | None:
    payload = _unsign(token)
    if payload and payload.get("t") == "cns":
        return payload
    return None


# ---------------------------------------------------------------------------
# Clients (stateless — secret derived from client_id)
# ---------------------------------------------------------------------------
def derive_client_secret(client_id: str) -> str:
    return _b64u(
        hmac.new(_get_signing_key(), b"client:" + client_id.encode(), hashlib.sha256).digest()
    )


def verify_client_secret(client_id: str, client_secret: str) -> bool:
    if not client_id or not client_secret:
        return False
    return hmac.compare_digest(client_secret, derive_client_secret(client_id))


def get_static_client() -> tuple[str, str]:
    """The fixed (client_id, client_secret) surfaced in the settings page."""
    return STATIC_CLIENT_ID, derive_client_secret(STATIC_CLIENT_ID)


def register_client() -> dict[str, Any]:
    """Dynamic Client Registration — mint a fresh public/confidential client."""
    client_id = "swarm-" + _secrets.token_hex(12)
    now = int(time.time())
    return {
        "client_id": client_id,
        "client_secret": derive_client_secret(client_id),
        "client_id_issued_at": now,
        "client_secret_expires_at": 0,  # 0 = never
        "token_endpoint_auth_method": "client_secret_post",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }


# ---------------------------------------------------------------------------
# Redirect-URI allowlist
# ---------------------------------------------------------------------------
def _allowed_redirect_hosts() -> tuple[str, ...]:
    hosts = list(_DEFAULT_REDIRECT_HOSTS)
    extra = load_secret(_REDIRECT_HOSTS_SECRET)
    if isinstance(extra, list):
        hosts.extend(str(h) for h in extra)
    return tuple(hosts)


def is_allowed_redirect(redirect_uri: str) -> bool:
    """True if *redirect_uri* is a well-formed callback on an allowlisted host."""
    if not redirect_uri:
        return False
    try:
        parsed = urlparse(redirect_uri)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    is_loopback = host in ("localhost", "127.0.0.1")
    # Non-loopback callbacks must be https (no plaintext code delivery).
    if parsed.scheme != "https" and not (is_loopback and parsed.scheme in ("http", "https")):
        return False
    for allowed in _allowed_redirect_hosts():
        if allowed.startswith("."):
            if host == allowed[1:] or host.endswith(allowed):
                return True
        elif host == allowed:
            return True
    return False


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------
def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    """Verify an S256 PKCE challenge."""
    if not code_verifier or not code_challenge:
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return hmac.compare_digest(_b64u(digest), code_challenge)


# ---------------------------------------------------------------------------
# Authorization codes (in-memory, short-lived)
# ---------------------------------------------------------------------------
def issue_code(client_id: str, redirect_uri: str, code_challenge: str, scope: str) -> str:
    _prune_codes()
    code = _secrets.token_urlsafe(24)
    _auth_codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "scope": scope,
        "exp": time.time() + AUTH_CODE_TTL,
    }
    return code


def consume_code(code: str) -> dict[str, Any] | None:
    """Pop and return a code's data if present and unexpired (single use)."""
    _prune_codes()
    data = _auth_codes.pop(code, None)
    if data is None or data["exp"] < time.time():
        return None
    return data


def _prune_codes() -> None:
    now = time.time()
    for code in [c for c, d in _auth_codes.items() if d["exp"] < now]:
        _auth_codes.pop(code, None)
