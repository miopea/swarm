"""Handler for the ``swarm_create_task`` MCP tool.

Extracted from ``mcp/tools.py`` (task #518) — split into its own module
to keep both this file and the sibling ``_tasks.py`` under the audit's
≤ 300 LOC per-module budget without breaking the
schema-and-handler-co-located pattern.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swarm.logging import get_logger
from swarm.mcp._arg_types import CreateTaskArgs
from swarm.mcp.types import TextContent
from swarm.tasks.authority_guard import AuthorityVerdict, screen_task_authority
from swarm.tasks.task import HOLD_TAG
from swarm.worker.worker import QUEEN_WORKER_NAME

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("mcp.create")


async def _synthesize_then_dispatch(
    d: SwarmDaemon, task_id: str, actor: str, dispatch: Awaitable[Any]
) -> Any:
    """Synthesize the Outcomes rubric for a just-created task, THEN await its
    assign/dispatch coroutine — so the criteria are visible in the task message
    the target worker receives and available to the verifier. Runs inside the
    scheduled background coroutine (not the sync tool call), so swarm_create_task
    returns immediately and the synthesis latency is absorbed before dispatch,
    not before the reply. Synthesis failure never blocks dispatch.
    """
    task = d.task_board.get(task_id)
    if task is not None:
        try:
            await d.tasks.apply_synthesized_criteria(task, actor=actor)
        except Exception:
            _log.warning("criteria synthesis failed for task %s", task_id, exc_info=True)
    return await dispatch


def _schedule_synth_dispatch(
    d: SwarmDaemon, task_id: str, target: str, worker_name: str, dispatch: Awaitable[Any]
) -> None:
    """Schedule synthesis+dispatch on the running loop, or fall back to a
    synchronous board-level assign when there's no loop (test/CLI context).

    Extracted from ``_handle_create_task`` to keep that handler under the
    complexity budget. ``dispatch`` is an already-created coroutine (so the
    assign call is recorded synchronously); on the no-loop path it is closed
    to avoid an un-awaited-coroutine warning.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            dispatch.close()  # type: ignore[attr-defined]
        except Exception:
            pass
        d.task_board.assign(task_id, target)
        return
    _task = loop.create_task(_synthesize_then_dispatch(d, task_id, worker_name, dispatch))
    _task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_create_task",
        "description": (
            "File a new task on the Swarm task board. Use this when you discover work that "
            "needs doing but shouldn't block your current task — a bug in another module, "
            "a refactor opportunity, a followup from a fix, a cross-project change another "
            "worker owns. Set target_worker to route cross-project work (see the worker name "
            "table in CLAUDE.md). Priority defaults to 'normal'; use 'urgent' only for "
            "production-impacting issues. Attachments must be absolute paths to existing "
            "files (typically screenshots captured during debugging)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": (
                        "Short imperative title (e.g. 'Fix tenant resolution in "
                        "anonymous sessions')."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "What needs doing and why. Include repro steps for bugs, "
                        "acceptance criteria for features."
                    ),
                },
                "target_worker": {
                    "type": "string",
                    "description": (
                        "Worker name to assign to (e.g. 'hub', 'platform', "
                        "'project-root'). Omit to leave unassigned."
                    ),
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high", "urgent"],
                    "description": "'urgent' only for production-impacting issues.",
                },
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Absolute paths to existing files (typically screenshots).",
                },
                "start": {
                    "type": "boolean",
                    "description": (
                        "Whether to dispatch the task into the target_worker's PTY "
                        "immediately (default true). Pass false to queue the task "
                        "in ASSIGNED status without interrupting the target's "
                        "current turn — useful when lining up follow-up work."
                    ),
                },
                "acceptance_criteria": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Machine-checkable success criteria, one per item. Each "
                        "should be a short, verifiable statement (e.g. 'returns "
                        "200 for new tasks' / 'logs creation event'). The verifier "
                        "drone reads these post-completion and cites failed ones "
                        "in the verification reason — leaving them empty falls back "
                        "to the verifier's default-pass behaviour for tasks without "
                        "objective criteria."
                    ),
                },
                "hold": {
                    "type": "boolean",
                    "description": (
                        "File the task as HOLD/dormant (default false). A HOLD "
                        "task stays UNASSIGNED and visible/tracked on the board but "
                        "is NOT auto-dispatched to a worker — use it for deferred "
                        "work you're deliberately parking (e.g. 'hold this jQuery "
                        "3→4 upgrade until we decide'). An operator assigns it "
                        "manually when it's time; the auto-assigner leaves it alone."
                    ),
                },
            },
            "required": ["title"],
            "examples": [
                {
                    "title": "Remove dead feature flag FEATURE_X_ENABLED",
                    "description": (
                        "Flag has been 100% rolled out for 4 weeks. Remove from "
                        "config.ts and all call sites."
                    ),
                    "priority": "low",
                },
                {
                    "title": "Nexus: emails over 1MB fail to ingest",
                    "description": (
                        "Reproduced with attached sample. Root cause likely "
                        "base64 buffer in MailParser. Repro: POST "
                        "/api/v1/nexus/ingest with the attached eml."
                    ),
                    "target_worker": "nexus",
                    "priority": "high",
                    "attachments": ["/home/user/bug-evidence/large-email.eml"],
                },
            ],
        },
    },
]


def _resolve_attachments(args: CreateTaskArgs) -> tuple[list[str] | None, list[TextContent] | None]:
    """Resolve + existence-check attachment paths. Returns (paths, error)."""
    attachments = args.get("attachments") or None
    if not attachments:
        return None, None
    validated: list[str] = []
    for p in attachments:
        rp = Path(p).resolve()
        if not rp.exists():
            return None, [{"type": "text", "text": f"Attachment not found: {p}"}]
        validated.append(str(rp))
    return validated, None


def _park_for_authority_review(
    d: SwarmDaemon, worker_name: str, task: Any, matched: str
) -> list[TextContent]:
    """#894: an auto-generated task fabricated operator authority — log a
    warning + return the parked-for-review response (NOT dispatched)."""
    from swarm.drones.log import LogCategory, SystemAction

    try:
        d.drone_log.add(
            SystemAction.TASK_AUTHORITY_GATED,
            worker_name,
            (
                f"#{task.number} cites operator authority without a verifiable source "
                f"('{matched}') — parked HOLD for review, not dispatched"
            ),
            category=LogCategory.TASK,
        )
    except Exception:
        pass
    return [
        {
            "type": "text",
            "text": (
                f"Task #{task.number} created but PARKED (HOLD) for operator review: its text "
                f"claims operator authority ('{matched}') without a verifiable source. "
                f"Auto-generated tasks can't assert operator decisions — if this is real, cite "
                f"the operator's approval (a thread/message/link) or have the operator dispatch "
                f"it. NOT auto-dispatched."
            ),
        }
    ]


def _handle_create_task(
    d: SwarmDaemon, worker_name: str, args: CreateTaskArgs
) -> list[TextContent]:
    title = args.get("title", "")
    if not title:
        return [{"type": "text", "text": "Missing 'title'"}]
    attachments, att_error = _resolve_attachments(args)
    if att_error is not None:
        return att_error
    description = args.get("description", "")
    # #894: a task arriving here is AUTO-GENERATED (a worker/drone filed it via
    # swarm_create_task — operator tasks come through the dashboard). If its
    # text CITES operator authority / a policy amendment with no verifiable
    # source, it's fabricated authorization (the @types/node "operator opted
    # in, amendment in flight" case). Park it HOLD for operator review instead
    # of dispatching — never let an auto-task invent authority to act.
    # #939: the Queen is the operator's authorized relay — surfacing operator
    # reports ("operator says…", "operator reported…") IS her job, so the
    # authority guard must not park every Queen-authored task. Exempt her
    # (mirrors her #873 fanout-cap exemption); the guard still catches genuine
    # auto-generated / worker-spawned fabrications.
    if worker_name == QUEEN_WORKER_NAME:
        authority = AuthorityVerdict(flagged=False, matched="")
    else:
        authority = screen_task_authority(title, description)
    # A HOLD task is filed UNASSIGNED but tagged so the auto-assign drone won't
    # grab it (see SwarmTask.is_available). Stays visible/tracked. Authority-
    # flagged tasks are forced HOLD regardless of the caller's ``hold`` arg.
    on_hold = bool(args.get("hold")) or authority.flagged
    tags = [HOLD_TAG] if on_hold else None
    task = d.create_task(
        title=title,
        description=description,
        attachments=attachments,
        tags=tags,
        actor=worker_name,
    )
    # Acceptance criteria flow through edit_task to keep create_task's
    # signature small. The field has lived on SwarmTask since v1 but
    # was unread until Phase 2 wired it into the verifier (2026-05-08).
    raw_criteria = args.get("acceptance_criteria")
    if isinstance(raw_criteria, list):
        cleaned = [str(c).strip() for c in raw_criteria if str(c).strip()]
        if cleaned:
            d.edit_task(task.id, acceptance_criteria=cleaned, actor=worker_name)
    if authority.flagged:
        return _park_for_authority_review(d, worker_name, task, authority.matched)
    target = args.get("target_worker")
    # Record cross-project attribution BEFORE assigning. When a worker
    # files a task for a *different* worker, the calling worker is the
    # source and the arg is the target — without this the task row
    # lands in the DB with ``source_worker=''`` and cross-project
    # lineage is lost. Self-targeted tasks aren't cross-project and
    # are skipped.
    if target and target != worker_name:
        source = worker_name if worker_name and worker_name != "unknown" else ""
        d.edit_task(
            task.id,
            source_worker=source,
            target_worker=target,
            actor=worker_name,
        )
    if target:
        # Phase 1 of task #225: by default, assignment DISPATCHES the task
        # into the target worker's PTY. The old behaviour stopped at
        # ``assign_task`` (ASSIGNED status only), which left workers sitting
        # on queued work because nothing pushed the task body into their
        # input buffer. ``start=False`` opts out for Queen/operator flows
        # that want to line up work without interrupting the target
        # worker's current turn. Self-targeted tasks never dispatch —
        # injecting a task description back into the caller's own PTY
        # would interleave with the response it is currently producing.
        should_dispatch = bool(args.get("start", True)) and target != worker_name
        # Create the dispatch coroutine eagerly (records the assign call), then
        # schedule it behind Outcomes-rubric synthesis.
        if should_dispatch:
            dispatch = d.assign_and_start_task(task.id, target, actor=worker_name)
        else:
            dispatch = d.assign_task(task.id, target, actor=worker_name)
        _schedule_synth_dispatch(d, task.id, target, worker_name, dispatch)
    suffix = " [HOLD — parked, not auto-dispatched]" if (tags and not target) else ""
    return [{"type": "text", "text": f"Task created: #{task.number} {title}{suffix}"}]


HANDLERS = {"swarm_create_task": _handle_create_task}
