"""Handler for ``swarm_relabel_blocker`` — move a BLOCKED task between its two causes (#1269).

Closes the #1104 audit's failing property (d). BLOCKED is reachable by two
semantically distinct causes and there was no transition between them:

* ``block_on_external`` — waiting on an upstream ARTIFACT (a release, a vendor PR)
* an operator ask — ``external_blocker_ref == AWAITING_OPERATOR_REF``, which is what
  ``is_awaiting_operator`` keys off and what the Queen batches into one set of
  operator questions instead of relaying them one at a time

A task whose cause CHANGED — the upstream shipped and now a human must decide, or the
human decided and now it waits on a release — stayed described by whichever cause
happened to be recorded first. The board then batches it wrongly, and the operator is
either asked about something that is no longer his call or never asked at all.

WHY THIS IS ONE VERB AND NOT EXIT-AND-RE-ENTER, the decision #1269's AC-3 asks to be
recorded — full reasoning in ``TaskBoard.relabel_blocker``. Short version: ``unblock``
lands the task in ASSIGNED while ``block_for_operator`` requires ACTIVE, so
re-labelling toward an operator ask would need unblock → activate → block. That
passes through two states the task was never in, mints a spurious ``STARTED`` history
row, and briefly makes it the worker's one ACTIVE task — an INV-1 interaction for what
is purely a re-description.

THE HISTORY ROW NAMES BOTH ENDS. ``relabel_blocker`` returns ``(old_ref, new_ref)``
specifically so the audit trail records the TRANSITION rather than just the
destination. #1269's AC-2 requires that, and it is the difference between "this task
is now an operator ask" and "this task stopped waiting on platform#234 and became an
operator ask" — only the second lets a reader reconstruct why the Queen's batch
changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.mcp.types import TextContent
from swarm.tasks.task import AWAITING_OPERATOR_REF, TaskStatus

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_relabel_blocker",
        "description": (
            "Change WHY one of your own BLOCKED tasks is blocked, without "
            "unblocking it. Use this when the thing you were waiting on changed "
            "KIND: the upstream release landed but now a human has to decide, or "
            "the operator answered and now you are waiting on a vendor PR. The "
            "task stays BLOCKED and stays yours — this corrects the reason, it "
            "does not resume the work (that is swarm_unblock_task). Pass "
            "``operator=true`` to say you are now waiting on a HUMAN DECISION, "
            "which is what puts the task in the operator's batch of asks; pass "
            "``watch_ref`` to say you are waiting on an artifact instead. "
            "REFUSES when the task is not blocked, is not yours, or when the "
            "cause is already what you asked for — each refusal names what "
            "would resolve it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "The NEW reason, replacing the old one — 'platform#234 "
                        "shipped, operator must pick the vendor'. 1 sentence."
                    ),
                },
                "operator": {
                    "type": "boolean",
                    "description": (
                        "True when you are now waiting on a HUMAN DECISION. "
                        "Mutually exclusive with ``watch_ref``."
                    ),
                },
                "watch_ref": {
                    "type": "string",
                    "description": (
                        "What artifact you are now waiting on ('vendor-pr#88', "
                        "'numpy 2.1 release'). Mutually exclusive with "
                        "``operator``."
                    ),
                },
                "task_number": {
                    "type": "integer",
                    "description": (
                        "Which of YOUR blocked tasks. Optional only when you own "
                        "exactly one; required to disambiguate."
                    ),
                },
            },
            "required": ["reason"],
            "examples": [
                {"reason": "platform#234 shipped; operator must choose", "operator": True},
                {"reason": "operator approved; waiting on the vendor", "watch_ref": "vendor-pr#88"},
            ],
        },
    },
]


def relabel_result_text(board: Any, task: Any, old_ref: str, new_ref: str) -> str:
    """Success text quoting the cause READ BACK from the board.

    #1159's lesson: the park handler used to assert "the board is truthful now", a
    claim the caller cannot check, and it stayed convincing for months while a
    promoter silently undid the write. A read-back can still be stale, but it is a
    measurement rather than an assertion.
    """
    after = board.get(task.id)
    status = after.status.value if after is not None else "unknown"
    awaiting = bool(after is not None and after.is_awaiting_operator)
    if old_ref == AWAITING_OPERATOR_REF:
        old = "an operator decision"
    else:
        old = old_ref or "(unrecorded)"
    new = "an operator decision" if new_ref == AWAITING_OPERATOR_REF else new_ref
    tail = (
        " It is now in the operator's batch of asks."
        if awaiting
        else " It is no longer counted as an operator ask."
    )
    return (
        f"#{task.number} re-labelled: was waiting on {old}, now waiting on {new}. "
        f"Board reads: status={status}, still yours.{tail}"
    )


def record_relabel(d: SwarmDaemon, task: Any, actor: str, old_ref: str, new_ref: str) -> None:
    """Audit the transition, naming BOTH ends (#1269 AC-2).

    Shared by the worker and Queen surfaces so they cannot drift — a Queen re-label
    that recorded only the destination would leave a different-shaped audit trail on
    one surface, which is the hardest kind of inconsistency to notice.
    """
    from swarm.drones.log import LogCategory, SystemAction
    from swarm.tasks.history import TaskAction

    def _name(ref: str) -> str:
        return "operator-decision" if ref == AWAITING_OPERATOR_REF else (ref or "unrecorded")

    detail = f"blocker re-labelled: {_name(old_ref)} -> {_name(new_ref)}"
    try:
        d.drone_log.add(
            SystemAction.TASK_BLOCKER_RELABELLED,
            actor,
            f"#{task.number} {detail}",
            category=LogCategory.TASK,
        )
        if getattr(d, "task_history", None) is not None:
            d.task_history.append(task.id, TaskAction.EDITED, actor=actor, detail=detail)
    except Exception:
        # The re-label already succeeded; a failed audit write must not make the
        # call look like it did nothing. WARNING because a transition with no
        # history row is what made #1159 hard to diagnose.
        import logging

        logging.getLogger("swarm.mcp.relabel").warning(
            "re-labelled #%s but could not record history (%s)", task.number, detail, exc_info=True
        )


def resolve_new_ref(args: dict[str, Any]) -> tuple[str | None, str | None]:
    """(new_ref, refusal). Exactly one of ``operator`` / ``watch_ref`` must be given.

    Refusing the ambiguous case rather than picking a default: silently preferring one
    would let a caller who meant "operator" record an artifact wait, and the failure
    would surface later as the operator never being asked.
    """
    operator = bool(args.get("operator") or False)
    watch_ref = str(args.get("watch_ref") or "").strip()
    if operator and watch_ref:
        return None, (
            "Pass either operator=true OR watch_ref, not both — they are the two "
            "different causes. Nothing changed."
        )
    if not operator and not watch_ref:
        return None, (
            "Say what you are now waiting on: operator=true for a human decision, "
            "or watch_ref='<artifact>' for an upstream wait. Nothing changed."
        )
    return (AWAITING_OPERATOR_REF if operator else watch_ref), None


def _handle_relabel_blocker(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[TextContent]:
    reason = str(args.get("reason") or "").strip()
    if not reason:
        return [{"type": "text", "text": "Missing 'reason' — say why the cause changed."}]
    board = getattr(d, "task_board", None)
    if board is None:
        return [{"type": "text", "text": "Task board unavailable on this daemon."}]

    new_ref, refusal = resolve_new_ref(args)
    if refusal is not None:
        return [{"type": "text", "text": refusal}]

    mine = board.tasks_for_worker(worker_name)
    blocked = [t for t in mine if t.status == TaskStatus.BLOCKED]
    raw = args.get("task_number")

    if raw is not None and str(raw).strip() != "":
        try:
            want = int(raw)
        except (TypeError, ValueError):
            return [
                {
                    "type": "text",
                    "text": f"'task_number' must be a task number, got {raw!r}. Nothing changed.",
                }
            ]
        target = next((t for t in mine if t.number == want), None)
        if target is None:
            other = next((t for t in board.all_tasks if t.number == want), None)
            owner = getattr(other, "assigned_worker", None)
            whose = f" It belongs to {owner}." if owner else ""
            queue = ", ".join(f"#{t.number}" for t in sorted(blocked, key=lambda t: t.number))
            return [
                {
                    "type": "text",
                    "text": (
                        f"#{want} is not assigned to you.{whose} Nothing changed. "
                        f"Your blocked tasks: {queue or '(none)'}."
                    ),
                }
            ]
        if target.status != TaskStatus.BLOCKED:
            return [
                {
                    "type": "text",
                    "text": (
                        f"#{want} is {target.status.value}, not blocked — there is no "
                        f"blocker to re-label, and nothing changed. To declare a NEW "
                        f"blocker use swarm_block_on_external or "
                        f"swarm_block_on_operator."
                    ),
                }
            ]
        task = target
    else:
        if not blocked:
            return [
                {
                    "type": "text",
                    "text": (
                        f"No blocked task for '{worker_name}' — you own nothing in "
                        f"BLOCKED. Check swarm_task_status."
                    ),
                }
            ]
        if len(blocked) > 1:
            nums = ", ".join(f"#{t.number}" for t in sorted(blocked, key=lambda t: t.number))
            return [
                {
                    "type": "text",
                    "text": (
                        f"Ambiguous — you own {len(blocked)} blocked tasks ({nums}). "
                        f"swarm_relabel_blocker won't guess which. Re-call it with "
                        f"task_number=<n>. Nothing changed."
                    ),
                }
            ]
        task = blocked[0]

    result = board.relabel_blocker(task.id, external_ref=new_ref, reason=reason)
    if result is None:
        current = board.get(task.id)
        same = current is not None and current.external_blocker_ref == new_ref
        if same:
            return [
                {
                    "type": "text",
                    "text": (
                        f"#{task.number} is already waiting on that — nothing to "
                        f"re-label, and nothing changed. To resume the work use "
                        f"swarm_unblock_task."
                    ),
                }
            ]
        return [
            {
                "type": "text",
                "text": f"Could not re-label #{task.number} (status changed under us?).",
            }
        ]

    old_ref, new_ref_applied = result
    record_relabel(d, task, worker_name, old_ref, new_ref_applied)
    return [{"type": "text", "text": relabel_result_text(board, task, old_ref, new_ref_applied)}]


HANDLERS = {"swarm_relabel_blocker": _handle_relabel_blocker}
