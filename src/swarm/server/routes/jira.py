"""Jira integration routes — status, sync, preview, create."""

from __future__ import annotations

from aiohttp import web

from swarm.logging import get_logger
from swarm.server.helpers import get_daemon, handle_errors, json_error

_log = get_logger("server.routes.jira")


def register(app: web.Application) -> None:
    app.router.add_get("/api/jira/status", handle_jira_status)
    app.router.add_post("/api/jira/sync", handle_jira_sync)
    app.router.add_post("/api/jira/import-by-key", handle_jira_import_by_key)
    app.router.add_get("/api/jira/preview", handle_jira_preview)
    # v2 phase 3 — setup flow. discover proposes, plan shows what a sweep WOULD do,
    # confirm is the explicit go-ahead. Enabling the integration must not be a bulk
    # write to a shared tracker.
    app.router.add_get("/api/jira/discover", handle_jira_discover)
    app.router.add_get("/api/jira/plan", handle_jira_plan)
    app.router.add_post("/api/jira/confirm", handle_jira_confirm)
    app.router.add_get("/api/jira/mappings", handle_jira_mappings)
    app.router.add_post("/api/tasks/{task_id}/jira", handle_jira_create)
    app.router.add_post("/api/tasks/{task_id}/jira/refresh", handle_jira_refresh)


# The message a user actually hits after a SUCCESSFUL OAuth connection, so it has to say
# where to look. "Jira integration not enabled" alone reads as "your connection failed"
# when the connection is fine and a separate checkbox is off.
_NOT_ENABLED = (
    "Jira integration is switched off. OAuth may be connected, but the integration's "
    "'enabled' setting is off — tick it in Settings → Integrations → Jira and save, "
    "then retry."
)


@handle_errors
async def handle_jira_status(request: web.Request) -> web.Response:
    """Return Jira sync service status."""
    d = get_daemon(request)
    jira = getattr(d, "jira", None)
    if jira is None:
        return web.json_response({"enabled": False})
    return web.json_response(jira.get_status())


@handle_errors
async def handle_jira_sync(request: web.Request) -> web.Response:
    """Trigger a manual Jira import sync."""
    d = get_daemon(request)
    jira = getattr(d, "jira", None)
    if jira is None or not jira.enabled:
        return json_error(_NOT_ENABLED, status=400)
    count = await d.jira_svc.run_import()
    return web.json_response({"imported": count})


@handle_errors
async def handle_jira_import_by_key(request: web.Request) -> web.Response:
    """Import a single Jira issue by key. Used by drag-drop in the dashboard."""
    import re as _re

    d = get_daemon(request)
    jira = getattr(d, "jira", None)
    if jira is None or not jira.enabled:
        return json_error(_NOT_ENABLED, status=400)

    data = await request.post()
    raw = (data.get("key") or "").strip()
    if not raw:
        return json_error("key required (e.g. PROJ-123 or full URL)")

    # Accept full URLs (https://foo.atlassian.net/browse/PROJ-123) or bare keys.
    match = _re.search(r"([A-Z][A-Z0-9_]+-\d+)", raw.upper())
    if not match:
        return json_error(f"could not parse Jira issue key from '{raw}'")
    issue_key = match.group(1)

    result = await d.jira_svc.import_one(issue_key)
    if not result:
        return json_error(f"Failed to import {issue_key}", status=502)
    return web.json_response(result)


@handle_errors
async def handle_jira_preview(request: web.Request) -> web.Response:
    """Preview what a Jira sync would import (dry run — no tasks created)."""
    d = get_daemon(request)
    jira = getattr(d, "jira", None)
    if jira is None:
        return json_error("Jira integration not configured", status=400)
    if not jira.enabled:
        connected = d.jira_mgr.is_connected() if d.jira_mgr else False
        return json_error(
            f"Jira not enabled (enabled={d.config.jira.enabled}, oauth_connected={connected})",
            status=400,
        )
    jql = jira.build_jql()
    existing = {t.id: t for t in d.task_board.all_tasks}
    prev_errors = jira.stats.errors
    new_tasks = await jira.import_issues(existing)
    preview = [
        {
            "jira_key": t.jira_key,
            "title": t.title,
            "type": t.task_type.value,
            "priority": t.priority.value,
        }
        for t in new_tasks
    ]
    result: dict[str, object] = {"count": len(preview), "tasks": preview, "jql": jql}
    if jira.stats.errors > prev_errors:
        result["error"] = jira.stats.last_error
        _log.warning("Jira preview error: %s (jql=%s)", jira.stats.last_error, jql)
    return web.json_response(result)


@handle_errors
async def handle_jira_refresh(request: web.Request) -> web.Response:
    """Pull the latest description, comments, and attachments from Jira."""
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    jira = getattr(d, "jira", None)
    if jira is None or not jira.enabled:
        return json_error(_NOT_ENABLED, status=400)
    task = d.task_board.get(task_id)
    if not task:
        return json_error("Task not found", status=404)
    if not task.jira_key:
        return json_error("Task is not linked to a Jira issue", status=400)
    ok = await d.jira_svc.refresh_task(task_id)
    if not ok:
        return json_error("Failed to refresh task from Jira", status=502)
    refreshed = d.task_board.get(task_id)
    return web.json_response(
        {
            "task_id": task_id,
            "jira_key": task.jira_key,
            "attachments": list(refreshed.attachments) if refreshed else [],
            "description_length": len(refreshed.description) if refreshed else 0,
        }
    )


@handle_errors
async def handle_jira_create(request: web.Request) -> web.Response:
    """Create a Jira issue from an existing Swarm task."""
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    jira = getattr(d, "jira", None)
    if jira is None or not jira.enabled:
        return json_error(_NOT_ENABLED, status=400)
    task = d.task_board.get(task_id)
    if not task:
        return json_error("Task not found", status=404)
    if task.jira_key:
        return json_error(f"Task already linked to {task.jira_key}", status=409)
    try:
        jira_key = await jira.create_jira_issue(task)
    except Exception as exc:
        return json_error(f"Failed to create Jira issue: {exc}", status=502)
    if jira_key:
        d.task_board.set_jira_key(task_id, jira_key)
        from swarm.tasks.history import TaskAction

        detail = f"linked to {jira_key}"
        d.task_history.append(task_id, TaskAction.EDITED, actor="user", detail=detail)

        # GAP A (#1354). Synthesis runs at CREATION and again on ASSIGN — but the
        # assign-time hook is scoped to tasks that already carry a jira_key, and in this
        # create-then-link flow the assignment happens BEFORE the link exists. Measured
        # on #1352: CREATED/ASSIGNED at 17:03:19 with no key, creation-time synthesis
        # returned nothing at 17:03:26 (an EDITED row with an empty detail), the link
        # landed at 17:03:31, and nothing tried again. The task reached a linked,
        # assignable state with no criteria — and the verifier default-passes those.
        #
        # THIS is the moment to retry rather than widening the assign hook: linking is
        # when the Jira context actually arrives. Re-running at assign time would feed
        # synthesis the same pre-Jira description that already came back empty.
        #
        # apply_synthesized_criteria is gated on config, skips a task that already has
        # criteria, and never raises — so the guard against double work lives in the
        # callee rather than being restated at every call site. Awaited, not fired and
        # forgotten: the caller is already async and the criteria should exist before the
        # response says the link succeeded.
        task = d.task_board.get(task_id)
        if task is not None and not task.acceptance_criteria:
            await d.tasks.apply_synthesized_criteria(task, actor="user")
    return web.json_response({"jira_key": jira_key, "task_id": task_id})


@handle_errors
async def handle_jira_discover(request: web.Request) -> web.Response:
    """Read a project's real workflow and PROPOSE a status map. Writes nothing.

    The setup screen's data source. Returns the project's status vocabulary, the
    proposed mapping, its terminal statuses, and — named explicitly — any Swarm status
    the project offers no plausible target for. That last list is the case that failed
    silently before: a hardcoded "Done" was refused by 11 real tickets whose workflow
    only offered "Waiting for support", and nothing surfaced it until the export failed.
    """
    d = get_daemon(request)
    project = request.query.get("project", "").strip()
    if not project:
        return json_error("project is required", status=400)
    jira = getattr(d, "jira", None)
    if jira is None or not jira.enabled:
        return json_error(_NOT_ENABLED, status=400)
    return web.json_response(await jira.discover_workflow(project))


@handle_errors
async def handle_jira_plan(request: web.Request) -> web.Response:
    """The DRY RUN: what a reconcile sweep would change, without changing it.

    On 2026-08-07 a schema change made 25 tasks look unacknowledged and the reconciler
    transitioned 14 real tickets before anyone had looked. This is what should have
    been shown instead.
    """
    d = get_daemon(request)
    svc = getattr(d, "jira_svc", None)
    if svc is None:
        return json_error(_NOT_ENABLED, status=400)
    plan = svc.plan_exports()
    return web.json_response(
        {
            "count": len(plan),
            "unconfirmed": sum(1 for p in plan if not p.get("project_confirmed")),
            "changes": plan,
        }
    )


@handle_errors
async def handle_jira_confirm(request: web.Request) -> web.Response:
    """Confirm a project's status map — the explicit go-ahead that lets the sweep write.

    Takes the mapping the operator actually approved rather than re-deriving it, so
    what was on screen is what gets stored. Confirming is a separate act from
    discovering on purpose: Done / Resolved / Closed are rarely interchangeable and a
    wrong automatic choice transitions real tickets while reporting success.
    """
    d = get_daemon(request)
    body = await request.json()
    project = str(body.get("project", "")).strip()
    if not project:
        return json_error("project is required", status=400)
    status_map = body.get("status_map")
    if not isinstance(status_map, dict) or not status_map:
        return json_error("status_map is required and must be non-empty", status=400)

    cfg = getattr(getattr(d, "jira", None), "_config", None)
    if cfg is None:
        return json_error(_NOT_ENABLED, status=400)

    cfg.project_status_maps[project] = {str(k): str(v) for k, v in status_map.items()}
    if project not in cfg.confirmed_projects:
        cfg.confirmed_projects.append(project)
    mgr = getattr(d, "config_mgr", None)
    if mgr is not None and hasattr(mgr, "save"):
        mgr.save()
    _log.warning(
        "jira: workflow confirmed for project %s (%d mappings) — the reconcile sweep "
        "may now converge this project",
        project,
        len(cfg.project_status_maps[project]),
    )
    return web.json_response(
        {
            "project": project,
            "confirmed": True,
            "status_map": cfg.project_status_maps[project],
        }
    )


# The Swarm statuses a project map is expected to cover. An absent entry is not
# cosmetic: export_status looks up the target name here and refuses the transition when
# it is missing, so "not mapped" means "this state will never reach Jira".
_MAPPED_STATUSES: tuple[str, ...] = (
    "backlog",
    "unassigned",
    "assigned",
    "active",
    "blocked",
    "done",
    "failed",
)


@handle_errors
async def handle_jira_mappings(request: web.Request) -> web.Response:
    """Return the saved workflow mappings — the READ side of the setup screen.

    Exists because the mappings panel was server-rendered once at page load, so
    confirming a workflow updated the config and the operator still saw the old table
    until they refreshed. Rather than pushing an update after confirm, the panel now
    re-reads this: a view that can re-derive its state from the authority recovers from
    any missed update, including ones nobody predicted. Same reasoning as the task
    board's reconciler.

    Reports one row per CONFIGURED project, not per mapped project. A project listed in
    ``projects`` with no map is exactly the case the operator cannot currently see — it
    imports issues and silently exports nothing.
    """
    d = get_daemon(request)
    cfg = getattr(getattr(d, "jira", None), "_config", None)
    if cfg is None:
        return web.json_response({"enabled": False, "rows": []})

    maps: dict[str, dict[str, str]] = dict(getattr(cfg, "project_status_maps", {}) or {})
    confirmed = list(getattr(cfg, "confirmed_projects", []) or [])
    projects = list(getattr(cfg, "projects", None) or [])
    legacy = str(getattr(cfg, "project", "") or "").strip()
    if legacy and legacy not in projects:
        projects.append(legacy)
    # A project can hold a saved map without being in `projects` — it was removed from
    # the sync scope after being mapped. Showing it is honest: the map is still stored
    # and would apply again the moment the key is re-added.
    for key in maps:
        if key not in projects:
            projects.append(key)

    # ...and a project can have LINKED TASKS while appearing in neither list. MTR-11806
    # was exactly that: a real task linked to a real ticket in a project that is not in
    # the sync scope, has no map, and was therefore invisible on this screen while the
    # reconciler warned about it every five minutes. A project the board is already
    # entangled with is precisely what the operator needs to see.
    d_board = getattr(d, "task_board", None)
    for task in getattr(d_board, "all_tasks", []) or []:
        key = str(getattr(task, "jira_key", "") or "")
        prefix = key.split("-")[0] if "-" in key else ""
        if prefix and prefix not in projects:
            projects.append(prefix)

    linked: dict[str, int] = {}
    for task in getattr(d_board, "all_tasks", []) or []:
        key = str(getattr(task, "jira_key", "") or "")
        prefix = key.split("-")[0] if "-" in key else ""
        if prefix:
            linked[prefix] = linked.get(prefix, 0) + 1

    rows = [
        {
            "project": key,
            "confirmed": key in confirmed,
            "in_scope": key in (getattr(cfg, "projects", None) or []) or key == legacy,
            "status_map": maps.get(key, {}),
            "unmapped": [s for s in _MAPPED_STATUSES if s not in maps.get(key, {})],
            "linked_tasks": linked.get(key, 0),
        }
        for key in projects
    ]
    return web.json_response({"enabled": bool(getattr(cfg, "enabled", False)), "rows": rows})
