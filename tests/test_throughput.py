"""Tests for analysis/throughput.py — task throughput analytics."""

import time

from swarm.analysis.throughput import compute_throughput

from swarm.tasks.task import SwarmTask, TaskStatus

NOW = 1_750_000_000.0
DAY = 86_400.0


def make_task(
    status: TaskStatus = TaskStatus.DONE,
    created_offset: float = -2 * DAY,
    started_offset: float | None = -2 * DAY,
    completed_offset: float | None = -2 * DAY + 600,
    worker: str | None = "alice",
) -> SwarmTask:
    t = SwarmTask(title="t", status=status, assigned_worker=worker)
    t.created_at = NOW + created_offset
    t.started_at = None if started_offset is None else NOW + started_offset
    t.completed_at = None if completed_offset is None else NOW + completed_offset
    return t


class TestComputeThroughput:
    def test_counts_completed_in_window(self):
        tasks = [make_task(), make_task(), make_task(status=TaskStatus.FAILED)]
        s = compute_throughput(tasks, now=NOW, window_days=7)
        assert s["completed"] == 2
        assert s["failed"] == 1
        assert s["window_days"] == 7

    def test_excludes_tasks_outside_window(self):
        old = make_task(
            created_offset=-30 * DAY, started_offset=-30 * DAY, completed_offset=-29 * DAY
        )
        recent = make_task()
        s = compute_throughput([old, recent], now=NOW, window_days=7)
        assert s["completed"] == 1
        assert s["created"] == 1

    def test_tasks_per_day(self):
        tasks = [make_task() for _ in range(14)]
        s = compute_throughput(tasks, now=NOW, window_days=7)
        assert s["completed_per_day"] == 2.0

    def test_avg_and_median_completion_seconds(self):
        # durations: 600s, 1200s, 3000s → avg 1600, median 1200
        tasks = [
            make_task(completed_offset=-2 * DAY + 600),
            make_task(completed_offset=-2 * DAY + 1200),
            make_task(completed_offset=-2 * DAY + 3000),
        ]
        s = compute_throughput(tasks, now=NOW, window_days=7)
        assert s["avg_completion_seconds"] == 1600.0
        assert s["median_completion_seconds"] == 1200.0

    def test_falls_back_to_created_at_when_never_started(self):
        t = make_task(started_offset=None, completed_offset=-2 * DAY + 900)
        s = compute_throughput([t], now=NOW, window_days=7)
        assert s["avg_completion_seconds"] == 900.0

    def test_per_worker_breakdown(self):
        tasks = [
            make_task(worker="alice"),
            make_task(worker="alice"),
            make_task(worker="bob", status=TaskStatus.FAILED, completed_offset=None),
        ]
        s = compute_throughput(tasks, now=NOW, window_days=7)
        by_name = {w["worker"]: w for w in s["workers"]}
        assert by_name["alice"]["completed"] == 2
        assert by_name["bob"]["failed"] == 1

    def test_backlog_snapshot_counts_current_statuses(self):
        tasks = [
            make_task(status=TaskStatus.BACKLOG, completed_offset=None),
            make_task(status=TaskStatus.ASSIGNED, completed_offset=None),
            make_task(status=TaskStatus.ACTIVE, completed_offset=None),
            make_task(),
        ]
        s = compute_throughput(tasks, now=NOW, window_days=7)
        assert s["backlog"]["backlog"] == 1
        assert s["backlog"]["assigned"] == 1
        assert s["backlog"]["active"] == 1

    def test_empty_board(self):
        s = compute_throughput([], now=NOW, window_days=7)
        assert s["completed"] == 0
        assert s["avg_completion_seconds"] is None
        assert s["median_completion_seconds"] is None
        assert s["workers"] == []

    def test_defaults_use_wall_clock(self):
        t = SwarmTask(title="t", status=TaskStatus.DONE, assigned_worker="a")
        t.completed_at = time.time() - 60
        t.started_at = t.completed_at - 120
        t.created_at = t.started_at
        s = compute_throughput([t], window_days=7)
        assert s["completed"] == 1
