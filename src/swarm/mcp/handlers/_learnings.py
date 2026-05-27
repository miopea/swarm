"""Handler for the ``swarm_get_learnings`` MCP tool.

Extracted from ``mcp/tools.py`` (task #518).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import GetLearningsArgs
from swarm.mcp.types import TextContent

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_get_learnings",
        "description": (
            "Search learnings captured from previously-completed tasks. Call this when you "
            "start a task that sounds similar to something already done, or when you hit "
            "an unfamiliar error — another worker may have documented the fix. Results are "
            "capped at 5, so pass a specific query (function name, error message, file path) "
            "rather than a broad topic. If you find relevant learnings, cite them in your "
            "own resolution so the knowledge compounds."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Substring to filter learnings (case-insensitive). Omit "
                        "to return all (capped at 5)."
                    ),
                },
            },
            "examples": [
                {"query": "tenant resolution"},
                {"query": "MailParser"},
                {},
            ],
        },
    },
]


def _handle_get_learnings(
    d: SwarmDaemon, worker_name: str, args: GetLearningsArgs
) -> list[TextContent]:
    if not d.task_board:
        return [{"type": "text", "text": "No task board."}]
    query = args.get("query", "").lower()
    results = []
    for t in d.task_board.all_tasks:
        if not t.learnings:
            continue
        if query and query not in t.title.lower() and query not in t.learnings.lower():
            continue
        results.append(f"Task #{t.number} ({t.title}):\n{t.learnings}")
    if not results:
        return [{"type": "text", "text": "No learnings found."}]
    return [{"type": "text", "text": "\n---\n".join(results[:5])}]


HANDLERS = {"swarm_get_learnings": _handle_get_learnings}
