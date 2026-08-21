"""Passkey (WebAuthn) credential storage — swarm.db with file fallback."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from swarm.paths import state_dir

_log = logging.getLogger("swarm.auth.passkeys")

_DEFAULT_PATH = state_dir() / "passkeys.json"


@dataclass
class StoredCredential:
    """A registered WebAuthn credential."""

    credential_id: bytes
    public_key: bytes
    sign_count: int
    device_name: str
    registered_at: float

    def to_dict(self) -> dict[str, object]:
        return {
            "credential_id": bytes_to_base64url(self.credential_id),
            "public_key": bytes_to_base64url(self.public_key),
            "sign_count": self.sign_count,
            "device_name": self.device_name,
            "registered_at": self.registered_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> StoredCredential:
        return cls(
            credential_id=base64url_to_bytes(str(d["credential_id"])),
            public_key=base64url_to_bytes(str(d["public_key"])),
            sign_count=int(d.get("sign_count", 0)),
            device_name=str(d.get("device_name", "")),
            registered_at=float(d.get("registered_at", 0)),
        )


class PasskeyStore:
    """Manages WebAuthn credentials persisted in a JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _DEFAULT_PATH
        self._use_db = path is None  # Only use DB when using default path

    def load(self) -> list[StoredCredential]:
        from swarm.db.secrets import load_secret

        data = load_secret("passkeys") if self._use_db else None
        if data is None and self._path.exists():
            try:
                data = json.loads(self._path.read_text())
            except Exception:
                _log.warning("Failed to load passkeys", exc_info=True)
                return []
        if not data:
            return []
        try:
            return [StoredCredential.from_dict(d) for d in data]
        except Exception:
            _log.warning("Failed to parse passkeys", exc_info=True)
            return []

    def save(self, credentials: list[StoredCredential]) -> None:
        cred_dicts = [c.to_dict() for c in credentials]
        from swarm.db.secrets import save_secret

        if self._use_db and save_secret("passkeys", cred_dicts):
            return
        # File fallback
        self._path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(cred_dicts, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        closed = False
        try:
            os.write(fd, content.encode())
            os.fchmod(fd, 0o600)
            os.close(fd)
            closed = True
            os.replace(tmp, str(self._path))
        except BaseException:
            if not closed:
                os.close(fd)
            Path(tmp).unlink(missing_ok=True)
            raise

    def add(self, credential: StoredCredential) -> None:
        creds = self.load()
        creds.append(credential)
        self.save(creds)

    def remove(self, credential_id: bytes) -> None:
        creds = [c for c in self.load() if c.credential_id != credential_id]
        self.save(creds)

    def update_sign_count(self, credential_id: bytes, new_count: int) -> None:
        creds = self.load()
        for c in creds:
            if c.credential_id == credential_id:
                c.sign_count = new_count
                break
        self.save(creds)

    def get_all(self) -> list[StoredCredential]:
        return self.load()

    def has_any(self) -> bool:
        return bool(self.load())
