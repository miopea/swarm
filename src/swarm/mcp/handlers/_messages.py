"""Handlers for the message-oriented MCP tools (check_messages, send_message,
note_to_queen).

Extracted from ``mcp/tools.py`` (task #518). The Queen auto-relay +
Attention-thread upsert helpers used by send_message and note_to_queen
live in :mod:`swarm.mcp.handlers._queen_relay`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import CheckMessagesArgs, NoteToQueenArgs, SendMessageArgs
from swarm.mcp.handlers._queen_relay import _auto_relay_to_queen
from swarm.mcp.types import TextContent

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_check_messages",
        "description": (
            "Check the Swarm inbox for pending messages from other workers or the operator. "
            "Call this at three moments: (1) at the start of every task so you don't miss "
            "dependency warnings or operator hints, (2) after completing a task so downstream "
            "workers' replies don't stack up, and (3) whenever you encounter unexpected state "
            "(files changed under you, tests failing that passed last run) — another worker "
            "may have sent a 'warning' or 'finding' that explains it. Messages are marked read "
            "on retrieval, so don't call speculatively."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "examples": [{}],
        },
    },
    {
        "name": "swarm_send_message",
        "description": (
            "Send a direct message to ONE named worker. Use this whenever you learn "
            "something that affects another worker's ability to do their job "
            "correctly. Message types:\n"
            "  - 'finding'    — a discovery that might be useful (schema shape, gotcha, pattern)\n"
            "  - 'warning'    — you are about to change something that will break their build\n"
            "  - 'dependency' — they need to do X before you can finish Y (blocks your task)\n"
            "  - 'status'     — routine progress update, not action-required\n"
            "BROADCAST IS QUEEN-ONLY. Sending to '*' is refused for workers. If something "
            "genuinely affects everyone, send it to the Queen with swarm_note_to_queen and "
            "say you think it warrants a broadcast — she has the fan-out authority and the "
            "context to judge whether it does."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": (
                        "Recipient worker name (e.g. 'hub', 'platform'). One worker. "
                        "'*' is Queen-only and will be refused."
                    ),
                },
                "type": {
                    "type": "string",
                    "enum": ["finding", "warning", "dependency", "status"],
                    "description": "Message type — see tool description for semantics.",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "The message body. Be concrete: include file paths, function "
                        "names, and any action the recipient needs to take."
                    ),
                },
            },
            "required": ["to", "type", "content"],
            "examples": [
                {
                    "to": "platform",
                    "type": "warning",
                    "content": (
                        "Renamed ContactDto.emailAddress → ContactDto.email in hub "
                        "PR #321; please update your imports."
                    ),
                },
                {
                    "to": "hub",
                    "type": "finding",
                    "content": (
                        "The /api/v1/contacts endpoint now requires X-Tenant-Id "
                        "header as of platform commit abc123."
                    ),
                },
            ],
        },
    },
    {
        "name": "swarm_note_to_queen",
        "description": (
            "Send a lightweight side-channel note to the Queen. Use this when you have "
            "a coordination-question, a pre-response reminder, or an 'FYI' directed at "
            "the Queen that doesn't rise to a formal 'finding' / 'warning' / 'dependency' "
            "message — short things like 'should I /clear before this next run?' or "
            "'FYI queen, I'm about to branch off X'. Every note is persisted in the "
            "inter-worker message log AND auto-relayed into the Queen's PTY (same path "
            "as ``swarm_send_message(to='queen', ...)``), so her next turn sees it "
            "naturally. Workers MAY NOT use this to prompt each other — the elevated "
            "relay channel is Queen-only. Self-notes (queen → queen) are a no-op."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "The note body. Keep it short — 1-3 sentences is ideal. For "
                        "longer structured memos use ``swarm_send_message(to='queen', "
                        "type='finding'|'status')`` instead."
                    ),
                },
            },
            "required": ["content"],
            "examples": [
                {"content": "Should I /clear before the 8-task dispatch run?"},
                {"content": "FYI queen: I'm branching off to investigate #247 first."},
            ],
        },
    },
]


def _handle_check_messages(
    d: SwarmDaemon, worker_name: str, _args: CheckMessagesArgs
) -> list[TextContent]:
    messages = d.message_store.get_unread(worker_name)
    if not messages:
        return [{"type": "text", "text": "No pending messages."}]
    # Mark as read
    d.message_store.mark_read(worker_name, [m.id for m in messages])
    lines = []
    for m in messages:
        lines.append(f"[{m.msg_type}] from {m.sender}: {m.content}")
    return [{"type": "text", "text": "\n".join(lines)}]


def _known_worker_names(d: SwarmDaemon) -> set[str] | None:
    """Return the set of registered worker names (+ the Queen), or ``None``.

    Sourced from ``d.config.workers`` (the configured roster — the same
    source the ``*`` broadcast fan-out uses), not the live PTYs, so an
    offline-but-configured worker still validates. ``None`` means the roster
    can't be determined (no real config — e.g. a minimal test fixture), in
    which case the caller fails open and skips recipient validation rather
    than crash a legitimate send.
    """
    from swarm.worker.worker import QUEEN_WORKER_NAME

    workers = getattr(getattr(d, "config", None), "workers", None)
    if not isinstance(workers, (list, tuple)):
        return None
    names = {n for w in workers if isinstance((n := getattr(w, "name", None)), str) and n}
    if not names:
        return None
    names.add(QUEEN_WORKER_NAME)
    return names


def _guard_direct_send(
    d: SwarmDaemon, worker_name: str, recipient: str, content: str
) -> tuple[str, list[TextContent] | None]:
    """Validate the recipient + enforce the fan-out cap for a 1:1 send.

    Returns ``(canonical_recipient, None)`` when the send may proceed, or
    ``(recipient, error_response)`` when a guard blocks it — the caller
    returns the error verbatim. Task #873.
    """
    from swarm.drones.log import LogCategory, SystemAction
    from swarm.worker.worker import QUEEN_WORKER_NAME

    # (a) Recipient validation — reject a send to a name that is not a
    #     registered worker instead of silently enqueuing a row no one reads.
    #     The #873 incident wrote rows to nonexistent workers (aria, …).
    known = _known_worker_names(d)
    if known is not None:
        from swarm.messages.send_guard import resolve_recipient

        canonical = resolve_recipient(known, recipient)
        if canonical is None:
            d.drone_log.add(
                SystemAction.REJECTED,
                worker_name,
                f"→ {recipient}: not a registered worker (message dropped)",
                category=LogCategory.MESSAGE,
            )
            return recipient, [
                {
                    "type": "text",
                    "text": (
                        f"'{recipient}' is not a registered worker — message not sent. "
                        f"Known workers: {', '.join(sorted(known))}. "
                        "Use '*' to broadcast to all workers."
                    ),
                }
            ]
        recipient = canonical

    # (b) Fan-out cap — a worker may reach only a small number of DISTINCT
    #     recipients with an IDENTICAL body in a short window before it must
    #     use the explicit '*' broadcast path. The Queen is exempt (she holds
    #     the authority a worker lacks). ``is False`` is deliberate: a
    #     MagicMock daemon in unrelated tests yields a truthy stub here.
    fanout_guard = getattr(d, "fanout_guard", None)
    if (
        fanout_guard is not None
        and worker_name != QUEEN_WORKER_NAME
        and fanout_guard.check(worker_name, recipient, content) is False
    ):
        d.drone_log.add(
            SystemAction.REJECTED,
            worker_name,
            f"→ {recipient}: fan-out cap exceeded (identical message); use '*'",
            category=LogCategory.MESSAGE,
        )
        return recipient, [
            {
                "type": "text",
                "text": (
                    "Fan-out cap reached: you've already sent this same message to the "
                    "maximum number of distinct workers in a short window. To reach the "
                    "rest of the fleet, send it once with to='*' (a real broadcast) "
                    "instead of messaging workers one by one."
                ),
            }
        ]

    return recipient, None


def _handle_broadcast(
    d: SwarmDaemon, worker_name: str, msg_type: str, content: str
) -> list[TextContent]:
    """Fan a ``to='*'`` send out to every registered worker (minus sender).

    ``send(..., "*", ...)`` would write a single row whose ``read_at`` column
    belongs to whichever worker called ``get_unread`` first — so the
    broadcast "won" by the first reader and nobody else saw it. The roster is
    sourced from ``d.config.workers`` (configured roster), NOT ``d.workers``
    (live PTYs); messages persist in SQLite, so workers offline at send time
    still pick up the broadcast when they start and call ``get_unread``.
    """
    from swarm.drones.log import LogCategory, SystemAction
    from swarm.worker.worker import QUEEN_WORKER_NAME

    configured = getattr(getattr(d, "config", None), "workers", None) or []
    roster_names: list[str] = []
    seen: set[str] = set()
    for w in configured:
        name = getattr(w, "name", None)
        if not name or name == worker_name or name in seen:
            continue
        seen.add(name)
        roster_names.append(name)
    ids = d.message_store.broadcast(worker_name, roster_names, msg_type, content)
    d.drone_log.add(
        SystemAction.OPERATOR,
        worker_name,
        f"→ * ({len(ids)} recipient(s)): {content[:80]}",
        category=LogCategory.MESSAGE,
    )
    if not ids:
        return [{"type": "text", "text": "No other workers registered to receive broadcast."}]
    # Broadcast reached the Queen if she's in the configured roster.
    if QUEEN_WORKER_NAME in roster_names and worker_name != QUEEN_WORKER_NAME:
        # broadcast() preserves ``recipients`` order for successful sends.
        # Our pre-filtered roster already drops empties + the sender, so in
        # the happy path ``ids`` and ``roster_names`` align 1:1. Only a
        # mid-broadcast sqlite failure (send returns None) would shorten ids;
        # in that edge case skip mark-read rather than mis-target another
        # worker's row.
        queen_msg_id: int | None = None
        if len(ids) == len(roster_names):
            queen_msg_id = ids[roster_names.index(QUEEN_WORKER_NAME)]
        _auto_relay_to_queen(d, worker_name, msg_type, content, message_id=queen_msg_id)
    recipients_list = ", ".join(sorted(roster_names))
    return [
        {
            "type": "text",
            "text": f"Broadcast sent to {len(ids)} worker(s): {recipients_list}.",
        }
    ]


def _handle_send_message(
    d: SwarmDaemon, worker_name: str, args: SendMessageArgs
) -> list[TextContent]:
    recipient = args.get("to", "")
    msg_type = args.get("type", "finding")
    content = args.get("content", "")
    if not recipient or not content:
        return [{"type": "text", "text": "Missing 'to' or 'content'"}]
    from swarm.drones.log import LogCategory, SystemAction
    from swarm.worker.worker import QUEEN_WORKER_NAME

    # BROADCAST IS A QUEEN CAPABILITY, NOT A WORKER ONE (operator ruling
    # 2026-08-12: "this is a constant pain point; only the queen can broadcast").
    #
    # DISTINCT FROM THE #647 GATE BELOW, WHICH IS WHY THAT ONE WAS NOT ENOUGH.
    # #647 inspects the CONTENT and blocks directive/authority language. A worker
    # with something benign-sounding still reached every inbox, so fan-out was
    # governed by phrasing rather than by authority — and the fleet-wide cost of a
    # broadcast does not depend on how politely it is worded. This is a capability
    # check: it does not read the message at all.
    #
    # Refuses BEFORE any write and returns the resolving path rather than a bare
    # "denied" — a worker that cannot see what to do instead just rephrases and
    # retries, which is how the #647 gate ended up being routed around.
    if recipient == "*" and worker_name != QUEEN_WORKER_NAME:
        d.drone_log.add(
            SystemAction.OPERATOR,
            worker_name,
            f"broadcast refused (queen-only): {content[:80]}",
            category=LogCategory.MESSAGE,
            metadata={"msg_type": msg_type, "recipient": "*", "refused": "queen_only"},
        )
        return [
            {
                "type": "text",
                "text": (
                    "Broadcast to '*' is Queen-only — nothing was sent, and no worker "
                    "received this. Two ways forward: send it directly to the workers it "
                    "actually affects (naming them costs you a sentence and spares "
                    "everyone else), or if you believe it genuinely affects the whole "
                    "fleet, use swarm_note_to_queen to hand it to the Queen and say you "
                    "think it warrants a broadcast. She has the fan-out authority."
                ),
            }
        ]

    # Mass-broadcast gate (task #647). A worker cannot issue a swarm-wide
    # directive or claim operator authority. Authority-claim patterns gate any
    # recipient count; directive/policy language gates only when it fans out
    # (broadcast to '*'). The Queen relaying her own message is exempt — she
    # has the authority a worker lacks. Enforcement is deterministic on
    # purpose (injection-proof); the Queen runs only as async enrichment.
    if worker_name != QUEEN_WORKER_NAME:
        from swarm.mcp.handlers._queen_relay import _gate_broadcast
        from swarm.messages.broadcast_gate import classify_broadcast

        verdict = classify_broadcast(content, is_broadcast=(recipient == "*"), fanout_count=1)
        if verdict.blocked:
            text = _gate_broadcast(
                d, worker_name, recipient, msg_type, content, verdict.reason, verdict.matched
            )
            return [{"type": "text", "text": text}]

    if recipient == "*":
        return _handle_broadcast(d, worker_name, msg_type, content)

    # Direct (1:1) path. Task #873: deterministic send-path guards (recipient
    # validation + fan-out cap) close the rate-limit-amplifier hole where a
    # worker hand-enumerated the roster in one burst. Returns the canonical
    # recipient on success, or an error response the caller surfaces verbatim.
    recipient, guard_error = _guard_direct_send(d, worker_name, recipient, content)
    if guard_error is not None:
        return guard_error

    msg_id = d.message_store.send(worker_name, recipient, msg_type, content)
    if msg_id:
        d.drone_log.add(
            SystemAction.OPERATOR,
            worker_name,
            f"→ {recipient}: {content[:80]}",
            category=LogCategory.MESSAGE,
        )
        # Task #235 Phase 1: when a worker sends to the Queen, inject a
        # short relay notification into the Queen's PTY so her next turn
        # processes the reply naturally — same ergonomic as #225's task
        # auto-dispatch. Skipped when the Queen messages herself
        # (self-loop guard) and when a worker messages another worker
        # (workers deliberately can't auto-interrupt each other — that
        # bypass is Queen-only).
        if recipient == QUEEN_WORKER_NAME and worker_name != QUEEN_WORKER_NAME:
            _auto_relay_to_queen(d, worker_name, msg_type, content, message_id=msg_id)
        return [{"type": "text", "text": f"Message sent to {recipient}."}]
    return [{"type": "text", "text": "Failed to send message."}]


def _handle_note_to_queen(
    d: SwarmDaemon, worker_name: str, args: NoteToQueenArgs
) -> list[TextContent]:
    """Persist a side-channel note addressed to the Queen + auto-relay it.

    Task #248: workers often address the Queen via PTY text (pre-
    response reminders, inline coordination questions) that never goes
    through ``swarm_send_message``. This tool is a lightweight shortcut
    — the note is persisted with ``msg_type="note"`` (so it shows up
    alongside formal messages in ``queen_view_messages``) AND fires
    the same ``_auto_relay_to_queen`` path as #235 so the Queen's PTY
    sees it the same turn.
    """
    from swarm.drones.log import LogCategory, SystemAction
    from swarm.worker.worker import QUEEN_WORKER_NAME

    content = args.get("content", "")
    if not content:
        return [{"type": "text", "text": "Missing 'content'"}]

    if worker_name == QUEEN_WORKER_NAME:
        # Self-relay would pump the Queen's own PTY on every
        # note-to-self and potentially loop. No real use case.
        return [
            {
                "type": "text",
                "text": "No-op: queen cannot note-to-queen (self-loop guard).",
            }
        ]

    msg_id = d.message_store.send(worker_name, QUEEN_WORKER_NAME, "note", content)
    if not msg_id:
        return [{"type": "text", "text": "Failed to persist note."}]

    d.drone_log.add(
        SystemAction.OPERATOR,
        worker_name,
        f"→ queen (note): {content[:80]}",
        category=LogCategory.MESSAGE,
    )
    _auto_relay_to_queen(d, worker_name, "note", content, message_id=msg_id)
    return [{"type": "text", "text": "Note queued for the Queen."}]


HANDLERS = {
    "swarm_check_messages": _handle_check_messages,
    "swarm_send_message": _handle_send_message,
    "swarm_note_to_queen": _handle_note_to_queen,
}
