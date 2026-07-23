"""Dedicated bearer token for the MCP HTTP endpoints.

The ``/mcp`` Streamable-HTTP surface can dispatch tasks straight into worker
PTYs (``swarm_create_task`` → a prompt injected into Claude Code, executed
with full tool access), so it is the swarm's single most sensitive remote
surface. When the daemon is exposed on a public tunnel, an unauthenticated
``/mcp`` is anonymous remote code execution.

It is therefore gated by a **dedicated** token — deliberately separate from
the dashboard password — so external MCP clients (ChatGPT / Claude Desktop
connectors) can be given MCP access without ever holding the dashboard
credential, and the two can be rotated independently.

The token is persisted in the ``swarm.db`` secrets table under ``mcp_token``
and generated on first access. Local workers receive it transparently: the
daemon injects it into each worker's ``.mcp.json`` ``headers`` when it writes
those configs.
"""

from __future__ import annotations

import hmac
import secrets as _secrets

from swarm.db.secrets import load_secret, save_secret

_SECRET_KEY = "mcp_token"

# Process-local cache so we don't hit the DB on every request. Wiped on
# daemon restart (os.execv re-imports the module); the token itself is
# persisted in swarm.db, so the value is stable across restarts.
_cached: str | None = None


def get_or_create_mcp_token() -> str:
    """Return the MCP bearer token, generating and persisting it on first use."""
    global _cached
    if _cached:
        return _cached

    stored = load_secret(_SECRET_KEY)
    if isinstance(stored, dict):
        tok = stored.get("token")
        if isinstance(tok, str) and tok:
            _cached = tok
            return _cached

    token = _secrets.token_urlsafe(32)
    save_secret(_SECRET_KEY, {"token": token})
    _cached = token
    return token


def verify_mcp_token(provided: str) -> bool:
    """Constant-time check of *provided* against the stored MCP token."""
    if not provided:
        return False
    expected = get_or_create_mcp_token()
    return hmac.compare_digest(provided.encode(), expected.encode())
