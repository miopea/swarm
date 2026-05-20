"""Playbook routes — operator surface for the playbook-synthesis loop.

Phase 4 of ``docs/specs/playbook-synthesis-loop.md`` (swarm task #404).
Read-only list + operator promote/retire controls. Auth/CSRF is handled
by the global middleware (same ``X-Requested-With`` requirement as every
other ``/api`` route) — these handlers don't re-implement it.
"""

from __future__ import annotations

from aiohttp import web

from swarm.logging import get_logger
from swarm.server.helpers import get_daemon, handle_errors, json_error

_log = get_logger("server.routes.playbooks")


def register(app: web.Application) -> None:
    app.router.add_get("/api/playbooks", handle_list_playbooks)
    # /analytics MUST come before /{name}/... routes so it isn't shadowed
    # by aiohttp's URL dispatcher matching "analytics" as a playbook name.
    app.router.add_get("/api/playbooks/analytics", handle_analytics)
    app.router.add_get("/api/playbooks/{name}/events", handle_playbook_events)
    app.router.add_post("/api/playbooks/{name}/promote", handle_promote_playbook)
    app.router.add_post("/api/playbooks/{name}/retire", handle_retire_playbook)


@handle_errors
async def handle_list_playbooks(request: web.Request) -> web.Response:
    """All playbooks (candidates included, distinguished by ``status``).

    Optional ``?status=`` / ``?scope=`` filters. Candidates are returned
    so the operator can see what's pending vetting vs. fleet-active.
    """
    from swarm.playbooks.models import PlaybookStatus

    d = get_daemon(request)
    store = getattr(d, "playbook_store", None)
    if store is None:
        return json_error("playbook store unavailable", 503)
    scope = (request.query.get("scope") or "").strip() or None
    status_q = (request.query.get("status") or "").strip()
    status = None
    if status_q:
        try:
            status = PlaybookStatus(status_q)
        except ValueError:
            return json_error(f"invalid status '{status_q}'", 400)
    try:
        limit = min(int(request.query.get("limit", "200")), 500)
    except ValueError:
        limit = 200
    playbooks = store.list(scope=scope, status=status, limit=limit)
    return web.json_response({"playbooks": [p.to_api() for p in playbooks]})


@handle_errors
async def handle_promote_playbook(request: web.Request) -> web.Response:
    """Operator promotes a candidate playbook to active (fleet-propagated)."""
    d = get_daemon(request)
    store = getattr(d, "playbook_store", None)
    if store is None:
        return json_error("playbook store unavailable", 503)
    name = request.match_info["name"]
    ok = store.promote(name)
    if not ok:
        return json_error("playbook not found or already active", 404)
    _log.info("operator promoted playbook %s", name)
    return web.json_response({"promoted": True})


@handle_errors
async def handle_analytics(request: web.Request) -> web.Response:
    """Aggregate playbook stats for the dashboard analytics pane (P4).

    Optional ``?since_hours=N`` (default 24) bounds the event-count
    window. Returns ``totals`` / ``scope_breakdown`` / ``event_counts`` /
    ``top_by_uses`` / ``top_by_winrate`` — see ``PlaybookStore.get_analytics``.
    """
    import time as _time

    d = get_daemon(request)
    store = getattr(d, "playbook_store", None)
    if store is None:
        return json_error("playbook store unavailable", 503)
    try:
        since_hours = float(request.query.get("since_hours", "24"))
    except ValueError:
        since_hours = 24.0
    since_ts = _time.time() - max(0.0, since_hours) * 3600.0
    return web.json_response(store.get_analytics(since_ts=since_ts))


@handle_errors
async def handle_playbook_events(request: web.Request) -> web.Response:
    """Recent event timeline for one playbook (P4)."""
    d = get_daemon(request)
    store = getattr(d, "playbook_store", None)
    if store is None:
        return json_error("playbook store unavailable", 503)
    name = request.match_info["name"]
    pb = store.get(name)
    if pb is None:
        return json_error(f"playbook {name!r} not found", 404)
    try:
        limit = min(int(request.query.get("limit", "100")), 500)
    except ValueError:
        limit = 100
    events = store.get_events_for_playbook(pb.id, limit=limit)
    return web.json_response({"name": name, "events": events})


@handle_errors
async def handle_retire_playbook(request: web.Request) -> web.Response:
    """Operator retires a playbook (held off the fleet)."""
    d = get_daemon(request)
    store = getattr(d, "playbook_store", None)
    if store is None:
        return json_error("playbook store unavailable", 503)
    name = request.match_info["name"]
    try:
        body = await request.json() if request.body_exists else {}
    except Exception:
        return json_error("invalid JSON body", 400)
    reason = str((body or {}).get("reason") or "operator-retired").strip()
    ok = store.retire(name, reason)
    if not ok:
        return json_error("playbook not found or already retired", 404)
    _log.info("operator retired playbook %s (%s)", name, reason)
    return web.json_response({"retired": True})
