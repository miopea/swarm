"""Handler for the ``swarm_get_playbooks`` MCP tool.

Extracted from ``mcp/tools.py`` (task #518).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import GetPlaybooksArgs
from swarm.mcp.types import TextContent

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_get_playbooks",
        "description": (
            "Recall reusable PLAYBOOKS — generalizable procedures synthesized "
            "from previously-successful tasks (distinct from learnings, which "
            "are operator corrections). Call this at the start of a task that "
            "resembles work the swarm has done before: a matching playbook "
            "gives you vetted steps + known pitfalls so you don't re-derive "
            "the approach. Pass a specific query (the task's goal, an error, a "
            "subsystem). Only active playbooks are returned. If one applies, "
            "follow it and note it in your resolution."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What you're about to do (goal / error / subsystem). "
                        "Omit to list recent active playbooks."
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Optional exact scope filter: 'global', "
                        "'project:<repo>', or 'worker:<name>'."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max playbooks to return (default 5, max 20).",
                },
            },
            "examples": [
                {"query": "flaky pytest under load"},
                {"query": "add retry to an outbound sender", "scope": "global"},
                {},
            ],
        },
    },
]


def _handle_get_playbooks(
    d: SwarmDaemon, worker_name: str, args: GetPlaybooksArgs
) -> list[TextContent]:
    from swarm.playbooks.models import PlaybookStatus

    store = getattr(d, "playbook_store", None)
    if store is None:
        return [{"type": "text", "text": "No playbook store."}]
    query = str(args.get("query", "")).strip()
    scope = str(args.get("scope", "")).strip() or None
    try:
        limit = min(int(args.get("limit", 5)), 20)
    except (TypeError, ValueError):
        limit = 5
    if query:
        hits = store.search(query, scope=scope, status=PlaybookStatus.ACTIVE, limit=limit)
    else:
        hits = store.list(scope=scope, status=PlaybookStatus.ACTIVE, limit=limit)
    if not hits:
        return [{"type": "text", "text": "No matching playbooks."}]
    blocks = []
    for pb in hits:
        blocks.append(
            f"## {pb.title or pb.name}  [{pb.scope}]\n"
            f"Trigger: {pb.trigger}\n"
            f"(uses={pb.uses} winrate={pb.winrate:.0%} conf={pb.confidence:.2f})\n\n"
            f"{pb.body}"
        )
    return [{"type": "text", "text": "\n\n---\n\n".join(blocks)}]


HANDLERS = {"swarm_get_playbooks": _handle_get_playbooks}
