"""Queen MCP handlers for task-targeted actions (reassign, force-complete).

Extracted from ``mcp/queen_tools.py`` (task #519). Hosts the shared
``_fire_async`` + ``_resolve_task`` helpers used by these handlers AND
the worker-targeted ones in ``_workers.py``.

Destructive-action note: the spec calls for an inline operator
confirmation UI before these fire. That UI ships with the chat-panel
sub-pass. Until then these execute immediately; every call logs to the
OPERATOR category in the buzz log so the operator can audit, and each
handler requires a free-text ``reason`` so intent is captured at the
call site.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import QueenForceCompleteTaskArgs, QueenReassignTaskArgs
from swarm.mcp.queen_handlers._common import _assert_queen

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
    {
        "name": "queen_reassign_task",
        "description": (
            "Move an assigned or in-progress task from one worker to another.  Use "
            "when you've determined the original assignee can't reach the work "
            "(blocked, wrong expertise, over-loaded) and a peer is better-positioned. "
            "Call queen_view_worker_state on both workers first so you're acting on "
            "current reality, not a stale assumption.  If `start` is true, the new "
            "worker is immediately sent the task message; otherwise the task sits "
            "ASSIGNED for the next poll cycle."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "number": {
                    "type": "integer",
                    "description": (
                        "Task number (from queen_view_task_board).  Preferred over "
                        "task_id because operator-readable logs show this."
                    ),
                },
                "task_id": {
                    "type": "string",
                    "description": "Internal task id.  Use if you only have the id.",
                },
                "to_worker": {
                    "type": "string",
                    "description": "Name of the worker that should receive the task.",
                },
                "start": {
                    "type": "boolean",
                    "description": (
                        "When true, dispatch the task to the new worker's PTY "
                        "immediately.  Default false (task sits ASSIGNED)."
                    ),
                    "default": False,
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Short reason shown in the buzz log and task history.  "
                        "Required — the operator audits reassignments."
                    ),
                },
            },
            "required": ["to_worker", "reason"],
            "examples": [
                {"number": 42, "to_worker": "platform", "reason": "hub over-loaded", "start": True},
            ],
        },
    },
    {
        "name": "queen_force_complete_task",
        "description": (
            "Mark a task COMPLETED even though the assigned worker didn't call "
            "swarm_complete_task.  DESTRUCTIVE: bypasses the worker's own signal, "
            "freeing them to pick up new work and removing the task from the open "
            "board.  Use when the worker is demonstrably done but silent — e.g. "
            "they went RESTING after shipping and their PTY shows the outcome but "
            "they never issued the completion call.  Always include a resolution "
            "summary noting what the worker actually did (so task_history has it)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "number": {
                    "type": "integer",
                    "description": "Task number.  Preferred.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task id.  Use if only the id is known.",
                },
                "resolution": {
                    "type": "string",
                    "description": (
                        "Summary of what was actually accomplished.  Shown in "
                        "task history and downstream reports — be specific."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Short reason for forcing completion.  Required — the "
                        "operator audits force-completions."
                    ),
                },
            },
            "required": ["resolution", "reason"],
            "examples": [
                {
                    "number": 42,
                    "resolution": "Fixed auth middleware; verified via grep + running tests.",
                    "reason": "worker went RESTING after shipping — forgot completion call",
                },
            ],
        },
    },
]


def _resolve_task(d: SwarmDaemon, args: dict[str, Any]) -> Any | list[dict[str, Any]]:
    """Look up a task by ``number`` or ``task_id``. Return the task or an error payload."""
    number = args.get("number")
    task_id = (args.get("task_id") or "").strip() or None
    if number is None and not task_id:
        return [{"type": "text", "text": "Missing 'number' or 'task_id'."}]
    if d.task_board is None:
        return [{"type": "text", "text": "Task board is unavailable."}]
    if number is not None:
        try:
            target = int(number)
        except (TypeError, ValueError):
            return [{"type": "text", "text": f"Invalid 'number': {number!r}"}]
        for t in d.task_board.all_tasks:
            if t.number == target:
                return t
        return [{"type": "text", "text": f"No task with number #{target}."}]
    task = d.task_board.get(task_id)
    if task is None:
        return [{"type": "text", "text": f"No task with id {task_id!r}."}]
    return task


def _fire_async(coro: Any) -> None:
    """Fire an async daemon method from a sync MCP handler context.

    Falls back to silently dropping the call if no event loop is
    available (should only happen in unit tests that mock the daemon).
    """
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    except RuntimeError:
        try:
            coro.close()
        except Exception:
            pass


def _handle_reassign_task(
    d: SwarmDaemon, worker_name: str, args: QueenReassignTaskArgs
) -> list[dict[str, Any]]:
    err = _assert_queen(worker_name)
    if err:
        return err
    to_worker = (args.get("to_worker") or "").strip()
    reason = (args.get("reason") or "").strip()
    if not to_worker:
        return [{"type": "text", "text": "Missing 'to_worker'."}]
    if not reason:
        return [{"type": "text", "text": "Missing 'reason' — reassignments must be audited."}]
    target = _resolve_task(d, args)
    if isinstance(target, list):
        return target
    task = target
    start = bool(args.get("start", False))
    prev = task.assigned_worker or "unassigned"

    if prev == to_worker:
        return [{"type": "text", "text": f"Task #{task.number} already assigned to {to_worker}."}]

    # Unassign first so assign() accepts (it checks is_available).
    if task.assigned_worker:
        d.task_board.unassign(task.id)
    if not d.task_board.assign(task.id, to_worker):
        return [
            {
                "type": "text",
                "text": f"Failed to assign #{task.number} to {to_worker} (not available).",
            }
        ]
    from swarm.drones.log import LogCategory, SystemAction

    d.drone_log.add(
        SystemAction.OPERATOR,
        to_worker,
        f"queen reassigned #{task.number} from {prev}: {reason[:120]}",
        category=LogCategory.OPERATOR,
    )
    if start:
        _fire_async(d.assign_and_start_task(task.id, to_worker, actor="queen"))
        return [
            {
                "type": "text",
                "text": (f"Reassigned #{task.number} from {prev} → {to_worker} and dispatched."),
            }
        ]
    return [
        {
            "type": "text",
            "text": f"Reassigned #{task.number} from {prev} → {to_worker} (ASSIGNED, not started).",
        }
    ]


def _handle_force_complete_task(
    d: SwarmDaemon, worker_name: str, args: QueenForceCompleteTaskArgs
) -> list[dict[str, Any]]:
    err = _assert_queen(worker_name)
    if err:
        return err
    resolution = (args.get("resolution") or "").strip()
    reason = (args.get("reason") or "").strip()
    if not resolution:
        return [{"type": "text", "text": "Missing 'resolution'."}]
    if not reason:
        return [
            {
                "type": "text",
                "text": "Missing 'reason' — force-completions must be audited.",
            }
        ]
    target = _resolve_task(d, args)
    if isinstance(target, list):
        return target
    task = target
    prev_worker = task.assigned_worker or "unassigned"

    # d.complete_task handles board + history + drone_log + downstream
    # triggers.  Passing actor='queen' lets the audit trail distinguish
    # her calls from operator button clicks.
    ok = d.complete_task(task.id, actor="queen", resolution=resolution, verify=False)
    if not ok:
        return [
            {
                "type": "text",
                "text": (
                    f"Failed to complete #{task.number} "
                    f"(status was {task.status.value if task.status else '?'})."
                ),
            }
        ]
    from swarm.drones.log import LogCategory, SystemAction

    d.drone_log.add(
        SystemAction.OPERATOR,
        prev_worker,
        f"queen force-completed #{task.number}: {reason[:120]}",
        category=LogCategory.OPERATOR,
    )
    return [
        {
            "type": "text",
            "text": f"Force-completed #{task.number} (was on {prev_worker}).",
        }
    ]


HANDLERS = {
    "queen_reassign_task": _handle_reassign_task,
    "queen_force_complete_task": _handle_force_complete_task,
}
