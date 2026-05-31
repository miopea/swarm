"""Unit tests for DirectiveExecutor's static prompt-detection helpers.

These read ``worker.process.get_content(...)`` and classify the PTY tail.
They were previously only mocked in test_decision_executor.py — these
exercise the real regex / substring logic.
"""

from __future__ import annotations

from swarm.drones.directives import DirectiveExecutor
from swarm.worker.worker import Worker


class _FakeProc:
    """Minimal stand-in for a worker process: returns canned PTY content."""

    def __init__(self, content: str) -> None:
        self._content = content

    def get_content(self, lines: int = 0) -> str:
        return self._content


def _worker(content: str | None) -> Worker:
    w = Worker(name="w1", path="/tmp/test")
    w.process = _FakeProc(content) if content is not None else None
    return w


class TestHasOperatorTextAtPrompt:
    def test_slash_command_at_prompt(self) -> None:
        assert DirectiveExecutor.has_operator_text_at_prompt(_worker("> /verify")) is True

    def test_fancy_prompt_char_with_text(self) -> None:
        assert DirectiveExecutor.has_operator_text_at_prompt(_worker("❯ fix the bug")) is True

    def test_empty_prompt_has_no_text(self) -> None:
        # "> " with no following non-space char must not match.
        assert DirectiveExecutor.has_operator_text_at_prompt(_worker("> ")) is False

    def test_non_prompt_last_line(self) -> None:
        assert DirectiveExecutor.has_operator_text_at_prompt(_worker("just some output")) is False

    def test_no_process(self) -> None:
        assert DirectiveExecutor.has_operator_text_at_prompt(_worker(None)) is False

    def test_empty_content(self) -> None:
        assert DirectiveExecutor.has_operator_text_at_prompt(_worker("")) is False


class TestHasPendingBashApproval:
    def test_bash_with_accept_edits(self) -> None:
        tail = "Bash(rm -rf build)\nDo you want to proceed?\n Accept edits "
        assert DirectiveExecutor.has_pending_bash_approval(_worker(tail)) is True

    def test_bash_paren_with_allow_deny(self) -> None:
        tail = "bash(git push)\n  1. Allow  2. Deny"
        assert DirectiveExecutor.has_pending_bash_approval(_worker(tail)) is True

    def test_unrelated_content(self) -> None:
        assert DirectiveExecutor.has_pending_bash_approval(_worker("running tests...")) is False

    def test_no_process(self) -> None:
        assert DirectiveExecutor.has_pending_bash_approval(_worker(None)) is False


class TestHasIdlePrompt:
    def test_shortcuts_hint(self) -> None:
        assert DirectiveExecutor.has_idle_prompt(_worker("? for shortcuts")) is True

    def test_ctrl_t_hint(self) -> None:
        assert DirectiveExecutor.has_idle_prompt(_worker("ctrl+t to hide")) is True

    def test_no_idle_markers(self) -> None:
        assert DirectiveExecutor.has_idle_prompt(_worker("esc to interrupt")) is False

    def test_no_process(self) -> None:
        assert DirectiveExecutor.has_idle_prompt(_worker(None)) is False
