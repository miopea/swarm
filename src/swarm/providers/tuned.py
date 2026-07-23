"""TunedProvider — wraps any LLMProvider with user-configurable tuning overrides."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from swarm.providers.base import LLMProvider
from swarm.providers.events import TerminalEvent
from swarm.worker.worker import WorkerState

if TYPE_CHECKING:
    from pathlib import Path

    from swarm.config import ProviderTuning
    from swarm.providers.styled import StyledContent
    from swarm.worker.worker import TokenUsage


class TunedProvider(LLMProvider):
    """Wraps an inner LLMProvider, applying tuning patterns first.

    Tuning patterns are checked before delegating to the inner provider.
    If a tuning pattern matches, that result wins; otherwise the inner
    provider's behavior is used unchanged.
    """

    def __init__(self, inner: LLMProvider, tuning: ProviderTuning) -> None:
        self._inner = inner
        self._tuning = tuning

    # --- Identity (delegate) ---

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def display_name(self) -> str:
        return self._inner.display_name

    # --- Commands (delegate) ---

    def worker_command(self, resume: bool = True) -> list[str]:
        return self._inner.worker_command(resume)

    def headless_command(
        self,
        prompt: str,
        output_format: str = "text",
        max_turns: int | None = None,
        session_id: str | None = None,
    ) -> list[str]:
        return self._inner.headless_command(prompt, output_format, max_turns, session_id)

    def parse_headless_response(self, stdout: bytes) -> tuple[str, str | None]:
        return self._inner.parse_headless_response(stdout)

    # --- State detection (tuning-first) ---

    def classify_output(self, command: str, content: str) -> WorkerState:
        tail = self._get_tail(
            content,
            self._tuning.tail_lines if self._tuning.tail_lines else 30,
        )
        t = self._tuning
        if t._busy_re and t._busy_re.search(tail):
            return WorkerState.BUZZING
        if t._choice_re and t._choice_re.search(tail):
            return WorkerState.WAITING
        if t._idle_re and t._idle_re.search(tail):
            return WorkerState.RESTING
        return self._inner.classify_output(command, content)

    def has_choice_prompt(self, content: str) -> bool:
        t = self._tuning
        if t._choice_re and t._choice_re.search(content):
            return True
        return self._inner.has_choice_prompt(content)

    def is_user_question(self, content: str) -> bool:
        t = self._tuning
        if t._user_question_re and t._user_question_re.search(content):
            return True
        return self._inner.is_user_question(content)

    def get_choice_summary(self, content: str) -> str:
        return self._inner.get_choice_summary(content)

    def safe_tool_patterns(self) -> re.Pattern[str]:
        t = self._tuning
        if t._safe_re:
            return t._safe_re
        return self._inner.safe_tool_patterns()

    # --- Environment (combined) ---

    def env_strip_prefixes(self) -> tuple[str, ...]:
        inner = self._inner.env_strip_prefixes()
        extra = tuple(self._tuning.env_strip_prefixes)
        return inner + extra if extra else inner

    # --- Approval (tuning overrides) ---

    def approval_response(self, approve: bool = True) -> str:
        t = self._tuning
        if approve and t.approval_key:
            return t.approval_key
        if not approve and t.rejection_key:
            return t.rejection_key
        return self._inner.approval_response(approve)

    # --- Delegated optional methods ---

    def session_dir(self, worker_path: str) -> Path | None:
        return self._inner.session_dir(worker_path)

    def has_plan_prompt(self, content: str) -> bool:
        return self._inner.has_plan_prompt(content)

    def has_accept_edits_prompt(self, content: str) -> bool:
        return self._inner.has_accept_edits_prompt(content)

    def has_idle_prompt(self, content: str) -> bool:
        return self._inner.has_idle_prompt(content)

    def has_empty_prompt(self, content: str) -> bool:
        return self._inner.has_empty_prompt(content)

    def is_long_running_tool_active(self, content: str) -> bool:
        # Must delegate: the base default is False, so without this a tuned
        # Claude loses dynamic-workflow detection and gets false nudges
        # mid-workflow.
        return self._inner.is_long_running_tool_active(content)

    def plan_mode_preamble(self) -> str | None:
        # Delegate: base default is generic, so a tuned Claude would otherwise
        # lose its ExitPlanMode-specific preamble.
        return self._inner.plan_mode_preamble()

    def has_active_turn_signal(self, content: str) -> bool:
        # Delegate: base default is False, so a tuned provider would otherwise
        # lose its mid-turn signal (stuck-BUZZING net would misjudge it).
        return self._inner.has_active_turn_signal(content)

    @property
    def supports_slash_commands(self) -> bool:
        return self._inner.supports_slash_commands

    @property
    def supports_hooks(self) -> bool:
        return self._inner.supports_hooks

    @property
    def supports_native_goal(self) -> bool:
        # Delegate: base default is False, so a tuned Claude would otherwise
        # silently lose /goal seeding.
        return self._inner.supports_native_goal

    @property
    def supports_native_loop(self) -> bool:
        # Delegate: a tuned Claude would otherwise silently lose the
        # /loop-coexistence guard.
        return self._inner.supports_native_loop

    @property
    def supports_resume(self) -> bool:
        return self._inner.supports_resume

    @property
    def supports_max_turns(self) -> bool:
        return self._inner.supports_max_turns

    @property
    def supports_json_output(self) -> bool:
        return self._inner.supports_json_output

    def parse_usage(self, result: dict[str, Any]) -> TokenUsage | None:
        return self._inner.parse_usage(result)

    def parse_events(self, content: str) -> list[TerminalEvent]:
        return self._inner.parse_events(content)

    def classify_with_events(
        self, command: str, content: str
    ) -> tuple[WorkerState, list[TerminalEvent]]:
        state = self.classify_output(command, content)
        events = self.parse_events(content)
        return state, events

    def classify_styled_output(self, command: str, styled: StyledContent) -> WorkerState:
        return self.classify_output(command, styled.text)

    def classify_styled_with_events(
        self, command: str, styled: StyledContent
    ) -> tuple[WorkerState, list[TerminalEvent]]:
        return self.classify_with_events(command, styled.text)
