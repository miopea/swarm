"""queen_unarchive_task must show WHY a task was archived, before you restore it.

THE INCIDENT THIS EXISTS FOR, AND I CAUSED IT. This tool's description used to assert
"NOTHING RECORDS WHY A TASK WAS ARCHIVED". I wrote that after checking two things — the
`tasks` row has no reason column, and no unarchive inverse existed — and generalised to
"nothing anywhere" WITHOUT QUERYING task_history, the one table designed to hold it.
`TaskManager.archive_task` has always written the reason there as `TaskAction.REMOVED`.

A zero reported without a positive control, in a tool description, where a future Queen
reads it at exactly the moment she needs the reason.

THE COST WAS NOT THE SENTENCE. #1672 was archived with a full explanation — "DUPLICATE OF
#1671, and the duplicate is mine", naming which ticket carried its criteria forward — and
was restored on a ruling made in the belief that no reason existed, a belief this tool
asserted. The false claim was then relayed into the restore reason and is now permanently
in task_history: the table that held the refutation.

Showing the reason at the moment of restoring is what makes that unrepeatable.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from swarm.mcp.queen_handlers._tasks import _archive_reason, _handle_queen_unarchive_task
from swarm.tasks.history import TaskAction, TaskEvent


def _event(action: TaskAction, detail: str) -> TaskEvent:
    return TaskEvent(timestamp=0.0, task_id="t1", action=action, actor="queen", detail=detail)


def _daemon(events):
    history = SimpleNamespace(get_events=lambda _tid, limit=50: list(events))
    return SimpleNamespace(task_history=history)


REAL_REASON = "DUPLICATE OF #1671, and the duplicate is mine. Its criteria were carried on."


# ---------------------------------------------------------------------------
# The lookup itself
# ---------------------------------------------------------------------------


def test_it_finds_the_reason_on_the_REMOVED_entry():
    d = _daemon([_event(TaskAction.CREATED, "filed"), _event(TaskAction.REMOVED, REAL_REASON)])

    assert _archive_reason(d, "t1") == REAL_REASON


def test_it_ignores_details_on_other_actions():
    """POSITIVE CONTROL the other way: a helper that returned the last detail of ANY event
    would pass the test above and report a CREATED note as the archive reason."""
    d = _daemon([_event(TaskAction.REMOVED, REAL_REASON), _event(TaskAction.EDITED, "retitled")])

    assert _archive_reason(d, "t1") == REAL_REASON


def test_the_most_recent_REMOVED_wins():
    """A task archived, restored, archived again has two. The current reason is the last."""
    d = _daemon([_event(TaskAction.REMOVED, "first time"), _event(TaskAction.REMOVED, "second")])

    assert _archive_reason(d, "t1") == "second"


def test_a_genuinely_absent_reason_returns_empty():
    """The claim I made IS true for some tasks — archive surfaces allow an empty reason.
    The defect was asserting it for ALL of them without looking."""
    d = _daemon([_event(TaskAction.REMOVED, "")])

    assert _archive_reason(d, "t1") == ""


def test_no_history_and_a_broken_history_both_degrade_to_empty():
    """A history read must never break a restore that already succeeded."""
    assert _archive_reason(SimpleNamespace(task_history=None), "t1") == ""

    def _boom(_tid, limit=50):
        raise RuntimeError("db gone")

    assert (
        _archive_reason(SimpleNamespace(task_history=SimpleNamespace(get_events=_boom)), "t") == ""
    )


# ---------------------------------------------------------------------------
# It reaches the caller — the whole point
# ---------------------------------------------------------------------------


def _full_daemon(events, task):
    board = MagicMock()
    board.find_archived.return_value = task
    board.all_tasks = []
    manager = MagicMock()
    manager.unarchive_task.return_value = True
    return SimpleNamespace(
        task_board=board,
        tasks=manager,
        task_history=SimpleNamespace(get_events=lambda _t, limit=50: list(events)),
    )


def _task():
    t = MagicMock()
    t.id = "t1"
    t.number = 1672
    t.status.value = "assigned"
    return t


def test_the_restore_output_shows_why_it_was_archived():
    d = _full_daemon([_event(TaskAction.REMOVED, REAL_REASON)], _task())

    out = _handle_queen_unarchive_task(
        d, "queen", {"number": 1672, "reason": "operator ruling: still live"}
    )[0]["text"]

    assert "WHY IT WAS ARCHIVED" in out
    assert "DUPLICATE OF #1671" in out


def test_an_absent_reason_is_reported_as_CHECKED_not_assumed():
    """The distinction that was missing. "No reason recorded" and "I did not look" are
    different claims, and only one of them is honest."""
    d = _full_daemon([_event(TaskAction.REMOVED, "")], _task())

    out = _handle_queen_unarchive_task(d, "queen", {"number": 1672, "reason": "why"})[0]["text"]

    assert "CHECKED" in out
    assert "WHY IT WAS ARCHIVED" not in out


def test_the_restore_still_happens_and_still_reports_the_status():
    """The reason is added TO the existing output, not instead of it."""
    d = _full_daemon([_event(TaskAction.REMOVED, REAL_REASON)], _task())

    out = _handle_queen_unarchive_task(d, "queen", {"number": 1672, "reason": "why"})[0]["text"]

    assert "restored to the board with status assigned" in out
    assert "Reason recorded for the restore: why" in out
    d.tasks.unarchive_task.assert_called_once()


def test_the_tool_description_no_longer_states_the_falsehood():
    """It is not enough to fix the behaviour: the sentence itself misled a Queen at the
    moment of the decision, and it is the part she reads."""
    from swarm.mcp.queen_handlers._tasks import QUEEN_UNARCHIVE_TOOL

    desc = QUEEN_UNARCHIVE_TOOL["description"]

    assert "NOTHING RECORDS WHY A TASK WAS ARCHIVED" not in desc
    assert "ARCHIVE REASON IS READ BACK TO YOU" in desc


@pytest.mark.parametrize("bad", [None, MagicMock(spec=[])])
def test_a_history_without_get_events_is_tolerated(bad):
    assert _archive_reason(SimpleNamespace(task_history=bad), "t1") == ""
