"""Handler for ``swarm_unblock_task`` — the worker-facing exit from BLOCKED (#1268).

WHAT WAS ACTUALLY MISSING, corrected from the #1104 audit's first pass. The audit
claimed BLOCKED had no reachable non-falsifying exit. That was wrong:
``board.release`` accepts BLOCKED (its only guards are DONE/FAILED and
already-ownerless) and the Queen reaches it through ``queen_reassign_task``. So
the operator was never stuck.

Two narrower gaps were real, and this closes the first:

1. **No worker-surface exit from BLOCKED at all.** The worker that declared the
   blocker could not clear it — sculpt-studio hit this on #1237.
2. **No owner-preserving exit from either surface.** ``release`` drops the owner,
   so "the wait ended, resume where you left off" meant reassigning the task back
   to the same worker.

``board.unblock`` already does the right transition (BLOCKED → ASSIGNED, owner
kept) and clears ``block_reason`` / ``external_blocker_ref``. It simply had zero
callers. This wires it to the worker surface.

LANDING IN ASSIGNED RATHER THAN ACTIVE IS DELIBERATE: it keeps INV-1 true by
construction (this can never mint a second ACTIVE task), and the worker then
asserts with ``swarm_start_task`` when it actually resumes.

THE BLOCKER ROWS ARE THIS CALLER'S JOB. ``board.release``'s docstring is explicit
that the board has no handle on the BlockerStore, so clearing the status without
clearing the rows leaves the IdleWatcher nudging about a blocker that is gone —
that is #529. Status and rows move together here or the fix is half a fix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import UnblockTaskArgs
from swarm.mcp.types import TextContent
from swarm.tasks.task import TaskStatus

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_unblock_task",
        "description": (
            "Clear the blocker on one of your OWN blocked tasks and take it "
            "back — the thing you waited for has happened. The task returns to "
            "ASSIGNED and STAYS YOURS, so this is how you resume work you had "
            "to stop. Use it after the upstream task shipped, the operator "
            "answered, or the external dependency landed. Pass ``task_number`` "
            "to say which; optional only when you own exactly one blocked task. "
            "REFUSES rather than guessing when you own several, when the task "
            "is not blocked, or when it belongs to someone else — each refusal "
            "names what would resolve it. Different from "
            "``swarm_complete_task`` (which means the WORK is done, not the "
            "WAIT) — never close a still-open task just to escape BLOCKED."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "What cleared it — 'platform #234 deployed', 'operator "
                        "approved the spend'. 1 sentence; recorded to history."
                    ),
                },
                "task_number": {
                    "type": "integer",
                    "description": (
                        "Which of YOUR blocked tasks to unblock. Optional only "
                        "when you own exactly one; required to disambiguate."
                    ),
                },
            },
            "required": ["reason"],
            "examples": [
                {"reason": "platform PR #234 deployed", "task_number": 1128},
                {"reason": "operator approved the vault deletion"},
            ],
        },
    },
]


def blocked_tasks_for_worker(board: Any, worker_name: str) -> list[Any]:
    """The caller's own BLOCKED tasks. Shared with the Queen handler."""
    return [t for t in board.tasks_for_worker(worker_name) if t.status == TaskStatus.BLOCKED]


def record_unblock(d: SwarmDaemon, task: Any, actor: str, reason: str) -> int:
    """Audit the transition and clear the blocker rows. Returns rows removed.

    Shared by both surfaces so they cannot drift — a Queen unblock that forgot
    the BlockerStore would reproduce #529 on one surface only, which is the
    hardest kind of gap to notice.
    """
    from swarm.drones.log import LogCategory, SystemAction
    from swarm.tasks.history import TaskAction

    removed = 0
    store = getattr(d, "blocker_store", None)
    if store is not None:
        try:
            # clear_for_task, NOT clear(worker, n): a BLOCKED task can carry rows
            # filed by more than one worker, and the per-worker variant would
            # leave the others behind — a stale row is exactly what kept the
            # IdleWatcher nudging in #529.
            removed = store.clear_for_task(task.number)
        except Exception:
            # The status transition already succeeded; a failure here must not
            # make the call look like it did nothing. Logged loudly because an
            # orphaned blocker row is a silent nudge-forever condition.
            import logging

            logging.getLogger("swarm.mcp.unblock").warning(
                "unblocked #%s but could not clear its blocker rows — the "
                "IdleWatcher may keep nudging (#529)",
                task.number,
                exc_info=True,
            )
    try:
        d.drone_log.add(
            SystemAction.TASK_UNBLOCKED,
            actor,
            f"#{task.number} unblocked: {reason[:120]}",
            category=LogCategory.TASK,
        )
        if getattr(d, "task_history", None) is not None:
            d.task_history.append(
                task.id, TaskAction.UNASSIGNED, actor=actor, detail=f"unblocked: {reason}"
            )
    except Exception:
        pass  # audit best-effort — the transition already succeeded
    return removed


def unblock_result_text(board: Any, task: Any, removed: int) -> str:
    """Success text quoting the status READ BACK from the board.

    #1159's lesson: the park handler used to assert "the board is truthful now",
    a claim the caller cannot check, and it stayed convincing for months while a
    promoter silently undid the write. A read-back can still be stale, but it is
    a measurement rather than an assertion.
    """
    after = board.get(task.id)
    status = after.status.value if after is not None else "unknown"
    owner = (after.assigned_worker if after is not None else None) or "nobody"
    rows = f", {removed} blocker row(s) cleared" if removed else ""
    return (
        f"Unblocked #{task.number}. Board now reads: status={status}, "
        f"owner={owner}{rows}. Start it with swarm_start_task when you resume."
    )


def _handle_unblock_task(
    d: SwarmDaemon, worker_name: str, args: UnblockTaskArgs
) -> list[TextContent]:
    reason = str(args.get("reason") or "").strip()
    if not reason:
        return [{"type": "text", "text": "Missing 'reason' — say what cleared the blocker."}]
    board = getattr(d, "task_board", None)
    if board is None:
        return [{"type": "text", "text": "Task board unavailable on this daemon."}]

    mine = board.tasks_for_worker(worker_name)
    blocked = blocked_tasks_for_worker(board, worker_name)
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
                        f"Your blocked tasks: {queue or '(none)'}. Ask the Queen "
                        f"to reassign it if it should be yours."
                    ),
                }
            ]
        if target.status != TaskStatus.BLOCKED:
            return [
                {
                    "type": "text",
                    "text": (
                        f"#{want} is {target.status.value}, not blocked — nothing to "
                        f"unblock, and nothing changed. Only a BLOCKED task can be "
                        f"unblocked; to set an active task down use swarm_park_task."
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
                        f"swarm_unblock_task won't guess which cleared. Re-call it "
                        f"with task_number=<n>. Nothing changed."
                    ),
                }
            ]
        task = blocked[0]

    if not board.unblock(task.id):
        return [
            {
                "type": "text",
                "text": f"Could not unblock #{task.number} (status changed under us?).",
            }
        ]

    removed = record_unblock(d, task, worker_name, reason)
    return [{"type": "text", "text": unblock_result_text(board, task, removed)}]


HANDLERS = {"swarm_unblock_task": _handle_unblock_task}
