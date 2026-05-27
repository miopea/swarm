"""Handler for the ``swarm_claim_file`` MCP tool.

Extracted from ``mcp/tools.py`` (task #518).
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import ClaimFileArgs
from swarm.mcp.types import TextContent

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_claim_file",
        "description": (
            "Place an advisory lock on a file before editing it, so other workers can see "
            "the claim and avoid concurrent edits. Call this right before you start editing "
            "any shared file — config files (package.json, pyproject.toml), shared utilities, "
            "API contracts, shared types. Claims auto-expire so you don't need to release; "
            "a fresh claim renews the timer. Path MUST be absolute (the daemon will reject "
            "relative paths). If another worker holds the claim, the tool returns an error "
            "naming them — ask them via swarm_send_message rather than forcing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute filesystem path to claim. Use realpath output — "
                        "symlinks are resolved server-side."
                    ),
                },
            },
            "required": ["path"],
            "examples": [
                {"path": "/home/user/projects/repo/src/shared/types.ts"},
                {"path": "/home/user/projects/repo/pyproject.toml"},
            ],
        },
    },
]


def _handle_claim_file(d: SwarmDaemon, worker_name: str, args: ClaimFileArgs) -> list[TextContent]:
    path = args.get("path", "")
    if not path:
        return [{"type": "text", "text": "Missing 'path'"}]
    resolved = os.path.realpath(path)
    now = time.time()
    lock = d.file_locks.get(resolved)
    if lock:
        owner, ts = lock
        if owner != worker_name and (now - ts) < d._file_lock_ttl:
            return [{"type": "text", "text": f"File claimed by {owner}."}]
    d.file_locks[resolved] = (worker_name, now)
    return [{"type": "text", "text": f"File claimed: {path}"}]


HANDLERS = {"swarm_claim_file": _handle_claim_file}
