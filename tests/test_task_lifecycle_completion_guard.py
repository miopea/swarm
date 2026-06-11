"""Regression tests for the TaskLifecycle false-idle completion guard.

2026-06-11 bug: drone nudges (AUTO_NUDGE) and completion proposals
(PROPOSED_COMPLETION) fired against workers that READ idle but weren't —
either the operator was actively typing in the PTY, or the worker was mid
a long *quiet* foreground command whose display_state momentarily fell to
RESTING. The idle-watcher guards live in ``test_idle_watcher.py``; these
cover the parallel guard in ``TaskLifecycle._check_task_completions``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from swarm.config import DroneConfig
from swarm.drones.task_lifecycle import TaskLifecycle
from swarm.worker.worker import WorkerState


def _worker(
    name: str,
    *,
    state: WorkerState = WorkerState.RESTING,
    state_duration: float = 999.0,
    operator_engaged: bool = False,
) -> MagicMock:
    w = MagicMock()
    w.name = name
    w.state = state
    w.state_duration = state_duration
    w.process.operator_engaged_within.return_value = operator_engaged
    return w


def _task(number: int, task_id: str, worker_name: str) -> MagicMock:
    t = MagicMock()
    t.number = number
    t.id = task_id
    t.title = f"task {number}"
    t.assigned_worker = worker_name
    return t


def _lifecycle(
    workers: list[MagicMock],
    tasks: list[MagicMock],
    *,
    worker_busy_check=None,
) -> tuple[TaskLifecycle, list[tuple]]:
    board = MagicMock()
    board.active_tasks = tasks
    emitted: list[tuple] = []

    lc = TaskLifecycle(
        workers=workers,
        log=MagicMock(),
        task_board=board,
        queen=None,
        drone_config=DroneConfig(),
        proposed_completions={},
        idle_consecutive={},
        emit=lambda *a, **k: emitted.append(a),
        build_context=lambda **k: "",
        pending_proposals_check=None,
        pending_proposals_for_worker=None,
        worker_busy_check=worker_busy_check,
    )
    return lc, emitted


def test_idle_worker_proposes_completion() -> None:
    """Baseline: a long-idle RESTING worker with an active task proposes completion."""
    w = _worker("alpha")
    lc, emitted = _lifecycle([w], [_task(42, "t-42", "alpha")])

    assert lc._check_task_completions() is True
    assert any(a[0] == "task_done" for a in emitted)


def test_operator_engaged_worker_does_not_propose_completion() -> None:
    """Trigger #1: operator typing in the PTY suppresses the completion proposal."""
    w = _worker("d365", operator_engaged=True)
    lc, emitted = _lifecycle([w], [_task(700, "t-700", "d365")])

    assert lc._check_task_completions() is False
    assert emitted == []


def test_busy_worker_does_not_propose_completion() -> None:
    """Trigger #2: live-PTY busy check suppresses the proposal despite RESTING."""
    w = _worker("root")
    lc, emitted = _lifecycle([w], [_task(679, "t-679", "root")], worker_busy_check=lambda _w: True)

    assert lc._check_task_completions() is False
    assert emitted == []


def test_busy_check_exception_does_not_suppress() -> None:
    """A raising busy check is treated as 'not busy' — proposal still fires."""

    def _boom(_w: object) -> bool:
        raise RuntimeError("provider lookup failed")

    w = _worker("alpha")
    lc, emitted = _lifecycle([w], [_task(42, "t-42", "alpha")], worker_busy_check=_boom)

    assert lc._check_task_completions() is True
    assert any(a[0] == "task_done" for a in emitted)
