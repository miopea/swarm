"""Tests for P3 learning preload — PlaybookOps.recall_learnings_for_task,
and (post-#1185) PlaybookOps.consolidate_learnings, which decides what the
recall path has to work with in the first place."""

from __future__ import annotations

from unittest.mock import MagicMock

from swarm.drones.log import DroneLog
from swarm.server.playbook_ops import (
    _LEARNING_BLOCK_CHARS,
    _LEARNING_CHARS_PER_ITEM,
    PlaybookOps,
)
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask


def _ops(board: TaskBoard, get_worker=lambda _n: None) -> PlaybookOps:
    return PlaybookOps(
        get_store=lambda: None,
        get_synthesizer=lambda: None,
        get_config=lambda: None,  # type: ignore[arg-type,return-value]
        drone_log=DroneLog(),
        task_board=board,
        track_task=lambda _t: None,
        get_worker=get_worker,
    )


def test_recall_returns_relevant_learning_by_keyword_overlap():
    board = TaskBoard()
    past = board.create(title="Fix websocket reconnect on mobile resume")
    board.update(past.id, description="")
    past.learnings = "The zombie websocket keeps readyState OPEN after mobile resume."
    board.persist(past)

    board.create(title="unrelated database migration work")  # no overlap

    ops = _ops(board)
    task = SwarmTask(title="Investigate websocket reconnect delay", description="mobile resume")
    block = ops.recall_learnings_for_task(task)

    assert "Relevant learnings" in block
    assert "zombie websocket" in block
    assert "database migration" not in block


def test_recall_empty_when_no_overlap():
    board = TaskBoard()
    other = board.create(title="Kubernetes ingress tuning")
    other.learnings = "Adjust nginx annotations for the ingress controller."
    board.persist(other)

    ops = _ops(board)
    task = SwarmTask(title="Add a budget chart to the dashboard", description="recharts widget")
    assert ops.recall_learnings_for_task(task) == ""


def test_recall_ignores_tasks_without_learnings():
    board = TaskBoard()
    board.create(title="websocket reconnect fix")  # no .learnings set
    ops = _ops(board)
    task = SwarmTask(title="websocket reconnect follow-up")
    assert ops.recall_learnings_for_task(task) == ""


def test_recall_caps_at_three():
    board = TaskBoard()
    for i in range(6):
        t = board.create(title=f"websocket reconnect mobile fix number {i}")
        t.learnings = f"learning about websocket reconnect mobile resume {i}"
        board.persist(t)
    ops = _ops(board)
    task = SwarmTask(title="websocket reconnect mobile resume", description="mobile")
    block = ops.recall_learnings_for_task(task)
    # At most 3 learning entries rendered.
    assert block.count("[#") <= 3


# ---------------------------------------------------------------------------
# #1185 — the recall block is injected into a worker's PTY as one paste, so it
# has to be SIZE-BOUNDED. Counting entries is not a size bound: three entries
# of 10k chars each is a 30k-char paste. Measured on the live board, real
# resolutions average 2077 chars and reach 10539.
# ---------------------------------------------------------------------------


def _long_learning(marker: str, *, lines: int = 60) -> str:
    """A learning far larger than the per-item cap, on tidy line boundaries."""
    return "\n".join(
        f"{marker} websocket reconnect mobile resume detail line {i}" for i in range(lines)
    )


def test_recall_truncates_an_oversized_learning():
    board = TaskBoard()
    t = board.create(title="websocket reconnect mobile fix")
    t.learnings = _long_learning("alpha")
    board.persist(t)

    ops = _ops(board)
    task = SwarmTask(title="websocket reconnect mobile resume", description="mobile")
    block = ops.recall_learnings_for_task(task)

    assert "alpha" in block, "the learning is still recalled, just bounded"
    assert len(block) < len(t.learnings), "an oversized learning must not pass through whole"


def test_recall_truncation_cuts_on_a_line_boundary():
    """Do NOT reintroduce the defect being fixed: a quarter of the scraped
    learnings started mid-word because a screen capture cuts at the pane
    width. The cap must not do the same thing."""
    board = TaskBoard()
    t = board.create(title="websocket reconnect mobile fix")
    t.learnings = _long_learning("beta")
    board.persist(t)

    ops = _ops(board)
    task = SwarmTask(title="websocket reconnect mobile resume", description="mobile")
    block = ops.recall_learnings_for_task(task)

    body = [ln for ln in block.splitlines() if ln.startswith("beta ")]
    originals = set(t.learnings.splitlines())
    assert body, "expected some of the learning to survive"
    for line in body:
        assert line in originals, f"line was cut mid-content: {line!r}"


def test_recall_block_is_bounded_across_several_large_learnings():
    board = TaskBoard()
    for i in range(3):
        t = board.create(title=f"websocket reconnect mobile fix {i}")
        t.learnings = _long_learning(f"item{i}")
        board.persist(t)

    ops = _ops(board)
    task = SwarmTask(title="websocket reconnect mobile resume", description="mobile")
    block = ops.recall_learnings_for_task(task)

    assert len(block) <= _LEARNING_BLOCK_CHARS * 2, (
        f"recall block grew to {len(block)} chars — this is pasted into a PTY"
    )


def test_truncation_marker_names_the_pull_tool():
    """Capping must not silently lose text — say where the rest lives."""
    board = TaskBoard()
    t = board.create(title="websocket reconnect mobile fix")
    t.learnings = _long_learning("gamma")
    board.persist(t)

    ops = _ops(board)
    task = SwarmTask(title="websocket reconnect mobile resume", description="mobile")
    block = ops.recall_learnings_for_task(task)

    assert "swarm_get_learnings" in block


def test_short_learnings_are_untouched():
    board = TaskBoard()
    t = board.create(title="websocket reconnect mobile fix")
    t.learnings = "The zombie websocket keeps readyState OPEN after mobile resume."
    board.persist(t)

    ops = _ops(board)
    task = SwarmTask(title="websocket reconnect mobile resume", description="mobile")
    block = ops.recall_learnings_for_task(task)

    assert t.learnings in block
    assert "truncated" not in block
    assert len(t.learnings) < _LEARNING_CHARS_PER_ITEM  # precondition


# ---------------------------------------------------------------------------
# #1185 — consolidate_learnings. It used to scrape the worker's PTY screen
# (get_content(30) → strip CSI → last 15 lines), so 96.5% of stored learnings
# carried Claude Code footer chrome and 26.6% began mid-sentence. The worker's
# actual resolution — written for exactly this audience — was discarded.
# ---------------------------------------------------------------------------


def _worker_whose_pty_must_not_be_read() -> MagicMock:
    """A worker that FAILS LOUDLY if anyone reads its terminal.

    Asserting on the output cannot prove the scrape is gone — a scrape that
    happens and is then overwritten looks identical. Only a call that raises
    distinguishes them.
    """
    worker = MagicMock()
    worker.process.get_content.side_effect = AssertionError(
        "consolidate_learnings read the PTY — the #1185 scrape is back"
    )
    return worker


def test_consolidate_uses_the_resolution_the_worker_wrote():
    board = TaskBoard()
    ops = _ops(board, get_worker=lambda _n: _worker_whose_pty_must_not_be_read())
    task = board.create(title="fix the thing")
    task.assigned_worker = "web"
    task.resolution = "Root cause was a stale cache key in resolveTenant()."

    ops.consolidate_learnings(task)

    assert task.learnings == "Root cause was a stale cache key in resolveTenant()."


def test_consolidate_leaves_learnings_empty_without_a_resolution():
    """A force-complete carries no resolution. Nothing was written, so nothing
    is claimed — better than filing a screenful of UI chrome as knowledge."""
    board = TaskBoard()
    ops = _ops(board, get_worker=lambda _n: _worker_whose_pty_must_not_be_read())
    task = board.create(title="force-completed thing")
    task.assigned_worker = "web"
    task.resolution = ""

    ops.consolidate_learnings(task)

    assert task.learnings == ""


def test_consolidate_never_reads_the_pty():
    """The regression guard. If the scrape returns, the mock raises."""
    board = TaskBoard()
    worker = _worker_whose_pty_must_not_be_read()
    ops = _ops(board, get_worker=lambda _n: worker)
    task = board.create(title="fix the thing")
    task.assigned_worker = "web"
    task.resolution = "A real resolution."

    ops.consolidate_learnings(task)

    worker.process.get_content.assert_not_called()
