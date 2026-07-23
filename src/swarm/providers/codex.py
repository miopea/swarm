"""Codex CLI (OpenAI) provider — stub implementation based on research.

HIGH RISK: Codex uses Ratatui alternate screen buffer by default.
PTY text detection may not work — may need --no-alt-screen or JSONL monitoring.
Install: npm i -g @openai/codex
"""

from __future__ import annotations

import json
import re

from swarm.providers.base import (
    SHELL_STYLE_SAFE_PATTERNS,
    TAIL_NARROW,
    TAIL_WIDE,
    LLMProvider,
)
from swarm.worker.worker import WorkerState

# Codex uses Ratatui icons — these may not survive ANSI stripping
_RE_CODEX_IDLE = re.compile(r"[◇□]")
_RE_CODEX_BUSY = re.compile(r"[▶▷]")

# Codex has no ExitPlanMode tool. Its plan-mode convention (per the
# ~/.codex/AGENTS.md mapping installed by codex-team-config) is to present the
# plan in-conversation and wait for approval, surfaced via AskUserQuestion.
# Codex DOES have the swarm_* MCP tools (wired through ~/.codex/config.toml),
# so naming swarm_complete_task here is correct.
_CODEX_PLAN_PREAMBLE = """\
This task came from a user request (Jira ticket, email, or the operator dashboard). \
Plan BEFORE making any changes:

1. Read the task description below and any linked context.
2. Investigate read-only — open relevant files, search the codebase, check git \
history, verify assumptions against the real system if external (database, \
third-party API, CRM, etc.).
3. Present a concrete plan — what you'll change, which files, what tests you'll \
add, what the failure modes are, and what you've ruled out — and surface it with \
AskUserQuestion so the operator can approve it.
4. WAIT for explicit operator approval before editing files or running mutating \
commands.
5. After approval, execute the plan as agreed.

Do not edit files, run mutating shell commands, or call swarm_complete_task before \
approval. Worker-to-worker handoffs skip this gate; this preamble appears because \
the task came from a user channel.

--- TASK ---
"""


_log = __import__("logging").getLogger("swarm.providers.codex")


class CodexProvider(LLMProvider):
    """Codex CLI provider (stub — requires empirical alternate screen testing)."""

    def __init__(self) -> None:
        _log.warning("CodexProvider is a stub — alternate screen detection is unvalidated")

    @property
    def name(self) -> str:
        return "codex"

    @property
    def supports_native_goal(self) -> bool:
        # Codex CLI has a native /goal command (parity with Claude Code).
        return True

    def worker_command(self, resume: bool = True) -> list[str]:
        # --no-alt-screen is critical for PTY text detection
        return ["codex", "--no-alt-screen"]

    def headless_command(
        self,
        prompt: str,
        output_format: str = "text",
        max_turns: int | None = None,
        session_id: str | None = None,
    ) -> list[str]:
        args = ["codex", "exec", prompt]
        if output_format == "json":
            args.append("--json")
        # Codex doesn't support --resume or --max-turns
        return args

    def parse_headless_response(self, stdout: bytes) -> tuple[str, str | None]:
        """Parse Codex JSONL event stream, extract last agent_message."""
        text = stdout.decode(errors="replace").strip()
        last_message = ""
        for line in text.strip().splitlines():
            try:
                event = json.loads(line)
                if event.get("type") == "item.completed":
                    item = event.get("item", {})
                    if item.get("type") == "agent_message":
                        last_message = item.get("text", "")
            except json.JSONDecodeError:
                continue
        return last_message or text, None

    def classify_output(self, command: str, content: str) -> WorkerState:
        if self._is_shell_exited(command):
            return WorkerState.STUNG

        tail = self._get_tail(content, TAIL_WIDE)

        # Ratatui icons (may not survive ANSI stripping)
        if _RE_CODEX_BUSY.search(tail):
            return WorkerState.BUZZING

        if _RE_CODEX_IDLE.search(tail):
            return WorkerState.RESTING

        return WorkerState.BUZZING

    def plan_mode_preamble(self) -> str | None:
        return _CODEX_PLAN_PREAMBLE

    def has_active_turn_signal(self, content: str) -> bool:
        """Narrow-tail busy check — the `[▶▷]` run/think glyphs at the bottom
        of Codex's TUI. Lets the stuck-BUZZING net and nudge guards recognise a
        genuinely-working Codex worker instead of misjudging it via Claude's
        signals."""
        if not content:
            return False
        tail = "\n".join(content.strip().splitlines()[-TAIL_NARROW:])
        return bool(_RE_CODEX_BUSY.search(tail))

    def has_choice_prompt(self, content: str) -> bool:
        # Codex uses Ratatui widgets for approval — TBD how they render in raw PTY
        return False

    def is_user_question(self, content: str) -> bool:
        return False

    def get_choice_summary(self, content: str) -> str:
        return ""

    def safe_tool_patterns(self) -> re.Pattern[str]:
        return SHELL_STYLE_SAFE_PATTERNS

    def env_strip_prefixes(self) -> tuple[str, ...]:
        return ("OPENAI",)

    @property
    def display_name(self) -> str:
        return "Codex"
