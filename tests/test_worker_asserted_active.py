"""Worker-asserted ACTIVE. See docs/specs/worker-asserted-active.md.

ACTIVE used to be INFERRED by the daemon and never asserted by the worker. Two
callers reached ``TaskBoard.activate`` — dispatch and
``WorkerStateTracker._promote_one_assigned`` — and neither is the worker. The
promoter picked the most-recently-updated ASSIGNED task on a RESTING→BUZZING
transition, so the board could say a worker was on B while it was on A. #1159
was that biting: ``park`` stamps ``updated_at``, so the just-set-down task sorted
FIRST and was re-activated seconds later.

The pre-existing machinery was never the gap. ``activate`` is already the single
chokepoint, ``_assert_no_double_active`` self-heals double-ACTIVE at persist, and
two reconcilers run — all enforcing *at most one ACTIVE per worker*. None of it
can know WHICH is right, because the only party that knows was never asked.

The tests that matter most here are the two NEGATIVE ones: that the promoter no
longer activates, and that a dispatched-but-unasserted task is left alone with no
nudge and no timed fallback. A fallback would restore the exact inference being
removed, and would pass every positive test while doing so.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from swarm.mcp.handlers._start import _handle_start_task
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import TaskStatus


def _text(result) -> str:
    return " ".join(r["text"] for r in result)


@pytest.fixture
def daemon_with_board():
    d = MagicMock()
    d.task_board = TaskBoard()
    d.drone_log = MagicMock()
    d.task_history = MagicMock()
    return d


# --- AC-1 / AC-2: the verb ------------------------------------------------


def test_worker_can_assert_active_and_it_is_recorded(daemon_with_board):
    """AC-1. The assertion is the only path to ACTIVE for a worker's own work."""
    d = daemon_with_board
    t = d.task_board.create(title="do the thing")
    d.task_board.assign(t.id, "alice")

    out = _text(_handle_start_task(d, "alice", {"task_number": t.number}))

    assert d.task_board.get(t.id).status == TaskStatus.ACTIVE
    assert f"#{t.number}" in out
    # History is the audit anchor — absence of a history row is what settled
    # #1159's write-failed-vs-write-reverted question.
    assert d.task_history.append.called


def test_success_text_reports_the_status_read_back_not_the_one_requested(daemon_with_board):
    """#1159's lesson. The park handler used to assert "the board is truthful
    now" — a claim the caller cannot check, which stayed convincing for months
    while a promoter silently undid the write."""
    d = daemon_with_board
    t = d.task_board.create(title="x")
    d.task_board.assign(t.id, "alice")
    out = _text(_handle_start_task(d, "alice", {}))
    assert "status=active" in out.lower()


def test_refuses_another_workers_task_and_names_the_owner(daemon_with_board):
    """AC-2 + #1057: a refusal must carry the resolving fact."""
    d = daemon_with_board
    theirs = d.task_board.create(title="not yours")
    d.task_board.assign(theirs.id, "project-root")
    mine = d.task_board.create(title="mine")
    d.task_board.assign(mine.id, "alice")

    out = _text(_handle_start_task(d, "alice", {"task_number": theirs.number}))

    assert d.task_board.get(theirs.id).status == TaskStatus.ASSIGNED, "refusal mutated"
    assert "project-root" in out, f"refusal did not name the owner: {out}"
    assert f"#{mine.number}" in out, "refusal did not name the caller's own queue"


def test_refuses_when_already_working_something(daemon_with_board):
    """Silent switching is how the board stopped matching reality."""
    d = daemon_with_board
    a = d.task_board.create(title="a")
    b = d.task_board.create(title="b")
    d.task_board.assign(a.id, "alice")
    d.task_board.assign(b.id, "alice")
    _handle_start_task(d, "alice", {"task_number": a.number})

    out = _text(_handle_start_task(d, "alice", {"task_number": b.number}))

    assert d.task_board.get(a.id).status == TaskStatus.ACTIVE, "asserted task was displaced"
    assert d.task_board.get(b.id).status == TaskStatus.ASSIGNED
    assert "swarm_park_task" in out and "swarm_complete_task" in out, "no exits named"


def test_refuses_ambiguity_rather_than_guessing(daemon_with_board):
    d = daemon_with_board
    for title in ("a", "b"):
        t = d.task_board.create(title=title)
        d.task_board.assign(t.id, "alice")

    out = _text(_handle_start_task(d, "alice", {}))

    assert "Ambiguous" in out
    assert not [t for t in d.task_board.all_tasks if t.status == TaskStatus.ACTIVE]


def test_refuses_a_closed_task_with_the_reason(daemon_with_board):
    d = daemon_with_board
    t = d.task_board.create(title="done already")
    d.task_board.assign(t.id, "alice")
    d.task_board.complete(t.id)

    out = _text(_handle_start_task(d, "alice", {"task_number": t.number}))
    assert "already done" in out.lower()


# --- AC-3 / AC-7: the daemon stops inferring — THE regression guards -------


def test_promoter_no_longer_activates_on_buzzing():
    """AC-3, and the whole point. A RESTING->BUZZING transition must NOT pick a
    task. Going BUZZING is evidence the worker is doing something; it is not
    evidence of WHICH task, and this function never had a way to tell."""
    from swarm.drones.state_tracker import WorkerStateTracker

    board = TaskBoard()
    t = board.create(title="queued")
    board.assign(t.id, "alice")

    tracker = WorkerStateTracker.__new__(WorkerStateTracker)
    tracker.task_board = board
    tracker._emit = lambda *a, **k: None

    worker = MagicMock()
    worker.name = "alice"
    tracker._promote_one_assigned(worker)

    assert board.get(t.id).status == TaskStatus.ASSIGNED, (
        "the promoter activated a task again — that is the #1159 mechanism"
    )


def test_the_1159_sequence_cannot_recur():
    """AC-7, the exact measured sequence: park stamps updated_at, which used to
    make the just-set-down task the top candidate for re-activation."""
    from swarm.drones.state_tracker import WorkerStateTracker

    board = TaskBoard()
    t = board.create(title="parked work")
    board.assign(t.id, "alice")
    board.activate(t.id)
    assert board.park(t.id, "alice", "operator preempt")

    tracker = WorkerStateTracker.__new__(WorkerStateTracker)
    tracker.task_board = board
    tracker._emit = lambda *a, **k: None
    worker = MagicMock()
    worker.name = "alice"
    tracker._promote_one_assigned(worker)

    after = board.get(t.id)
    assert after.status == TaskStatus.ASSIGNED
    assert after.is_on_hold, "the park was undone — #1159 verbatim"


def test_no_timed_fallback_reintroduces_inference():
    """AC-6, structurally. A grace-period auto-activate would pass every
    positive test above while restoring exactly what was removed, so assert on
    the source that no activation path survives in the promoter."""
    import inspect

    from swarm.drones import state_tracker

    src = inspect.getsource(state_tracker.WorkerStateTracker._promote_one_assigned)
    assert "activate(" not in src, "the promoter can still activate a task"


# --- AC-8: pre-existing guarantees still hold -----------------------------


def test_one_active_per_worker_still_enforced():
    """AC-8. The change removes an inference path, not the INV-1 guarantee."""
    board = TaskBoard()
    a = board.create(title="a")
    b = board.create(title="b")
    board.assign(a.id, "alice")
    board.assign(b.id, "alice")
    board.activate(a.id)
    board.activate(b.id)

    active = [t for t in board.all_tasks if t.status == TaskStatus.ACTIVE]
    assert len(active) == 1 and active[0].id == b.id


# --- AC-5: don't dispatch into an operator conversation -------------------


def test_operator_engaged_uses_a_longer_window_than_is_user_active():
    """AC-5. is_user_active answers "would typing now collide with a keystroke?"
    with a 2s window. Dispatch gating asks "is this worker in a conversation?",
    and a human pauses longer than 2s between sentences — so reusing the tight
    window would report available mid-conversation."""
    from swarm.pty.process import WorkerProcess

    assert WorkerProcess._OPERATOR_ENGAGED_WINDOW > WorkerProcess._USER_ACTIVE_WINDOW
    assert WorkerProcess._OPERATOR_ENGAGED_WINDOW >= 60


def test_operator_engaged_clears_when_the_terminal_detaches():
    """It must not be a flag anyone has to remember to clear — a stuck one would
    silently starve a worker of work forever."""
    import time

    from swarm.pty.process import WorkerProcess

    proc = WorkerProcess(name="alice", cwd="/tmp")
    proc._terminal_active = True
    proc._last_user_input = time.time()
    assert proc.is_operator_engaged is True

    proc._terminal_active = False
    assert proc.is_operator_engaged is False, "engagement survived terminal detach"


def test_auto_chain_defers_for_an_engaged_worker_and_leaves_the_task_queued():
    """AC-5. The task keeps its owner — it is queued, not reassigned or dropped."""
    import time

    from swarm.pty.process import WorkerProcess
    from swarm.server.task_coordinator import TaskCoordinator

    board = TaskBoard()
    t = board.create(title="queued work")
    board.assign(t.id, "alice")

    proc = WorkerProcess(name="alice", cwd="/tmp")
    proc._terminal_active = True
    proc._last_user_input = time.time()
    worker = MagicMock()
    worker.process = proc

    d = MagicMock()
    d.task_board = board
    d.get_worker.return_value = worker
    d.start_task = MagicMock(side_effect=AssertionError("dispatched into a conversation"))

    coord = TaskCoordinator.__new__(TaskCoordinator)
    coord._d = d
    coord.auto_start_next_assigned("alice")

    after = board.get(t.id)
    assert after.status == TaskStatus.ASSIGNED
    assert after.assigned_worker == "alice", "task lost its owner while deferred"
