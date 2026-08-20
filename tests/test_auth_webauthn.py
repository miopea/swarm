"""Tests for swarm.auth.webauthn — WebAuthn registration/authentication wrappers."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from swarm.auth.passkeys import StoredCredential
from swarm.auth.webauthn import (
    _CHALLENGE_TTL,
    _challenges,
    _cleanup_challenges,
    _pop_challenge,
    _store_challenge,
    _user_id,
    _user_name,
    credential_id_to_base64url,
    generate_authentication_options,
    generate_registration_options,
    verify_authentication,
    verify_registration,
)

# Short aliases for long patch targets
_P_REG_GEN = "swarm.auth.webauthn.webauthn.generate_registration_options"
_P_REG_VERIFY = "swarm.auth.webauthn.webauthn.verify_registration_response"
_P_AUTH_GEN = "swarm.auth.webauthn.webauthn.generate_authentication_options"
_P_AUTH_VERIFY = "swarm.auth.webauthn.webauthn.verify_authentication_response"
_P_TO_JSON = "swarm.auth.webauthn.webauthn.options_to_json"


def _make_cred(
    name: str = "test-device",
    cred_id: bytes = b"cred-abc",
    sign_count: int = 0,
) -> StoredCredential:
    return StoredCredential(
        credential_id=cred_id,
        public_key=b"fake-public-key",
        sign_count=sign_count,
        device_name=name,
        registered_at=time.time(),
    )


@pytest.fixture(autouse=True)
def _clear_challenges() -> None:
    """Ensure challenge store is empty between tests."""
    _challenges.clear()


# -------------------------------------------------------------------
# Challenge store / pop lifecycle
# -------------------------------------------------------------------


class TestStoreChallenge:
    def test_store_returns_token(self) -> None:
        token = _store_challenge(b"challenge-data")
        assert isinstance(token, str)
        assert len(token) > 10

    def test_store_adds_entry(self) -> None:
        token = _store_challenge(b"abc")
        assert token in _challenges
        challenge_bytes, expiry = _challenges[token]
        assert challenge_bytes == b"abc"
        assert expiry > time.time()

    def test_store_unique_tokens(self) -> None:
        t1 = _store_challenge(b"a")
        t2 = _store_challenge(b"b")
        assert t1 != t2

    def test_store_sets_correct_ttl(self) -> None:
        before = time.time()
        token = _store_challenge(b"data")
        after = time.time()
        _, expiry = _challenges[token]
        assert before + _CHALLENGE_TTL <= expiry
        assert expiry <= after + _CHALLENGE_TTL


class TestPopChallenge:
    def test_pop_returns_challenge(self) -> None:
        token = _store_challenge(b"my-challenge")
        result = _pop_challenge(token)
        assert result == b"my-challenge"

    def test_pop_removes_entry(self) -> None:
        token = _store_challenge(b"data")
        _pop_challenge(token)
        assert token not in _challenges

    def test_pop_unknown_token_returns_none(self) -> None:
        assert _pop_challenge("nonexistent-token") is None

    def test_pop_reuse_returns_none(self) -> None:
        """Challenge can only be used once (replay prevention)."""
        token = _store_challenge(b"one-time")
        assert _pop_challenge(token) == b"one-time"
        assert _pop_challenge(token) is None

    def test_pop_expired_challenge_returns_none(self) -> None:
        token = _store_challenge(b"old")
        challenge_bytes, _ = _challenges[token]
        _challenges[token] = (challenge_bytes, time.time() - 1)
        assert _pop_challenge(token) is None

    def test_pop_expired_also_removes_entry(self) -> None:
        token = _store_challenge(b"old")
        challenge_bytes, _ = _challenges[token]
        _challenges[token] = (challenge_bytes, time.time() - 1)
        _pop_challenge(token)
        assert token not in _challenges


# -------------------------------------------------------------------
# Challenge cleanup
# -------------------------------------------------------------------


class TestCleanupChallenges:
    def test_removes_expired(self) -> None:
        _challenges["expired"] = (b"data", time.time() - 10)
        _challenges["valid"] = (b"data", time.time() + 300)
        _cleanup_challenges()
        assert "expired" not in _challenges
        assert "valid" in _challenges

    def test_no_op_when_empty(self) -> None:
        _cleanup_challenges()  # should not raise
        assert len(_challenges) == 0

    def test_removes_all_expired(self) -> None:
        past = time.time() - 1
        _challenges["a"] = (b"x", past)
        _challenges["b"] = (b"y", past)
        _challenges["c"] = (b"z", past)
        _cleanup_challenges()
        assert len(_challenges) == 0


# -------------------------------------------------------------------
# User ID / name helpers
# -------------------------------------------------------------------


class TestUserId:
    def test_deterministic(self) -> None:
        assert _user_id("password") == _user_id("password")

    def test_different_passwords_differ(self) -> None:
        assert _user_id("a") != _user_id("b")

    def test_returns_32_bytes(self) -> None:
        result = _user_id("test")
        assert isinstance(result, bytes)
        assert len(result) == 32


class TestUserName:
    def test_returns_string(self) -> None:
        name = _user_name()
        assert isinstance(name, str)
        assert len(name) > 0

    def test_fallback_on_error(self) -> None:
        target = "swarm.auth.webauthn.getpass.getuser"
        with patch(target, side_effect=OSError):
            assert _user_name() == "operator"


# -------------------------------------------------------------------
# generate_registration_options
# -------------------------------------------------------------------


class TestGenerateRegistrationOptions:
    def test_returns_options_and_token(self) -> None:
        mock_opts = MagicMock()
        mock_opts.challenge = b"reg-challenge"

        with (
            patch(_P_REG_GEN, return_value=mock_opts),
            patch(_P_TO_JSON, return_value={"test": True}),
        ):
            options_json, token = generate_registration_options(
                rp_id="localhost",
                password="secret",
                existing_credentials=[],
            )

        assert options_json == {"test": True}
        assert isinstance(token, str)

    def test_passes_rp_id_and_rp_name(self) -> None:
        mock_opts = MagicMock()
        mock_opts.challenge = b"ch"

        with (
            patch(_P_REG_GEN, return_value=mock_opts) as mock_gen,
            patch(_P_TO_JSON, return_value={}),
        ):
            generate_registration_options("example.com", "pw", [])

        kw = mock_gen.call_args.kwargs
        assert kw["rp_id"] == "example.com"
        assert kw["rp_name"] == "Swarm (legacy)"

    def test_excludes_existing_credentials(self) -> None:
        mock_opts = MagicMock()
        mock_opts.challenge = b"ch"
        cred = _make_cred(cred_id=b"existing-id")

        with (
            patch(_P_REG_GEN, return_value=mock_opts) as mock_gen,
            patch(_P_TO_JSON, return_value={}),
        ):
            generate_registration_options("localhost", "pw", [cred])

        exclude = mock_gen.call_args.kwargs["exclude_credentials"]
        assert len(exclude) == 1
        assert exclude[0].id == b"existing-id"

    def test_stores_challenge_from_options(self) -> None:
        mock_opts = MagicMock()
        mock_opts.challenge = b"the-challenge-bytes"

        with (
            patch(_P_REG_GEN, return_value=mock_opts),
            patch(_P_TO_JSON, return_value={}),
        ):
            _, token = generate_registration_options("localhost", "pw", [])

        result = _pop_challenge(token)
        assert result == b"the-challenge-bytes"


# -------------------------------------------------------------------
# verify_registration
# -------------------------------------------------------------------


class TestVerifyRegistration:
    def test_success_returns_stored_credential(self) -> None:
        token = _store_challenge(b"reg-challenge")

        mock_v = MagicMock()
        mock_v.credential_id = b"new-cred-id"
        mock_v.credential_public_key = b"pub-key"
        mock_v.sign_count = 1

        with patch(_P_REG_VERIFY, return_value=mock_v):
            result = verify_registration(
                rp_id="localhost",
                challenge_token=token,
                response={"id": "test"},
                expected_origin="http://localhost",
            )

        assert isinstance(result, StoredCredential)
        assert result.credential_id == b"new-cred-id"
        assert result.public_key == b"pub-key"
        assert result.sign_count == 1
        assert result.device_name == ""
        assert result.registered_at > 0

    def test_passes_correct_params_to_library(self) -> None:
        token = _store_challenge(b"my-challenge")

        mock_v = MagicMock()
        mock_v.credential_id = b"id"
        mock_v.credential_public_key = b"key"
        mock_v.sign_count = 0

        response_data = {"id": "abc", "response": {}}

        with patch(_P_REG_VERIFY, return_value=mock_v) as mock_fn:
            verify_registration(
                "example.com",
                token,
                response_data,
                "https://example.com",
            )

        mock_fn.assert_called_once_with(
            credential=response_data,
            expected_challenge=b"my-challenge",
            expected_rp_id="example.com",
            expected_origin="https://example.com",
            require_user_verification=False,
        )

    def test_expired_challenge_raises(self) -> None:
        token = _store_challenge(b"old")
        ch, _ = _challenges[token]
        _challenges[token] = (ch, time.time() - 1)

        with pytest.raises(ValueError, match="Challenge expired"):
            verify_registration("localhost", token, {})

    def test_invalid_token_raises(self) -> None:
        with pytest.raises(ValueError, match="Challenge expired"):
            verify_registration("localhost", "bad-token", {})

    def test_challenge_consumed_after_verify(self) -> None:
        token = _store_challenge(b"once")

        mock_v = MagicMock()
        mock_v.credential_id = b"id"
        mock_v.credential_public_key = b"key"
        mock_v.sign_count = 0

        with patch(_P_REG_VERIFY, return_value=mock_v):
            verify_registration("localhost", token, {})

        # Second attempt -- challenge was consumed
        with pytest.raises(ValueError, match="Challenge expired"):
            verify_registration("localhost", token, {})

    def test_library_error_propagates(self) -> None:
        token = _store_challenge(b"ch")

        with patch(
            _P_REG_VERIFY,
            side_effect=Exception("Verification failed"),
        ):
            with pytest.raises(Exception, match="Verification failed"):
                verify_registration("localhost", token, {})

    def test_accepts_origin_list(self) -> None:
        token = _store_challenge(b"ch")

        mock_v = MagicMock()
        mock_v.credential_id = b"id"
        mock_v.credential_public_key = b"key"
        mock_v.sign_count = 0

        origins = [
            "http://localhost",
            "https://swarm.example.com",
        ]

        with patch(_P_REG_VERIFY, return_value=mock_v) as mock_fn:
            verify_registration(
                "localhost",
                token,
                {},
                expected_origin=origins,
            )

        assert mock_fn.call_args.kwargs["expected_origin"] == origins


# -------------------------------------------------------------------
# generate_authentication_options
# -------------------------------------------------------------------


class TestGenerateAuthenticationOptions:
    def test_returns_options_and_token(self) -> None:
        mock_opts = MagicMock()
        mock_opts.challenge = b"auth-challenge"

        with (
            patch(_P_AUTH_GEN, return_value=mock_opts),
            patch(_P_TO_JSON, return_value={"auth": True}),
        ):
            opts_json, token = generate_authentication_options(
                "localhost",
                [_make_cred()],
            )

        assert opts_json == {"auth": True}
        assert isinstance(token, str)

    def test_passes_allow_credentials(self) -> None:
        mock_opts = MagicMock()
        mock_opts.challenge = b"ch"
        cred = _make_cred(cred_id=b"allowed-id")

        with (
            patch(_P_AUTH_GEN, return_value=mock_opts) as mock_gen,
            patch(_P_TO_JSON, return_value={}),
        ):
            generate_authentication_options("localhost", [cred])

        allow = mock_gen.call_args.kwargs["allow_credentials"]
        assert len(allow) == 1
        assert allow[0].id == b"allowed-id"

    def test_empty_credentials_list(self) -> None:
        mock_opts = MagicMock()
        mock_opts.challenge = b"ch"

        with (
            patch(_P_AUTH_GEN, return_value=mock_opts) as mock_gen,
            patch(_P_TO_JSON, return_value={}),
        ):
            generate_authentication_options("localhost", [])

        assert mock_gen.call_args.kwargs["allow_credentials"] == []

    def test_stores_challenge(self) -> None:
        mock_opts = MagicMock()
        mock_opts.challenge = b"stored-auth-challenge"

        with (
            patch(_P_AUTH_GEN, return_value=mock_opts),
            patch(_P_TO_JSON, return_value={}),
        ):
            _, token = generate_authentication_options("localhost", [])

        assert _pop_challenge(token) == b"stored-auth-challenge"

    def test_multiple_credentials(self) -> None:
        mock_opts = MagicMock()
        mock_opts.challenge = b"ch"
        creds = [
            _make_cred(cred_id=b"id-1"),
            _make_cred(cred_id=b"id-2"),
        ]

        with (
            patch(_P_AUTH_GEN, return_value=mock_opts) as mock_gen,
            patch(_P_TO_JSON, return_value={}),
        ):
            generate_authentication_options("localhost", creds)

        allow = mock_gen.call_args.kwargs["allow_credentials"]
        assert len(allow) == 2


# -------------------------------------------------------------------
# verify_authentication
# -------------------------------------------------------------------


class TestVerifyAuthentication:
    def test_success_returns_new_sign_count(self) -> None:
        token = _store_challenge(b"auth-ch")
        cred = _make_cred(sign_count=5)

        mock_v = MagicMock()
        mock_v.new_sign_count = 6

        with patch(_P_AUTH_VERIFY, return_value=mock_v):
            result = verify_authentication(
                "localhost",
                token,
                cred,
                {"id": "test"},
            )

        assert result == 6

    def test_passes_correct_params_to_library(self) -> None:
        token = _store_challenge(b"the-challenge")
        cred = _make_cred(sign_count=10)
        response_data = {"id": "xyz"}

        mock_v = MagicMock()
        mock_v.new_sign_count = 11

        with patch(_P_AUTH_VERIFY, return_value=mock_v) as mock_fn:
            verify_authentication(
                "example.com",
                token,
                cred,
                response_data,
                "https://example.com",
            )

        mock_fn.assert_called_once_with(
            credential=response_data,
            expected_challenge=b"the-challenge",
            expected_rp_id="example.com",
            expected_origin="https://example.com",
            credential_public_key=b"fake-public-key",
            credential_current_sign_count=10,
            require_user_verification=False,
        )

    def test_expired_challenge_raises(self) -> None:
        token = _store_challenge(b"old")
        ch, _ = _challenges[token]
        _challenges[token] = (ch, time.time() - 1)
        cred = _make_cred()

        with pytest.raises(ValueError, match="Challenge expired"):
            verify_authentication("localhost", token, cred, {})

    def test_invalid_token_raises(self) -> None:
        cred = _make_cred()
        with pytest.raises(ValueError, match="Challenge expired"):
            verify_authentication(
                "localhost",
                "no-such-token",
                cred,
                {},
            )

    def test_challenge_consumed_after_verify(self) -> None:
        token = _store_challenge(b"once")
        cred = _make_cred()

        mock_v = MagicMock()
        mock_v.new_sign_count = 1

        with patch(_P_AUTH_VERIFY, return_value=mock_v):
            verify_authentication("localhost", token, cred, {})

        with pytest.raises(ValueError, match="Challenge expired"):
            verify_authentication("localhost", token, cred, {})

    def test_library_error_propagates(self) -> None:
        token = _store_challenge(b"ch")
        cred = _make_cred()

        with patch(
            _P_AUTH_VERIFY,
            side_effect=Exception("Auth failed"),
        ):
            with pytest.raises(Exception, match="Auth failed"):
                verify_authentication(
                    "localhost",
                    token,
                    cred,
                    {},
                )

    def test_accepts_origin_list(self) -> None:
        token = _store_challenge(b"ch")
        cred = _make_cred()
        origins = [
            "http://localhost",
            "https://swarm.example.com",
        ]

        mock_v = MagicMock()
        mock_v.new_sign_count = 1

        with patch(_P_AUTH_VERIFY, return_value=mock_v) as mock_fn:
            verify_authentication(
                "localhost",
                token,
                cred,
                {},
                expected_origin=origins,
            )

        assert mock_fn.call_args.kwargs["expected_origin"] == origins


# -------------------------------------------------------------------
# credential_id_to_base64url
# -------------------------------------------------------------------


class TestCredentialIdToBase64url:
    def test_encodes_bytes(self) -> None:
        result = credential_id_to_base64url(b"\x00\x01\x02\xff")
        assert isinstance(result, str)
        # base64url should not contain + or /
        assert "+" not in result
        assert "/" not in result

    def test_roundtrip(self) -> None:
        from webauthn.helpers import base64url_to_bytes

        original = b"credential-id-bytes-123"
        encoded = credential_id_to_base64url(original)
        decoded = base64url_to_bytes(encoded)
        assert decoded == original

    def test_empty_bytes(self) -> None:
        result = credential_id_to_base64url(b"")
        assert isinstance(result, str)
