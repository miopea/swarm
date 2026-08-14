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
            "status ('open'|'awaiting-operator'|'backlog'|'unassigned'|"
            "'assigned'|'active'|'done'|'failed') or "
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


def _open_prompt_payload(pty_tail: str) -> dict[str, Any] | None:
    """Parse an open selection prompt out of the PTY tail, or None (#1608).

    Returns the fingerprint `queen_answer_prompt` requires. Nothing else in the stack
    produces one, so without this the answer tool cannot be called at all — a mechanism
    that ships, passes its tests, and is unusable in practice.

    Best-effort: a parse failure yields None rather than raising, because this is a
    read-only enrichment on a tool the Queen uses for everything else.
    """
    try:
        from swarm.pty.prompt_options import parse_open_prompt

        prompt = parse_open_prompt(pty_tail)
    except Exception:
        return None
    if prompt is None:
        return None
    return {
        "fingerprint": prompt.fingerprint,
        "options": [
            {"number": o.number, "label": o.label, "cursored": o.cursored} for o in prompt.options
        ],
    }


def _handle_view_worker_state(
    d: SwarmDaemon, worker_name: str, args: QueenViewWorkerStateArgs
) -> HandlerResult:
    """Return both a markdown text summary and a structured JSON sidecar.

    Claude Code 2.1.x prefers ``structuredContent`` when present, so the
    Queen sees the same data both as human-readable text (for thread
    logs) and as queryable JSON (for reasoning).

    THE NOT-FOUND PATH ALSO CARRIES A SIDECAR (#1432), REVERSING AN EARLIER
    DECISION. It used to return the bare list shape on the reasoning that
    "an empty/null sidecar would mislead clients". That reasoning had it
    backwards: with no ``structuredContent`` key at all, the natural way to
    consume this tool — ``result["structuredContent"]["worker"]`` — RAISES
    on a mistyped worker name instead of reading an error. The Queen is the
    only caller and typos are routine, so it was reachable in normal use.
    2026.8.10.20 sharpened it by putting ``pty_tail`` in the structured
    payload, which made that payload the whole reason to call the tool.

    #1535 ADDED ``mode``, which is "summary" or "single" on EVERY structured exit.
    This tool has two genuinely different result shapes — the summary keys
    ``workers`` (plural), a targeted lookup keys ``worker`` (singular) — so
    ``structuredContent["worker"]`` still raised on the summary path even after
    #1432. Rather than pad the summary with a null ``worker`` (a payload carrying
    both a null ``worker`` and a populated ``workers`` reads like a bug), the shape
    now says which shape it is. That is the rule below applied literally: read a
    FIELD to learn what you have, never infer it from which key happens to exist.

    THE RULE, narrower than "every handler": WITHIN A TOOL THAT EVER EMITS
    ``structuredContent``, EVERY EXIT EMITS IT. A client should branch on a
    FIELD, never on the response's Python type. This deliberately diverges
    from the bare-list convention used by most ``queen_handlers`` returns
    (51 bare lists vs 7 structured dicts at the time of writing) — those
    handlers never emit a sidecar on ANY path, so they are already
    self-consistent and are left alone. The inconsistency only bites where
    a tool invites a client into ``structuredContent`` and then withdraws it.
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
            active = (
                d.task_board.assigned_or_active_tasks_for_worker(w.name) if d.task_board else []
            )
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
            "structuredContent": {"mode": "summary", "workers": workers_payload},
        }

    worker = next((w for w in d.workers if w.name == target), None)
    if worker is None:
        # #1432: dict shape WITH a sidecar — see the rule in this handler's
        # docstring. ``error`` is the discriminator a client branches on;
        # ``requested`` echoes the name that missed, so a typo is legible
        # without re-reading the text block. The text block itself is
        # unchanged, so text-only clients see exactly what they saw before.
        return {
            "content": [{"type": "text", "text": f"Worker '{target}' not found."}],
            "structuredContent": {
                "mode": "single",
                "worker": None,
                "error": "not_found",
                "requested": target,
            },
        }

    pty_tail = ""
    if worker.process is not None:
        try:
            pty_tail = worker.process.get_content(lines) or ""
        except Exception:
            pty_tail = "(pty read failed)"

    active = d.task_board.assigned_or_active_tasks_for_worker(worker.name) if d.task_board else []
    task = active[0] if active else None
    task_line = f"#{task.number} [{task.status.value}] {task.title}" if task else "no active task"
    usage = worker.usage.to_dict()
    # #1608: surface the OPEN PROMPT, if any. queen_answer_prompt requires a fingerprint,
    # and nothing else produces one — without this the Queen would have to hash normalised
    # option labels by hand, which makes a shipped tool unusable. That is the same
    # "looks operational, is inert" shape this ticket exists to fix, so it is not optional.
    prompt_block = _open_prompt_payload(pty_tail)
    prompt_line = ""
    if prompt_block:
        opts = "\n".join(
            f"    {'>' if o['cursored'] else ' '} {o['number']}. {o['label']}"
            for o in prompt_block["options"]
        )
        prompt_line = (
            f"\n--- OPEN SELECTION PROMPT (fingerprint {prompt_block['fingerprint']}) ---\n"
            f"{opts}\n"
            f"    answer:  queen_answer_prompt(worker='{worker.name}', option=N, "
            f"fingerprint='{prompt_block['fingerprint']}')\n"
            f"    dismiss: queen_dismiss_prompt(worker='{worker.name}', reason=...)\n"
            f"    NOTE queen_prompt_worker will be REFUSED while this is open, and "
            f"queen_interrupt_worker does not close a picker.\n"
        )
    body = (
        f"worker: {worker.name} (kind={worker.kind})\n"
        f"state:  {worker.display_state.value} (for {int(worker.state_duration)}s)\n"
        f"task:   {task_line}\n"
        f"usage:  in={usage['input_tokens']} out={usage['output_tokens']} "
        f"ctx={int(worker.context_pct * 100)}% cost=${worker.usage.cost_usd:.4f}\n"
        f"{prompt_line}"
        f"--- pty tail ({lines} lines) ---\n{pty_tail}"
    )
    return {
        "content": [{"type": "text", "text": body}],
        "structuredContent": {
            "mode": "single",
            "prompt": prompt_block,
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
                # THE FIX (reported by another operator's Queen, verified 2026-08-10).
                # pty_tail is read at the top of this handler and was placed in the TEXT
                # block only — the structured payload carried `pty_tail_lines` (the line
                # COUNT) and never the content. A client reading structuredContent got a
                # field promising 40 lines and no lines, which is worse than an absent
                # key: it reads as a working contract.
                #
                # The dashboard is unaffected — it reads PTY output over /ws/terminal, a
                # separate path — so this only ever hit MCP clients, i.e. the Queen.
                "pty_tail_lines": lines,
                "pty_tail": pty_tail,
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
    if status_filter == "awaiting-operator":
        # #1070: the Queen's batching view — every task finished as far as the
        # swarm is concerned and waiting on a HUMAN decision. Surfacing these
        # as one class is the point: they become a single operator ask instead
        # of being relayed one at a time.
        tasks = [t for t in tasks if t.is_awaiting_operator]
    elif status_filter == "open":
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
