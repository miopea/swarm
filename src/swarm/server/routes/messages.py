"""Message routes — inter-worker messaging API."""

from __future__ import annotations

from typing import Any

from aiohttp import web

from swarm.drones.log import LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.server.helpers import get_daemon, handle_errors, json_error

_log = get_logger("server.messages")


def register(app: web.Application) -> None:
    app.router.add_post("/api/messages/send", handle_send_message)
    app.router.add_post("/api/messages/delete", handle_delete_messages)
    app.router.add_get("/api/messages/{worker}", handle_get_messages)
    app.router.add_post("/api/messages/{worker}/read", handle_mark_read)
    app.router.add_get("/api/messages", handle_recent_messages)


@handle_errors
async def handle_send_message(request: web.Request) -> web.Response:
    """Send a message from one worker (or operator) to another."""
    d = get_daemon(request)
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return json_error("invalid JSON body", status=400)

    sender = body.get("from", body.get("sender", "operator"))
    recipient = body.get("to", body.get("recipient", ""))
    msg_type = body.get("type", body.get("msg_type", "finding"))
    content = body.get("content", "")

    if not recipient:
        return json_error("missing recipient", status=400)
    if not content:
        return json_error("missing content", status=400)

    # Wildcard = fan-out to every worker (minus sender) so each row has
    # its own read_at column.  The legacy single-row wildcard was first-
    # reader-wins — most workers never saw the broadcast.
    if recipient == "*":
        roster = [w.name for w in getattr(d, "workers", []) if w.name != sender]
        ids = d.message_store.broadcast(sender, roster, msg_type, content)
        d.drone_log.add(
            SystemAction.OPERATOR,
            sender,
            f"→ * ({len(ids)} recipient(s)): {content[:80]}",
            category=LogCategory.MESSAGE,
            metadata={"msg_type": msg_type, "recipient": "*", "fanout": len(ids)},
        )
        d.broadcast_ws(
            {
                "type": "message",
                "from": sender,
                "to": "*",
                "msg_type": msg_type,
                "content": content[:200],
                "fanout": len(ids),
            }
        )
        return web.json_response(
            {"ids": ids, "delivered": True, "fanout": len(ids), "recipients": roster},
            status=201,
        )

    msg_id = d.message_store.send(sender, recipient, msg_type, content)
    if msg_id is None:
        return json_error("failed to send message", status=500)

    # Log to buzz log
    d.drone_log.add(
        SystemAction.OPERATOR,
        sender,
        f"→ {recipient}: {content[:80]}",
        category=LogCategory.MESSAGE,
        metadata={"msg_type": msg_type, "recipient": recipient},
    )
    # Broadcast to dashboard
    d.broadcast_ws(
        {
            "type": "message",
            "from": sender,
            "to": recipient,
            "msg_type": msg_type,
            "content": content[:200],
        }
    )

    return web.json_response({"id": msg_id, "delivered": True}, status=201)


@handle_errors
async def handle_get_messages(request: web.Request) -> web.Response:
    """Get unread messages for a worker."""
    d = get_daemon(request)
    worker = request.match_info["worker"]
    messages = d.message_store.get_unread(worker)
    return web.json_response(
        {
            "messages": [m.to_dict() for m in messages],
        }
    )


@handle_errors
async def handle_mark_read(request: web.Request) -> web.Response:
    """Mark messages as read for a worker."""
    d = get_daemon(request)
    worker = request.match_info["worker"]
    try:
        body = await request.json()
        ids = body.get("ids")
    except Exception:
        ids = None
    count = d.message_store.mark_read(worker, ids)
    return web.json_response({"marked": count})


@handle_errors
async def handle_recent_messages(request: web.Request) -> web.Response:
    """Get recent messages (all, for dashboard)."""
    d = get_daemon(request)
    limit = min(int(request.query.get("limit", "50")), 200)
    messages = d.message_store.get_recent(limit)
    return web.json_response(
        {
            "messages": [m.to_dict() for m in messages],
        }
    )


@handle_errors
async def handle_delete_messages(request: web.Request) -> web.Response:
    """Delete messages by id — operator cleanup from the dashboard."""
    d = get_daemon(request)
    try:
        body = await request.json()
        ids = [int(i) for i in body.get("ids", [])]
    except (TypeError, ValueError):
        return json_error("ids must be a list of integers", status=400)
    except Exception:
        return json_error("invalid JSON body", status=400)
    if not ids:
        return json_error("missing ids", status=400)
    deleted = d.message_store.delete(ids)
    return web.json_response({"deleted": deleted})
