"""Shared helpers used by every Queen MCP handler.

Extracted from ``mcp/queen_tools.py`` (task #519). The permission gate
(``_assert_queen``) and the ``_clamp`` int-coercion helper are imported
by every per-domain handler module under :mod:`swarm.mcp.queen_handlers`.
"""

from __future__ import annotations

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
    """Return an error payload if *worker_name* is not the Queen, else None.

    THE BARE LIST HERE IS DELIBERATE — DECIDED ON #1535, DON'T RE-OPEN IT.
    #1432 established that within a tool which ever emits ``structuredContent``,
    every exit should emit it. This return is the one exception, for two reasons:

    1. IT CANNOT BE FIXED CENTRALLY WITHOUT BREAKING THE RULE'S OWN SCOPE. This is
       shared by ~58 queen exits, of which ~51 never emit ``structuredContent`` on
       ANY path. Adding a sidecar here would give all 51 one, and those handlers are
       already self-consistent — converting them makes the codebase less predictable,
       not more, which is precisely what #1535 was told not to do. Fixing it for only
       the 7 structured tools would mean duplicating the check at each of them.
    2. IT IS A REFUSAL BEFORE THE CONTRACT APPLIES. A denied caller never reached the
       tool's result shape at all, so there is no shape for it to be inconsistent
       with. That is different from the empty-result case, where the caller DID reach
       the contract and got a different shape depending on how many rows matched.
    """
    if worker_name != QUEEN_WORKER_NAME:
        return _PERMISSION_DENIED
    return None


def _clamp(value: int | str | float | None, default: int, minimum: int, maximum: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, n))
