"""Hook routes — Claude Code hook callbacks for approval, session lifecycle, and events."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from aiohttp import web

from swarm.drones.log import DroneAction, LogCategory, SystemAction
from swarm.drones.rules import ALWAYS_ESCALATE
from swarm.logging import get_logger
from swarm.server.helpers import get_daemon, handle_errors, json_error

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon
    from swarm.worker.worker import Worker

_log = get_logger("server.hooks")

# Safe tools that are always auto-approved — no need to query rules.
_ALWAYS_APPROVE_TOOLS = frozenset({"Read", "Glob", "Grep", "WebSearch", "WebFetch"})

# Swarm's own MCP tools are always safe to approve — they're the coordination
# primitives the daemon itself exposes to workers (swarm_check_messages,
# swarm_complete_task, etc.). Gating them behind operator approval means the
# worker can stall indefinitely on a prompt that's definitionally safe.
_SWARM_MCP_PREFIX = "mcp__swarm__"

# Tools that always need operator approval via the drone rules engine.
_ALWAYS_ESCALATE_TOOLS = frozenset({"Bash"})


def register(app: web.Application) -> None:
    app.router.add_post("/api/hooks/approval", handle_approval)
    app.router.add_post("/api/hooks/session-end", handle_session_end)
    app.router.add_post("/api/hooks/session-start", handle_session_start)
    app.router.add_post("/api/hooks/event", handle_event)


@handle_errors
async def handle_approval(request: web.Request) -> web.Response:
    """PreToolUse hook endpoint — evaluate tool use against drone approval rules.

    Receives Claude Code's PreToolUse hook input:
    ``{"tool_name": "Bash", "tool_input": {...}, "session_id": "...", ...}``

    Returns ``{"decision": "approve"|"block"|"passthrough", "reason": "..."}``
    """
    d = get_daemon(request)

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return json_error("invalid JSON body", status=400)

    tool_name = body.get("tool_name", "")
    tool_input = body.get("tool_input", {})

    if not tool_name:
        return json_error("missing tool_name", status=400)

    # Track tool activity on the worker (Phase 2: agent progress)
    worker = _identify_worker(d, body)
    if worker is not None:
        _record_tool_activity(worker, tool_name, tool_input)

    # File conflict prevention: block Edit/Write if another worker holds the lock
    conflict = _check_file_lock(d, worker, tool_name, tool_input)
    if conflict is not None:
        return conflict

    # Fast path: always-approve safe read-only tools
    if tool_name in _ALWAYS_APPROVE_TOOLS:
        return web.json_response({"decision": "approve", "reason": "safe read-only tool"})

    # Fast path: Swarm's own MCP tools never require operator approval.
    if tool_name.startswith(_SWARM_MCP_PREFIX):
        _log_hook_decision(d, tool_name, "approve", "swarm MCP tool")
        return web.json_response({"decision": "approve", "reason": "swarm MCP tool"})

    # Build a text representation of the tool call for rules matching.
    # This mirrors what the drone sees in terminal output.
    tool_text = _build_tool_text(tool_name, tool_input)

    # Safety net: escalate destructive patterns to operator (never auto-approve)
    if ALWAYS_ESCALATE.search(tool_text):
        _log_hook_decision(d, tool_name, "escalate", "destructive pattern detected")
        return web.json_response({"decision": "passthrough"})

    return _evaluate_rules(d, body, tool_name, tool_text)


@handle_errors
async def handle_session_end(request: web.Request) -> web.Response:
    """SessionEnd hook endpoint — notify daemon that a Claude session ended.

    This enables immediate STUNG detection without relying on /proc polling.
    """
    d = get_daemon(request)

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return json_error("invalid JSON body", status=400)

    session_id = body.get("session_id", "")
    _log.info("session ended: %s", session_id or "(unknown)")

    # Find which worker this session belongs to and mark it
    worker = _identify_worker(d, body)
    if worker:
        _log.info("session end for worker %s — signaling STUNG", worker.name)
        # Emit event so pilot picks up the session end immediately
        d.broadcast_ws(
            {
                "type": "hook_session_end",
                "worker": worker.name,
                "session_id": session_id,
            }
        )
    else:
        _log.debug("session end from unknown worker (session_id=%s)", session_id)

    return web.json_response({"status": "ok"})


# Maximum number of unread messages to inline into the SessionStart bootstrap.
# If a worker has more, the rest are summarized as a count + pointer to MCP.
_BOOTSTRAP_MSG_LIMIT = 5

# Truncation limits for the bootstrap markdown so context stays bounded.
_BOOTSTRAP_DESC_CHARS = 500
_BOOTSTRAP_MSG_CHARS = 280

# Discoverability nudge — appended to every bootstrap so workers know the
# Swarm-specific slash commands exist.  See src/swarm/hooks/commands/.
_SLASH_COMMANDS_NUDGE = (
    "**Swarm slash commands available:** "
    "`/swarm-status` `/swarm-handoff` `/swarm-finding` "
    "`/swarm-warning` `/swarm-blocker` `/swarm-progress` "
    "— type `/help` for the full list."
)


def _empty_bootstrap_response() -> web.Response:
    """SessionStart no-op response — Claude Code injects nothing."""
    return web.json_response(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "",
            }
        }
    )


@handle_errors
async def handle_session_start(request: web.Request) -> web.Response:
    """SessionStart hook endpoint — inject per-worker bootstrap into Claude's context.

    Receives Claude Code's SessionStart hook input::

        {"session_id": "...", "cwd": "...", "hook_event_name": "SessionStart",
         "source": "startup"|"resume"|"clear"|"compact", ...}

    Returns ``hookSpecificOutput.additionalContext`` containing the worker's
    assigned task and unread inter-worker messages, so the worker doesn't
    have to remember to call ``swarm_check_messages`` / ``swarm_task_status``
    before starting work.

    Behavior:
      * ``source == "resume"`` → no injection (transcript already has it).
      * Unknown worker → no injection.
      * Daemon errors → fail open (empty additionalContext, status 200).
      * Messages stay unread; the worker still has to ack via MCP.
    """
    d = get_daemon(request)

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return _empty_bootstrap_response()

    source = body.get("source", "startup")
    # Skip on resume — the transcript already contains the original bootstrap
    if source == "resume":
        return _empty_bootstrap_response()

    worker = _identify_worker(d, body)
    if worker is None:
        _log.debug("session start from unknown worker (source=%s)", source)
        return _empty_bootstrap_response()

    additional_context = _build_bootstrap_context(d, worker)
    if not additional_context:
        return _empty_bootstrap_response()

    _log_session_bootstrap(d, worker.name, source, additional_context)

    return web.json_response(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": additional_context,
            }
        }
    )


@handle_errors
async def handle_event(request: web.Request) -> web.Response:
    """Generic hook event endpoint — forward Claude Code lifecycle events.

    Handles SubagentStart, SubagentStop, PreCompact, PostCompact, TeammateIdle.
    """
    d = get_daemon(request)

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return json_error("invalid JSON body", status=400)

    # Claude Code sends "hook_event_name"; keep "hook_event" as a fallback
    # for manual test payloads and forward-compat.
    hook_event = body.get("hook_event_name") or body.get("hook_event", "unknown")
    worker = _identify_worker(d, body)
    worker_name = worker.name if worker else "unknown"

    _log.debug("hook event %s from worker %s", hook_event, worker_name)

    # Track compaction state on workers (+ capture before/after token delta
    # so we can measure compaction effectiveness over time).
    if worker and hook_event in ("PreCompact", "preCompact"):
        worker.compacting = True
        worker._compact_tokens_before = worker.usage.last_turn_input_tokens
    elif worker and hook_event in ("PostCompact", "postCompact"):
        worker.compacting = False
        worker._context_warned = False  # reset warning after successful compact
        _log_compact_event(d, worker, body)

    # Broadcast to dashboard subscribers
    d.broadcast_ws(
        {
            "type": "hook_event",
            "hook_event": hook_event,
            "worker": worker_name,
            "data": body,
        }
    )

    return web.json_response({"status": "ok"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log_compact_event(d: SwarmDaemon, worker: Worker, body: dict[str, Any]) -> None:
    """Record a compact event to the buzz log with before/after token counts.

    PreCompact stashed ``worker._compact_tokens_before`` on the worker.
    At PostCompact we read the current turn tokens and record the delta
    so the operator can measure how effective compaction is over time.
    Trigger ('auto' vs 'manual') is inferred from the hook payload when
    Claude Code supplies it; otherwise defaults to 'manual'.
    """
    before = worker._compact_tokens_before
    after = worker.usage.last_turn_input_tokens
    trigger = str(body.get("trigger") or body.get("compact_trigger") or "manual")
    ratio: float | None = None
    if before > 0 and after >= 0:
        ratio = round(after / before, 3) if before else None
    metadata: dict[str, object] = {
        "tokens_before": before,
        "tokens_after": after,
        "trigger": trigger,
    }
    if ratio is not None:
        metadata["ratio"] = ratio
    detail_parts = [f"{before}→{after} tokens"]
    if ratio is not None:
        detail_parts.append(f"ratio={ratio}")
    detail_parts.append(f"trigger={trigger}")
    d.drone_log.add(
        SystemAction.COMPACT,
        worker.name,
        " ".join(detail_parts),
        category=LogCategory.COMPACT,
        metadata=metadata,
    )
    worker._compact_tokens_before = 0


def _evaluate_rules(
    d: SwarmDaemon, body: dict[str, Any], tool_name: str, tool_text: str
) -> web.Response:
    """Evaluate tool use against drone approval rules and return a JSON response."""
    from swarm.drones.rules import dry_run_rules

    pilot = d.pilot
    if pilot is None or not pilot.enabled:
        return web.json_response({"decision": "passthrough", "reason": "drones disabled"})

    drone_config = d.config.drones
    worker = _identify_worker(d, body)
    worker_name = worker.name if worker else "unknown"

    # Collect per-worker + global rules
    worker_rules: list[Any] = []
    if worker and pilot._worker_configs:
        wc = pilot._worker_configs.get(worker.name)
        if wc is not None and wc.approval_rules:
            worker_rules = list(wc.approval_rules)
    all_rules = worker_rules + list(drone_config.approval_rules)

    if not all_rules and tool_name not in _ALWAYS_ESCALATE_TOOLS:
        _log_hook_decision(d, tool_name, "approve", "no rules configured", worker_name)
        return web.json_response({"decision": "approve", "reason": "no approval rules configured"})

    results = dry_run_rules(
        tool_text, all_rules, allowed_read_paths=drone_config.allowed_read_paths
    )
    if not results:
        return web.json_response({"decision": "passthrough", "reason": "no matching rule"})

    result = results[0]
    if result.decision == "approve":
        _log_hook_decision(d, tool_name, "approve", f"rule matched: {result.source}", worker_name)
        return web.json_response(
            {
                "decision": "approve",
                "reason": f"Approved by drone rule ({result.source})",
            }
        )

    # "escalate" → check if queen can handle this autonomously
    if _queen_can_approve(d, tool_name):
        _log_hook_decision(
            d, tool_name, "approve", f"queen-delegated: {result.source}", worker_name
        )
        return web.json_response(
            {
                "decision": "approve",
                "reason": f"Approved under queen oversight ({result.source})",
            }
        )

    # No queen → pass through so Claude Code shows the normal permission prompt
    _log_hook_decision(d, tool_name, "passthrough", f"escalated: {result.source}", worker_name)
    return web.json_response(
        {
            "decision": "passthrough",
            "reason": f"Requires operator approval ({result.source})",
        }
    )


_MAX_RECENT_TOOLS = 5


def _check_file_lock(
    d: SwarmDaemon, worker: Worker | None, tool_name: str, tool_input: dict[str, Any]
) -> web.Response | None:
    """Block Edit/Write if another worker holds the file lock."""
    if tool_name not in ("Edit", "Write"):
        return None
    file_path = tool_input.get("file_path", "")
    if not file_path:
        return None
    import os
    import time

    resolved = os.path.realpath(file_path)
    lock = d.file_locks.get(resolved)
    worker_name = worker.name if worker else "unknown"
    now = time.time()
    if lock:
        lock_owner, lock_time = lock
        if lock_owner != worker_name and (now - lock_time) < d._file_lock_ttl:
            _log.info("file conflict: %s locked by %s", resolved, lock_owner)
            return web.json_response(
                {
                    "decision": "block",
                    "reason": f"File locked by worker {lock_owner}",
                }
            )
    # Acquire/refresh lock
    d.file_locks[resolved] = (worker_name, now)
    return None


def _record_tool_activity(worker: Worker, tool_name: str, tool_input: dict[str, Any]) -> None:
    """Append tool call to worker's recent_tools list (max 5)."""
    desc = tool_name
    if tool_name == "Bash" and "command" in tool_input:
        cmd = str(tool_input["command"])[:60]
        desc = f"Bash: {cmd}"
    elif "file_path" in tool_input:
        desc = f"{tool_name}: {tool_input['file_path']}"
    worker.recent_tools.append({"tool": tool_name, "desc": desc})
    if len(worker.recent_tools) > _MAX_RECENT_TOOLS:
        worker.recent_tools[:] = worker.recent_tools[-_MAX_RECENT_TOOLS:]


def _queen_can_approve(d: SwarmDaemon, tool_name: str) -> bool:
    """Check if the queen is active and can handle this approval autonomously."""
    queen = getattr(d, "queen", None)
    if queen is None or not queen.enabled or not queen.can_call:
        return False
    # Don't auto-approve Bash under queen — too risky without explicit review
    if tool_name in _ALWAYS_ESCALATE_TOOLS:
        return False
    return True


def _build_tool_text(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Build a text representation of a tool call for rules matching.

    Mimics the format the drone sees in terminal output so existing
    regex-based approval rules work unchanged.
    """
    parts = [tool_name]
    if tool_name == "Bash" and "command" in tool_input:
        parts.append(str(tool_input["command"]))
    elif tool_name == "Write" and "file_path" in tool_input:
        parts.append(str(tool_input["file_path"]))
    elif tool_name == "Edit" and "file_path" in tool_input:
        parts.append(str(tool_input["file_path"]))
    elif tool_name == "Read" and "file_path" in tool_input:
        parts.append(f"Read({tool_input['file_path']})")
    else:
        # Generic: include all input values
        for v in tool_input.values():
            parts.append(str(v))
    return "\n".join(parts)


def _identify_worker(d: SwarmDaemon, body: dict[str, Any]) -> Worker | None:
    """Best-effort worker identification from hook input.

    Tries session_id first, then CWD matching against worker paths.
    """
    # Try session_id if present (future: map session IDs to workers)
    # For now, match by CWD — hooks inherit the Claude Code process CWD
    cwd = body.get("cwd", "")
    if not cwd:
        # Fall back to SWARM_WORKER env var if the hook script forwards it
        cwd = body.get("worker_cwd", "")

    if cwd:
        cwd_resolved = os.path.realpath(cwd)
        for w in d.workers:
            if hasattr(w, "path") and w.path:
                worker_path = os.path.realpath(str(w.path))
                if cwd_resolved == worker_path or cwd_resolved.startswith(worker_path + "/"):
                    return w

    # Fallback: if only one worker exists, it's probably that one
    if len(d.workers) == 1:
        return d.workers[0]

    return None


def _log_hook_decision(
    d: SwarmDaemon,
    tool_name: str,
    decision: str,
    reason: str,
    worker_name: str = "unknown",
) -> None:
    """Log a hook-based approval decision to the drone log."""
    if d.drone_log is not None:
        d.drone_log.add(
            DroneAction.CONTINUED if decision == "approve" else SystemAction.QUEEN_BLOCKED,
            worker_name,
            f"hook:{tool_name} → {decision} ({reason})",
            metadata={"source": "hook", "tool_name": tool_name},
            category=LogCategory.DRONE,
        )


def _bootstrap_task_block(d: SwarmDaemon, worker_name: str) -> str:
    """Render the active-task section of the bootstrap, or '' if none."""
    task_board = getattr(d, "task_board", None)
    if task_board is None:
        return ""
    try:
        active_tasks = task_board.active_tasks_for_worker(worker_name)
    except Exception:
        _log.warning("failed to fetch active tasks for %s", worker_name, exc_info=True)
        return ""
    if not active_tasks:
        return ""

    # Workers are typically assigned a single task at a time; show the first.
    task = active_tasks[0]
    description = (task.description or "").strip()
    if len(description) > _BOOTSTRAP_DESC_CHARS:
        description = description[:_BOOTSTRAP_DESC_CHARS].rstrip() + "…"
    lines = [
        f"**Your assigned task:** {task.title}",
        f"**Status:** {task.status.value}",
    ]
    if description:
        lines.append(f"**Description:** {description}")
    return "\n".join(lines)


def _bootstrap_messages_block(d: SwarmDaemon, worker_name: str) -> str:
    """Render the unread-messages section of the bootstrap, or '' if none."""
    message_store = getattr(d, "message_store", None)
    if message_store is None:
        return ""
    try:
        unread = message_store.get_unread(worker_name, limit=20)
    except Exception:
        _log.warning("failed to fetch unread messages for %s", worker_name, exc_info=True)
        return ""
    if not unread:
        return ""

    shown = unread[:_BOOTSTRAP_MSG_LIMIT]
    overflow = len(unread) - len(shown)
    lines = [f"**Unread messages ({len(unread)}):**"]
    for msg in shown:
        content = (msg.content or "").strip().replace("\n", " ")
        if len(content) > _BOOTSTRAP_MSG_CHARS:
            content = content[:_BOOTSTRAP_MSG_CHARS].rstrip() + "…"
        lines.append(f"- From `{msg.sender}` ({msg.msg_type}): {content}")
    if overflow > 0:
        lines.append(f"- *…and {overflow} more — call `swarm_check_messages` for the full list*")
    return "\n".join(lines)


def _build_bootstrap_context(d: SwarmDaemon, worker: Worker) -> str:
    """Assemble the SessionStart bootstrap markdown for a worker.

    Returns an empty string if there's nothing to inject.
    """
    parts = [
        block
        for block in (
            _bootstrap_task_block(d, worker.name),
            _bootstrap_messages_block(d, worker.name),
        )
        if block
    ]
    if not parts:
        return ""

    # Append the slash-commands nudge so workers discover /swarm-* in /help.
    # Only emitted when there's already bootstrap content; fresh empty workers
    # discover via /help directly.
    parts.append(_SLASH_COMMANDS_NUDGE)

    header = "## Swarm Bootstrap"
    footer = (
        "_This bootstrap was injected by the Swarm SessionStart hook. "
        "Messages remain unread until you call `swarm_check_messages`._"
    )
    return "\n\n".join([header, *parts, footer])


def _log_session_bootstrap(
    d: SwarmDaemon, worker_name: str, source: str, additional_context: str
) -> None:
    """Record the bootstrap event in the drone/buzz log for visibility."""
    if d.drone_log is None:
        return
    try:
        d.drone_log.add(
            SystemAction.SESSION_BOOTSTRAP,
            worker_name,
            f"session_start({source}): injected {len(additional_context)} chars",
            metadata={"source": source, "context_chars": len(additional_context)},
            category=LogCategory.SYSTEM,
        )
    except Exception:
        _log.warning("failed to log session bootstrap for %s", worker_name, exc_info=True)
