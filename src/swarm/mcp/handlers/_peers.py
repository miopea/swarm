"""Handler for the ``swarm_query_peers`` MCP tool (feature B11).

Gives a worker a **read-only** snapshot of its peers' live state so it can
make an informed handoff decision. Deliberately exposes no action surface:
workers cannot interrupt each other (a hierarchy guardrail), so this tool
returns facts only — to act, the worker still uses ``swarm_create_task``
(which routes through the dispatch + plan-mode gate) or
``swarm_send_message``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import QueryPeersArgs
from swarm.mcp.types import HandlerResult
from swarm.tasks.task import TaskStatus
from swarm.worker.worker import WorkerState, format_duration

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


# States that count as "idle / potentially available for a handoff". WAITING
# (needs operator input) and STUNG (dead) are NOT idle — a worker should route
# around them, so they report idle_seconds=0 like a busy peer.
_IDLE_STATES = (WorkerState.RESTING, WorkerState.SLEEPING)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_query_peers",
        "description": (
            "Read-only snapshot of your peer workers' live state. Call this when you're "
            "deciding whether to hand work off, or before creating a task for another "
            "worker, to check who's actually free. Returns, per running peer (excluding the Queen "
            "and yourself): state (BUZZING/RESTING/SLEEPING/WAITING/STUNG), current task, "
            "context-window %, how long it's been idle, and how many tasks are queued "
            "behind it. Idle peers come first (longest-idle first). "
            "This tool does NOT let you interrupt, message, or assign work to a peer — "
            "workers cannot interrupt each other. To act on what you learn, create a task "
            "with swarm_create_task (it routes through the normal dispatch gate) or send "
            "a heads-up with swarm_send_message. A peer that reads RESTING but has a "
            "non-zero queue is NOT free — don't pile on."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "description": (
                        "Optional: only return peers in this state "
                        "(e.g. 'RESTING' to find idle workers). Omit for all peers."
                    ),
                },
            },
            "examples": [{}, {"state": "RESTING"}],
        },
    },
]


def _peer_row(d: SwarmDaemon, worker: Any, now: float) -> dict[str, Any]:
    state = worker.display_state
    active = d.task_board.active_tasks_for_worker(worker.name)
    current = next((t for t in active if t.status == TaskStatus.ACTIVE), None)
    queued = sum(1 for t in active if t.status == TaskStatus.ASSIGNED)
    idle_seconds = int(now - worker.state_since) if state in _IDLE_STATES else 0
    return {
        "name": worker.name,
        "state": state.value,
        "current_task": current.title if current else None,
        "current_task_number": current.number if current else None,
        "context_pct": round(worker.context_pct, 3),
        "idle_seconds": max(0, idle_seconds),
        "queued_count": queued,
    }


def _format_peer_line(row: dict[str, Any]) -> str:
    parts = [f"{row['name']} — {row['state']}"]
    if row["idle_seconds"]:
        parts[0] += f" {format_duration(row['idle_seconds'])}"
    parts.append(f"ctx {round(row['context_pct'] * 100)}%")
    if row["queued_count"]:
        parts.append(f"queue {row['queued_count']}")
    line = ", ".join(parts)
    if row["current_task"]:
        line += f' — "{row["current_task"]}" (#{row["current_task_number"]})'
    return line


def _handle_query_peers(d: SwarmDaemon, worker_name: str, args: QueryPeersArgs) -> HandlerResult:
    if not getattr(d, "task_board", None):
        return [{"type": "text", "text": "No task board available."}]

    state_filter = (args.get("state") or "").strip().upper() or None
    now = time.time()

    rows: list[dict[str, Any]] = []
    for w in d.workers:
        if w.is_queen or w.name == worker_name:
            continue
        row = _peer_row(d, w, now)
        if state_filter and row["state"] != state_filter:
            continue
        rows.append(row)

    # Idle peers first (longest-idle first), then busy (idle_seconds == 0 last).
    rows.sort(key=lambda r: (r["idle_seconds"] == 0, -r["idle_seconds"]))

    if not rows:
        text = (
            "No other workers running."
            if not state_filter
            else (f"No peers in state {state_filter}.")
        )
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"peers": [], "total": 0},
        }

    text = "\n".join(_format_peer_line(r) for r in rows)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": {"peers": rows, "total": len(rows)},
    }


HANDLERS = {"swarm_query_peers": _handle_query_peers}
