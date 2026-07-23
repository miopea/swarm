"""Tests for provider-specific logic — Claude, Codex, Gemini, OpenCode."""

from __future__ import annotations

import json

from swarm.providers import VALID_PROVIDERS, get_provider, list_providers
from swarm.providers.claude import ClaudeProvider
from swarm.providers.codex import CodexProvider
from swarm.providers.gemini import GeminiProvider
from swarm.providers.opencode import OpenCodeProvider
from swarm.worker.worker import WorkerState


class TestClaudeClassifyOutput:
    """Edge cases for Claude state detection."""

    def setup_method(self):
        self.p = ClaudeProvider()

    def test_esc_to_interrupt_buzzing(self):
        content = "Working...\nesc to interrupt\n" * 5
        assert self.p.classify_output("claude", content) == WorkerState.BUZZING

    def test_bare_prompt_is_waiting(self):
        # An empty prompt ("> " with nothing typed) is WAITING (empty prompt)
        content = "Done.\n> "
        assert self.p.classify_output("claude", content) == WorkerState.WAITING

    def test_prompt_with_text_resting(self):
        # Prompt with visible text means user is at the input prompt
        content = "Done.\n> some typed text"
        assert self.p.classify_output("claude", content) == WorkerState.RESTING

    def test_choice_prompt_waiting(self):
        content = "\n".join(
            [
                "Which option?",
                "> 1. Option A",
                "  2. Option B",
                "  3. Option C",
            ]
        )
        assert self.p.classify_output("claude", content) == WorkerState.WAITING

    def test_shell_exited_stung(self):
        assert self.p.classify_output("bash", "anything") == WorkerState.STUNG

    def test_empty_content_buzzing(self):
        assert self.p.classify_output("claude", "") == WorkerState.BUZZING

    def test_stale_esc_with_empty_prompt_waiting(self):
        # "esc to interrupt" in wide tail but empty prompt in narrow tail → WAITING
        lines = ["esc to interrupt"] + ["other line"] * 10 + ["> "]
        content = "\n".join(lines)
        assert self.p.classify_output("claude", content) == WorkerState.WAITING

    def test_accept_edits_waiting(self):
        content = "Some output\n>> accept edits on 3 files"
        assert self.p.classify_output("claude", content) == WorkerState.WAITING


class TestClaudeHeadlessCommand:
    def setup_method(self):
        self.p = ClaudeProvider()

    def test_basic_command(self):
        args = self.p.headless_command("hello")
        assert args == ["claude", "-p", "hello", "--output-format", "text"]

    def test_json_format(self):
        args = self.p.headless_command("hello", output_format="json")
        assert "--output-format" in args
        assert "json" in args

    def test_with_session(self):
        args = self.p.headless_command("hello", session_id="abc123")
        assert "--resume" in args
        assert "abc123" in args

    def test_with_max_turns(self):
        args = self.p.headless_command("hello", max_turns=5)
        assert "--max-turns" in args
        assert "5" in args

    def test_all_options(self):
        args = self.p.headless_command(
            "hello", output_format="json", session_id="sess", max_turns=10
        )
        assert "--output-format" in args
        assert "--resume" in args
        assert "--max-turns" in args


class TestClaudeParseResponse:
    def setup_method(self):
        self.p = ClaudeProvider()

    def test_valid_json_envelope(self):
        payload = json.dumps({"type": "result", "result": "hello", "session_id": "abc"})
        text, sid = self.p.parse_headless_response(payload.encode())
        assert text == "hello"
        assert sid == "abc"

    def test_invalid_json_fallback(self):
        text, sid = self.p.parse_headless_response(b"not json")
        assert text == "not json"
        assert sid is None

    def test_empty_result(self):
        payload = json.dumps({"type": "result"})
        text, sid = self.p.parse_headless_response(payload.encode())
        assert text == ""
        assert sid is None


class TestClaudePromptDetection:
    def setup_method(self):
        self.p = ClaudeProvider()

    def test_has_choice_prompt(self):
        content = "\n".join(
            [
                "Select an option:",
                "> 1. First",
                "  2. Second",
            ]
        )
        assert self.p.has_choice_prompt(content) is True

    def test_no_choice_prompt(self):
        assert self.p.has_choice_prompt("just some text\n> ") is False

    def test_has_plan_prompt(self):
        content = "\n".join(
            [
                "Plan saved to file",
                "Proceed with this plan?",
                "> 1. Yes",
                "  2. No",
            ]
        )
        assert self.p.has_plan_prompt(content) is True

    def test_has_plan_prompt_would_you_like_to_proceed(self):
        content = "\n".join(
            [
                "Would you like to proceed?",
                "> 1. Yes",
                "  2. No",
            ]
        )
        assert self.p.has_plan_prompt(content) is True

    def test_has_plan_prompt_how_would_you_not_plan(self):
        """'How would you like to proceed?' is a user question, not a plan."""
        content = "\n".join(
            [
                "How would you like to proceed?",
                "> 1. Fix both issues",
                "  2. File issues for later",
            ]
        )
        assert self.p.has_plan_prompt(content) is False

    def test_has_plan_prompt_has_written_plan(self):
        content = "\n".join(
            [
                "Claude has written up a plan for the changes.",
                "> 1. Approve plan",
                "  2. Reject",
            ]
        )
        assert self.p.has_plan_prompt(content) is True

    def test_has_accept_edits(self):
        content = "Some output\n>> accept edits on 5 files"
        assert self.p.has_accept_edits_prompt(content) is True

    def test_no_accept_edits(self):
        assert self.p.has_accept_edits_prompt("normal output\n> ") is False

    def test_has_idle_prompt(self):
        assert self.p.has_idle_prompt("output\n> ") is True

    def test_has_idle_prompt_hints(self):
        assert self.p.has_idle_prompt("? for shortcuts") is True

    def test_has_empty_prompt(self):
        assert self.p.has_empty_prompt("> ") is True

    def test_no_empty_prompt_with_text(self):
        assert self.p.has_empty_prompt("> hello") is False

    def test_is_user_question(self):
        assert self.p.is_user_question("Chat about this and decide") is True
        assert self.p.is_user_question("normal output") is False

    def test_get_choice_summary(self):
        content = "\n".join(
            [
                "Which file?",
                "> 1. src/main.py",
                "  2. src/util.py",
            ]
        )
        summary = self.p.get_choice_summary(content)
        assert "main.py" in summary

    def test_get_choice_summary_empty(self):
        assert self.p.get_choice_summary("no choices here") == ""


class TestClaudeWorkerCommand:
    def setup_method(self):
        self.p = ClaudeProvider()

    def test_resume_mode(self):
        assert self.p.worker_command(resume=True) == ["claude", "--continue"]

    def test_fresh_mode(self):
        assert self.p.worker_command(resume=False) == ["claude"]


class TestClaudeSafePatterns:
    def setup_method(self):
        self.p = ClaudeProvider()

    def test_safe_commands_match(self):
        pat = self.p.safe_tool_patterns()
        assert pat.search("Bash(ls -la)")
        assert pat.search("Bash(git status)")
        assert pat.search("Bash(uv run pytest tests/)")
        assert pat.search("Read(/path/to/file)")
        assert pat.search("Grep(pattern)")

    def test_unsafe_commands_dont_match(self):
        pat = self.p.safe_tool_patterns()
        assert not pat.search("Bash(rm -rf /)")
        assert not pat.search("Bash(curl evil.com)")


class TestClaudeSessionDir:
    def setup_method(self):
        self.p = ClaudeProvider()

    def test_encodes_path(self):
        result = self.p.session_dir("/home/user/project")
        assert result is not None
        assert "projects" in str(result)
        # Slashes should be replaced
        assert "/" not in result.name or result.name.startswith(".")


class TestCodexProvider:
    def setup_method(self):
        self.p = CodexProvider()

    def test_worker_command(self):
        cmd = self.p.worker_command()
        assert "codex" in cmd

    def test_headless_command(self):
        cmd = self.p.headless_command("test prompt")
        assert "codex" in cmd
        assert "test prompt" in cmd

    def test_classify_idle(self):
        # Idle = composer footer, no live-turn timer.
        state = self.p.classify_output("codex", "done\n\n  gpt-5.6-sol default · ~/proj\n")
        assert state == WorkerState.RESTING

    def test_classify_busy(self):
        state = self.p.classify_output("codex", "• Working (2s • esc to interrupt)\n")
        assert state == WorkerState.BUZZING

    def test_classify_waiting(self):
        content = "$ ls\n\n  Press enter to confirm or esc to cancel\n"
        assert self.p.classify_output("codex", content) == WorkerState.WAITING

    def test_display_name(self):
        assert self.p.display_name == "Codex"


class TestGeminiProvider:
    def setup_method(self):
        self.p = GeminiProvider()

    def test_worker_command(self):
        cmd = self.p.worker_command()
        assert "gemini" in cmd

    def test_classify_esc_buzzing(self):
        state = self.p.classify_output("gemini", "esc to cancel")
        assert state == WorkerState.BUZZING

    def test_classify_prompt_resting(self):
        state = self.p.classify_output("gemini", "gemini> ")
        assert state == WorkerState.RESTING

    def test_display_name(self):
        assert self.p.display_name == "Gemini CLI"

    def test_headless_command_with_resume(self):
        cmd = self.p.headless_command("test", session_id="sess123")
        assert "--resume" in cmd
        assert "sess123" in cmd


class TestOpenCodeProvider:
    def setup_method(self):
        self.p = OpenCodeProvider()

    def test_worker_command(self):
        cmd = self.p.worker_command()
        assert "opencode" in cmd

    def test_headless_command(self):
        cmd = self.p.headless_command("test prompt")
        assert cmd == ["opencode", "run", "test prompt"]

    def test_headless_command_json(self):
        cmd = self.p.headless_command("test", output_format="json")
        assert cmd == ["opencode", "run", "-f", "json", "test"]

    def test_headless_command_session(self):
        cmd = self.p.headless_command("test", session_id="sess1")
        assert cmd == ["opencode", "run", "-s", "sess1", "test"]

    def test_headless_command_all_options(self):
        cmd = self.p.headless_command("test", output_format="json", session_id="s1")
        assert "-f" in cmd
        assert "-s" in cmd
        assert cmd[-1] == "test"

    def test_classify_idle(self):
        state = self.p.classify_output("opencode", "ready> ")
        assert state == WorkerState.RESTING

    def test_classify_idle_press_enter(self):
        state = self.p.classify_output("opencode", "press enter to send")
        assert state == WorkerState.RESTING

    def test_classify_idle_help(self):
        state = self.p.classify_output("opencode", "ctrl+? help")
        assert state == WorkerState.RESTING

    def test_classify_busy_thinking(self):
        state = self.p.classify_output("opencode", "Thinking...")
        assert state == WorkerState.BUZZING

    def test_classify_busy_variants(self):
        busy_strings = [
            "Working...",
            "Generating...",
            "Loading...",
            "Building command...",
            "Finding files...",
            "Searching content...",
            "Listing directory...",
            "Searching code...",
            "Reading file...",
            "Preparing write...",
            "Preparing patch...",
            "Waiting for response...",
            "Waiting for tool response...",
            "Building tool call...",
            "Initializing LSP...",
        ]
        for s in busy_strings:
            state = self.p.classify_output("opencode", s)
            assert state == WorkerState.BUZZING, f"Expected BUZZING for {s!r}"

    def test_classify_choice(self):
        state = self.p.classify_output("opencode", "Permission Required\nAllow (a)")
        assert state == WorkerState.WAITING

    def test_has_choice_prompt(self):
        assert self.p.has_choice_prompt("Permission Required") is True
        assert self.p.has_choice_prompt("Allow (a)  Deny (d)") is True
        assert self.p.has_choice_prompt("Allow for session") is True

    def test_has_choice_prompt_negative(self):
        assert self.p.has_choice_prompt("normal output\n> ") is False

    def test_is_user_question(self):
        assert self.p.is_user_question("Agent is working, please wait") is True
        assert self.p.is_user_question("normal output") is False

    def test_approval_keys(self):
        assert self.p.approval_response(True) == "a"
        assert self.p.approval_response(False) == "d"

    def test_env_prefixes(self):
        prefixes = self.p.env_strip_prefixes()
        assert "OPENCODE" in prefixes
        assert "ANTHROPIC_API" in prefixes
        assert "OPENAI_API" in prefixes

    def test_display_name(self):
        assert self.p.display_name == "OpenCode"

    def test_name(self):
        assert self.p.name == "opencode"

    def test_shell_exited_stung(self):
        assert self.p.classify_output("bash", "anything") == WorkerState.STUNG


class TestProviderRegistry:
    """Tests for VALID_PROVIDERS and list_providers() registry helpers."""

    def test_valid_providers_contains_all_enum_values(self):
        assert "claude" in VALID_PROVIDERS
        assert "gemini" in VALID_PROVIDERS
        assert "codex" in VALID_PROVIDERS
        assert "opencode" in VALID_PROVIDERS

    def test_valid_providers_is_frozenset(self):
        assert isinstance(VALID_PROVIDERS, frozenset)

    def test_list_providers_returns_all(self):
        result = list_providers()
        assert set(result) == VALID_PROVIDERS

    def test_list_providers_preserves_order(self):
        result = list_providers()
        assert isinstance(result, list)
        # Should match enum definition order
        assert result == ["claude", "gemini", "codex", "opencode"]

    def test_get_provider_roundtrip(self):
        """Every name in VALID_PROVIDERS resolves via get_provider()."""
        for name in VALID_PROVIDERS:
            p = get_provider(name)
            assert p.name == name


class TestGenericProvider:
    """Tests for GenericProvider — used by custom LLM definitions."""

    def setup_method(self):
        from swarm.providers.generic import GenericProvider

        self.p = GenericProvider(name="aider", command=["aider"], display="Aider")

    def test_name(self):
        assert self.p.name == "aider"

    def test_display_name(self):
        assert self.p.display_name == "Aider"

    def test_display_name_defaults_to_title(self):
        from swarm.providers.generic import GenericProvider

        p = GenericProvider(name="mytool", command=["mytool"])
        assert p.display_name == "Mytool"

    def test_worker_command(self):
        assert self.p.worker_command() == ["aider"]

    def test_headless_command(self):
        cmd = self.p.headless_command("hello")
        assert cmd == ["aider", "hello"]

    def test_classify_shell_stung(self):
        assert self.p.classify_output("bash", "$ ") == WorkerState.STUNG

    def test_classify_default_buzzing(self):
        assert self.p.classify_output("aider", "working...") == WorkerState.BUZZING

    def test_classify_empty_buzzing(self):
        assert self.p.classify_output("aider", "") == WorkerState.BUZZING

    def test_has_choice_prompt_false(self):
        assert self.p.has_choice_prompt("anything") is False

    def test_is_user_question_false(self):
        assert self.p.is_user_question("anything") is False

    def test_safe_tool_patterns_never_match(self):
        assert not self.p.safe_tool_patterns().search("anything")

    def test_parse_headless_response(self):
        text, sid = self.p.parse_headless_response(b"hello world")
        assert text == "hello world"
        assert sid is None


class TestCustomRegistry:
    """Tests for custom provider registration and lookup."""

    def setup_method(self):
        from swarm.providers import register_custom_providers

        register_custom_providers([])  # clean state

    def teardown_method(self):
        from swarm.providers import register_custom_providers

        register_custom_providers([])  # clean up

    def test_register_and_get_valid_providers(self):
        from swarm.config import CustomLLMConfig
        from swarm.providers import get_valid_providers, register_custom_providers

        register_custom_providers(
            [
                CustomLLMConfig(name="aider", command=["aider"]),
            ]
        )
        valid = get_valid_providers()
        assert "aider" in valid
        assert "claude" in valid

    def test_list_providers_includes_custom(self):
        from swarm.config import CustomLLMConfig
        from swarm.providers import register_custom_providers

        register_custom_providers(
            [
                CustomLLMConfig(name="aider", command=["aider"]),
            ]
        )
        names = list_providers()
        assert "aider" in names
        assert "claude" in names

    def test_get_provider_custom(self):
        from swarm.config import CustomLLMConfig
        from swarm.providers import register_custom_providers

        register_custom_providers(
            [
                CustomLLMConfig(name="aider", command=["aider"], display_name="Aider"),
            ]
        )
        p = get_provider("aider")
        assert p.name == "aider"
        assert p.display_name == "Aider"

    def test_get_provider_unknown_raises(self):
        import pytest

        with pytest.raises(ValueError, match="Unknown provider"):
            get_provider("nonexistent")

    def test_list_builtin_providers(self):
        from swarm.providers import list_builtin_providers

        builtins = list_builtin_providers()
        names = [b["name"] for b in builtins]
        assert "claude" in names
        assert "gemini" in names
        assert all("display_name" in b and "command" in b for b in builtins)


class TestProviderTuning:
    """Tests for the ProviderTuning dataclass."""

    def test_empty_has_no_tuning(self):
        from swarm.config import ProviderTuning

        t = ProviderTuning()
        assert t.has_tuning() is False

    def test_has_tuning_with_idle_pattern(self):
        from swarm.config import ProviderTuning

        t = ProviderTuning(idle_pattern="^aider>")
        assert t.has_tuning() is True

    def test_has_tuning_with_approval_key(self):
        from swarm.config import ProviderTuning

        t = ProviderTuning(approval_key="y\r")
        assert t.has_tuning() is True

    def test_has_tuning_with_tail_lines(self):
        from swarm.config import ProviderTuning

        t = ProviderTuning(tail_lines=20)
        assert t.has_tuning() is True

    def test_compiles_patterns(self):
        from swarm.config import ProviderTuning

        t = ProviderTuning(idle_pattern="^prompt>", busy_pattern="working")
        assert t._idle_re is not None
        assert t._idle_re.search("prompt> ")
        assert t._busy_re is not None
        assert t._busy_re.search("still working")

    def test_invalid_regex_compiles_to_never_match(self):
        from swarm.config import ProviderTuning

        t = ProviderTuning(idle_pattern="[invalid")
        assert t._idle_re is not None
        assert not t._idle_re.search("[invalid")

    def test_env_vars_and_prefixes(self):
        from swarm.config import ProviderTuning

        t = ProviderTuning(
            env_strip_prefixes=["FOO_", "BAR_"],
            env_vars={"MY_KEY": "val"},
        )
        assert t.has_tuning() is True
        assert t.env_strip_prefixes == ["FOO_", "BAR_"]
        assert t.env_vars == {"MY_KEY": "val"}


class TestTunedProvider:
    """Tests for TunedProvider wrapper."""

    def setup_method(self):
        from swarm.config import ProviderTuning
        from swarm.providers.generic import GenericProvider
        from swarm.providers.tuned import TunedProvider

        self.inner = GenericProvider(name="test", command=["test-cli"], display="Test")
        self.tuning = ProviderTuning(
            idle_pattern=r"^test>\s*$",
            busy_pattern=r"processing\.\.\.",
            choice_pattern=r"\(y/n\)",
            user_question_pattern=r"Enter your name:",
            approval_key="y\r",
            rejection_key="n\r",
        )
        self.p = TunedProvider(self.inner, self.tuning)

    def test_name_delegates(self):
        assert self.p.name == "test"

    def test_display_name_delegates(self):
        assert self.p.display_name == "Test"

    def test_classify_idle_pattern(self):
        content = "some output\ntest> "
        assert self.p.classify_output("test-cli", content) == WorkerState.RESTING

    def test_classify_busy_pattern(self):
        content = "processing..."
        assert self.p.classify_output("test-cli", content) == WorkerState.BUZZING

    def test_classify_choice_pattern(self):
        content = "Do you accept? (y/n)"
        assert self.p.classify_output("test-cli", content) == WorkerState.WAITING

    def test_classify_fallthrough_to_inner(self):
        content = "random unknown output"
        # GenericProvider returns BUZZING for unknown content
        assert self.p.classify_output("test-cli", content) == WorkerState.BUZZING

    def test_classify_shell_stung_via_inner(self):
        assert self.p.classify_output("bash", "$ ") == WorkerState.STUNG

    def test_has_choice_prompt_tuning(self):
        assert self.p.has_choice_prompt("accept? (y/n)") is True

    def test_has_choice_prompt_fallthrough(self):
        # GenericProvider returns False
        assert self.p.has_choice_prompt("random text") is False

    def test_is_user_question_tuning(self):
        assert self.p.is_user_question("Enter your name: ") is True

    def test_is_user_question_fallthrough(self):
        assert self.p.is_user_question("random text") is False

    def test_approval_response_tuning(self):
        assert self.p.approval_response(True) == "y\r"
        assert self.p.approval_response(False) == "n\r"

    def test_approval_response_fallthrough(self):
        from swarm.config import ProviderTuning
        from swarm.providers.tuned import TunedProvider

        p = TunedProvider(self.inner, ProviderTuning())
        # Falls through to inner (GenericProvider base: y\r / n\r)
        assert p.approval_response(True) == "y\r"

    def test_safe_tool_patterns_tuning_overrides(self):
        from swarm.config import ProviderTuning
        from swarm.providers.tuned import TunedProvider

        tuning = ProviderTuning(safe_patterns=r"Read|Write")
        p = TunedProvider(self.inner, tuning)
        assert p.safe_tool_patterns().search("Read(file.txt)")

    def test_safe_tool_patterns_fallthrough(self):
        from swarm.config import ProviderTuning
        from swarm.providers.tuned import TunedProvider

        p = TunedProvider(self.inner, ProviderTuning())
        # Falls through to GenericProvider's never-match
        assert not p.safe_tool_patterns().search("anything")

    def test_env_strip_prefixes_combined(self):
        from swarm.config import ProviderTuning
        from swarm.providers.tuned import TunedProvider

        tuning = ProviderTuning(env_strip_prefixes=["EXTRA_"])
        p = TunedProvider(self.inner, tuning)
        result = p.env_strip_prefixes()
        assert "EXTRA_" in result

    def test_worker_command_delegates(self):
        assert self.p.worker_command() == ["test-cli"]


class TestProviderOverridesRegistry:
    """Tests for the overrides registry in providers/__init__."""

    def setup_method(self):
        from swarm.providers import register_provider_overrides

        register_provider_overrides({})

    def teardown_method(self):
        from swarm.providers import register_provider_overrides

        register_provider_overrides({})

    def test_builtin_with_override_returns_tuned(self):
        from swarm.config import ProviderTuning
        from swarm.providers import register_provider_overrides
        from swarm.providers.tuned import TunedProvider

        register_provider_overrides(
            {
                "claude": ProviderTuning(idle_pattern="^custom>"),
            }
        )
        p = get_provider("claude")
        assert isinstance(p, TunedProvider)

    def test_builtin_without_override_returns_raw(self):
        from swarm.providers.tuned import TunedProvider

        p = get_provider("claude")
        assert not isinstance(p, TunedProvider)

    def test_empty_tuning_not_wrapped(self):
        from swarm.config import ProviderTuning
        from swarm.providers import register_provider_overrides
        from swarm.providers.tuned import TunedProvider

        register_provider_overrides(
            {
                "claude": ProviderTuning(),
            }
        )
        p = get_provider("claude")
        assert not isinstance(p, TunedProvider)
