"""Attention queue routes — the operator's must-act surface.

The Attention queue is an **exception queue**: it surfaces only what is
genuinely escalated to a human or a hard failure the autonomous layers
can't resolve, ranked by urgency. Everything the Queen/drones are actively
handling is demoted to a collapsed "Queen is handling" drawer. The
classification policy is a pure function in :mod:`swarm.server.attention_model`;
this route just gathers live snapshots and delegates to it.

The ``reply`` verb is what makes Attention distinct from the existing
``queen.thread`` routes: it (a) appends an operator message to the thread,
(b) sends a real ``queen → worker`` message via ``message_store`` (so the
worker's PTY actually gets a reply), and (c) resolves the thread. Closes
the loop in one call without leaving the panel.
"""

from __future__ import annotations

from aiohttp import web

from swarm.logging import get_logger
from swarm.server import attention_model
from swarm.server.helpers import get_daemon, handle_errors, json_error
from swarm.server.routes.queen import _broadcast_message, _broadcast_thread

_log = get_logger("server.routes.attention")

# Window for counting REVIVED entries when deciding crash-loop severity.
_CRASH_LOOP_WINDOW = 600.0
# Threads whose latest message is worth showing as card detail.
_DETAIL_KINDS = attention_model._QUEEN_REVIEW_KINDS

_MAX_BODY_LEN = 8000


def register(app: web.Application) -> None:
    app.router.add_get("/api/attention", handle_list_attention)
    app.router.add_post("/api/attention/{thread_id}/reply", handle_reply)
    app.router.add_post("/api/attention/{thread_id}/resolve", handle_resolve)


@handle_errors
async def handle_list_attention(request: web.Request) -> web.Response:
    """Return the exception queue: ``{critical, decision, handled}``.

    Gathers live snapshots (threads, pending proposals, worker state, the
    autonomous-layer's recent actions from the buzz log, blockers, resource
    pressure) and delegates the policy to
    :func:`swarm.server.attention_model.classify`.
    """
    import time

    d = get_daemon(request)
    try:
        limit = min(int(request.query.get("limit", "100")), 500)
    except ValueError:
        limit = 100

    now = time.time()
    cfg = attention_model.AttentionConfig()
    chat = getattr(d, "queen_chat", None)
    buzz = getattr(getattr(d, "drone_log", None), "_buzz_store", None)

    view = attention_model.classify(
        threads=_gather_threads(chat, limit),
        proposals=_gather_proposals(d),
        workers=_gather_workers(d, buzz, now),
        nudged_workers=_gather_nudged(buzz, now, cfg),
        blocked_workers=_gather_blocked(d),
        resource_snapshot=getattr(getattr(d, "resource_mon", None), "snapshot", None),
        now=now,
        queen_busy=_queen_busy(d),
        cfg=cfg,
    )
    return web.json_response(view.to_dict())


def _queen_busy(d: object) -> bool:
    """True only when the Queen worker is actively processing. An idle
    Queen cannot be 'currently working on' a relayed message."""
    from swarm.worker.worker import QUEEN_WORKER_NAME

    for w in getattr(d, "workers", []):
        if w.name == QUEEN_WORKER_NAME:
            return w.state.value == "BUZZING"
    return False


def _gather_threads(chat: object, limit: int) -> list[attention_model.ThreadSnap]:
    if chat is None:
        return []
    out: list[attention_model.ThreadSnap] = []
    for t in chat.list_threads(status="active", limit=limit):
        if t.kind == "operator":
            continue
        latest: str | None = None
        if t.kind in _DETAIL_KINDS:
            try:
                msg = chat.latest_message(t.id)
                latest = msg.content if msg else None
            except Exception:
                _log.debug("attention: latest_message(%s) failed", t.id, exc_info=True)
        out.append(
            attention_model.ThreadSnap(
                id=t.id,
                kind=t.kind,
                title=t.title or "",
                worker_name=t.worker_name,
                task_id=t.task_id,
                created_at=t.created_at,
                updated_at=t.updated_at,
                latest_message=latest,
            )
        )
    return out


def _gather_proposals(d: object) -> list[attention_model.ProposalSnap]:
    store = getattr(d, "proposal_store", None)
    if store is None:
        return []
    out: list[attention_model.ProposalSnap] = []
    for p in store.pending:
        ptype = getattr(p.proposal_type, "value", str(p.proposal_type))
        out.append(
            attention_model.ProposalSnap(
                id=p.id,
                proposal_type=ptype,
                worker_name=p.worker_name,
                task_id=p.task_id or None,
                task_title=p.task_title or "",
                reasoning=p.reasoning or "",
                assessment=p.assessment or "",
                confidence=float(p.confidence or 0.0),
                is_plan=bool(p.is_plan),
                created_at=p.created_at,
            )
        )
    return out


def _gather_workers(d: object, buzz: object, now: float) -> list[attention_model.WorkerSnap]:
    from swarm.worker.worker import QUEEN_WORKER_NAME

    pilot = getattr(d, "pilot", None)
    waiting = getattr(pilot, "_waiting_content", {}) or {}

    # Batch the buzz-log lookups once rather than 2 queries per STUNG worker.
    # query() returns newest-first, so the first WORKER_STUNG row seen per
    # worker is its most recent stung detail.
    revive_counts: dict[str, int] = {}
    last_stung_by_worker: dict[str, str | None] = {}
    if buzz is not None:
        try:
            for r in buzz.query(action="REVIVED", since=now - _CRASH_LOOP_WINDOW, limit=1000):
                wname = r.get("worker_name")
                if wname:
                    revive_counts[wname] = revive_counts.get(wname, 0) + 1
            for r in buzz.query(action="WORKER_STUNG", since=now - 3600, limit=1000):
                wname = r.get("worker_name")
                if wname and wname not in last_stung_by_worker:
                    last_stung_by_worker[wname] = r.get("detail")
        except Exception:
            _log.debug("attention: buzz batch query failed", exc_info=True)

    out: list[attention_model.WorkerSnap] = []
    for w in getattr(d, "workers", []):
        if w.name == QUEEN_WORKER_NAME:
            continue
        state = w.state.value
        revive_at = getattr(w, "_revive_at", 0.0)
        grace = getattr(w, "revive_grace", 15.0)
        in_grace = revive_at > 0 and (now - revive_at) < grace
        revive_count = revive_counts.get(w.name, 0) if state == "STUNG" else 0
        last_stung = last_stung_by_worker.get(w.name) if state == "STUNG" else None
        out.append(
            attention_model.WorkerSnap(
                name=w.name,
                state=state,
                state_duration=w.state_duration,
                needs_operator_input=bool(getattr(w, "needs_operator_input", False)),
                in_revive_grace=in_grace,
                task_id=None,
                waiting_excerpt=waiting.get(w.name) or None,
                revive_count=revive_count,
                last_stung_detail=last_stung,
            )
        )
    return out


def _gather_nudged(buzz: object, now: float, cfg: attention_model.AttentionConfig) -> set[str]:
    if buzz is None:
        return set()
    try:
        rows = buzz.query(action="AUTO_NUDGE", since=now - cfg.nudge_window_seconds, limit=1000)
    except Exception:
        _log.debug("attention: AUTO_NUDGE query failed", exc_info=True)
        return set()
    return {r.get("worker_name") for r in rows if r.get("worker_name")}


def _gather_blocked(d: object) -> set[str]:
    store = getattr(d, "blocker_store", None)
    if store is None:
        return set()
    try:
        # One query for the whole board instead of one per worker.
        blocked = store.active_worker_names()
    except Exception:
        _log.debug("attention: active_worker_names failed", exc_info=True)
        return set()
    worker_names = {w.name for w in getattr(d, "workers", [])}
    return {name for name in blocked if name in worker_names}


@handle_errors
async def handle_reply(request: web.Request) -> web.Response:
    """Operator replies to a worker's Attention card → message + resolve."""
    d = get_daemon(request)
    thread_id = request.match_info["thread_id"]
    chat = getattr(d, "queen_chat", None)
    if chat is None:
        return json_error("queen_chat unavailable", 503)
    thread = chat.get_thread(thread_id)
    if thread is None:
        return json_error("thread not found", 404)
    if thread.status == "resolved":
        return json_error("thread already resolved", 409)
    try:
        data = await request.json()
    except Exception:
        return json_error("invalid JSON body", 400)
    body = str(data.get("body") or "").strip()
    if not body:
        return json_error("'body' is required", 400)
    if len(body) > _MAX_BODY_LEN:
        return json_error("body exceeds max length", 413)

    msg = chat.add_message(thread_id, role="operator", content=body, widgets=[])
    _broadcast_message(d, thread_id, msg.to_dict())

    sent_id, nudged = await _deliver_reply_to_worker(d, thread.worker_name, body)

    ok = chat.resolve_thread(thread_id, resolved_by="operator", reason="operator replied")
    if ok:
        _broadcast_thread(d, thread_id, "resolved")
    return web.json_response(
        {
            "message": msg.to_dict(),
            "delivered_to": thread.worker_name,
            "delivered_id": sent_id,
            "nudged": nudged,
        }
    )


async def _deliver_reply_to_worker(
    d: object, worker: str | None, body: str
) -> tuple[int | None, bool]:
    """Deliver an operator reply to a worker two ways: persist a row and
    inject a short prompt into the worker's PTY.

    Returns ``(sent_id, nudged)``.
    """
    if not worker:
        return None, False

    from swarm.worker.worker import QUEEN_WORKER_NAME

    sent_id: int | None = None
    store = getattr(d, "message_store", None)
    if store is not None:
        try:
            sent_id = store.send(QUEEN_WORKER_NAME, worker, "status", body)
        except Exception:
            _log.warning("attention reply: store.send failed", exc_info=True)

    worker_svc = getattr(d, "worker_svc", None)
    if worker_svc is None:
        return sent_id, False

    nudge = (
        f"[operator reply via Queen Dashboard] {body[:400]}\n"
        "Full thread: `swarm_check_messages`. Resume the blocked work."
    )
    try:
        await worker_svc.send_to_worker(worker, nudge, _log_operator=False)
        return sent_id, True
    except Exception:
        _log.warning("attention reply: send_to_worker(%s) failed", worker, exc_info=True)
        return sent_id, False


@handle_errors
async def handle_resolve(request: web.Request) -> web.Response:
    """Dismiss an Attention card without sending a reply."""
    d = get_daemon(request)
    thread_id = request.match_info["thread_id"]
    chat = getattr(d, "queen_chat", None)
    if chat is None:
        return json_error("queen_chat unavailable", 503)
    try:
        data = await request.json() if request.body_exists else {}
    except Exception:
        data = {}
    reason = str((data or {}).get("reason") or "").strip() or "operator dismissed"
    ok = chat.resolve_thread(thread_id, resolved_by="operator", reason=reason)
    if not ok:
        return json_error("thread not found or already resolved", 404)
    _broadcast_thread(d, thread_id, "resolved")
    return web.json_response({"resolved": True})
