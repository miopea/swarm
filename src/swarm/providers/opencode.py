"""OpenCode provider — source-verified detection patterns.

OpenCode is a full-screen Bubble Tea TUI coding agent that uses an alternate
screen buffer.  ANSI-stripped output contains the text literals matched below.
Install: go install github.com/opencode-ai/opencode@latest
"""

from __future__ import annotations

import re

from swarm.providers.base import (
    SHELL_STYLE_SAFE_PATTERNS,
    TAIL_NARROW,
    TAIL_WIDE,
    LLMProvider,
)
from swarm.worker.worker import WorkerState

# Busy: tool-specific status messages shown while working
_RE_OPENCODE_BUSY = re.compile(
    r"Thinking\.\.\.|Working\.\.\.|Generating\.\.\.|Loading\.\.\."
    r"|Preparing prompt\.\.\.|Building command\.\.\.|Preparing edit\.\.\."
    r"|Finding files\.\.\.|Searching content\.\.\.|Listing directory\.\.\."
    r"|Searching code\.\.\.|Reading file\.\.\.|Preparing write\.\.\."
    r"|Preparing patch\.\.\.|Writing fetch\.\.\."
    r"|Waiting for (?:tool )?response\.\.\."
    r"|Building tool call\.\.\.|Initializing LSP\.\.\.",
    re.IGNORECASE,
)

# Idle: prompt cursor or help text visible when ready for input
_RE_OPENCODE_IDLE = re.compile(r"[>❯]\s*$|press enter to send|ctrl\+\? help")

# Choice: permission dialog with Allow/Deny buttons
_RE_OPENCODE_CHOICE = re.compile(
    r"Permission Required|Allow \(a\)|Deny \(d\)|Allow for session",
    re.IGNORECASE,
)

# User question: structured question tool dialog
_RE_OPENCODE_QUESTION = re.compile(
    r"Agent is working, please wait",
    re.IGNORECASE,
)


class OpenCodeProvider(LLMProvider):
    """OpenCode provider with source-verified TUI detection patterns."""

    @property
    def name(self) -> str:
        return "opencode"

    def worker_command(self, resume: bool = True) -> list[str]:
        return ["opencode"]

    def headless_command(
        self,
        prompt: str,
        output_format: str = "text",
        max_turns: int | None = None,
        session_id: str | None = None,
    ) -> list[str]:
        cmd = ["opencode", "run"]
        if output_format == "json":
            cmd.extend(["-f", "json"])
        if session_id:
            cmd.extend(["-s", session_id])
        cmd.append(prompt)
        return cmd

    def parse_headless_response(self, stdout: bytes) -> tuple[str, str | None]:
        text = stdout.decode(errors="replace").strip()
        return text, None

    def classify_output(self, command: str, content: str) -> WorkerState:
        if self._is_shell_exited(command):
            return WorkerState.STUNG

        tail = self._get_tail(content, TAIL_WIDE)

        if _RE_OPENCODE_BUSY.search(tail):
            return WorkerState.BUZZING

        if _RE_OPENCODE_CHOICE.search(tail):
            return WorkerState.WAITING

        if _RE_OPENCODE_IDLE.search(tail):
            return WorkerState.RESTING

        return WorkerState.BUZZING

    def has_choice_prompt(self, content: str) -> bool:
        tail = self._get_tail(content, TAIL_WIDE)
        return bool(_RE_OPENCODE_CHOICE.search(tail))

    def is_user_question(self, content: str) -> bool:
        tail = self._get_tail(content, TAIL_WIDE)
        return bool(_RE_OPENCODE_QUESTION.search(tail))

    def approval_response(self, approve: bool = True) -> str:
        return "a" if approve else "d"

    def has_active_turn_signal(self, content: str) -> bool:
        """Narrow-tail busy check. Preserves OpenCode's mid-turn recognition
        now that the state tracker delegates this to the provider instead of
        applying Claude's regexes to every provider."""
        if not content:
            return False
        tail = "\n".join(content.strip().splitlines()[-TAIL_NARROW:])
        return bool(_RE_OPENCODE_BUSY.search(tail))

    def get_choice_summary(self, content: str) -> str:
        return ""

    def safe_tool_patterns(self) -> re.Pattern[str]:
        return SHELL_STYLE_SAFE_PATTERNS

    def env_strip_prefixes(self) -> tuple[str, ...]:
        return ("OPENCODE", "ANTHROPIC_API", "OPENAI_API")

    @property
    def display_name(self) -> str:
        return "OpenCode"
