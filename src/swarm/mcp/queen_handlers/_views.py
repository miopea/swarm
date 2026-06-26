"""Queen MCP handlers for the worker-state and task-board views.

Extracted from ``mcp/queen_tools.py`` (task #519).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import QueenViewTaskBoardArgs, QueenViewWorkerStateArgs
from swarm.mcp.queen_handlers._common import _assert_queen, _clamp
from swarm.mcp.types import HandlerResult

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


# #876: BLOCKED is an OPEN (tracked, awaiting-resume) state, not a closed one.
_OPEN_STATUSES = {"backlog", "unassigned", "assigned", "active", "blocked"}
_DONE_STATUSES = {"done"}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "queen_view_worker_state",
        "description": (
            "Inspect worker state to answer 'why is this stuck?' or 'what is hub doing "
            "right now?'. Returns state, current task, recent PTY output, and token usage. "
            "Omit 'worker' to list every worker with a one-line summary; pass a name to "
            "drill in with PTY tail. Use this BEFORE queen_interrupt_worker or any action "
            "so you're operating on current reality, not stale assumptions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {
                    "type": "string",
                    "description": (
                        "Worker name to inspect. Empty string returns a summary across all workers."
                    ),
                },
                "lines": {
                    "type": "integer",
                    "description": (
                        "Recent PTY lines to include when 'worker' is set. Default 50, max 500."
                    ),
                    "default": 50,
                },
            },
            "examples": [
                {"worker": "hub", "lines": 80},
                {"worker": ""},
            ],
        },
    },
    {
        "name": "queen_view_task_board",
        "description": (
            "Return the task board — open tasks first, then recently-closed. Filter by "
            "status ('open'|'backlog'|'unassigned'|'assigned'|'active'|'done'|'failed') or "
            "by assigned worker. Useful when the operator asks 'what's in flight?' or when "
            "reasoning about whether to propose a new assignment."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": (
                        "Filter by status group: 'open' "
                        "(backlog|unassigned|assigned|active), 'done', 'failed', or a "
                        "specific status value. Empty returns all."
                    ),
                },
                "worker": {
                    "type": "string",
                    "description": "Filter to tasks assigned to this worker.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return. Default 50, max 500.",
                    "default": 50,
                },
            },
            "examples": [
                {"status": "open"},
                {"worker": "hub", "limit": 20},
            ],
        },
    },
]


def _handle_view_worker_state(
    d: SwarmDaemon, worker_name: str, args: QueenViewWorkerStateArgs
) -> HandlerResult:
    """Return both a markdown text summary and a structured JSON sidecar.

    Claude Code 2.1.x prefers ``structuredContent`` when present, so the
    Queen sees the same data both as human-readable text (for thread
    logs) and as queryable JSON (for reasoning). On the not-found error
    path we fall back to the legacy list shape — there's no structured
    payload to deliver and an empty/null sidecar would mislead clients.
    """
    err = _assert_queen(worker_name)
    if err:
        return err

    target = (args.get("worker") or "").strip()
    lines = _clamp(args.get("lines", 50), 50, 1, 500)

    if not target:
        # Summary across all workers.
        summaries: list[str] = []
        workers_payload: list[dict[str, Any]] = []
        for w in d.workers:
            active = d.task_board.active_tasks_for_worker(w.name) if d.task_board else []
            task = active[0] if active else None
            task_info = f"task #{task.number}: {task.title}" if task else "idle"
            kind_tag = " (queen)" if w.is_queen else ""
            summaries.append(
                f"{w.name}{kind_tag} [{w.display_state.value}] — {task_info} "
                f"(ctx {int(w.context_pct * 100)}%)"
            )
            workers_payload.append(
                {
                    "name": w.name,
                    "kind": getattr(w, "kind", "claude"),
                    "is_queen": bool(w.is_queen),
                    "state": w.display_state.value,
                    "context_pct": float(w.context_pct),
                    "task": (
                        {
                            "number": task.number,
                            "title": task.title,
                            "status": task.status.value,
                        }
                        if task
                        else None
                    ),
                }
            )
        text = "\n".join(summaries) if summaries else "No workers."
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"workers": workers_payload},
        }

    worker = next((w for w in d.workers if w.name == target), None)
    if worker is None:
        # Error path: legacy list shape, no half-built sidecar.
        return [{"type": "text", "text": f"Worker '{target}' not found."}]

    pty_tail = ""
    if worker.process is not None:
        try:
            pty_tail = worker.process.get_content(lines) or ""
        except Exception:
            pty_tail = "(pty read failed)"

    active = d.task_board.active_tasks_for_worker(worker.name) if d.task_board else []
    task = active[0] if active else None
    task_line = f"#{task.number} [{task.status.value}] {task.title}" if task else "no active task"
    usage = worker.usage.to_dict()
    body = (
        f"worker: {worker.name} (kind={worker.kind})\n"
        f"state:  {worker.display_state.value} (for {int(worker.state_duration)}s)\n"
        f"task:   {task_line}\n"
        f"usage:  in={usage['input_tokens']} out={usage['output_tokens']} "
        f"ctx={int(worker.context_pct * 100)}% cost=${worker.usage.cost_usd:.4f}\n"
        f"--- pty tail ({lines} lines) ---\n{pty_tail}"
    )
    return {
        "content": [{"type": "text", "text": body}],
        "structuredContent": {
            "worker": {
                "name": worker.name,
                "kind": worker.kind,
                "is_queen": bool(worker.is_queen),
                "state": worker.display_state.value,
                "state_duration_seconds": int(worker.state_duration),
                "context_pct": float(worker.context_pct),
                "usage": {
                    "input_tokens": int(usage.get("input_tokens", 0)),
                    "output_tokens": int(usage.get("output_tokens", 0)),
                    "cost_usd": float(worker.usage.cost_usd),
                },
                "task": (
                    {
                        "number": task.number,
                        "title": task.title,
                        "status": task.status.value,
                    }
                    if task
                    else None
                ),
                "pty_tail_lines": lines,
            },
        },
    }


def _handle_view_task_board(
    d: SwarmDaemon, worker_name: str, args: QueenViewTaskBoardArgs
) -> HandlerResult:
    err = _assert_queen(worker_name)
    if err:
        return err
    status_filter = (args.get("status") or "").strip().lower()
    worker_filter = (args.get("worker") or "").strip()
    limit = _clamp(args.get("limit", 50), 50, 1, 500)

    tasks = list(d.task_board.all_tasks)
    if status_filter == "open":
        tasks = [t for t in tasks if t.status.value in _OPEN_STATUSES]
    elif status_filter == "done":
        tasks = [t for t in tasks if t.status.value in _DONE_STATUSES]
    elif status_filter:
        tasks = [t for t in tasks if t.status.value == status_filter]
    if worker_filter:
        tasks = [t for t in tasks if t.assigned_worker == worker_filter]

    # Open first, most recent first within each group.
    def _key(t: Any) -> tuple[int, float]:
        is_open = t.status.value in _OPEN_STATUSES
        recency = -(t.completed_at or 0.0) if not is_open else -float(t.number)
        return (0 if is_open else 1, recency)

    tasks.sort(key=_key)
    tasks = tasks[:limit]
    if not tasks:
        return [{"type": "text", "text": "No tasks match."}]
    lines = [
        f"#{t.number} [{t.status.value}] {t.title} ({t.assigned_worker or 'unassigned'})"
        for t in tasks
    ]
    payload = [
        {
            "number": t.number,
            "status": t.status.value,
            "title": t.title,
            "assigned_worker": t.assigned_worker or None,
            "is_open": t.status.value in _OPEN_STATUSES,
            "completed_at": t.completed_at,
        }
        for t in tasks
    ]
    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "structuredContent": {
            "tasks": payload,
            "filters": {
                "status": status_filter or None,
                "worker": worker_filter or None,
                "limit": limit,
            },
            "count": len(payload),
        },
    }


HANDLERS = {
    "queen_view_worker_state": _handle_view_worker_state,
    "queen_view_task_board": _handle_view_task_board,
}
