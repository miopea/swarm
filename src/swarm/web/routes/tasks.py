"""Task action routes: create, assign, complete, remove, fail, reopen, etc."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web

from swarm.logging import get_logger
from swarm.server.daemon import console_log
from swarm.server.helpers import get_daemon, handle_errors, json_error
from swarm.tasks.task import (
    PRIORITY_MAP,
    TYPE_MAP,
    TaskPriority,
    TaskType,
    smart_title,
)

_log = get_logger("server.routes.tasks")

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


def _apply_status_change(d: SwarmDaemon, task_id: str, current: str, target: str) -> None:
    """Dispatch a status transition to the appropriate daemon lifecycle method."""
    if target == "unassigned" and current in ("assigned", "active"):
        d.unassign_task(task_id)
    elif target == "unassigned" and current == "backlog":
        # Backlog → Unassigned is the "promote / Hand to Queen" transition.
        # Route through the guarded board method (#611 P5) — it enforces the
        # BACKLOG precondition and persists + notifies — instead of a raw
        # task.approve() + manual persist.
        d.task_board.approve_task(task_id)
    elif target == "done" and current in ("assigned", "active"):
        d.complete_task(task_id)
    elif target == "failed" and current == "active":
        d.fail_task(task_id)
    elif target in ("backlog", "unassigned", "assigned") and current in ("done", "failed"):
        d.reopen_task(task_id)


if TYPE_CHECKING:
    from types import ModuleType

    from swarm.server.daemon import SwarmDaemon

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

# Statuses an operator may author a task directly INTO via the create modal.
# ACTIVE is excluded — it must go through the activate() chokepoint (INV-1);
# BLOCKED is excluded — it's set only by the blocker / operator-park flow;
# ASSIGNED is handled by the worker-assign branch (it needs a worker). Authoring
# straight into any of those raw would bypass the guards. (#611 P5)
_CREATABLE_STATUSES = frozenset({"backlog", "unassigned", "done", "failed"})


@handle_errors
async def handle_action_create_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()

    priority = PRIORITY_MAP.get(data.get("priority", "normal"), TaskPriority.NORMAL)
    type_str = data.get("task_type", "").strip()
    task_type = TYPE_MAP.get(type_str) if type_str else None

    deps_raw = data.get("depends_on", "").strip()
    depends_on = [x.strip() for x in deps_raw.split(",") if x.strip()] if deps_raw else None

    att_raw = data.get("attachments", "").strip()
    attachments = [a.strip() for a in att_raw.split(",") if a.strip()] if att_raw else None

    task = await d.create_task_smart(
        title=data.get("title", "").strip(),
        description=data.get("description", ""),
        priority=priority,
        task_type=task_type,
        depends_on=depends_on,
        attachments=attachments,
        source_email_id=data.get("source_email_id", "").strip(),
    )

    # Apply explicit status + worker from the create modal so the new
    # smart-default flow ends up in the right lane (Backlog by default;
    # Assigned + dispatched if the operator picked a worker). The
    # top-level "Assign to" field submits as ``worker``; the
    # cross-project Advanced section's ``target_worker`` is the legacy
    # fallback so we honour both.
    requested_status = (data.get("status", "") or "").strip()
    chosen_worker = (data.get("worker", "") or "").strip() or (
        data.get("target_worker", "") or ""
    ).strip()
    if requested_status == "assigned" and chosen_worker:
        await d.assign_task(task.id, chosen_worker)
    elif requested_status and requested_status != task.status.value:
        # Direct lane authoring — Backlog/Unassigned creation, or the rare case
        # of recording historical work straight into Done/Failed. ACTIVE/BLOCKED/
        # ASSIGNED are refused here (#611 P5): ACTIVE must go through activate()
        # (INV-1), BLOCKED via the blocker flow, ASSIGNED via the worker-assign
        # branch above. The board notifies subscribers on persist.
        from swarm.tasks.task import TaskStatus

        if requested_status not in _CREATABLE_STATUSES:
            _log.warning(
                "create_task: refusing to author status %r; left as %s",
                requested_status,
                task.status.value,
            )
        else:
            try:
                task.status = TaskStatus(requested_status)
                d.task_board.persist(task)
            except ValueError:
                _log.warning("create_task: ignoring unknown status %r", requested_status)

    console_log(f'Task created: "{task.title}" ({task.priority.value}, {task.task_type.value})')
    return web.json_response({"id": task.id, "title": task.title}, status=201)


@handle_errors
async def handle_action_assign_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    worker_name = data.get("worker", "")
    auto_start = data.get("auto_start", "true") != "false"
    if not task_id or not worker_name:
        return json_error("task_id and worker required")

    # Operator assignment must work regardless of the task's current
    # lane. d.assign_task's is_available gate only accepts UNASSIGNED —
    # that gate exists to stop the auto-assign DRONE poaching in-flight
    # work, not to block an explicit operator assign. Getting to
    # UNASSIGNED needs a DIFFERENT primitive per source status, because
    # board.unassign() itself only accepts ASSIGNED/ACTIVE and silently
    # no-ops on BACKLOG — which is why assigning a backlog task 409'd
    # even with the earlier unassign-first attempt.
    from swarm.tasks.task import TaskStatus

    existing = d.task_board.get(task_id)
    if existing:
        if existing.status in (TaskStatus.ASSIGNED, TaskStatus.ACTIVE):
            d.task_board.unassign(task_id)  # → UNASSIGNED
        elif existing.status == TaskStatus.BACKLOG:
            existing.approve()  # BACKLOG → UNASSIGNED (same as promote)
            d.task_board.persist(existing)

    await d.assign_task(task_id, worker_name)

    started = False
    if auto_start:
        from swarm.worker.worker import WorkerState

        worker = d.get_worker(worker_name)
        # SLEEPING is the same idle state as RESTING just past the display
        # threshold — both are safe to push tasks into. Only BUZZING / WAITING
        # / STUNG are skipped (active worker, pending prompt, or dead).
        idle = worker and worker.state in (WorkerState.RESTING, WorkerState.SLEEPING)
        if idle:
            try:
                started = await d.start_task(task_id, actor="user")
            except Exception:
                pass  # Task assigned but start failed — still queued

    status = "started" if started else "assigned"
    console_log(f'Task {status} \u2192 "{worker_name}"')
    return web.json_response({"status": status, "task_id": task_id, "worker": worker_name})


@handle_errors
async def handle_action_start_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return json_error("task_id required")

    result = await d.start_task(task_id, actor="user")
    console_log(f"Task started: {task_id[:8]}")
    return web.json_response({"status": "started" if result else "failed", "task_id": task_id})


@handle_errors
async def handle_action_complete_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    resolution = data.get("resolution", "").strip()
    if not task_id:
        return json_error("task_id required")

    d.complete_task(task_id, resolution=resolution)
    console_log(f"Task completed: {task_id[:8]}")
    return web.json_response({"status": "done", "task_id": task_id})


@handle_errors
async def handle_action_remove_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return json_error("task_id required")

    d.remove_task(task_id)
    console_log(f"Task removed: {task_id[:8]}")
    return web.json_response({"status": "removed", "task_id": task_id})


@handle_errors
async def handle_action_fail_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return json_error("task_id required")

    d.fail_task(task_id)
    console_log(f"Task failed: {task_id[:8]}", level="warn")
    return web.json_response({"status": "failed", "task_id": task_id})


@handle_errors
async def handle_action_reopen_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return json_error("task_id required")

    d.reopen_task(task_id)
    console_log(f"Task reopened: {task_id[:8]}")
    return web.json_response({"status": "reopened", "task_id": task_id})


@handle_errors
async def handle_action_unassign_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return json_error("task_id required")

    d.unassign_task(task_id)
    console_log(f"Task unassigned: {task_id[:8]}")
    return web.json_response({"status": "unassigned", "task_id": task_id})


@handle_errors
async def handle_action_promote_task(request: web.Request) -> web.Response:
    """Promote a Backlog task to Unassigned ("Hand to Queen").

    Mirrors the existing approve flow but is operator-driven from the
    dashboard's Backlog row button rather than from the cross-project
    proposal banner. Backlog → Unassigned is the only legal transition
    here; the auto-assign drone picks up Unassigned tasks (when enabled).
    """
    from swarm.tasks.task import TaskStatus

    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return json_error("task_id required")

    task = d.task_board.get(task_id)
    if task is None:
        return json_error("Task not found", 404)
    if task.status != TaskStatus.BACKLOG:
        return json_error(
            f"Cannot promote task in {task.status.value} state — "
            "only Backlog tasks can be handed to the Queen",
            409,
        )

    d.task_board.approve_task(task_id)  # Backlog → Unassigned (#611 P5: guarded path)
    console_log(f"Task promoted to Unassigned: {task_id[:8]}")
    return web.json_response({"status": "unassigned", "task_id": task_id})


def _parse_cross_task_fields(
    data: aiohttp.MultiDict,
    kwargs: dict[str, Any],
) -> None:
    """Extract cross-project fields from form data into *kwargs*."""
    if "source_worker" in data:
        kwargs["source_worker"] = data["source_worker"].strip()
    if "target_worker" in data:
        kwargs["target_worker"] = data["target_worker"].strip()
    if "dependency_type" in data:
        dep_type = data["dependency_type"].strip()
        if dep_type in ("blocks", "enhances", "enables"):
            kwargs["dependency_type"] = dep_type
    if "acceptance_criteria" in data:
        raw = data["acceptance_criteria"].strip()
        kwargs["acceptance_criteria"] = (
            [line.strip() for line in raw.splitlines() if line.strip()] if raw else []
        )
    if "context_refs" in data:
        raw = data["context_refs"].strip()
        kwargs["context_refs"] = [r.strip() for r in raw.split(",") if r.strip()] if raw else []


async def _parse_edit_fields(data: aiohttp.MultiDict) -> dict[str, Any]:
    """Extract edit-task kwargs from form data."""
    kwargs: dict[str, Any] = {}
    title = data.get("title", "").strip()
    desc = data.get("description")
    if title:
        kwargs["title"] = title
    elif "title" in data and desc:
        kwargs["title"] = await smart_title(desc)
    if desc is not None:
        kwargs["description"] = desc
    if data.get("priority") and data["priority"] in PRIORITY_MAP:
        kwargs["priority"] = PRIORITY_MAP[data["priority"]]
    if data.get("task_type") and data["task_type"] in TYPE_MAP:
        kwargs["task_type"] = TYPE_MAP[data["task_type"]]
    tags_raw = data.get("tags", "").strip()
    if tags_raw:
        kwargs["tags"] = [t.strip() for t in tags_raw.split(",") if t.strip()]
    deps_raw = data.get("depends_on", "")
    if deps_raw:
        kwargs["depends_on"] = [x.strip() for x in deps_raw.strip().split(",") if x.strip()]
    _parse_cross_task_fields(data, kwargs)
    return kwargs


@handle_errors
async def handle_action_edit_task(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return json_error("task_id required")

    kwargs = await _parse_edit_fields(data)
    d.edit_task(task_id, **kwargs)

    # Handle status change via lifecycle methods
    new_status = data.get("status", "").strip()
    if new_status:
        task = d.task_board.get(task_id)
        if task and task.status.value != new_status:
            _apply_status_change(d, task_id, task.status.value, new_status)

    console_log(f"Task edited: {task_id[:8]}")
    return web.json_response({"status": "updated", "task_id": task_id})


async def handle_action_upload_attachment(request: web.Request) -> web.Response:
    d = get_daemon(request)
    reader = await request.multipart()

    task_id = None
    file_data = None
    file_name = "upload"

    while True:
        field = await reader.next()
        if field is None:
            break
        if field.name == "task_id":
            task_id = (await field.text()).strip()
        elif field.name == "file":
            file_name = field.filename or "upload"
            file_data = await field.read(decode=False)

    if not task_id or file_data is None:
        return json_error("task_id and file required")

    if len(file_data) > _MAX_UPLOAD_BYTES:
        return json_error("File too large (max 10 MB)", 413)

    task = d.task_board.get(task_id)
    if not task:
        return json_error(f"Task '{task_id}' not found", 404)

    path = d.save_attachment(file_name, file_data)
    new_attachments = [*task.attachments, path]
    d.task_board.update(task_id, attachments=new_attachments)

    console_log(f"Attachment uploaded: {file_name}")
    return web.json_response({"status": "uploaded", "path": path}, status=201)


async def handle_action_upload(request: web.Request) -> web.Response:
    """Upload a file and return its absolute server path."""
    d = get_daemon(request)
    reader = await request.multipart()

    file_data = None
    file_name = "upload"

    while True:
        field = await reader.next()
        if field is None:
            break
        if field.name == "file":
            file_name = field.filename or "upload"
            file_data = await field.read(decode=False)

    if file_data is None:
        return json_error("file required")

    if len(file_data) > _MAX_UPLOAD_BYTES:
        return json_error("File too large (max 10 MB)", 413)

    path = d.save_attachment(file_name, file_data)
    console_log(f"File uploaded: {file_name} → {path}")
    return web.json_response({"path": path}, status=201)


async def handle_action_fetch_outlook_email(request: web.Request) -> web.Response:
    """Fetch an email from Microsoft Graph API using a message ID."""
    d = get_daemon(request)
    data = await request.post()
    message_id = data.get("message_id", "").strip()
    if not message_id:
        return json_error("message_id required")

    # Check if Graph API is configured and connected
    if not d.graph_mgr:
        return json_error("Microsoft Graph not configured")

    graph_token = await d.graph_mgr.get_token()
    if not graph_token:
        return json_error("Microsoft Graph not connected — authenticate first")

    console_log(f"Fetching email via Graph: {message_id[:30]}...")
    fields = await _graph_email_fields(d, message_id, graph_token)
    if "error" in fields:
        return json_error(fields["error"])
    return web.json_response(fields)


@handle_errors
async def handle_list_outlook_messages(request: web.Request) -> web.Response:
    """List recent Inbox messages via Microsoft Graph for the import picker."""
    d = get_daemon(request)
    if not d.graph_mgr:
        return web.json_response(
            {"connected": False, "messages": [], "error": "Microsoft Graph not configured"}
        )
    token = await d.graph_mgr.get_token()
    if not token:
        return web.json_response(
            {
                "connected": False,
                "messages": [],
                "error": "Microsoft Graph not connected — authenticate first",
            }
        )
    try:
        limit = int(request.query.get("limit", "25"))
    except (TypeError, ValueError):
        limit = 25
    messages = await d.graph_mgr.list_inbox_messages(limit)
    return web.json_response({"connected": True, "messages": messages})


@handle_errors
async def handle_create_tasks_from_outlook(request: web.Request) -> web.Response:
    """Create task(s) from selected Outlook messages fetched via Graph.

    Body: ``{message_ids: [...], mode: "separate" | "merge"}``.
    ``separate`` files one task per email; ``merge`` combines all selected
    emails into a single task (bodies concatenated, attachments unioned).
    Tasks are filed UNASSIGNED for the operator to route from the board.
    """
    d = get_daemon(request)
    if not d.graph_mgr:
        return json_error("Microsoft Graph not configured")
    token = await d.graph_mgr.get_token()
    if not token:
        return json_error("Microsoft Graph not connected — authenticate first")

    body = await request.json()
    message_ids = body.get("message_ids") or []
    mode = (body.get("mode") or "separate").strip().lower()
    if not isinstance(message_ids, list) or not message_ids:
        return json_error("message_ids required")
    if mode not in ("separate", "merge"):
        return json_error("mode must be 'separate' or 'merge'")

    # Fetch each selected email's task fields via Graph (reuses the shared
    # single-email path). Collect per-message failures rather than aborting the
    # whole batch on one bad id.
    fetched: list[dict[str, Any]] = []
    errors: list[str] = []
    for mid in message_ids:
        fields = await _graph_email_fields(d, str(mid), token)
        if "error" in fields:
            errors.append(fields["error"])
            continue
        fetched.append(fields)
    if not fetched:
        return json_error("Could not fetch any selected email(s): " + "; ".join(errors[:3]))

    created: list[dict[str, Any]] = []
    if mode == "separate":
        for f in fetched:
            task = d.create_task(
                title=f.get("title") or "(no subject)",
                description=f.get("description", ""),
                task_type=TYPE_MAP.get(f.get("task_type", ""), TaskType.CHORE),
                attachments=f.get("attachments", []),
                source_email_id=f.get("message_id", ""),
                actor="user",
            )
            created.append({"number": task.number, "title": task.title})
    else:  # merge
        parts: list[str] = []
        merged_attachments: list[str] = []
        for i, f in enumerate(fetched, 1):
            parts.append(f"--- Email {i}: {f.get('title', '')} ---\n{f.get('description', '')}")
            merged_attachments.extend(f.get("attachments", []))
        merged_title = f"{len(fetched)} emails: {fetched[0].get('title', '')}"[:120]
        task = d.create_task(
            title=merged_title,
            description="\n\n".join(parts),
            task_type=TYPE_MAP.get(fetched[0].get("task_type", ""), TaskType.CHORE),
            attachments=merged_attachments,
            actor="user",
        )
        created.append({"number": task.number, "title": task.title})

    return web.json_response({"created": created, "count": len(created), "errors": errors})


async def _translate_exchange_id(
    sess: aiohttp.ClientSession, headers: dict[str, str], ews_id: str
) -> str | None:
    """Translate an EWS-format message ID to REST format via Graph API."""
    url = "https://graph.microsoft.com/v1.0/me/translateExchangeIds"
    payload = {
        "inputIds": [ews_id],
        "sourceIdType": "ewsId",
        "targetIdType": "restId",
    }
    try:
        async with sess.post(url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                err = await resp.text()
                console_log(f"translateExchangeIds failed ({resp.status}): {err[:200]}")
                return None
            data = await resp.json()
            values = data.get("value", [])
            if values:
                return values[0].get("targetId")
    except Exception as exc:
        console_log(f"translateExchangeIds error: {exc}")
    return None


async def _fetch_attachment_bytes(
    sess: aiohttp.ClientSession,
    headers: dict[str, str],
    msg_id: str,
    att_id: str,
    quote: Callable[..., str],
    yarl: ModuleType,
) -> str:
    """Fetch a single attachment's contentBytes from Graph API."""
    import aiohttp as _aiohttp

    encoded_msg = quote(msg_id, safe="")
    encoded_att = quote(att_id, safe="")
    url = yarl.URL(
        f"https://graph.microsoft.com/v1.0/me/messages/{encoded_msg}/attachments/{encoded_att}",
        encoded=True,
    )
    try:
        async with sess.get(url, headers=headers, timeout=_aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                _log.warning(
                    "attachment fetch failed (msg=%s att=%s status=%d)",
                    msg_id,
                    att_id,
                    resp.status,
                )
                return ""
            data = await resp.json()
            return data.get("contentBytes", "")
    except (_aiohttp.ClientError, TimeoutError) as exc:
        # Network / Graph-side transients — log forensically so ops can
        # correlate a missing attachment with a Graph outage, but keep
        # the empty-string sentinel so the renderer skips this attachment
        # rather than crashing the whole message render.
        _log.warning(
            "attachment fetch error (msg=%s att=%s): %s",
            msg_id,
            att_id,
            exc,
            exc_info=True,
        )
        return ""


async def _graph_email_fields(d: SwarmDaemon, message_id: str, token: str) -> dict[str, Any]:
    """Fetch one email + attachments from Microsoft Graph and process it into
    task fields. Returns the processed dict (title/description/task_type/
    attachments/message_id) or ``{"error": "..."}`` on failure. Shared by the
    single-email drag path and the bulk Outlook-import path."""
    from urllib.parse import quote

    import aiohttp as _aiohttp
    import yarl

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    base = "https://graph.microsoft.com/v1.0/me/messages"

    async def _graph_get(sess: _aiohttp.ClientSession, mid: str) -> tuple[int, str]:
        # Explicitly request the body fields we need — Graph's default
        # selection sometimes returns body.content empty for replies and
        # forwarded messages with nested HTML. bodyPreview is always
        # populated (first 255 chars) so it's our safety net.
        encoded = quote(mid, safe="")
        select = "subject,body,bodyPreview,from,toRecipients,ccRecipients,sentDateTime,uniqueBody"
        url = yarl.URL(
            f"{base}/{encoded}?$expand=attachments&$select={select}",
            encoded=True,
        )
        console_log(f"Graph GET: {str(url)[:160]}...")
        async with sess.get(url, headers=headers, timeout=_aiohttp.ClientTimeout(total=15)) as resp:
            return resp.status, await resp.text()

    import json as _json

    try:
        async with _aiohttp.ClientSession() as sess:
            effective_id = message_id
            status, body = await _graph_get(sess, message_id)

            # If 400/404, the ID may be in EWS format -- translate to REST format
            if status in (400, 404) and ("/" in message_id or "+" in message_id):
                console_log("Direct fetch failed; translating EWS ID to REST format...")
                rest_id = await _translate_exchange_id(sess, headers, message_id)
                if rest_id and rest_id != message_id:
                    effective_id = rest_id
                    status, body = await _graph_get(sess, rest_id)

            if status != 200:
                console_log(f"Graph API error {status}: {body[:200]}", level="error")
                return {"error": f"Graph API {status}: {body[:200]}"}

            msg = _json.loads(body)

            # Individually fetch attachment bytes if missing (Graph sometimes omits them)
            attachments = msg.get("attachments", [])
            for att in attachments:
                if (
                    att.get("@odata.type") == "#microsoft.graph.fileAttachment"
                    and not att.get("contentBytes")
                    and att.get("id")
                ):
                    att["contentBytes"] = await _fetch_attachment_bytes(
                        sess, headers, effective_id, att["id"], quote, yarl
                    )
    except Exception as exc:
        return {"error": str(exc)[:200]}

    # Pick the best body field. Graph populates these in this priority order
    # for a typical message: ``uniqueBody`` (the latest reply *only*, with the
    # quoted history stripped), ``body`` (the full conversation), then
    # ``bodyPreview`` (first 255 chars, plain text — always present). Some
    # tenants return ``body.content`` empty on replies/forwards; the cascade
    # below ensures we always land somewhere with text.
    body_obj = msg.get("body") or {}
    unique_obj = msg.get("uniqueBody") or {}
    raw_body = body_obj.get("content", "") or ""
    raw_unique = unique_obj.get("content", "") or ""
    raw_preview = msg.get("bodyPreview") or ""

    body_content = raw_body
    body_type = body_obj.get("contentType", "text") or "text"
    body_source = "body" if body_content.strip() else ""
    if not body_content.strip() and raw_unique.strip():
        body_content = raw_unique
        body_type = unique_obj.get("contentType", body_type) or body_type
        body_source = "uniqueBody"
    if not body_content.strip() and raw_preview.strip():
        body_content = raw_preview
        body_type = "text"
        body_source = "bodyPreview"

    _log.warning(
        "Graph body resolved (msg=%s): "
        "source=%r type=%s final_len=%d | body.len=%d (type=%s) "
        "uniqueBody.len=%d (type=%s) bodyPreview.len=%d | "
        "fields=%s | body.head=%r",
        effective_id[:30] + "...",
        body_source or "EMPTY",
        body_type,
        len(body_content),
        len(raw_body),
        body_obj.get("contentType", "?"),
        len(raw_unique),
        unique_obj.get("contentType", "?"),
        len(raw_preview),
        sorted(msg.keys()),
        (raw_body or raw_unique or raw_preview)[:300],
    )
    console_log(
        f"Graph body resolved: source={body_source or 'EMPTY'} type={body_type} "
        f"len={len(body_content)} body={len(raw_body)} unique={len(raw_unique)} "
        f"preview={len(raw_preview)}"
    )
    result = await d.process_email_data(
        subject=msg.get("subject", ""),
        body_content=body_content,
        body_type=body_type,
        attachment_dicts=attachments,
        effective_id=effective_id,
    )
    return result


@handle_errors
async def handle_action_fetch_image(request: web.Request) -> web.Response:
    """Fetch an external image URL and save it as an attachment."""
    d = get_daemon(request)
    data = await request.post()
    url = data.get("url", "").strip()
    if not url:
        return json_error("url required")

    path = await d.fetch_and_save_image(url)
    return web.json_response({"path": path}, status=201)


@handle_errors
async def handle_action_retry_draft(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    task_id = data.get("task_id", "")
    if not task_id:
        return json_error("task_id required")

    await d.retry_draft_reply(task_id)
    console_log(f"Retrying draft reply for task {task_id[:8]}")
    return web.json_response({"status": "retrying", "task_id": task_id})


def register(app: web.Application) -> None:
    """Register task action routes."""
    app.router.add_post("/action/task/create", handle_action_create_task)
    app.router.add_post("/action/task/assign", handle_action_assign_task)
    app.router.add_post("/action/task/start", handle_action_start_task)
    app.router.add_post("/action/task/complete", handle_action_complete_task)
    app.router.add_post("/action/task/remove", handle_action_remove_task)
    app.router.add_post("/action/task/fail", handle_action_fail_task)
    app.router.add_post("/action/task/unassign", handle_action_unassign_task)
    app.router.add_post("/action/task/promote", handle_action_promote_task)
    app.router.add_post("/action/task/reopen", handle_action_reopen_task)
    app.router.add_post("/action/task/edit", handle_action_edit_task)
    app.router.add_post("/action/task/upload", handle_action_upload_attachment)
    app.router.add_post("/action/upload", handle_action_upload)
    app.router.add_post("/action/fetch-image", handle_action_fetch_image)
    app.router.add_post("/action/fetch-outlook-email", handle_action_fetch_outlook_email)
    app.router.add_get("/api/outlook/messages", handle_list_outlook_messages)
    app.router.add_post("/api/tasks/from-outlook", handle_create_tasks_from_outlook)
    app.router.add_post("/action/task/retry-draft", handle_action_retry_draft)
