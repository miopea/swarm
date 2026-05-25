"""Queen-only MCP tools — the coordinator's introspection surface.

These tools are exposed on the shared swarm MCP server but gated by
caller identity: only the MCP client identified as ``worker=queen``
may invoke them.  Every handler calls :func:`_assert_queen` first; a
non-queen caller sees a permission-denied text response.

Read-only tools ship in the foundation pass.  Action tools
(``queen_send_message``, ``queen_interrupt_worker`` …) are reserved
for the second pass alongside the chat UI.

See `docs/specs/interactive-queen.md` §4.2 for the full surface.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import (
    QueenForceCompleteTaskArgs,
    QueenInterruptWorkerArgs,
    QueenPostThreadArgs,
    QueenPromptWorkerArgs,
    QueenQueryLearningsArgs,
    QueenReassignTaskArgs,
    QueenReplyArgs,
    QueenSaveLearningArgs,
    QueenUpdateThreadArgs,
    QueenViewBuzzLogArgs,
    QueenViewDroneActionsArgs,
    QueenViewMessagesArgs,
    QueenViewMessageStreamArgs,
    QueenViewTaskBoardArgs,
    QueenViewWorkerStateArgs,
)
from swarm.worker.worker import QUEEN_WORKER_NAME

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


_PERMISSION_DENIED = [
    {
        "type": "text",
        "text": (
            "Permission denied: this tool is only available to the Queen. "
            f"Caller identity must be '{QUEEN_WORKER_NAME}'."
        ),
    }
]


def _assert_queen(worker_name: str) -> list[dict[str, Any]] | None:
    """Return an error payload if *worker_name* is not the Queen, else None."""
    if worker_name != QUEEN_WORKER_NAME:
        return _PERMISSION_DENIED
    return None


# ---------------------------------------------------------------------------
# Tool definitions (MCP schema format)
# ---------------------------------------------------------------------------


QUEEN_TOOLS: list[dict[str, Any]] = [
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
    {
        "name": "queen_view_messages",
        "description": (
            "Read the inter-worker message log — findings, warnings, dependencies workers "
            "have sent each other. Filter by worker (either side) or by age. Call this when "
            "tracing 'why is worker X confused?' — often they got a warning they didn't heed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {
                    "type": "string",
                    "description": (
                        "Filter to messages involving this worker (sender OR recipient)."
                    ),
                },
                "since_seconds": {
                    "type": "integer",
                    "description": "Only messages from the last N seconds. Default 3600 (1h).",
                    "default": 3600,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return. Default 50, max 500.",
                    "default": 50,
                },
                "full": {
                    "type": "boolean",
                    "description": (
                        "When true, return each message's COMPLETE body instead of "
                        "the 160-char preview. Use this when you need to relay a "
                        "worker's message verbatim (e.g. a decision memo to the "
                        "operator). Default false keeps the list-view ergonomic "
                        "for scanning; narrow with ``worker`` + ``limit`` before "
                        "flipping to ``full=true`` so you don't page through "
                        "many KB of unrelated chat."
                    ),
                    "default": False,
                },
            },
            "examples": [
                {"worker": "hub", "since_seconds": 1800},
                {"since_seconds": 3600, "limit": 30},
                {"worker": "project-root", "limit": 1, "full": True},
            ],
        },
    },
    {
        "name": "queen_view_message_stream",
        "description": (
            "Inter-worker message feed with recipient-state joined. Call this when "
            "you want to see who has unread messages sitting in their inbox while "
            "they're idle — those are the workers most likely to need a nudge. "
            "Surfaces every message (sender → recipient, type, preview, age) in a "
            "recent window and tags each one with whether the recipient is idle "
            "AND hasn't read it yet. Use ``actionable_only=true`` to filter down "
            "to the subset you should act on — ones where the recipient is "
            "RESTING/SLEEPING/STUNG and the message is still unread. Companion "
            "to ``queen_view_messages`` — that one is a raw log; this one is a "
            "triage feed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since_seconds": {
                    "type": "integer",
                    "description": "Only messages from the last N seconds. Default 900 (15m).",
                    "default": 900,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return. Default 50, max 500.",
                    "default": 50,
                },
                "actionable_only": {
                    "type": "boolean",
                    "description": (
                        "When true, filter to unread messages whose recipient is "
                        "currently RESTING / SLEEPING / STUNG — the subset where a "
                        "Queen nudge is most likely to unblock work. Default false."
                    ),
                    "default": False,
                },
                "full": {
                    "type": "boolean",
                    "description": (
                        "When true, return each message's complete body instead of "
                        "the 160-char preview. Same semantics as the ``full`` flag "
                        "on ``queen_view_messages``. Default false."
                    ),
                    "default": False,
                },
            },
            "examples": [
                {"since_seconds": 900, "actionable_only": True},
                {"since_seconds": 3600, "limit": 30},
                {"since_seconds": 900, "actionable_only": True, "full": True},
            ],
        },
    },
    {
        "name": "queen_view_buzz_log",
        "description": (
            "Read the buzz log — the system's activity feed: drone decisions, state "
            "transitions, operator actions, MCP calls. Filter by worker or category "
            "('drone'|'worker'|'operator'|'message') or age. Most useful for answering "
            "'what just happened?' after a notification fires."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {
                    "type": "string",
                    "description": "Filter to entries for this worker.",
                },
                "category": {
                    "type": "string",
                    "description": "Filter by category tag.",
                },
                "since_seconds": {
                    "type": "integer",
                    "description": "Only entries from the last N seconds. Default 600 (10m).",
                    "default": 600,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows. Default 50, max 500.",
                    "default": 50,
                },
            },
            "examples": [
                {"since_seconds": 300},
                {"worker": "platform", "category": "drone"},
            ],
        },
    },
    {
        "name": "queen_view_drone_actions",
        "description": (
            "Show recent drone (automated decision) actions — the fast-path auto-approvals, "
            "auto-assigns, auto-revives. Filter by worker or age. Use when deciding whether "
            "to intervene: if drones are already handling something routinely, don't "
            "duplicate their work."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {"type": "string", "description": "Filter by worker name."},
                "since_seconds": {
                    "type": "integer",
                    "description": "Only last N seconds. Default 600 (10m).",
                    "default": 600,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows. Default 50, max 200.",
                    "default": 50,
                },
            },
            "examples": [
                {"since_seconds": 600},
                {"worker": "hub", "limit": 30},
            ],
        },
    },
    {
        "name": "queen_post_thread",
        "description": (
            "Start a new chat thread with the operator. Use this when you proactively "
            "surface something (a stuck worker, a proposal ready to review, an anomaly "
            "you've spotted). Prefer this over dropping proactive info into the main "
            "operator thread — threads let the operator triage without losing focus. "
            "Return value includes the thread_id for follow-up via queen_reply."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": (
                        "Short title shown in the thread list. Aim for <60 chars, "
                        "action-oriented ('Hub stuck on tests'  not 'issue')."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": (
                        "Initial message body. Keep terse; include the specific "
                        "worker/task/file when relevant."
                    ),
                },
                "kind": {
                    "type": "string",
                    "description": (
                        "Category for filtering: 'oversight' (stuck/drift), 'proposal', "
                        "'escalation', 'anomaly', or 'operator' for operator-triggered "
                        "topics. Default 'oversight' when Queen initiates."
                    ),
                    "default": "oversight",
                },
                "worker": {
                    "type": "string",
                    "description": "Subject worker name, if this thread is about a specific one.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Subject task id, if applicable.",
                },
                "widgets": {
                    "type": "array",
                    "description": (
                        "Inline widget descriptors. Supported types: "
                        "'approve_buttons' (Approve/Dismiss/Discuss), "
                        "'worker_card' (live worker status), "
                        "'task_list' (live task references). "
                        "UI renders these; pass an empty array if plain text is enough."
                    ),
                },
            },
            "required": ["title", "body"],
            "examples": [
                {
                    "title": "Hub stuck on tests",
                    "body": (
                        "Hub has been BUZZING on task #42 for 18 minutes without token "
                        "growth. Plan: interrupt and ask for a status report?"
                    ),
                    "kind": "oversight",
                    "worker": "hub",
                    "widgets": [{"type": "approve_buttons"}],
                },
            ],
        },
    },
    {
        "name": "queen_reply",
        "description": (
            "Post a reply in an existing thread. Use this to respond to the operator "
            "or to update a thread with new information. The default operator thread "
            "is available as thread_id='operator' — writes there when no specific "
            "thread is in play."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {
                    "type": "string",
                    "description": (
                        "Target thread. Pass 'operator' for the default operator "
                        "thread (created lazily on first use)."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": "Message content.",
                },
                "widgets": {
                    "type": "array",
                    "description": (
                        "Optional inline widgets to render with this message. "
                        "Same shape as queen_post_thread.widgets."
                    ),
                },
            },
            "required": ["thread_id", "body"],
            "examples": [
                {"thread_id": "operator", "body": "Everyone's idle. Queue is clear."},
                {
                    "thread_id": "abc123def456",
                    "body": "Confirmed — hub's stuck on the same test. Suggest interrupt.",
                },
            ],
        },
    },
    {
        "name": "queen_update_thread",
        "description": (
            "Resolve a thread or change its status when an outcome is reached. Call "
            "this when the discussion's conclusion is final so the thread collapses "
            "in the UI and the operator sees it as done. Resolved threads have their "
            "composer disabled; start a new thread to reopen the topic."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {
                    "type": "string",
                    "description": "Target thread id.",
                },
                "status": {
                    "type": "string",
                    "description": (
                        "New status. Currently only 'resolved' is supported "
                        "(the Queen can self-resolve a thread she created)."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Short resolution reason shown in the collapsed summary "
                        "('operator approved', 'worker recovered')."
                    ),
                },
            },
            "required": ["thread_id", "status"],
            "examples": [
                {"thread_id": "abc123", "status": "resolved", "reason": "operator approved"},
            ],
        },
    },
    {
        "name": "queen_save_learning",
        "description": (
            "Persist a correction after the operator tells you you got something "
            "wrong. Future judgement calls will consult this via queen_query_learnings. "
            "Call this immediately after an operator correction, not speculatively."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "context": {
                    "type": "string",
                    "description": "What you decided / assumed (1-2 sentences).",
                },
                "correction": {
                    "type": "string",
                    "description": "What the operator said was actually right.",
                },
                "applied_to": {
                    "type": "string",
                    "description": (
                        "Decision category tag for later filtering "
                        "('oversight'|'proposal'|'assignment'|'escalation'|…)."
                    ),
                },
                "thread_id": {
                    "type": "string",
                    "description": "Originating thread id, if known.",
                },
            },
            "required": ["context", "correction"],
            "examples": [
                {
                    "context": "Flagged hub as stuck — assumed test flakiness.",
                    "correction": "Hub was actually mid-rebase; not stuck.",
                    "applied_to": "oversight",
                },
            ],
        },
    },
    {
        "name": "queen_query_learnings",
        "description": (
            "Query the Queen-learnings store — corrections the operator has given on past "
            "decisions. Call this BEFORE making a similar judgment call ('should I auto-"
            "approve this plan?', 'is this worker normally stuck?') so you don't re-make a "
            "mistake the operator already corrected."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "applied_to": {
                    "type": "string",
                    "description": (
                        "Filter by decision type tag (e.g. 'oversight', 'proposal'). "
                        "Empty returns all."
                    ),
                },
                "search": {
                    "type": "string",
                    "description": "Substring match against context or correction text.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows. Default 20, max 100.",
                    "default": 20,
                },
            },
            "examples": [
                {"applied_to": "oversight"},
                {"search": "auth"},
            ],
        },
    },
    {
        "name": "queen_reassign_task",
        "description": (
            "Move an assigned or in-progress task from one worker to another.  Use "
            "when you've determined the original assignee can't reach the work "
            "(blocked, wrong expertise, over-loaded) and a peer is better-positioned. "
            "Call queen_view_worker_state on both workers first so you're acting on "
            "current reality, not a stale assumption.  If `start` is true, the new "
            "worker is immediately sent the task message; otherwise the task sits "
            "ASSIGNED for the next poll cycle."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "number": {
                    "type": "integer",
                    "description": (
                        "Task number (from queen_view_task_board).  Preferred over "
                        "task_id because operator-readable logs show this."
                    ),
                },
                "task_id": {
                    "type": "string",
                    "description": "Internal task id.  Use if you only have the id.",
                },
                "to_worker": {
                    "type": "string",
                    "description": "Name of the worker that should receive the task.",
                },
                "start": {
                    "type": "boolean",
                    "description": (
                        "When true, dispatch the task to the new worker's PTY "
                        "immediately.  Default false (task sits ASSIGNED)."
                    ),
                    "default": False,
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Short reason shown in the buzz log and task history.  "
                        "Required — the operator audits reassignments."
                    ),
                },
            },
            "required": ["to_worker", "reason"],
            "examples": [
                {"number": 42, "to_worker": "platform", "reason": "hub over-loaded", "start": True},
            ],
        },
    },
    {
        "name": "queen_interrupt_worker",
        "description": (
            "Send Ctrl-C to a worker's PTY to interrupt its current turn. "
            "DESTRUCTIVE: cancels in-flight tool use and loses any uncommitted "
            "work.  Use only when the worker is genuinely stuck (queen_view_worker_state "
            "shows long BUZZING with flat token growth) or going the wrong direction "
            "and you've confirmed via the buzz log.  Always provide a reason — it "
            "lands in the buzz log as an OPERATOR entry."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {
                    "type": "string",
                    "description": "Name of the worker to interrupt.",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Why you're interrupting.  Required — surfaces in buzz log "
                        "so the operator can audit."
                    ),
                },
            },
            "required": ["worker", "reason"],
            "examples": [
                {"worker": "hub", "reason": "BUZZING 20m, 3 low-delta ticks, likely stuck"},
            ],
        },
    },
    {
        "name": "queen_force_complete_task",
        "description": (
            "Mark a task COMPLETED even though the assigned worker didn't call "
            "swarm_complete_task.  DESTRUCTIVE: bypasses the worker's own signal, "
            "freeing them to pick up new work and removing the task from the open "
            "board.  Use when the worker is demonstrably done but silent — e.g. "
            "they went RESTING after shipping and their PTY shows the outcome but "
            "they never issued the completion call.  Always include a resolution "
            "summary noting what the worker actually did (so task_history has it)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "number": {
                    "type": "integer",
                    "description": "Task number.  Preferred.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task id.  Use if only the id is known.",
                },
                "resolution": {
                    "type": "string",
                    "description": (
                        "Summary of what was actually accomplished.  Shown in "
                        "task history and downstream reports — be specific."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Short reason for forcing completion.  Required — the "
                        "operator audits force-completions."
                    ),
                },
            },
            "required": ["resolution", "reason"],
            "examples": [
                {
                    "number": 42,
                    "resolution": "Fixed auth middleware; verified via grep + running tests.",
                    "reason": "worker went RESTING after shipping — forgot completion call",
                },
            ],
        },
    },
    {
        "name": "queen_prompt_worker",
        "description": (
            "Push a prompt directly into a worker's PTY — the worker sees it "
            "exactly as if the operator had typed it in the dashboard chat.  "
            "Use this when you want a worker to DO something now (take a task, "
            "answer a question, run a check), not just when you want them to "
            "know something (use queen_send_message for the inbox channel).  "
            "Safe to call on BUZZING workers: Claude Code queues the text and "
            "injects it as a new user turn after the current one completes — "
            "no interruption, no lost work.  Refuses only when the target is "
            "the Queen herself or the worker is STUNG (dead process).  "
            "Always include a reason; it lands in the buzz log as an "
            "OPERATOR entry for audit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {
                    "type": "string",
                    "description": "Name of the worker to prompt.",
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "Text to inject into the worker's PTY.  Enter is sent "
                        "automatically after the text (same as operator typing)."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Why you're prompting this worker now.  Required — "
                        "shows up in the buzz log so the operator can audit."
                    ),
                },
            },
            "required": ["worker", "prompt", "reason"],
            "examples": [
                {
                    "worker": "hub",
                    "prompt": "Please run /check and paste the output.",
                    "reason": "verifying pre-commit hooks before asking for a PR",
                },
                {
                    "worker": "platform",
                    "prompt": "Pause current work — rate limit warning.",
                    "reason": "5hr window at 88%",
                },
            ],
        },
    },
]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _clamp(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, n))


def _handle_view_worker_state(
    d: SwarmDaemon, worker_name: str, args: QueenViewWorkerStateArgs
) -> list[dict[str, Any]] | dict[str, Any]:
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


_OPEN_STATUSES = {"backlog", "unassigned", "assigned", "active"}
_DONE_STATUSES = {"done"}


def _handle_view_task_board(
    d: SwarmDaemon, worker_name: str, args: QueenViewTaskBoardArgs
) -> list[dict[str, Any]] | dict[str, Any]:
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


def _handle_view_messages(
    d: SwarmDaemon, worker_name: str, args: QueenViewMessagesArgs
) -> list[dict[str, Any]] | dict[str, Any]:
    err = _assert_queen(worker_name)
    if err:
        return err
    worker_filter = (args.get("worker") or "").strip()
    since = _clamp(args.get("since_seconds", 3600), 3600, 1, 30 * 86400)
    limit = _clamp(args.get("limit", 50), 50, 1, 500)
    # Task #237: ``full=true`` returns the complete message body
    # instead of the 160-char preview. The auto-relay path from #235
    # tells the Queen to call this tool to read the full message she
    # was just notified about — but the default 160-char truncation
    # left her unable to relay verbatim content. Default stays
    # truncated so list-view ergonomics don't change.
    full = bool(args.get("full", False))

    since_ts = time.time() - since
    sql_parts = ["SELECT * FROM messages WHERE created_at >= ?"]
    params: list[Any] = [since_ts]
    if worker_filter:
        sql_parts.append("AND (sender = ? OR recipient = ?)")
        params.extend([worker_filter, worker_filter])
    sql_parts.append("ORDER BY created_at DESC LIMIT ?")
    params.append(limit)
    rows = d.swarm_db.fetchall(" ".join(sql_parts), tuple(params))
    if not rows:
        return [{"type": "text", "text": "No messages match."}]
    lines: list[str] = []
    payload: list[dict[str, Any]] = []
    for r in rows:
        content = r["content"] or ""
        body = content if full else content[:160]
        header = f"[{r['msg_type']}] {r['sender']} → {r['recipient']}"
        if full:
            # Multi-message / multi-line bodies: separate with a blank
            # line so the Queen can identify message boundaries when
            # relaying verbatim.
            lines.append(f"{header}:\n{body}")
        else:
            lines.append(f"{header}: {body}")
        payload.append(
            {
                "id": r["id"],
                "msg_type": r["msg_type"],
                "sender": r["sender"],
                "recipient": r["recipient"],
                # Always carry the FULL body in the structured payload —
                # the truncation is purely a text-rendering concern, the
                # JSON sidecar is for the model to query precisely.
                "content": content,
                "created_at": r["created_at"],
                "read_at": r["read_at"],
            }
        )
    separator = "\n\n---\n\n" if full else "\n"
    return {
        "content": [{"type": "text", "text": separator.join(lines)}],
        "structuredContent": {
            "messages": payload,
            "count": len(payload),
            "filters": {
                "worker": worker_filter or None,
                "since_seconds": since,
                "limit": limit,
                "full_body": full,
            },
        },
    }


_IDLE_RECIPIENT_STATES = ("RESTING", "SLEEPING", "STUNG")


def _handle_view_message_stream(
    d: SwarmDaemon, worker_name: str, args: QueenViewMessageStreamArgs
) -> list[dict[str, Any]] | dict[str, Any]:
    """Return recent messages joined against the recipient's current state.

    ``actionable_only=true`` narrows to the subset the Queen is most
    likely to need to act on: unread messages whose recipient is
    currently idle (RESTING / SLEEPING / STUNG). That's the shape the
    InterWorkerMessageWatcher drone uses when deciding who to nudge.
    """
    err = _assert_queen(worker_name)
    if err:
        return err
    since = _clamp(args.get("since_seconds", 900), 900, 1, 30 * 86400)
    limit = _clamp(args.get("limit", 50), 50, 1, 500)
    actionable_only = bool(args.get("actionable_only", False))
    # Task #237: mirror ``queen_view_messages``' full-body flag so the
    # stream view can also return complete message content when the
    # Queen needs to relay verbatim.
    full = bool(args.get("full", False))

    since_ts = time.time() - since
    rows = d.swarm_db.fetchall(
        "SELECT * FROM messages WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
        (since_ts, limit * 4 if actionable_only else limit),
    )
    if not rows:
        return [{"type": "text", "text": "No messages in window."}]

    worker_state = _message_stream_worker_states(d)
    lines = _render_message_stream_rows(
        rows,
        worker_state=worker_state,
        actionable_only=actionable_only,
        limit=limit,
        full=full,
    )
    if not lines:
        if actionable_only:
            return [{"type": "text", "text": "No actionable messages."}]
        return [{"type": "text", "text": "No messages in window."}]
    structured_rows = _structured_message_stream_rows(
        rows,
        worker_state=worker_state,
        actionable_only=actionable_only,
        limit=limit,
    )
    separator = "\n\n---\n\n" if full else "\n"
    return {
        "content": [{"type": "text", "text": separator.join(lines)}],
        "structuredContent": {
            "messages": structured_rows,
            "count": len(structured_rows),
            "filters": {
                "since_seconds": since,
                "limit": limit,
                "actionable_only": actionable_only,
                "full_body": full,
            },
        },
    }


def _message_stream_worker_states(d: SwarmDaemon) -> dict[str, str]:
    """Map worker-name → display_state string for the in-memory workers."""
    out: dict[str, str] = {}
    for w in getattr(d, "workers", []) or []:
        state = getattr(w, "display_state", None) or getattr(w, "state", None)
        if state is not None and hasattr(state, "value"):
            out[w.name] = state.value
        elif state is not None:
            out[w.name] = str(state)
    return out


def _render_message_stream_rows(
    rows: list[Any],
    *,
    worker_state: dict[str, str],
    actionable_only: bool,
    limit: int,
    full: bool,
) -> list[str]:
    """Format message-stream rows into display lines.

    Extracted from ``_handle_view_message_stream`` to keep the handler's
    complexity under the lint cap (task #237 added the ``full`` branch
    and pushed it over).
    """
    lines: list[str] = []
    for r in rows:
        recipient = r["recipient"]
        recipient_state = worker_state.get(recipient, "UNKNOWN")
        has_read = r["read_at"] is not None
        if actionable_only:
            if has_read or recipient_state not in _IDLE_RECIPIENT_STATES:
                continue
            if len(lines) >= limit:
                break
        flag = "READ" if has_read else "UNREAD"
        content = r["content"] or ""
        body = content if full else content[:160]
        header = f"[{r['msg_type']}] {r['sender']} → {recipient} ({recipient_state}, {flag})"
        lines.append(f"{header}:\n{body}" if full else f"{header}: {body}")
    return lines


def _structured_message_stream_rows(
    rows: list[Any],
    *,
    worker_state: dict[str, str],
    actionable_only: bool,
    limit: int,
) -> list[dict[str, Any]]:
    """Companion to ``_render_message_stream_rows`` returning structured payload.

    Same filter logic; emits one dict per visible row with the full
    message body (truncation is text-only) and the recipient's state
    joined in. Kept separate from the rendering helper so the text and
    structured shapes can evolve independently if needed.
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        recipient = r["recipient"]
        recipient_state = worker_state.get(recipient, "UNKNOWN")
        has_read = r["read_at"] is not None
        if actionable_only:
            if has_read or recipient_state not in _IDLE_RECIPIENT_STATES:
                continue
            if len(out) >= limit:
                break
        out.append(
            {
                "id": r["id"],
                "msg_type": r["msg_type"],
                "sender": r["sender"],
                "recipient": recipient,
                "recipient_state": recipient_state,
                "read": has_read,
                "content": r["content"] or "",
                "created_at": r["created_at"],
                "read_at": r["read_at"],
            }
        )
    return out


def _handle_view_buzz_log(
    d: SwarmDaemon, worker_name: str, args: QueenViewBuzzLogArgs
) -> list[dict[str, Any]] | dict[str, Any]:
    err = _assert_queen(worker_name)
    if err:
        return err
    worker_filter = (args.get("worker") or "").strip()
    category_filter = (args.get("category") or "").strip()
    since = _clamp(args.get("since_seconds", 600), 600, 1, 30 * 86400)
    limit = _clamp(args.get("limit", 50), 50, 1, 500)

    since_ts = time.time() - since
    sql_parts = ["SELECT * FROM buzz_log WHERE timestamp >= ?"]
    params: list[Any] = [since_ts]
    if worker_filter:
        sql_parts.append("AND worker_name = ?")
        params.append(worker_filter)
    if category_filter:
        sql_parts.append("AND category = ?")
        params.append(category_filter)
    sql_parts.append("ORDER BY timestamp DESC LIMIT ?")
    params.append(limit)
    rows = d.swarm_db.fetchall(" ".join(sql_parts), tuple(params))
    if not rows:
        return [{"type": "text", "text": "No buzz entries match."}]
    lines = [
        f"[{r['category']}] {r['worker_name'] or '-'}: {r['action']} — {(r['detail'] or '')[:120]}"
        for r in rows
    ]
    payload = [
        {
            "id": r["id"],
            "timestamp": r["timestamp"],
            "category": r["category"],
            "worker_name": r["worker_name"] or None,
            "action": r["action"],
            "detail": r["detail"] or "",
        }
        for r in rows
    ]
    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "structuredContent": {
            "entries": payload,
            "count": len(payload),
            "filters": {
                "worker": worker_filter or None,
                "category": category_filter or None,
                "since_seconds": since,
                "limit": limit,
            },
        },
    }


def _handle_view_drone_actions(
    d: SwarmDaemon, worker_name: str, args: QueenViewDroneActionsArgs
) -> list[dict[str, Any]] | dict[str, Any]:
    err = _assert_queen(worker_name)
    if err:
        return err
    worker_filter = (args.get("worker") or "").strip()
    since = _clamp(args.get("since_seconds", 600), 600, 1, 30 * 86400)
    limit = _clamp(args.get("limit", 50), 50, 1, 200)

    since_ts = time.time() - since
    sql_parts = ["SELECT * FROM buzz_log WHERE category = 'drone' AND timestamp >= ?"]
    params: list[Any] = [since_ts]
    if worker_filter:
        sql_parts.append("AND worker_name = ?")
        params.append(worker_filter)
    sql_parts.append("ORDER BY timestamp DESC LIMIT ?")
    params.append(limit)
    rows = d.swarm_db.fetchall(" ".join(sql_parts), tuple(params))
    if not rows:
        return [{"type": "text", "text": "No recent drone actions."}]
    lines = [
        f"{r['worker_name'] or '-'}: {r['action']} — {(r['detail'] or '')[:120]}" for r in rows
    ]
    payload = [
        {
            "id": r["id"],
            "timestamp": r["timestamp"],
            "worker_name": r["worker_name"] or None,
            "action": r["action"],
            "detail": r["detail"] or "",
        }
        for r in rows
    ]
    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "structuredContent": {
            "actions": payload,
            "count": len(payload),
            "filters": {
                "worker": worker_filter or None,
                "since_seconds": since,
                "limit": limit,
            },
        },
    }


def _handle_query_learnings(
    d: SwarmDaemon, worker_name: str, args: QueenQueryLearningsArgs
) -> list[dict[str, Any]]:
    err = _assert_queen(worker_name)
    if err:
        return err
    applied_to = (args.get("applied_to") or "").strip() or None
    search = (args.get("search") or "").strip() or None
    limit = _clamp(args.get("limit", 20), 20, 1, 100)

    store = getattr(d, "queen_chat", None)
    if store is None:
        return [{"type": "text", "text": "Queen chat store is unavailable."}]
    learnings = store.query_learnings(applied_to=applied_to, search=search, limit=limit)
    if not learnings:
        return [{"type": "text", "text": "No learnings match."}]
    lines = [
        f"[{lg.applied_to or 'general'}] {lg.context[:80]} → {lg.correction[:120]}"
        for lg in learnings
    ]
    return [{"type": "text", "text": "\n".join(lines)}]


# ---------------------------------------------------------------------------
# Conversation tools — Queen posts, replies, resolves threads, saves learnings
# ---------------------------------------------------------------------------


_DEFAULT_OPERATOR_THREAD_ALIAS = "operator"
_DEFAULT_OPERATOR_THREAD_TITLE = "Operator chat"


def _ensure_operator_thread(d: SwarmDaemon) -> str:
    """Return the id of the default operator thread, creating it if needed.

    The Queen references this thread via the alias ``"operator"`` so she
    doesn't need to remember a uuid between sessions.  The alias maps to
    the single most-recent active ``kind='operator'`` thread; if none
    exists we create one.
    """
    store = d.queen_chat
    active = store.list_threads(kind="operator", status="active", limit=1)
    if active:
        return active[0].id
    thread = store.create_thread(
        title=_DEFAULT_OPERATOR_THREAD_TITLE,
        kind="operator",
    )
    _broadcast_thread_event(d, thread.id, "created")
    return thread.id


def _broadcast_thread_event(d: SwarmDaemon, thread_id: str, event: str) -> None:
    """Push a ``queen.thread`` WS event for the chat panel to react to.

    ``event`` is one of ``created|updated|resolved``. Safe to swallow
    failures — the UI polls on reconnect.
    """
    store = getattr(d, "queen_chat", None)
    if store is None:
        return
    try:
        thread = store.get_thread(thread_id)
        if thread is None:
            return
        d.broadcast_ws({"type": "queen.thread", "event": event, "thread": thread.to_dict()})
    except Exception:
        pass


def _broadcast_message_event(d: SwarmDaemon, thread_id: str, message_dict: dict[str, Any]) -> None:
    """Push a ``queen.message`` WS event for a newly-added message."""
    try:
        d.broadcast_ws({"type": "queen.message", "thread_id": thread_id, "message": message_dict})
    except Exception:
        pass


def _resolve_thread_alias(d: SwarmDaemon, raw: str) -> str | None:
    """Translate a thread_id or alias to a real thread id.

    Returns ``None`` when the target can't be resolved.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw == _DEFAULT_OPERATOR_THREAD_ALIAS:
        return _ensure_operator_thread(d)
    # Real id — just confirm it exists.
    if d.queen_chat.get_thread(raw) is None:
        return None
    return raw


def _handle_post_thread(
    d: SwarmDaemon, worker_name: str, args: QueenPostThreadArgs
) -> list[dict[str, Any]]:
    err = _assert_queen(worker_name)
    if err:
        return err
    title = (args.get("title") or "").strip()
    body = (args.get("body") or "").strip()
    if not title or not body:
        return [{"type": "text", "text": "Missing required 'title' or 'body'."}]
    kind = (args.get("kind") or "oversight").strip().lower()
    worker = (args.get("worker") or "").strip() or None
    task_id = (args.get("task_id") or "").strip() or None
    widgets = args.get("widgets") or []

    store = d.queen_chat
    try:
        thread = store.create_thread(
            title=title,
            kind=kind,
            worker_name=worker,
            task_id=task_id,
        )
    except ValueError as e:
        return [{"type": "text", "text": f"Invalid thread kind: {e}"}]

    message = store.add_message(
        thread.id,
        role="queen",
        content=body,
        widgets=widgets if isinstance(widgets, list) else [],
    )
    _broadcast_thread_event(d, thread.id, "created")
    _broadcast_message_event(d, thread.id, message.to_dict())
    return [
        {
            "type": "text",
            "text": f"Thread posted: id={thread.id} title={title!r}",
        }
    ]


def _handle_reply(d: SwarmDaemon, worker_name: str, args: QueenReplyArgs) -> list[dict[str, Any]]:
    err = _assert_queen(worker_name)
    if err:
        return err
    thread_id = _resolve_thread_alias(d, args.get("thread_id", ""))
    if thread_id is None:
        return [{"type": "text", "text": "Unknown thread_id."}]
    body = (args.get("body") or "").strip()
    if not body:
        return [{"type": "text", "text": "Missing 'body'."}]
    widgets = args.get("widgets") or []

    thread = d.queen_chat.get_thread(thread_id)
    if thread is None:
        return [{"type": "text", "text": "Thread not found."}]
    if thread.status == "resolved":
        return [
            {
                "type": "text",
                "text": "Thread is resolved. Start a new thread to continue the topic.",
            }
        ]
    message = d.queen_chat.add_message(
        thread_id,
        role="queen",
        content=body,
        widgets=widgets if isinstance(widgets, list) else [],
    )
    _broadcast_message_event(d, thread_id, message.to_dict())
    _broadcast_thread_event(d, thread_id, "updated")
    return [{"type": "text", "text": f"Reply posted to {thread_id}."}]


def _handle_update_thread(
    d: SwarmDaemon, worker_name: str, args: QueenUpdateThreadArgs
) -> list[dict[str, Any]]:
    err = _assert_queen(worker_name)
    if err:
        return err
    thread_id = _resolve_thread_alias(d, args.get("thread_id", ""))
    if thread_id is None:
        return [{"type": "text", "text": "Unknown thread_id."}]
    status = (args.get("status") or "").strip().lower()
    if status != "resolved":
        return [{"type": "text", "text": "Only status='resolved' is supported."}]
    reason = (args.get("reason") or "").strip()
    ok = d.queen_chat.resolve_thread(thread_id, resolved_by="queen", reason=reason)
    if not ok:
        return [
            {
                "type": "text",
                "text": "Thread was already resolved or does not exist.",
            }
        ]
    _broadcast_thread_event(d, thread_id, "resolved")
    return [{"type": "text", "text": f"Thread {thread_id} resolved."}]


def _handle_save_learning(
    d: SwarmDaemon, worker_name: str, args: QueenSaveLearningArgs
) -> list[dict[str, Any]]:
    err = _assert_queen(worker_name)
    if err:
        return err
    context = (args.get("context") or "").strip()
    correction = (args.get("correction") or "").strip()
    if not context or not correction:
        return [{"type": "text", "text": "Missing required 'context' or 'correction'."}]
    applied_to = (args.get("applied_to") or "").strip()
    thread_id = (args.get("thread_id") or "").strip() or None
    if thread_id:
        # Resolve alias — operator thread id may be passed as 'operator'
        resolved = _resolve_thread_alias(d, thread_id)
        if resolved is None:
            thread_id = None
        else:
            thread_id = resolved
    learning = d.queen_chat.add_learning(
        context=context,
        correction=correction,
        applied_to=applied_to,
        thread_id=thread_id,
    )
    return [{"type": "text", "text": f"Learning saved (id={learning.id})."}]


# ---------------------------------------------------------------------------
# Write-side action tools — reassign, interrupt, force-complete
#
# Destructive-action note: the spec calls for an inline operator
# confirmation UI before any of these fire (§4.2).  That UI ships with
# the chat-panel sub-pass.  Until then these execute immediately;
# every call logs to the OPERATOR category in the buzz log so the
# operator can audit, and each handler requires a free-text `reason`
# so the intent is captured at the call site.
# ---------------------------------------------------------------------------


def _resolve_task(d: SwarmDaemon, args: dict[str, Any]) -> Any | list[dict[str, Any]]:
    """Look up a task by ``number`` or ``task_id``. Return the task or an error payload."""
    number = args.get("number")
    task_id = (args.get("task_id") or "").strip() or None
    if number is None and not task_id:
        return [{"type": "text", "text": "Missing 'number' or 'task_id'."}]
    if d.task_board is None:
        return [{"type": "text", "text": "Task board is unavailable."}]
    if number is not None:
        try:
            target = int(number)
        except (TypeError, ValueError):
            return [{"type": "text", "text": f"Invalid 'number': {number!r}"}]
        for t in d.task_board.all_tasks:
            if t.number == target:
                return t
        return [{"type": "text", "text": f"No task with number #{target}."}]
    task = d.task_board.get(task_id)
    if task is None:
        return [{"type": "text", "text": f"No task with id {task_id!r}."}]
    return task


def _fire_async(coro: Any) -> None:
    """Fire an async daemon method from a sync MCP handler context.

    Falls back to silently dropping the call if no event loop is
    available (should only happen in unit tests that mock the daemon).
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    except RuntimeError:
        try:
            coro.close()
        except Exception:
            pass


def _handle_reassign_task(
    d: SwarmDaemon, worker_name: str, args: QueenReassignTaskArgs
) -> list[dict[str, Any]]:
    err = _assert_queen(worker_name)
    if err:
        return err
    to_worker = (args.get("to_worker") or "").strip()
    reason = (args.get("reason") or "").strip()
    if not to_worker:
        return [{"type": "text", "text": "Missing 'to_worker'."}]
    if not reason:
        return [{"type": "text", "text": "Missing 'reason' — reassignments must be audited."}]
    target = _resolve_task(d, args)
    if isinstance(target, list):
        return target
    task = target
    start = bool(args.get("start", False))
    prev = task.assigned_worker or "unassigned"

    if prev == to_worker:
        return [{"type": "text", "text": f"Task #{task.number} already assigned to {to_worker}."}]

    # Unassign first so assign() accepts (it checks is_available).
    if task.assigned_worker:
        d.task_board.unassign(task.id)
    if not d.task_board.assign(task.id, to_worker):
        return [
            {
                "type": "text",
                "text": f"Failed to assign #{task.number} to {to_worker} (not available).",
            }
        ]
    from swarm.drones.log import LogCategory, SystemAction

    d.drone_log.add(
        SystemAction.OPERATOR,
        to_worker,
        f"queen reassigned #{task.number} from {prev}: {reason[:120]}",
        category=LogCategory.OPERATOR,
    )
    if start:
        _fire_async(d.assign_and_start_task(task.id, to_worker, actor="queen"))
        return [
            {
                "type": "text",
                "text": (f"Reassigned #{task.number} from {prev} → {to_worker} and dispatched."),
            }
        ]
    return [
        {
            "type": "text",
            "text": f"Reassigned #{task.number} from {prev} → {to_worker} (ASSIGNED, not started).",
        }
    ]


def _handle_interrupt_worker(
    d: SwarmDaemon, worker_name: str, args: QueenInterruptWorkerArgs
) -> list[dict[str, Any]]:
    err = _assert_queen(worker_name)
    if err:
        return err
    target = (args.get("worker") or "").strip()
    reason = (args.get("reason") or "").strip()
    if not target:
        return [{"type": "text", "text": "Missing 'worker'."}]
    if not reason:
        return [{"type": "text", "text": "Missing 'reason' — interrupts must be audited."}]
    if target == QUEEN_WORKER_NAME:
        return [{"type": "text", "text": "Refusing to interrupt the Queen herself."}]
    if not any(w.name == target for w in d.workers):
        return [{"type": "text", "text": f"Worker '{target}' not found."}]
    from swarm.drones.log import LogCategory, SystemAction

    d.drone_log.add(
        SystemAction.OPERATOR,
        target,
        f"queen interrupted (Ctrl-C): {reason[:120]}",
        category=LogCategory.OPERATOR,
    )
    worker_svc = getattr(d, "worker_svc", None)
    if worker_svc is None:
        return [{"type": "text", "text": "Worker service unavailable."}]
    _fire_async(worker_svc.interrupt_worker(target))
    return [{"type": "text", "text": f"Interrupt sent to {target}."}]


def _handle_force_complete_task(
    d: SwarmDaemon, worker_name: str, args: QueenForceCompleteTaskArgs
) -> list[dict[str, Any]]:
    err = _assert_queen(worker_name)
    if err:
        return err
    resolution = (args.get("resolution") or "").strip()
    reason = (args.get("reason") or "").strip()
    if not resolution:
        return [{"type": "text", "text": "Missing 'resolution'."}]
    if not reason:
        return [
            {
                "type": "text",
                "text": "Missing 'reason' — force-completions must be audited.",
            }
        ]
    target = _resolve_task(d, args)
    if isinstance(target, list):
        return target
    task = target
    prev_worker = task.assigned_worker or "unassigned"

    # d.complete_task handles board + history + drone_log + downstream
    # triggers.  Passing actor='queen' lets the audit trail distinguish
    # her calls from operator button clicks.
    ok = d.complete_task(task.id, actor="queen", resolution=resolution, verify=False)
    if not ok:
        return [
            {
                "type": "text",
                "text": (
                    f"Failed to complete #{task.number} "
                    f"(status was {task.status.value if task.status else '?'})."
                ),
            }
        ]
    from swarm.drones.log import LogCategory, SystemAction

    d.drone_log.add(
        SystemAction.OPERATOR,
        prev_worker,
        f"queen force-completed #{task.number}: {reason[:120]}",
        category=LogCategory.OPERATOR,
    )
    return [
        {
            "type": "text",
            "text": f"Force-completed #{task.number} (was on {prev_worker}).",
        }
    ]


def _handle_prompt_worker(
    d: SwarmDaemon, worker_name: str, args: QueenPromptWorkerArgs
) -> list[dict[str, Any]]:
    """Push a prompt into a worker's PTY — Queen-initiated direct chat.

    Claude Code queues PTY input while a turn is in progress, so sending
    to a BUZZING worker does NOT interrupt current work — it lands as a
    new user turn after the current one completes.  Hard refusals:
    self-target (Queen prompting herself) and STUNG (dead process).
    """
    err = _assert_queen(worker_name)
    if err:
        return err
    target = (args.get("worker") or "").strip()
    prompt = args.get("prompt") or ""
    reason = (args.get("reason") or "").strip()
    if not target:
        return [{"type": "text", "text": "Missing 'worker'."}]
    if not prompt:
        return [{"type": "text", "text": "Missing 'prompt'."}]
    if not reason:
        return [{"type": "text", "text": "Missing 'reason' — prompts must be audited."}]
    if target == QUEEN_WORKER_NAME:
        return [{"type": "text", "text": "Refusing to prompt the Queen herself."}]
    worker = next((w for w in d.workers if w.name == target), None)
    if worker is None:
        return [{"type": "text", "text": f"Worker '{target}' not found."}]

    from swarm.worker.worker import WorkerState

    if worker.state == WorkerState.STUNG:
        return [{"type": "text", "text": f"Worker '{target}' is STUNG — revive before prompting."}]
    from swarm.drones.log import LogCategory, SystemAction

    # Note in the buzz log whether the prompt will queue (worker mid-turn)
    # or land on an idle worker — auditing benefits from that distinction.
    will_queue = worker.state == WorkerState.BUZZING
    queue_tag = " [queued, worker BUZZING]" if will_queue else ""
    d.drone_log.add(
        SystemAction.OPERATOR,
        target,
        f"queen prompt{queue_tag} ({reason[:80]}): {prompt[:100]}",
        category=LogCategory.OPERATOR,
    )
    worker_svc = getattr(d, "worker_svc", None)
    if worker_svc is None:
        return [{"type": "text", "text": "Worker service unavailable."}]
    _fire_async(worker_svc.send_to_worker(target, prompt, _log_operator=False))
    suffix = " — queued for next turn" if will_queue else ""
    return [{"type": "text", "text": f"Prompt sent to {target}{suffix}."}]


QUEEN_HANDLERS: dict[str, Any] = {
    "queen_view_worker_state": _handle_view_worker_state,
    "queen_view_task_board": _handle_view_task_board,
    "queen_view_messages": _handle_view_messages,
    "queen_view_message_stream": _handle_view_message_stream,
    "queen_view_buzz_log": _handle_view_buzz_log,
    "queen_view_drone_actions": _handle_view_drone_actions,
    "queen_query_learnings": _handle_query_learnings,
    "queen_post_thread": _handle_post_thread,
    "queen_reply": _handle_reply,
    "queen_update_thread": _handle_update_thread,
    "queen_save_learning": _handle_save_learning,
    "queen_reassign_task": _handle_reassign_task,
    "queen_interrupt_worker": _handle_interrupt_worker,
    "queen_force_complete_task": _handle_force_complete_task,
    "queen_prompt_worker": _handle_prompt_worker,
}
