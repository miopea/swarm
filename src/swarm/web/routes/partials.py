"""HTMX partial routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiohttp_jinja2
from aiohttp import web

from swarm.server.helpers import MAX_QUERY_LIMIT, get_daemon
from swarm.web.app import _queen_dict, _system_log_dicts, _task_dicts, _worker_dicts
from swarm.web.log_filter import LOG_LEVEL_INCLUSIVE, line_matches_level
from swarm.worker.worker import WorkerState


def _paginate(request: web.Request, items: list[Any]) -> tuple[int, list[Any], bool]:
    """Apply limit/offset pagination from query params. Returns (total, page, has_more).

    Default + cap match ``MAX_QUERY_LIMIT`` so this partial mirrors the
    initial dashboard render (which returns every task unconditionally).
    The dashboard JS never passes ``limit``, and the task panel has no
    "load more" affordance — pre-fix the default of 100 silently
    truncated any swarm with more than 100 tasks the moment a filter
    chip was clicked.
    """
    total = len(items)
    try:
        limit = min(int(request.query.get("limit", str(MAX_QUERY_LIMIT))), MAX_QUERY_LIMIT)
    except ValueError:
        limit = MAX_QUERY_LIMIT
    try:
        offset = max(0, int(request.query.get("offset", "0")))
    except ValueError:
        offset = 0
    page = items[offset : offset + limit]
    return total, page, offset + limit < total


@aiohttp_jinja2.template("partials/worker_list.html")
async def handle_partial_workers(request: web.Request) -> dict[str, Any]:
    d = get_daemon(request)
    worker_tasks: dict[str, str] = {}
    for t in d.task_board.active_tasks:
        if t.assigned_worker:
            worker_tasks[t.assigned_worker] = t.title
    return {
        "workers": _worker_dicts(d),
        "queen": _queen_dict(d),
        "selected_worker": request.query.get("worker"),
        "worker_tasks": worker_tasks,
    }


async def handle_partial_status(request: web.Request) -> web.Response:
    d = get_daemon(request)
    workers = d.workers
    total = len(workers)
    if total == 0:
        return web.Response(text="0 workers", content_type="text/html")

    from collections import Counter

    counts = Counter(w.display_state.value for w in workers)
    full_parts = []
    compact_parts = []
    for state in WorkerState:
        c = counts.get(state.value, 0)
        if c > 0:
            full_parts.append(f'<span class="{state.css_class}">{c} {state.display}</span>')
            abbrev = state.display[:3].upper()
            compact_parts.append(
                f'<span class="{state.css_class}" title="{state.display}">{c}{abbrev}</span>'
            )
    full_breakdown = ", ".join(full_parts)
    compact_breakdown = " ".join(compact_parts)
    html = (
        f'<span class="status-full">{total} workers: {full_breakdown}</span>'
        f'<span class="status-compact">{compact_breakdown}</span>'
    )
    return web.Response(text=html, content_type="text/html")


@aiohttp_jinja2.template("partials/task_list.html")
async def handle_partial_tasks(request: web.Request) -> dict[str, Any]:
    d = get_daemon(request)
    tasks = _task_dicts(d)

    # Filter by status (supports comma-separated multi-select from JS)
    status_filter = request.query.get("status")
    if status_filter and status_filter != "all":
        match_statuses: set[str] = set()
        for s in status_filter.split(","):
            s = s.strip()
            if s == "assigned":
                match_statuses.add("assigned")
            elif s:
                match_statuses.add(s)
        if match_statuses:
            tasks = [t for t in tasks if t["status"] in match_statuses]

    # Filter by priority (supports comma-separated multi-select)
    priority_filter = request.query.get("priority")
    if priority_filter and priority_filter != "all":
        priorities = {p.strip() for p in priority_filter.split(",") if p.strip()}
        if priorities:
            tasks = [t for t in tasks if t["priority"] in priorities]

    # Text search
    q = request.query.get("q", "").strip().lower()
    if q:
        tasks = [
            t for t in tasks if q in t["title"].lower() or q in (t.get("description") or "").lower()
        ]

    # Pagination — limit DOM size for large task lists
    total, tasks, has_more = _paginate(request, tasks)

    return {
        "tasks": tasks,
        "task_total": total,
        "task_has_more": has_more,
        "task_summary": d.task_board.summary(),
        "task_buttons": [
            {
                "label": b.label,
                "action": b.action,
                "show_mobile": b.show_mobile,
                "show_desktop": b.show_desktop,
            }
            for b in d.config.task_buttons
        ],
    }


@aiohttp_jinja2.template("partials/system_log.html")
async def handle_partial_system_log(request: web.Request) -> dict[str, Any]:
    d = get_daemon(request)
    category = request.query.get("category")
    notification = request.query.get("notification") == "true"
    query = request.query.get("q", "").strip() or None
    entries = _system_log_dicts(d, category=category, notification_only=notification, query=query)
    return {"entries": entries}


async def handle_partial_launch_config(request: web.Request) -> web.Response:
    d = get_daemon(request)
    running_names = {w.name.lower() for w in d.workers}
    workers = [
        {"name": w.name, "path": w.path, "running": w.name.lower() in running_names}
        for w in d.config.workers
    ]
    groups = [{"name": g.name, "workers": g.workers} for g in d.config.groups]
    return web.json_response({"workers": workers, "groups": groups})


async def handle_partial_task_history(request: web.Request) -> web.Response:
    """Return task history events as HTML for inline display."""
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    events = d.task_history.get_events(task_id, limit=50)
    if not events:
        return web.Response(
            text='<div class="history-empty">No history</div>',
            content_type="text/html",
        )

    from markupsafe import escape

    action_class = {
        "CREATED": "text-leaf",
        "PROPOSED": "text-honey",
        "APPROVED": "text-leaf",
        "ASSIGNED": "text-lavender",
        "COMPLETED": "text-leaf",
        "FAILED": "text-poppy",
        "REMOVED": "text-poppy",
        "EDITED": "text-honey",
    }
    parts = ['<div class="history-container">']
    for ev in events:
        cls = action_class.get(ev.action.value, "text-muted")
        ft = escape(ev.formatted_time)
        ts = ev.timestamp
        parts.append(
            f'<div class="history-entry">'
            f'<span class="history-time local-time" data-ts="{ts}">'
            f"{ft}</span>"
            f'<span class="history-action {cls}">'
            f"{escape(ev.action.value)}</span>"
            f'<span class="text-muted">{escape(ev.actor)}</span>'
        )
        if ev.detail:
            parts.append(f'<span class="history-detail">{escape(ev.detail)}</span>')
        parts.append("</div>")
    parts.append("</div>")
    html = "".join(parts)
    return web.Response(text=html, content_type="text/html")


async def handle_partial_logs(request: web.Request) -> web.Response:
    """Return the last N lines of ~/.swarm/swarm.log, optionally filtered by level.

    Lines are returned newest-first (the dashboard renders top-to-bottom).
    The severity filter is inclusive: selecting ``INFO`` returns
    INFO + WARNING + ERROR, mirroring how Python's logging module
    treats level thresholds.
    """
    log_path = Path.home() / ".swarm" / "swarm.log"
    if not log_path.exists():
        return web.Response(text="(no log file found)", content_type="text/plain")

    lines_count = min(int(request.query.get("lines", "500")), 5000)
    level_filter = request.query.get("level", "").upper()

    try:
        text = log_path.read_text(errors="replace")
    except OSError:
        return web.Response(text="(could not read log file)", content_type="text/plain")

    all_lines = text.splitlines()
    allowed = LOG_LEVEL_INCLUSIVE.get(level_filter)
    if allowed is not None:
        all_lines = [ln for ln in all_lines if line_matches_level(ln, allowed)]
    # Newest first — dashboard renders top-to-bottom, so we want the
    # most recent line at the top of the buffer.
    tail = list(reversed(all_lines[-lines_count:]))
    return web.Response(text="\n".join(tail), content_type="text/plain")


def register(app: web.Application) -> None:
    """Register partial routes."""
    app.router.add_get("/partials/workers", handle_partial_workers)
    app.router.add_get("/partials/status", handle_partial_status)
    app.router.add_get("/partials/tasks", handle_partial_tasks)
    app.router.add_get("/partials/system-log", handle_partial_system_log)
    app.router.add_get("/partials/launch-config", handle_partial_launch_config)
    app.router.add_get("/partials/task-history/{task_id}", handle_partial_task_history)
    app.router.add_get("/partials/logs", handle_partial_logs)
