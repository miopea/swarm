"""Shared OAuth token-response handling for the auth token managers.

``JiraTokenManager`` and ``GraphTokenManager`` POST to different IdP token
endpoints with different request shapes (Atlassian: JSON body, fixed URL;
Microsoft: form body, per-tenant URL), but the *response* handling — parse the
error body, and parse/validate a successful token body — was byte-for-byte
identical. This module centralises that shared half so the two managers don't
drift, and carries the robustness rules in one place:

* a 200 with no ``access_token`` is NOT success (was a silent auth failure);
* a non-numeric ``expires_in`` falls back to 3600 instead of raising.
"""

from __future__ import annotations

import json
import time

_DEFAULT_EXPIRES_IN = 3600
_ERR_TRUNCATE = 300


def parse_token_error(err_body: str) -> str:
    """Extract a human-readable error from an OAuth token-endpoint error body.

    Prefers ``error_description``, then ``error``, then the truncated raw body.
    """
    try:
        err_json = json.loads(err_body)
    except (json.JSONDecodeError, ValueError):
        return err_body[:_ERR_TRUNCATE]
    if not isinstance(err_json, dict):
        return err_body[:_ERR_TRUNCATE]
    return err_json.get("error_description", err_json.get("error", err_body[:_ERR_TRUNCATE]))


def apply_token_response(
    body: dict[str, object], *, prev_refresh: str | None
) -> tuple[str, str | None, float] | None:
    """Validate + parse a successful OAuth token response.

    Returns ``(access_token, refresh_token, expires_at)`` on success, or
    ``None`` if the body is malformed (no usable ``access_token``) — callers
    set ``last_error`` and return failure on ``None``. The refresh token falls
    back to ``prev_refresh`` when the response omits one (IdPs that don't rotate
    it on refresh). A non-numeric ``expires_in`` defaults rather than raising.
    """
    access = body.get("access_token")
    if not access or not isinstance(access, str):
        return None
    raw_refresh = body.get("refresh_token")
    refresh = raw_refresh if isinstance(raw_refresh, str) and raw_refresh else prev_refresh
    raw_expires = body.get("expires_in", _DEFAULT_EXPIRES_IN)
    try:
        expires_in = int(raw_expires)  # type: ignore-free: bad types fall through to except
    except (TypeError, ValueError):
        expires_in = _DEFAULT_EXPIRES_IN
    return access, refresh, time.time() + expires_in
