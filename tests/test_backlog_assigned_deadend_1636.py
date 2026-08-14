"""#1636 — an ASSIGNED-and-BACKLOG task could be neither started nor completed.

THE DEAD END, reproduced on the real handlers (not mocks, not a reading):

    swarm_complete_task(N)  -> "not in progress (status=backlog) — nothing to complete."
    swarm_start_task(N)     -> "a backlog task cannot be started. Nothing changed."

Completion requires in-progress; starting requires not-backlog; nothing moved
backlog → assigned from the worker side. Hit on sculpt-studio #1304, which the
operator had to force-complete from the Queen side.

WHY THE STATE IS NOT CORRUPTION, which decides the shape of the fix.
``SwarmTask.assign`` KEEPS backlog status by a deliberate 2026-08-07 operator
decision: "Backlog means parked, not for now; promoting it to ASSIGNED on
assignment would un-park it." Its safety argument is that no dispatch path
accepts BACKLOG, "so an owned Backlog task is inert." That decision is correct
and this fix preserves it — an owned BACKLOG task stays out of ``_startable``,
so the bare call and the ambiguity list still cannot reach it, and nothing
auto-dispatches it. What the decision never covered is a worker who has already
DONE the work: inertness was meant to stop accidental starts, not to strand
finished work. So the route added here is the deliberate one that already exists
for the other kind of parking — explicit ``task_number`` plus ``unpark=true``.

MEASURED BEFORE THE FIX, live DB: 4 of 4 BACKLOG rows carry an owner (of 1589
tasks total). The dead-end state is not an edge case — it is the only form
BACKLOG currently takes.

TWO THINGS THE TICKET DID NOT REPORT, found by probing rather than reading:

1. ``unpark=true`` did NOT rescue it. The hold check runs before the status
   check, so a caller who supplied the documented consent word got the identical
   backlog refusal. That is #1286's defect recurring — a refusal naming a
   resolving action that is a provable no-op — and all four live BACKLOG rows
   are hold-tagged, so it was the case that mattered.
2. docs/specs/taskboard-state-machine-audit.md lists ``assign``→ASSIGNED as one
   of BACKLOG's three exits. The 2026-08-07 change removed it and the audit was
   never updated; property (a) "exit set non-empty" still passed on the two
   survivors, so no test could notice.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm.mcp.handlers._start import _handle_start_task
from swarm.mcp.handlers._tasks import _handle_complete_task
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask, TaskStatus


class _Daemon:
    """Minimal daemon surface the two handlers touch."""

    def __init__(self, board: TaskBoard) -> None:
        self.task_board = board
        self.completed: list[tuple[str, str, bool]] = []

    def complete_task(
        self, task_id: str, *, actor: str = "", resolution: str = "", force: bool = False, **_: Any
    ) -> bool:
        self.completed.append((task_id, actor, force))
        # The real daemon's force path clears blocker bindings first; the board's
        # own status gate (ASSIGNED/ACTIVE only) is what matters to these tests,
        # and it is deliberately NOT bypassed here — a stub that always succeeded
        # would let a broken start path still show a DONE task.
        return self.task_board.complete(task_id, resolution=resolution)


def _text(result: list[dict[str, Any]]) -> str:
    return "\n".join(part.get("text", "") for part in result)


@pytest.fixture
def board() -> TaskBoard:
    return TaskBoard()


def _owned_backlog(board: TaskBoard, *, hold: bool = False) -> SwarmTask:
    """The exact reported state: status=BACKLOG, assigned_worker=<caller>.

    Built through ``board.assign`` rather than by setting the field, so the test
    exercises the real route into this state rather than fabricating one. #1510
    was a case of tests forging an unreachable state and proving nothing.
    """
    task = SwarmTask(title="work that is finished", status=TaskStatus.BACKLOG)
    if hold:
        task.tags = ["hold"]
    board.add(task)
    board.assign(task.id, "swarm")
    result = board.get(task.id)
    assert result is not None
    assert result.status == TaskStatus.BACKLOG, "precondition: assign must KEEP backlog"
    assert result.assigned_worker == "swarm"
    return result


# ======================================================================================
# The state is real and reachable
# ======================================================================================


def test_assign_keeps_backlog_so_the_reported_state_is_reachable(board: TaskBoard) -> None:
    """The premise of the whole ticket, asserted rather than assumed.

    Also pins the 2026-08-07 decision: if someone later makes assign() promote to
    ASSIGNED, this fails and they must revisit that decision deliberately.
    """
    task = _owned_backlog(board)

    assert task.status == TaskStatus.BACKLOG
    assert task.assigned_worker == "swarm"


# ======================================================================================
# AC2 — the reproduction no longer dead-ends
# ======================================================================================


def test_start_task_promotes_an_owned_backlog_task_with_explicit_consent(
    board: TaskBoard,
) -> None:
    """THE FIX. unpark=true is the caller saying "yes, I mean this parked one"."""
    task = _owned_backlog(board)

    text = _text(
        _handle_start_task(_Daemon(board), "swarm", {"task_number": task.number, "unpark": True})
    )

    after = board.get(task.id)
    assert after is not None
    assert after.status == TaskStatus.ACTIVE, text
    assert "Started" in text


def test_the_promoted_task_can_then_be_completed(board: TaskBoard) -> None:
    """AC2 end to end — the worker closes their own finished work, no Queen."""
    task = _owned_backlog(board)
    d = _Daemon(board)

    _handle_start_task(d, "swarm", {"task_number": task.number, "unpark": True})
    text = _text(_handle_complete_task(d, "swarm", {"number": task.number, "resolution": "done"}))

    after = board.get(task.id)
    assert after is not None
    assert after.status == TaskStatus.DONE, text


def test_a_hold_tagged_backlog_task_is_startable_too(board: TaskBoard) -> None:
    """ALL FOUR live BACKLOG rows are hold-tagged, so this is the case that matters.

    Before the fix the hold check passed on unpark=true and the status check then
    refused anyway — the documented consent word was a no-op.
    """
    task = _owned_backlog(board, hold=True)
    assert task.is_on_hold

    _handle_start_task(_Daemon(board), "swarm", {"task_number": task.number, "unpark": True})

    after = board.get(task.id)
    assert after is not None
    assert after.status == TaskStatus.ACTIVE
    assert not after.is_on_hold, "the hold tag must be cleared, not carried into ACTIVE"


# ======================================================================================
# AC3 — the remaining refusals name a route that WORKS
# ======================================================================================


def test_start_without_consent_still_refuses_and_names_unpark(board: TaskBoard) -> None:
    """Refusing by default is the 2026-08-07 decision holding: no accidental un-park."""
    task = _owned_backlog(board)

    text = _text(_handle_start_task(_Daemon(board), "swarm", {"task_number": task.number}))

    assert "unpark=true" in text
    after = board.get(task.id)
    assert after is not None
    assert after.status == TaskStatus.BACKLOG, "a refusal must not mutate"


def test_the_named_route_actually_works_when_followed(board: TaskBoard) -> None:
    """#1286: a refusal naming a provable no-op is worse than one naming nothing.

    So do not assert the wording alone — FOLLOW it and check the outcome.
    """
    task = _owned_backlog(board)
    d = _Daemon(board)

    refusal = _text(_handle_start_task(d, "swarm", {"task_number": task.number}))
    assert "unpark=true" in refusal
    _handle_start_task(d, "swarm", {"task_number": task.number, "unpark": True})

    after = board.get(task.id)
    assert after is not None
    assert after.status == TaskStatus.ACTIVE


def test_complete_task_refusal_names_the_start_route(board: TaskBoard) -> None:
    """The message the operator saw named nothing at all."""
    task = _owned_backlog(board)

    text = _text(
        _handle_complete_task(_Daemon(board), "swarm", {"number": task.number, "resolution": "x"})
    )

    assert "swarm_start_task" in text
    assert "unpark=true" in text


def test_complete_task_refusal_route_is_followable(board: TaskBoard) -> None:
    """Same discipline: follow what the refusal says, assert the end state."""
    task = _owned_backlog(board)
    d = _Daemon(board)

    _handle_complete_task(d, "swarm", {"number": task.number, "resolution": "x"})
    _handle_start_task(d, "swarm", {"task_number": task.number, "unpark": True})
    _handle_complete_task(d, "swarm", {"number": task.number, "resolution": "x"})

    after = board.get(task.id)
    assert after is not None
    assert after.status == TaskStatus.DONE


# ======================================================================================
# AC4 — clearing the slot then starting a different owned task composes
# ======================================================================================


def test_blocking_the_active_task_frees_the_slot_for_a_backlog_task(board: TaskBoard) -> None:
    """The ticket's second, independent way in.

    A worker told to clear their slot does so correctly (block_for_operator), and
    must then be able to proceed rather than meet a second refusal.
    """
    busy = SwarmTask(title="in progress", status=TaskStatus.UNASSIGNED)
    board.add(busy)
    board.assign(busy.id, "swarm")
    board.activate(busy.id)
    parked = _owned_backlog(board)
    d = _Daemon(board)

    refused = _text(_handle_start_task(d, "swarm", {"task_number": parked.number, "unpark": True}))
    assert "already have" in refused, "precondition: the slot is occupied"

    board.block_for_operator(busy.id, reason="waiting on a human")
    text = _text(_handle_start_task(d, "swarm", {"task_number": parked.number, "unpark": True}))

    after = board.get(parked.id)
    assert after is not None
    assert after.status == TaskStatus.ACTIVE, text


# ======================================================================================
# CONTROLS — the half that decides whether this is safe to keep
# ======================================================================================


def test_an_owned_backlog_task_is_still_not_reachable_by_the_bare_call(board: TaskBoard) -> None:
    """THE 2026-08-07 DECISION, PINNED.

    "Backlog means parked, not for now." If a BACKLOG task entered ``_startable``
    the no-argument call would start it, which is un-parking by accident — the
    exact thing the decision forbids. Consent must be per-task and explicit.
    """
    _owned_backlog(board)

    text = _text(_handle_start_task(_Daemon(board), "swarm", {}))

    assert "No startable task" in text


def test_an_owned_backlog_task_does_not_make_the_bare_call_ambiguous(board: TaskBoard) -> None:
    """Sibling of the above: it must not pad the ambiguity list either.

    Otherwise one parked task would block the bare call on an ordinary one.
    """
    ordinary = SwarmTask(title="ordinary", status=TaskStatus.UNASSIGNED)
    board.add(ordinary)
    board.assign(ordinary.id, "swarm")
    _owned_backlog(board)

    text = _text(_handle_start_task(_Daemon(board), "swarm", {}))

    assert "Ambiguous" not in text
    after = board.get(ordinary.id)
    assert after is not None
    assert after.status == TaskStatus.ACTIVE


def test_someone_elses_backlog_task_is_still_refused(board: TaskBoard) -> None:
    """unpark=true is consent to un-park YOUR OWN work, not authority over others'."""
    task = SwarmTask(title="theirs", status=TaskStatus.BACKLOG)
    board.add(task)
    board.assign(task.id, "nexus")

    text = _text(
        _handle_start_task(_Daemon(board), "swarm", {"task_number": task.number, "unpark": True})
    )

    assert "not assigned to you" in text
    after = board.get(task.id)
    assert after is not None
    assert after.status == TaskStatus.BACKLOG


def test_ordinary_assigned_tasks_still_start_without_unpark(board: TaskBoard) -> None:
    """POSITIVE CONTROL: the common path must not acquire a consent requirement."""
    task = SwarmTask(title="ordinary", status=TaskStatus.UNASSIGNED)
    board.add(task)
    board.assign(task.id, "swarm")

    text = _text(_handle_start_task(_Daemon(board), "swarm", {"task_number": task.number}))

    after = board.get(task.id)
    assert after is not None
    assert after.status == TaskStatus.ACTIVE, text


def test_closed_and_blocked_refusals_are_unchanged(board: TaskBoard) -> None:
    """The other rungs of the ladder must not be loosened by widening one of them."""
    d = _Daemon(board)
    for status, needle in (
        (TaskStatus.DONE, "already done"),
        (TaskStatus.FAILED, "reopen it first"),
        (TaskStatus.BLOCKED, "blocked"),
    ):
        task = SwarmTask(title=f"a {status.value} task", status=TaskStatus.UNASSIGNED)
        board.add(task)
        board.assign(task.id, "swarm")
        found = board.get(task.id)
        assert found is not None
        found.status = status

        text = _text(_handle_start_task(d, "swarm", {"task_number": task.number, "unpark": True}))

        assert needle in text, f"{status.value}: {text}"
        after = board.get(task.id)
        assert after is not None
        assert after.status == status
