"""Claude Code provider — extracts all Claude-specific CLI behavior."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from swarm.providers.base import (
    SAFE_GIT_SUBCMDS,
    SAFE_SHELL_CMDS,
    TAIL_LAST_LINE,
    TAIL_MEDIUM,
    TAIL_NARROW,
    TAIL_WIDE,
    LLMProvider,
)
from swarm.providers.events import EventType, TerminalEvent
from swarm.providers.styled import StyledContent
from swarm.worker.worker import TokenUsage, WorkerState

_style_log = logging.getLogger("swarm.style_discovery")
_STYLE_DISCOVERY = os.environ.get("SWARM_STYLE_DISCOVERY", "") == "1"

# Pre-compiled patterns — these run every poll cycle for every worker
_RE_PROMPT = re.compile(r"^\s*[>❯]", re.MULTILINE)
_RE_CURSOR_OPTION = re.compile(r"^\s*[>❯]\s*\d+\.", re.MULTILINE)
_RE_OTHER_OPTION = re.compile(r"^\s+\d+\.", re.MULTILINE)
_RE_HINTS = re.compile(r"(\? for shortcuts|ctrl\+t to hide)", re.IGNORECASE)
_RE_EMPTY_PROMPT = re.compile(r"^[>❯]\s*$")
# Subagent / spinner activity in the Claude Code 2.x TUI.
#
# Three signals — any one means "Claude is actively working":
#
#   1. ``↓ N tokens``        — subagent finishing, token-pull indicator
#   2. ``thought for N``     — extended-thinking indicator
#   3. ``<glyph> <Verb><suffix>`` — animated spinner
#
# The spinner glyph set is the official Claude Code character cycle per
# the source mirror (kdxsydq/ClaudeCode, src/components/Spinner/utils.ts):
#
#     macOS:        · ✢ ✳ ✶ ✻ ✽
#     Linux/Win:    · ✢ * ✶ ✻ ✽
#     Ghostty:      · ✢ ✳ ✶ ✻ *
#
# The animation plays forward + backward, so any of the union characters
# can appear on screen on any given poll. ``·`` and ``*`` are ambiguous
# on their own (separators, list bullets), so we require the trailing
# verb + termination (``…``, ``...``, or `` for <digit>...``) to avoid
# false positives on lines like ``auto mode on · esc to interrupt``.
#
# The verb itself is intentionally ``\w+`` rather than a fixed list —
# Claude Code rotates verbs constantly (Cooking, Sautéed, Brewing,
# Wrangling, Tinkering, Verifying, Shipping, Generating, etc.) and
# pinning the list would break with each Claude Code release.
#
# Legacy Braille glyphs (``⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏``) are kept so older Claude
# Code versions, generic-provider workers, and other CLIs that share
# the npm cli-spinners ``dots`` set still classify correctly.
_RE_SUBAGENT_ACTIVE = re.compile(
    r"↓\s*[\d.]+k?\s*tokens"
    r"|thought for \d+"
    r"|[·✢✳✶✻✽*⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s+\w+(?:…|\.\.\.|\s+for\s+\d)",
    re.IGNORECASE,
)
# Claude Code's interruptible-turn footer is "… · esc to interrupt", but at
# narrow PTY widths it TRUNCATES to "… · esc to…" (verified live on workers
# my-rcg / budgetbug, Claude Code v2.1.158). Idle footers instead show
# "· ← for agents" / "· ? for shortcuts" — never "esc to" — so the (possibly
# truncated) hint is an active-turn-only signal. Matching only the full literal
# "esc to interrupt" misses the truncated form, so an active worker whose
# animated spinner glyph isn't on-screen at poll time (between animation frames
# or while a tool result renders) was misclassified RESTING.
# NOTE: must NOT match choice-menu footers, which carry "Esc to cancel" — so we
# match only the interrupt/stop hint and the observed truncation "esc to…"
# (ellipsis right after "to"), never a bare "esc to <word>".
_RE_INTERRUPT_HINT = re.compile(
    r"esc to int"  # "esc to interrupt" + mid-word truncations ("esc to int…")
    r"|esc to stop"
    r"|esc to ?…",  # truncated right after "to": "esc to…" / "esc to …"
    re.IGNORECASE,
)
_RE_ACCEPT_EDITS = re.compile(r">>\s*accept edits on", re.IGNORECASE)

# Claude Code's auto-mode (2.x+) lets users background long-running work so the
# chat prompt returns for follow-up input while the work continues. Two flavours
# share the same surface forms:
#   - "monitors": long-running watchers (dev servers, test runners, …)
#   - "shells":  async Bash commands launched in auto mode
# While background work is running "esc to interrupt" is absent — Claude itself
# is idle for the current turn — but the worker is not available for new work.
# Swarm must treat these as BUZZING so the pilot doesn't auto-assign on top of
# running background work and the sidebar stays coloured.
# Two surface forms, either can appear on screen:
#   Header: "* Brewed for 2m 19s · 1 monitor still running"
#           "* Sautéed for 1m 17s · 2 shells still running"
#   Footer: "auto mode on · 1 monitor · ↓ to manage"
#           "auto mode on · 2 shells · ↓ to manage"
# We match both so the signal is robust to Claude UI tweaks.
_RE_BACKGROUND_RUNNING = re.compile(
    r"(\d+\s+(?:monitors?|shells?)\s+still\s+running"
    r"|auto\s+mode\s+on\s*[·.]?\s*\d+\s+(?:monitors?|shells?))",
    re.IGNORECASE,
)
# Claude Code dynamic workflows (Opus 4.8+, the ``Workflow`` tool) fan out
# ephemeral subagents from a deterministic script. A launched workflow runs in
# the *background*: the tool call returns immediately, the worker's turn yields,
# and the prompt reappears while subagents keep executing — so the worker LOOKS
# idle but is not free for new work and will be re-invoked on completion. Claude
# Code's footer status tray surfaces the in-flight run; Swarm must read that as
# BUZZING (same rationale as ``_RE_BACKGROUND_RUNNING`` for shells/monitors) so
# it doesn't nudge, auto-complete, or assign over the worker mid-workflow.
#
# Surface forms verified against the installed Claude Code binary (v2.1.156):
#   Footer tray (count-prefixed):
#     "1 background dynamic workflow"  / "3 background dynamic workflows"   (local)
#     "1 remote dynamic workflow"      / "2 remote dynamic workflows"       (cloud)
#     "2 dynamic workflows"            (inline footer count component)
#   Progress line:
#     "running dynamic workflow"
# The count prefix is what distinguishes an ACTIVE run from non-running mentions
# we must NOT match — "Run a dynamic workflow?" (a permission prompt → WAITING),
# the "(dynamic workflow)" command tag, and "No dynamic workflows in this
# session." (the /workflows history browser).
_RE_WORKFLOW_ACTIVE = re.compile(
    r"\b\d+\s+(?:(?:background|remote)\s+)?dynamic\s+workflows?\b"
    r"|running\s+dynamic\s+workflow\b",
    re.IGNORECASE,
)
# Native ``/loop`` (Claude Code, June 2026) re-runs a worker on a cadence.
# Between fires there is NO persistent footer indicator — unlike a dynamic
# workflow, a parked loop sits at an ordinary idle prompt, so a footer scrape
# can't distinguish "loop-armed, waiting for next tick" from "genuinely free".
# The reliable signal is the ScheduleWakeup *tool result* the harness prints
# into the transcript when the worker self-schedules its next tick and parks:
#
#   "Next wakeup scheduled for <time> (in 270s). Nothing more to do this turn
#    — the harness re-invokes you when the wakeup fires or a task-notification
#    arrives."
#
# (Verified against the installed Claude Code binary, v2.1.186.) The captured
# ``(in Ns)`` is the exact dwell before the worker resumes itself — Swarm uses
# it to compute a precise no-disturb window (see ``LoopDetector``) so the idle-
# watcher and speculative dispatch leave the worker alone until it re-wakes.
# This fires for any ScheduleWakeup-paced loop (native ``/loop`` dynamic mode
# and the autonomous-loop runtime), which is exactly the parked-idle case we
# must not disturb. Fixed-cadence (cron) ``/loop`` tasks are a follow-up — they
# don't emit this line.
_RE_LOOP_WAKEUP = re.compile(
    r"Next wakeup scheduled for .+?\(in (\d+)s\)",
    re.IGNORECASE,
)
_RE_PLAN_MARKERS = re.compile(
    r"plan file|plan saved|"
    r"proceed with (?:this|the) plan|"
    r"approve (?:this|the) plan|"
    r"(?<!how )would you like to proceed|"
    r"has written.*\bplan\b",
    re.IGNORECASE,
)

# Tool name extraction — captures the tool name from approval prompts.
_RE_TOOL_NAME = re.compile(
    r"(Bash|Edit|Write|Read|Glob|Grep|NotebookEdit|WebSearch|WebFetch|Agent|Skill)"
    r"(?:\s+\w|\()",
    re.MULTILINE,
)

_BUILTIN_SAFE_PATTERNS = re.compile(
    # Old format: Bash(ls ...) — tool-call style
    rf"Bash\(.*({SAFE_SHELL_CMDS})\b"
    rf"|Bash\(.*git\s+({SAFE_GIT_SUBCMDS})\b"
    r"|Bash\(.*uv\s+run\s+(pytest|ruff)\b"
    # New format: "Bash command\n  ls ..." — indented command on next line
    rf"|Bash command\s+({SAFE_SHELL_CMDS})\b"
    rf"|Bash command\s+git\s+({SAFE_GIT_SUBCMDS})\b"
    r"|Bash command\s+uv\s+run\s+(pytest|ruff)\b"
    # Tool patterns — both old Foo(...) and new "Foo " header formats
    r"|Glob\(|Glob "
    r"|Grep\(|Grep "
    r"|Read\(|Read file"
    r"|WebSearch\(|WebSearch "
    r"|WebFetch\(|WebFetch ",
    re.IGNORECASE,
)


# Rate limit detection — exact prefixes from Claude Code source
_RATE_LIMIT_PREFIXES = (
    "You've hit your",
    "You've used",
    "You're now using extra usage",
    "You're close to",
    "You're out of extra usage",
)
_RE_RATE_LIMIT = re.compile(
    r"(?:" + "|".join(re.escape(p) for p in _RATE_LIMIT_PREFIXES) + r")",
    re.IGNORECASE,
)


# Claude-specific plan-mode preamble — names the ``ExitPlanMode`` tool and the
# dashboard-approval UX that only Claude Code has. Relocated here (from
# ``server/messages.py``) so the Claude-specific tool name lives with the Claude
# provider; ``server/messages.py`` falls back to this string for callers that
# don't supply a provider preamble, so the emitted message stays byte-identical.
CLAUDE_PLAN_PREAMBLE = """\
This task came from a user request (Jira ticket, email, or the operator dashboard). \
Use plan mode BEFORE making any changes:

1. Read the task description below and any linked context.
2. Investigate read-only — open relevant files, search the codebase, check git \
history, verify assumptions against the real system if external (database, \
third-party API, CRM, etc.).
3. Call the ExitPlanMode tool with a concrete proposed approach: what you'll \
change, which files, what tests you'll add, what the failure modes are, and \
what you've ruled out.
4. WAIT for the operator to approve the plan from the dashboard.
5. After approval, execute the plan as agreed.

DO NOT edit files, run mutating shell commands, invoke skills, or call \
swarm_complete_task before plan approval. If the task body below invokes a \
skill like /feature or /fix-and-ship, wrap the plan around the skill \
invocation — don't run the skill yet. Worker-to-worker handoffs skip this \
gate; this preamble appears because the task came from a user channel.

--- TASK ---
"""


class ClaudeProvider(LLMProvider):
    """Claude Code CLI provider."""

    @property
    def name(self) -> str:
        return "claude"

    def worker_command(self, resume: bool = True) -> list[str]:
        cmd = ["claude"]
        if resume:
            cmd.append("--continue")
        return cmd

    def headless_command(
        self,
        prompt: str,
        output_format: str = "text",
        max_turns: int | None = None,
        session_id: str | None = None,
    ) -> list[str]:
        args = ["claude", "-p", prompt, "--output-format", output_format]
        if session_id:
            args.extend(["--resume", session_id])
        if max_turns is not None:
            args.extend(["--max-turns", str(max_turns)])
        return args

    def parse_headless_response(self, stdout: bytes) -> tuple[str, str | None]:
        """Parse Claude's JSON envelope: {"type":"result","result":"...","session_id":"..."}."""
        try:
            result = json.loads(stdout.decode())
            if isinstance(result, dict):
                text = result.get("result", "")
                session_id = result.get("session_id")
                return str(text), session_id
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        return stdout.decode(errors="replace"), None

    def _has_actionable_prompt(self, content: str, include_empty: bool = False) -> bool:
        """Check whether content contains a prompt requiring user action."""
        return (
            self.has_choice_prompt(content)
            or self.has_plan_prompt(content)
            or self.has_accept_edits_prompt(content)
            or (include_empty and self.has_empty_prompt(content))
        )

    def _classify_stale_buzzing(self, content: str) -> WorkerState | None:
        """Handle stale "esc to interrupt" in wide tail but not narrow tail.

        Returns a state if the worker has transitioned past BUZZING, or
        None if the indicator is still fresh (caller should return BUZZING).
        """
        tail_last = self._get_tail(content, TAIL_LAST_LINE)
        if _RE_PROMPT.search(tail_last) or "? for shortcuts" in tail_last:
            if self._has_actionable_prompt(content, include_empty=True):
                return WorkerState.WAITING
            if _RE_SUBAGENT_ACTIVE.search(self._get_tail(content, TAIL_WIDE)):
                return None  # not stale — still buzzing
            return WorkerState.RESTING
        if self._has_actionable_prompt(content):
            return WorkerState.WAITING
        return None

    def classify_output(self, command: str, content: str) -> WorkerState:
        if self._is_shell_exited(command):
            return WorkerState.STUNG

        tail_wide = self._get_tail(content, TAIL_WIDE)
        tail_narrow = self._get_tail(content, TAIL_NARROW)

        # When the interrupt hint is in the wide tail but NOT the narrow tail,
        # it may be stale (from before an interruption).
        if _RE_INTERRUPT_HINT.search(tail_wide) and not _RE_INTERRUPT_HINT.search(tail_narrow):
            stale = self._classify_stale_buzzing(content)
            if stale is not None:
                return stale

        if _RE_INTERRUPT_HINT.search(tail_wide):
            return WorkerState.BUZZING

        # Background work (monitor, shell, or in-flight dynamic workflow)
        # present → treat as BUZZING even though the prompt is visible. The
        # worker isn't available for new work and will be re-invoked when the
        # background work completes.
        if _RE_BACKGROUND_RUNNING.search(tail_wide) or _RE_WORKFLOW_ACTIVE.search(tail_wide):
            return WorkerState.BUZZING

        if _RE_PROMPT.search(tail_narrow) or "? for shortcuts" in tail_narrow:
            # Subagent progress (token counters, thinking indicators) → BUZZING
            if _RE_SUBAGENT_ACTIVE.search(tail_wide):
                return WorkerState.BUZZING
            if self._has_actionable_prompt(content, include_empty=True):
                return WorkerState.WAITING
            return WorkerState.RESTING

        if self._has_actionable_prompt(content):
            return WorkerState.WAITING

        return WorkerState.BUZZING

    def has_choice_prompt(self, content: str) -> bool:
        tail = self._get_tail(content, TAIL_WIDE)
        if not tail:
            return False
        return bool(_RE_CURSOR_OPTION.search(tail)) and bool(_RE_OTHER_OPTION.search(tail))

    def is_user_question(self, content: str) -> bool:
        tail_lower = self._get_tail(content, TAIL_MEDIUM).lower()
        return "chat about this" in tail_lower or "type something" in tail_lower

    def get_choice_summary(self, content: str) -> str:
        tail_str = self._get_tail(content, TAIL_WIDE)
        if not tail_str:
            return ""
        tail = tail_str.splitlines()
        cursor_idx = None
        selected = ""
        for i in range(len(tail) - 1, -1, -1):
            if _RE_CURSOR_OPTION.match(tail[i]):
                cursor_idx = i
                selected = tail[i].lstrip().lstrip(">❯").strip()
                break
        if not selected:
            return ""
        question = ""
        for i in range(cursor_idx - 1, -1, -1):
            stripped = tail[i].strip()
            if stripped and not _RE_OTHER_OPTION.match(tail[i]):
                question = stripped
                break
        if question:
            return f'"{question}" → {selected}'
        return selected

    def safe_tool_patterns(self) -> re.Pattern[str]:
        return _BUILTIN_SAFE_PATTERNS

    def env_strip_prefixes(self) -> tuple[str, ...]:
        return ("CLAUDE",)

    def approval_response(self, approve: bool = True) -> str:
        return "\r" if approve else "\x1b"  # Enter to approve, Esc to reject

    def session_dir(self, worker_path: str) -> Path | None:
        encoded = worker_path.replace("/", "-")
        return Path.home() / ".claude" / "projects" / encoded

    def has_plan_prompt(self, content: str) -> bool:
        tail = self._get_tail(content, TAIL_WIDE)
        if not tail:
            return False
        if not (bool(_RE_CURSOR_OPTION.search(tail)) and bool(_RE_OTHER_OPTION.search(tail))):
            return False
        return bool(_RE_PLAN_MARKERS.search(tail))

    def has_accept_edits_prompt(self, content: str) -> bool:
        tail = self._get_tail(content, TAIL_NARROW)
        if not tail:
            return False
        return bool(_RE_ACCEPT_EDITS.search(tail))

    def is_long_running_tool_active(self, content: str) -> bool:
        """Whether the wide PTY tail shows in-flight long-running work.

        Background shells/monitors, an active subagent, or an in-flight
        dynamic workflow. Mirrors the BUZZING-routing checks in
        ``classify_output`` so callers outside the classifier (oversight
        prolonged-BUZZING suppression, the stuck-BUZZING safety net) share
        one definition of "the worker is busy with a long-running tool".
        """
        tail = self._get_tail(content, TAIL_WIDE)
        if not tail:
            return False
        return bool(
            _RE_BACKGROUND_RUNNING.search(tail)
            or _RE_SUBAGENT_ACTIVE.search(tail)
            or _RE_WORKFLOW_ACTIVE.search(tail)
        )

    def has_idle_prompt(self, content: str) -> bool:
        tail = self._get_tail(content, TAIL_NARROW)
        if not tail:
            return False
        if _RE_PROMPT.search(tail):
            return True
        if _RE_HINTS.search(tail):
            return True
        return False

    def has_empty_prompt(self, content: str) -> bool:
        tail = self._get_tail(content, TAIL_LAST_LINE)
        if not tail:
            return False
        return bool(_RE_EMPTY_PROMPT.match(tail.strip()))

    def plan_mode_preamble(self) -> str | None:
        return CLAUDE_PLAN_PREAMBLE

    def has_active_turn_signal(self, content: str) -> bool:
        """Narrow-tail check that the worker is mid-turn.

        Only inspects the last ``TAIL_NARROW`` lines — the active-turn
        indicators are always at the bottom of Claude Code's TUI, whereas
        stale subagent / background-work patterns drift higher in the
        scrollback once their turn completes, so the narrow tail rejects them.
        Relocated from ``drones/state_tracker._has_active_turn_signal`` so the
        Claude-specific regexes live with the Claude provider.
        """
        if not content:
            return False
        tail = "\n".join(content.strip().splitlines()[-TAIL_NARROW:])
        # Interruptible-turn footer — possibly truncated to "esc to…" at narrow
        # PTY widths, so match the hint rather than the full literal.
        if _RE_INTERRUPT_HINT.search(tail):
            return True
        if _RE_BACKGROUND_RUNNING.search(tail):
            return True
        if _RE_SUBAGENT_ACTIVE.search(tail):
            return True
        # In-flight dynamic workflow (footer tray) — keeps the worker BUZZING;
        # without this the stuck-BUZZING safety net would flip a long workflow
        # run to RESTING after the threshold.
        if _RE_WORKFLOW_ACTIVE.search(tail):
            return True
        return False

    @property
    def supports_slash_commands(self) -> bool:
        return True

    @property
    def supports_hooks(self) -> bool:
        return True

    @property
    def supports_native_goal(self) -> bool:
        # Native /goal shipped in Claude Code v2.1.139.
        return True

    @property
    def supports_native_loop(self) -> bool:
        # Native /loop shipped in Claude Code (June 2026). Enables the
        # loop-coexistence guard: a worker parked between /loop fires is
        # left undisturbed (see ``_RE_LOOP_WAKEUP`` / ``LoopDetector``).
        return True

    @property
    def supports_resume(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        return "Claude Code"

    @property
    def supports_max_turns(self) -> bool:
        return True

    @property
    def supports_json_output(self) -> bool:
        return True

    def parse_usage(self, result: dict[str, Any]) -> TokenUsage | None:
        usage = result.get("usage", {})
        if not isinstance(usage, dict):
            return None
        return TokenUsage(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
            cost_usd=result.get("total_cost_usd", 0.0) or 0.0,
        )

    # --- Structured event parsing ---

    def parse_events(self, content: str) -> list[TerminalEvent]:
        """Extract structured events from Claude Code terminal output.

        Returns ALL matching events — content can contain multiple signals
        (e.g. a THINKING indicator + a TOOL_CALL + a CHOICE prompt).
        """
        events: list[TerminalEvent] = []
        tail_wide = self._get_tail(content, TAIL_WIDE)
        tail_narrow = self._get_tail(content, TAIL_NARROW)

        # Thinking indicator
        if "esc to interrupt" in tail_wide:
            events.append(TerminalEvent(EventType.THINKING, "esc to interrupt"))

        # Choice menu (numbered options with cursor)
        if self.has_choice_prompt(content):
            summary = self.get_choice_summary(content)
            meta: dict[str, object] = {"summary": summary} if summary else {}
            events.append(TerminalEvent(EventType.CHOICE, metadata=meta))

        # Plan approval
        if self.has_plan_prompt(content):
            events.append(TerminalEvent(EventType.PLAN))

        # Accept edits
        if self.has_accept_edits_prompt(content):
            has_bash = "bash" in tail_narrow.lower()
            events.append(TerminalEvent(EventType.ACCEPT_EDITS, metadata={"has_bash": has_bash}))

        # User question (AskUserQuestion prompt)
        if self.is_user_question(content):
            events.append(TerminalEvent(EventType.USER_QUESTION))

        # Tool call detection
        tool_match = _RE_TOOL_NAME.search(tail_wide)
        if tool_match:
            events.append(TerminalEvent(EventType.TOOL_CALL, tool_name=tool_match.group(1)))

        # Idle prompt
        if self.has_idle_prompt(content):
            meta_prompt: dict[str, object] = {}
            if self.has_empty_prompt(content):
                meta_prompt["empty"] = True
            events.append(TerminalEvent(EventType.PROMPT, metadata=meta_prompt))

        # Fallback: if nothing matched, emit UNKNOWN
        if not events:
            events.append(TerminalEvent(EventType.UNKNOWN, content))

        return events

    def classify_with_events(
        self, command: str, content: str
    ) -> tuple[WorkerState, list[TerminalEvent]]:
        """Classify worker state and parse events in one pass.

        Calls parse_events() once, then derives state from the events
        combined with the existing classify_output() logic to avoid
        double-parsing the content.
        """
        state = self.classify_output(command, content)
        events = self.parse_events(content)
        return state, events

    # --- Style-aware classification ---

    def classify_styled_output(self, command: str, styled: StyledContent) -> WorkerState:
        """Classify worker state using style data to reduce false positives.

        Style checks can only *tighten* detection (reject a text match),
        never *loosen* it.  If no style signal matches, falls back to
        the text-only ``classify_output()``.
        """
        if self._is_shell_exited(command):
            return WorkerState.STUNG

        if not styled.has_styles():
            return self.classify_output(command, styled.text)

        text = styled.text
        tail_wide = self._get_tail(text, TAIL_WIDE)

        # BUZZING: require "esc to interrupt" to be dim-styled
        buzzing = self._check_styled_buzzing(styled, tail_wide, text)
        if buzzing is not None:
            return buzzing

        # Background work (monitor, shell, or in-flight dynamic workflow)
        # present → BUZZING (same rationale as classify_output — prompt may be
        # visible but the worker isn't free).
        if _RE_BACKGROUND_RUNNING.search(tail_wide) or _RE_WORKFLOW_ACTIVE.search(tail_wide):
            return WorkerState.BUZZING

        # Prompt: require styled (non-default fg) prompt character
        if self._has_styled_prompt(styled):
            if self._has_actionable_prompt(text, include_empty=True):
                return WorkerState.WAITING
            if _RE_SUBAGENT_ACTIVE.search(tail_wide):
                return WorkerState.BUZZING
            # Active-turn footer hint (possibly truncated to "esc to…") — the
            # turn is still running even when the animated spinner glyph isn't
            # on-screen this poll. ``_check_styled_buzzing`` only catches the
            # full dim-styled literal, so the truncated form lands here. Still
            # require dim styling: a non-dim "esc to interrupt" is pasted text,
            # not the live footer.
            if _RE_INTERRUPT_HINT.search(tail_wide) and styled.find_styled_text("esc to", dim=True):
                return WorkerState.BUZZING
            return WorkerState.RESTING

        # Choice cursor: styled cursor character
        if self._has_styled_choice(styled):
            return WorkerState.WAITING

        # Accept edits (text-only, no style confirmation needed)
        if self.has_accept_edits_prompt(text):
            return WorkerState.WAITING

        # No styled signal matched — fall back to text-only
        return self.classify_output(command, text)

    def classify_styled_with_events(
        self, command: str, styled: StyledContent
    ) -> tuple[WorkerState, list[TerminalEvent]]:
        """Classify state and parse events from styled content."""
        state = self.classify_styled_output(command, styled)
        events = self.parse_events(styled.text)
        return state, events

    def _check_styled_buzzing(
        self, styled: StyledContent, tail_wide: str, text: str
    ) -> WorkerState | None:
        """Check dim-styled 'esc to interrupt' and return post-buzzing state.

        Returns None if no dim-styled indicator found (caller should continue).
        """
        if "esc to interrupt" not in tail_wide:
            return None
        if not styled.find_styled_text("esc to interrupt", dim=True):
            return None  # text matches but style doesn't — don't trust as BUZZING
        if _STYLE_DISCOVERY:
            self._log_style_discovery(styled, "esc to interrupt", "BUZZING (dim)")
        return self._classify_after_buzzing(text)

    def _classify_after_buzzing(self, text: str) -> WorkerState:
        """Determine state when dim 'esc to interrupt' confirms BUZZING.

        Mirrors the stale-buzzing logic in classify_output — if "esc to
        interrupt" is only in the wide tail (not the narrow tail), check
        for prompts that indicate the worker has actually transitioned.
        """
        tail_wide = self._get_tail(text, TAIL_WIDE)
        tail_narrow = self._get_tail(text, TAIL_NARROW)

        if "esc to interrupt" in tail_wide and "esc to interrupt" not in tail_narrow:
            tail_last = self._get_tail(text, TAIL_LAST_LINE)
            if _RE_PROMPT.search(tail_last) or "? for shortcuts" in tail_last:
                if (
                    self.has_choice_prompt(text)
                    or self.has_plan_prompt(text)
                    or self.has_empty_prompt(text)
                    or self.has_accept_edits_prompt(text)
                ):
                    return WorkerState.WAITING
                if _RE_SUBAGENT_ACTIVE.search(tail_wide):
                    return WorkerState.BUZZING
                return WorkerState.RESTING
            if (
                self.has_choice_prompt(text)
                or self.has_plan_prompt(text)
                or self.has_accept_edits_prompt(text)
            ):
                return WorkerState.WAITING

        return WorkerState.BUZZING

    def _has_styled_prompt(self, styled: StyledContent) -> bool:
        """Check for styled Claude Code prompt area (hint line with non-default fg).

        Claude Code's ``❯`` prompt character uses default fg, so we can't
        distinguish it from ``>`` in diff output by color alone.  Instead we
        look for the *hint line* that always accompanies the idle prompt —
        ``? for shortcuts`` or ``ctrl+t to hide`` — which is rendered in a
        non-default fg (typically gray/``999999``).
        """
        if not styled.rows:
            return False
        found = styled.find_styled_text(
            "? for shortcuts", fg="!default"
        ) or styled.find_styled_text("ctrl+t to hide", fg="!default")
        if found and _STYLE_DISCOVERY:
            self._log_style_discovery(styled, "? for shortcuts", "prompt-hint")
        return found

    def _has_styled_choice(self, styled: StyledContent) -> bool:
        """Check for a choice cursor (> N.) with non-default fg color."""
        if not styled.rows:
            return False
        # Check last 25 rows for styled choice cursor
        check_rows = styled.rows[-25:]
        has_other = False
        has_cursor = False
        for row_text, row_styles in check_rows:
            if _RE_OTHER_OPTION.search(row_text):
                has_other = True
            m = _RE_CURSOR_OPTION.search(row_text)
            if m:
                cursor_pos = m.start()
                # Skip whitespace to find the > or ❯
                while cursor_pos < len(row_text) and row_text[cursor_pos] == " ":
                    cursor_pos += 1
                if cursor_pos < len(row_styles) and row_styles[cursor_pos].fg != "default":
                    has_cursor = True
                    if _STYLE_DISCOVERY:
                        ch = row_text[cursor_pos]
                        s = row_styles[cursor_pos]
                        _style_log.info(
                            "STYLE_DISCOVERY [choice] %r at col %d: fg=%s bg=%s bold=%s dim=%s",
                            ch,
                            cursor_pos,
                            s.fg,
                            s.bg,
                            s.bold,
                            s.dim,
                        )
        return has_cursor and has_other

    @staticmethod
    def _log_style_discovery(styled: StyledContent, needle: str, context: str) -> None:
        """Log style values at detection points for discovery."""
        for row_text, row_styles in styled.rows:
            idx = row_text.find(needle)
            if idx < 0 or idx >= len(row_styles):
                continue
            s = row_styles[idx]
            _style_log.info(
                "STYLE_DISCOVERY [%s] %r at col %d: fg=%s bg=%s bold=%s dim=%s",
                context,
                needle,
                idx,
                s.fg,
                s.bg,
                s.bold,
                s.dim,
            )
            break
