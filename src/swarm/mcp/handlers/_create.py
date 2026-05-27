"""Handler for the ``swarm_create_task`` MCP tool.

Extracted from ``mcp/tools.py`` (task #518) — split into its own module
to keep both this file and the sibling ``_tasks.py`` under the audit's
≤ 300 LOC per-module budget without breaking the
schema-and-handler-co-located pattern.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import CreateTaskArgs
from swarm.mcp.types import TextContent

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


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


def _handle_create_task(
    d: SwarmDaemon, worker_name: str, args: CreateTaskArgs
) -> list[TextContent]:
    title = args.get("title", "")
    if not title:
        return [{"type": "text", "text": "Missing 'title'"}]
    attachments = args.get("attachments") or None
    if attachments:
        validated: list[str] = []
        for p in attachments:
            rp = Path(p).resolve()
            if not rp.exists():
                return [{"type": "text", "text": f"Attachment not found: {p}"}]
            validated.append(str(rp))
        attachments = validated
    task = d.create_task(
        title=title,
        description=args.get("description", ""),
        attachments=attachments,
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
        if should_dispatch:
            coro = d.assign_and_start_task(task.id, target, actor=worker_name)
        else:
            coro = d.assign_task(task.id, target, actor=worker_name)
        try:
            loop = asyncio.get_running_loop()
            _task = loop.create_task(coro)
            _task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        except RuntimeError:
            # No running event loop (test/CLI context): close the coroutine
            # we created above so Python doesn't emit "coroutine was never
            # awaited" and fall back to the synchronous board-level assign.
            try:
                coro.close()
            except Exception:
                pass
            d.task_board.assign(task.id, target)
    return [{"type": "text", "text": f"Task created: #{task.number} {title}"}]


HANDLERS = {"swarm_create_task": _handle_create_task}
