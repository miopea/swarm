"""Shared HTTP helpers for the API and web layers."""

from __future__ import annotations

import functools
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aiohttp import web

from swarm.logging import get_logger

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("server.helpers")

WORKER_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
MAX_QUERY_LIMIT = 1000


def json_error(
    msg: str, status: int = 400, *, error_id: str | None = None, request_id: str | None = None
) -> web.Response:
    """Return a JSON error response."""
    body: dict[str, object] = {"error": msg}
    if error_id:
        body["error_id"] = error_id
    if request_id:
        body["request_id"] = request_id
    return web.json_response(body, status=status)


def get_daemon(request: web.Request) -> SwarmDaemon:
    """Extract the SwarmDaemon from the request's app dict."""
    return request.app["daemon"]


def parse_limit(request: web.Request, *, default: int = 50) -> int:
    """Parse a 'limit' query parameter, clamped to MAX_QUERY_LIMIT."""
    try:
        return min(int(request.query.get("limit", str(default))), MAX_QUERY_LIMIT)
    except ValueError:
        return default


def parse_offset(request: web.Request) -> int:
    """Parse an 'offset' query parameter, clamped to >= 0."""
    try:
        return max(0, int(request.query.get("offset", "0")))
    except ValueError:
        return 0


def validate_worker_name(name: str) -> str | None:
    """Validate worker name, return error message or None."""
    if not name or not WORKER_NAME_RE.match(name):
        return f"Invalid worker name: '{name}'. Use alphanumeric, dash, or underscore only."
    return None


def validate_body(
    body: dict[str, object],
    required: list[str] | None = None,
    max_lengths: dict[str, int] | None = None,
) -> str | None:
    """Validate a request body. Returns error message or None if valid.

    *required*: field names that must be non-empty strings.
    *max_lengths*: field name → max character count.
    """
    for field in required or []:
        val = body.get(field)
        if not val or (isinstance(val, str) and not val.strip()):
            return f"{field} is required"
    for field, limit in (max_lengths or {}).items():
        val = body.get(field, "")
        if isinstance(val, str) and len(val) > limit:
            return f"{field} exceeds {limit} characters"
    return None


def require_message(body: dict[str, object]) -> str | web.Response:
    """Extract and validate a non-empty message string from request body.

    Returns the message or a json_error Response.
    """
    message = body.get("message", "")
    if not isinstance(message, str) or not message.strip():
        return json_error("message must be a non-empty string")
    return message


def truncate_preview(text: str, max_len: int = 80) -> str:
    """Truncate text with ellipsis for log/display previews."""
    return text[:max_len] + ("\u2026" if len(text) > max_len else "")


async def read_file_field(request: web.Request, field_name: str = "file") -> tuple[str, bytes]:
    """Read a multipart file upload field.

    Returns ``(filename, data)``.
    Raises ``ValueError`` on missing/wrong field or empty data so the
    caller's error-handling decorator can map it to a 400 response.
    """
    reader = await request.multipart()
    field = await reader.next()
    if not field or field.name != field_name:
        raise ValueError(f"{field_name} field required")
    filename = field.filename or "upload"
    data = await field.read(decode=False)
    if not data:
        raise ValueError("empty file")
    return filename, data


def handle_errors(
    handler: Callable[[web.Request], Awaitable[web.Response]],
) -> Callable[[web.Request], Awaitable[web.Response]]:
    """Decorator that maps common exceptions to HTTP error responses.

    - JSONDecodeError    → 400 ("Invalid JSON in request body")
    - WorkerNotFoundError → 404
    - TaskOperationError  → 404 (not found) or 409 (wrong state)
    - SwarmOperationError → 409 (Conflict — operation can't proceed in
      current state).  Pre-Phase-C of the duplication sweep this was
      400 in server routes and 409 in web routes; unified on 409 since
      SwarmOperationError semantically means "conflict with current
      state" (Queen offline, worker in wrong state, task already
      shipped, etc.) — not "your input was malformed".
    - ValueError          → 400 (validation errors from config parsing)
    - Exception           → 500 with error_id + request_id, _log.exception
      captures the traceback so the operator can correlate by error_id

    Canonical error decorator for both server and web route layers.
    Replaces the older ``handle_swarm_errors`` from ``swarm.web.app``
    (deleted in Phase C of the duplication-cluster sweep, 2026.5.5.10).
    """
    from swarm.integrations.jira import JiraAuthError
    from swarm.server.daemon import SwarmOperationError, TaskOperationError, WorkerNotFoundError

    async def wrapper(request: web.Request) -> web.Response:
        try:
            return await handler(request)
        except json.JSONDecodeError:
            return json_error("Invalid JSON in request body")
        except JiraAuthError as e:
            # Expected operational state (token expired/revoked), not a
            # crash — surface the actionable message, not a 500+error_id.
            return json_error(str(e), 400)
        except WorkerNotFoundError as e:
            return json_error(str(e), 404)
        except TaskOperationError as e:
            return json_error(str(e), e.status_code)
        except SwarmOperationError as e:
            return json_error(str(e), 409)
        except ValueError as e:
            return json_error(str(e))
        except web.HTTPException:
            # aiohttp's own HTTP responses (e.g. a handler raising
            # ``web.HTTPServiceUnavailable``) are intentional responses, not
            # crashes — let them propagate instead of masking them as a 500.
            raise
        except Exception:
            eid = uuid.uuid4().hex[:12]
            rid = request.get("request_id", "")
            _log.exception(
                "unhandled error in %s [error_id=%s] [request_id=%s]",
                handler.__name__,
                eid,
                rid,
            )
            return json_error("Internal server error", 500, error_id=eid, request_id=rid)

    functools.update_wrapper(wrapper, handler)
    return wrapper


async def worker_action(
    request: web.Request,
    action: Callable[[SwarmDaemon, str], Awaitable[None]],
    success_status: str,
) -> web.Response:
    """Common handler for worker-targeted actions with WorkerNotFoundError→404."""
    from swarm.server.daemon import SwarmOperationError, WorkerNotFoundError

    d = get_daemon(request)
    name = request.match_info["name"]
    try:
        await action(d, name)
    except WorkerNotFoundError:
        return json_error(f"Worker '{name}' not found", 404)
    except SwarmOperationError as e:
        # 409 Conflict — operation can't proceed in current state.
        # See ``handle_errors`` for why this is 409 not 400.
        return json_error(str(e), 409)
    return web.json_response({"status": success_status, "worker": name})
