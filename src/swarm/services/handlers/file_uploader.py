"""Google Drive file uploader service handler."""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, cast

import aiohttp

from swarm.logging import get_logger
from swarm.services.registry import ServiceContext, ServiceResult

_log = get_logger("services.file_uploader")

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
_SCOPE = "https://www.googleapis.com/auth/drive.file"


def _b64url(data: bytes) -> str:
    """Base64url-encode without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _build_jwt(sa_info: dict[str, Any]) -> str:
    """Build a signed RS256 JWT for Google service account auth."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    now = int(time.time())
    payload = _b64url(
        json.dumps(
            {
                "iss": sa_info["client_email"],
                "scope": _SCOPE,
                "aud": _TOKEN_URL,
                "iat": now,
                "exp": now + 3600,
            }
        ).encode()
    )
    message = f"{header}.{payload}".encode()

    private_key = cast(
        rsa.RSAPrivateKey,
        serialization.load_pem_private_key(sa_info["private_key"].encode(), password=None),
    )
    signature = private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64url(signature)}"


async def _get_access_token(
    session: aiohttp.ClientSession,
    sa_info: dict[str, Any],
) -> str:
    """Exchange a self-signed JWT for a Google access token."""
    jwt_token = _build_jwt(sa_info)
    async with session.post(
        _TOKEN_URL,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": jwt_token,
        },
    ) as resp:
        resp.raise_for_status()
        data: dict[str, Any] = await resp.json()
        token = data["access_token"]
        if not isinstance(token, str):
            raise RuntimeError("token endpoint returned non-string access_token")
        return token


@dataclass
class FileUploader:
    """Upload a local file to Google Drive via service account."""

    description = "Upload a local file to Google Drive via a service account."
    example_config: ClassVar[dict[str, Any]] = {
        "credentials_path": "/path/to/sa.json",
        "file_path": "/path/to/file.pdf",
        "mime_type": "application/pdf",
        "folder_id": "",
    }

    async def execute(
        self,
        config: dict[str, Any],
        context: ServiceContext,
    ) -> ServiceResult:
        creds_path = config.get("credentials_path") or os.environ.get(
            "GOOGLE_APPLICATION_CREDENTIALS", ""
        )
        if not creds_path:
            return ServiceResult(
                success=False,
                error="Missing credentials_path in config"
                " and GOOGLE_APPLICATION_CREDENTIALS env var",
            )

        file_path = config.get("file_path", "")
        if not file_path or not Path(file_path).is_file():
            return ServiceResult(success=False, error=f"File not found: {file_path}")

        try:
            sa_info = json.loads(Path(creds_path).read_text())
        except (json.JSONDecodeError, OSError) as exc:
            return ServiceResult(success=False, error=f"Invalid credentials: {exc}")

        mime_type = config.get("mime_type", "application/octet-stream")
        folder_id = config.get("folder_id")

        try:
            async with aiohttp.ClientSession() as session:
                access_token = await _get_access_token(session, sa_info)

                # Build multipart upload
                metadata: dict[str, Any] = {"name": Path(file_path).name}
                if folder_id:
                    metadata["parents"] = [folder_id]

                with aiohttp.MultipartWriter("related") as mpw:
                    meta_part = mpw.append_json(metadata)
                    meta_part.set_content_disposition("form-data", name="metadata")

                    file_bytes = Path(file_path).read_bytes()
                    file_part = mpw.append(
                        file_bytes,
                        {"Content-Type": mime_type},
                    )
                    file_part.set_content_disposition("form-data", name="file")

                    headers = {
                        "Authorization": f"Bearer {access_token}",
                    }
                    async with session.post(
                        f"{_UPLOAD_URL}?uploadType=multipart&fields=id,webViewLink,webContentLink",
                        data=mpw,
                        headers=headers,
                    ) as resp:
                        resp.raise_for_status()
                        result = await resp.json()

        except aiohttp.ClientError as exc:
            _log.error("Drive upload error: %s", exc)
            return ServiceResult(success=False, error=f"HTTP error: {exc}")

        _log.info("uploaded %s → %s", file_path, result.get("id"))
        return ServiceResult(
            data={
                "file_id": result.get("id", ""),
                "web_view_link": result.get("webViewLink", ""),
                "web_content_link": result.get("webContentLink", ""),
            }
        )
