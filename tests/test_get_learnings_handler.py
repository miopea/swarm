"""Tests for the ``swarm_get_learnings`` MCP handler.

Covers query filtering (title + learnings text), the empty-query "return all"
path, the cap at 5 results, skipping tasks with no learnings, and the
no-results message.
"""

from __future__ import annotations

from swarm.mcp.handlers._learnings import _handle_get_learnings
from tests.conftest import make_daemon


def _with_learnings(d, title: str, learnings: str):
    t = d.task_board.create(title=title)
    t.learnings = learnings
    return t


def test_query_matches_on_learnings_text(monkeypatch):
    d = make_daemon(monkeypatch)
    _with_learnings(d, "Fix tenant bug", "Root cause: missing guard in resolveTenant")
    out = _handle_get_learnings(d, "swarm", {"query": "resolveTenant"})
    text = out[0]["text"]
    assert "resolveTenant" in text
    assert "Fix tenant bug" in text


def test_query_matches_on_title(monkeypatch):
    d = make_daemon(monkeypatch)
    _with_learnings(d, "MailParser rewrite", "switched to streaming parse")
    out = _handle_get_learnings(d, "swarm", {"query": "mailparser"})  # case-insensitive
    assert "MailParser rewrite" in out[0]["text"]


def test_no_match_returns_none_message(monkeypatch):
    d = make_daemon(monkeypatch)
    _with_learnings(d, "Fix X", "some learning about Y")
    out = _handle_get_learnings(d, "swarm", {"query": "nonexistent-token"})
    assert out[0]["text"] == "No learnings found."


def test_empty_query_returns_all_with_learnings(monkeypatch):
    d = make_daemon(monkeypatch)
    _with_learnings(d, "A", "learning alpha")
    _with_learnings(d, "B", "learning beta")
    text = _handle_get_learnings(d, "swarm", {})[0]["text"]
    assert "learning alpha" in text
    assert "learning beta" in text


def test_tasks_without_learnings_are_skipped(monkeypatch):
    d = make_daemon(monkeypatch)
    d.task_board.create(title="no-learnings task")  # learnings defaults to ""
    out = _handle_get_learnings(d, "swarm", {})
    assert out[0]["text"] == "No learnings found."


def test_results_capped_at_five(monkeypatch):
    d = make_daemon(monkeypatch)
    for i in range(7):
        _with_learnings(d, f"task {i}", f"learning number {i}")
    text = _handle_get_learnings(d, "swarm", {})[0]["text"]
    # 5 results joined by 4 separators — the 6th/7th are dropped by the cap.
    assert text.count("\n---\n") == 4
