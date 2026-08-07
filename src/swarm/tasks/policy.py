"""Task write policy — the assignability rule, stated once.

WHY THIS MODULE EXISTS. "Can this task be assigned?" was answered independently in
three places: ``TaskBoard.assign``, ``TaskCoordinator.assign_task`` (which raises the
operator-facing 409), and ``/action/task/assign`` (which "normalised" the status first
so the other two would accept it). Three implementations of one rule, and every
divergence between them has been a bug:

* #894 / #1281 — ``is_available`` means "the auto-assign DRONE may take this". Using it
  to gate an OPERATOR's explicit routing answered a different question than the one
  asked, so a HOLD task could not be assigned by anyone.
* 2026-08-07 — the route worked around the same gate by calling ``approve()`` on a
  BACKLOG task, which UN-PARKED it as a side effect. The operator hit that directly:
  "when I assign a worker and click save it moves to assigned, even if it was already
  on backlog."
* the same day — relaxing the board's gate without the coordinator's turned "silently
  un-parks" into "409, cannot assign": the same bug in different clothes, because the
  second copy still refused.

``task_coordinator`` already carried a comment about exactly this ("board.assign gates
on is_available too, so relaxing only this method's check would still refuse at the
board layer"). That comment is the argument for this module: a rule that must be
relaxed in lockstep across layers should not be written down more than once.

THE POLICY RETURNS A REASON, NOT A BOOLEAN, so the refusal text is part of the rule
rather than re-derived per layer. #939 cost the Queen an hour because one layer's
message said "(not available)", which reads as though the TARGET worker is unavailable
— the target is never consulted. One rule, one explanation.
"""

from __future__ import annotations

from swarm.tasks.task import SwarmTask, TaskStatus


def assignment_refusal(task: SwarmTask, *, override_hold: bool = False) -> str | None:
    """Return why *task* cannot be assigned, or ``None`` when it can.

    ``override_hold`` marks a deliberate operator/Queen action, which may take a
    HOLD-tagged task. It does NOT widen anything else: the auto-assign drone and the
    Queen select candidates through ``board.available_tasks``, which is a separate
    predicate and is unaffected by this function.
    """
    if task.is_available:
        return None

    # BACKLOG is routable and STAYS parked — assigning it must not promote it
    # (``SwarmTask.assign`` preserves the status). Operator decision, 2026-08-07:
    # "Backlog is meant to be a backlog for tasks not to get picked up for now, but
    # should be able to carry an assignment." Safe because no dispatch path accepts
    # BACKLOG.
    if task.status == TaskStatus.BACKLOG:
        return None

    if override_hold and task.status == TaskStatus.UNASSIGNED and task.is_on_hold:
        return None

    if task.is_on_hold and not override_hold:
        return (
            f"task is on hold ({task.status.value}) — assign it explicitly from the "
            f"dashboard, which overrides the hold"
        )

    if task.status in (TaskStatus.DONE, TaskStatus.FAILED):
        return (
            f"task is {task.status.value} — closed work cannot be reassigned; reopen it "
            f"first if it genuinely needs more work"
        )

    # ASSIGNED / ACTIVE / BLOCKED all still own a worker. Releasing that owner is a
    # DIFFERENT operation (board.release), which is why this names the state rather
    # than pretending the assign itself is impossible.
    return (
        f"task is {task.status.value} and still owned by "
        f"{task.assigned_worker or 'another worker'} — release it first "
        f"(the reassign path does this for you)"
    )


def is_assignable(task: SwarmTask, *, override_hold: bool = False) -> bool:
    """Boolean form for call sites that have no use for the reason."""
    return assignment_refusal(task, override_hold=override_hold) is None


# --- status transitions -------------------------------------------------------
#
# The legal (current -> target) pairs, stated here rather than inside the dashboard's
# route module. They lived in ``swarm/web/routes/tasks.py`` with exactly one caller,
# which made arbitrary status changes a DASHBOARD-ONLY capability: any other surface —
# a Jira sync, an MCP verb, the CLI — would have to duplicate the grid or import from a
# web route. That is the same duplication that produced #1280, #1288 and the 2026-08-07
# un-parking bug, one level up.

_LEGAL_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        # park open work
        ("unassigned", "backlog"),
        ("assigned", "backlog"),
        ("active", "backlog"),
        # promote / hand to the Queen
        ("backlog", "unassigned"),
        # start work — ASSIGNED is the ONLY source (#1288): a task must be owned and
        # queued before it can be in progress.
        ("assigned", "active"),
        # back to the pool
        ("assigned", "unassigned"),
        ("active", "unassigned"),
        # close
        ("assigned", "done"),
        ("active", "done"),
        ("active", "failed"),
        # reopen
        ("done", "backlog"),
        ("done", "unassigned"),
        ("done", "assigned"),
        ("failed", "backlog"),
        ("failed", "unassigned"),
        ("failed", "assigned"),
        # leave BLOCKED — every exit also owes the #529 blocker-row cleanup, which is
        # why they are executed through one function rather than per-target.
        ("blocked", "assigned"),
        ("blocked", "unassigned"),
        ("blocked", "backlog"),
    }
)


def status_transition_refusal(current: str, target: str) -> str | None:
    """Why this status change is refused, in words the operator can act on, or None.

    #1057/#1288: "not a supported transition" tells nobody what to do instead, and the
    cells chosen by accident are exactly the ones that deserve a sentence.
    """
    if (current, target) in _LEGAL_TRANSITIONS:
        return None

    if target == "blocked":
        # DISPLAY-ONLY on purpose. The option must exist so a BLOCKED task's own status
        # can be shown in the select at all (#1280) — without it the select landed on
        # selectedIndex=-1 and submitted nothing. But blocking REQUIRES a reason and
        # this form has nowhere to collect one; a blocker with an empty reason is
        # #1057's withheld-fact shape, and #1287 showed an unrecorded cause leaves the
        # task in no operator batch at all.
        return (
            "Blocked is shown so a blocked task's status is visible, but it cannot be "
            "SET here — a blocker needs a reason, and this form has nowhere to put one. "
            "Use swarm_block_on_external / swarm_block_on_operator from the worker, or "
            "have the Queen park it."
        )
    if target == "active" and current != "assigned":
        return (
            f"In Progress means a worker is working it, so the task must be ASSIGNED "
            f"to someone first — it is {current}. Assign it, then set In Progress."
        )
    if target == "assigned":
        return (
            f"Use the 'Assign to' picker rather than the status dropdown: moving to "
            f"assigned needs a worker, and {current} → assigned has none to infer."
        )
    if current == "blocked" and target in ("done", "failed"):
        # BLOCKED -> DONE is force_complete, which records a completion for work that
        # is still open — the falsification #1268 exists to avoid.
        return (
            f"blocked → {target} would record a completion for work that is still "
            f"open. Clear the blocker first (it lands in assigned), then close it."
        )
    return f"{current} → {target} is not a supported transition."


def is_legal_transition(current: str, target: str) -> bool:
    """Boolean form for callers with no use for the reason."""
    return status_transition_refusal(current, target) is None
