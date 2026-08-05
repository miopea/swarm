"""#1268 — an owner-preserving exit from BLOCKED, on both surfaces.

CORRECTED PREMISE. The #1104 audit claimed BLOCKED had no reachable
non-falsifying exit. That was wrong: ``board.release`` accepts BLOCKED (only
DONE/FAILED and already-ownerless are refused) and the Queen reaches it via
``queen_reassign_task``, whose own inline comment says so. The operator was never
stuck.

Two narrower things WERE missing, and they are what these tests pin:

1. No worker-surface exit from BLOCKED at all — the worker that declared the
   blocker could not clear it (sculpt-studio, #1237).
2. No OWNER-PRESERVING exit from either surface. ``release`` drops the owner, so
   "the wait ended, resume where you left off" meant reassigning the task back.

So the load-bearing assertion in this file is not "BLOCKED can be left" — it
already could be. It is **the owner survives**, which is the property `release`
does not provide, plus **no completion is recorded**, which is what
`force_complete` gets wrong.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from swarm.mcp.handlers._unblock import _handle_unblock_task
from swarm.mcp.queen_handlers._tasks import _handle_queen_unblock_task
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import TaskStatus


def _text(result) -> str:
    return " ".join(r["text"] for r in result)


@pytest.fixture
def d():
    daemon = MagicMock()
    daemon.task_board = TaskBoard()
    daemon.drone_log = MagicMock()
    daemon.task_history = MagicMock()
    daemon.blocker_store = MagicMock()
    daemon.blocker_store.clear_for_task.return_value = 1
    return daemon


def _blocked(d, worker="alice", title="waiting"):
    t = d.task_board.create(title=title)
    d.task_board.assign(t.id, worker)
    d.task_board.activate(t.id)
    assert d.task_board.block_on_external(t.id, worker, "upstream PR", "platform#234")
    assert d.task_board.get(t.id).status == TaskStatus.BLOCKED
    return t


# --- AC-1 / AC-2: both surfaces, and the OWNER SURVIVES --------------------


def test_worker_can_unblock_its_own_task_and_keeps_it(d):
    """AC-1. The property release does not provide: owner preserved."""
    t = _blocked(d)
    out = _text(_handle_unblock_task(d, "alice", {"reason": "platform#234 deployed"}))

    after = d.task_board.get(t.id)
    assert after.status == TaskStatus.ASSIGNED
    assert after.assigned_worker == "alice", "owner dropped — that is release, not unblock"
    assert after.status != TaskStatus.DONE
    assert f"#{t.number}" in out


def test_queen_can_unblock_and_keeps_the_owner(d):
    """AC-2. queen_reassign_task already exits BLOCKED but DROPS the owner."""
    t = _blocked(d, worker="sculpt-studio")
    out = _text(
        _handle_queen_unblock_task(d, "queen", {"number": t.number, "reason": "operator decided"})
    )

    after = d.task_board.get(t.id)
    assert after.status == TaskStatus.ASSIGNED
    assert after.assigned_worker == "sculpt-studio", "Queen unblock dropped the owner"
    assert "status=assigned" in out.lower()


def test_neither_surface_records_a_completion(d):
    """The falsification this task exists to remove. force_complete also exits
    BLOCKED — by recording DONE for work that is still open."""
    for handler, actor, kwargs in (
        (_handle_unblock_task, "alice", {"reason": "x"}),
        (_handle_queen_unblock_task, "queen", {"reason": "x"}),
    ):
        t = _blocked(d)
        if handler is _handle_queen_unblock_task:
            kwargs = {**kwargs, "number": t.number}
        handler(d, actor, kwargs)
        after = d.task_board.get(t.id)
        assert after.status is not TaskStatus.DONE
        assert not after.resolution, "a resolution was written for open work"


# --- AC-3: history + read-back --------------------------------------------


def test_history_is_written(d):
    """Absence of a history row is what made #1159 diagnosable; presence is the
    audit anchor for this transition."""
    _blocked(d)
    _handle_unblock_task(d, "alice", {"reason": "upstream shipped"})
    assert d.task_history.append.called
    assert d.drone_log.add.called


def test_success_text_reports_the_status_read_back(d):
    """#1159's park lesson: the old text asserted "the board is truthful now",
    which the caller cannot check, and stayed convincing while a promoter undid
    the write. This quotes what the board says AFTER the call."""
    t = _blocked(d)
    out = _text(_handle_unblock_task(d, "alice", {"reason": "x"}))
    assert "status=assigned" in out.lower()
    assert "owner=alice" in out.lower()
    assert str(t.number) in out


# --- AC-4: the BlockerStore rows go too (#529) ----------------------------


def test_blocker_rows_are_cleared_by_task_not_by_worker(d):
    """AC-4. clear_for_task, not clear(worker, n): a BLOCKED task can carry rows
    from several workers, and the per-worker variant leaves the rest behind. A
    stale row is what kept the IdleWatcher nudging."""
    t = _blocked(d)
    _handle_unblock_task(d, "alice", {"reason": "x"})
    d.blocker_store.clear_for_task.assert_called_once_with(t.number)
    assert not d.blocker_store.clear.called, "used the per-worker variant"


def test_queen_surface_clears_them_too(d):
    """The same gap on one surface only would be the hardest kind to notice."""
    t = _blocked(d, worker="platform")
    _handle_queen_unblock_task(d, "queen", {"number": t.number, "reason": "x"})
    d.blocker_store.clear_for_task.assert_called_once_with(t.number)


def test_a_blocker_store_failure_does_not_hide_the_transition(d):
    """The status change already succeeded. Reporting failure would tell the
    caller nothing happened when something did — but the orphaned row must be
    logged loudly, since it is a nudge-forever condition."""
    t = _blocked(d)
    d.blocker_store.clear_for_task.side_effect = RuntimeError("db gone")
    out = _text(_handle_unblock_task(d, "alice", {"reason": "x"}))
    assert d.task_board.get(t.id).status == TaskStatus.ASSIGNED
    assert f"#{t.number}" in out


def test_missing_blocker_store_still_unblocks(d):
    """Test daemons build via __new__; a missing store must not break the verb."""
    t = _blocked(d)
    del d.blocker_store
    _handle_unblock_task(d, "alice", {"reason": "x"})
    assert d.task_board.get(t.id).status == TaskStatus.ASSIGNED


# --- AC-5: refusals name the resolving fact and mutate nothing -------------


def test_refuses_a_task_that_is_not_blocked_naming_its_status(d):
    t = d.task_board.create(title="fine")
    d.task_board.assign(t.id, "alice")
    out = _text(_handle_unblock_task(d, "alice", {"reason": "x", "task_number": t.number}))
    assert "assigned, not blocked" in out.lower()
    assert "swarm_park_task" in out, "did not name the verb that WOULD apply"
    assert d.task_board.get(t.id).status == TaskStatus.ASSIGNED


def test_refuses_another_workers_task_naming_the_owner(d):
    theirs = _blocked(d, worker="project-root")
    mine = _blocked(d, worker="alice", title="mine")
    out = _text(_handle_unblock_task(d, "alice", {"reason": "x", "task_number": theirs.number}))
    assert "project-root" in out
    assert f"#{mine.number}" in out, "did not name the caller's own blocked queue"
    assert d.task_board.get(theirs.id).status == TaskStatus.BLOCKED, "refusal mutated"


def test_refuses_ambiguity_rather_than_guessing(d):
    _blocked(d, title="a")
    _blocked(d, title="b")
    out = _text(_handle_unblock_task(d, "alice", {"reason": "x"}))
    assert "Ambiguous" in out
    assert all(t.status == TaskStatus.BLOCKED for t in d.task_board.all_tasks)
    assert not d.blocker_store.clear_for_task.called, "refusal cleared blocker rows"


def test_queen_refuses_a_non_blocked_task_and_points_at_reassign(d):
    t = d.task_board.create(title="fine")
    d.task_board.assign(t.id, "alice")
    out = _text(_handle_queen_unblock_task(d, "queen", {"number": t.number, "reason": "x"}))
    assert "not blocked" in out.lower()
    assert "queen_reassign_task" in out
    assert d.task_board.get(t.id).status == TaskStatus.ASSIGNED


def test_reason_is_required_on_both_surfaces(d):
    t = _blocked(d)
    assert "reason" in _text(_handle_unblock_task(d, "alice", {})).lower()
    assert "reason" in _text(_handle_queen_unblock_task(d, "queen", {"number": t.number})).lower()
    assert d.task_board.get(t.id).status == TaskStatus.BLOCKED


# --- the corrected premise, pinned so nobody re-derives the wrong headline --


def test_release_does_exit_blocked_but_drops_the_owner(d):
    """Pins the fact that falsified the audit's first headline. If this ever
    fails, BLOCKED's exit picture changed and the audit doc needs revisiting."""
    t = _blocked(d)
    assert d.task_board.release(t.id) is True, "release no longer accepts BLOCKED"
    after = d.task_board.get(t.id)
    assert after.status == TaskStatus.UNASSIGNED
    assert not after.assigned_worker, "release kept the owner — then unblock is redundant"
