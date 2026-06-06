"""Tests for the shared OAuth token-response helpers (#auth-audit).

These back the consolidation of JiraTokenManager / GraphTokenManager
``_token_request`` response + error handling, and carry the robustness fixes:
reject a 200 with no ``access_token``, and tolerate a non-int ``expires_in``.
"""

from __future__ import annotations

import time

from swarm.auth._oauth import apply_token_response, parse_token_error


class TestApplyTokenResponse:
    def test_valid_response(self) -> None:
        before = time.time()
        result = apply_token_response(
            {"access_token": "at", "refresh_token": "rt", "expires_in": 7200},
            prev_refresh=None,
        )
        assert result is not None
        access, refresh, expires_at = result
        assert access == "at"
        assert refresh == "rt"
        assert before + 7200 <= expires_at <= time.time() + 7200

    def test_missing_access_token_returns_none(self) -> None:
        """A 200 body without access_token must NOT be treated as success
        (the old code set None but returned True → silent auth failure)."""
        assert apply_token_response({"refresh_token": "rt"}, prev_refresh=None) is None
        assert apply_token_response({"access_token": ""}, prev_refresh=None) is None

    def test_keeps_previous_refresh_when_absent(self) -> None:
        result = apply_token_response({"access_token": "at"}, prev_refresh="old-rt")
        assert result is not None
        assert result[1] == "old-rt"

    def test_string_expires_in_is_coerced(self) -> None:
        result = apply_token_response(
            {"access_token": "at", "expires_in": "3600"}, prev_refresh=None
        )
        assert result is not None
        assert result[2] <= time.time() + 3600

    def test_bad_expires_in_defaults(self) -> None:
        """A non-numeric expires_in must not crash the refresh (old code did
        time.time() + body['expires_in'] → TypeError, uncaught)."""
        before = time.time()
        result = apply_token_response(
            {"access_token": "at", "expires_in": "not-a-number"}, prev_refresh=None
        )
        assert result is not None
        assert result[2] >= before + 3600


class TestParseTokenError:
    def test_error_description_preferred(self) -> None:
        assert parse_token_error('{"error": "invalid_grant", "error_description": "expired"}') == (
            "expired"
        )

    def test_error_fallback(self) -> None:
        assert parse_token_error('{"error": "invalid_grant"}') == "invalid_grant"

    def test_non_json_truncated(self) -> None:
        out = parse_token_error("x" * 500)
        assert out == "x" * 300
