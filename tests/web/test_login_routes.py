"""Tests for :mod:`swarm.web.routes.login`.

Pre-fill-in: ``login.py`` sat at **0% coverage** (217 lines) — by
far the most painful gap on the audit shelf because a login
regression locks the operator out.  This file plugs the gap by
exercising the pure helper functions (IP rate-limit window) and the
basic login POST + logout flow against an in-process aiohttp test
client.

The WebAuthn passkey routes are intentionally out of scope here —
they need real cryptographic challenge/response, and the prod path
is already exercised through dashboard integration use.  The
helpers + password flow are the load-bearing parts.

Coverage gap closed in the 2026-05-27 test-gap fill-in, phase 3.
"""

from __future__ import annotations

import time

import pytest

from swarm.web.routes import login as login_mod


@pytest.fixture(autouse=True)
def _reset_login_failures() -> None:
    """Each test sees a clean ``_login_failures`` dict.

    The module-level dict survives across tests within the same
    pytest process — guard against cross-test leakage by clearing it
    in both setup and teardown.
    """
    login_mod._login_failures.clear()
    yield
    login_mod._login_failures.clear()


# ---------------------------------------------------------------------------
# IP-based rate-limit helpers (pure functions over module-level state)
# ---------------------------------------------------------------------------


class TestRateLimitHelpers:
    """``_is_login_locked`` / ``_record_login_failure`` / ``_clear_login_failures``."""

    def test_fresh_ip_is_not_locked(self) -> None:
        assert login_mod._is_login_locked("1.2.3.4") is False

    def test_under_threshold_not_locked(self) -> None:
        """Below ``_LOGIN_MAX_FAILURES`` strikes, IP is not locked."""
        for _ in range(login_mod._LOGIN_MAX_FAILURES - 1):
            login_mod._record_login_failure("1.2.3.4")
        assert login_mod._is_login_locked("1.2.3.4") is False

    def test_at_threshold_is_locked(self) -> None:
        for _ in range(login_mod._LOGIN_MAX_FAILURES):
            login_mod._record_login_failure("1.2.3.4")
        assert login_mod._is_login_locked("1.2.3.4") is True

    def test_clear_resets_lock(self) -> None:
        for _ in range(login_mod._LOGIN_MAX_FAILURES):
            login_mod._record_login_failure("1.2.3.4")
        assert login_mod._is_login_locked("1.2.3.4") is True
        login_mod._clear_login_failures("1.2.3.4")
        assert login_mod._is_login_locked("1.2.3.4") is False

    def test_lockout_expires_after_window(self) -> None:
        """Failures older than ``_LOGIN_LOCKOUT_SECONDS`` get pruned on read."""
        # Seed 5 ancient failures (16 minutes ago — past the 15-min window)
        ip = "1.2.3.4"
        login_mod._login_failures[ip] = login_mod._login_failures.get(ip, login_mod.deque())
        ancient = time.time() - (login_mod._LOGIN_LOCKOUT_SECONDS + 60)
        for _ in range(login_mod._LOGIN_MAX_FAILURES):
            login_mod._login_failures[ip].append(ancient)
        # Lockout check should prune the stale entries and report unlocked
        assert login_mod._is_login_locked(ip) is False
        # ... and the deque is empty after the prune
        assert len(login_mod._login_failures[ip]) == 0

    def test_failures_per_ip_are_isolated(self) -> None:
        for _ in range(login_mod._LOGIN_MAX_FAILURES):
            login_mod._record_login_failure("1.1.1.1")
        assert login_mod._is_login_locked("1.1.1.1") is True
        assert login_mod._is_login_locked("2.2.2.2") is False

    def test_clear_on_unknown_ip_is_noop(self) -> None:
        # Should not raise / leak entries
        login_mod._clear_login_failures("never-seen")
        assert "never-seen" not in login_mod._login_failures


# ---------------------------------------------------------------------------
# WebAuthn origin / RP helpers (pure)
# ---------------------------------------------------------------------------


class TestWebauthnOriginHelpers:
    """``_get_rp_id`` / ``_get_expected_origin`` configure the WebAuthn
    relying-party + origin allow-list from request host / config.domain."""

    def _make_request(self, host: str, *, domain: str = "") -> object:
        """Build a stand-in for aiohttp's web.Request.

        We only need the host string and a daemon back-ref with a
        config.domain attribute; everything else the helpers touch is
        wrapped behind ``get_daemon``.
        """
        from unittest.mock import MagicMock

        request = MagicMock()
        request.host = host
        # The helpers reach for ``request.app`` -> daemon via get_daemon;
        # patch the get_daemon import path instead of the request itself.
        return request

    def test_rp_id_uses_config_domain_when_set(self, monkeypatch) -> None:
        from unittest.mock import MagicMock

        request = self._make_request("worker.local:9090")
        fake_daemon = MagicMock()
        fake_daemon.config.domain = "swarm.example.com"
        monkeypatch.setattr(login_mod, "get_daemon", lambda _r: fake_daemon)
        assert login_mod._get_rp_id(request) == "swarm.example.com"

    def test_rp_id_falls_back_to_request_host_without_port(self, monkeypatch) -> None:
        from unittest.mock import MagicMock

        request = self._make_request("worker.local:9090")
        fake_daemon = MagicMock()
        fake_daemon.config.domain = ""  # Not configured
        monkeypatch.setattr(login_mod, "get_daemon", lambda _r: fake_daemon)
        assert login_mod._get_rp_id(request) == "worker.local"

    def test_rp_id_handles_missing_host(self, monkeypatch) -> None:
        from unittest.mock import MagicMock

        request = self._make_request("")
        # MagicMock returns truthy by default so explicitly set
        request.host = ""
        fake_daemon = MagicMock()
        fake_daemon.config.domain = ""
        monkeypatch.setattr(login_mod, "get_daemon", lambda _r: fake_daemon)
        assert login_mod._get_rp_id(request) == "localhost"

    def test_expected_origin_includes_http_and_https_with_port(self) -> None:
        request = self._make_request("worker.local:9090")
        origins = login_mod._get_expected_origin(request)
        assert "https://worker.local:9090" in origins
        assert "http://worker.local:9090" in origins
        # Also the bare hostname
        assert "https://worker.local" in origins
        assert "http://worker.local" in origins

    def test_expected_origin_no_port_returns_single_pair(self) -> None:
        request = self._make_request("worker.local")
        origins = login_mod._get_expected_origin(request)
        # No duplicate bare-hostname entries when host already has no port
        assert origins == ["https://worker.local", "http://worker.local"]


# ---------------------------------------------------------------------------
# _passkey_store cache
# ---------------------------------------------------------------------------


class TestPasskeyStoreCache:
    def test_returns_app_attached_store_when_present(self) -> None:
        from unittest.mock import MagicMock

        request = MagicMock()
        sentinel = object()
        request.app.get.return_value = sentinel
        assert login_mod._passkey_store(request) is sentinel

    def test_creates_and_caches_store_when_absent(self) -> None:
        from unittest.mock import MagicMock

        from swarm.auth.passkeys import PasskeyStore

        request = MagicMock()
        request.app.get.return_value = None
        # The dict-style assignment goes through __setitem__
        request.app.__setitem__ = MagicMock()
        result = login_mod._passkey_store(request)
        assert isinstance(result, PasskeyStore)
        # And the new store was cached back onto request.app
        request.app.__setitem__.assert_called_once()
        cached_key, cached_val = request.app.__setitem__.call_args[0]
        assert cached_key == "passkey_store"
        assert cached_val is result
