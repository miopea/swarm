"""Handler for ``swarm_archive_task`` — remove your own unstarted task (#1298).

THE GAP THIS CLOSES. Deleting a task was reachable from the dashboard and from nowhere
else: 0 of 22 worker verbs, 0 of 17 Queen verbs, 0 CLI actions, against a
``DELETE /api/tasks/{id}`` route that has existed all along. A worker that filed a task
by mistake, in duplicate, or purely as a probe had exactly two options — complete it
with a resolution that is a lie, or leave it on the board forever. Both poison the
record, because resolutions become learnings and are re-served to future workers as
advice.

That asymmetry is the class this repo keeps re-finding: an operation that exists on one
surface and silently does not on the others (#1288 In Progress, #1286 parked-start,
#1280 blocked exits, #1270/#1281 the HOLD class). Every previous instance was found by
the operator rather than by looking.

IT ARCHIVES, IT DOES NOT DELETE. ``task_history.task_id`` is
``REFERENCES tasks(id) ON DELETE CASCADE``, so a hard delete destroys every history row
for the task. Archiving stamps ``tasks.archived_at`` and drops the task from the board's
memory, so the row and its history survive and every existing query omits it without a
single call site changing.

TWO PRECONDITIONS, both deliberate, neither negotiable by argument at the call site:

* **Yours.** A worker may archive only a task assigned to itself. Erasing another
  worker's work from the board is not a capability any worker needs, and no other verb
  it holds is destructive to shared state.
* **Unstarted.** ACTIVE is refused, and so is anything terminal. Archiving work that is
  under way loses the fact that it was under way; archiving a CLOSED task erases a
  resolution that may already have been served as a learning. Closed records are
  corrected with ``swarm_annotate_resolution`` (#1274), never removed.

The operator chose this shape over Queen-only and over exposing nothing at all
(2026-08-06). The Queen gets the unrestricted form separately as
``queen_archive_task``; deliberately NOT via this handler with a role check, because a
verb whose authority depends on who calls it is how #1281's ``is_available`` ended up
gating two different questions with one predicate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.mcp.types import TextContent
from swarm.tasks.task import TaskStatus

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


# Statuses a worker may archive its own task from. ACTIVE and the terminal states are
# excluded on purpose — see the module docstring.
_ARCHIVABLE = {TaskStatus.BACKLOG, TaskStatus.UNASSIGNED, TaskStatus.ASSIGNED}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_archive_task",
        "description": (
            "Remove one of YOUR OWN unstarted tasks from the board. Call this WHEN "
            "you have filed a task by mistake, in duplicate, or as a throwaway probe "
            "and it should simply leave the board. Use it instead "
            "of completing it with an invented resolution: resolutions become "
            "learnings and are re-served to future workers as advice, so closing a "
            "task that was never real puts a lie into that record. The task is "
            "ARCHIVED, not destroyed: its row and its full task_history are kept and "
            "it simply stops appearing on the board. REFUSES a task that is not "
            "assigned to you, one that is in progress, and one that is already "
            "closed — a closed task's resolution is corrected with "
            "swarm_annotate_resolution, never removed. Each refusal names what would "
            "resolve it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "number": {
                    "type": "integer",
                    "description": "Display number of YOUR unstarted task to archive.",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Why it is being removed — recorded in task_history. "
                        "'duplicate of #1290' beats 'not needed'."
                    ),
                },
            },
            "required": ["number", "reason"],
            "examples": [
                {"number": 1296, "reason": "throwaway probe for the #1294 live-update test"},
                {"number": 1301, "reason": "duplicate of #1299, filed twice by mistake"},
            ],
        },
    },
]


def _handle_archive_task(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[TextContent]:
    board = getattr(d, "task_board", None)
    if board is None:
        return [{"type": "text", "text": "Task board unavailable on this daemon."}]

    raw = args.get("number")
    try:
        number = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return [{"type": "text", "text": f"'number' must be a task number, got {raw!r}."}]

    reason = str(args.get("reason") or "").strip()
    if not reason:
        return [
            {
                "type": "text",
                "text": (
                    "'reason' is required — it is the only record of why the task left "
                    "the board. Name the cause, e.g. 'duplicate of #1290'."
                ),
            }
        ]

    task = next((t for t in board.all_tasks if t.number == number), None)
    if task is None:
        return [{"type": "text", "text": f"No task found with number #{number}."}]

    if task.assigned_worker != worker_name:
        owner = task.assigned_worker or "nobody"
        return [
            {
                "type": "text",
                "text": (
                    f"Task #{number} is not yours (assigned_worker={owner}), and a "
                    f"worker may only archive its own task. Ask the Queen to archive "
                    f"it (queen_archive_task) if it genuinely should leave the board."
                ),
            }
        ]

    if task.status not in _ARCHIVABLE:
        if task.status is TaskStatus.ACTIVE:
            hint = (
                "It is in progress. Park it (swarm_park_task) or finish it "
                "(swarm_complete_task) — archiving live work loses the fact that it "
                "was under way."
            )
        elif task.status in (TaskStatus.DONE, TaskStatus.FAILED):
            hint = (
                "It is closed, and its resolution may already have been served to "
                "other workers as a learning. Correct it with "
                "swarm_annotate_resolution instead — that adds a caveat without "
                "destroying the record."
            )
        else:
            hint = (
                "Clear the blocker first (swarm_unblock_task), then archive it if it "
                "is still not wanted."
            )
        return [
            {
                "type": "text",
                "text": f"Task #{number} is {task.status.value} and cannot be archived. {hint}",
            }
        ]

    if not board.archive(task.id):
        return [
            {
                "type": "text",
                "text": (
                    f"Task #{number} could not be archived — the board reported no "
                    f"change and nothing was modified."
                ),
            }
        ]

    history = getattr(d, "task_history", None)
    if history is not None:
        try:
            from swarm.tasks.history import TaskAction

            history.append(task.id, TaskAction.REMOVED, actor=worker_name, detail=reason)
        except Exception:
            # The archive already succeeded and is durable; a history failure must not
            # be reported as an archive failure. Logged rather than swallowed silently.
            from swarm.logging import get_logger

            get_logger("mcp.archive").warning(
                "archived task #%s but failed to record its history entry",
                number,
                exc_info=True,
            )

    return [
        {
            "type": "text",
            "text": (
                f"Task #{number} archived — off the board, row and task_history kept. "
                f"Reason recorded: {reason}"
            ),
        }
    ]


HANDLERS = {"swarm_archive_task": _handle_archive_task}
