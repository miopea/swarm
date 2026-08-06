"""BLOCKED's two causes must be interchangeable in place (#1269).

Closes the #1104 audit's failing property (d): BLOCKED is reachable by two
semantically distinct causes with no transition between them.

  * ``block_on_external`` — waiting on an upstream ARTIFACT (a release, a vendor PR)
  * ``block_for_operator`` — waiting on a HUMAN DECISION

The board already treats them differently: ``is_awaiting_operator`` keys off
``external_blocker_ref == AWAITING_OPERATOR_REF``, and the Queen batches those into
one set of operator asks rather than relaying them one at a time. But nothing could
move a task between them, so a task whose cause CHANGED — the upstream shipped and
now it needs an operator decision, or the operator decided and now it waits on a
release — stayed described by whichever cause happened to be recorded first.

DECISION (AC-3), one verb in place rather than exit-and-re-enter via #1268's
``unblock``, and the reasoning is in ``TaskBoard.relabel_blocker``'s docstring. The
short version: re-entry is not available for both causes, because ``unblock`` lands
in ASSIGNED while ``block_for_operator`` requires ACTIVE — so re-labelling toward
operator-decision would need unblock → activate → block, passing through two states
the task was never in, minting a spurious STARTED history row, and briefly making it
the worker's one ACTIVE task. That is an INV-1 interaction for what is purely a
re-description.
"""

from __future__ import annotations

import pytest

from swarm.tasks.board import TaskBoard
from swarm.tasks.task import AWAITING_OPERATOR_REF, TaskStatus


@pytest.fixture
def board():
    return TaskBoard()


def _blocked_on_artifact(board, ref="platform#234", reason="waiting on the release"):
    t = board.create(title="waits on upstream")
    board.assign(t.id, "swarm")
    board.activate(t.id)
    assert board.block_on_external(t.id, "swarm", ref, reason)
    assert board.get(t.id).status == TaskStatus.BLOCKED
    assert not board.get(t.id).is_awaiting_operator
    return t


def _blocked_on_operator(board, reason="needs the operator to choose"):
    """A genuinely awaiting-operator task.

    Goes through ``block_on_external`` with the ``AWAITING_OPERATOR_REF`` sentinel —
    the route the WORKER verb ``swarm_block_on_operator`` takes
    (``_block_external.py:288``). NOT through ``board.block_for_operator``, which
    despite its name calls ``task.block(reason)`` with no ref and therefore leaves
    ``is_awaiting_operator`` FALSE (see
    ``test_block_for_operator_does_not_set_the_awaiting_operator_sentinel`` below).
    An earlier version of this fixture used that verb, which made the reverse
    direction start False and end False — passing while testing nothing.
    """
    t = board.create(title="waits on a human")
    board.assign(t.id, "swarm")
    board.activate(t.id)
    assert board.block_on_external(t.id, "swarm", AWAITING_OPERATOR_REF, reason)
    assert board.get(t.id).status == TaskStatus.BLOCKED
    assert board.get(t.id).is_awaiting_operator is True, "fixture is not awaiting-operator"
    return t


# --- AC-1: both directions, never through DONE or FAILED -------------------


def test_artifact_to_operator_decision(board):
    t = _blocked_on_artifact(board)
    result = board.relabel_blocker(
        t.id, external_ref=AWAITING_OPERATOR_REF, reason="upstream shipped; operator must choose"
    )

    after = board.get(t.id)
    assert result is not None, "re-label refused"
    assert after.status == TaskStatus.BLOCKED, "left BLOCKED — this is a re-description"
    assert after.is_awaiting_operator is True, "the Queen will not see this as an operator ask"
    assert after.assigned_worker == "swarm", "owner dropped"
    assert not after.resolution, "a resolution was written for open work"


def test_operator_decision_to_artifact(board):
    """The reverse direction, which is the one a two-verb design tends to forget."""
    t = _blocked_on_operator(board)
    result = board.relabel_blocker(
        t.id, external_ref="vendor-pr#88", reason="operator decided; now waiting on the vendor"
    )

    after = board.get(t.id)
    assert result is not None
    assert after.status == TaskStatus.BLOCKED
    assert after.is_awaiting_operator is False, "still counted as an operator ask"
    assert after.external_blocker_ref == "vendor-pr#88"


@pytest.mark.parametrize("start", ["artifact", "operator"])
def test_relabelling_never_passes_through_a_terminal_status(board, start):
    """AC-1's real constraint. ``force_complete`` also 'changes' a blocked task by
    recording DONE for open work — the falsification #1268 exists to avoid."""
    t = _blocked_on_artifact(board) if start == "artifact" else _blocked_on_operator(board)
    target = "x#1" if start == "operator" else AWAITING_OPERATOR_REF

    board.relabel_blocker(t.id, external_ref=target, reason="cause changed")

    after = board.get(t.id)
    assert after.status not in (TaskStatus.DONE, TaskStatus.FAILED)
    assert after.completed_at is None


# --- AC-5: the binding and the reason move together ------------------------


def test_the_reason_is_updated_with_the_binding(board):
    """Leaving ``block_reason`` describing the old cause is worse than either field
    being stale alone: the machine-readable cause and the human explanation would
    disagree, and each corroborates the other to a reader."""
    t = _blocked_on_artifact(board, reason="waiting on platform#234 to land")
    board.relabel_blocker(
        t.id, external_ref=AWAITING_OPERATOR_REF, reason="operator must pick a vendor"
    )
    after = board.get(t.id)
    assert after.block_reason == "operator must pick a vendor"
    assert "platform#234" not in after.block_reason


# --- refusals mutate nothing ------------------------------------------------


def test_a_task_that_is_not_blocked_is_refused(board):
    t = board.create(title="fine")
    board.assign(t.id, "swarm")
    assert board.relabel_blocker(t.id, external_ref=AWAITING_OPERATOR_REF, reason="x") is None
    assert board.get(t.id).status == TaskStatus.ASSIGNED


def test_a_missing_task_is_refused(board):
    assert board.relabel_blocker("no-such-id", external_ref="x", reason="y") is None


def test_relabelling_to_the_same_cause_is_a_no_op(board):
    """No history row and no websocket frame for a change that did not happen. Every
    connected dashboard re-fetches on the board's change event, so a broadcast for a
    non-change is real wasted work."""
    t = _blocked_on_artifact(board, ref="platform#234")
    events: list[int] = []
    board.on_change(lambda: events.append(1))

    assert board.relabel_blocker(t.id, external_ref="platform#234", reason="same") is None
    assert not events, "a no-op re-label broadcast anyway"


def test_the_old_and_new_cause_are_both_returned(board):
    """AC-2's input: the caller needs both to write a history row naming the
    transition, rather than just the destination."""
    t = _blocked_on_artifact(board, ref="platform#234")
    result = board.relabel_blocker(t.id, external_ref=AWAITING_OPERATOR_REF, reason="r")
    assert result == ("platform#234", AWAITING_OPERATOR_REF)


# --- AC-4: block_for_operator's precondition is NOT relaxed ----------------


def test_block_for_operator_is_still_active_only(board):
    """AC-4, asserted here as well as in test_status_exit_reachability.py, because
    the tempting way to implement #1269 is to relax this precondition so a BLOCKED
    task can be re-blocked. That would break the Queen's auto-park semantics, where
    'no longer ACTIVE' legitimately means the stall resolved."""
    t = board.create(title="assigned only")
    board.assign(t.id, "swarm")
    assert board.get(t.id).status == TaskStatus.ASSIGNED

    assert board.block_for_operator(t.id, "nope") is False, (
        "block_for_operator now accepts ASSIGNED — its ACTIVE-only precondition was "
        "relaxed to implement the re-label, which is a regression dressed as a fix"
    )
    assert board.get(t.id).status == TaskStatus.ASSIGNED


def test_relabelling_broadcasts_so_the_dashboard_sees_it(board):
    """#1275's lesson: a mutation that persists without notifying is invisible to
    every dashboard until the operator clicks something."""
    t = _blocked_on_artifact(board)
    events: list[int] = []
    board.on_change(lambda: events.append(1))

    board.relabel_blocker(t.id, external_ref=AWAITING_OPERATOR_REF, reason="r")
    assert events, "re-label persisted without firing a change event"


def test_block_for_operator_registers_as_an_operator_ask(board):
    """#1287, OPERATOR DECISION 2026-08-06 — changed deliberately, not incidentally.

    This test previously asserted the OPPOSITE and existed to fail if anyone changed
    it, precisely so the change would be a decision rather than a drift. It failed;
    this is the revisit it demanded.

    ``block_for_operator`` used to call ``task.block(reason)`` with no ref, so the
    verb NAMED for operator blocking left ``is_awaiting_operator`` FALSE — producing
    tasks that were operator-blocked in substance and appeared in no batch at all.
    The operator was never asked. Same failure #1070 created the sentinel to fix, on
    a different entry point.

    THE OPERATOR'S REASONING, verbatim: "if we park it either is me telling the queen
    or I need to know." Both callers match — ``server/proposals.py`` is an
    operator-APPROVED park proposal (he told the Queen), and ``server/daemon.py``'s
    #762 token-ceiling governor is the case he needs to know about. So both belong in
    the batch.

    AND THE TWO ARE DELIBERATELY NOT DISTINGUISHED. #1287 asked whether "the Queen
    parked a stalled worker" should stay separable from "a worker asked a question".
    The operator's answer collapses them: both need the same outcome, which is
    reaching him. Recorded here rather than only in the task, because this is where
    someone would try to re-split them.

    VOLUME, measured before deciding: 39 TASK_PARKED and 46 PARK_PROPOSED entries
    across a 1243-task board, so this adds a trickle to the batch, not a flood.
    """
    t = board.create(title="queen auto-park")
    board.assign(t.id, "swarm")
    board.activate(t.id)
    assert board.block_for_operator(t.id, "stalled on a human decision")

    after = board.get(t.id)
    assert after.status == TaskStatus.BLOCKED
    assert after.external_blocker_ref == AWAITING_OPERATOR_REF
    assert after.is_awaiting_operator is True, (
        "a Queen auto-park is not in the operator's batch — he is not being asked"
    )
    assert after.assigned_worker == "swarm", "the park dropped the owner"
    assert after.block_reason == "stalled on a human decision", "the reason was lost"


def test_a_queen_autopark_can_be_relabelled_to_an_artifact_wait(board):
    """The reverse direction, now that an auto-park starts as an operator ask. If the
    operator answers and the task turns out to be waiting on a release instead, it can
    be re-labelled in place — and it must LEAVE the batch, or he keeps being asked
    about something he has already settled."""
    t = board.create(title="queen auto-park")
    board.assign(t.id, "swarm")
    board.activate(t.id)
    board.block_for_operator(t.id, "over token ceiling")
    assert board.get(t.id).is_awaiting_operator is True

    board.relabel_blocker(
        t.id, external_ref="vendor-pr#88", reason="operator raised the ceiling; waiting on vendor"
    )

    after = board.get(t.id)
    assert after.is_awaiting_operator is False, "still in the batch after being settled"
    assert after.status == TaskStatus.BLOCKED
    assert after.assigned_worker == "swarm"


# --- the MCP surface, and AC-2's history row -------------------------------


def _daemon_with(board):
    from unittest.mock import MagicMock

    d = MagicMock()
    d.task_board = board
    d.drone_log = MagicMock()
    d.task_history = MagicMock()
    return d


def _text(result) -> str:
    return " ".join(r["text"] for r in result)


def test_worker_verb_relabels_and_records_both_ends(board):
    """AC-2: the history detail must name the OLD and NEW cause, not just the
    destination. "became an operator ask" does not let a reader reconstruct why the
    Queen's batch changed; "stopped waiting on platform#234 and became an operator
    ask" does."""
    from swarm.mcp.handlers._relabel import _handle_relabel_blocker

    t = _blocked_on_artifact(board, ref="platform#234")
    d = _daemon_with(board)

    out = _text(
        _handle_relabel_blocker(
            d, "swarm", {"reason": "platform#234 shipped; operator must choose", "operator": True}
        )
    )

    assert board.get(t.id).is_awaiting_operator is True
    assert "platform#234" in out and "operator decision" in out

    assert d.task_history.append.called, "no history row for the transition"
    detail = d.task_history.append.call_args.kwargs.get("detail", "")
    assert "platform#234" in detail, f"history does not name the OLD cause: {detail!r}"
    assert "operator-decision" in detail, f"history does not name the NEW cause: {detail!r}"
    assert d.drone_log.add.called, "no buzz-log entry"


def test_worker_verb_refuses_both_operator_and_watch_ref(board):
    """Ambiguity is refused rather than defaulted: silently preferring one would let a
    caller who meant 'operator' record an artifact wait, and the failure would surface
    later as the operator never being asked."""
    from swarm.mcp.handlers._relabel import _handle_relabel_blocker

    t = _blocked_on_artifact(board)
    d = _daemon_with(board)
    out = _text(
        _handle_relabel_blocker(
            d, "swarm", {"reason": "x", "operator": True, "watch_ref": "vendor#1"}
        )
    )
    assert "not both" in out
    assert board.get(t.id).external_blocker_ref == "platform#234", "refusal mutated"


def test_worker_verb_refuses_neither_operator_nor_watch_ref(board):
    from swarm.mcp.handlers._relabel import _handle_relabel_blocker

    _blocked_on_artifact(board)
    d = _daemon_with(board)
    out = _text(_handle_relabel_blocker(d, "swarm", {"reason": "x"}))
    assert "operator=true" in out and "watch_ref" in out


def test_worker_verb_refuses_a_task_that_is_not_blocked(board):
    from swarm.mcp.handlers._relabel import _handle_relabel_blocker

    t = board.create(title="fine")
    board.assign(t.id, "swarm")
    d = _daemon_with(board)
    out = _text(
        _handle_relabel_blocker(
            d, "swarm", {"reason": "x", "operator": True, "task_number": t.number}
        )
    )
    assert "not blocked" in out
    assert "swarm_block_on_operator" in out, "refusal does not name the verb that WOULD apply"
    assert not d.task_history.append.called, "refusal wrote history"


def test_worker_verb_refuses_ambiguity_across_several_blocked_tasks(board):
    from swarm.mcp.handlers._relabel import _handle_relabel_blocker

    _blocked_on_artifact(board, ref="a#1")
    _blocked_on_artifact(board, ref="b#2")
    d = _daemon_with(board)
    out = _text(_handle_relabel_blocker(d, "swarm", {"reason": "x", "operator": True}))
    assert "Ambiguous" in out
    assert not d.task_history.append.called


def test_worker_verb_says_so_when_the_cause_is_already_that(board):
    """A no-op must report itself as one. Answering with the success text would be
    #1159's shape — a verb that appears to have done something."""
    from swarm.mcp.handlers._relabel import _handle_relabel_blocker

    _blocked_on_operator(board)
    d = _daemon_with(board)
    out = _text(_handle_relabel_blocker(d, "swarm", {"reason": "same", "operator": True}))
    assert "already waiting on that" in out
    assert "swarm_unblock_task" in out, "did not point at the verb that resumes work"
    assert not d.task_history.append.called, "history row for a non-change"


def test_the_verb_is_reachable_from_the_worker_surface(board):
    """#1268's lesson: ``board.unblock`` existed, worked, was tested, and had ZERO
    callers — so BLOCKED had no reachable exit while the board tests passed. A board
    verb is not a feature until a surface can invoke it."""
    from swarm.mcp.tools import _HANDLERS, TOOLS

    assert "swarm_relabel_blocker" in {t["name"] for t in TOOLS}
    assert "swarm_relabel_blocker" in _HANDLERS
