"""Codex CLI (OpenAI) provider.

State detection is driven by the RAW PTY text Codex emits under
``codex --no-alt-screen`` — empirically captured 2026-07-23 against a live
worker (see the ``reference_codex_pty_patterns`` note). The Ratatui glyphs the
earlier stub relied on (``[◇□]`` / ``[▶▷]``) never appear in the PTY text, so
they are gone; detection now keys off Codex's actual text markers.

Install: npm i -g @openai/codex
"""

from __future__ import annotations

import json
import re

from swarm.providers.base import (
    SAFE_GIT_SUBCMDS,
    SAFE_SHELL_CMDS,
    TAIL_NARROW,
    TAIL_WIDE,
    LLMProvider,
)
from swarm.worker.worker import WorkerState

# --- Real Codex PTY signals (--no-alt-screen), empirically verified ---
# BUSY: the live turn timer, e.g. "• Working (4s • esc to interrupt)". The
# "esc to interrupt" phrase is shared with Claude Code.
_RE_CODEX_BUSY = re.compile(r"Working \(\d+|esc to interrupt")
# WAITING: the command-approval widget —
#   $ git status
#   › 1. Yes, proceed (y)
#     2. Yes, and don't ask again for commands that start with `git status` (p)
#     3. No, and tell Codex what to do differently (esc)
#     Press enter to confirm or esc to cancel
_RE_CODEX_APPROVAL = re.compile(
    r"Press enter to confirm or esc to cancel|^\s*1\.\s*Yes,\s*proceed",
    re.MULTILINE,
)
# IDLE: the composer footer "gpt-5.6-sol default · ~/projects/..." — a middot
# (U+00B7) followed by a path. Present during a turn too, so only meaningful
# once BUSY / WAITING have been excluded.
_RE_CODEX_FOOTER = re.compile(r"·\s+~?/")
# The "$ <command>" line the approval widget is gating (for the choice summary).
_RE_CODEX_APPROVAL_CMD = re.compile(r"^\s*\$\s*(.+?)\s*$", re.MULTILINE)
# Read-only commands shown as "$ ls" / "$ git status" in the approval widget, so
# the drone auto-approves them the way it does Claude's Read/Grep tools. Without
# this a Codex worker stalls on a "git status" approval a Claude worker sails
# through.
_RE_CODEX_SAFE = re.compile(
    rf"\$\s*(?:{SAFE_SHELL_CMDS})\b|\$\s*git\s+(?:{SAFE_GIT_SUBCMDS})\b",
    re.IGNORECASE,
)

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


class CodexProvider(LLMProvider):
    """Codex CLI provider."""

    @property
    def name(self) -> str:
        return "codex"

    @property
    def display_name(self) -> str:
        return "Codex"

    @property
    def supports_native_goal(self) -> bool:
        # Codex CLI has a native /goal command (parity with Claude Code).
        return True

    def worker_command(self, resume: bool = True) -> list[str]:
        # --no-alt-screen is critical for PTY text detection.
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
        # Codex doesn't support --resume or --max-turns.
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

        # WAITING before BUSY: while an approval widget is up the turn is paused
        # (no "Working" timer), and WAITING must take priority so the drone can
        # act on it.
        if _RE_CODEX_APPROVAL.search(tail):
            return WorkerState.WAITING
        if _RE_CODEX_BUSY.search(tail):
            return WorkerState.BUZZING
        # Footer present with neither a live turn nor an approval → idle & ready.
        if _RE_CODEX_FOOTER.search(tail):
            return WorkerState.RESTING
        # Unknown → assume active (conservative, matches Claude's fallback).
        return WorkerState.BUZZING

    def has_choice_prompt(self, content: str) -> bool:
        return bool(_RE_CODEX_APPROVAL.search(self._get_tail(content, TAIL_WIDE)))

    def get_choice_summary(self, content: str) -> str:
        tail = self._get_tail(content, TAIL_WIDE)
        if not _RE_CODEX_APPROVAL.search(tail):
            return ""
        m = _RE_CODEX_APPROVAL_CMD.search(tail)
        return m.group(1).strip() if m else ""

    def is_user_question(self, content: str) -> bool:
        # Codex's interactive prompt is the command-approval widget (handled by
        # has_choice_prompt). No distinct free-text question prompt is captured.
        return False

    def has_idle_prompt(self, content: str) -> bool:
        tail = self._get_tail(content, TAIL_WIDE)
        if _RE_CODEX_BUSY.search(tail) or _RE_CODEX_APPROVAL.search(tail):
            return False
        return bool(_RE_CODEX_FOOTER.search(tail))

    def approval_response(self, approve: bool = True) -> str:
        # Codex approval widget: "Press enter to confirm or esc to cancel".
        return "\r" if approve else "\x1b"

    def safe_tool_patterns(self) -> re.Pattern[str]:
        return _RE_CODEX_SAFE

    def env_strip_prefixes(self) -> tuple[str, ...]:
        return ("OPENAI",)

    def plan_mode_preamble(self) -> str | None:
        return _CODEX_PLAN_PREAMBLE

    def has_active_turn_signal(self, content: str) -> bool:
        """Narrow-tail busy check — the "Working (Ns · esc to interrupt)" line at
        the bottom of Codex's TUI. Lets the stuck-BUZZING net and nudge guards
        recognise a genuinely-working Codex worker."""
        if not content:
            return False
        tail = "\n".join(content.strip().splitlines()[-TAIL_NARROW:])
        return bool(_RE_CODEX_BUSY.search(tail))
