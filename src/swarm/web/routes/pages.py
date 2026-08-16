"""Full-page routes: dashboard and config."""

from __future__ import annotations

from typing import Any

import aiohttp_jinja2
from aiohttp import web

from swarm.server.helpers import get_daemon
from swarm.web.app import (
    _format_age,
    _get_ws_token,
    _queen_dict,
    _task_dicts,
    _worker_dicts,
    _worker_pending_counts,
    _worker_task_cards,
    _worker_task_titles,
)


def _jira_site_url(d: object) -> str:
    """The browsable Jira host, e.g. https://acme.atlassian.net — "" when unavailable.

    Defensive at every hop: Jira may be unconfigured, the integration may not be wired,
    and the token store may predate site_url being recorded. A missing base costs a
    hyperlink; an exception here would cost the whole dashboard render.
    """
    try:
        jira = getattr(getattr(d, "jira", None), "client", None)
        tm = getattr(jira, "_token_manager", None)
        return str(getattr(tm, "_site_url", "") or "").rstrip("/")
    except Exception:
        return ""


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

    return {
        "workers": _worker_dicts(d),
        "queen": _queen_dict(d),
        "selected_worker": selected,
        "worker_output": worker_output,
        "tasks": _task_dicts(d),
        "task_summary": d.task_board.summary(),
        # The initial full-page render must stamp the version too, not just the
        # htmx partial. Without it the page loads claiming version 0, the
        # reconciler sees instant "drift" and burns a refresh on every load.
        # Caught by the browser test — no source scan could see it, because both
        # the template and the partial handler were individually correct.
        "board_version": d.task_board.version,
        "worker_count": len(d.workers),
        # Base URL for linking a task to its Jira ticket (#1359, operator: "there is
        # nothing in the issue that links back to the jira ticket ... except a little
        # text"). jira_key has been in the task payload all along and was never rendered.
        #
        # Passed from the SERVER because the client cannot construct it: the API is
        # reached via api.atlassian.com/ex/jira/<cloudId>, which is not a browsable
        # address. The human-facing host is the `url` from the OAuth accessible-resources
        # response, discovered and persisted during auth. Empty when Jira is not
        # connected, and the UI renders a plain badge rather than a dead link.
        "jira_site_url": _jira_site_url(d),
        "drones_enabled": d.pilot.enabled if d.pilot else False,
        "ws_auth_required": True,  # auth is always required (auto-token if no explicit password)
        "ws_token": _get_ws_token(d),
        "proposals": proposals,
        "proposal_count": len(proposals),
        "worker_tasks": _worker_task_titles(d),
        "worker_task_cards": _worker_task_cards(d),
        "worker_pending": _worker_pending_counts(d),
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
