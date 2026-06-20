"""Tests for the swarm_query_peers MCP tool (feature B11)."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from swarm.mcp.tools import TOOLS, handle_tool_call
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask, TaskPriority
from swarm.worker.worker import QUEEN_WORKER_NAME, Worker, WorkerState


def _structured(result):
    """Pull structuredContent from the dual-shape MCP return."""
    assert isinstance(result, dict), f"expected structured result, got {result!r}"
    return result["structuredContent"]


def _text(result):
    if isinstance(result, dict):
        return result["content"][0]["text"]
    return result[0]["text"]


@pytest.fixture
def daemon():
    d = MagicMock()
    d.broadcast_ws = MagicMock()
    board = TaskBoard()
    d.task_board = board
    # caller = "api"; peers = hub (busy), platform (idle), queen (excluded)
    d.workers = [
        Worker(name=QUEEN_WORKER_NAME, path="/tmp/q", kind="queen"),
        Worker(name="api", path="/tmp/api", state=WorkerState.BUZZING),
        Worker(name="hub", path="/tmp/hub", state=WorkerState.BUZZING),
        Worker(name="platform", path="/tmp/platform", state=WorkerState.RESTING),
    ]
    return d


def _idle_for(worker: Worker, seconds: float) -> None:
    worker.state = WorkerState.RESTING
    worker.state_since = time.time() - seconds


class TestRegistration:
    def test_tool_is_registered(self):
        names = {t["name"] for t in TOOLS}
        assert "swarm_query_peers" in names

    def test_no_required_args(self):
        tool = next(t for t in TOOLS if t["name"] == "swarm_query_peers")
        schema = tool["inputSchema"]
        assert schema.get("required", []) == []

    def test_description_states_readonly_guardrail(self):
        tool = next(t for t in TOOLS if t["name"] == "swarm_query_peers")
        desc = tool["description"].lower()
        assert "read-only" in desc or "read only" in desc
        # must not promise an interrupt/handoff capability
        assert "interrupt" in desc  # mentions it to say it's NOT available


class TestQueryPeers:
    def test_excludes_queen_and_caller(self, daemon):
        result = handle_tool_call(daemon, "api", "swarm_query_peers", {})
        peers = _structured(result)["peers"]
        names = {p["name"] for p in peers}
        assert names == {"hub", "platform"}
        assert QUEEN_WORKER_NAME not in names
        assert "api" not in names

    def test_total_matches(self, daemon):
        result = handle_tool_call(daemon, "api", "swarm_query_peers", {})
        s = _structured(result)
        assert s["total"] == len(s["peers"]) == 2

    def test_idle_peers_sorted_first_longest_first(self, daemon):
        # platform idle 300s, hub idle 60s, api stays caller
        _idle_for(daemon.workers[3], 300)  # platform
        _idle_for(daemon.workers[2], 60)  # hub
        peers = _structured(handle_tool_call(daemon, "api", "swarm_query_peers", {}))["peers"]
        assert [p["name"] for p in peers] == ["platform", "hub"]
        assert peers[0]["idle_seconds"] >= 300
        assert peers[1]["idle_seconds"] >= 60

    def test_busy_peer_has_zero_idle(self, daemon):
        # hub is BUZZING
        peers = _structured(handle_tool_call(daemon, "api", "swarm_query_peers", {}))["peers"]
        hub = next(p for p in peers if p["name"] == "hub")
        assert hub["idle_seconds"] == 0
        assert hub["state"] == "BUZZING"

    def test_current_task_and_queue_depth(self, daemon):
        board = daemon.task_board
        # hub has one ACTIVE task + two ASSIGNED (queued)
        active = SwarmTask(title="Fix auth", priority=TaskPriority.NORMAL, number=412)
        board.add(active)
        board.assign(active.id, "hub")
        board.activate(active.id)
        for n in (413, 414):
            q = SwarmTask(title=f"Queued {n}", number=n)
            board.add(q)
            board.assign(q.id, "hub")
        peers = _structured(handle_tool_call(daemon, "api", "swarm_query_peers", {}))["peers"]
        hub = next(p for p in peers if p["name"] == "hub")
        assert hub["current_task"] == "Fix auth"
        assert hub["current_task_number"] == 412
        assert hub["queued_count"] == 2

    def test_no_task_peer_has_nulls(self, daemon):
        peers = _structured(handle_tool_call(daemon, "api", "swarm_query_peers", {}))["peers"]
        platform = next(p for p in peers if p["name"] == "platform")
        assert platform["current_task"] is None
        assert platform["current_task_number"] is None
        assert platform["queued_count"] == 0

    def test_state_filter_narrows(self, daemon):
        _idle_for(daemon.workers[3], 120)  # platform RESTING
        peers = _structured(
            handle_tool_call(daemon, "api", "swarm_query_peers", {"state": "RESTING"})
        )["peers"]
        assert [p["name"] for p in peers] == ["platform"]

    def test_alone_returns_empty(self):
        d = MagicMock()
        d.broadcast_ws = MagicMock()
        d.task_board = TaskBoard()
        d.workers = [
            Worker(name=QUEEN_WORKER_NAME, path="/tmp/q", kind="queen"),
            Worker(name="api", path="/tmp/api", state=WorkerState.BUZZING),
        ]
        result = handle_tool_call(d, "api", "swarm_query_peers", {})
        assert _structured(result)["total"] == 0
        assert "no other workers" in _text(result).lower()

    def test_text_summary_mentions_peer(self, daemon):
        result = handle_tool_call(daemon, "api", "swarm_query_peers", {})
        text = _text(result)
        assert "hub" in text and "platform" in text

    def test_readonly_does_not_broadcast_or_mutate(self, daemon):
        handle_tool_call(daemon, "api", "swarm_query_peers", {})
        # A pure read must not emit any WS event or mark anything.
        daemon.broadcast_ws.assert_not_called()
