"""Queen MCP handlers for the buzz log + drone action views.

Extracted from ``mcp/queen_tools.py`` (task #519). Both handlers read
from the unified ``buzz_log`` SQLite table — ``view_buzz_log`` returns
any category, ``view_drone_actions`` filters to the ``drone`` category.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import QueenViewBuzzLogArgs, QueenViewDroneActionsArgs
from swarm.mcp.queen_handlers._common import _assert_queen, _clamp
from swarm.mcp.types import HandlerResult

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
    {
        "name": "queen_view_buzz_log",
        "description": (
            "Read the buzz log — the system's activity feed: drone decisions, state "
            "transitions, operator actions, MCP calls. Filter by worker or category "
            "('drone'|'worker'|'operator'|'message') or age. Most useful for answering "
            "'what just happened?' after a notification fires."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {
                    "type": "string",
                    "description": "Filter to entries for this worker.",
                },
                "category": {
                    "type": "string",
                    "description": "Filter by category tag.",
                },
                "since_seconds": {
                    "type": "integer",
                    "description": "Only entries from the last N seconds. Default 600 (10m).",
                    "default": 600,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows. Default 50, max 500.",
                    "default": 50,
                },
            },
            "examples": [
                {"since_seconds": 300},
                {"worker": "platform", "category": "drone"},
            ],
        },
    },
    {
        "name": "queen_view_drone_actions",
        "description": (
            "Show recent drone (automated decision) actions — the fast-path auto-approvals, "
            "auto-assigns, auto-revives. Filter by worker or age. Use when deciding whether "
            "to intervene: if drones are already handling something routinely, don't "
            "duplicate their work."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {"type": "string", "description": "Filter by worker name."},
                "since_seconds": {
                    "type": "integer",
                    "description": "Only last N seconds. Default 600 (10m).",
                    "default": 600,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows. Default 50, max 200.",
                    "default": 50,
                },
            },
            "examples": [
                {"since_seconds": 600},
                {"worker": "hub", "limit": 30},
            ],
        },
    },
]


def _handle_view_buzz_log(
    d: SwarmDaemon, worker_name: str, args: QueenViewBuzzLogArgs
) -> HandlerResult:
    err = _assert_queen(worker_name)
    if err:
        return err
    worker_filter = (args.get("worker") or "").strip()
    category_filter = (args.get("category") or "").strip()
    since = _clamp(args.get("since_seconds", 600), 600, 1, 30 * 86400)
    limit = _clamp(args.get("limit", 50), 50, 1, 500)

    since_ts = time.time() - since
    sql_parts = ["SELECT * FROM buzz_log WHERE timestamp >= ?"]
    params: list[Any] = [since_ts]
    if worker_filter:
        sql_parts.append("AND worker_name = ?")
        params.append(worker_filter)
    if category_filter:
        sql_parts.append("AND category = ?")
        params.append(category_filter)
    sql_parts.append("ORDER BY timestamp DESC LIMIT ?")
    params.append(limit)
    rows = d.swarm_db.fetchall(" ".join(sql_parts), tuple(params))
    if not rows:
        return [{"type": "text", "text": "No buzz entries match."}]
    lines = [
        f"[{r['category']}] {r['worker_name'] or '-'}: {r['action']} — {(r['detail'] or '')[:120]}"
        for r in rows
    ]
    payload = [
        {
            "id": r["id"],
            "timestamp": r["timestamp"],
            "category": r["category"],
            "worker_name": r["worker_name"] or None,
            "action": r["action"],
            "detail": r["detail"] or "",
        }
        for r in rows
    ]
    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "structuredContent": {
            "entries": payload,
            "count": len(payload),
            "filters": {
                "worker": worker_filter or None,
                "category": category_filter or None,
                "since_seconds": since,
                "limit": limit,
            },
        },
    }


def _handle_view_drone_actions(
    d: SwarmDaemon, worker_name: str, args: QueenViewDroneActionsArgs
) -> HandlerResult:
    err = _assert_queen(worker_name)
    if err:
        return err
    worker_filter = (args.get("worker") or "").strip()
    since = _clamp(args.get("since_seconds", 600), 600, 1, 30 * 86400)
    limit = _clamp(args.get("limit", 50), 50, 1, 200)

    since_ts = time.time() - since
    sql_parts = ["SELECT * FROM buzz_log WHERE category = 'drone' AND timestamp >= ?"]
    params: list[Any] = [since_ts]
    if worker_filter:
        sql_parts.append("AND worker_name = ?")
        params.append(worker_filter)
    sql_parts.append("ORDER BY timestamp DESC LIMIT ?")
    params.append(limit)
    rows = d.swarm_db.fetchall(" ".join(sql_parts), tuple(params))
    if not rows:
        return [{"type": "text", "text": "No recent drone actions."}]
    lines = [
        f"{r['worker_name'] or '-'}: {r['action']} — {(r['detail'] or '')[:120]}" for r in rows
    ]
    payload = [
        {
            "id": r["id"],
            "timestamp": r["timestamp"],
            "worker_name": r["worker_name"] or None,
            "action": r["action"],
            "detail": r["detail"] or "",
        }
        for r in rows
    ]
    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "structuredContent": {
            "actions": payload,
            "count": len(payload),
            "filters": {
                "worker": worker_filter or None,
                "since_seconds": since,
                "limit": limit,
            },
        },
    }


HANDLERS = {
    "queen_view_buzz_log": _handle_view_buzz_log,
    "queen_view_drone_actions": _handle_view_drone_actions,
}
