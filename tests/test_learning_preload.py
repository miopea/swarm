"""Tests for P3 learning preload — PlaybookOps.recall_learnings_for_task."""

from __future__ import annotations

from swarm.drones.log import DroneLog
from swarm.server.playbook_ops import PlaybookOps
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask


def _ops(board: TaskBoard) -> PlaybookOps:
    return PlaybookOps(
        get_store=lambda: None,
        get_synthesizer=lambda: None,
        get_config=lambda: None,  # type: ignore[arg-type,return-value]
        drone_log=DroneLog(),
        task_board=board,
        track_task=lambda _t: None,
        get_worker=lambda _n: None,
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
