"""Drone decision rules — determine background drones actions for each worker."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from swarm.config import DroneApprovalRule, DroneConfig
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.providers.base import LLMProvider
    from swarm.providers.events import TerminalEvent


@dataclass
class DryRunResult:
    """Result of a dry-run evaluation against approval rules."""

    matched: bool
    decision: str  # "approve" or "escalate"
    rule_index: int  # -1 when no user rule matched
    rule_pattern: str  # regex that matched, or "" if none
    source: str  # "always_escalate", "safe_builtin", "rule", "default_escalate"


class Decision(Enum):
    NONE = "none"
    CONTINUE = "continue"  # Send Enter (accept prompt, select default, continue)
    REVIVE = "revive"
    ESCALATE = "escalate"


@dataclass
class DroneDecision:
    decision: Decision
    reason: str = ""
    rule_pattern: str = ""  # regex pattern that matched (test mode enrichment)
    rule_index: int = -1  # index in approval_rules (-1 = no match)
    source: str = ""  # "builtin", "rule", or "escalation" — distinguishes decision origin
    events: list[TerminalEvent] | None = None  # structured events from terminal output
    # Confidence in this decision, 0.0-1.0. Rule-based decisions get 1.0
    # (exact regex match). Future LLM-classifier decisions will set
    # fractional values so the pilot can escalate low-confidence calls.
    confidence: float | None = None


# Patterns that ALWAYS escalate — never auto-approve regardless of user rules.
# Must be specific to genuinely destructive operations. Do NOT include words
# like "production" or "database" that appear in normal connection strings.
ALWAYS_ESCALATE = re.compile(
    r"DROP\s+(TABLE|DATABASE|INDEX|SCHEMA|COLUMN)"
    r"|TRUNCATE\s+(TABLE\s+)?\w"
    r"|ALTER\s+(TABLE|DATABASE)\s"
    r"|DELETE\s+FROM\s+\S+\s*;"  # DELETE without WHERE
    r"|rm\s+-(r|rf|fr)\s"
    r"|rm\s+-[a-z]*r[a-z]*\s"  # rm with -r anywhere in flags
    r"|git\s+(push\s+.*--force|reset\s+--hard)"
    r"|--no-verify"
    r"|`\s*DROP\s"  # backtick-escaped SQL
    r"|`\s*TRUNCATE\s",  # backtick-escaped SQL
    re.IGNORECASE,
)


_RE_READ_PATH = re.compile(r"Read\((.+?)\)")


def _get_safe_patterns(provider: LLMProvider | None) -> re.Pattern[str]:
    """Return the safe-tool regex, using provider override if available."""
    if provider is not None:
        return provider.safe_tool_patterns()
    from swarm.providers import get_provider

    return get_provider().safe_tool_patterns()


def _is_allowed_read(content: str, allowed_paths: list[str]) -> bool:
    """Check if a Read operation targets an allowed directory.

    Uses the *last* ``Read(path)`` match in the worker output so that older
    Read operations higher in the scrollback don't shadow the current prompt.

    Uses Path.resolve() to prevent path traversal (e.g. ``../../../etc/passwd``).
    """
    matches = _RE_READ_PATH.findall(content)
    if not matches:
        return False
    # Check the last match — the one closest to the active prompt
    target = Path(os.path.expanduser(matches[-1])).resolve()
    for prefix in allowed_paths:
        allowed = Path(os.path.expanduser(prefix)).resolve()
        try:
            target.relative_to(allowed)
            return True
        except ValueError:
            continue
    return False


def _check_approval_rules(choice_text: str, config: DroneConfig) -> tuple[Decision, str, int]:
    """First-match-wins rule evaluation.  Falls back to ESCALATE (safe default).

    Built-in safety patterns always escalate regardless of user rules.

    Returns (decision, matched_pattern, matched_index).
    """
    # Safety net: always escalate dangerous operations
    if ALWAYS_ESCALATE.search(choice_text):
        return Decision.ESCALATE, "ALWAYS_ESCALATE", -1

    for idx, rule in enumerate(config.approval_rules):
        if rule.compiled.search(choice_text):
            decision = Decision.ESCALATE if rule.action == "escalate" else Decision.CONTINUE
            return decision, rule.pattern, idx
    # No match → escalate (fail-safe); users can add explicit approve rules
    return Decision.ESCALATE, "", -1


def _mark_escalated(_esc: dict[str, float], name: str) -> None:
    """Record escalation timestamp for a worker."""
    import time

    _esc[name] = time.monotonic()


def _has_event_type(events: list[TerminalEvent] | None, type_value: str) -> bool:
    """Check if events list contains an event of the given type."""
    if events is None:
        return False
    return any(e.event_type.value == type_value for e in events)


def _has_structured_events(events: list[TerminalEvent] | None) -> bool:
    """True when events carry real structured typing, not just the base
    ``UNKNOWN`` wrapper.

    Only Claude overrides ``parse_events`` to emit typed events; every other
    provider inherits the base default, which returns a single UNKNOWN event.
    That non-None list must NOT switch prompt detection to the event path —
    doing so silently disables the provider's regex ``has_*_prompt`` methods
    (a Codex/OpenCode/Gemini approval prompt would then never be seen, so the
    drone can't auto-approve it). Gate event-routing on this instead of a bare
    ``events is not None``.
    """
    if not events:
        return False
    from swarm.providers.events import EventType

    return any(e.event_type is not EventType.UNKNOWN for e in events)


def _get_event(events: list[TerminalEvent] | None, type_value: str) -> TerminalEvent | None:
    """Return the first event of the given type, or None."""
    if events is None:
        return None
    for e in events:
        if e.event_type.value == type_value:
            return e
    return None


# Safe tool names that can be auto-approved via event-based matching.
_SAFE_TOOL_NAMES = frozenset({"Glob", "Grep", "Read", "WebSearch", "WebFetch"})


def _is_safe_tool_event(events: list[TerminalEvent] | None) -> bool:
    """Check if events contain a safe tool call that can be auto-approved."""
    tool_event = _get_event(events, "tool_call")
    return tool_event is not None and tool_event.tool_name in _SAFE_TOOL_NAMES


def _check_user_question(
    worker: Worker,
    content: str,
    label: str,
    events: list[TerminalEvent] | None,
    _esc: dict[str, float],
    is_user_question_fn: Callable[[str], bool],
) -> DroneDecision | None:
    """Escalate if prompt is a user question. Returns None if not a question."""
    if _has_structured_events(events):
        is_question = _has_event_type(events, "user_question")
    else:
        is_question = is_user_question_fn(content)
    if not is_question:
        return None
    if worker.name not in _esc:
        _mark_escalated(_esc, worker.name)
        return DroneDecision(
            Decision.ESCALATE,
            f"user question: {label}",
            source="escalation",
            events=events,
        )
    return DroneDecision(
        Decision.NONE, "user question — already escalated, awaiting user", events=events
    )


def _check_allowed_tools(
    worker: Worker,
    events: list[TerminalEvent] | None,
    allowed_tools: list[str] | None,
    _esc: dict[str, float],
) -> DroneDecision | None:
    """Return an ESCALATE decision if the tool is not in allowed_tools, else None."""
    if not allowed_tools:
        return None
    tool_event = _get_event(events, "tool_use") if events else None
    tool_name = tool_event.tool_name if tool_event and hasattr(tool_event, "tool_name") else ""
    if tool_name and tool_name not in allowed_tools:
        if worker.name not in _esc:
            _mark_escalated(_esc, worker.name)
        return DroneDecision(
            Decision.ESCALATE,
            f"tool '{tool_name}' not in allowed_tools for {worker.name}",
            source="allowed_tools",
            events=events,
        )
    return None


def _decide_choice(
    worker: Worker,
    content: str,
    lines: list[str],
    cfg: DroneConfig,
    _esc: dict[str, float],
    provider: LLMProvider | None = None,
    events: list[TerminalEvent] | None = None,
    allowed_tools: list[str] | None = None,
) -> DroneDecision:
    """Decide action for a worker showing a choice menu."""
    # Use provider methods when available, fall back to default provider
    if provider is None:
        from swarm.providers import get_provider

        provider = get_provider()
    _get_choice_summary = provider.get_choice_summary
    _is_user_question = provider.is_user_question

    selected = _get_choice_summary(content)
    label = f"choice menu — selected '{selected}'" if selected else "choice menu"

    # AskUserQuestion prompts require user decision — never auto-continue.
    question_result = _check_user_question(worker, content, label, events, _esc, _is_user_question)
    if question_result:
        return question_result

    # Trim to last TAIL_WIDE lines for safe-pattern matching — prevents stale
    # output (e.g. old "plan" text) from triggering rules on unrelated prompts.
    from swarm.providers.base import TAIL_MEDIUM, TAIL_WIDE

    prompt_area = "\n".join(lines[-TAIL_WIDE:])

    # Read operations from allowed directories — auto-approve without rules check
    if cfg.allowed_read_paths and _is_allowed_read(content, cfg.allowed_read_paths):
        return DroneDecision(
            Decision.CONTINUE, f"read from allowed path: {label}", source="builtin", events=events
        )

    # Per-worker tool restrictions
    blocked = _check_allowed_tools(worker, events, allowed_tools, _esc)
    if blocked:
        return blocked

    # Built-in safe operations — fast-approve before hitting approval_rules.
    # Event-based: check tool_name directly. Regex fallback: pattern match.
    is_safe = _is_safe_tool_event(events) or _get_safe_patterns(provider).search(prompt_area)
    if is_safe and not ALWAYS_ESCALATE.search(prompt_area):
        return DroneDecision(
            Decision.CONTINUE, f"safe operation: {label}", source="builtin", events=events
        )

    # Narrow window for user-defined approval rules (TAIL_MEDIUM lines vs
    # TAIL_WIDE for safe patterns).  The actual tool prompt is typically 6-8
    # lines; using TAIL_MEDIUM gives enough margin for multi-line commands
    # while preventing stale context (e.g. "plan" in a task description 20
    # lines above) from matching broad user rules like `\bplan\b`.
    rule_area = "\n".join(lines[-TAIL_MEDIUM:])

    # Standard permission/tool prompts — check approval rules, then auto-continue.
    if cfg.approval_rules:
        ruling, matched_pattern, matched_index = _check_approval_rules(rule_area, cfg)
        if ruling == Decision.ESCALATE:
            if worker.name not in _esc:
                _mark_escalated(_esc, worker.name)
                return DroneDecision(
                    Decision.ESCALATE,
                    f"choice requires approval: {label}",
                    rule_pattern=matched_pattern,
                    rule_index=matched_index,
                    source="rule",
                    events=events,
                )
            return DroneDecision(
                Decision.NONE, "choice — already escalated, awaiting user", events=events
            )
        return DroneDecision(
            Decision.CONTINUE,
            label,
            rule_pattern=matched_pattern,
            rule_index=matched_index,
            source="rule",
            events=events,
        )
    return DroneDecision(Decision.CONTINUE, label, source="builtin", events=events)


def _decide_accept_edits(
    worker: Worker,
    lines: list[str],
    _esc: dict[str, float],
    events: list[TerminalEvent] | None = None,
) -> DroneDecision:
    """Decide action for an 'accept edits' prompt.

    File-only edits are safe to auto-accept.  Prompts that include bash
    commands (e.g. "accept edits on · 2 bashes") require operator approval.
    """
    # Event-based: check metadata directly. Regex fallback: search tail text.
    ae_event = _get_event(events, "accept_edits")
    if ae_event is not None:
        has_bash = bool(ae_event.metadata.get("has_bash"))
    else:
        has_bash = "bash" in "\n".join(lines[-5:]).lower()
    if has_bash:
        if worker.name not in _esc:
            _mark_escalated(_esc, worker.name)
        return DroneDecision(
            Decision.ESCALATE,
            "accept edits includes bash commands — needs operator approval",
            source="builtin",
            events=events,
        )
    return DroneDecision(
        Decision.CONTINUE,
        "accept edits (files only) — auto-accepting",
        source="builtin",
        events=events,
    )


def _decide_idle_state(
    worker: Worker,
    content: str,
    lines: list[str],
    cfg: DroneConfig,
    _esc: dict[str, float],
    provider: LLMProvider | None = None,
    events: list[TerminalEvent] | None = None,
    allowed_tools: list[str] | None = None,
) -> DroneDecision:
    """Decide action for a RESTING worker based on worker output."""
    # Use provider methods when available, fall back to default provider
    if provider is None:
        from swarm.providers import get_provider

        provider = get_provider()
    _has_plan_prompt = provider.has_plan_prompt
    _has_choice_prompt = provider.has_choice_prompt
    _has_empty_prompt = provider.has_empty_prompt
    _has_accept_edits_prompt = provider.has_accept_edits_prompt
    _has_idle_prompt = provider.has_idle_prompt

    # Event-based routing only when the provider emits structured events
    # (Claude); otherwise fall back to the provider's regex detectors so
    # non-Claude approval/plan prompts aren't silently missed.
    _use_events = _has_structured_events(events)
    has_plan = _has_event_type(events, "plan") if _use_events else _has_plan_prompt(content)
    has_choice = _has_event_type(events, "choice") if _use_events else _has_choice_prompt(content)

    # Plan approval prompts always escalate — never auto-approve plans
    if has_plan:
        if worker.name not in _esc:
            _mark_escalated(_esc, worker.name)
            return DroneDecision(
                Decision.ESCALATE, "plan requires user approval", source="escalation", events=events
            )
        return DroneDecision(
            Decision.NONE, "plan — already escalated, awaiting user", events=events
        )

    if has_choice:
        return _decide_choice(
            worker,
            content,
            lines,
            cfg,
            _esc,
            provider=provider,
            events=events,
            allowed_tools=allowed_tools,
        )

    # Check idle/suggestion hints BEFORE empty prompt — a suggestion at the
    # idle prompt can look like an empty prompt line, but `? for shortcuts`
    # (or `ctrl+t to hide`) in the tail means the user has a suggestion
    # pre-filled.  Only the operator should press Enter on those.
    # (Use a narrow hints-only check here; the full has_idle_prompt is broader
    # and would false-positive on normal `>` prompts.)
    from swarm.providers.base import TAIL_NARROW

    tail_lower = "\n".join(lines[-TAIL_NARROW:]).lower()
    if "? for shortcuts" in tail_lower or "ctrl+t to hide" in tail_lower:
        return DroneDecision(Decision.NONE, "idle at prompt", events=events)

    if _has_empty_prompt(content):
        return DroneDecision(Decision.NONE, "empty prompt — idle", events=events)

    has_ae = (
        _has_event_type(events, "accept_edits")
        if _use_events
        else _has_accept_edits_prompt(content)
    )
    if has_ae:
        return _decide_accept_edits(worker, lines, _esc, events=events)

    if _has_idle_prompt(content):
        return DroneDecision(Decision.NONE, "idle at prompt", events=events)

    # Unknown/unrecognized prompt state — escalate to Queen
    if worker.resting_duration > cfg.escalation_threshold and worker.name not in _esc:
        from swarm.providers.events import EventType, TerminalEvent

        _mark_escalated(_esc, worker.name)
        unknown_event = TerminalEvent(
            EventType.UNKNOWN_PROMPT, content="\n".join(lines[-TAIL_NARROW:])
        )
        return DroneDecision(
            Decision.ESCALATE,
            f"unrecognized state for {worker.resting_duration:.0f}s",
            source="escalation",
            events=[*(events or []), unknown_event],
        )

    return DroneDecision(Decision.NONE, "resting, monitoring", events=events)


def _effective_config(
    config: DroneConfig,
    worker_rules: list[DroneApprovalRule] | None = None,
) -> DroneConfig:
    """Return a DroneConfig with per-worker approval rules prepended if present.

    Worker-level rules take priority (checked first) over global rules.
    """
    if not worker_rules:
        return config
    merged_rules = list(worker_rules) + list(config.approval_rules)
    # Create a shallow copy with merged rules
    return DroneConfig(
        enabled=config.enabled,
        escalation_threshold=config.escalation_threshold,
        poll_interval=config.poll_interval,
        poll_interval_buzzing=config.poll_interval_buzzing,
        poll_interval_waiting=config.poll_interval_waiting,
        poll_interval_resting=config.poll_interval_resting,
        auto_approve_yn=config.auto_approve_yn,
        max_revive_attempts=config.max_revive_attempts,
        max_poll_failures=config.max_poll_failures,
        max_idle_interval=config.max_idle_interval,
        auto_stop_on_complete=config.auto_stop_on_complete,
        auto_approve_assignments=config.auto_approve_assignments,
        idle_assign_threshold=config.idle_assign_threshold,
        auto_complete_min_idle=config.auto_complete_min_idle,
        sleeping_poll_interval=config.sleeping_poll_interval,
        sleeping_threshold=config.sleeping_threshold,
        stung_reap_timeout=config.stung_reap_timeout,
        state_thresholds=config.state_thresholds,
        approval_rules=merged_rules,
        allowed_read_paths=config.allowed_read_paths,
        context_warning_threshold=config.context_warning_threshold,
        context_critical_threshold=config.context_critical_threshold,
    )


def decide(
    worker: Worker,
    content: str,
    config: DroneConfig | None = None,
    escalated: dict[str, float] | None = None,
    provider: LLMProvider | None = None,
    events: list[TerminalEvent] | None = None,
    worker_rules: list[DroneApprovalRule] | None = None,
    allowed_tools: list[str] | None = None,
) -> DroneDecision:
    """Decide what background drones action to take for a worker.

    Args:
        escalated: per-pilot dict tracking which workers have been escalated
                   (name → monotonic escalation time).
                   If None, escalation tracking is disabled.
        provider: LLM provider for provider-specific detection patterns.
                  If None, uses Claude Code defaults via state.py.
        events: structured terminal events from provider.parse_events().
                If None, falls back to regex-based detection.
        worker_rules: per-worker approval rules (checked before global rules).
    """
    cfg = _effective_config(config or DroneConfig(), worker_rules)
    _esc = escalated if escalated is not None else {}
    lines = content.strip().splitlines()

    if worker.state == WorkerState.STUNG:
        if worker.revive_count >= cfg.max_revive_attempts:
            if worker.name not in _esc:
                _mark_escalated(_esc, worker.name)
                return DroneDecision(
                    Decision.ESCALATE,
                    f"crash loop — {worker.revive_count} revives exhausted",
                    events=events,
                )
            return DroneDecision(
                Decision.NONE, "crash loop — already escalated, awaiting user", events=events
            )
        return DroneDecision(Decision.REVIVE, "worker exited", events=events)

    if worker.state == WorkerState.BUZZING:
        # Check if content contains an actionable prompt despite BUZZING state.
        # This catches prompts that appeared while "esc to interrupt" is still
        # in the terminal buffer (stale indicator, classifier hasn't caught up).
        if provider is None:
            from swarm.providers import get_provider

            provider = get_provider()
        has_actionable = (
            provider.has_choice_prompt(content)
            or provider.has_plan_prompt(content)
            or provider.has_accept_edits_prompt(content)
        )
        if has_actionable:
            return _decide_idle_state(
                worker,
                content,
                lines,
                cfg,
                _esc,
                provider=provider,
                events=events,
                allowed_tools=allowed_tools,
            )
        _esc.pop(worker.name, None)
        return DroneDecision(Decision.NONE, "actively working", events=events)

    # Both RESTING and WAITING workers need prompt evaluation
    return _decide_idle_state(
        worker,
        content,
        lines,
        cfg,
        _esc,
        provider=provider,
        events=events,
        allowed_tools=allowed_tools,
    )


def dry_run_rules(
    content: str,
    approval_rules: list[DroneApprovalRule],
    allowed_read_paths: list[str] | None = None,
    provider: LLMProvider | None = None,
) -> list[DryRunResult]:
    """Evaluate content against approval rules without taking action.

    Runs the same pipeline as ``_decide_choice``:
    1. ``ALWAYS_ESCALATE`` safety net
    2. ``_is_allowed_read`` (if allowed_read_paths given)
    3. Safe-builtin patterns
    4. User-defined approval_rules (first-match-wins)
    5. Default escalate (no match)

    Returns a list with a single winning ``DryRunResult``.
    """
    # 1. Always-escalate safety net
    if ALWAYS_ESCALATE.search(content):
        return [
            DryRunResult(
                matched=True,
                decision="escalate",
                rule_index=-1,
                rule_pattern="ALWAYS_ESCALATE",
                source="always_escalate",
            )
        ]

    # 2. Allowed read paths
    if allowed_read_paths and _is_allowed_read(content, allowed_read_paths):
        return [
            DryRunResult(
                matched=True,
                decision="approve",
                rule_index=-1,
                rule_pattern="",
                source="safe_builtin",
            )
        ]

    # 3. Safe builtin patterns
    safe = _get_safe_patterns(provider)
    if safe.search(content) and not ALWAYS_ESCALATE.search(content):
        return [
            DryRunResult(
                matched=True,
                decision="approve",
                rule_index=-1,
                rule_pattern="",
                source="safe_builtin",
            )
        ]

    # 4. User-defined approval rules (first-match-wins)
    cfg = DroneConfig(approval_rules=approval_rules, allowed_read_paths=allowed_read_paths or [])
    for idx, rule in enumerate(cfg.approval_rules):
        if rule.compiled.search(content):
            decision = "escalate" if rule.action == "escalate" else "approve"
            return [
                DryRunResult(
                    matched=True,
                    decision=decision,
                    rule_index=idx,
                    rule_pattern=rule.pattern,
                    source="rule",
                )
            ]

    # 5. No match — default escalate
    return [
        DryRunResult(
            matched=False,
            decision="escalate",
            rule_index=-1,
            rule_pattern="",
            source="default_escalate",
        )
    ]
