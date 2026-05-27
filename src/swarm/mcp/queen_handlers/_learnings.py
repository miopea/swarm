"""Queen MCP handlers for the Queen-learnings store (query / save).

Extracted from ``mcp/queen_tools.py`` (task #519). ``save_learning``
reaches into ``_threads`` for the thread-alias resolver because the
Queen often calls save with thread_id='operator' (the alias path).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import QueenQueryLearningsArgs, QueenSaveLearningArgs
from swarm.mcp.queen_handlers._common import _assert_queen, _clamp
from swarm.mcp.queen_handlers._thread_helpers import _resolve_thread_alias
from swarm.mcp.types import TextContent

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
    {
        "name": "queen_save_learning",
        "description": (
            "Persist a correction after the operator tells you you got something "
            "wrong. Future judgement calls will consult this via queen_query_learnings. "
            "Call this immediately after an operator correction, not speculatively."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "context": {
                    "type": "string",
                    "description": "What you decided / assumed (1-2 sentences).",
                },
                "correction": {
                    "type": "string",
                    "description": "What the operator said was actually right.",
                },
                "applied_to": {
                    "type": "string",
                    "description": (
                        "Decision category tag for later filtering "
                        "('oversight'|'proposal'|'assignment'|'escalation'|…)."
                    ),
                },
                "thread_id": {
                    "type": "string",
                    "description": "Originating thread id, if known.",
                },
            },
            "required": ["context", "correction"],
            "examples": [
                {
                    "context": "Flagged hub as stuck — assumed test flakiness.",
                    "correction": "Hub was actually mid-rebase; not stuck.",
                    "applied_to": "oversight",
                },
            ],
        },
    },
    {
        "name": "queen_query_learnings",
        "description": (
            "Query the Queen-learnings store — corrections the operator has given on past "
            "decisions. Call this BEFORE making a similar judgment call ('should I auto-"
            "approve this plan?', 'is this worker normally stuck?') so you don't re-make a "
            "mistake the operator already corrected."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "applied_to": {
                    "type": "string",
                    "description": (
                        "Filter by decision type tag (e.g. 'oversight', 'proposal'). "
                        "Empty returns all."
                    ),
                },
                "search": {
                    "type": "string",
                    "description": "Substring match against context or correction text.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows. Default 20, max 100.",
                    "default": 20,
                },
            },
            "examples": [
                {"applied_to": "oversight"},
                {"search": "auth"},
            ],
        },
    },
]


def _handle_query_learnings(
    d: SwarmDaemon, worker_name: str, args: QueenQueryLearningsArgs
) -> list[TextContent]:
    err = _assert_queen(worker_name)
    if err:
        return err
    applied_to = (args.get("applied_to") or "").strip() or None
    search = (args.get("search") or "").strip() or None
    limit = _clamp(args.get("limit", 20), 20, 1, 100)

    store = getattr(d, "queen_chat", None)
    if store is None:
        return [{"type": "text", "text": "Queen chat store is unavailable."}]
    learnings = store.query_learnings(applied_to=applied_to, search=search, limit=limit)
    if not learnings:
        return [{"type": "text", "text": "No learnings match."}]
    lines = [
        f"[{lg.applied_to or 'general'}] {lg.context[:80]} → {lg.correction[:120]}"
        for lg in learnings
    ]
    return [{"type": "text", "text": "\n".join(lines)}]


def _handle_save_learning(
    d: SwarmDaemon, worker_name: str, args: QueenSaveLearningArgs
) -> list[TextContent]:
    err = _assert_queen(worker_name)
    if err:
        return err
    context = (args.get("context") or "").strip()
    correction = (args.get("correction") or "").strip()
    if not context or not correction:
        return [{"type": "text", "text": "Missing required 'context' or 'correction'."}]
    applied_to = (args.get("applied_to") or "").strip()
    thread_id = (args.get("thread_id") or "").strip() or None
    if thread_id:
        # Resolve alias — operator thread id may be passed as 'operator'
        resolved = _resolve_thread_alias(d, thread_id)
        if resolved is None:
            thread_id = None
        else:
            thread_id = resolved
    learning = d.queen_chat.add_learning(
        context=context,
        correction=correction,
        applied_to=applied_to,
        thread_id=thread_id,
    )
    return [{"type": "text", "text": f"Learning saved (id={learning.id})."}]


HANDLERS = {
    "queen_query_learnings": _handle_query_learnings,
    "queen_save_learning": _handle_save_learning,
}
