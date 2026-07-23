"""Full-page routes: dashboard and config."""

from __future__ import annotations

from typing import Any

import aiohttp_jinja2
from aiohttp import web

from swarm.server.helpers import get_daemon
from swarm.web.app import _format_age, _get_ws_token, _queen_dict, _task_dicts, _worker_dicts


@aiohttp_jinja2.template("config.html")
async def handle_config_page(request: web.Request) -> dict[str, Any]:
    import secrets

    nonce = secrets.token_urlsafe(16)
    request["csp_nonce"] = nonce

    d = get_daemon(request)
    from swarm.config import _serialize_tuning, serialize_config
    from swarm.providers import list_builtin_providers, list_providers
    from swarm.update import _get_installed_version, _is_dev_install, build_sha

    po = {pname: _serialize_tuning(t) for pname, t in d.config.provider_overrides.items()}

    from swarm.server.routes.oauth import _connection_info

    return {
        "config": serialize_config(d.config),
        "mcp": _connection_info(d, request),
        "providers": list_providers(),
        "builtin_providers": list_builtin_providers(),
        "provider_overrides": po,
        "version": _get_installed_version(),
        "is_dev": _is_dev_install(),
        "build_sha": build_sha(),
        "csp_nonce": nonce,
        # Same server-injected WS auth token the dashboard uses.
        # Pre-fix the config page read only ``sessionStorage['swarm_api_password']``
        # which was empty for cookie-authenticated sessions, so its
        # /ws connect sent ``token: ''`` and tripped the wrong-token
        # lockout after 5 attempts — blocking the dashboard's /ws too
        # since they share the per-IP lockout.
        "ws_token": _get_ws_token(d),
    }


@aiohttp_jinja2.template("dashboard.html")
async def handle_dashboard(request: web.Request) -> dict[str, Any]:
    import secrets

    nonce = secrets.token_urlsafe(16)
    request["csp_nonce"] = nonce

    d = get_daemon(request)
    from swarm.providers import list_providers
    from swarm.update import _get_installed_version, _is_dev_install, build_sha

    selected = request.query.get("worker")

    worker_output = ""
    if selected:
        worker = d.get_worker(selected)
        if worker:
            worker_output = await d.safe_capture_output(selected)

    proposals = [
        {
            **d.proposal_dict(p),
            "age_str": _format_age(p.created_at),
        }
        for p in d.proposal_store.pending
    ]

    # Build worker->task_title map
    worker_tasks: dict[str, str] = {}
    for t in d.task_board.active_tasks:
        if t.assigned_worker:
            worker_tasks[t.assigned_worker] = t.title

    return {
        "workers": _worker_dicts(d),
        "queen": _queen_dict(d),
        "selected_worker": selected,
        "worker_output": worker_output,
        "tasks": _task_dicts(d),
        "task_summary": d.task_board.summary(),
        "worker_count": len(d.workers),
        "drones_enabled": d.pilot.enabled if d.pilot else False,
        "ws_auth_required": True,  # auth is always required (auto-token if no explicit password)
        "ws_token": _get_ws_token(d),
        "proposals": proposals,
        "proposal_count": len(proposals),
        "worker_tasks": worker_tasks,
        "tool_buttons": [{"label": b.label, "command": b.command} for b in d.config.tool_buttons],
        "action_buttons": [
            {
                "label": b.label,
                "action": b.action,
                "command": b.command,
                "style": b.style,
                "show_mobile": b.show_mobile,
                "show_desktop": b.show_desktop,
            }
            for b in d.config.action_buttons
        ],
        "task_buttons": [
            {
                "label": b.label,
                "action": b.action,
                "show_mobile": b.show_mobile,
                "show_desktop": b.show_desktop,
            }
            for b in d.config.task_buttons
        ],
        "tunnel": d.tunnel.to_dict(),
        "providers": list_providers(),
        "version": _get_installed_version(),
        "is_dev": _is_dev_install(),
        "build_sha": build_sha(),
        "csp_nonce": nonce,
    }


def register(app: web.Application) -> None:
    """Register page routes."""
    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/dashboard", handle_dashboard)
    app.router.add_get("/config", handle_config_page)
