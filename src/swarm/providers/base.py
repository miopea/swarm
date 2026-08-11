"""Abstract base class for LLM CLI providers."""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from swarm.providers.events import EventType, TerminalEvent
from swarm.providers.styled import StyledContent
from swarm.worker.worker import TokenUsage, WorkerState

_SHELLS = frozenset(("bash", "zsh", "sh", "fish", "dash", "ksh", "csh", "tcsh"))

# Shared safe command lists — referenced by each provider's safe_tool_patterns.
# Read-only tools only (a drone auto-approves these). Excludes dual-use tools
# like sed/awk that can write in-place (`sed -i`), so those still escalate.
SAFE_SHELL_CMDS = (
    r"ls|cat|head|tail|find|wc|stat|file|which|pwd|echo|date|rg|grep|nl|sort|uniq|cut|tr"
)
SAFE_GIT_SUBCMDS = r"status|log|diff|show|branch|remote|tag"

# Shared safe-tool regex for the CLI-style providers (codex, opencode) that
# expose ``shell()`` / ``file_read()`` / ``file_search()`` tool calls. Kept
# here so the two providers can't drift (they used identical copies).
SHELL_STYLE_SAFE_PATTERNS = re.compile(
    rf"shell\(.*({SAFE_SHELL_CMDS})\b"
    rf"|shell\(.*git\s+({SAFE_GIT_SUBCMDS})\b"
    r"|file_read\("
    r"|file_search\(",
    re.IGNORECASE,
)

# Canonical tail-window sizes for _get_tail() — prevents magic-number drift.
TAIL_LAST_LINE = 1  # Single line: empty prompt check
TAIL_NARROW = 5  # Narrow: accept-edits, idle prompt, hints
TAIL_MEDIUM = 15  # Medium: user rules, user question detection
TAIL_WIDE = 30  # Wide: safe patterns, choice menus, plan markers

# Provider-neutral plan-mode preamble (the default returned by
# ``LLMProvider.plan_mode_preamble``). Deliberately names NO provider-specific
# mechanism — no ExitPlanMode tool, no slash commands, no MCP tool names — so a
# provider without a bespoke plan-mode UX still gets coherent instructions.
# Claude / Codex override with wording that names their real mechanism.
_GENERIC_PLAN_PREAMBLE = """\
This task came from a user request (Jira ticket, email, or the operator dashboard). \
Plan BEFORE making any changes:

1. Read the task description below and any linked context.
2. Investigate read-only — open relevant files, search the codebase, check git \
history, verify assumptions against the real system if external (database, \
third-party API, CRM, etc.).
3. Present a concrete plan: what you'll change, which files, what tests you'll \
add, what the failure modes are, and what you've ruled out.
4. WAIT for the operator to approve the plan before making changes.
5. After approval, execute the plan as agreed.

Do not edit files or run mutating shell commands before approval. Worker-to-worker \
handoffs skip this gate; this preamble appears because the task came from a user channel.

--- TASK ---
"""


class LLMProvider(ABC):
    """Abstract base for LLM CLI provider implementations.

    Each provider encapsulates all CLI-specific behavior: startup commands,
    state detection patterns, headless invocation, approval handling, etc.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this provider (e.g. 'claude', 'gemini')."""

    @abstractmethod
    def worker_command(self, resume: bool = True) -> list[str]:
        """Command to launch an interactive worker session."""

    @abstractmethod
    def headless_command(
        self,
        prompt: str,
        output_format: str = "text",
        max_turns: int | None = None,
        session_id: str | None = None,
    ) -> list[str]:
        """Command for non-interactive headless prompt."""

    @abstractmethod
    def parse_headless_response(self, stdout: bytes) -> tuple[str, str | None]:
        """Parse headless output -> (text_result, session_id_or_none)."""

    @abstractmethod
    def classify_output(self, command: str, content: str) -> WorkerState:
        """Classify worker state from foreground command name and PTY output."""

    @abstractmethod
    def has_choice_prompt(self, content: str) -> bool:
        """Detect approval/choice prompts that drones can auto-handle."""

    @abstractmethod
    def is_user_question(self, content: str) -> bool:
        """Detect prompts requiring human input (never auto-approve)."""

    @abstractmethod
    def get_choice_summary(self, content: str) -> str:
        """Extract a short summary of the choice/approval prompt."""

    @abstractmethod
    def safe_tool_patterns(self) -> re.Pattern[str]:
        """Regex for tool invocations safe to auto-approve."""

    @abstractmethod
    def env_strip_prefixes(self) -> tuple[str, ...]:
        """Env var prefixes to strip when running headless."""

    def approval_response(self, approve: bool = True) -> str:
        """What to send to the PTY to approve/reject.

        Default: y/n (used by Gemini, Codex). Claude overrides with Enter/Esc.
        """
        return "y\r" if approve else "n\r"

    def quit_command(self) -> str:
        """Slash command that makes this CLI exit gracefully, or "" if none.

        Used by the operator kill path to let an agent shut itself down — and
        save its session — before the process is signalled.

        Default is "" (unknown), NOT a guess. A wrong quit string is worse
        than none: it gets typed into the agent's prompt as literal text and
        sits there, so the kill both fails to be graceful and leaves junk in
        the transcript. Providers whose quit command is actually known
        override this; the rest fall through to closing the shell and the
        signal backstop, which is what happens today anyway.
        """
        return ""

    def session_dir(self, worker_path: str) -> Path | None:
        """Path to session/usage data for this worker, or None if unsupported."""
        return None

    # --- Shared helpers for subclasses ---

    def _is_shell_exited(self, command: str) -> bool:
        """Check if the foreground command is a shell (worker has exited)."""
        return os.path.basename(command) in _SHELLS

    def _get_tail(self, content: str, lines: int = 30) -> str:
        """Extract the last N lines from content for pattern matching."""
        all_lines = content.strip().splitlines()
        return "\n".join(all_lines[-lines:])

    # --- Optional methods with sensible defaults ---

    def has_plan_prompt(self, content: str) -> bool:
        """Detect plan approval prompts. Default: False (only Claude has this)."""
        return False

    def has_accept_edits_prompt(self, content: str) -> bool:
        """Detect edit acceptance prompts. Default: False (only Claude has this)."""
        return False

    def has_idle_prompt(self, content: str) -> bool:
        """Check if output shows a normal idle input prompt."""
        return False

    def is_long_running_tool_active(self, content: str) -> bool:
        """Whether the PTY tail shows an in-flight long-running tool.

        Covers background work the worker can't be interrupted for or
        assigned over: background shells/monitors, active subagents, and
        in-flight dynamic workflows. Used to hold a worker in BUZZING
        (not idle) and to suppress prolonged-BUZZING oversight while such
        work runs. Default ``False`` — only providers whose CLI renders
        these indicators (Claude Code) override this, which self-gates
        the behaviour for other providers.
        """
        return False

    def has_empty_prompt(self, content: str) -> bool:
        """Check if output shows an empty input prompt ready for continuation."""
        return False

    def has_open_selection_prompt(self, content: str) -> bool:
        """Whether the screen is showing a prompt that a stray Enter would ANSWER.

        This is the guard used by ``WorkerService.send_to_worker(automated=True)``
        to refuse writing into a question the operator is being asked. It is
        deliberately SEPARATE from ``is_user_question`` and from
        ``has_choice_prompt``, for two reasons:

        NO TAIL WINDOW. ``is_user_question`` matches inside ``TAIL_MEDIUM`` (15
        lines) and ``has_choice_prompt`` inside ``TAIL_WIDE`` (30). An
        AskUserQuestion set with three questions of four options each renders
        TALLER THAN BOTH, so the marker that says "a selection is pending" scrolls
        out of the window while the prompt is still open and still answerable.
        A guard that is blind to exactly the biggest prompts is worse than none,
        because it reports safe. So this scans the WHOLE captured screen.

        STRUCTURAL, NOT LEXICAL. It keys on the shape every selection prompt has —
        a cursored option line (``> 1.`` / ``❯ 2.``) with at least one sibling
        option — rather than on wording like "chat about this", which differs per
        provider and per prompt type and silently stops matching when the CLI
        rewords its UI.

        Default is a generic implementation rather than an abstractmethod so that
        adding this guard cannot break a provider that has not been updated; a
        provider whose prompts do not render this way should override it.
        """
        from swarm.pty.prompt_guard import has_open_selection_prompt

        return has_open_selection_prompt(content)

    def plan_mode_preamble(self) -> str | None:
        """Preamble prepended to user-request tasks that need a plan-approval gate.

        Returns provider-neutral plan-then-approve wording by default. Providers
        whose CLI has a specific plan-mode mechanism (Claude's ``ExitPlanMode``
        tool) override this so the injected text names the right mechanism. A
        provider with no plan concept may return ``None`` to skip the preamble.
        """
        return _GENERIC_PLAN_PREAMBLE

    def has_active_turn_signal(self, content: str) -> bool:
        """True when the narrow PTY tail proves the worker is mid-turn.

        Used by the idle-watcher / task-lifecycle nudge guards and the stuck-
        BUZZING safety net to avoid poking a worker that is actually working.
        Default ``False`` — a provider with no cheap live-turn signal opts out
        (the safety net may then flip it to RESTING after the threshold).
        """
        return False

    @property
    def supports_slash_commands(self) -> bool:
        """Whether the CLI supports slash commands (/fix-and-ship, etc.)."""
        return False

    @property
    def supports_hooks(self) -> bool:
        """Whether the CLI supports installable hooks."""
        return False

    @property
    def supports_native_goal(self) -> bool:
        """Whether the CLI has a native session-scoped ``/goal`` command.

        When True, Swarm seeds a task's acceptance criteria as a native
        ``/goal`` at dispatch and lets the provider's own evaluator run
        the keep-working loop. False = clean no-op (Swarm injects
        nothing; the generic idle-watcher remains the only safety net).
        """
        return False

    @property
    def supports_native_loop(self) -> bool:
        """Whether the CLI has a native cadence-based ``/loop`` command.

        When True, Swarm watches for the ScheduleWakeup tool result a
        parked loop emits and leaves the worker undisturbed until its
        next tick (the loop-coexistence guard). False = clean no-op;
        non-Claude providers never emit the signal, so the guard is a
        pure no-op for them by construction.
        """
        return False

    @property
    def supports_resume(self) -> bool:
        """Whether the headless CLI supports --resume for session continuity."""
        return False

    @property
    def display_name(self) -> str:
        """Human-readable name for prompts (e.g. 'Claude Code', 'Gemini CLI')."""
        return self.name.title()

    @property
    def supports_max_turns(self) -> bool:
        """Whether the headless CLI supports --max-turns."""
        return False

    @property
    def supports_json_output(self) -> bool:
        """Whether the headless CLI supports --output-format json."""
        return False

    def parse_usage(self, result: dict[str, Any]) -> TokenUsage | None:
        """Extract token usage from a headless response. None if unsupported."""
        return None

    def parse_events(self, content: str) -> list[TerminalEvent]:
        """Parse structured events from terminal output.

        Default returns a single UNKNOWN event wrapping the content.
        Providers override to extract typed events (tool calls, prompts, etc.).
        """
        return [TerminalEvent(EventType.UNKNOWN, content)]

    def classify_with_events(
        self, command: str, content: str
    ) -> tuple[WorkerState, list[TerminalEvent]]:
        """Classify worker state and parse events in one pass.

        Default calls classify_output() and parse_events() independently.
        Providers can override to avoid double-parsing.
        """
        state = self.classify_output(command, content)
        events = self.parse_events(content)
        return state, events

    # --- Style-aware classification (backward-compatible defaults) ---

    def classify_styled_output(self, command: str, styled: StyledContent) -> WorkerState:
        """Classify worker state using styled terminal content.

        Default falls back to text-only ``classify_output()``.
        Providers override to use style data as a secondary signal.
        """
        return self.classify_output(command, styled.text)

    def classify_styled_with_events(
        self, command: str, styled: StyledContent
    ) -> tuple[WorkerState, list[TerminalEvent]]:
        """Classify state and parse events from styled content.

        Default falls back to ``classify_with_events()`` using text only.
        """
        return self.classify_with_events(command, styled.text)
