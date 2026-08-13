"""Handler for ``swarm_annotate_resolution`` — flag a closed task's resolution (#1274).

A resolution is not an archived note. It becomes ``task.learnings``, and learnings are
recalled into future dispatches by ``playbook_ops.recall_learnings_for_task``. So a
resolution that has gone stale is actively RE-SERVED as advice, carrying a completed
task's authority, to a worker with no way to know it aged out. #1174's
``delete_branch_on_merge`` claim was true when written and wrong by the time #1267 read
it, and nothing in the record said so.

WHY THIS ANNOTATES INSTEAD OF EDITING, which is the opposite of #1270's fix. There, the
HOLD class could not be edited and the fix was to allow it. Here an edit would be
WRONG: rewriting a closed resolution destroys the record of what was actually believed
and done at the time, which is what an audit trail exists to preserve. Verified as part
of #1274's AC-1 that all three edit verbs already refuse a closed task, and that
``TaskBoard.update`` does not accept a ``resolution`` kwarg at all — so the
immutability is structural, not a policy this verb needs to respect by convention.

ANY WORKER MAY ANNOTATE ANY CLOSED TASK, deliberately. The person who discovers that
advice has expired is whoever the advice was just served to, not whoever wrote it —
gating this on ownership would put the correction path behind the one worker least
likely to be looking. That is the composition trap #1270 documents: a precondition
that is individually reasonable and, for the class that needs the verb, unsatisfiable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.drones.log import truncate_for_log
from swarm.mcp.types import TextContent
from swarm.tasks.task import TaskStatus

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_annotate_resolution",
        "description": (
            "Flag a CLOSED task's resolution as stale or wrong, so the next worker "
            "who is served it as a learning sees the caveat. Use this the moment you "
            "find that recalled advice from a past task no longer holds — the "
            "resolution text becomes a learning and is pushed into future task "
            "dispatches, so a stale one keeps being handed out as current guidance. "
            "This ADDS a note; it never rewrites the original text, because the "
            "record of what was believed at the time is worth keeping. Pass "
            "``kind='stale'`` when the claim WAS true and has expired (say what "
            "changed), or ``kind='wrong'`` when it was never true. Any worker may "
            "annotate any closed task — you do not have to own it, because whoever "
            "was just served the bad advice is who discovers it. REFUSES on an open "
            "task and names the verb that applies there instead. Pass ``corrected_title`` "
            "as well when the TITLE points at the wrong thing — that one DOES replace "
            "the title (the original is preserved and the change is recorded), because "
            "a title is a pointer nobody grades, and a permanently wrong pointer sends "
            "every future reader to the wrong layer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "number": {
                    "type": "integer",
                    "description": "Display number of the CLOSED task to annotate.",
                },
                "kind": {
                    "type": "string",
                    "enum": ["stale", "wrong"],
                    "description": (
                        "'stale' = was true, no longer (name what changed). "
                        "'wrong' = never true. The distinction matters: 'wrong' "
                        "impugns the original work, 'stale' does not."
                    ),
                },
                "note": {
                    "type": "string",
                    "description": (
                        "What a future reader needs — ideally what changed and "
                        "when. 'True until delete_branch_on_merge was enabled "
                        "2026-07-30' beats 'out of date'."
                    ),
                },
                "corrected_title": {
                    "type": "string",
                    "description": (
                        "Optional. Replaces the task's title when the original names "
                        "the wrong mechanism. The previous title is preserved in the "
                        "record and the change is written to task history. Requires "
                        "'note' like any other annotation — a retitle with no stated "
                        "reason is indistinguishable from vandalism to a later reader."
                    ),
                },
            },
            "required": ["number", "kind", "note"],
            "examples": [
                {
                    "number": 1174,
                    "kind": "stale",
                    "note": "True when written; delete_branch_on_merge was enabled later.",
                },
                {"number": 900, "kind": "wrong", "note": "The endpoint never existed."},
            ],
        },
    },
]


def _record_annotation(
    d: SwarmDaemon,
    worker_name: str,
    *,
    number: int,
    task_id: str,
    kind: str,
    note: str,
    title_before: str,
    title_after: str,
) -> None:
    """Buzz-log + task-history the annotation. Best effort — a failure here must not
    undo an annotation that already landed on the board.

    ``title_before``/``title_after`` are empty unless a retitle actually happened, and
    the caller decides that by reading the board back rather than by trusting its own
    argument (#1159: a handler claiming a write the caller cannot check).
    """
    try:
        from swarm.drones.log import LogCategory, SystemAction
        from swarm.tasks.history import TaskAction

        d.drone_log.add(
            SystemAction.TASK_RESOLUTION_ANNOTATED,
            worker_name,
            f"#{number} resolution flagged {kind}: {truncate_for_log(note, 100)}",
            category=LogCategory.TASK,
        )
        if getattr(d, "task_history", None) is None:
            return
        d.task_history.append(
            task_id,
            TaskAction.EDITED,
            actor=worker_name,
            detail=f"resolution annotated {kind}: {note[:200]}",
        )
        if title_after:
            # A SEPARATE entry naming BOTH titles, so the board's new wording can be
            # traced to what it replaced. Without the old text here the correction is
            # only visible as an unexplained difference from any message or commit
            # that quoted the original.
            d.task_history.append(
                task_id,
                TaskAction.EDITED,
                actor=worker_name,
                detail=(
                    f"title corrected: {truncate_for_log(title_before, 120)} "
                    f"-> {truncate_for_log(title_after, 120)}"
                ),
            )
    except Exception:
        import logging

        logging.getLogger("swarm.mcp.annotate").warning(
            "annotated #%s but could not record history", number, exc_info=True
        )


def _handle_annotate_resolution(
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

    kind = str(args.get("kind") or "").strip().lower()
    if kind not in board.RESOLUTION_NOTE_KINDS:
        return [
            {
                "type": "text",
                "text": (
                    f"'kind' must be 'stale' (was true, now expired) or 'wrong' "
                    f"(never true), got {kind!r}. Nothing changed. The distinction is "
                    f"kept because 'wrong' impugns the original work and 'stale' does "
                    f"not."
                ),
            }
        ]

    note = str(args.get("note") or "").strip()
    if not note:
        return [
            {
                "type": "text",
                "text": (
                    "Missing 'note' — an unexplained flag is worse than none, because "
                    "the next reader cannot tell whether it still applies. Say what "
                    "changed and when."
                ),
            }
        ]

    task = next((t for t in board.all_tasks if t.number == number), None)
    if task is None:
        return [{"type": "text", "text": f"No task found with number #{number}."}]
    if task.status not in (TaskStatus.DONE, TaskStatus.FAILED):
        return [
            {
                "type": "text",
                "text": (
                    f"#{number} is {task.status.value}, not closed — its resolution "
                    f"does not exist yet, so there is nothing to annotate and nothing "
                    f"changed. To correct a LIVE task's requirements use "
                    f"swarm_edit_task instead."
                ),
            }
        ]

    before = task.resolution
    corrected_title = str(args.get("corrected_title") or "").strip()
    title_before = task.title
    if not board.annotate_resolution(
        task.id, kind=kind, note=note, corrected_title=corrected_title
    ):
        return [{"type": "text", "text": f"Could not annotate #{number} (status changed?)."}]

    after = board.get(task.id)
    # Read back rather than assert. #1159's park handler claimed "the board is
    # truthful now" — a claim the caller cannot check — and stayed convincing for
    # months while a promoter silently undid the write.
    intact = after is not None and after.resolution == before
    # READ BACK from the board, don't infer from the argument. A caller that passed a
    # corrected_title identical to the current one changed nothing, and reporting a
    # retitle that did not happen is the same class of false claim as #1159's park.
    retitled = after is not None and after.title != title_before
    _record_annotation(
        d,
        worker_name,
        number=number,
        task_id=task.id,
        kind=kind,
        note=note,
        title_before=title_before if retitled else "",
        title_after=after.title if (retitled and after is not None) else "",
    )

    title_line = ""
    if retitled:
        title_line = (
            f" Title corrected — the board, search and learning headers now read "
            f"{after.title[:80]!r}; the original is preserved in the record."
        )
    elif corrected_title:
        title_line = " Title unchanged (the correction matched the existing title)."

    return [
        {
            "type": "text",
            "text": (
                f"#{number} resolution flagged as {kind}. Original text "
                f"{'intact' if intact else '*** CHANGED — report this'}. Future "
                f"workers served this as a learning will now see the caveat inline."
                f"{title_line}"
            ),
        }
    ]


HANDLERS = {"swarm_annotate_resolution": _handle_annotate_resolution}
