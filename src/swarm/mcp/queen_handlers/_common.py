"""Shared helpers used by every Queen MCP handler.

Extracted from ``mcp/queen_tools.py`` (task #519). The permission gate
(``_assert_queen``) and the ``_clamp`` int-coercion helper are imported
by every per-domain handler module under :mod:`swarm.mcp.queen_handlers`.
"""

from __future__ import annotations

from typing import Any

from swarm.mcp.types import TextContent
from swarm.worker.worker import QUEEN_WORKER_NAME

_PERMISSION_DENIED: list[TextContent] = [
    {
        "type": "text",
        "text": (
            "Permission denied: this tool is only available to the Queen. "
            f"Caller identity must be '{QUEEN_WORKER_NAME}'."
        ),
    }
]


def _assert_queen(worker_name: str) -> list[TextContent] | None:
    """Return an error payload if *worker_name* is not the Queen, else None."""
    if worker_name != QUEEN_WORKER_NAME:
        return _PERMISSION_DENIED
    return None


def _clamp(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, n))
