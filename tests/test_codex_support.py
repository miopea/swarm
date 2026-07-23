"""Stage 0 Codex-support: provider-aware injected text + state correctness.

Covers the provider `plan_mode_preamble` / `has_active_turn_signal` hooks, that
`build_task_message` stays byte-identical for the default (Claude) path while
Codex gets Codex-appropriate wording, that the state tracker delegates the
active-turn check to the worker's provider, and that inert Claude artifacts
(`.mcp.json`) are no longer written into non-hooks worker dirs.
"""

from __future__ import annotations

import json

import pytest

from swarm.providers import get_provider
from swarm.providers.base import _GENERIC_PLAN_PREAMBLE
from swarm.providers.claude import CLAUDE_PLAN_PREAMBLE
from swarm.server.messages import build_task_message
from swarm.tasks.task import SwarmTask


# ---------------------------------------------------------------------------
# Provider plan-mode preamble
# ---------------------------------------------------------------------------
def test_claude_preamble_names_exitplanmode():
    assert "ExitPlanMode" in get_provider("claude").plan_mode_preamble()


def test_codex_preamble_is_codex_shaped():
    text = get_provider("codex").plan_mode_preamble()
    assert "ExitPlanMode" not in text  # Codex has no such tool
    assert "AskUserQuestion" in text
    # Codex DOES have the swarm_* MCP tools (via ~/.codex/config.toml)
    assert "swarm_complete_task" in text


def test_base_default_preamble_is_provider_neutral():
    # gemini/opencode have no override → provider-neutral default
    text = get_provider("gemini").plan_mode_preamble()
    assert text == _GENERIC_PLAN_PREAMBLE
    assert "ExitPlanMode" not in text
    assert "swarm_complete_task" not in text


# ---------------------------------------------------------------------------
# build_task_message: Claude byte-identical, Codex divergent
# ---------------------------------------------------------------------------
def _user_task() -> SwarmTask:
    # source_worker empty ⇒ user-request ⇒ plan gate applies
    return SwarmTask(title="Do a thing", description="details", source_worker="")


def test_build_message_default_is_claude_byte_identical():
    """No plan_preamble supplied ⇒ falls back to Claude's exact preamble, so the
    emitted message is unchanged from before the refactor."""
    msg = build_task_message(_user_task(), plan_mode_for_user_requests=True)
    assert msg.startswith(CLAUDE_PLAN_PREAMBLE)
    assert "ExitPlanMode" in msg


def test_build_message_codex_preamble_has_no_exitplanmode():
    codex_preamble = get_provider("codex").plan_mode_preamble()
    msg = build_task_message(
        _user_task(),
        supports_slash_commands=False,
        plan_mode_for_user_requests=True,
        plan_preamble=codex_preamble,
    )
    assert "ExitPlanMode" not in msg
    assert "AskUserQuestion" in msg
    # completion instruction still references the MCP tool (valid for Codex)
    assert "swarm_complete_task" in msg


def test_worker_handoff_skips_preamble_regardless_of_provider():
    # source_worker set ⇒ peer handoff ⇒ no plan gate
    task = SwarmTask(title="x", source_worker="peer")
    msg = build_task_message(task, plan_preamble=get_provider("codex").plan_mode_preamble())
    assert "--- TASK ---" not in msg


# ---------------------------------------------------------------------------
# Provider active-turn signal
# ---------------------------------------------------------------------------
def test_claude_active_turn_signal():
    c = get_provider("claude")
    assert c.has_active_turn_signal("working\nesc to interrupt\n") is True
    assert c.has_active_turn_signal("> \n1 background dynamic workflow · /workflows\n") is True
    assert c.has_active_turn_signal("Done.\n> \n? for shortcuts\n") is False


def test_codex_active_turn_signal():
    x = get_provider("codex")
    assert x.has_active_turn_signal("• Working (4s • esc to interrupt)\n") is True
    assert x.has_active_turn_signal("done\n  gpt-5.6-sol default · ~/proj\n") is False
    assert x.has_active_turn_signal("") is False


def test_base_default_active_turn_is_false():
    # A provider with no override opts out (base default False).
    assert get_provider("gemini").has_active_turn_signal("anything ▶\n") is False


# ---------------------------------------------------------------------------
# State-tracker delegation
# ---------------------------------------------------------------------------
def test_state_tracker_delegates_active_turn_to_provider():
    from tests.test_state_tracker import _make_tracker  # reuse the fixture

    tracker, _ = _make_tracker()
    # Content-only call (no worker) falls back to Claude → real Claude behaviour.
    assert tracker._has_active_turn_signal("working\nesc to interrupt\n") is True
    assert tracker._has_active_turn_signal("Done.\n> \n") is False


# ---------------------------------------------------------------------------
# Artifact gating — no inert .mcp.json in non-hooks worker dirs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "provider,expect_mcp",
    [("claude", True), ("codex", False)],
)
def test_mcp_config_written_only_for_hooks_providers(provider, expect_mcp, tmp_path, monkeypatch):
    from swarm.auth import mcp_token
    from swarm.worker.worker import Worker
    from tests.conftest import make_daemon
    from tests.fakes.process import FakeWorkerProcess

    monkeypatch.setattr(mcp_token, "_cached", "tok")
    d = make_daemon(monkeypatch=monkeypatch)
    wdir = tmp_path / "proj"
    wdir.mkdir()
    w = Worker(name="w1", path=str(wdir), process=FakeWorkerProcess(name="w1"))
    w.provider_name = provider
    d.workers = [w]
    d.config.port = 9090

    d._write_worker_mcp_configs()

    mcp_file = wdir / ".mcp.json"
    assert mcp_file.exists() is expect_mcp
    if expect_mcp:
        server = json.loads(mcp_file.read_text())["mcpServers"]["swarm"]
        assert server["headers"]["Authorization"] == "Bearer tok"
