"""Harness-improvement digest route (operator-gated hill-climbing, Loop 4).

GET-only and read-only. The digest's suggestions carry ``apply_action``
objects that name EXISTING endpoints (``/api/config/approval-rules``,
``/api/playbooks/{name}/retire|promote``); this route adds NO apply endpoints
of its own — apply reuses those battle-tested, server-validated routes. That
keeps "no novel apply path / no autonomous self-rewriting" a structural fact.
"""

from __future__ import annotations

from aiohttp import web

from swarm.server.helpers import get_daemon, handle_errors


def register(app: web.Application) -> None:
    app.router.add_get("/api/harness-digest", handle_digest)


@handle_errors
async def handle_digest(request: web.Request) -> web.Response:
    """Aggregate the hill-climbing signals into one operator review digest."""
    from swarm.analysis.harness_digest import collect_digest

    d = get_daemon(request)
    return web.json_response(collect_digest(d).to_api())
