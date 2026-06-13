"""Daily digest — one-message summary of the last 24h of swarm activity.

Pure formatting over ``compute_throughput`` output so it's unit-testable;
the daemon's digest loop computes the summary and pushes the rendered
message through the notification bus once a day.
"""

from __future__ import annotations

from typing import Any


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def build_digest(summary: dict[str, Any]) -> tuple[str, str]:
    """Render a throughput summary into a (title, message) pair."""
    completed = summary.get("completed", 0)
    failed = summary.get("failed", 0)
    created = summary.get("created", 0)
    title = f"Swarm daily digest: {completed} done, {failed} failed"

    lines = [
        f"Last 24h: {completed} completed, {failed} failed, {created} new tasks.",
        f"Avg completion time: {_fmt_duration(summary.get('avg_completion_seconds'))}.",
    ]
    workers = summary.get("workers") or []
    if workers:
        top = ", ".join(f"{w['worker']} ({w['completed']})" for w in workers[:3])
        lines.append(f"Top workers: {top}.")
    backlog = summary.get("backlog") or {}
    open_count = sum(
        backlog.get(status, 0)
        for status in ("backlog", "unassigned", "assigned", "active", "blocked")
    )
    lines.append(f"Open tasks on the board: {open_count}.")
    return title, " ".join(lines)
