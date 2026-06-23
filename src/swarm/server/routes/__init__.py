"""Domain-specific route modules for the swarm API."""

from __future__ import annotations

from aiohttp import web


def register_all(app: web.Application) -> None:
    """Register all route modules on the application."""
    from swarm.mcp.server import register as register_mcp
    from swarm.server.routes import (
        analytics,
        attention,
        config,
        drones,
        events,
        feedback,
        harness_digest,
        hooks,
        jira,
        messages,
        pipelines,
        playbooks,
        proposals,
        queen,
        standing_loops,
        system,
        tasks,
        websocket,
        workers,
    )

    workers.register(app)
    analytics.register(app)
    drones.register(app)
    hooks.register(app)
    messages.register(app)
    register_mcp(app)
    jira.register(app)
    queen.register(app)
    tasks.register(app)
    pipelines.register(app)
    playbooks.register(app)
    proposals.register(app)
    harness_digest.register(app)
    standing_loops.register(app)
    system.register(app)
    config.register(app)
    feedback.register(app)
    websocket.register(app)
    # Command Center
    events.register(app)
    attention.register(app)
