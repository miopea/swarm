"""The operator's own surface must be able to move BLOCKED and HOLD tasks.

OPERATOR-REPORTED, 2026-08-06, two failures in the dashboard after the
#1277/#1278/#1279 fixes made blocked tasks visible for the first time:

  A. "I see 5 blocked tasks but no way to change their status from blocked."
  B. Assigning #1270 (unassigned, tagged hold) failed with
     "Task 'ea1f92ab8f1d' is not available (unassigned)".

BOTH ARE THE #1104 AUDIT'S PROPERTY (b) ON A SURFACE THE AUDIT DID NOT COVER.
#1268 wired an owner-preserving exit from BLOCKED to the worker and Queen MCP
surfaces. The dashboard — the operator's own surface, and the only one he uses —
got neither, so making blocked tasks visible immediately exposed that they were
also immovable. Visibility was necessary to notice it; it did not cause it.

DEFECT A HAS THREE LAYERS, and each alone would have been enough to block him:

  1. ``#tm-status`` in dashboard.html offered backlog/unassigned/assigned/
     active/done/failed and NO ``blocked``. A blocked task's real status could
     not be represented, so the select landed on ``selectedIndex = -1``, an
     empty value was submitted, and ``if new_status:`` skipped the change.
  2. ``_apply_status_change`` (web/routes/tasks.py) had no branch whose
     ``current`` was "blocked" — every branch matched assigned/active/backlog/
     done/failed — so any target chosen fell through and did nothing.
  3. ``handle_action_edit_task`` returned ``{"status": "updated"}``
     unconditionally, so the no-op reported success. This is #1159's shape
     exactly: a verb that succeeds and does nothing is worse than one that
     refuses, because the caller stops looking.

DEFECT B IS A PREDICATE USED FOR TWO DIFFERENT QUESTIONS. ``task.is_available``
documents itself as "True when the AUTO-ASSIGN DRONE is allowed to pick this
task up", and #894 deliberately excludes HOLD from it so parked work is not
auto-dispatched. ``assign_task`` then used that same predicate to gate EXPLICIT
assignment, so the mechanism that stops the drone also stopped the operator —
and HOLD became a trap no one could route out of.

The irony is load-bearing, not decorative: the task he could not assign was
#1270, whose entire subject is HOLD tasks being unreachable because a
precondition is structurally unsatisfiable for the class. It reproduced itself
on a second verb while sitting in the tracker.

WHAT MUST NOT REGRESS. The auto-assigner selects candidates through
``board.available_tasks``, which filters on ``is_available`` — a SEPARATE
mechanism from ``assign_task``'s gate. These tests pin that HOLD stays excluded
there, so the operator gaining an explicit route does not hand parked work to
the drone. Explicit is not automatic (#894, #1270).
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from swarm.tasks.board import TaskBoard
from swarm.tasks.task import TaskStatus

_DASHBOARD = Path(__file__).parent.parent / "src" / "swarm" / "web" / "templates" / "dashboard.html"


# --- defect A layer 1: the status select must represent BLOCKED -------------


def _status_options() -> list[str]:
    html = _DASHBOARD.read_text()
    block = html.split('id="tm-status"', 1)[1].split("</select>", 1)[0]
    return re.findall(r'<option value="([a-z]+)"', block)


def test_the_status_option_scan_is_honest():
    """Positive control — an empty scan would make the next test pass for the
    wrong reason, which is how an empty board once "confirmed" a truncation."""
    opts = _status_options()
    assert "unassigned" in opts and "done" in opts, f"scan broken, found {opts}"


def test_the_status_select_can_represent_a_blocked_task():
    """Layer 1. With no ``blocked`` option the select cannot show the task's own
    status: the browser reports ``selectedIndex = -1`` and submits an empty
    value, so the operator's Save silently carried no status at all."""
    assert "blocked" in _status_options(), (
        f"#tm-status has no 'blocked' option, so a blocked task's status cannot "
        f"be displayed or changed from the modal. Options: {_status_options()}"
    )


# --- defect A layers 2+3: the transition must apply, or say it did not ------


@pytest.fixture
def daemon_with_board():
    d = MagicMock()
    d.task_board = TaskBoard()
    d.blocker_store = MagicMock()
    d.blocker_store.clear_for_task.return_value = 1
    return d


def _blocked_task(d, worker="swarm"):
    t = d.task_board.create(title="waiting on the operator")
    d.task_board.assign(t.id, worker)
    d.task_board.activate(t.id)
    assert d.task_board.block_on_external(t.id, worker, "operator decision", "op#1")
    assert d.task_board.get(t.id).status == TaskStatus.BLOCKED
    return t


def test_blocked_to_assigned_applies_and_keeps_the_owner(daemon_with_board):
    """Layer 2. The transition the operator actually wants: the wait ended, put
    it back on its worker. Owner-preserving, matching #1268 — ``release`` would
    drop the owner and make him reassign it by hand."""
    from swarm.web.routes.tasks import _apply_status_change

    d = daemon_with_board
    t = _blocked_task(d)
    applied = _apply_status_change(d, t.id, "blocked", "assigned")

    after = d.task_board.get(t.id)
    assert applied is True, "the transition reported that it did nothing"
    assert after.status == TaskStatus.ASSIGNED
    assert after.assigned_worker == "swarm", "owner dropped — that is release, not unblock"
    assert after.status is not TaskStatus.DONE, "exited BLOCKED by falsifying completion"


def test_blocked_to_unassigned_applies_and_releases(daemon_with_board):
    """The other legitimate exit: give it back to the pool."""
    from swarm.web.routes.tasks import _apply_status_change

    d = daemon_with_board
    t = _blocked_task(d)
    applied = _apply_status_change(d, t.id, "blocked", "unassigned")

    after = d.task_board.get(t.id)
    assert applied is True
    assert after.status == TaskStatus.UNASSIGNED
    assert not after.assigned_worker


def test_leaving_blocked_clears_the_blocker_rows(daemon_with_board):
    """#529. ``board`` has no handle on the BlockerStore, so the caller owns it.
    Clearing the status without the rows leaves the IdleWatcher nudging about a
    blocker that is gone — and this surface must not drift from the MCP ones."""
    from swarm.web.routes.tasks import _apply_status_change

    d = daemon_with_board
    t = _blocked_task(d)
    _apply_status_change(d, t.id, "blocked", "assigned")
    d.blocker_store.clear_for_task.assert_called_once_with(t.number)
    assert not d.blocker_store.clear.called, "used the per-worker variant (#529)"


def test_an_unsupported_transition_reports_failure_rather_than_silence(daemon_with_board):
    """Layer 3, and the reason the operator could not tell it had failed.

    ``_apply_status_change`` used to return None for every unmatched pair and the
    handler answered ``{"status": "updated"}`` regardless. A refusal he can see
    beats a success he cannot check — #1159.
    """
    from swarm.web.routes.tasks import _apply_status_change

    d = daemon_with_board
    t = _blocked_task(d)
    # Nothing should route an open task straight to DONE from BLOCKED: that is
    # force_complete, which records a completion for work that is still open.
    applied = _apply_status_change(d, t.id, "blocked", "done")
    assert applied is False, "claimed to apply a transition it does not support"
    assert d.task_board.get(t.id).status == TaskStatus.BLOCKED, "refusal mutated the board"


def test_previously_working_transitions_still_work(daemon_with_board):
    """Regression guard: the branches that already existed must keep returning
    True now that the function reports its outcome.

    Asserts the DELEGATION, not the resulting board status — those branches go
    through ``d.unassign_task``, which is a MagicMock here and mutates nothing. An
    earlier version of this test checked the board status and failed for that
    reason, which would have read as a regression in the code rather than a
    mistake in the test.
    """
    from swarm.web.routes.tasks import _apply_status_change

    d = daemon_with_board
    t = d.task_board.create(title="ordinary")
    d.task_board.assign(t.id, "swarm")
    assert _apply_status_change(d, t.id, "assigned", "unassigned") is True
    d.unassign_task.assert_called_once_with(t.id)


# --- defect B: the operator may assign a HOLD task; the drone may not -------


def test_hold_task_stays_out_of_the_auto_assigner(daemon_with_board):
    """THE CONSTRAINT, asserted before the capability below it.

    #894 excludes HOLD from ``is_available``, and ``available_tasks`` is what the
    auto-assign and task-lifecycle loops read every poll. The operator gaining an
    explicit route must not put parked work back in front of the drone.
    """
    d = daemon_with_board
    t = d.task_board.create(title="parked on purpose", tags=["hold"])
    assert d.task_board.get(t.id).status == TaskStatus.UNASSIGNED
    assert t.is_on_hold, "the hold tag stopped being recognised"
    assert t.is_available is False, "HOLD re-entered the auto-assigner's candidate set"
    assert t.id not in {x.id for x in d.task_board.available_tasks}, (
        "a HOLD task is in available_tasks — the drone can now pick up parked work"
    )


def test_explicit_assignment_of_a_hold_task_is_allowed():
    """Defect B. ``is_available`` answers "may the DRONE take this?"; it was used
    to answer "may the OPERATOR route this?" too, so HOLD blocked both. The
    operator hit this assigning #1270 — the task about HOLD unreachability."""
    import asyncio

    from swarm.server.task_coordinator import TaskCoordinator

    d = MagicMock()
    d.task_board = TaskBoard()
    t = d.task_board.create(title="hold task", tags=["hold"])

    coord = TaskCoordinator(d)
    coord.check_ownership = MagicMock()
    d._require_worker = MagicMock()

    ok = asyncio.run(coord.assign_task(t.id, "swarm", actor="user", override_hold=True))
    after = d.task_board.get(t.id)
    assert ok is True
    assert after.status == TaskStatus.ASSIGNED
    assert after.assigned_worker == "swarm"


def test_automated_callers_do_not_get_the_hold_override_by_default():
    """The override must be opt-in and explicit. Defaulting it on would let the
    Queen's directive path and the proposal coordinator dispatch parked work,
    trading a routing defect for the one #894 exists to prevent."""
    import inspect

    from swarm.server.task_coordinator import TaskCoordinator

    sig = inspect.signature(TaskCoordinator.assign_task)
    param = sig.parameters.get("override_hold")
    assert param is not None, "no override_hold parameter — the gate was widened for everyone"
    assert param.default is False, (
        f"override_hold defaults to {param.default!r}; every automated caller "
        f"would silently gain the ability to dispatch HOLD tasks"
    )
