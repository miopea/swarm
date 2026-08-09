"""Reconstructing how long a task was actually worked.

This is somebody's timesheet, so the arithmetic gets its own file and its own tests.
The rule throughout: where the duration cannot be determined, return None so the caller
logs NOTHING. An invented duration is worse than an absent one because it looks like a
measurement.
"""

from __future__ import annotations

from swarm.tasks.history import TaskAction, TaskEvent
from swarm.tasks.worklog import active_seconds, worklog_marker


def _ev(action: TaskAction, ts: float) -> TaskEvent:
    return TaskEvent(timestamp=ts, task_id="t1", action=action)


def test_a_simple_start_to_finish_span():
    events = [_ev(TaskAction.STARTED, 1000), _ev(TaskAction.COMPLETED, 4600)]
    assert active_seconds(events) == 3600


def test_parked_and_resumed_time_is_SUMMED():
    """THE CASE `completed_at - started_at` GETS WRONG. activate() resets started_at, so
    that subtraction reports only the final stretch — five minutes for a task that was
    worked three hours, parked, then resumed briefly."""
    events = [
        _ev(TaskAction.STARTED, 0),
        _ev(TaskAction.UNASSIGNED, 10_800),  # parked after 3h
        _ev(TaskAction.STARTED, 200_000),  # resumed two days later
        _ev(TaskAction.COMPLETED, 200_300),  # 5 more minutes
    ]
    assert active_seconds(events) == 10_800 + 300


def test_time_parked_on_a_blocker_is_NOT_counted():
    """A task blocked on someone else is not being worked. Counting the wait would bill
    days of nothing."""
    events = [
        _ev(TaskAction.STARTED, 0),
        _ev(TaskAction.BLOCKED, 600),
        _ev(TaskAction.STARTED, 500_000),
        _ev(TaskAction.COMPLETED, 500_600),
    ]
    assert active_seconds(events) == 1200


def test_a_never_started_task_returns_None_not_zero():
    """ "No record of work" and "worked for no time" are different claims, and only the
    second is safe to log."""
    events = [_ev(TaskAction.CREATED, 0), _ev(TaskAction.ASSIGNED, 10)]
    assert active_seconds(events) is None


def test_an_empty_history_returns_None():
    assert active_seconds([]) is None


def test_an_unclosed_interval_is_ignored_by_default():
    """Usually means the history is incomplete, not that the task has been running for
    a year. Ignoring under-reports; assuming would invent hours nobody worked."""
    assert active_seconds([_ev(TaskAction.STARTED, 0)]) is None


def test_an_unclosed_interval_can_be_closed_at_an_explicit_now():
    assert active_seconds([_ev(TaskAction.STARTED, 0)], now=90) == 90


def test_a_duplicate_START_takes_the_later_one():
    """Two STARTED events with no close between them means the first stretch's end was
    never written. Taking the later one under-reports, which is the safe direction."""
    events = [
        _ev(TaskAction.STARTED, 0),
        _ev(TaskAction.STARTED, 1000),
        _ev(TaskAction.COMPLETED, 1600),
    ]
    assert active_seconds(events) == 600


def test_a_close_with_no_open_is_ignored():
    """Legacy rows, or history pruned mid-task. Must not produce a negative or a
    nonsense span."""
    events = [
        _ev(TaskAction.COMPLETED, 5000),
        _ev(TaskAction.STARTED, 6000),
        _ev(TaskAction.COMPLETED, 6060),
    ]
    assert active_seconds(events) == 60


def test_a_zero_length_span_does_not_count_as_a_measurement():
    """Start and finish on the same timestamp is a bookkeeping artifact, not work."""
    assert active_seconds([_ev(TaskAction.STARTED, 100), _ev(TaskAction.COMPLETED, 100)]) is None


def test_out_of_order_timestamps_never_subtract():
    """Clock skew or a bad row must not produce negative time."""
    events = [_ev(TaskAction.STARTED, 5000), _ev(TaskAction.COMPLETED, 4000)]
    assert active_seconds(events) is None


# --- the idempotence marker ---------------------------------------------------


def test_the_marker_is_stable_for_one_completion():
    assert worklog_marker(1339, 1_700_000_000.4) == worklog_marker(1339, 1_700_000_000.9)


def test_a_reopened_and_reworked_task_gets_a_DIFFERENT_marker():
    """Keying on the task alone would suppress the second worklog, and the second
    stretch is real work that deserves logging."""
    assert worklog_marker(1339, 1_700_000_000) != worklog_marker(1339, 1_700_500_000)


def test_different_tasks_never_share_a_marker():
    assert worklog_marker(1339, 1_700_000_000) != worklog_marker(1340, 1_700_000_000)
