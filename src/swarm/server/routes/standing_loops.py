"""Standing background-improvement loop routes (#765).

Operator controls for the recurring task generators: per-worker start / pause /
stop, the global kill switch, and a live per-loop token-burn readout. The
dashboard is the intended surface (CLAUDE.md mandates dashboard-first
operation); an always-on token-burn source MUST have a one-click stop.
"""

from __future__ import annotations

from aiohttp import web

from swarm.server.helpers import get_daemon, handle_errors, json_error


def register(app: web.Application) -> None:
    app.router.add_get("/api/standing-loops", handle_status)
    app.router.add_post("/api/standing-loops/start", handle_start)
    app.router.add_post("/api/standing-loops/pause", handle_pause)
    app.router.add_post("/api/standing-loops/stop", handle_stop)
    app.router.add_post("/api/standing-loops/kill-switch", handle_kill_switch)


@handle_errors
async def handle_status(request: web.Request) -> web.Response:
    """Live readout: kill-switch state + per-worker loop state and token burn."""
    d = get_daemon(request)
    return web.json_response(d.standing_loop.status())


async def _worker_action(request: web.Request, action: str) -> web.Response:
    d = get_daemon(request)
    body = await request.json() if request.can_read_body else {}
    worker = str(body.get("worker", "")).strip()
    if not worker:
        return json_error("worker is required", status=400)
    getattr(d.standing_loop, action)(worker)
    return web.json_response(d.standing_loop.status())


@handle_errors
async def handle_start(request: web.Request) -> web.Response:
    return await _worker_action(request, "start")


@handle_errors
async def handle_pause(request: web.Request) -> web.Response:
    return await _worker_action(request, "pause")


@handle_errors
async def handle_stop(request: web.Request) -> web.Response:
    return await _worker_action(request, "stop")


@handle_errors
async def handle_kill_switch(request: web.Request) -> web.Response:
    """Global kill switch — halts generation for every loop at once."""
    d = get_daemon(request)
    body = await request.json() if request.can_read_body else {}
    on = bool(body.get("on", True))
    d.standing_loop.set_kill_switch(on)
    return web.json_response(d.standing_loop.status())
