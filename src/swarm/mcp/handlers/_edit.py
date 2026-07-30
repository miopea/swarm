"""Handler for the ``swarm_edit_task`` MCP tool (task #1060).

Once a task was filed, nobody could correct its description — workers had
``swarm_create_task`` and no edit; the Queen had no edit verb either. The
failure mode is SILENT: the task keeps its stale description and whoever
picks it up works from wrong requirements, with nothing signalling that a
correction was attempted and lost.

Deliberately narrow. The underlying ``daemon.edit_task`` accepts thirteen
fields; this exposes two. ``acceptance_criteria`` is Queen-only (see
``queen_edit_task``) because the verifier drone grades completions against
it — letting the assignee rewrite its own grading criteria is self-grading.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import EditTaskArgs
from swarm.mcp.types import TextContent
from swarm.tasks.task import TaskStatus

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


# Editing the requirements of closed work rewrites the record rather than
# correcting a live task, so terminal tasks are refused.
_TERMINAL = {TaskStatus.DONE, TaskStatus.FAILED}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_edit_task",
        "description": (
            "Correct the description or title of a task ASSIGNED TO YOU. Use "
            "this when the requirements you were given turn out to be wrong, "
            "incomplete, or superseded — for example when a peer or the Queen "
            "sends you an addendum after the task was filed. Correcting the "
            "task itself means the next person to read it sees the truth, "
            "instead of the correction living only in a message thread. "
            "Prefer editing over re-filing: a new task loses the original's "
            "history, number and cross-references.\n\n"
            "AUTHORITY: you may only edit a task assigned to you — the same "
            "ownership rule as swarm_complete_task. An unassigned task is "
            "refused (adopt it first if it is genuinely yours), and so is a "
            "completed or failed one.\n\n"
            "NOT editable here: acceptance_criteria. The verifier grades your "
            "completion against those, so editing them yourself would be "
            "self-grading — ask the Queen, who can. Priority, tags, "
            "dependencies and assignment are likewise out of scope; this verb "
            "corrects REQUIREMENTS, it does not move work around.\n\n"
            "Every edit is recorded in task history naming the fields changed "
            "and the values replaced."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "number": {
                    "type": "integer",
                    "description": "Display number of the task to correct (e.g. 1059).",
                    "minimum": 1,
                },
                "description": {
                    "type": "string",
                    "description": (
                        "New full description. REPLACES the old one — include "
                        "anything from the original you still want kept."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "New short title. Omit to leave unchanged.",
                },
            },
            "required": ["number"],
            "examples": [
                {
                    "number": 1059,
                    "description": (
                        "Original scope, plus the Queen's addendum: use #1013 as the "
                        "acceptance fixture — it must be movable without a false "
                        "COMPLETED entry."
                    ),
                },
                {"number": 1057, "title": "Reassign refusal withholds the resolving fact"},
            ],
        },
    }
]


def _handle_edit_task(d: SwarmDaemon, worker_name: str, args: EditTaskArgs) -> list[TextContent]:
    if not d.task_board:
        return [{"type": "text", "text": "No task board."}]

    # Same fail-fast as swarm_complete_task: an unresolved caller identity must
    # name the real problem (the MCP URL) rather than look like an ownership
    # failure. #1045 guarantees this is a registry name or exactly "unknown".
    if worker_name == "unknown":
        return [
            {
                "type": "text",
                "text": (
                    "Cannot identify calling worker (worker_name=unknown). "
                    "swarm_edit_task requires caller identity, which the server "
                    "reads from the MCP URL. Check that .mcp.json includes "
                    "`?worker=<name>` in the swarm MCP server URL."
                ),
            }
        ]

    raw = args.get("number")
    try:
        number = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return [{"type": "text", "text": f"'number' must be a task number, got {raw!r}."}]

    task = next((t for t in d.task_board.all_tasks if t.number == number), None)
    if task is None:
        return [{"type": "text", "text": f"No task found with number #{number}."}]

    if not task.assigned_worker:
        return [
            {
                "type": "text",
                "text": (
                    f"Task #{number} is unassigned — swarm_edit_task only corrects a task "
                    f"assigned to you. Nothing changed."
                ),
            }
        ]
    if task.assigned_worker != worker_name:
        return [
            {
                "type": "text",
                "text": (
                    f"Task #{number} is not assigned to you "
                    f"(assigned_worker={task.assigned_worker}). Nothing changed."
                ),
            }
        ]
    if task.status in _TERMINAL:
        return [
            {
                "type": "text",
                "text": (
                    f"Task #{number} is {task.status.value} — editing closed work rewrites "
                    f"the record rather than correcting live requirements. Nothing changed."
                ),
            }
        ]

    description = args.get("description")
    title = args.get("title")
    if description is None and title is None:
        return [
            {
                "type": "text",
                "text": "Pass 'description' and/or 'title' — nothing to change otherwise.",
            }
        ]

    d.edit_task(
        task.id,
        title=title,
        description=description,
        actor=worker_name,
    )
    changed = ", ".join(n for n, v in (("title", title), ("description", description)) if v)
    return [{"type": "text", "text": f"Task #{number} updated ({changed}). Recorded in history."}]


HANDLERS = {"swarm_edit_task": _handle_edit_task}
