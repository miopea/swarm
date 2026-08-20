"""WebAuthn registration and authentication wrappers.

Wraps the ``webauthn`` library for passkey flows with in-memory challenge
storage (5-minute TTL).
"""

from __future__ import annotations

import getpass
import hashlib
import secrets
import time
from typing import Any

import webauthn
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from swarm.auth.passkeys import StoredCredential

_RP_NAME = "Swarm (legacy)"

# In-memory challenge store: token -> (challenge_bytes, expiry_ts)
_challenges: dict[str, tuple[bytes, float]] = {}
_CHALLENGE_TTL = 300  # 5 minutes


def _cleanup_challenges() -> None:
    """Remove expired challenges."""
    now = time.time()
    expired = [k for k, (_, exp) in _challenges.items() if now > exp]
    for k in expired:
        del _challenges[k]


def _store_challenge(challenge: bytes) -> str:
    """Store a challenge and return a token to retrieve it."""
    _cleanup_challenges()
    token = secrets.token_urlsafe(32)
    _challenges[token] = (challenge, time.time() + _CHALLENGE_TTL)
    return token


def _pop_challenge(token: str) -> bytes | None:
    """Retrieve and remove a stored challenge by token."""
    _cleanup_challenges()
    entry = _challenges.pop(token, None)
    if entry is None:
        return None
    challenge, expiry = entry
    if time.time() > expiry:
        return None
    return challenge


def _user_id(password: str) -> bytes:
    """Deterministic user ID derived from the API password."""
    return hashlib.sha256(f"swarm-user:{password}".encode()).digest()[:32]


def _user_name() -> str:
    """Display name for the WebAuthn user."""
    try:
        return getpass.getuser()
    except Exception:
        return "operator"


def generate_registration_options(
    rp_id: str,
    password: str,
    existing_credentials: list[StoredCredential],
) -> tuple[dict[str, Any], str]:
    """Generate WebAuthn registration options.

    Returns ``(options_json_dict, challenge_token)``.
    """
    exclude = [PublicKeyCredentialDescriptor(id=c.credential_id) for c in existing_credentials]
    opts = webauthn.generate_registration_options(
        rp_id=rp_id,
        rp_name=_RP_NAME,
        user_id=_user_id(password),
        user_name=_user_name(),
        user_display_name=_user_name(),
        exclude_credentials=exclude,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    token = _store_challenge(opts.challenge)
    return webauthn.options_to_json(opts), token


def verify_registration(
    rp_id: str,
    challenge_token: str,
    response: dict[str, Any],
    expected_origin: str | list[str] = "http://localhost",
) -> StoredCredential:
    """Verify a registration response and return a StoredCredential.

    Raises ``Exception`` on failure.
    """
    challenge = _pop_challenge(challenge_token)
    if challenge is None:
        raise ValueError("Challenge expired or invalid")

    verification = webauthn.verify_registration_response(
        credential=response,
        expected_challenge=challenge,
        expected_rp_id=rp_id,
        expected_origin=expected_origin,
        require_user_verification=False,
    )
    return StoredCredential(
        credential_id=verification.credential_id,
        public_key=verification.credential_public_key,
        sign_count=verification.sign_count,
        device_name="",  # caller sets this
        registered_at=time.time(),
    )


def generate_authentication_options(
    rp_id: str,
    credentials: list[StoredCredential],
) -> tuple[dict[str, Any], str]:
    """Generate WebAuthn authentication options.

    Returns ``(options_json_dict, challenge_token)``.
    """
    allow = [PublicKeyCredentialDescriptor(id=c.credential_id) for c in credentials]
    opts = webauthn.generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    token = _store_challenge(opts.challenge)
    return webauthn.options_to_json(opts), token


def verify_authentication(
    rp_id: str,
    challenge_token: str,
    credential: StoredCredential,
    response: dict[str, Any],
    expected_origin: str | list[str] = "http://localhost",
) -> int:
    """Verify an authentication response.

    Returns the new sign count. Raises ``Exception`` on failure.
    """
    challenge = _pop_challenge(challenge_token)
    if challenge is None:
        raise ValueError("Challenge expired or invalid")

    verification = webauthn.verify_authentication_response(
        credential=response,
        expected_challenge=challenge,
        expected_rp_id=rp_id,
        expected_origin=expected_origin,
        credential_public_key=credential.public_key,
        credential_current_sign_count=credential.sign_count,
        require_user_verification=False,
    )
    return verification.new_sign_count


def credential_id_to_base64url(credential_id: bytes) -> str:
    """Encode a credential ID as base64url for JSON transport."""
    return bytes_to_base64url(credential_id)
