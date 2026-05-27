"""Handler for the ``swarm_report_progress`` MCP tool.

Extracted from ``mcp/tools.py`` (task #518).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import ReportProgressArgs
from swarm.mcp.types import TextContent

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_report_progress",
        "description": (
            "Report structured progress on your current task. The operator sees these in the "
            "dashboard and uses them to decide when to intervene. Call this at meaningful "
            "milestones — finished reading, starting implementation, test passing, hit a "
            "blocker — not on every trivial step. If you're blocked (waiting on another "
            "worker, missing credentials, flaky test), set blockers to the specific thing "
            "that would unblock you so the operator can act."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "phase": {
                    "type": "string",
                    "description": (
                        "Current phase label. Conventional values: 'reading', "
                        "'planning', 'implementing', 'testing', 'debugging', "
                        "'shipping'."
                    ),
                },
                "pct": {
                    "type": "number",
                    "description": (
                        "Estimated completion percentage 0-100. Be honest — "
                        "overestimates frustrate the operator."
                    ),
                },
                "blockers": {
                    "type": "string",
                    "description": "Specific blocker, if any. Empty string when making progress.",
                },
            },
            "examples": [
                {"phase": "implementing", "pct": 40},
                {
                    "phase": "debugging",
                    "pct": 60,
                    "blockers": "Waiting on platform worker to deploy schema change from PR #87.",
                },
                {"phase": "shipping", "pct": 95},
            ],
        },
    },
]


def _handle_report_progress(
    d: SwarmDaemon, worker_name: str, args: ReportProgressArgs
) -> list[TextContent]:
    phase = args.get("phase", "")
    pct = args.get("pct", -1)
    blockers = args.get("blockers", "")
    parts = []
    if phase:
        parts.append(f"phase={phase}")
    if pct >= 0:
        parts.append(f"{pct}%")
    if blockers:
        parts.append(f"blockers: {blockers}")
    summary = ", ".join(parts) if parts else "progress update"
    from swarm.drones.log import LogCategory, SystemAction

    d.drone_log.add(
        SystemAction.OPERATOR,
        worker_name,
        f"progress: {summary}",
        category=LogCategory.WORKER,
    )
    d.broadcast_ws(
        {
            "type": "worker_progress",
            "worker": worker_name,
            "phase": phase,
            "pct": pct,
            "blockers": blockers,
        }
    )
    return [{"type": "text", "text": "Progress reported."}]


HANDLERS = {"swarm_report_progress": _handle_report_progress}
