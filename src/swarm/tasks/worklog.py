"""How long a task was actually being worked, reconstructed from its history.

WHY NOT ``completed_at - started_at``. ``SwarmTask.activate`` RESETS ``started_at``
every time, so that subtraction measures only the final stretch. A task that was
started, parked for two days, and resumed for an hour would report one hour — which is
right — but a task started, worked three hours, parked, then resumed for five minutes
reports five minutes. For a description of progress that hardly matters. For a WORKLOG
it is somebody's timesheet, so it is worth reconstructing properly.

WHY UNDER-REPORTING IS THE SAFE DIRECTION. A worklog that overstates bills time nobody
worked; one that understates is merely incomplete. Where this cannot tell, it returns
None and the caller logs nothing rather than guessing — an invented duration is worse
than an absent one, because it looks like a measurement.

Pure and I/O-free on purpose: the interval arithmetic is the part most likely to be
wrong, and it should be testable against a recorded history without a database.
"""

from __future__ import annotations

from swarm.tasks.history import TaskAction, TaskEvent

# STARTED opens a stretch of real work. Every one of these ends it: the task was
# finished, abandoned, handed back, or parked on a blocker. REOPENED deliberately does
# NOT open one — a reopened task returns to ASSIGNED and only counts again once somebody
# actually starts it, which emits its own STARTED.
_OPENS = TaskAction.STARTED
_CLOSES = frozenset(
    {
        TaskAction.COMPLETED,
        TaskAction.FAILED,
        TaskAction.UNASSIGNED,
        TaskAction.BLOCKED,
        TaskAction.MIGRATED,
    }
)


def active_seconds(events: list[TaskEvent], *, now: float | None = None) -> float | None:
    """Total seconds the task spent ACTIVE, or None when it cannot be determined.

    ``events`` must be in chronological order — the order both history stores return.

    Returns None rather than 0.0 when the task was never started: "no record of work"
    and "worked for no time" are different claims, and only the second is safe to log.

    An interval left open (started, never closed) is closed at ``now`` when given, and
    otherwise ignored. Ignoring is the conservative default: an open interval usually
    means the history is incomplete, not that the task has been running since 2024.
    """
    total = 0.0
    opened_at: float | None = None
    counted = False

    for event in events:
        if event.action is _OPENS:
            # A second STARTED without an intervening close is not an error worth
            # refusing over — take the LATEST, because the earlier one is the record of
            # a stretch whose end simply was not written.
            opened_at = event.timestamp
        elif event.action in _CLOSES and opened_at is not None:
            if event.timestamp > opened_at:
                total += event.timestamp - opened_at
                counted = True
            opened_at = None

    if opened_at is not None and now is not None and now > opened_at:
        total += now - opened_at
        counted = True

    return total if counted else None


def worklog_marker(task_number: int, completed_at: float) -> str:
    """A stable identifier for one completion of one task.

    Stamped into the worklog comment so a re-fire can recognise its own earlier entry
    and skip. Keyed on the COMPLETION TIME, not just the task, on purpose: a task that
    is reopened and genuinely worked again SHOULD get a second worklog, and keying on
    the task alone would suppress it.

    Idempotence by comparison rather than by remembering: nothing new is persisted, and
    it survives a daemon restart or a database rebuild, which a local "already sent"
    flag would not.
    """
    return f"[swarm:worklog:{task_number}:{int(completed_at)}]"
