"""Every (current, target) pair the dashboard can request is decided (#1288).

OPERATOR-REPORTED TWICE, one cell apart:
  #1280 — "I see 5 blocked tasks but no way to change their status from blocked."
  #1288 — setting a task to In Progress refused: "assigned → active is not a
          supported transition" (diagnosed from the WARNING that 2026.8.6.14 added).

BOTH ARE THE SAME DEFECT: ``#tm-status`` offers a status the server cannot reach.
``_apply_status_change`` is an if/elif chain, so any pair nobody thought about returns
False silently — and before 2026.8.6.3 it did not even return False, it returned None
while the handler answered ``{"status": "updated"}``.

#1280 fixed the BLOCKED row and did not sweep for other holes; #1288 is the cell it
missed. So this file stops fixing cells and enumerates the whole grid: every status the
dropdown offers, crossed with every other, each pair either SUPPORTED or explicitly
expected to refuse. A new dropdown option or a changed branch now fails here instead of
reaching an operator. That is #1104's method applied to the web surface.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from swarm.tasks.board import TaskBoard
from swarm.tasks.task import TaskStatus

_DASHBOARD = Path(__file__).parent.parent / "src" / "swarm" / "web" / "templates" / "dashboard.html"


def _dropdown_statuses() -> list[str]:
    """The statuses the operator can actually pick, read from the markup."""
    html = _DASHBOARD.read_text()
    block = html.split('id="tm-status"', 1)[1].split("</select>", 1)[0]
    return re.findall(r'<option value="([a-z]+)"', block)


# The intended grid. (current, target) -> supported?
# Anything absent is expected to REFUSE. Written out rather than derived so that a
# behaviour change has to edit this table deliberately.
_SUPPORTED: set[tuple[str, str]] = {
    # promote / park
    ("backlog", "unassigned"),
    ("unassigned", "backlog"),
    ("assigned", "backlog"),
    ("active", "backlog"),
    # assignment lane
    ("assigned", "active"),  # #1288 — the cell that was missing
    ("assigned", "unassigned"),
    ("active", "unassigned"),
    # closing
    ("assigned", "done"),
    ("active", "done"),
    ("active", "failed"),
    # reopening
    ("done", "backlog"),
    ("done", "unassigned"),
    ("done", "assigned"),
    ("failed", "backlog"),
    ("failed", "unassigned"),
    ("failed", "assigned"),
    # leaving BLOCKED (#1280)
    ("blocked", "assigned"),
    ("blocked", "unassigned"),
    ("blocked", "backlog"),
}


def test_the_dropdown_scan_is_honest():
    """Positive control: an empty scan would make the grid vacuous."""
    opts = _dropdown_statuses()
    assert "blocked" in opts and "active" in opts and len(opts) >= 6, f"scan broken: {opts}"


def test_every_dropdown_status_is_a_real_TaskStatus():
    """A typo'd option value can never be satisfied by any branch."""
    valid = {s.value for s in TaskStatus}
    bogus = [o for o in _dropdown_statuses() if o not in valid]
    assert not bogus, f"dropdown offers non-statuses: {bogus}"


def _daemon():
    d = MagicMock()
    d.task_board = TaskBoard()
    d.blocker_store = MagicMock()
    # Route the daemon-level lifecycle proxies at the real board so the outcome is
    # observable. MagicMocks here would make every transition "succeed".
    b = d.task_board
    d.unassign_task = lambda tid: b.unassign(tid)
    d.complete_task = lambda tid, *a, **k: b.complete(tid, "done")
    d.fail_task = lambda tid, *a, **k: b.fail(tid)
    d.reopen_task = lambda tid, *a, **k: b.reopen(tid)
    d.mark_task_in_progress = lambda tid, actor="user": b.activate(tid) is not None
    return d


def _put_in(board, status: str):
    """Drive a fresh task into `status` using only real board verbs."""
    t = board.create(title=f"in {status}")
    if status == "backlog":
        board.demote_to_backlog(t.id)
    elif status == "unassigned":
        pass
    elif status == "assigned":
        board.assign(t.id, "alice")
    elif status == "active":
        board.assign(t.id, "alice")
        board.activate(t.id)
    elif status == "blocked":
        board.assign(t.id, "alice")
        board.activate(t.id)
        board.block_on_external(t.id, "alice", "upstream", "x#1")
    elif status == "done":
        board.assign(t.id, "alice")
        board.complete(t.id, "done")
    elif status == "failed":
        board.assign(t.id, "alice")
        board.fail(t.id)
    else:  # pragma: no cover
        raise AssertionError(status)
    assert board.get(t.id).status.value == status, f"fixture failed for {status}"
    return t


_PAIRS = [
    (c, t)
    for c in ("backlog", "unassigned", "assigned", "active", "blocked", "done", "failed")
    for t in ("backlog", "unassigned", "assigned", "active", "blocked", "done", "failed")
    if c != t
]


@pytest.mark.parametrize(("current", "target"), _PAIRS)
def test_every_pair_matches_the_intended_grid(current, target):
    """THE SWEEP. 42 ordered pairs; each is supported or refused, per _SUPPORTED.

    A pair that changes behaviour fails here, which is the property #1280 and #1288
    both lacked — each was found by an operator instead.
    """
    from swarm.web.routes.tasks import _apply_status_change

    d = _daemon()
    t = _put_in(d.task_board, current)

    applied = _apply_status_change(d, t.id, current, target)
    expected = (current, target) in _SUPPORTED

    assert applied is expected, (
        f"{current} → {target}: _apply_status_change returned {applied}, grid says "
        f"{expected}. Either implement the pair or update _SUPPORTED deliberately."
    )


# `blocked` is offered in the dropdown for DISPLAY ONLY. #1280 added the option so a
# blocked task's own status could be shown at all (without it the select landed on
# selectedIndex=-1 and submitted an empty value). It is deliberately not SETTABLE:
# blocking requires a reason and the form has nowhere to collect one, and #1287 showed
# that a blocker with an unrecorded cause lands in no operator batch.
_DISPLAY_ONLY = {"blocked"}


def test_the_grid_covers_the_dropdown():
    """Every status the operator can PICK is either reachable or explicitly
    display-only. This test FOUND a third instance of #1280/#1288's shape — the
    dropdown offered `blocked` with nothing reaching it — which is why the exemption
    below is a named set with a reason rather than a loosened assertion."""
    targets = {t for _c, t in _SUPPORTED} | _DISPLAY_ONLY
    unreachable = [o for o in _dropdown_statuses() if o not in targets]
    assert not unreachable, (
        f"the dropdown offers {unreachable} but no supported transition reaches "
        f"them, so selecting one can only ever fail"
    )


@pytest.mark.parametrize("current", ["unassigned", "assigned", "active", "done", "failed"])
def test_selecting_blocked_refuses_with_an_actionable_reason(current):
    """A display-only option must refuse in words the operator can act on. #1057: a
    refusal that withholds the resolving fact is the defect, not the refusal."""
    from swarm.web.routes.tasks import _unsupported_reason

    reason = _unsupported_reason(current, "blocked")
    assert "cannot be SET here" in reason
    assert "swarm_block_on_external" in reason, "does not name the verb that DOES block"
    assert "reason" in reason.lower(), "does not say WHY it is refused"


def test_marking_in_progress_does_not_redispatch_to_the_pty():
    """#1288's second criterion. sculpt-studio was ALREADY working #1255 — the board
    was wrong, not the work. Re-sending would paste the prompt a second time.

    Asserts against the real daemon method, not the test double above.
    """
    from swarm.server.daemon import SwarmDaemon

    d = SwarmDaemon.__new__(SwarmDaemon)
    board = TaskBoard()
    d.task_board = board  # type: ignore[misc]
    coord = MagicMock()
    coord._activate_with_history.return_value = True
    d.tasks_coord = coord  # type: ignore[misc]
    d.send_to_worker = MagicMock()  # type: ignore[misc]

    t = _put_in(board, "assigned")
    assert d.mark_task_in_progress(t.id) is True
    coord._activate_with_history.assert_called_once_with(t.id, "alice", "user")
    assert not d.send_to_worker.called, "marking in progress re-sent the task to the PTY"


def test_marking_in_progress_refuses_an_ownerless_task():
    """ACTIVE means 'this worker is working it', so an ownerless ACTIVE task is a
    claim about nobody."""
    from swarm.server.daemon import SwarmDaemon

    d = SwarmDaemon.__new__(SwarmDaemon)
    board = TaskBoard()
    d.task_board = board  # type: ignore[misc]
    d.tasks_coord = MagicMock()  # type: ignore[misc]

    t = board.create(title="ownerless")
    assert d.mark_task_in_progress(t.id) is False
    assert not d.tasks_coord._activate_with_history.called


def test_no_new_activate_caller_was_added():
    """#1288 must not raise the activate() caller count. The property-(f) test pins it
    at 2 because a caller that activates without writing history is what made #1159
    undiagnosable — so this routes through _activate_with_history instead."""
    import inspect

    from swarm.server.daemon import SwarmDaemon

    src = inspect.getsource(SwarmDaemon.mark_task_in_progress)
    assert "_activate_with_history" in src
    assert ".activate(" not in src, "mark_task_in_progress calls board.activate directly"
