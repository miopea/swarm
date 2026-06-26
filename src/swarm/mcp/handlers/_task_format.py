"""Task-formatting + lookup helpers shared by the task handlers.

Extracted from ``mcp/tools.py`` (task #518). These are pure-function
helpers used to render `SwarmTask` rows for the MCP `swarm_task_status`
output and to project tasks into the structuredContent payload shape.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

from swarm.mcp.types import TextContent

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon
    from swarm.tasks.task import SwarmTask


_TASK_STATUS_DEFAULT_LIMIT = 50
_TASK_STATUS_MAX_LIMIT = 500
# #876: BLOCKED is an OPEN state — a task held on an internal/external blocker
# is tracked work awaiting resume, NOT a closed task. It must stay in the
# default "mine"/open views and sort with open work (top), not sink into the
# done/failed bucket. (It is still excluded from ``active_tasks``, so the
# idle-watcher does not nudge it — visibility and nudge-gating are separate.)
_OPEN_STATUSES = {"backlog", "unassigned", "assigned", "active", "blocked"}


def _format_task_line(t: SwarmTask) -> str:
    w = t.assigned_worker or "unassigned"
    return f"#{t.number} [{t.status.value}] {t.title} ({w})"


def _enum_value(v: Enum | str | None) -> str:
    if v is None:
        return ""
    return v.value if isinstance(v, Enum) else str(v)


def _format_task_meta_line(t: SwarmTask) -> str:
    parts = [f"worker={t.assigned_worker or 'unassigned'}"]
    if getattr(t, "priority", None):
        parts.append(f"priority={_enum_value(t.priority)}")
    if getattr(t, "task_type", None):
        parts.append(f"type={_enum_value(t.task_type)}")
    if getattr(t, "tags", None):
        parts.append(f"tags={','.join(t.tags)}")
    return "  " + " | ".join(parts)


def _format_cross_project_line(t: SwarmTask) -> str | None:
    if not getattr(t, "is_cross_project", False):
        return None
    parts: list[str] = []
    if getattr(t, "source_worker", None):
        parts.append(f"from={t.source_worker}")
    if getattr(t, "target_worker", None):
        parts.append(f"to={t.target_worker}")
    if getattr(t, "dependency_type", None):
        parts.append(f"dep_type={_enum_value(t.dependency_type)}")
    return ("  cross-project: " + " | ".join(parts)) if parts else None


def _format_section(label: str, items: list[str], bullet: str = "  - ") -> list[str]:
    if not items:
        return []
    out = ["", f"{label}:"]
    out.extend(f"{bullet}{x}" for x in items)
    return out


def _format_task_detail(t: SwarmTask) -> str:
    """Multi-line view used for single-task lookups by number — gives the
    worker the full context (description, acceptance criteria, attachments,
    etc.) instead of just the title."""
    lines = [f"#{t.number} [{t.status.value}] {t.title}", _format_task_meta_line(t)]

    cross = _format_cross_project_line(t)
    if cross:
        lines.append(cross)

    deps = getattr(t, "depends_on", None) or []
    if deps:
        formatted_deps = [f"#{d}" if isinstance(d, int) else str(d) for d in deps]
        lines.append("  depends_on: " + ", ".join(formatted_deps))

    if getattr(t, "jira_key", None):
        lines.append(f"  jira: {t.jira_key}")

    # #876: surface what a BLOCKED task is waiting on so the operator/worker
    # sees the unblock condition without digging into history.
    ext_ref = (getattr(t, "external_blocker_ref", None) or "").strip()
    if ext_ref:
        lines.append(f"  blocked-on-external: {ext_ref}")
    block_reason = (getattr(t, "block_reason", None) or "").strip()
    if block_reason:
        lines.append(f"  block_reason: {block_reason}")

    desc = (getattr(t, "description", None) or "").strip()
    if desc:
        lines.extend(["", "Description:", desc])

    acceptance = getattr(t, "acceptance_criteria", None) or []
    refs = getattr(t, "context_refs", None) or []
    attachments = getattr(t, "attachments", None) or []
    lines.extend(_format_section("Acceptance criteria", acceptance))
    lines.extend(_format_section("Context refs", refs))
    lines.extend(_format_section("Attachments", attachments))

    if t.status.value == "done" and getattr(t, "resolution", None):
        lines.extend(["", "Resolution:", t.resolution])

    return "\n".join(lines)


def _sort_tasks_for_display(tasks: list[SwarmTask]) -> list[SwarmTask]:
    """Open tasks first (newest-by-number DESC), then completed/failed by
    completed_at DESC (falling back to number DESC). Older implementations
    sorted ASC and sliced the head, which hid newer assignments — see task
    #142."""

    def key(t: SwarmTask) -> tuple[int, float, int]:
        is_open = t.status.value in _OPEN_STATUSES
        # Primary: open first (0) vs closed (1).
        # Secondary: most recent first — completed_at for closed tasks,
        # or number for open ones (a proxy for recency without requiring
        # a db timestamp).
        recency = -(t.completed_at or 0.0) if not is_open else -float(t.number)
        return (0 if is_open else 1, recency, -t.number)

    return sorted(tasks, key=key)


def _lookup_task_by_number(d: SwarmDaemon, raw: int | str | None) -> list[TextContent]:
    # ``int(None)`` raises TypeError → caught below and reported with the
    # caller-facing snippet. Narrow ``raw`` first so the conversion only
    # sees the supported types.
    if raw is None:
        return [{"type": "text", "text": f"Invalid 'number': {raw!r}"}]
    try:
        target = int(raw)
    except (TypeError, ValueError):
        return [{"type": "text", "text": f"Invalid 'number': {raw!r}"}]
    for t in d.task_board.all_tasks:
        if t.number == target:
            return [{"type": "text", "text": _format_task_detail(t)}]
    return [{"type": "text", "text": f"No task found with number #{target}."}]


def _coerce_limit(raw: int | str | None) -> int | str:
    """Return a clamped integer limit or a user-facing error string."""
    if raw is None:
        return f"Invalid 'limit': {raw!r}"
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return f"Invalid 'limit': {raw!r}"
    if limit < 1:
        return "'limit' must be >= 1."
    return min(limit, _TASK_STATUS_MAX_LIMIT)


def _apply_task_filter(
    tasks: list[SwarmTask], filt: str, worker_name: str, *, include_completed: bool
) -> list[SwarmTask]:
    if filt == "backlog":
        return [t for t in tasks if t.status.value == "backlog"]
    if filt == "unassigned":
        return [t for t in tasks if t.status.value == "unassigned"]
    if filt == "active":
        return [t for t in tasks if t.status.value == "active"]
    if filt == "assigned":
        return [t for t in tasks if t.assigned_worker is not None]
    if filt == "mine":
        mine = [t for t in tasks if t.assigned_worker == worker_name]
        # Default for 'mine' surfaces actionable work. Completed/failed rows
        # used to crowd out newer assignments from the old fixed 20-row
        # window (task #142). Opt back in with include_completed=True.
        if not include_completed:
            mine = [t for t in mine if t.status.value in _OPEN_STATUSES]
        return mine
    return tasks


def _task_to_payload(t: SwarmTask) -> dict[str, Any]:
    """Project a SwarmTask onto a JSON-friendly dict for structuredContent.

    Carries only the fields the model needs to reason about — title,
    status, assignment, type/priority, criteria, dependencies. Avoids
    leaking internal fields (raw timestamps beyond completed_at, cost
    accounting, verifier internals) that would bloat the payload
    without helping the Queen.
    """
    return {
        "number": t.number,
        "title": t.title,
        "status": t.status.value,
        "assigned_worker": t.assigned_worker or None,
        "priority": _enum_value(getattr(t, "priority", None)),
        "task_type": _enum_value(getattr(t, "task_type", None)),
        "tags": list(getattr(t, "tags", []) or []),
        "depends_on": list(getattr(t, "depends_on", []) or []),
        "acceptance_criteria": list(getattr(t, "acceptance_criteria", []) or []),
        "is_cross_project": bool(getattr(t, "is_cross_project", False)),
        "source_worker": getattr(t, "source_worker", "") or None,
        "target_worker": getattr(t, "target_worker", "") or None,
        "completed_at": getattr(t, "completed_at", None),
    }
