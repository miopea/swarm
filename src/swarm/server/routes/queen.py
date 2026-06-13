"""Queen routes — coordination, queue status, chat-panel health, and threads."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from aiohttp import web

from swarm.logging import get_logger
from swarm.server.helpers import get_daemon, handle_errors, json_error
from swarm.worker.worker import WorkerState

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("server.routes.queen")


def register(app: web.Application) -> None:
    # ``/api/queen/coordinate`` removed (task #253 spec B) — the periodic
    # full-hive Queen coordination cycle was deleted.
    app.router.add_get("/api/queen/queue", handle_queen_queue)
    app.router.add_get("/api/queen/health", handle_queen_health)
    # Chat panel — threads + messages
    app.router.add_get("/api/queen/threads", handle_list_threads)
    app.router.add_post("/api/queen/threads", handle_create_thread)
    app.router.add_get("/api/queen/threads/{thread_id}", handle_get_thread)
    app.router.add_post("/api/queen/threads/{thread_id}/messages", handle_post_message)
    app.router.add_post("/api/queen/threads/{thread_id}/resolve", handle_resolve_thread)
    # Learnings — operator-visible list + cleanup of stale corrections
    app.router.add_get("/api/queen/learnings", handle_list_learnings)
    app.router.add_delete("/api/queen/learnings/{learning_id}", handle_delete_learning)


@handle_errors
async def handle_queen_queue(request: web.Request) -> web.Response:
    d = get_daemon(request)
    return web.json_response(d.queen_queue.status())


@handle_errors
async def handle_queen_health(request: web.Request) -> web.Response:
    """Snapshot of the interactive Queen's runtime health.

    The chat-panel health strip consumes this at initial render and then
    updates live via the WebSocket ``queen.health`` event.  Offline is
    explicit: when the Queen PTY isn't in the worker list we return
    ``state="offline"`` rather than 404 — the UI always has a payload
    to render.
    """
    d = get_daemon(request)
    return web.json_response(build_queen_health(d))


def build_queen_health(daemon: object) -> dict[str, object]:
    """Compute the Queen health snapshot.

    Extracted so the daemon can broadcast the same shape over the
    WebSocket without duplicating the mapping logic.
    """
    from swarm.queen.runtime import find_queen
    from swarm.queen.session import load_session

    workers = getattr(daemon, "workers", [])
    queen = find_queen(workers)

    config = getattr(daemon, "config", None)
    session_name = getattr(config, "session_name", "swarm")
    session_id = load_session(session_name)

    if queen is None or queen.process is None or not queen.process.is_alive:
        return {
            "state": "offline",
            "session_id": session_id,
            "pid_alive": False,
            "context_fill_pct": 0.0,
            "last_activity_ts": 0.0,
            "usage_5hr_pct": 0.0,
        }

    # Map worker state → health state.  STUNG = offline (surfaces the banner);
    # BUZZING = thinking; anything else = alive.
    state_map = {
        WorkerState.BUZZING: "thinking",
        WorkerState.WAITING: "degraded",
        WorkerState.RESTING: "alive",
        WorkerState.SLEEPING: "alive",
        WorkerState.STUNG: "offline",
    }
    health_state = state_map.get(queen.state, "alive")
    usage = queen.usage

    # Context fill percentage — worker dataclass tracks the best proxy
    # (last turn's input tokens vs model window) in `context_pct`.
    return {
        "state": health_state,
        "session_id": session_id,
        "pid_alive": True,
        "pid": getattr(queen.process, "pid", None),
        "context_fill_pct": round(queen.context_pct, 3),
        "last_activity_ts": round(queen.state_since, 2),
        "uptime_seconds": round(time.time() - queen.state_since, 1),
        "revive_count": queen.revive_count,
        "usage": usage.to_dict(),
        # Placeholder until rate-limit detection ships in the second pass.
        "usage_5hr_pct": 0.0,
    }


# ---------------------------------------------------------------------------
# Chat threads — list, create, fetch, post, resolve
#
# Operator-posted messages land in queen_messages with role="operator" AND
# are forwarded into the Queen's PTY so her conversation stream sees them
# and she can respond.  The Queen's reply comes back through her MCP tool
# calls (``queen_reply`` / ``queen_post_thread``) which broadcast their
# own WS events.
# ---------------------------------------------------------------------------


_MAX_TITLE_LEN = 200
_MAX_BODY_LEN = 8000


async def _parse_json(request: web.Request) -> dict[str, object] | web.Response:
    try:
        data = await request.json()
    except Exception:
        return json_error("invalid JSON body", 400)
    if not isinstance(data, dict):
        return json_error("body must be a JSON object", 400)
    return data


@handle_errors
async def handle_list_threads(request: web.Request) -> web.Response:
    d = get_daemon(request)
    status = (request.query.get("status") or "").strip() or None
    kind = (request.query.get("kind") or "").strip() or None
    worker = (request.query.get("worker") or "").strip() or None
    try:
        limit = min(int(request.query.get("limit", "100")), 500)
    except ValueError:
        limit = 100
    threads = d.queen_chat.list_threads(status=status, kind=kind, worker_name=worker, limit=limit)
    return web.json_response({"threads": [t.to_dict() for t in threads]})


@handle_errors
async def handle_create_thread(request: web.Request) -> web.Response:
    """Operator starts a new chat thread (e.g. from the chat panel composer).

    The operator's opening message is appended in the same call so the
    thread never exists empty — worker experience sees a complete unit.
    """
    d = get_daemon(request)
    data = await _parse_json(request)
    if isinstance(data, web.Response):
        return data
    title = str(data.get("title") or "").strip()
    body = str(data.get("body") or "").strip()
    if not title or not body:
        return json_error("'title' and 'body' are required", 400)
    if len(title) > _MAX_TITLE_LEN or len(body) > _MAX_BODY_LEN:
        return json_error("title/body exceeds max length", 413)
    kind = str(data.get("kind") or "operator").strip().lower()
    worker = str(data.get("worker") or "").strip() or None
    task_id = str(data.get("task_id") or "").strip() or None

    try:
        thread = d.queen_chat.create_thread(
            title=title, kind=kind, worker_name=worker, task_id=task_id
        )
    except ValueError as e:
        return json_error(str(e), 400)
    msg = d.queen_chat.add_message(thread.id, role="operator", content=body, widgets=[])
    _broadcast_thread(d, thread.id, "created")
    _broadcast_message(d, thread.id, msg.to_dict())
    # Forward to Queen's PTY so her session sees the operator turn.
    delivered = await _forward_to_queen(d, thread.id, body)
    return web.json_response(
        {
            "thread": thread.to_dict(),
            "message": msg.to_dict(),
            "queen_delivered": delivered,
        }
    )


@handle_errors
async def handle_get_thread(request: web.Request) -> web.Response:
    d = get_daemon(request)
    thread_id = request.match_info["thread_id"]
    thread = d.queen_chat.get_thread(thread_id)
    if thread is None:
        return json_error("thread not found", 404)
    messages = d.queen_chat.list_messages(thread_id)
    return web.json_response(
        {"thread": thread.to_dict(), "messages": [m.to_dict() for m in messages]}
    )


@handle_errors
async def handle_post_message(request: web.Request) -> web.Response:
    """Operator posts a follow-up message into an existing thread."""
    d = get_daemon(request)
    thread_id = request.match_info["thread_id"]
    thread = d.queen_chat.get_thread(thread_id)
    if thread is None:
        return json_error("thread not found", 404)
    if thread.status == "resolved":
        return json_error("thread is resolved — start a new thread to continue", 409)
    data = await _parse_json(request)
    if isinstance(data, web.Response):
        return data
    body = str(data.get("body") or "").strip()
    if not body:
        return json_error("'body' is required", 400)
    if len(body) > _MAX_BODY_LEN:
        return json_error("body exceeds max length", 413)
    msg = d.queen_chat.add_message(thread_id, role="operator", content=body, widgets=[])
    _broadcast_message(d, thread_id, msg.to_dict())
    _broadcast_thread(d, thread_id, "updated")
    delivered = await _forward_to_queen(d, thread_id, body)
    return web.json_response({"message": msg.to_dict(), "queen_delivered": delivered})


@handle_errors
async def handle_resolve_thread(request: web.Request) -> web.Response:
    """Operator resolves a thread (approve/dismiss affordances both land here)."""
    d = get_daemon(request)
    thread_id = request.match_info["thread_id"]
    data = await _parse_json(request) if request.body_exists else {}
    if isinstance(data, web.Response):
        return data
    reason = str(data.get("reason") or "").strip()
    ok = d.queen_chat.resolve_thread(thread_id, resolved_by="operator", reason=reason)
    if not ok:
        return json_error("thread not found or already resolved", 404)
    _broadcast_thread(d, thread_id, "resolved")
    return web.json_response({"resolved": True})


# ---------------------------------------------------------------------------
# Broadcast + PTY-forward helpers (shared with the MCP conversation handlers)
# ---------------------------------------------------------------------------


def _broadcast_thread(daemon: SwarmDaemon, thread_id: str, event: str) -> None:
    try:
        store = getattr(daemon, "queen_chat", None)
        if store is None:
            return
        thread = store.get_thread(thread_id)
        if thread is None:
            return
        daemon.broadcast_ws({"type": "queen.thread", "event": event, "thread": thread.to_dict()})
    except Exception:
        _log.debug("queen thread broadcast failed", exc_info=True)


def _broadcast_message(
    daemon: SwarmDaemon, thread_id: str, message_dict: dict[str, object]
) -> None:
    try:
        daemon.broadcast_ws(
            {"type": "queen.message", "thread_id": thread_id, "message": message_dict}
        )
    except Exception:
        _log.debug("queen message broadcast failed", exc_info=True)


_OPERATOR_FORWARD_TEMPLATE = (
    "[operator in thread {thread_id}] {body}\n"
    "Respond via queen_reply (or queen_post_thread for a new topic)."
)


async def _forward_to_queen(daemon: object, thread_id: str, body: str) -> bool:
    """Inject the operator's message into the Queen's PTY so she reads it.

    The Queen runs as a regular Claude PTY process, so operator-turn
    input arrives through the same channel that workers use for direct
    operator chat.  We wrap the body with a thread-id hint so the
    Queen knows where to reply via her MCP tools.

    Returns ``True`` when the message was delivered into a live Queen PTY,
    ``False`` when the Queen is offline (message is still persisted in the
    thread — the caller surfaces this so the operator isn't left waiting on
    a reply that will never come).
    """
    from swarm.queen.runtime import find_queen

    queen = find_queen(getattr(daemon, "workers", []))
    if queen is None or queen.process is None or not queen.process.is_alive:
        _log.warning("queen not running — operator message persisted but not delivered")
        return False
    text = _OPERATOR_FORWARD_TEMPLATE.format(thread_id=thread_id, body=body)
    try:
        worker_svc = getattr(daemon, "worker_svc", None)
        if worker_svc is not None:
            await worker_svc.send_to_worker(queen.name, text, _log_operator=False)
        else:
            await queen.process.send_keys(text)
            await queen.process.send_enter()
        return True
    except Exception:
        _log.warning("queen PTY forward failed", exc_info=True)
        return False


@handle_errors
async def handle_list_learnings(request: web.Request) -> web.Response:
    """GET /api/queen/learnings?applied_to=&q=&limit= — saved corrections."""
    d = get_daemon(request)
    applied_to = request.query.get("applied_to") or None
    search = request.query.get("q") or None
    try:
        limit = min(int(request.query.get("limit", "50")), 500)
    except ValueError:
        limit = 50
    learnings = d.queen_chat.query_learnings(applied_to=applied_to, search=search, limit=limit)
    return web.json_response({"learnings": [item.to_dict() for item in learnings]})


@handle_errors
async def handle_delete_learning(request: web.Request) -> web.Response:
    """DELETE /api/queen/learnings/{id} — remove a stale/wrong correction."""
    d = get_daemon(request)
    try:
        learning_id = int(request.match_info["learning_id"])
    except ValueError:
        return json_error("learning_id must be an integer", status=400)
    if not d.queen_chat.delete_learning(learning_id):
        return json_error("learning not found", status=404)
    return web.json_response({"deleted": learning_id})
