"""Web dashboard — Jinja2 + HTMX frontend served by the daemon."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp_jinja2
import jinja2
from aiohttp import web

from swarm.logging import get_logger
from swarm.tasks.task import (
    PRIORITY_LABEL,
    STATUS_ICON,
    STATUS_LABEL,
    TASK_TYPE_LABEL,
    TaskStatus,
)
from swarm.worker.worker import WorkerState, format_duration

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("web.app")

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Shared utilities (used by route modules)
# ---------------------------------------------------------------------------


def _get_ws_token(daemon: SwarmDaemon) -> str:
    """Return the token the dashboard should use for WebSocket auth.

    For same-origin page loads the server injects the effective token
    directly into the rendered HTML so the user is never prompted.
    """
    from swarm.server.api import get_api_password

    return get_api_password(daemon)


def _worker_dicts(daemon: SwarmDaemon) -> list[dict[str, Any]]:
    """Serialize regular (non-Queen) workers for the sidebar worker list.

    The Queen is rendered separately via :func:`_queen_dict` so she can
    occupy a dedicated card above the worker list — she's the coordinator,
    not a peer worker, and mixing her in was misleading.
    """
    result = []
    for w in daemon.workers:
        if w.is_queen:
            continue
        d = w.to_api_dict()
        d["in_config"] = daemon.config.get_worker(w.name) is not None
        # STUNG workers show a countdown to removal
        if d["state"] == WorkerState.STUNG.value:
            remaining = max(0, w.stung_reap_timeout - w.state_duration)
            d["state_duration"] = f"{int(remaining)}s"
        else:
            d["state_duration"] = format_duration(w.state_duration)
        result.append(d)
    return result


def _queen_dict(daemon: SwarmDaemon) -> dict[str, Any] | None:
    """Serialize the Queen for her dedicated sidebar card.

    Returns ``None`` when she isn't running — the template renders an
    "offline" placeholder in that case so the spot doesn't disappear
    mid-session.
    """
    for w in daemon.workers:
        if not w.is_queen:
            continue
        d = w.to_api_dict()
        d["state_duration"] = format_duration(w.state_duration)
        return d
    return None


def _format_age(ts: float) -> str:
    """Format a timestamp as a human-readable age string."""
    import time

    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _task_dicts(daemon: SwarmDaemon) -> list[dict[str, Any]]:
    all_tasks = daemon.task_board.all_tasks
    completed_ids = {t.id for t in all_tasks if t.status == TaskStatus.DONE}
    title_by_id = {t.id: t.title for t in all_tasks}
    return [
        {
            "id": t.id,
            "title": t.title,
            "description": t.description,
            "status": t.status.value,
            "status_icon": STATUS_ICON.get(t.status, "?"),
            "status_label": STATUS_LABEL.get(t.status, t.status.value),
            "priority": t.priority.value,
            "priority_label": PRIORITY_LABEL.get(t.priority, ""),
            "task_type": t.task_type.value,
            "task_type_label": TASK_TYPE_LABEL.get(t.task_type, "Chore"),
            "assigned_worker": t.assigned_worker,
            "created_age": _format_age(t.created_at),
            "updated_age": _format_age(t.updated_at),
            "tags": t.tags,
            "attachments": t.attachments,
            "depends_on": t.depends_on,
            "depends_on_titles": [title_by_id.get(d, d) for d in t.depends_on],
            "blocked": bool(t.depends_on and not all(d in completed_ids for d in t.depends_on)),
            "resolution": t.resolution,
            "source_email_id": t.source_email_id,
            "number": t.number,
            "is_cross_project": t.is_cross_project,
            "source_worker": t.source_worker,
            "target_worker": t.target_worker,
            "dependency_type": t.dependency_type,
            "acceptance_criteria": t.acceptance_criteria,
            "context_refs": t.context_refs,
            "cost_budget": t.cost_budget,
            "cost_spent": round(t.cost_spent, 4),
            "learnings": t.learnings,
            "verification_status": t.verification_status.value,
            "verification_reason": t.verification_reason,
            "verification_reopen_count": t.verification_reopen_count,
        }
        for t in all_tasks
    ]


def _system_log_dicts(
    daemon: SwarmDaemon,
    limit: int = 50,
    category: str | None = None,
    notification_only: bool = False,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """Build system log entry dicts with optional category/notification/text filters.

    When no category is specified, SYSTEM entries are excluded by default
    (they belong on the config page's deeper log).
    """
    from swarm.drones.log import LogCategory

    entries = daemon.drone_log.entries
    if category:
        cats: set[LogCategory] = set()
        for c in category.split(","):
            try:
                cats.add(LogCategory(c.strip()))
            except ValueError:
                pass
        if cats:
            entries = [e for e in entries if e.category in cats]
    else:
        # Exclude system-category entries by default
        entries = [e for e in entries if e.category != LogCategory.SYSTEM]
    if notification_only:
        entries = [e for e in entries if e.is_notification]
    if query:
        q = query.lower()
        entries = [
            e
            for e in entries
            if q in e.worker_name.lower() or q in e.action.value.lower() or q in e.detail.lower()
        ]
    entries = list(reversed(entries[-limit:]))
    return [
        {
            "time": e.formatted_time,
            "timestamp": e.timestamp,
            "action": e.action.value.lower(),
            "worker": e.worker_name,
            "detail": e.detail,
            "category": e.category.value,
            "is_notification": e.is_notification,
            "prompt_snippet": e.metadata.get("prompt_snippet", ""),
            "repeat_count": e.repeat_count,
        }
        for e in entries
    ]


def _resolve_web_dirs(app: web.Application) -> tuple[Path, Path]:
    """Resolve templates/static dirs, preferring source tree for dev mode.

    Set SWARM_DEV=1 (uses config file's parent as source root) or
    SWARM_DEV=/path/to/swarm to serve templates and static files from
    the source tree. Edits are reflected on page reload without reinstalling.
    """
    import os

    templates_dir = TEMPLATES_DIR
    static_dir = STATIC_DIR

    dev_val = os.environ.get("SWARM_DEV", "")
    dev_root = ""
    if dev_val and dev_val != "0":
        if dev_val == "1":
            # Resolve from config file location
            daemon = app.get("daemon")
            if daemon and getattr(daemon.config, "source_path", None):
                dev_root = str(Path(daemon.config.source_path).parent)
        else:
            dev_root = dev_val

    if dev_root:
        src_web = Path(dev_root) / "src" / "swarm" / "web"
        src_templates = src_web / "templates"
        src_static = src_web / "static"
        if src_templates.is_dir():
            templates_dir = src_templates
            _log.info("dev mode: serving templates from %s", templates_dir)
        if src_static.is_dir():
            static_dir = src_static
            _log.info("dev mode: serving static from %s", static_dir)

    return templates_dir, static_dir


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def setup_web_routes(app: web.Application) -> None:
    """Add web dashboard routes to an aiohttp app."""
    import os

    from swarm.web.routes import register_all

    templates_dir, static_dir = _resolve_web_dirs(app)
    app["static_dir"] = static_dir

    env = aiohttp_jinja2.setup(
        app,
        loader=jinja2.FileSystemLoader(str(templates_dir)),
        autoescape=jinja2.select_autoescape(["html"]),
    )
    env.filters["basename"] = lambda p: os.path.basename(p)

    register_all(app)

    app.router.add_static("/static", static_dir)


# ---------------------------------------------------------------------------
# Backward-compatible re-exports — tests and other modules import handlers
# from ``swarm.web.app``.  These imports ensure existing call sites continue
# to work without modification.
# ---------------------------------------------------------------------------

from swarm.web.routes.auth import (  # noqa: E402, F401
    handle_graph_callback,
    handle_graph_disconnect,
    handle_graph_login,
    handle_graph_status,
    handle_jira_auth_status,
    handle_jira_callback,
    handle_jira_disconnect,
    handle_jira_login,
)
from swarm.web.routes.pages import (  # noqa: E402, F401
    handle_config_page,
    handle_dashboard,
)
from swarm.web.routes.partials import (  # noqa: E402, F401
    handle_partial_launch_config,
    handle_partial_logs,
    handle_partial_status,
    handle_partial_system_log,
    handle_partial_task_history,
    handle_partial_tasks,
    handle_partial_workers,
)
from swarm.web.routes.proposals import (  # noqa: E402, F401
    handle_action_add_approval_rule,
    handle_action_approve_always,
    handle_action_approve_proposal,
    handle_action_reject_all_proposals,
    handle_action_reject_proposal,
)
from swarm.web.routes.pwa import (  # noqa: E402, F401
    handle_bee_icon,
    handle_manifest,
    handle_offline_page,
    handle_service_worker,
)
from swarm.web.routes.system import (  # noqa: E402, F401
    handle_action_check_update,
    handle_action_clear_logs,
    handle_action_install_update,
    handle_action_stop_server,
    handle_action_tunnel_start,
    handle_action_tunnel_stop,
    handle_action_update_and_restart,
)
from swarm.web.routes.tasks import (  # noqa: E402, F401
    handle_action_assign_task,
    handle_action_complete_task,
    handle_action_create_task,
    handle_action_edit_task,
    handle_action_fail_task,
    handle_action_fetch_image,
    handle_action_fetch_outlook_email,
    handle_action_remove_task,
    handle_action_reopen_task,
    handle_action_retry_draft,
    handle_action_unassign_task,
    handle_action_upload,
    handle_action_upload_attachment,
)
from swarm.web.routes.workers import (  # noqa: E402, F401
    handle_action_continue,
    handle_action_continue_all,
    handle_action_escape,
    handle_action_interrupt,
    handle_action_kill,
    handle_action_kill_session,
    handle_action_launch,
    handle_action_redraw,
    handle_action_revive,
    handle_action_send,
    handle_action_send_all,
    handle_action_send_group,
    handle_action_spawn,
    handle_action_toggle_drones,
)
