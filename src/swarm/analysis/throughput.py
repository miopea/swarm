"""Task throughput analytics — completion rates, durations, per-worker stats.

Pure functions over the in-memory task board so the API route stays a
thin wrapper and the math is unit-testable without a daemon.
"""

from __future__ import annotations

import statistics
import time
from typing import Any

from swarm.tasks.task import SwarmTask, TaskStatus


def _completion_seconds(task: SwarmTask) -> float | None:
    """Wall-clock duration from start (or creation) to completion."""
    if task.completed_at is None:
        return None
    start = task.started_at if task.started_at is not None else task.created_at
    if start is None or task.completed_at < start:
        return None
    return task.completed_at - start


def compute_throughput(
    tasks: list[SwarmTask],
    *,
    now: float | None = None,
    window_days: int = 7,
) -> dict[str, Any]:
    """Summarize task throughput over the trailing ``window_days``.

    Counts are windowed on the relevant event timestamp (created_at for
    ``created``, completed_at for ``completed``/``failed``); the backlog
    snapshot reflects *current* statuses regardless of window.
    """
    if now is None:
        now = time.time()
    window_days = max(1, window_days)
    since = now - window_days * 86_400.0

    created = 0
    completed = 0
    failed = 0
    durations: list[float] = []
    per_worker: dict[str, dict[str, Any]] = {}
    backlog: dict[str, int] = {}

    for t in tasks:
        backlog[t.status.value] = backlog.get(t.status.value, 0) + 1
        if t.created_at and t.created_at >= since:
            created += 1
        # FAILED tasks may have no completed_at — fall back to updated_at
        # so they don't silently vanish from the failure count.
        event_ts = t.completed_at or t.updated_at or 0.0
        if event_ts < since:
            continue
        if t.status not in (TaskStatus.DONE, TaskStatus.FAILED):
            continue
        worker = t.assigned_worker or "(unassigned)"
        stats = per_worker.setdefault(
            worker,
            {"worker": worker, "completed": 0, "failed": 0, "durations": []},
        )
        if t.status == TaskStatus.DONE:
            completed += 1
            stats["completed"] += 1
            dur = _completion_seconds(t)
            if dur is not None:
                durations.append(dur)
                stats["durations"].append(dur)
        else:
            failed += 1
            stats["failed"] += 1

    workers = []
    for stats in sorted(per_worker.values(), key=lambda s: -s["completed"]):
        durs = stats.pop("durations")
        stats["avg_completion_seconds"] = round(sum(durs) / len(durs), 1) if durs else None
        workers.append(stats)

    return {
        "window_days": window_days,
        "since": since,
        "created": created,
        "completed": completed,
        "failed": failed,
        "completed_per_day": round(completed / window_days, 2),
        "avg_completion_seconds": (
            round(sum(durations) / len(durations), 1) if durations else None
        ),
        "median_completion_seconds": (
            round(statistics.median(durations), 1) if durations else None
        ),
        "workers": workers,
        "backlog": backlog,
    }
