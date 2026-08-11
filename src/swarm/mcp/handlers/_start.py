"""Handler for the ``swarm_start_task`` MCP tool — worker-asserted ACTIVE.

See docs/specs/worker-asserted-active.md.

WHY THIS VERB EXISTS. Before it, ``ACTIVE`` was **inferred by the daemon and
never asserted by the worker**. Two callers reached ``TaskBoard.activate`` —
``start_task`` (dispatch) and ``WorkerStateTracker._promote_one_assigned`` — and
neither is the worker. The promoter picked the *most-recently-updated* ASSIGNED
task on a RESTING→BUZZING transition, so the board could say the worker was on
task B while it was actually on A. #1159 was that mechanism biting: parking a
task stamped ``updated_at``, which made it the top candidate for immediate
re-activation.

The existing machinery was never the gap. ``activate`` is already the single
chokepoint, ``_assert_no_double_active`` self-heals double-ACTIVE on the way to
disk, and two reconcilers run. All of it enforces *at most one ACTIVE per
worker*. None of it can know **which one is right**, because the only party that
knows was never asked. This verb asks it.

REFUSALS NAME WHAT RESOLVES THEM. #1057 was filed because a refusal withheld the
resolving fact, so the message text is part of the feature rather than
decoration. Nothing here mutates on a refusal path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import StartTaskArgs
from swarm.mcp.types import TextContent
from swarm.tasks.task import TaskStatus

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_start_task",
        "description": (
            "Declare that you are NOW working one of your own assigned tasks — "
            "this is how a task becomes in-progress. Call it as your first "
            "action on a task, before you start doing the work. The daemon no "
            "longer guesses which of your tasks you are on from PTY activity, "
            "so if you don't call this the board correctly shows the task as "
            "still queued rather than in progress. Pass ``task_number`` to say "
            "exactly which; if you own exactly one startable task you may omit "
            "it. REFUSES rather than guessing when you own several, when the "
            "task belongs to another worker, when it is blocked or already "
            "closed, or when you already have a task in progress — and each "
            "refusal names what would resolve it. Not the same as "
            "``swarm_report_progress`` (which reports percent within work you "
            "have already started)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_number": {
                    "type": "integer",
                    "description": (
                        "Which of YOUR tasks you are starting (its display "
                        "number). Optional only when you own exactly one "
                        "startable task; required to disambiguate otherwise."
                    ),
                },
                "unpark": {
                    "type": "boolean",
                    "description": (
                        "Set true to start a PARKED (HOLD) task, clearing the "
                        "hold as you take it. Parked means nobody should start "
                        "it by accident, so it must be said explicitly — "
                        "required only for parked tasks, and refused-with-"
                        "instructions otherwise."
                    ),
                },
            },
            "required": [],
            "examples": [{"task_number": 1104}, {}, {"task_number": 1269, "unpark": True}],
        },
    },
]


def _startable(board: Any, worker_name: str) -> list[Any]:
    """The caller's own ASSIGNED, non-parked tasks.

    ASSIGNED only. A task already ACTIVE needs no assertion, and one that is
    BLOCKED or closed is refused with a reason rather than silently filtered, so
    the caller learns why instead of seeing an empty list.
    """
    return [
        t
        for t in board.tasks_for_worker(worker_name)
        if t.status == TaskStatus.ASSIGNED and not t.is_on_hold
    ]


def _resolve_explicit(
    board: Any, mine: list[Any], startable: list[Any], raw: Any, unpark: bool = False
) -> tuple[Any | None, str | None]:
    """Resolve an explicit ``task_number`` to a task, or to a refusal string.

    Split out of the handler because it is the entire refusal ladder, and each
    rung names what would resolve it (#1057). Returns ``(task, None)`` on
    success or ``(None, message)`` on refusal — never mutates.

    ``unpark`` is the caller's explicit consent to start a parked (HOLD) task.
    """
    try:
        want = int(raw)
    except (TypeError, ValueError):
        return None, f"'task_number' must be a task number, got {raw!r}. Nothing started."

    target = next((t for t in mine if t.number == want), None)
    if target is None:
        # Look it up across the whole board so the refusal can say WHOSE it is.
        # A hasattr-guarded call to a method that does not exist would degrade
        # silently to "not yours" with no owner named — the useful half.
        owner_task = next((t for t in board.all_tasks if t.number == want), None)
        owner = getattr(owner_task, "assigned_worker", None)
        queue = ", ".join(f"#{t.number}" for t in sorted(startable, key=lambda t: t.number))
        whose = f" It belongs to {owner}." if owner else ""
        return None, (
            f"#{want} is not assigned to you.{whose} Nothing started. "
            f"Your startable queue: {queue or '(nothing)'}. Ask the Queen to "
            f"reassign it if it should be yours."
        )
    if target.is_on_hold and not unpark:
        # #1286: this used to say "re-call this to resume it deliberately" — and
        # re-calling produced the identical refusal, because nothing made a second
        # call behave differently. A refusal that names a resolving action which is
        # a provable no-op is worse than #1057's withheld fact: a caller who trusts
        # it retries forever. Now the named action exists.
        return None, (
            f"#{want} is parked (HOLD). Nothing changed. Parked means nobody should "
            f"start it by accident, so say so explicitly: re-call with "
            f"unpark=true. That clears the hold and starts it."
        )
    if target.status != TaskStatus.ASSIGNED:
        hint = {
            TaskStatus.ACTIVE: "it is already in progress",
            TaskStatus.BLOCKED: (
                "it is blocked — clear the blocker first, or close it if the block is stale"
            ),
            TaskStatus.DONE: "it is already done",
            TaskStatus.FAILED: "it is closed as failed — reopen it first",
        }.get(target.status, f"a {target.status.value} task cannot be started")
        return None, f"#{want} was not started — {hint}. Nothing changed."
    return target, None


def _handle_start_task(d: SwarmDaemon, worker_name: str, args: StartTaskArgs) -> list[TextContent]:
    board = getattr(d, "task_board", None)
    if board is None:
        return [{"type": "text", "text": "Task board unavailable on this daemon."}]

    mine = board.tasks_for_worker(worker_name)
    startable = _startable(board, worker_name)

    # Already working something? Refuse and name the exits. Switching silently is
    # how the board stopped matching reality in the first place — if the worker
    # really has moved on, saying so explicitly is the point of this whole change.
    already = [t for t in mine if t.status == TaskStatus.ACTIVE]
    if already:
        cur = already[0]
        return [
            {
                "type": "text",
                "text": (
                    f"You already have #{cur.number} in progress. Nothing changed. "
                    f"Finish it (swarm_complete_task), set it down "
                    f"(swarm_park_task) or declare a blocker "
                    f"(swarm_report_blocker) before starting another."
                ),
            }
        ]

    raw = args.get("task_number")
    unpark = bool(args.get("unpark") or False)
    if raw is not None and str(raw).strip() != "":
        task, refusal = _resolve_explicit(board, mine, startable, raw, unpark)
        if refusal is not None:
            return [{"type": "text", "text": refusal}]
        if task is None:
            # Contract violation, not a user error: _resolve_explicit promises
            # (task, None) or (None, refusal) and never (None, None). Stating it
            # here is what lets every later `task.` access be type-checked rather
            # than assumed — a silent None would AttributeError twelve lines on.
            return [
                {
                    "type": "text",
                    "text": (
                        f"Could not resolve task {raw!r} and could not say why — "
                        f"this is a bug in swarm_start_task, not something you did. "
                        f"Nothing changed."
                    ),
                }
            ]
    else:
        if not startable:
            return [
                {
                    "type": "text",
                    "text": (
                        f"No startable task for '{worker_name}' — you own no "
                        f"ASSIGNED task that isn't parked. Check swarm_task_status."
                    ),
                }
            ]
        if len(startable) > 1:
            nums = ", ".join(f"#{t.number}" for t in sorted(startable, key=lambda t: t.number))
            return [
                {
                    "type": "text",
                    "text": (
                        f"Ambiguous — you own {len(startable)} startable tasks ({nums}). "
                        f"swarm_start_task won't guess which you mean. Re-call it with "
                        f"task_number=<n>. Nothing changed."
                    ),
                }
            ]
        task = startable[0]

    # Clear the hold BEFORE activating, so the promise the refusal makes is the
    # thing that actually happens. #1286: the old text claimed "starting it will
    # un-park it" while no code path ever removed the tag, so even a caller who
    # somehow got past the refusal would have left a parked task in progress.
    unparked_tags: list[str] | None = None
    if task.is_on_hold and unpark:
        from swarm.tasks.task import HOLD_TAGS

        unparked_tags = [t for t in task.tags if str(t).strip().lower() in HOLD_TAGS]
        board.update(task.id, tags=[t for t in task.tags if t not in unparked_tags])
        task = board.get(task.id) or task

    if board.activate(task.id) is None:
        return [
            {
                "type": "text",
                "text": (
                    f"Could not start #{task.number} — the board refused the "
                    f"transition (operator-action tasks never go in progress). "
                    f"Nothing changed."
                ),
            }
        ]

    from swarm.drones.log import LogCategory, SystemAction
    from swarm.tasks.history import TaskAction

    try:
        d.drone_log.add(
            SystemAction.TASK_STARTED,
            worker_name,
            f"#{task.number} started (worker-asserted)",
            category=LogCategory.TASK,
        )
        if getattr(d, "task_history", None) is not None:
            d.task_history.append(
                task.id, TaskAction.STARTED, actor=worker_name, detail="worker-asserted start"
            )
    except Exception:
        pass  # audit best-effort — the transition already succeeded

    # Report the status READ BACK from the board, not the transition asked for.
    # #1159's park handler learned this the hard way: asserting "the board is
    # truthful now" stayed word-for-word convincing for months while a promoter
    # silently undid the write. A read-back can still be stale, but it is a
    # measurement rather than a claim.
    after = board.get(task.id)
    status = after.status.value if after is not None else "unknown"
    return [
        {
            "type": "text",
            "text": (
                f"Started #{task.number} — {task.title[:70]}. Board now reads: "
                f"status={status}. Report progress with swarm_report_progress; "
                f"close with swarm_complete_task."
            ),
        }
    ]


HANDLERS = {"swarm_start_task": _handle_start_task}
