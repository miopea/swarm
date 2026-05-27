"""Queen MCP handlers for the message-log views.

Extracted from ``mcp/queen_tools.py`` (task #519). ``view_messages``
returns the raw message log; ``view_message_stream`` joins each row
against the recipient's current state so the Queen can triage which
unread messages need a nudge.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import QueenViewMessagesArgs, QueenViewMessageStreamArgs
from swarm.mcp.queen_handlers._common import _assert_queen, _clamp
from swarm.mcp.queen_handlers._message_stream_helpers import (
    _message_stream_worker_states,
    _render_message_stream_rows,
    _structured_message_stream_rows,
)

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
    {
        "name": "queen_view_messages",
        "description": (
            "Read the inter-worker message log — findings, warnings, dependencies workers "
            "have sent each other. Filter by worker (either side) or by age. Call this when "
            "tracing 'why is worker X confused?' — often they got a warning they didn't heed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {
                    "type": "string",
                    "description": (
                        "Filter to messages involving this worker (sender OR recipient)."
                    ),
                },
                "since_seconds": {
                    "type": "integer",
                    "description": "Only messages from the last N seconds. Default 3600 (1h).",
                    "default": 3600,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return. Default 50, max 500.",
                    "default": 50,
                },
                "full": {
                    "type": "boolean",
                    "description": (
                        "When true, return each message's COMPLETE body instead of "
                        "the 160-char preview. Use this when you need to relay a "
                        "worker's message verbatim (e.g. a decision memo to the "
                        "operator). Default false keeps the list-view ergonomic "
                        "for scanning; narrow with ``worker`` + ``limit`` before "
                        "flipping to ``full=true`` so you don't page through "
                        "many KB of unrelated chat."
                    ),
                    "default": False,
                },
            },
            "examples": [
                {"worker": "hub", "since_seconds": 1800},
                {"since_seconds": 3600, "limit": 30},
                {"worker": "project-root", "limit": 1, "full": True},
            ],
        },
    },
    {
        "name": "queen_view_message_stream",
        "description": (
            "Inter-worker message feed with recipient-state joined. Call this when "
            "you want to see who has unread messages sitting in their inbox while "
            "they're idle — those are the workers most likely to need a nudge. "
            "Surfaces every message (sender → recipient, type, preview, age) in a "
            "recent window and tags each one with whether the recipient is idle "
            "AND hasn't read it yet. Use ``actionable_only=true`` to filter down "
            "to the subset you should act on — ones where the recipient is "
            "RESTING/SLEEPING/STUNG and the message is still unread. Companion "
            "to ``queen_view_messages`` — that one is a raw log; this one is a "
            "triage feed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since_seconds": {
                    "type": "integer",
                    "description": "Only messages from the last N seconds. Default 900 (15m).",
                    "default": 900,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return. Default 50, max 500.",
                    "default": 50,
                },
                "actionable_only": {
                    "type": "boolean",
                    "description": (
                        "When true, filter to unread messages whose recipient is "
                        "currently RESTING / SLEEPING / STUNG — the subset where a "
                        "Queen nudge is most likely to unblock work. Default false."
                    ),
                    "default": False,
                },
                "full": {
                    "type": "boolean",
                    "description": (
                        "When true, return each message's complete body instead of "
                        "the 160-char preview. Same semantics as the ``full`` flag "
                        "on ``queen_view_messages``. Default false."
                    ),
                    "default": False,
                },
            },
            "examples": [
                {"since_seconds": 900, "actionable_only": True},
                {"since_seconds": 3600, "limit": 30},
                {"since_seconds": 900, "actionable_only": True, "full": True},
            ],
        },
    },
]


def _handle_view_messages(
    d: SwarmDaemon, worker_name: str, args: QueenViewMessagesArgs
) -> list[dict[str, Any]] | dict[str, Any]:
    err = _assert_queen(worker_name)
    if err:
        return err
    worker_filter = (args.get("worker") or "").strip()
    since = _clamp(args.get("since_seconds", 3600), 3600, 1, 30 * 86400)
    limit = _clamp(args.get("limit", 50), 50, 1, 500)
    # Task #237: ``full=true`` returns the complete message body
    # instead of the 160-char preview. The auto-relay path from #235
    # tells the Queen to call this tool to read the full message she
    # was just notified about — but the default 160-char truncation
    # left her unable to relay verbatim content. Default stays
    # truncated so list-view ergonomics don't change.
    full = bool(args.get("full", False))

    since_ts = time.time() - since
    sql_parts = ["SELECT * FROM messages WHERE created_at >= ?"]
    params: list[Any] = [since_ts]
    if worker_filter:
        sql_parts.append("AND (sender = ? OR recipient = ?)")
        params.extend([worker_filter, worker_filter])
    sql_parts.append("ORDER BY created_at DESC LIMIT ?")
    params.append(limit)
    rows = d.swarm_db.fetchall(" ".join(sql_parts), tuple(params))
    if not rows:
        return [{"type": "text", "text": "No messages match."}]
    lines: list[str] = []
    payload: list[dict[str, Any]] = []
    for r in rows:
        content = r["content"] or ""
        body = content if full else content[:160]
        header = f"[{r['msg_type']}] {r['sender']} → {r['recipient']}"
        if full:
            # Multi-message / multi-line bodies: separate with a blank
            # line so the Queen can identify message boundaries when
            # relaying verbatim.
            lines.append(f"{header}:\n{body}")
        else:
            lines.append(f"{header}: {body}")
        payload.append(
            {
                "id": r["id"],
                "msg_type": r["msg_type"],
                "sender": r["sender"],
                "recipient": r["recipient"],
                # Always carry the FULL body in the structured payload —
                # the truncation is purely a text-rendering concern, the
                # JSON sidecar is for the model to query precisely.
                "content": content,
                "created_at": r["created_at"],
                "read_at": r["read_at"],
            }
        )
    separator = "\n\n---\n\n" if full else "\n"
    return {
        "content": [{"type": "text", "text": separator.join(lines)}],
        "structuredContent": {
            "messages": payload,
            "count": len(payload),
            "filters": {
                "worker": worker_filter or None,
                "since_seconds": since,
                "limit": limit,
                "full_body": full,
            },
        },
    }


def _handle_view_message_stream(
    d: SwarmDaemon, worker_name: str, args: QueenViewMessageStreamArgs
) -> list[dict[str, Any]] | dict[str, Any]:
    """Return recent messages joined against the recipient's current state.

    ``actionable_only=true`` narrows to the subset the Queen is most
    likely to need to act on: unread messages whose recipient is
    currently idle (RESTING / SLEEPING / STUNG). That's the shape the
    InterWorkerMessageWatcher drone uses when deciding who to nudge.
    """
    err = _assert_queen(worker_name)
    if err:
        return err
    since = _clamp(args.get("since_seconds", 900), 900, 1, 30 * 86400)
    limit = _clamp(args.get("limit", 50), 50, 1, 500)
    actionable_only = bool(args.get("actionable_only", False))
    # Task #237: mirror ``queen_view_messages``' full-body flag so the
    # stream view can also return complete message content when the
    # Queen needs to relay verbatim.
    full = bool(args.get("full", False))

    since_ts = time.time() - since
    rows = d.swarm_db.fetchall(
        "SELECT * FROM messages WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
        (since_ts, limit * 4 if actionable_only else limit),
    )
    if not rows:
        return [{"type": "text", "text": "No messages in window."}]

    worker_state = _message_stream_worker_states(d)
    lines = _render_message_stream_rows(
        rows,
        worker_state=worker_state,
        actionable_only=actionable_only,
        limit=limit,
        full=full,
    )
    if not lines:
        if actionable_only:
            return [{"type": "text", "text": "No actionable messages."}]
        return [{"type": "text", "text": "No messages in window."}]
    structured_rows = _structured_message_stream_rows(
        rows,
        worker_state=worker_state,
        actionable_only=actionable_only,
        limit=limit,
    )
    separator = "\n\n---\n\n" if full else "\n"
    return {
        "content": [{"type": "text", "text": separator.join(lines)}],
        "structuredContent": {
            "messages": structured_rows,
            "count": len(structured_rows),
            "filters": {
                "since_seconds": since,
                "limit": limit,
                "actionable_only": actionable_only,
                "full_body": full,
            },
        },
    }


HANDLERS = {
    "queen_view_messages": _handle_view_messages,
    "queen_view_message_stream": _handle_view_message_stream,
}
