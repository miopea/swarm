"""Handlers for the task-status + task-completion MCP tools.

Extracted from ``mcp/tools.py`` (task #518). ``create_task`` is its
own module (:mod:`swarm.mcp.handlers._create`) to keep both files under
the per-module LOC budget. The presentation helpers used by
``_handle_task_status`` live in :mod:`swarm.mcp.handlers._task_format`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import CompleteTaskArgs, TaskStatusArgs
from swarm.mcp.handlers._task_format import (
    _TASK_STATUS_DEFAULT_LIMIT,
    _apply_task_filter,
    _coerce_limit,
    _format_task_line,
    _lookup_task_by_number,
    _sort_tasks_for_display,
    _task_to_payload,
)
from swarm.mcp.types import HandlerResult, TextContent

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


_ACTIVE_STATUSES = ("assigned", "active")


TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_task_status",
        "description": (
            "Query the Swarm task board — including YOUR OWN assigned work. "
            "Call swarm_task_status with filter='mine' to see just your tasks (your "
            "current work + your queue) — this is your 'what am I supposed to be doing?' "
            "lookup; pass include_completed=true to also see your recent closeouts. "
            "Other filters: 'unassigned' to find queen-eligible work, 'assigned' for "
            "anything with an owner, or omit filter for the whole board. "
            "Open tasks (backlog/unassigned/assigned/active) come first, newest-by-number first; "
            "done/failed tasks sort after, most-recently-completed first. Results are "
            "capped at ``limit`` (default 50, max 500); when output is truncated a summary "
            "footer names the total. For ``filter='mine'``, completed history is suppressed "
            "unless ``include_completed`` is true — the default surfaces your actionable work "
            "rather than bury it behind old closeouts. Pass ``number`` to look up a single task "
            "by its display number (bypasses all other filters)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "enum": ["all", "backlog", "unassigned", "assigned", "active", "mine"],
                    "description": "Which tasks to return (default: 'all').",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "Maximum rows to return (default 50, max 500).",
                },
                "include_completed": {
                    "type": "boolean",
                    "description": (
                        "Include completed/failed tasks when filter='mine'. "
                        "Default false (open tasks only). Ignored for other filters."
                    ),
                },
                "number": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Look up a single task by its display number "
                        "(e.g. 142). Overrides filter/limit."
                    ),
                },
            },
            "examples": [
                {"filter": "mine"},
                {"filter": "mine", "include_completed": True},
                {"filter": "unassigned", "limit": 100},
                {"number": 142},
                {},
            ],
        },
    },
    {
        "name": "swarm_complete_task",
        "description": (
            "Mark one of your assigned tasks as completed. Call this only after you have "
            "verified your work (tests pass, /check clean, feature demonstrably works). The "
            "resolution is stored as task learnings and shown to future workers picking up "
            "similar tasks — write it for *them*, not for a manager. A good resolution names "
            "the root cause (for bugs), the files you touched, and any followup work you "
            "spotted but didn't do. When you have exactly one active assignment, ``number`` "
            "can be omitted. When you have multiple active assignments, pass ``number`` "
            "explicitly — the tool refuses to guess which task you mean, because silent "
            "guessing is how resolutions get attached to the wrong record. Fails if you "
            "have no active task or the specified number isn't assigned to you."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "resolution": {
                    "type": "string",
                    "description": (
                        "What was done. Name files touched, root cause for bugs, "
                        "and any followup worth flagging."
                    ),
                },
                "number": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Display number of the task you are closing (e.g. 169). "
                        "Required when you have more than one active assignment. "
                        "Optional when you have exactly one."
                    ),
                },
            },
            "required": ["resolution"],
            "examples": [
                {
                    "resolution": (
                        "Fixed null pointer in ContactService.resolveTenant "
                        "(src/services/contact.ts:142) — missing guard for anonymous "
                        "sessions. Added regression test. Followup: refactor tenant "
                        "resolution out of service constructor (noted but not done)."
                    ),
                },
                {
                    "number": 169,
                    "resolution": (
                        "Added disambiguation to swarm_complete_task (src/swarm/mcp/tools.py). "
                        "Workers with multiple in_progress tasks must now pass ``number``."
                    ),
                },
            ],
        },
    },
]


def _handle_task_status(d: SwarmDaemon, worker_name: str, args: TaskStatusArgs) -> HandlerResult:
    if not d.task_board:
        return [{"type": "text", "text": "No task board available."}]

    # Single-task lookup by display number — bypasses filter/limit so a worker
    # that hears about task #142 from another channel can always pull it up.
    if (number := args.get("number")) is not None:
        return _lookup_task_by_number(d, number)

    limit = _coerce_limit(args.get("limit", _TASK_STATUS_DEFAULT_LIMIT))
    if isinstance(limit, str):
        return [{"type": "text", "text": limit}]

    tasks = _apply_task_filter(
        list(d.task_board.all_tasks),
        args.get("filter", "all"),
        worker_name,
        include_completed=bool(args.get("include_completed", False)),
    )
    total = len(tasks)
    shown = _sort_tasks_for_display(tasks)[:limit]
    if not shown:
        return [{"type": "text", "text": "No tasks found."}]

    lines = [_format_task_line(t) for t in shown]
    if total > len(shown):
        lines.append(
            f"\n… {total - len(shown)} more not shown "
            f"(total={total}, limit={limit}). "
            "Pass a higher 'limit' or a more specific 'filter'."
        )
    payload = [_task_to_payload(t) for t in shown]
    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "structuredContent": {
            "tasks": payload,
            "shown": len(payload),
            "total": total,
            "filter": args.get("filter", "all"),
            "limit": limit,
            "include_completed": bool(args.get("include_completed", False)),
        },
    }


def _handle_complete_task(
    d: SwarmDaemon, worker_name: str, args: CompleteTaskArgs
) -> list[TextContent]:
    resolution = args.get("resolution", "")
    if not d.task_board:
        return [{"type": "text", "text": "No task board."}]

    # Task #275: the server resolves worker identity from the MCP URL query
    # string on every request. When a session's `.mcp.json` lacks
    # `?worker=<name>` (common after editing .mcp.json live — Claude Code's
    # HTTP MCP transport keeps using the bootstrap URL), `worker_name` here
    # is `"unknown"`. Every ownership check below would fail with a message
    # that points at the wrong root cause ("not assigned to you", "no active
    # task"). Fail fast with the diagnostic so the caller fixes the URL
    # instead of chasing the assignment.
    if worker_name == "unknown":
        return [
            {
                "type": "text",
                "text": (
                    "Cannot identify calling worker (worker_name=unknown). "
                    "swarm_complete_task requires caller identity, which the "
                    "server reads from the MCP URL. Check that .mcp.json "
                    "includes `?worker=<name>` in the swarm MCP server URL. "
                    "If you just edited .mcp.json, restart Claude Code so the "
                    "MCP transport picks up the new URL."
                ),
            }
        ]

    requested = args.get("number")
    active = [
        t
        for t in d.task_board.all_tasks
        if t.assigned_worker == worker_name and t.status.value in _ACTIVE_STATUSES
    ]

    # Explicit lookup wins — validate ownership and status before closing.
    # Runs even when ``active`` is empty so the caller gets a targeted error
    # (e.g. "not assigned to you") instead of a generic "no active task".
    if requested is not None:
        try:
            target_num = int(requested)
        except (TypeError, ValueError):
            return [{"type": "text", "text": f"Invalid 'number': {requested!r}"}]
        match = next(
            (t for t in d.task_board.all_tasks if t.number == target_num),
            None,
        )
        if match is None:
            return [{"type": "text", "text": f"No task found with number #{target_num}."}]
        if match.assigned_worker != worker_name:
            owner = match.assigned_worker or "nobody"
            return [
                {
                    "type": "text",
                    "text": (
                        f"Task #{target_num} is not assigned to you (assigned_worker={owner})."
                    ),
                }
            ]
        if match.status.value not in _ACTIVE_STATUSES:
            return [
                {
                    "type": "text",
                    "text": (
                        f"Task #{target_num} is not in progress "
                        f"(status={match.status.value}) — nothing to complete."
                    ),
                }
            ]
        d.complete_task(match.id, actor=worker_name, resolution=resolution)
        return [{"type": "text", "text": f"Task #{target_num} completed."}]

    if not active:
        return [{"type": "text", "text": "No active task found."}]

    # Multiple active assignments and no ``number`` — refuse to guess. The
    # pre-#169 behaviour closed whichever task iteration happened to yield
    # first, attaching the resolution to the wrong record. Listing the
    # candidate numbers gives the worker everything it needs to retry.
    if len(active) > 1:
        numbers = ", ".join(f"#{t.number}" for t in sorted(active, key=lambda t: t.number))
        return [
            {
                "type": "text",
                "text": (
                    f"You have {len(active)} active tasks ({numbers}); pass "
                    f"'number' to specify which to complete."
                ),
            }
        ]

    task = active[0]
    d.complete_task(task.id, actor=worker_name, resolution=resolution)
    return [{"type": "text", "text": f"Task #{task.number} completed."}]


HANDLERS = {
    "swarm_task_status": _handle_task_status,
    "swarm_complete_task": _handle_complete_task,
}
