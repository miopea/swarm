"""Tests for :class:`swarm.drones.detectors.context_files.ContextFileTracker`.

Moved from ``tests/test_state_tracker.py::TestContextFileTracking`` as
part of Phase 1 of ``docs/specs/state-tracker-refactor.md``.
"""

from __future__ import annotations

from swarm.drones.detectors import ContextFileTracker
from swarm.worker.worker import Worker, WorkerState


def _make_worker(name: str = "w1", state: WorkerState = WorkerState.BUZZING) -> Worker:
    w = Worker(name=name, path=f"/tmp/{name}")
    w.state = state
    return w


class TestContextFileTracking:
    def test_appends_unique_paths_capped(self) -> None:
        tracker = ContextFileTracker()
        worker = _make_worker("w1", state=WorkerState.BUZZING)
        content = "\n".join(f"Read('/tmp/file{i}')" for i in range(15))
        tracker.check(worker, content)
        # _MAX_CONTEXT_FILES is 10
        assert len(worker.last_context_files) <= 10

    def test_skipped_when_not_buzzing(self) -> None:
        tracker = ContextFileTracker()
        worker = _make_worker("w1", state=WorkerState.RESTING)
        tracker.check(worker, "Read('/tmp/a')")
        assert worker.last_context_files == []

    def test_duplicate_paths_not_re_added(self) -> None:
        tracker = ContextFileTracker()
        worker = _make_worker("w1", state=WorkerState.BUZZING)
        tracker.check(worker, "Read('/tmp/a')")
        tracker.check(worker, "Read('/tmp/a')")
        assert worker.last_context_files == ["/tmp/a"]
