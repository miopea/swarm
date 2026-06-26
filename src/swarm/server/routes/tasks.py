"""Task routes — CRUD, assignment, attachments, history."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp import web

from swarm.server.helpers import (
    get_daemon,
    handle_errors,
    json_error,
    parse_limit,
    parse_offset,
    read_file_field,
)
from swarm.tasks.cross_task import validate_cross_task
from swarm.tasks.task import (
    SwarmTask,
    TaskPriority,
    TaskType,
    validate_priority,
    validate_task_type,
)


def register(app: web.Application) -> None:
    app.router.add_get("/api/tasks", handle_tasks)
    app.router.add_get("/api/tasks/export", handle_export_tasks)
    app.router.add_post("/api/tasks", handle_create_task)
    app.router.add_post("/api/tasks/from-email", handle_create_task_from_email)
    app.router.add_post("/api/tasks/cross", handle_create_cross_task)
    app.router.add_post("/api/tasks/bulk", handle_bulk_task_action)
    app.router.add_post("/api/tasks/{task_id}/assign", handle_assign_task)
    app.router.add_post("/api/tasks/{task_id}/start", handle_start_task)
    app.router.add_post("/api/tasks/{task_id}/complete", handle_complete_task)
    app.router.add_post("/api/tasks/{task_id}/force-complete", handle_force_complete_task)
    app.router.add_post("/api/tasks/{task_id}/fail", handle_fail_task)
    app.router.add_post("/api/tasks/{task_id}/unassign", handle_unassign_task)
    app.router.add_post("/api/tasks/{task_id}/reopen", handle_reopen_task)
    app.router.add_post("/api/tasks/{task_id}/approve", handle_approve_task)
    app.router.add_post("/api/tasks/{task_id}/reject", handle_reject_task)
    app.router.add_delete("/api/tasks/{task_id}", handle_remove_task)
    app.router.add_patch("/api/tasks/{task_id}", handle_edit_task)
    app.router.add_post("/api/tasks/{task_id}/attachments", handle_upload_attachment)
    app.router.add_post("/api/tasks/{task_id}/retry-draft", handle_retry_draft)
    app.router.add_get("/api/tasks/history", handle_search_task_history)
    app.router.add_get("/api/tasks/{task_id}/history", handle_task_history)
    # GET by id — cleanup batch follow-up. Must be registered AFTER the
    # static-path GETs (/export, /history) because aiohttp dispatches in
    # registration order and `{task_id}` would otherwise eat them.
    app.router.add_get("/api/tasks/{task_id}", handle_get_task)


def _validate_priority(raw: str) -> TaskPriority:
    # Lets ValueError propagate — handle_errors maps it to 400.  Pre-Phase-C
    # this re-raised as SwarmOperationError, which mapped to 400 then but
    # would now map to 409 (Conflict).  Conflict is wrong semantics for an
    # invalid priority value sent in the request body — that's a 400.
    return validate_priority(raw)


def _validate_task_type(raw: str) -> TaskType:
    # See ``_validate_priority`` — ValueError → 400 is the right mapping
    # for an invalid type value in the request body.
    return validate_task_type(raw)


def _validate_edit_body(body: dict[str, Any]) -> web.Response | None:
    """Return an error Response if edit body fields are invalid, else None."""
    if "title" in body:
        raw_title = body["title"]
        if isinstance(raw_title, str) and len(raw_title) > 500:
            return json_error("Task title too long (max 500 characters)")
    if "description" in body:
        desc = body["description"]
        if isinstance(desc, str) and len(desc) > 10_000:
            return json_error("Task description too long (max 10000 characters)")
    if "attachments" in body:
        uploads_dir = (Path.home() / ".swarm" / "uploads").resolve()
        for att in body["attachments"]:
            att_path = Path(att).resolve()
            if not att_path.is_relative_to(uploads_dir):
                return json_error("attachment path outside uploads directory", 400)
    return None


def _task_dict(t: SwarmTask) -> dict[str, object]:
    return {
        "id": t.id,
        "title": t.title,
        "description": t.description,
        "status": t.status.value,
        "priority": t.priority.value,
        "task_type": t.task_type.value,
        "assigned_worker": t.assigned_worker,
    }


def _task_full_dict(t: SwarmTask) -> dict[str, object]:
    """Full task dict for the editor — every field the modal reads.

    Cleanup batch follow-up to the P1-P6 series: the dashboard's task
    editor takes a dict with 17 known keys (see `showEditTask`). The
    list-view `_task_dict` only carries the 7 columns the table needs,
    so a deep-link by ID couldn't open the editor — hence this richer
    serializer. Field names match the editor's expected shape exactly
    so the client doesn't have to re-map.
    """
    return {
        "id": t.id,
        "number": t.number,
        "title": t.title,
        "description": t.description,
        "status": t.status.value,
        "priority": t.priority.value,
        "task_type": t.task_type.value,
        "assigned_worker": t.assigned_worker or "",
        "tags": list(t.tags),
        "depends_on": list(t.depends_on),
        "attachments": list(t.attachments),
        "resolution": t.resolution,
        "block_reason": t.block_reason,
        "external_blocker_ref": t.external_blocker_ref,
        "is_cross_project": t.is_cross_project,
        "source_worker": t.source_worker,
        "target_worker": t.target_worker,
        "dependency_type": t.dependency_type,
        "acceptance_criteria": list(t.acceptance_criteria),
        "context_refs": list(t.context_refs),
        "jira_key": t.jira_key,
        "source_email_id": t.source_email_id,
        "verification_status": t.verification_status.value,
        "verification_reason": t.verification_reason,
    }


@handle_errors
async def handle_get_task(request: web.Request) -> web.Response:
    """Return a single task by id (cleanup batch follow-up).

    Backs the dashboard's `showTaskEditorById(id)` so deep-links (the
    P3 pipeline-step task chip, future notifications, queen relays)
    can open the editor without already having the full task data in
    hand. 404 if the id doesn't match any task.
    """
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    task = d.task_board.get(task_id)
    if task is None:
        return json_error(f"task {task_id!r} not found", 404)
    return web.json_response(_task_full_dict(task))


@handle_errors
async def handle_tasks(request: web.Request) -> web.Response:
    d = get_daemon(request)
    limit = parse_limit(request)
    offset = parse_offset(request)

    tasks, total = d.task_board.query(
        status=request.query.get("status"),
        priority=request.query.get("priority"),
        task_type=request.query.get("task_type"),
        worker=request.query.get("worker"),
        search=request.query.get("search"),
        sort=request.query.get("sort", "priority"),
        desc=request.query.get("desc", "true").lower() != "false",
        limit=limit,
        offset=offset,
    )
    return web.json_response(
        {
            "tasks": [_task_dict(t) for t in tasks],
            "total": total,
            "limit": limit,
            "offset": offset,
            "summary": d.task_board.summary(),
        }
    )


@handle_errors
async def handle_export_tasks(request: web.Request) -> web.Response:
    """Export tasks as CSV or JSON."""
    import csv
    import io

    d = get_daemon(request)
    fmt = request.query.get("format", "csv")
    tasks, _ = d.task_board.query(
        status=request.query.get("status"),
        priority=request.query.get("priority"),
        task_type=request.query.get("task_type"),
        worker=request.query.get("worker"),
        search=request.query.get("search"),
        sort=request.query.get("sort", "priority"),
        desc=request.query.get("desc", "true").lower() != "false",
        limit=10_000,
        offset=0,
    )
    rows = []
    for t in tasks:
        rows.append(
            {
                "id": t.id,
                "number": t.number,
                "title": t.title,
                "status": t.status.value,
                "priority": t.priority.value,
                "type": t.task_type.value,
                "assigned_worker": t.assigned_worker or "",
                "created_at": t.created_at,
                "completed_at": t.completed_at or "",
                "resolution": t.resolution or "",
            }
        )

    if fmt == "json":
        return web.json_response(
            rows,
            headers={
                "Content-Disposition": "attachment; filename=tasks.json",
            },
        )

    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    return web.Response(
        text=buf.getvalue(),
        content_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=tasks.csv",
        },
    )


@handle_errors
async def handle_bulk_task_action(request: web.Request) -> web.Response:
    d = get_daemon(request)
    body = await request.json()
    action = body.get("action", "")
    task_ids = body.get("task_ids", [])

    valid_actions = ("complete", "fail", "reopen", "remove", "assign")
    if action not in valid_actions:
        return json_error(f"Invalid bulk action: {action!r}")
    if not isinstance(task_ids, list):
        return json_error("task_ids must be a list")

    worker = body.get("worker", "")
    if action == "assign" and not worker:
        return json_error("worker required for assign action")

    dispatch: dict[str, object] = {
        "complete": lambda tid: d.complete_task(tid, actor="user"),
        "fail": lambda tid: d.fail_task(tid, actor="user"),
        "reopen": lambda tid: d.reopen_task(tid, actor="user"),
        "remove": lambda tid: d.remove_task(tid, actor="user"),
        "assign": lambda tid: d.assign_task(tid, worker, actor="user"),
    }
    fn = dispatch[action]
    succeeded = 0
    errors: list[dict[str, str]] = []
    for tid in task_ids:
        try:
            fn(tid)
            succeeded += 1
        except Exception as exc:
            errors.append({"id": tid, "error": str(exc)})

    return web.json_response(
        {
            "status": "ok",
            "succeeded": succeeded,
            "failed": len(errors),
            "errors": errors,
        }
    )


@handle_errors
async def handle_create_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    body = await request.json()
    title = body.get("title", "")
    if not isinstance(title, str):
        title = ""
    title = title.strip()
    if len(title) > 500:
        return json_error("Task title too long (max 500 characters)")
    description = body.get("description", "")
    if isinstance(description, str) and len(description) > 10_000:
        return json_error("Task description too long (max 10000 characters)")

    priority = _validate_priority(body.get("priority", "normal"))

    type_str = body.get("task_type", "")
    task_type = None
    if type_str:
        task_type = _validate_task_type(type_str)

    task = await d.create_task_smart(
        title=title,
        description=description,
        priority=priority,
        task_type=task_type,
    )
    # Apply optional cost budget
    cost_budget = body.get("cost_budget", 0)
    if isinstance(cost_budget, (int, float)) and cost_budget > 0:
        task.cost_budget = float(cost_budget)
    return web.json_response({"id": task.id, "title": task.title}, status=201)


@handle_errors
async def handle_create_task_from_email(request: web.Request) -> web.Response:
    """Parse a .eml file and return extracted data for the create-task modal."""
    d = get_daemon(request)
    try:
        filename, data = await read_file_field(request)
    except ValueError as e:
        return json_error(str(e))

    from swarm.tasks.task import parse_email, smart_title

    parsed = parse_email(data, filename=filename)
    subject = parsed.get("subject", "")
    body = parsed.get("body", "")

    title = subject.strip()
    if not title and body:
        title = await smart_title(body)

    attachment_paths: list[str] = []
    for att in parsed.get("attachments", []):
        path = d.save_attachment(att["filename"], att["data"])
        attachment_paths.append(path)

    return web.json_response(
        {
            "title": title or "",
            "description": body or "",
            "attachments": attachment_paths,
            "message_id": parsed.get("message_id", ""),
        }
    )


@handle_errors
async def handle_assign_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    body = await request.json()
    worker_name = body.get("worker", "")
    auto_start = body.get("auto_start", True)
    if not worker_name:
        return json_error("worker required")

    await d.assign_task(task_id, worker_name)

    started = False
    if auto_start:
        from swarm.worker.worker import WorkerState

        worker = d.get_worker(worker_name)
        if worker and worker.state == WorkerState.RESTING:
            try:
                started = await d.start_task(task_id, actor="user")
            except Exception:
                pass  # Task assigned but start failed — still queued

    status = "started" if started else "assigned"
    return web.json_response({"status": status, "task_id": task_id, "worker": worker_name})


@handle_errors
async def handle_start_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    result = await d.start_task(task_id, actor="user")
    return web.json_response({"status": "started" if result else "failed", "task_id": task_id})


@handle_errors
async def handle_complete_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    body = await request.json() if request.can_read_body else {}
    resolution = body.get("resolution", "") if body else ""
    d.complete_task(task_id, resolution=resolution)
    return web.json_response({"status": "done", "task_id": task_id})


@handle_errors
async def handle_force_complete_task(request: web.Request) -> web.Response:
    """Force-complete a wedged task (operator override).

    Clears any blocker rows pinning the task and completes it from ANY
    non-terminal status, including BLOCKED — the clean path out of a
    self-block / blocker-cycle deadlock that the normal /complete endpoint
    refuses (it requires ASSIGNED/ACTIVE). Returns 400 if the task is
    missing or already terminal.
    """
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    body = await request.json() if request.can_read_body else {}
    resolution = body.get("resolution", "") if body else ""
    ok = d.complete_task(task_id, actor="user", resolution=resolution, force=True)
    if not ok:
        return json_error(f"Could not force-complete task {task_id} (missing or already terminal)")
    return web.json_response({"status": "done", "task_id": task_id, "forced": True})


@handle_errors
async def handle_fail_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    d.fail_task(task_id)
    return web.json_response({"status": "failed", "task_id": task_id})


@handle_errors
async def handle_unassign_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    d.unassign_task(task_id)
    return web.json_response({"status": "unassigned", "task_id": task_id})


@handle_errors
async def handle_reopen_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    d.reopen_task(task_id)
    return web.json_response({"status": "reopened", "task_id": task_id})


@handle_errors
async def handle_remove_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    d.remove_task(task_id)
    return web.json_response({"status": "removed", "task_id": task_id})


_EDIT_PASSTHROUGH_FIELDS = (
    "description",
    "tags",
    "attachments",
    "source_worker",
    "target_worker",
    "dependency_type",
    "acceptance_criteria",
    "context_refs",
)


def _extract_edit_kwargs(body: dict[str, Any]) -> dict[str, Any]:
    """Build kwargs dict from edit request body."""
    kwargs: dict[str, Any] = {}
    if "priority" in body:
        kwargs["priority"] = _validate_priority(body["priority"])
    if "task_type" in body:
        kwargs["task_type"] = _validate_task_type(body["task_type"])
    for field in _EDIT_PASSTHROUGH_FIELDS:
        if field in body:
            kwargs[field] = body[field]
    return kwargs


@handle_errors
async def handle_edit_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    body = await request.json()

    err = _validate_edit_body(body)
    if err is not None:
        return err

    kwargs = _extract_edit_kwargs(body)
    if "title" in body:
        title = await d.tasks.resolve_title(body["title"], body.get("description", ""), task_id)
        if not title:
            return json_error("title or description required to generate title")
        kwargs["title"] = title

    d.edit_task(task_id, **kwargs)
    return web.json_response({"status": "updated", "task_id": task_id})


@handle_errors
async def handle_upload_attachment(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]

    task = d.task_board.get(task_id)
    if not task:
        return json_error(f"Task '{task_id}' not found", 404)

    filename, data = await read_file_field(request)
    path = d.save_attachment(filename, data)

    new_attachments = [*task.attachments, path]
    d.task_board.update(task_id, attachments=new_attachments)

    return web.json_response({"status": "uploaded", "path": path}, status=201)


@handle_errors
async def handle_search_task_history(request: web.Request) -> web.Response:
    """Search across all task history entries."""
    d = get_daemon(request)
    limit = parse_limit(request)
    offset = parse_offset(request)
    query = request.query.get("search", "")
    action = request.query.get("action", "")
    actor = request.query.get("actor", "")
    since = float(request.query.get("since", "0") or "0")
    until = float(request.query.get("until", "0") or "0")
    events, total = d.task_history.search(
        query=query,
        action=action,
        actor=actor,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )
    return web.json_response(
        {
            "events": [e.to_dict() for e in events],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@handle_errors
async def handle_task_history(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    limit = parse_limit(request)
    events = d.task_history.get_events(task_id, limit=limit)
    return web.json_response(
        {
            "events": [e.to_dict() for e in events],
        }
    )


@handle_errors
async def handle_retry_draft(request: web.Request) -> web.Response:
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    await d.retry_draft_reply(task_id)
    return web.json_response({"status": "retrying", "task_id": task_id})


@handle_errors
async def handle_create_cross_task(request: web.Request) -> web.Response:
    """Create a cross-project task from JSON payload."""
    d = get_daemon(request)
    body = await request.json()
    err = validate_cross_task(body)
    if err:
        return json_error(err)

    from swarm.tasks.task import PRIORITY_MAP, TYPE_MAP

    priority = PRIORITY_MAP.get(body.get("priority", "normal"), TaskPriority.NORMAL)
    type_str = body.get("task_type", "")
    task_type = TYPE_MAP.get(type_str, TaskType.CHORE) if type_str else TaskType.CHORE

    task = d.create_cross_task(
        title=body["title"],
        description=body.get("description", ""),
        source_worker=body["source_worker"],
        target_worker=body["target_worker"],
        dependency_type=body.get("dependency_type", "blocks"),
        priority=priority,
        task_type=task_type,
        acceptance_criteria=body.get("acceptance_criteria"),
        context_refs=body.get("context_refs"),
    )
    return web.json_response({"id": task.id, "title": task.title}, status=201)


@handle_errors
async def handle_approve_task(request: web.Request) -> web.Response:
    """Approve a PROPOSED cross-project task."""
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    d.approve_cross_task(task_id)
    return web.json_response({"status": "approved", "task_id": task_id})


@handle_errors
async def handle_reject_task(request: web.Request) -> web.Response:
    """Reject a PROPOSED cross-project task."""
    d = get_daemon(request)
    task_id = request.match_info["task_id"]
    d.reject_cross_task(task_id)
    return web.json_response({"status": "rejected", "task_id": task_id})
