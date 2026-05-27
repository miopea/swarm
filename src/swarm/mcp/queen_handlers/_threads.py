"""Queen MCP handlers for the operator chat threads (post / reply / update).

Extracted from ``mcp/queen_tools.py`` (task #519). The thread helpers
(``_ensure_operator_thread``, ``_broadcast_thread_event``,
``_broadcast_message_event``, ``_resolve_thread_alias``) live in
``_thread_helpers.py`` — split out to keep this module under the
per-module LOC budget. The save_learning handler in ``_learnings.py``
also imports ``_resolve_thread_alias`` from there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import QueenPostThreadArgs, QueenReplyArgs, QueenUpdateThreadArgs
from swarm.mcp.queen_handlers._common import _assert_queen
from swarm.mcp.queen_handlers._thread_helpers import (
    _broadcast_message_event,
    _broadcast_thread_event,
    _resolve_thread_alias,
)

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
    {
        "name": "queen_post_thread",
        "description": (
            "Start a new chat thread with the operator. Use this when you proactively "
            "surface something (a stuck worker, a proposal ready to review, an anomaly "
            "you've spotted). Prefer this over dropping proactive info into the main "
            "operator thread — threads let the operator triage without losing focus. "
            "Return value includes the thread_id for follow-up via queen_reply."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": (
                        "Short title shown in the thread list. Aim for <60 chars, "
                        "action-oriented ('Hub stuck on tests'  not 'issue')."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": (
                        "Initial message body. Keep terse; include the specific "
                        "worker/task/file when relevant."
                    ),
                },
                "kind": {
                    "type": "string",
                    "description": (
                        "Category for filtering: 'oversight' (stuck/drift), 'proposal', "
                        "'escalation', 'anomaly', or 'operator' for operator-triggered "
                        "topics. Default 'oversight' when Queen initiates."
                    ),
                    "default": "oversight",
                },
                "worker": {
                    "type": "string",
                    "description": "Subject worker name, if this thread is about a specific one.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Subject task id, if applicable.",
                },
                "widgets": {
                    "type": "array",
                    "description": (
                        "Inline widget descriptors. Supported types: "
                        "'approve_buttons' (Approve/Dismiss/Discuss), "
                        "'worker_card' (live worker status), "
                        "'task_list' (live task references). "
                        "UI renders these; pass an empty array if plain text is enough."
                    ),
                },
            },
            "required": ["title", "body"],
            "examples": [
                {
                    "title": "Hub stuck on tests",
                    "body": (
                        "Hub has been BUZZING on task #42 for 18 minutes without token "
                        "growth. Plan: interrupt and ask for a status report?"
                    ),
                    "kind": "oversight",
                    "worker": "hub",
                    "widgets": [{"type": "approve_buttons"}],
                },
            ],
        },
    },
    {
        "name": "queen_reply",
        "description": (
            "Post a reply in an existing thread. Use this to respond to the operator "
            "or to update a thread with new information. The default operator thread "
            "is available as thread_id='operator' — writes there when no specific "
            "thread is in play."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {
                    "type": "string",
                    "description": (
                        "Target thread. Pass 'operator' for the default operator "
                        "thread (created lazily on first use)."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": "Message content.",
                },
                "widgets": {
                    "type": "array",
                    "description": (
                        "Optional inline widgets to render with this message. "
                        "Same shape as queen_post_thread.widgets."
                    ),
                },
            },
            "required": ["thread_id", "body"],
            "examples": [
                {"thread_id": "operator", "body": "Everyone's idle. Queue is clear."},
                {
                    "thread_id": "abc123def456",
                    "body": "Confirmed — hub's stuck on the same test. Suggest interrupt.",
                },
            ],
        },
    },
    {
        "name": "queen_update_thread",
        "description": (
            "Resolve a thread or change its status when an outcome is reached. Call "
            "this when the discussion's conclusion is final so the thread collapses "
            "in the UI and the operator sees it as done. Resolved threads have their "
            "composer disabled; start a new thread to reopen the topic."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {
                    "type": "string",
                    "description": "Target thread id.",
                },
                "status": {
                    "type": "string",
                    "description": (
                        "New status. Currently only 'resolved' is supported "
                        "(the Queen can self-resolve a thread she created)."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Short resolution reason shown in the collapsed summary "
                        "('operator approved', 'worker recovered')."
                    ),
                },
            },
            "required": ["thread_id", "status"],
            "examples": [
                {"thread_id": "abc123", "status": "resolved", "reason": "operator approved"},
            ],
        },
    },
]


def _handle_post_thread(
    d: SwarmDaemon, worker_name: str, args: QueenPostThreadArgs
) -> list[dict[str, Any]]:
    err = _assert_queen(worker_name)
    if err:
        return err
    title = (args.get("title") or "").strip()
    body = (args.get("body") or "").strip()
    if not title or not body:
        return [{"type": "text", "text": "Missing required 'title' or 'body'."}]
    kind = (args.get("kind") or "oversight").strip().lower()
    worker = (args.get("worker") or "").strip() or None
    task_id = (args.get("task_id") or "").strip() or None
    widgets = args.get("widgets") or []

    store = d.queen_chat
    try:
        thread = store.create_thread(
            title=title,
            kind=kind,
            worker_name=worker,
            task_id=task_id,
        )
    except ValueError as e:
        return [{"type": "text", "text": f"Invalid thread kind: {e}"}]

    message = store.add_message(
        thread.id,
        role="queen",
        content=body,
        widgets=widgets if isinstance(widgets, list) else [],
    )
    _broadcast_thread_event(d, thread.id, "created")
    _broadcast_message_event(d, thread.id, message.to_dict())
    return [
        {
            "type": "text",
            "text": f"Thread posted: id={thread.id} title={title!r}",
        }
    ]


def _handle_reply(d: SwarmDaemon, worker_name: str, args: QueenReplyArgs) -> list[dict[str, Any]]:
    err = _assert_queen(worker_name)
    if err:
        return err
    thread_id = _resolve_thread_alias(d, args.get("thread_id", ""))
    if thread_id is None:
        return [{"type": "text", "text": "Unknown thread_id."}]
    body = (args.get("body") or "").strip()
    if not body:
        return [{"type": "text", "text": "Missing 'body'."}]
    widgets = args.get("widgets") or []

    thread = d.queen_chat.get_thread(thread_id)
    if thread is None:
        return [{"type": "text", "text": "Thread not found."}]
    if thread.status == "resolved":
        return [
            {
                "type": "text",
                "text": "Thread is resolved. Start a new thread to continue the topic.",
            }
        ]
    message = d.queen_chat.add_message(
        thread_id,
        role="queen",
        content=body,
        widgets=widgets if isinstance(widgets, list) else [],
    )
    _broadcast_message_event(d, thread_id, message.to_dict())
    _broadcast_thread_event(d, thread_id, "updated")
    return [{"type": "text", "text": f"Reply posted to {thread_id}."}]


def _handle_update_thread(
    d: SwarmDaemon, worker_name: str, args: QueenUpdateThreadArgs
) -> list[dict[str, Any]]:
    err = _assert_queen(worker_name)
    if err:
        return err
    thread_id = _resolve_thread_alias(d, args.get("thread_id", ""))
    if thread_id is None:
        return [{"type": "text", "text": "Unknown thread_id."}]
    status = (args.get("status") or "").strip().lower()
    if status != "resolved":
        return [{"type": "text", "text": "Only status='resolved' is supported."}]
    reason = (args.get("reason") or "").strip()
    ok = d.queen_chat.resolve_thread(thread_id, resolved_by="queen", reason=reason)
    if not ok:
        return [
            {
                "type": "text",
                "text": "Thread was already resolved or does not exist.",
            }
        ]
    _broadcast_thread_event(d, thread_id, "resolved")
    return [{"type": "text", "text": f"Thread {thread_id} resolved."}]


HANDLERS = {
    "queen_post_thread": _handle_post_thread,
    "queen_reply": _handle_reply,
    "queen_update_thread": _handle_update_thread,
}
