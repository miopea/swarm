"""Tests for CodexProvider — state detection and CLI command generation.

State-detection fixtures are built from RAW PTY output captured against a live
`codex --no-alt-screen` worker on 2026-07-23 (see the
`reference_codex_pty_patterns` memory). The earlier `[◇□]`/`[▶▷]` glyph stub is
gone — these assert the real text markers.
"""

import json

from swarm.providers.codex import CodexProvider
from swarm.worker.worker import WorkerState

_provider = CodexProvider()

# --- Real captured PTY fixtures ---

_APPROVAL = (
    "$ git status\n"
    "  git log --oneline -5\n"
    "\n"
    "› 1. Yes, proceed (y)\n"
    "  2. Yes, and don't ask again for commands that start with `git status` (p)\n"
    "  3. No, and tell Codex what to do differently (esc)\n"
    "\n"
    "  Press enter to confirm or esc to cancel\n"
)

_BUSY = (
    "• Working (4s • esc to interrupt)\n"
    "\n"
    "› Write tests for @filename\n"
    "\n"
    "  gpt-5.6-sol default · ~/projects/personal/sculpt-studio\n"
)

# Idle — note "Working tree" (git output) must NOT be read as busy.
_IDLE = (
    "• Current branch: main\n"
    "  Working tree: not clean (.mcp.json modified)\n"
    "  Latest commit: feat(readiness): sleep-quality awareness\n"
    "\n"
    "› Write tests for @filename\n"
    "\n"
    "  gpt-5.6-sol default · ~/projects/personal/sculpt-studio\n"
)


# --- classify_output ---


class TestCodexClassifyOutput:
    def test_approval_widget_is_waiting(self):
        assert _provider.classify_output("codex", _APPROVAL) == WorkerState.WAITING

    def test_working_timer_is_buzzing(self):
        assert _provider.classify_output("codex", _BUSY) == WorkerState.BUZZING

    def test_idle_composer_is_resting(self):
        assert _provider.classify_output("codex", _IDLE) == WorkerState.RESTING

    def test_working_tree_is_not_busy(self):
        """'Working tree' in git output must not trip the busy timer regex."""
        content = "  Working tree: clean\n\n  gpt-5.6-sol default · ~/proj\n"
        assert _provider.classify_output("codex", content) == WorkerState.RESTING

    def test_waiting_takes_priority_over_footer(self):
        content = _APPROVAL + "\n  gpt-5.6-sol default · ~/proj\n"
        assert _provider.classify_output("codex", content) == WorkerState.WAITING

    def test_shell_exit_is_stung(self):
        assert _provider.classify_output("bash", "user@host:~$ ") == WorkerState.STUNG

    def test_unknown_falls_back_to_buzzing(self):
        assert _provider.classify_output("codex", "some opaque output\n") == WorkerState.BUZZING


# --- has_choice_prompt ---


class TestCodexHasChoicePrompt:
    def test_detects_approval_widget(self):
        assert _provider.has_choice_prompt(_APPROVAL) is True

    def test_false_when_idle(self):
        assert _provider.has_choice_prompt(_IDLE) is False

    def test_false_when_busy(self):
        assert _provider.has_choice_prompt(_BUSY) is False

    def test_false_empty(self):
        assert _provider.has_choice_prompt("") is False


# --- get_choice_summary ---


class TestCodexGetChoiceSummary:
    def test_extracts_command_awaiting_approval(self):
        assert _provider.get_choice_summary(_APPROVAL) == "git status"

    def test_empty_when_no_approval(self):
        assert _provider.get_choice_summary(_IDLE) == ""


# --- has_idle_prompt ---


class TestCodexHasIdlePrompt:
    def test_true_when_idle(self):
        assert _provider.has_idle_prompt(_IDLE) is True

    def test_false_when_busy(self):
        assert _provider.has_idle_prompt(_BUSY) is False

    def test_false_when_awaiting_approval(self):
        assert _provider.has_idle_prompt(_APPROVAL) is False


# --- approval_response ---


class TestCodexApprovalResponse:
    def test_approve_is_enter(self):
        assert _provider.approval_response(approve=True) == "\r"

    def test_reject_is_esc(self):
        assert _provider.approval_response(approve=False) == "\x1b"


# --- safe_tool_patterns (auto-approve read-only shell commands) ---


class TestCodexSafeToolPatterns:
    def test_matches_safe_git(self):
        p = _provider.safe_tool_patterns()
        assert p.search("$ git status")
        assert p.search("$ git log --oneline -5")

    def test_matches_safe_shell(self):
        p = _provider.safe_tool_patterns()
        assert p.search("$ ls -la")
        assert p.search("$ cat pyproject.toml")

    def test_rejects_mutating(self):
        p = _provider.safe_tool_patterns()
        assert not p.search("$ rm -rf /")
        assert not p.search("$ git push origin main")


# --- has_active_turn_signal ---


class TestCodexActiveTurnSignal:
    def test_busy_is_active(self):
        assert _provider.has_active_turn_signal(_BUSY) is True

    def test_idle_is_not_active(self):
        assert _provider.has_active_turn_signal(_IDLE) is False

    def test_empty_is_not_active(self):
        assert _provider.has_active_turn_signal("") is False


# --- worker_command ---


class TestCodexWorkerCommand:
    def test_always_includes_no_alt_screen(self):
        assert _provider.worker_command(resume=True) == ["codex", "--no-alt-screen"]

    def test_resume_flag_ignored(self):
        assert _provider.worker_command(resume=True) == _provider.worker_command(resume=False)


# --- headless_command ---


class TestCodexHeadlessCommand:
    def test_basic(self):
        assert _provider.headless_command("hello world") == ["codex", "exec", "hello world"]

    def test_with_json_format(self):
        cmd = _provider.headless_command("check status", output_format="json")
        assert cmd == ["codex", "exec", "check status", "--json"]

    def test_session_id_ignored(self):
        cmd = _provider.headless_command("do stuff", session_id="abc123")
        assert "--resume" not in cmd and "abc123" not in cmd

    def test_max_turns_ignored(self):
        cmd = _provider.headless_command("do stuff", max_turns=10)
        assert "--max-turns" not in cmd


# --- parse_headless_response ---


class TestCodexParseHeadlessResponse:
    def test_extracts_last_agent_message(self):
        events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "First response"}},
            {"type": "item.completed", "item": {"type": "tool_call", "text": "ls -la"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Final answer"}},
        ]
        stdout = "\n".join(json.dumps(e) for e in events).encode()
        text, session_id = _provider.parse_headless_response(stdout)
        assert text == "Final answer"
        assert session_id is None

    def test_falls_back_to_raw_text(self):
        text, session_id = _provider.parse_headless_response(b"plain text output")
        assert text == "plain text output"
        assert session_id is None

    def test_handles_mixed_valid_invalid_jsonl(self):
        lines = [
            '{"type": "item.completed", "item": {"type": "agent_message", "text": "good"}}',
            "not valid json",
            '{"type": "item.completed", "item": {"type": "agent_message", "text": "last"}}',
        ]
        stdout = "\n".join(lines).encode()
        text, _ = _provider.parse_headless_response(stdout)
        assert text == "last"

    def test_empty_stdout(self):
        text, session_id = _provider.parse_headless_response(b"")
        assert text == "" and session_id is None

    def test_handles_invalid_utf8(self):
        text, _ = _provider.parse_headless_response(b"valid \xff invalid")
        assert "valid" in text


# --- misc properties ---


class TestCodexMiscProperties:
    def test_name(self):
        assert _provider.name == "codex"

    def test_env_strip_prefixes(self):
        assert _provider.env_strip_prefixes() == ("OPENAI",)

    def test_supports_resume(self):
        assert _provider.supports_resume is False

    def test_supports_hooks(self):
        assert _provider.supports_hooks is False

    def test_supports_slash_commands(self):
        assert _provider.supports_slash_commands is False

    def test_supports_native_goal(self):
        assert _provider.supports_native_goal is True
