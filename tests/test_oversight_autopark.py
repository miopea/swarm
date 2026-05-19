"""Operator-blocked-stall guard — OversightMonitor.collect_park_proposals.

Deterministic, Queen-free detector: an ACTIVE task whose updated_at stays
frozen across N drift-cadence checks while the worker idles is the
"standing by, blocked on the operator" pattern (#443). It must raise ONE
park proposal and then stay quiet (pending dedupe + reject backoff).

`drift_check_interval_minutes=0` makes the cadence gate a no-op so each
call == one check (no clock monkeypatching needed).
"""

from __future__ import annotations

from swarm.config import OversightConfig
from swarm.queen.oversight import OversightMonitor
from swarm.tasks.task import SwarmTask, TaskStatus
from swarm.worker.worker import Worker, WorkerState


def _worker(name: str = "project-root") -> Worker:
    w = Worker(name=name, path="/tmp/test")
    w.state = WorkerState.RESTING
    return w


def _task(updated_at: float, status: TaskStatus = TaskStatus.ACTIVE) -> SwarmTask:
    t = SwarmTask(title="PROGRAM: Renovate rollout")
    t.status = status
    t.updated_at = updated_at
    return t


class _Board:
    def __init__(self, task: SwarmTask | None) -> None:
        self._task = task

    def active_tasks_for_worker(self, name: str) -> list[SwarmTask]:
        return [self._task] if self._task is not None else []


def _monitor(**cfg) -> OversightMonitor:
    base = dict(drift_check_interval_minutes=0, auto_park_no_progress_checks=2)
    base.update(cfg)
    return OversightMonitor(OversightConfig(**base))


def test_frozen_active_task_proposes_park_at_threshold():
    m = _monitor()
    w = _worker()
    board = _Board(_task(updated_at=1000.0))
    # 1st call: first observation (records marker, streak 0).
    assert m.collect_park_proposals([w], board) == []
    # 2nd: no progress → streak 1 (< 2).
    assert m.collect_park_proposals([w], board) == []
    # 3rd: streak 2 (>= threshold) → propose.
    out = m.collect_park_proposals([w], board)
    assert len(out) == 1
    wname, task_id, reason = out[0]
    assert wname == "project-root"
    assert "blocked on the operator" in reason
    # Streak reset after proposing — no duplicate next cycle.
    assert m.collect_park_proposals([w], board) == []


def test_progress_resets_the_streak():
    m = _monitor()
    w = _worker()
    t = _task(updated_at=1000.0)
    board = _Board(t)
    assert m.collect_park_proposals([w], board) == []  # observe
    assert m.collect_park_proposals([w], board) == []  # streak 1
    t.updated_at = 2000.0  # real progress
    assert m.collect_park_proposals([w], board) == []  # progress → reset 0
    assert m.collect_park_proposals([w], board) == []  # streak 1
    out = m.collect_park_proposals([w], board)  # streak 2 → propose
    assert len(out) == 1


def test_non_active_task_never_proposes_and_clears_streak():
    m = _monitor()
    w = _worker()
    blocked = _Board(_task(updated_at=1000.0, status=TaskStatus.BLOCKED))
    for _ in range(5):
        assert m.collect_park_proposals([w], blocked) == []
    # ASSIGNED (not ACTIVE) likewise ignored.
    assigned = _Board(_task(updated_at=1000.0, status=TaskStatus.ASSIGNED))
    for _ in range(5):
        assert m.collect_park_proposals([w], assigned) == []


def test_reject_backoff_suppresses_reproposal():
    m = _monitor()
    w = _worker()
    board = _Board(_task(updated_at=1000.0))
    for _ in range(3):
        m.collect_park_proposals([w], board)  # would have proposed by now
    m.note_park_rejected("project-root", _board_task_id(board))
    # Even with the stall persisting, no re-propose within the window.
    for _ in range(5):
        assert m.collect_park_proposals([w], board) == []


def test_disabled_never_proposes():
    m = _monitor(auto_park_enabled=False)
    w = _worker()
    board = _Board(_task(updated_at=1000.0))
    for _ in range(6):
        assert m.collect_park_proposals([w], board) == []


def test_reset_worker_clears_streak():
    m = _monitor()
    w = _worker()
    board = _Board(_task(updated_at=1000.0))
    m.collect_park_proposals([w], board)
    m.collect_park_proposals([w], board)  # streak building
    m.reset_worker("project-root")
    # Streak forgotten → needs the full run again, no early trigger.
    assert m.collect_park_proposals([w], board) == []
    assert m.collect_park_proposals([w], board) == []
    assert len(m.collect_park_proposals([w], board)) == 1


def _board_task_id(board: _Board) -> str:
    return board._task.id
