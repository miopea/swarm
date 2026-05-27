"""MCP tool registry + dispatcher.

Per-tool schemas + handlers live in :mod:`swarm.mcp.handlers._*`; this
module aggregates them into the unified ``TOOLS`` / ``_HANDLERS``
surface the MCP server publishes, exposes the
:func:`handle_tool_call` dispatcher, and provides the source-drift
probe the dashboard uses to detect stale daemon bytecode.

Task #518 split the monolithic ``tools.py`` into per-concern handler
modules. Tests and external callers still import a few private
``_handle_*`` symbols by name from this module — those are
re-exported below so the split is invisible to existing call sites.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

# Per-domain modules contribute both their schemas and their handlers.
from swarm.mcp.handlers._batch import HANDLERS as _BATCH_H
from swarm.mcp.handlers._batch import TOOLS as _BATCH_T
from swarm.mcp.handlers._blockers import HANDLERS as _BLOCKER_H
from swarm.mcp.handlers._blockers import TOOLS as _BLOCKER_T
from swarm.mcp.handlers._create import HANDLERS as _CREATE_H
from swarm.mcp.handlers._create import TOOLS as _CREATE_T
from swarm.mcp.handlers._create import _handle_create_task  # noqa: F401
from swarm.mcp.handlers._email import HANDLERS as _EMAIL_H
from swarm.mcp.handlers._email import TOOLS as _EMAIL_T
from swarm.mcp.handlers._files import HANDLERS as _FILES_H
from swarm.mcp.handlers._files import TOOLS as _FILES_T
from swarm.mcp.handlers._learnings import HANDLERS as _LEARN_H
from swarm.mcp.handlers._learnings import TOOLS as _LEARN_T
from swarm.mcp.handlers._messages import HANDLERS as _MSG_H
from swarm.mcp.handlers._messages import TOOLS as _MSG_T
from swarm.mcp.handlers._messages import _handle_check_messages  # noqa: F401
from swarm.mcp.handlers._park import HANDLERS as _PARK_H
from swarm.mcp.handlers._park import TOOLS as _PARK_T

# Re-exports for backward compatibility with existing call sites that
# imported these handlers by name from ``swarm.mcp.tools``. (Tests reach
# for ``_handle_park_task``, ``_handle_get_playbooks``,
# ``_handle_create_task``, ``_handle_complete_task``, ``_handle_task_status``
# directly; the split keeps those imports working unchanged.)
from swarm.mcp.handlers._park import _handle_park_task  # noqa: F401
from swarm.mcp.handlers._playbooks import HANDLERS as _PB_H
from swarm.mcp.handlers._playbooks import TOOLS as _PB_T
from swarm.mcp.handlers._playbooks import _handle_get_playbooks  # noqa: F401
from swarm.mcp.handlers._progress import HANDLERS as _PROG_H
from swarm.mcp.handlers._progress import TOOLS as _PROG_T
from swarm.mcp.handlers._tasks import HANDLERS as _TASKS_H
from swarm.mcp.handlers._tasks import TOOLS as _TASKS_T
from swarm.mcp.handlers._tasks import (  # noqa: F401
    _handle_complete_task,
    _handle_task_status,
)
from swarm.mcp.types import HandlerResult

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


class ToolsSourceDrift(TypedDict):
    """Return shape for :func:`tools_source_drift` — dashboard probe."""

    drift: bool
    source_path: str
    startup_hash: str
    current_hash: str


def _hash_source(path: Path) -> str:
    """Return sha256 of *path*, or empty string if the file can't be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


# Path + hash of this module captured at import time. The hash frozen here is
# the one the running daemon is actually serving — if tools.py on disk
# changes, the daemon keeps publishing the old ``TOOLS`` list until it
# reloads. ``tools_source_drift()`` compares stored vs current so the
# dashboard can prompt the operator to reload before live MCP calls hit a
# stale schema (regression scenario: task #169 fix landed but workers
# kept seeing the old no-``number`` schema until the daemon restarted).
_SOURCE_PATH: Path = Path(__file__).resolve()
_SOURCE_HASH_AT_IMPORT: str = _hash_source(_SOURCE_PATH)


def tools_source_drift() -> ToolsSourceDrift:
    """Return whether ``tools.py`` on disk differs from the imported copy.

    Output shape::

        {
          "drift": bool,           # True when current hash != import-time hash
          "source_path": str,      # absolute path to tools.py
          "startup_hash": str,     # sha256 captured when daemon started
          "current_hash": str,     # sha256 of the file right now ('' on read error)
        }

    Intended for the dashboard's dev-mode footer: when ``drift`` is True,
    tell the operator to hit Reload before MCP tool schemas go out of sync
    with the source of truth on disk.
    """
    current = _hash_source(_SOURCE_PATH)
    return {
        "drift": bool(current) and current != _SOURCE_HASH_AT_IMPORT,
        "source_path": str(_SOURCE_PATH),
        "startup_hash": _SOURCE_HASH_AT_IMPORT,
        "current_hash": current,
    }


# ---------------------------------------------------------------------------
# Registry — worker MCP tools (Queen tools fold in at the bottom)
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    *_MSG_T,
    *_BLOCKER_T,
    *_PARK_T,
    *_EMAIL_T,
    *_TASKS_T,
    *_CREATE_T,
    *_FILES_T,
    *_LEARN_T,
    *_PB_T,
    *_PROG_T,
    *_BATCH_T,
]

_HANDLERS = {
    **_MSG_H,
    **_BLOCKER_H,
    **_PARK_H,
    **_EMAIL_H,
    **_TASKS_H,
    **_CREATE_H,
    **_FILES_H,
    **_LEARN_H,
    **_PB_H,
    **_PROG_H,
    **_BATCH_H,
}

# Tool name → handler function mapping (populated above + Queen tools below).
_TOOL_NAMES = {t["name"] for t in TOOLS}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def handle_tool_call(
    daemon: SwarmDaemon,
    worker_name: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> HandlerResult:
    """Dispatch a tool call and return MCP content blocks.

    Phase 3 (2026-05-08): handlers may now return either the legacy
    bare ``list[dict]`` content array OR a dict wrapper with
    ``content`` + optional ``structuredContent``/``_meta`` keys.
    Claude Code 2.1.x prefers ``structuredContent`` when present
    (verified in the leaked source at ``services/mcp/client.ts:2662``)
    so view-side tools can deliver typed JSON the model can query
    directly without re-parsing markdown summaries.

    The dispatcher passes either shape through verbatim. The MCP
    server-side ``_handle_tools_call`` strips the wrapper before
    serializing into the JSON-RPC envelope.
    """
    from swarm.drones.log import LogCategory, SystemAction

    handler = _HANDLERS.get(tool_name)
    if not handler:
        return [{"type": "text", "text": f"Unknown tool: {tool_name}"}]
    try:
        result = handler(daemon, worker_name, arguments)
    except Exception as e:
        return [{"type": "text", "text": f"Error: {e}"}]

    # Extract the operator-facing detail line for the buzz log audit
    # entry, regardless of which return shape the handler used.
    if isinstance(result, dict):
        content = result.get("content") or []
    else:
        content = result
    short_name = tool_name.removeprefix("swarm_")
    detail = content[0].get("text", "")[:120] if content else ""
    daemon.drone_log.add(
        SystemAction.OPERATOR,
        worker_name,
        f"mcp:{short_name} → {detail}",
        category=LogCategory.WORKER,
    )
    return result


# Queen-only tools live in their own module to keep the core tools.py
# focused on the shared worker surface. They're folded into the live
# TOOLS list and _HANDLERS map at import time so the MCP server
# publishes a single unified tool catalog.
from swarm.mcp.queen_tools import QUEEN_HANDLERS, QUEEN_TOOLS  # noqa: E402

TOOLS.extend(QUEEN_TOOLS)
_HANDLERS.update(QUEEN_HANDLERS)
_TOOL_NAMES.update(QUEEN_HANDLERS.keys())
