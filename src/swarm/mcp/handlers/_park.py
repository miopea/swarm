"""Handler for the ``swarm_park_task`` MCP tool.

Extracted from ``mcp/tools.py`` (task #518).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import ParkTaskArgs
from swarm.mcp.types import TextContent

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_park_task",
        "description": (
            "Hand your OWN in-progress task back to ASSIGNED with a reason — "
            "an intentional set-down, NOT a blocker. Call this the moment you "
            "stop actively working a task you still own: an operator preempt, "
            "a scope change, or you're switching to something urgent and want "
            "the board to immediately tell the truth (no daemon reload, no "
            "fabricated blocker). The task stays yours (still ASSIGNED to "
            "you) so you can resume it later. Different from "
            "``swarm_report_blocker`` (which means 'I'm waiting on an "
            "upstream task') and from ``swarm_complete_task`` (which means "
            "'done'). Pass ``task_number`` to say exactly which of your "
            "active tasks to set down; if you own only one active task you "
            "may omit it. If you own more than one and omit ``task_number`` "
            "the tool REFUSES and lists them rather than guessing — never "
            "silently parks an arbitrary task."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "Why you're setting it down (operator preempt, scope "
                        "change, pivot). 1 sentence; recorded to history + buzz."
                    ),
                },
                "task_number": {
                    "type": "integer",
                    "description": (
                        "Which of YOUR active tasks to park (its display "
                        "number). Optional only when you own exactly one "
                        "active task; required to disambiguate when you own "
                        "several. Must be owned by you and ACTIVE."
                    ),
                },
            },
            "required": ["reason"],
            "examples": [
                {"reason": "operator preempt — pivoting to urgent #405", "task_number": 401},
                {"reason": "scope changed; re-planning before continuing"},
            ],
        },
    },
]


def _handle_park_task(d: SwarmDaemon, worker_name: str, args: ParkTaskArgs) -> list[TextContent]:
    """#406/#407: park one of the caller's OWN ACTIVE tasks back to ASSIGNED.

    Only ever touches *this caller's* own tasks, so cross-worker parking
    is impossible by construction. Not a blocker — no binding is created.
    Composes with #405: the worker has no ACTIVE task right after, so the
    board is truthful immediately (no reload/reconciler).

    #407: #406 shipped with NO task argument — it parked "the" active
    task. When a worker owns >1 ACTIVE task (legal pre-#405-reload /
    un-reconciled state) that silently set down an arbitrary one (the
    2026-05-17 public-website wrong-task footgun). Now: an explicit
    ``task_number`` parks exactly that task (must be owned + ACTIVE);
    omitted parks the sole ACTIVE task iff there is exactly one; omitted
    with >1 candidate REFUSES and lists them — never a silent guess, no
    mutation on the refusal/rejection paths.
    """
    reason = str(args.get("reason") or "").strip()
    if not reason:
        return [{"type": "text", "text": "Missing 'reason' — say why you're setting it down."}]
    board = getattr(d, "task_board", None)
    if board is None:
        return [{"type": "text", "text": "Task board unavailable on this daemon."}]

    parkable = board.parkable_tasks_for_worker(worker_name)
    raw_num = args.get("task_number")

    if raw_num is not None and str(raw_num).strip() != "":
        try:
            want = int(raw_num)
        except (TypeError, ValueError):
            return [
                {
                    "type": "text",
                    "text": (
                        f"'task_number' must be a task number, got {raw_num!r}. Nothing parked."
                    ),
                }
            ]
        target = next((t for t in board.tasks_for_worker(worker_name) if t.number == want), None)
        if target is None:
            return [
                {
                    "type": "text",
                    "text": (
                        f"Task #{want} is not assigned to you (or doesn't exist) — "
                        f"you can only park your own task. Nothing changed."
                    ),
                }
            ]
        if target.id not in {t.id for t in parkable}:
            return [
                {
                    "type": "text",
                    "text": (
                        f"Task #{want} is {target.status.value}, not ACTIVE — only an "
                        f"active task can be parked. Nothing changed."
                    ),
                }
            ]
        task = target
    else:
        if not parkable:
            return [{"type": "text", "text": f"No active task to park for '{worker_name}'."}]
        if len(parkable) > 1:
            nums = ", ".join(f"#{t.number}" for t in sorted(parkable, key=lambda t: t.number))
            return [
                {
                    "type": "text",
                    "text": (
                        f"Ambiguous — you own {len(parkable)} active tasks ({nums}). "
                        f"swarm_park_task won't guess which to set down. Re-call it "
                        f"with task_number=<n>. Nothing changed."
                    ),
                }
            ]
        task = parkable[0]

    if not board.park(task.id, worker_name, reason):
        return [{"type": "text", "text": f"Could not park #{task.number} (state changed?)."}]

    from swarm.drones.log import LogCategory, SystemAction
    from swarm.tasks.history import TaskAction

    detail = f"#{task.number} parked: {reason[:120]}"
    try:
        d.drone_log.add(SystemAction.TASK_PARKED, worker_name, detail, category=LogCategory.TASK)
        if getattr(d, "task_history", None) is not None:
            d.task_history.append(
                task.id, TaskAction.UNASSIGNED, actor=worker_name, detail=f"parked: {reason}"
            )
    except Exception:
        pass  # audit best-effort — the transition already succeeded
    return [
        {
            "type": "text",
            "text": (
                f"Parked #{task.number} → ASSIGNED (still yours). Board is "
                f"truthful now — no reload needed. Resume it anytime."
            ),
        }
    ]


HANDLERS = {"swarm_park_task": _handle_park_task}
