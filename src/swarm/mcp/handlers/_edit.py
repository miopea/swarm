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
            "AUTHORITY: you may edit a task assigned to you — the same "
            "ownership rule as swarm_complete_task — and, as the one exception, "
            "any UNASSIGNED task tagged HOLD. An unassigned task that is NOT on "
            "hold is refused (adopt it first if it is genuinely yours), and so "
            "is a completed or failed one.\n\n"
            "THE HOLD EXCEPTION (#1270): HOLD tasks are unassigned by design — "
            "that is what stops the auto-assigner — so requiring assignment "
            "made them permanently uncorrectable, and they are the tasks whose "
            "premises rot most because they sit parked longest. Editing one "
            "does NOT adopt it: it stays unassigned, keeps its hold tag, and "
            "the auto-assigner still skips it. Correct a stale HOLD in place "
            "rather than asking the Queen to relay it.\n\n"
            "NOT editable here: acceptance_criteria. The verifier grades your "
            "completion against those, so editing them yourself would be "
            "self-grading — ask the Queen, who can. Priority, tags, "
            "dependencies and assignment are likewise out of scope; this verb "
            "corrects REQUIREMENTS, it does not move work around.\n\n"
            "Every edit is recorded in task history naming the fields changed, "
            "the values replaced, and the character delta — so a "
            "description that got shorter is visible in the audit trail "
            "and not only in the reply (#1289)."
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
                        "New full description. REPLACES the old one entirely — "
                        "anything you omit is LOST. To add to a description "
                        "without retyping it, use append_description instead. "
                        "The reply reports the character delta so a "
                        "shortening is visible immediately."
                    ),
                },
                "append_description": {
                    "type": "string",
                    "description": (
                        "Text to ADD to the end of the existing description, "
                        "separated by a blank line. Use this for 'here is what "
                        "I just learned' — you do not reproduce, and cannot "
                        "lose, the existing text. Mutually exclusive with "
                        "description."
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


def _resolve_description(
    task: Any, description: str | None, append: str | None
) -> tuple[str | None, str | None]:
    """(new_description, refusal). Split out to keep the handler under the gate.

    #1289: REPLACE and ADD are named parameters, not one moded parameter. A flag on
    ``description`` would be forgettable, and forgetting it performs a full replace —
    the exact data loss ``append_description`` exists to prevent. Supplying both is
    refused rather than resolved by preference: silently preferring one is how a caller
    who meant append loses everything.
    """
    if description is not None and append is not None:
        return None, (
            "Pass either 'description' (replaces everything) OR 'append_description' "
            "(adds to the end), not both — they are different intents and guessing "
            "which you meant risks losing the existing text. Nothing changed."
        )
    if append is None:
        return description, None
    addition = str(append).strip()
    if not addition:
        return None, "'append_description' is empty — nothing to add. Nothing changed."
    existing = task.description or ""
    # Blank-line separated so appended blocks stay readable instead of running into
    # the previous paragraph.
    return (f"{existing}\n\n{addition}" if existing else addition), None


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

    # #1270: the HOLD class is exempt from the ownership rule, because for this
    # class the rule has no owner to protect.
    #
    # HOLD tasks are UNASSIGNED BY DESIGN — that is the mechanism stopping the
    # auto-assign drone (#894). The ownership rule below exists so a worker
    # cannot rewrite ANOTHER worker's assigned work; an unassigned HOLD task has
    # no other worker, so the rule was guarding nothing here and only blocking.
    # Neither verb was individually wrong, which is why auditing either alone
    # never surfaced it — the gap existed purely in composition, and the class it
    # closed the verb for is the one whose premises rot most because HOLDs sit
    # parked longest.
    #
    # EDITING DOES NOT ADOPT: nothing below assigns, so the task stays UNASSIGNED
    # and keeps its hold tag, and the drone still skips it. "Can edit" must not
    # leak into "can start" — that is #894's incident and #1281's constraint.
    if not task.assigned_worker and not task.is_on_hold:
        return [
            {
                "type": "text",
                "text": (
                    f"Task #{number} is unassigned — swarm_edit_task only corrects a task "
                    f"assigned to you. Nothing changed. (A task tagged HOLD is editable "
                    f"while unassigned; this one is not tagged.)"
                ),
            }
        ]
    # Owner-match applies only when there IS an owner. An unassigned HOLD task
    # reached this far by design (#1270) and has no assigned_worker, so comparing
    # it against the caller would refuse every HOLD edit and re-close the class
    # through the next branch down — the fix above would have looked applied
    # while changing nothing.
    if task.assigned_worker and task.assigned_worker != worker_name:
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

    title = args.get("title")
    description, refusal = _resolve_description(
        task, args.get("description"), args.get("append_description")
    )
    if refusal is not None:
        return [{"type": "text", "text": refusal}]

    if description is None and title is None:
        return [
            {
                "type": "text",
                "text": (
                    "Pass 'description', 'append_description' and/or 'title' "
                    "— nothing to change otherwise."
                ),
            }
        ]

    before_desc = task.description or ""
    d.edit_task(
        task.id,
        title=title,
        description=description,
        actor=worker_name,
    )
    changed = ", ".join(n for n, v in (("title", title), ("description", description)) if v)
    # #1289: THE DELTA IS THE FIX. This verb replaces by default, so a caller who
    # reproduces the old text imperfectly silently shortens the record and the call
    # still reports success — #1159's shape. I lost ~2200 characters of verified
    # findings from #1274's own description that way and only noticed hours later by
    # accident. Naming the before/after makes the loss visible at the moment it
    # happens, for every caller, without them doing anything differently.
    delta = ""
    if description is not None and description != before_desc:
        diff = len(description) - len(before_desc)
        delta = f" description {len(before_desc)} → {len(description)} chars ({diff:+d})."
    return [
        {
            "type": "text",
            "text": f"Task #{number} updated ({changed}).{delta} Recorded in history.",
        }
    ]


HANDLERS = {"swarm_edit_task": _handle_edit_task}
