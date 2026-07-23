"""Universal contract tests for all LLM providers.

Parametrized over claude/gemini/codex/opencode/generic — each test runs once per provider.
Covers shared base-class behavior and universal classify_output contracts.
"""

import pytest

from swarm.config import ProviderTuning
from swarm.providers import get_provider
from swarm.providers.base import LLMProvider
from swarm.providers.generic import GenericProvider
from swarm.providers.tuned import TunedProvider
from swarm.worker.worker import TokenUsage, WorkerState

_GENERIC = GenericProvider(name="test-generic", command=["test-cli"], display="Test Generic")
_TUNED_GENERIC = TunedProvider(
    GenericProvider(name="tuned-generic", command=["tuned-cli"], display="Tuned Generic"),
    ProviderTuning(idle_pattern=r"^tuned>"),
)


def _get_provider(name: str) -> LLMProvider:
    if name == "generic":
        return _GENERIC
    if name == "tuned-generic":
        return _TUNED_GENERIC
    return get_provider(name)


@pytest.fixture(params=["claude", "gemini", "codex", "opencode", "generic", "tuned-generic"])
def provider(request: pytest.FixtureRequest) -> LLMProvider:
    return _get_provider(request.param)


@pytest.fixture(params=["gemini", "codex", "opencode", "generic", "tuned-generic"])
def non_claude_provider(request: pytest.FixtureRequest) -> LLMProvider:
    return _get_provider(request.param)


@pytest.fixture(params=["gemini", "generic", "tuned-generic"])
def yn_provider(request: pytest.FixtureRequest) -> LLMProvider:
    """Non-Claude providers that use y\\r / n\\r approval keys.

    Excludes OpenCode (a/d) and Codex (Enter/Esc — its approval widget says
    "Press enter to confirm or esc to cancel").
    """
    return _get_provider(request.param)


# --- Universal classify_output contracts ---


class TestClassifyOutputUniversal:
    """Every provider must classify shell foreground as STUNG and unknown as BUZZING."""

    def test_shell_name_is_stung(self, provider: LLMProvider) -> None:
        for shell in ("bash", "zsh", "sh", "fish", "dash", "ksh", "csh", "tcsh"):
            assert provider.classify_output(shell, "$ ") == WorkerState.STUNG

    def test_shell_full_path_is_stung(self, provider: LLMProvider) -> None:
        assert provider.classify_output("/bin/bash", "$ ") == WorkerState.STUNG
        assert provider.classify_output("/usr/bin/zsh", "$ ") == WorkerState.STUNG

    def test_empty_content_defaults_to_buzzing(self, provider: LLMProvider) -> None:
        assert provider.classify_output(provider.name, "") == WorkerState.BUZZING

    def test_unknown_content_defaults_to_buzzing(self, provider: LLMProvider) -> None:
        assert (
            provider.classify_output(provider.name, "random stuff happening") == WorkerState.BUZZING
        )


# --- Universal base-class defaults (empty input) ---


class TestBaseDefaultsEmpty:
    """All providers return falsy/empty for empty content on optional methods."""

    def test_has_plan_prompt_empty(self, provider: LLMProvider) -> None:
        assert provider.has_plan_prompt("") is False

    def test_has_accept_edits_prompt_empty(self, provider: LLMProvider) -> None:
        assert provider.has_accept_edits_prompt("") is False

    def test_get_choice_summary_empty(self, provider: LLMProvider) -> None:
        assert provider.get_choice_summary("") == ""

    def test_is_user_question_empty(self, provider: LLMProvider) -> None:
        assert provider.is_user_question("") is False

    def test_has_choice_prompt_empty(self, provider: LLMProvider) -> None:
        assert provider.has_choice_prompt("") is False


# --- Non-Claude providers: plan/edits always False even with content ---


class TestNonClaudeDefaults:
    """Gemini and Codex never detect Claude-specific plan or accept-edits prompts."""

    def test_has_plan_prompt_with_content(self, non_claude_provider: LLMProvider) -> None:
        content = "Do you want me to proceed with this plan?\n> 1. Yes\n  2. No"
        assert non_claude_provider.has_plan_prompt(content) is False

    def test_has_accept_edits_prompt_with_content(self, non_claude_provider: LLMProvider) -> None:
        content = ">> accept edits on (shift+tab to cycle)"
        assert non_claude_provider.has_accept_edits_prompt(content) is False


# --- Universal approval_response contract ---


class TestApprovalResponseUniversal:
    """Most non-Claude providers use y/n defaults; OpenCode uses a/d."""

    def test_yn_approve(self, yn_provider: LLMProvider) -> None:
        assert yn_provider.approval_response(approve=True) == "y\r"

    def test_yn_reject(self, yn_provider: LLMProvider) -> None:
        assert yn_provider.approval_response(approve=False) == "n\r"

    def test_opencode_approve(self) -> None:
        p = get_provider("opencode")
        assert p.approval_response(approve=True) == "a"

    def test_opencode_reject(self) -> None:
        p = get_provider("opencode")
        assert p.approval_response(approve=False) == "d"

    def test_codex_approve(self) -> None:
        # Codex approval widget: "Press enter to confirm or esc to cancel".
        assert get_provider("codex").approval_response(approve=True) == "\r"

    def test_codex_reject(self) -> None:
        assert get_provider("codex").approval_response(approve=False) == "\x1b"


# --- Universal session_dir contract ---


class TestSessionDirUniversal:
    """Gemini and Codex return None; Claude returns a Path."""

    def test_non_claude_returns_none(self, non_claude_provider: LLMProvider) -> None:
        assert non_claude_provider.session_dir("/some/path") is None


# --- display_name ---


class TestDisplayName:
    """All providers have a non-empty display_name; specific values for each."""

    def test_display_name_non_empty(self, provider: LLMProvider) -> None:
        assert isinstance(provider.display_name, str)
        assert len(provider.display_name) > 0

    def test_claude_display_name(self) -> None:
        assert get_provider("claude").display_name == "Claude Code"

    def test_gemini_display_name(self) -> None:
        assert get_provider("gemini").display_name == "Gemini CLI"

    def test_codex_display_name(self) -> None:
        assert get_provider("codex").display_name == "Codex"


# --- Feature flags ---


class TestFeatureFlags:
    """Claude supports max_turns and json_output; others don't."""

    def test_claude_supports_max_turns(self) -> None:
        assert get_provider("claude").supports_max_turns is True

    def test_claude_supports_json_output(self) -> None:
        assert get_provider("claude").supports_json_output is True

    def test_non_claude_no_max_turns(self, non_claude_provider: LLMProvider) -> None:
        assert non_claude_provider.supports_max_turns is False

    def test_non_claude_no_json_output(self, non_claude_provider: LLMProvider) -> None:
        assert non_claude_provider.supports_json_output is False


# --- parse_usage ---


class TestParseUsage:
    """Claude extracts TokenUsage; non-Claude returns None."""

    def test_claude_parse_usage(self) -> None:
        provider = get_provider("claude")
        result = {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 200,
                "cache_creation_input_tokens": 300,
            },
            "total_cost_usd": 0.05,
        }
        usage = provider.parse_usage(result)
        assert isinstance(usage, TokenUsage)
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert usage.cache_read_tokens == 200
        assert usage.cache_creation_tokens == 300
        assert usage.cost_usd == pytest.approx(0.05)

    def test_claude_parse_usage_missing_fields(self) -> None:
        provider = get_provider("claude")
        result = {"usage": {"input_tokens": 42}}
        usage = provider.parse_usage(result)
        assert isinstance(usage, TokenUsage)
        assert usage.input_tokens == 42
        assert usage.output_tokens == 0

    def test_claude_parse_usage_bad_usage_type(self) -> None:
        provider = get_provider("claude")
        result = {"usage": "not a dict"}
        assert provider.parse_usage(result) is None

    def test_non_claude_parse_usage_returns_none(self, non_claude_provider: LLMProvider) -> None:
        assert non_claude_provider.parse_usage({"usage": {"input_tokens": 1}}) is None


# --- is_long_running_tool_active: Claude detects dynamic workflows; others don't ---


class TestLongRunningToolActive:
    """Dynamic workflows are a Claude Code feature. The base default returns
    False, which self-gates the behaviour for all other providers — even if
    Claude's footer string somehow appeared in their output."""

    _WORKFLOW = "> \n1 background dynamic workflow · /workflows\n"

    def test_claude_detects_workflow(self) -> None:
        assert get_provider("claude").is_long_running_tool_active(self._WORKFLOW) is True

    def test_non_claude_default_false(self, non_claude_provider: LLMProvider) -> None:
        assert non_claude_provider.is_long_running_tool_active(self._WORKFLOW) is False

    def test_claude_false_for_plain_idle(self) -> None:
        assert get_provider("claude").is_long_running_tool_active("Done.\n> \n") is False


class TestTunedDelegation:
    """A TunedProvider must delegate ALL behavior to its inner provider. Both
    is_long_running_tool_active and supports_native_goal default to False in the
    base, so a missing delegation silently disables Claude's dynamic-workflow
    detection (→ false nudges mid-workflow) and /goal seeding when tuning is on.
    """

    @staticmethod
    def _tuned_claude() -> TunedProvider:
        return TunedProvider(get_provider("claude"), ProviderTuning())

    def test_is_long_running_tool_active_delegates_to_claude(self) -> None:
        content = "1 background dynamic workflow"
        # Sanity: bare Claude detects it.
        assert get_provider("claude").is_long_running_tool_active(content) is True
        # The tuned wrapper must too (regressed before the delegation was added).
        assert self._tuned_claude().is_long_running_tool_active(content) is True

    def test_supports_native_goal_delegates_to_claude(self) -> None:
        assert get_provider("claude").supports_native_goal is True
        assert self._tuned_claude().supports_native_goal is True
