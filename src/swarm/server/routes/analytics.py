"""Analytics routes — task throughput summary."""

from __future__ import annotations

from aiohttp import web

from swarm.analysis.throughput import compute_throughput
from swarm.server.helpers import get_daemon


async def handle_analytics_summary(request: web.Request) -> web.Response:
    """GET /api/analytics/summary?days=7 — throughput + per-worker stats."""
    d = get_daemon(request)
    try:
        days = int(request.query.get("days", "7"))
    except ValueError:
        days = 7
    days = min(max(days, 1), 365)
    summary = compute_throughput(d.task_board.all_tasks, window_days=days)
    return web.json_response(summary)


def register(app: web.Application) -> None:
    """Register analytics routes."""
    app.router.add_get("/api/analytics/summary", handle_analytics_summary)
