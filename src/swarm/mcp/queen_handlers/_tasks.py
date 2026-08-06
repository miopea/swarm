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
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from swarm.logging import get_logger
from swarm.mcp._arg_types import QueenForceCompleteTaskArgs, QueenReassignTaskArgs
from swarm.mcp.queen_handlers._common import _assert_queen
from swarm.mcp.types import TextContent
from swarm.tasks.history import TaskAction
from swarm.tasks.task import TaskStatus

_log = get_logger("mcp.queen.tasks")

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon
    from swarm.tasks.task import SwarmTask


TOOLS: list[dict[str, Any]] = [
    {
        "name": "queen_reassign_task",
        "description": (
            "Give a task an owner: MOVE an ASSIGNED or ACTIVE task between workers, "
            "OR ASSIGN an UNASSIGNED task (including an authority-guard / HOLD-parked "
            "one) to a worker for the first time.  Use when the original assignee can't "
            "reach the work (wrong expertise, over-loaded) and a peer is "
            "better-positioned, or when an orphaned unassigned/parked task needs an "
            "owner.  Assigning a HOLD-parked task clears the HOLD — your assignment is "
            "the endorsement.  THIS DOES MOVE A BLOCKED TASK: it releases first, and "
            "board.release accepts any holdable status (#1059) — but release DROPS THE "
            "OWNER, so the task lands on the new worker. To clear a blocker and keep "
            "the SAME owner, use queen_unblock_task (#1268).  An earlier version of "
            "this text claimed a BLOCKED task 'must be unblocked first', which "
            "contradicted the implementation below and sent a worker chasing a path "
            "that did not exist (#1237).  "
            "never consulted, so a busy target is never why this fails.  "
            "Call queen_view_worker_state on the target worker first "
            "so you're acting on current reality, not a stale assumption.  If `start` is "
            "true, the worker is immediately sent the task message; otherwise the task "
            "sits ASSIGNED for the next poll cycle."
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
            "summary noting what the worker actually did (so task_history has it).  "
            "Also the way to close a WEDGED task: it force-closes from ANY "
            "non-terminal status (including BLOCKED) and clears any blocker rows "
            "pinning the task — the only clean path out of a self-block/cycle "
            "deadlock that the normal completion API refuses."
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
    {
        "name": "queen_edit_task",
        "description": (
            "Correct a filed task's description, title, or acceptance criteria. "
            "Use this when requirements change after filing, or when a worker "
            "sends you an addendum — writing it into the task means the next "
            "reader sees the truth instead of the correction living only in a "
            "message thread. Prefer editing over re-filing: a new task loses "
            "the original's history, number and cross-references.\n\n"
            "You can edit ANY non-terminal task regardless of owner — that is "
            "oversight, and it is why you have acceptance_criteria here and "
            "workers do not: the verifier grades a completion against those, so "
            "an assignee editing its own would be self-grading. Completed and "
            "failed tasks are refused; editing closed work rewrites the record. "
            "Every edit is recorded in task history naming what changed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "number": {"type": "integer", "description": "Task number.  Preferred."},
                "task_id": {
                    "type": "string",
                    "description": "Task id.  Use if only the id is known.",
                },
                "description": {
                    "type": "string",
                    "description": (
                        "New full description. REPLACES the old one — carry over "
                        "anything from the original worth keeping."
                    ),
                },
                "title": {"type": "string", "description": "New short title."},
                "acceptance_criteria": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Replacement criteria list. The verifier grades against "
                        "these, so changing them changes how the task is judged."
                    ),
                },
            },
            "examples": [
                {
                    "number": 1059,
                    "description": "Original scope, plus: use #1013 as the acceptance fixture.",
                },
                {"number": 1070, "acceptance_criteria": ["Worker can declare a human blocker"]},
            ],
        },
    },
]


def _clear_blockers(d: SwarmDaemon, task_number: int) -> int:
    """#1059: drop BlockerStore rows for a task being released or reassigned.

    A BLOCKED task can carry rows filed by more than one worker, so this uses
    ``clear_for_task`` (all workers) rather than ``clear`` (one pair) — the
    same call the coordinator's force-complete path already makes before
    closing a wedged task. Without it a released task arrives at its new
    owner still carrying the old owner's blocker, and the IdleWatcher goes on
    suppressing nudges for a dependency that is no longer anyone's.

    Best-effort: the release itself is the load-bearing transition, and a
    store that is absent (tests, older DBs) must not fail the move.
    """
    store = getattr(d, "blocker_store", None)
    if store is None:
        return 0
    try:
        return int(store.clear_for_task(task_number))
    except Exception:
        _log.warning("failed clearing blocker rows for #%s", task_number, exc_info=True)
        return 0


def _resolve_task(d: SwarmDaemon, args: dict[str, Any]) -> SwarmTask | list[TextContent]:
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


def _fire_async(coro: Coroutine[Any, Any, None]) -> None:
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


def _why_unassignable(d: SwarmDaemon, task_id: str) -> str:
    """#1057: say WHY ``board.assign`` refused, in resolving terms.

    The gate is ``task.is_available`` — ``status == UNASSIGNED and not
    is_on_hold`` — a property of the SOURCE task. It never inspects the
    target worker. The old text was a bare "(not available)", which reads
    as though the TARGET is unavailable and sends the reader to look at
    the wrong thing entirely. It cost the Queen an hour on the theory
    that reassignment fails when the target already holds a task; the
    target's load is never consulted at all.

    This handler already tries to release an owned task first
    (``board.unassign`` above), but that requires ASSIGNED/ACTIVE — so on
    a BLOCKED task it silently no-ops and we land here still owned.
    """
    task = d.task_board.get(task_id)
    if task is None:
        return "the task no longer exists."
    status = task.status.value
    if task.status == TaskStatus.BLOCKED:
        # #1059 made BLOCKED releasable, so reaching here with one now means
        # the release step itself failed rather than the old dead-end. Do not
        # restore the previous text ("a blocked task cannot be released or
        # moved") — that is no longer true.
        return (
            f"#{task.number} is BLOCKED"
            + (f" ({task.block_reason})" if task.block_reason else "")
            + " and could not be released — unexpected; check the daemon log."
        )
    if task.is_on_hold:
        return f"#{task.number} is on HOLD — clear the hold tag before assigning it."
    if task.assigned_worker:
        return (
            f"#{task.number} is still assigned to {task.assigned_worker} and could not "
            f"be released (status={status})."
        )
    return f"#{task.number} is {status}, and only an UNASSIGNED task can be assigned."


def _handle_reassign_task(
    d: SwarmDaemon, worker_name: str, args: QueenReassignTaskArgs
) -> list[TextContent]:
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

    if task.assigned_worker:
        # #1059: release first so board.assign accepts (it checks is_available).
        # This used to call board.unassign, which requires ASSIGNED/ACTIVE — so
        # on a BLOCKED task it returned False, THE RESULT WAS DISCARDED, the
        # task stayed owned, and assign then refused with a message about
        # availability. board.release accepts any holdable status, and the
        # result is now checked instead of swallowed.
        if not d.task_board.release(task.id):
            return [
                {
                    "type": "text",
                    "text": (
                        f"Could not release #{task.number} from {prev} "
                        f"(status={task.status.value}). Nothing changed."
                    ),
                }
            ]
        _clear_blockers(d, task.number)
    elif task.is_on_hold:
        # #939: assigning an UNASSIGNED, HOLD-parked task is a plain assign +
        # endorsement — clear the HOLD so board.assign's is_available gate
        # accepts it. Orphaned authority-guard / HOLD tasks (#919/#929/#935)
        # were otherwise un-assignable by anyone: the Queen couldn't give them
        # an owner and a worker couldn't self-close them (a coordination
        # dead-zone, closed together with the #939 self-close path).
        from swarm.tasks.task import HOLD_TAGS

        kept = [t for t in task.tags if str(t).strip().lower() not in HOLD_TAGS]
        d.edit_task(task.id, tags=kept, actor="queen")
    if not d.task_board.assign(task.id, to_worker):
        return [
            {
                "type": "text",
                "text": (
                    f"Failed to assign #{task.number} to {to_worker} — "
                    f"{_why_unassignable(d, task.id)}"
                ),
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
) -> list[TextContent]:
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
    # her calls from operator button clicks.  force=True is what makes this a
    # real override: it clears any blocker rows and completes from ANY
    # non-terminal status, including BLOCKED — the wedged-task case the
    # status-gated path refuses (the #574 deadlock).
    ok = d.complete_task(task.id, actor="queen", resolution=resolution, verify=False, force=True)
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


def _handle_edit_task(d: SwarmDaemon, worker_name: str, args: dict[str, Any]) -> list[TextContent]:
    """#1060: the Queen's edit verb — description/title plus acceptance_criteria.

    She gets ``acceptance_criteria`` and workers do not, deliberately: the
    verifier drone grades a completion against those criteria, so an assignee
    editing its own would be self-grading. The Queen is oversight rather than
    the graded party, which is also how today's addenda actually arrived —
    Queen-authored, worker-applied.
    """
    err = _assert_queen(worker_name)
    if err:
        return err
    target = _resolve_task(d, args)
    if isinstance(target, list):
        return target
    task = target

    if task.status in {TaskStatus.DONE, TaskStatus.FAILED}:
        return [
            {
                "type": "text",
                "text": (
                    f"Task #{task.number} is {task.status.value} — editing closed work "
                    f"rewrites the record rather than correcting live requirements."
                ),
            }
        ]

    description = args.get("description")
    title = args.get("title")
    criteria = args.get("acceptance_criteria")
    if description is None and title is None and criteria is None:
        return [
            {
                "type": "text",
                "text": "Pass 'description', 'title' and/or 'acceptance_criteria'.",
            }
        ]
    cleaned = None
    if criteria is not None:
        cleaned = [str(c).strip() for c in criteria if str(c).strip()]

    d.edit_task(
        task.id,
        title=title,
        description=description,
        acceptance_criteria=cleaned,
        actor="queen",
    )
    changed = ", ".join(
        n
        for n, v in (
            ("title", title),
            ("description", description),
            ("acceptance_criteria", criteria),
        )
        if v is not None
    )
    return [
        {"type": "text", "text": f"Task #{task.number} updated ({changed}). Recorded in history."}
    ]


QUEEN_UNBLOCK_TOOL: dict[str, Any] = {
    "name": "queen_unblock_task",
    "description": (
        "Clear a BLOCKED task's blocker and hand it back to the SAME worker, "
        "still ASSIGNED. Use this when the thing it waited on has happened — an "
        "operator decision you relayed, an upstream task that shipped. "
        "#1268: this is the OWNER-PRESERVING exit from BLOCKED. "
        "queen_reassign_task also moves a blocked task (it releases first, which "
        "accepts any holdable status) but it DROPS the owner, so resuming with "
        "the same worker meant reassigning the task back to them. "
        "queen_force_complete_task also exits BLOCKED but records the task as "
        "DONE — never use it to escape a blocker on work that is still open. "
        "REFUSES if the task is not BLOCKED, naming its actual status."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "number": {"type": "integer", "description": "Task display number."},
            "task_id": {"type": "string", "description": "Task id (alternative to number)."},
            "reason": {
                "type": "string",
                "description": "What cleared it. Recorded to history; required for audit.",
            },
        },
        "required": ["reason"],
        "examples": [{"number": 1237, "reason": "operator chose PWA-only; build unblocked"}],
    },
}


def _handle_queen_unblock_task(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[TextContent]:
    """#1268: BLOCKED -> ASSIGNED keeping the owner, from the Queen surface.

    Shares its audit + BlockerStore clearing with the worker handler
    (``mcp/handlers/_unblock.py``) rather than reimplementing them. A Queen
    unblock that forgot the BlockerStore would reproduce #529 on one surface
    only, which is the hardest kind of gap to spot.
    """
    from swarm.mcp.handlers._unblock import record_unblock, unblock_result_text

    err = _assert_queen(worker_name)
    if err:
        return err
    reason = (args.get("reason") or "").strip()
    if not reason:
        return [{"type": "text", "text": "Missing 'reason' — unblocks must be audited."}]
    target = _resolve_task(d, args)
    if isinstance(target, list):
        return target
    task = target

    if task.status != TaskStatus.BLOCKED:
        return [
            {
                "type": "text",
                "text": (
                    f"#{task.number} is {task.status.value}, not blocked — nothing "
                    f"to unblock and nothing changed. To move an unblocked task to "
                    f"another worker use queen_reassign_task."
                ),
            }
        ]

    if not d.task_board.unblock(task.id):
        return [
            {
                "type": "text",
                "text": f"Could not unblock #{task.number} (status changed under us?).",
            }
        ]

    removed = record_unblock(d, task, "queen", reason)
    return [{"type": "text", "text": unblock_result_text(d.task_board, task, removed)}]


QUEEN_ARCHIVE_TOOL: dict[str, Any] = {
    "name": "queen_archive_task",
    "description": (
        "Remove a task from the board — ANY task, not only an unstarted one, which is "
        "what separates this from the worker's swarm_archive_task (#1298). Call this "
        "when a task should leave the board without being completed: a duplicate, a "
        "probe, or one filed in error. The task is "
        "ARCHIVED, not destroyed: its row and its full task_history are kept and it "
        "simply stops appearing. Use it for duplicates, probes and tasks filed in "
        "error. DO NOT use it to make finished work disappear — a closed task's "
        "resolution has already been served to other workers as a learning, and the "
        "correction path for that is queen_edit_task / swarm_annotate_resolution. "
        "Archiving an ACTIVE task takes live work off the board, so say why."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "number": {"type": "integer", "description": "Display number of the task."},
            "reason": {
                "type": "string",
                "description": "Why it is leaving the board — recorded in task_history.",
            },
        },
        "required": ["number", "reason"],
        "examples": [{"number": 1301, "reason": "duplicate of #1299"}],
    },
}


def _handle_queen_archive_task(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[TextContent]:
    err = _assert_queen(worker_name)
    if err:
        return err
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
                    "'reason' is required — it is the only record of why the task left the board."
                ),
            }
        ]

    task = next((t for t in board.all_tasks if t.number == number), None)
    if task is None:
        return [{"type": "text", "text": f"No task found with number #{number}."}]

    was = task.status.value
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
            history.append(task.id, TaskAction.REMOVED, actor="queen", detail=reason)
        except Exception:
            _log.warning(
                "archived task #%s but failed to record its history entry",
                number,
                exc_info=True,
            )

    return [
        {
            "type": "text",
            "text": (
                f"Task #{number} archived (was {was}) — off the board, row and "
                f"task_history kept. Reason recorded: {reason}"
            ),
        }
    ]


TOOLS.append(QUEEN_UNBLOCK_TOOL)
TOOLS.append(QUEEN_ARCHIVE_TOOL)

HANDLERS = {
    "queen_reassign_task": _handle_reassign_task,
    "queen_unblock_task": _handle_queen_unblock_task,
    "queen_force_complete_task": _handle_force_complete_task,
    "queen_edit_task": _handle_edit_task,
    "queen_archive_task": _handle_queen_archive_task,
}
