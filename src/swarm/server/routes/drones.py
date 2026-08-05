"""Drone routes — log, status, toggle, tuning, rules, notifications."""

from __future__ import annotations

import time

from aiohttp import web

from swarm.server.helpers import get_daemon, handle_errors, json_error, parse_limit, parse_offset


def register(app: web.Application) -> None:
    app.router.add_get("/api/drones/log", handle_drone_log)
    app.router.add_get("/api/drones/status", handle_drone_status)
    app.router.add_post("/api/drones/toggle", handle_drone_toggle)
    app.router.add_post("/api/drones/poll", handle_drones_poll)
    app.router.add_get("/api/drones/tuning", handle_tuning_suggestions)
    app.router.add_get("/api/drones/rules/analytics", handle_rule_analytics)
    app.router.add_get("/api/drones/approval-rate", handle_approval_rate)
    app.router.add_post("/api/drones/rules/suggest", handle_rule_suggest)
    app.router.add_get("/api/notifications", handle_notification_history)
    app.router.add_post("/api/notifications", handle_notification_raise)
    app.router.add_get("/api/queen/oversight", handle_oversight_status)
    app.router.add_get("/api/coordination/ownership", handle_ownership_status)
    app.router.add_get("/api/coordination/sync", handle_sync_status)


@handle_errors
async def handle_drone_log(request: web.Request) -> web.Response:
    d = get_daemon(request)
    limit = parse_limit(request)
    offset = parse_offset(request)

    worker = request.query.get("worker")
    action = request.query.get("action")
    category = request.query.get("category")
    since_str = request.query.get("since")
    overridden_str = request.query.get("overridden")

    use_store = any([worker, action, category, since_str, overridden_str])

    if use_store and d.drone_log.store is not None:
        since = float(since_str) if since_str else None
        overridden = None
        if overridden_str is not None:
            overridden = overridden_str.lower() in ("true", "1", "yes")
        rows = d.drone_log.query(
            worker_name=worker,
            action=action.upper() if action else None,
            category=category,
            since=since,
            overridden=overridden,
            limit=limit,
            offset=offset,
        )
        return web.json_response(
            {
                "entries": rows,
                "limit": limit,
                "offset": offset,
                "has_more": len(rows) == limit,
            }
        )

    all_entries = d.drone_log.entries
    total = len(all_entries)
    # Apply offset/limit to in-memory entries (newest last → reverse slice)
    start = max(0, total - offset - limit)
    end = max(0, total - offset)
    page = all_entries[start:end]
    return web.json_response(
        {
            "entries": [
                {
                    "time": e.formatted_time,
                    "timestamp": e.timestamp,
                    "action": e.action.value.lower(),
                    "worker": e.worker_name,
                    "detail": e.detail,
                }
                for e in page
            ],
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < total,
        }
    )


@handle_errors
async def handle_tuning_suggestions(request: web.Request) -> web.Response:
    """Return auto-tuning suggestions based on override patterns."""
    from swarm.drones.tuning import analyze_overrides

    d = get_daemon(request)
    store = d.drone_log.store
    if store is None:
        return web.json_response({"suggestions": []})
    try:
        days = int(request.query.get("days", "7"))
    except ValueError:
        return json_error("Invalid 'days' parameter — must be an integer", 400)
    suggestions = analyze_overrides(store, days=days)
    return web.json_response(
        {
            "suggestions": [
                {
                    "id": s.id,
                    "description": s.description,
                    "config_path": s.config_path,
                    "current_value": s.current_value,
                    "suggested_value": s.suggested_value,
                    "reason": s.reason,
                    "override_count": s.override_count,
                    "total_decisions": s.total_decisions,
                    "override_rate": round(s.override_rate, 2),
                }
                for s in suggestions
            ]
        }
    )


@handle_errors
async def handle_rule_analytics(request: web.Request) -> web.Response:
    """Return per-rule firing statistics from the decision log."""
    d = get_daemon(request)
    store = d.drone_log.store
    if store is None:
        return web.json_response({"analytics": [], "config_rules": []})

    try:
        days = int(request.query.get("days", "7"))
    except ValueError:
        return json_error("Invalid 'days' parameter — must be an integer", 400)
    since = time.time() - days * 86400
    analytics = store.rule_analytics(since=since)

    config_rules = [
        {"pattern": r.pattern, "action": r.action} for r in d.config.drones.approval_rules
    ]

    return web.json_response({"analytics": analytics, "config_rules": config_rules})


@handle_errors
async def handle_approval_rate(request: web.Request) -> web.Response:
    """Return the drone auto-approval rate over a rolling window.

    Query: ``?hours=24`` (default 24). Returns ``{approvals, escalations,
    rate, window_hours}``. ``rate`` is ``null`` when the window has no
    approval/escalation events.
    """
    d = get_daemon(request)
    try:
        hours = float(request.query.get("hours", "24"))
    except ValueError:
        return json_error("Invalid 'hours' parameter — must be a number", 400)
    if hours <= 0:
        return json_error("'hours' must be positive", 400)
    since = time.time() - hours * 3600
    stats = d.drone_log.approval_rate(since=since)
    return web.json_response({**stats, "window_hours": hours})


@handle_errors
async def handle_rule_suggest(request: web.Request) -> web.Response:
    """Suggest a drone approval rule pattern from log detail strings."""
    from swarm.drones.suggest import suggest_rule

    try:
        body = await request.json()
    except Exception:
        return json_error("Invalid JSON body", 400)

    details = body.get("details")
    if not details or not isinstance(details, list):
        return json_error("'details' is required and must be a non-empty list of strings", 400)
    if not all(isinstance(d, str) for d in details):
        return json_error("'details' must contain only strings", 400)

    action = body.get("action", "approve")
    if action not in ("approve", "escalate"):
        return json_error("'action' must be 'approve' or 'escalate'", 400)

    suggestion = suggest_rule(details, action=action)
    return web.json_response(
        {
            "suggestion": {
                "pattern": suggestion.pattern,
                "action": suggestion.action,
                "confidence": suggestion.confidence,
                "explanation": suggestion.explanation,
            }
        }
    )


@handle_errors
async def handle_notification_history(request: web.Request) -> web.Response:
    """Return recent notification history."""
    d = get_daemon(request)
    limit = min(int(request.query.get("limit", "50")), 50)
    history = d.escalation._notification_history[-limit:]
    return web.json_response({"notifications": list(reversed(history))})


@handle_errors
async def handle_notification_raise(request: web.Request) -> web.Response:
    """Raise an operator notification from an EXTERNAL tool (#1265).

    Before this, the only way for something outside the daemon to reach the
    operator was to INSERT into ``buzz_log`` directly — which
    ``bfg-solutions/scripts/credential-check-cron.sh`` did, documenting the
    coupling and filing this endpoint as the follow-up. ``GET
    /api/notifications`` is history-only, and ``/api/hooks/event`` only speaks
    Claude Code lifecycle events.

    DELIBERATELY DOES ONE THING: appends a drone-log entry with
    ``is_notification=True``. It does NOT also call ``push_notification`` —
    ``StatePublisher`` already fans notification-worthy entries out to the
    WebSocket (see ``state_publisher.py``), so calling both would deliver the
    notification twice. Using the same single entry point is what makes an
    external notification indistinguishable from an internal one on the
    dashboard, rather than something that merely looks similar.

    FAILURE MODE, stated because a notifier that fails quietly is worse than
    none: a rejected request returns 4xx with a reason and NOTHING is recorded;
    a caller that never reaches the daemon at all gets a connection error. In
    both cases the failure is the CALLER's to surface — the daemon cannot notify
    you that it failed to notify you, and pretending otherwise would be the same
    circularity as a check that reports on itself. Callers that must not lose
    the signal should treat a non-2xx as a hard failure (``curl --fail``) rather
    than best-effort.
    """
    d = get_daemon(request)
    body = await request.json()

    detail = str(body.get("detail") or body.get("message") or "").strip()
    if not detail:
        return json_error("Missing 'detail' — say what the operator needs to know.")
    label = str(body.get("label") or body.get("action") or "external").strip()
    source = str(body.get("source") or "external").strip()
    worker = str(body.get("worker") or "external").strip()

    _MAX_DETAIL = 2000
    if len(detail) > _MAX_DETAIL:
        detail = detail[: _MAX_DETAIL - 3] + "..."

    from swarm.drones.log import LogCategory, SystemAction

    entry = d.drone_log.add(
        SystemAction.EXTERNAL_NOTIFICATION,
        worker,
        f"{label}: {detail}" if label != "external" else detail,
        category=LogCategory.OPERATOR,
        is_notification=True,
        metadata={"source": source, "label": label},
    )
    return web.json_response(
        {
            "status": "raised",
            "label": label,
            "source": source,
            "worker": worker,
            "timestamp": getattr(entry, "timestamp", None),
        }
    )


@handle_errors
async def handle_oversight_status(request: web.Request) -> web.Response:
    """Return Queen oversight monitor status."""
    d = get_daemon(request)
    monitor = getattr(d, "_oversight_monitor", None)
    if monitor is None:
        return web.json_response({"enabled": False})
    return web.json_response(monitor.get_status())


@handle_errors
async def handle_ownership_status(request: web.Request) -> web.Response:
    """Return file ownership map status."""
    d = get_daemon(request)
    ownership = getattr(d, "file_ownership", None)
    if ownership is None:
        return web.json_response({"mode": "off"})
    return web.json_response(ownership.to_dict())


@handle_errors
async def handle_sync_status(request: web.Request) -> web.Response:
    """Return auto-pull sync status."""
    d = get_daemon(request)
    sync = getattr(d, "auto_pull", None)
    if sync is None:
        return web.json_response({"enabled": False})
    return web.json_response(sync.get_status())


@handle_errors
async def handle_drone_status(request: web.Request) -> web.Response:
    d = get_daemon(request)
    return web.json_response(
        {
            "enabled": d.pilot.enabled if d.pilot else False,
        }
    )


@handle_errors
async def handle_drone_toggle(request: web.Request) -> web.Response:
    d = get_daemon(request)
    if d.pilot:
        new_state = d.toggle_drones()
        return web.json_response({"enabled": new_state})
    return json_error("pilot not running")


@handle_errors
async def handle_drones_poll(request: web.Request) -> web.Response:
    d = get_daemon(request)
    if not d.pilot:
        return json_error("pilot not running")
    had_action = await d.poll_once()
    return web.json_response({"status": "ok", "had_action": had_action})
