"""Handler for the ``swarm_report_blocker`` MCP tool.

Extracted from ``mcp/tools.py`` (task #518).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.drones.log import truncate_for_log
from swarm.mcp._arg_types import ReportBlockerArgs
from swarm.mcp.types import TextContent

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_report_blocker",
        "description": (
            "Declare that one of your in-progress tasks is blocked on another task and "
            "should not trigger idle-watcher nudges until the blocker clears. Call this "
            "when you have nothing to do autonomously on a ticket — e.g. 'scaffolded 60 "
            "percent, cannot proceed further until platform #245 ships the backend "
            "field'. The idle-watcher drone will skip nudges for you on that task "
            "until either (a) ``blocked_by_task`` flips to completed, or (b) a new "
            "message lands in your inbox. Re-call with the same ``task_number`` "
            "anytime to refresh the reason or reset the message-since window. You "
            "can also clear a blocker early by completing the blocked task normally."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_number": {
                    "type": "integer",
                    "description": "The display number of YOUR in-progress task that is blocked.",
                },
                "blocked_by_task": {
                    "type": "integer",
                    "description": (
                        "The display number of the task whose completion would unblock "
                        "you. The watcher auto-clears this blocker when that task's "
                        "status flips to completed."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Short human-readable explanation of the blocker so the "
                        "operator and Queen can audit it. 1-2 sentences."
                    ),
                },
            },
            "required": ["task_number", "blocked_by_task"],
            "examples": [
                {
                    "task_number": 246,
                    "blocked_by_task": 245,
                    "reason": "scaffolded UI; needs platform #245 backend field to ship",
                },
            ],
        },
    },
]


def _handle_report_blocker(
    d: SwarmDaemon, worker_name: str, args: ReportBlockerArgs
) -> list[TextContent]:
    """Persist a worker-reported blocker so the IdleWatcher can skip it.

    Task #250: workers nudged by the idle-watcher while waiting on a
    peer's dependency burned tokens replying "still blocked" every
    3 minutes. This tool gives them a first-class way to say "don't
    ping me about this one until #X completes or my inbox changes".
    """
    task_number = args.get("task_number")
    blocked_by = args.get("blocked_by_task")
    reason = (args.get("reason") or "").strip()
    if task_number is None or blocked_by is None:
        return [
            {
                "type": "text",
                "text": "Missing 'task_number' or 'blocked_by_task'.",
            }
        ]
    try:
        task_number = int(task_number)
        blocked_by = int(blocked_by)
    except (TypeError, ValueError):
        return [{"type": "text", "text": "'task_number' and 'blocked_by_task' must be integers."}]

    # Task #609: reject self-referential blockers (task blocks itself).
    # ``blocked_by`` never reaches a terminal status, so the IdleWatcher
    # auto-clear in BlockerStore.has_active_blocker never fires — the row
    # wedges the task in BLOCKED forever. And a BLOCKED task is uncloseable
    # through the normal API: complete, queen_force_complete_task, and
    # reassign all refuse it. Task #574 deadlocked exactly this way. Reject
    # at the filing moment so the deadlock can never be created.
    if task_number == blocked_by:
        return [
            {
                "type": "text",
                "text": (
                    f"Cannot file blocker — task #{task_number} cannot block itself "
                    "(blocked_by_task must be a different task). A self-referential "
                    "blocker never auto-clears and wedges the task in BLOCKED. To "
                    "suppress idle nudges during an operator-gated hold, park or "
                    "complete the task instead."
                ),
            }
        ]

    # Task #529: reject filings whose target is already in a terminal
    # status. The auto-clear in BlockerStore.has_active_blocker would
    # purge this row on the next IdleWatcher sweep anyway — but a
    # silent auto-clear left rcg-networks looping with re-filings it
    # could not see being cleared (the #526/#528 repro burned ~$51
    # in tokens). Surface the rejection AT THE FILING moment instead.
    board = getattr(d, "task_board", None)
    if board is not None:
        target = next((t for t in board.all_tasks if t.number == blocked_by), None)
        if target is not None and target.status.value in {"done", "failed", "removed"}:
            return [
                {
                    "type": "text",
                    "text": (
                        f"Cannot file blocker — task #{blocked_by} is already "
                        f"{target.status.value}. Your blocked task "
                        f"#{task_number} may need re-evaluation: check inbox "
                        f"+ task status to see what's pending now."
                    ),
                }
            ]

    store = getattr(d, "blocker_store", None)
    if store is None:
        return [{"type": "text", "text": "Blocker store unavailable on this daemon."}]

    # Task #609: reject filings that would close a blocker CYCLE (A→B→A, or
    # longer). ``blocked_by`` already waits on ``task_number`` directly or
    # transitively, so adding this edge rings every task in the loop in BLOCKED
    # with no terminal task to trigger the auto-clear — the same uncloseable
    # deadlock as a self-block, just spread across N tasks.
    if store.would_create_cycle(task_number, blocked_by):
        return [
            {
                "type": "text",
                "text": (
                    f"Cannot file blocker — #{blocked_by} already waits on "
                    f"#{task_number} (directly or transitively), so this would "
                    "create a blocker cycle that wedges every task in the loop "
                    "in BLOCKED. Resolve the upstream chain, or park/complete "
                    "instead of cross-blocking."
                ),
            }
        ]
    try:
        store.report(worker_name, task_number, blocked_by, reason=reason)
    except Exception as exc:  # defensive — DB errors shouldn't crash the handler
        return [{"type": "text", "text": f"Failed to record blocker: {exc}"}]

    from swarm.drones.log import LogCategory, SystemAction

    detail = f"#{task_number} blocked by #{blocked_by}"
    if reason:
        detail = f"{detail} — {truncate_for_log(reason, 120)}"
    d.drone_log.add(
        SystemAction.OPERATOR,
        worker_name,
        detail,
        category=LogCategory.WORKER,
    )
    return [
        {
            "type": "text",
            "text": (
                f"Blocker recorded: #{task_number} blocked by #{blocked_by}. "
                "IdleWatcher will skip nudges for this task until the blocker clears."
            ),
        }
    ]


HANDLERS = {"swarm_report_blocker": _handle_report_blocker}
