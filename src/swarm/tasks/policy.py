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
