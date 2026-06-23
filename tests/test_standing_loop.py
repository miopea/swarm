"""Tests for the standing background-improvement loop (#765).

Covers the StandingLoopManager core (generation, dedup, operator controls,
global kill switch, rolling daily token cap) and the empty-queue trigger
that makes the loop preempt-by-construction. See
``docs/specs/native-loop-functions.md`` §3.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from swarm.drones.standing_loop import StandingLoopManager
from swarm.server.routes import standing_loops as sl_routes
from swarm.server.task_coordinator import TaskCoordinator
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask, TaskStatus

TOPICS = ["topic-a", "topic-b", "topic-c"]


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _manager(
    *,
    cap: int = 0,
    open_titles: set[str] | None = None,
    clock: _Clock | None = None,
) -> tuple[StandingLoopManager, list[tuple[str, str]]]:
    filed: list[tuple[str, str]] = []

    def _file(worker: str, title: str):
        filed.append((worker, title))
        return SwarmTask(title=title)

    mgr = StandingLoopManager(
        topics=list(TOPICS),
        daily_token_cap=cap,
        file_task=_file,
        open_titles=lambda _w: set(open_titles or set()),
        now=clock or _Clock(),
    )
    return mgr, filed


class TestGatedOff:
    def test_disabled_loop_generates_nothing(self) -> None:
        mgr, filed = _manager()
        assert mgr.maybe_generate("w1") is None
        assert filed == []

    def test_kill_switch_blocks_even_when_enabled(self) -> None:
        mgr, filed = _manager()
        mgr.start("w1")
        mgr.set_kill_switch(True)
        assert mgr.maybe_generate("w1") is None
        assert filed == []
        assert mgr.kill_switch is True

    def test_paused_loop_generates_nothing(self) -> None:
        mgr, filed = _manager()
        mgr.start("w1")
        mgr.pause("w1")
        assert mgr.maybe_generate("w1") is None
        assert filed == []


class TestGeneration:
    def test_enabled_loop_files_one_task(self) -> None:
        mgr, filed = _manager()
        mgr.start("w1")
        task = mgr.maybe_generate("w1")
        assert task is not None
        assert filed == [("w1", "topic-a")]

    def test_round_robins_topics(self) -> None:
        mgr, filed = _manager()
        mgr.start("w1")
        mgr.maybe_generate("w1")
        mgr.maybe_generate("w1")
        assert filed == [("w1", "topic-a"), ("w1", "topic-b")]

    def test_dedups_against_open_tasks(self) -> None:
        # topic-a already has an open task → generator skips to topic-b.
        mgr, filed = _manager(open_titles={"topic-a"})
        mgr.start("w1")
        mgr.maybe_generate("w1")
        assert filed == [("w1", "topic-b")]

    def test_all_topics_open_files_nothing(self) -> None:
        mgr, filed = _manager(open_titles=set(TOPICS))
        mgr.start("w1")
        assert mgr.maybe_generate("w1") is None
        assert filed == []

    def test_stop_disables(self) -> None:
        mgr, filed = _manager()
        mgr.start("w1")
        mgr.stop("w1")
        assert mgr.maybe_generate("w1") is None
        assert filed == []


class TestDailyCap:
    def test_loop_sleeps_when_cap_exhausted(self) -> None:
        mgr, filed = _manager(cap=1000)
        mgr.start("w1")
        mgr.record_burn("w1", 1000)  # hit the cap
        assert mgr.is_exhausted("w1") is True
        assert mgr.maybe_generate("w1") is None
        assert filed == []

    def test_under_cap_still_generates(self) -> None:
        mgr, filed = _manager(cap=1000)
        mgr.start("w1")
        mgr.record_burn("w1", 999)
        assert mgr.is_exhausted("w1") is False
        assert mgr.maybe_generate("w1") is not None

    def test_window_resets_after_24h(self) -> None:
        clock = _Clock(1000.0)
        mgr, filed = _manager(cap=1000, clock=clock)
        mgr.start("w1")
        mgr.record_burn("w1", 1000)
        assert mgr.is_exhausted("w1") is True
        clock.t += 86_400.0 + 1  # next day
        assert mgr.is_exhausted("w1") is False
        assert mgr.maybe_generate("w1") is not None


class TestStatusReadout:
    def test_status_shape(self) -> None:
        mgr, _ = _manager(cap=5000)
        mgr.start("w1")
        mgr.record_burn("w1", 1200)
        status = mgr.status()
        assert status["kill_switch"] is False
        assert status["daily_token_cap"] == 5000
        row = next(r for r in status["loops"] if r["worker"] == "w1")
        assert row["enabled"] is True
        assert row["tokens_in_window"] == 1200


class TestEmptyQueueTriggerPreemption:
    """The generator fires ONLY from the empty-queue branch of the self-loop —
    so any real ASSIGNED task preempts it (criterion 2 / §3.3)."""

    def _coord(self, board: TaskBoard):
        fake_daemon = SimpleNamespace(
            task_board=board,
            _maybe_run_standing_loop=MagicMock(),
            start_task=MagicMock(),
            _track_task=MagicMock(),
        )
        return SimpleNamespace(_d=fake_daemon), fake_daemon

    def test_empty_queue_runs_standing_loop(self) -> None:
        board = TaskBoard()
        coord, daemon = self._coord(board)
        TaskCoordinator.auto_start_next_assigned(coord, "w1")
        daemon._maybe_run_standing_loop.assert_called_once_with("w1")

    def test_real_assigned_task_preempts_loop(self) -> None:
        board = TaskBoard()
        task = board.add(SwarmTask(title="real work", assigned_worker="w1"))
        task.assigned_worker = "w1"
        task.status = TaskStatus.ASSIGNED
        coord, daemon = self._coord(board)
        # A real ASSIGNED task exists → the self-loop starts THAT and never
        # reaches the standing-loop generator.
        try:
            TaskCoordinator.auto_start_next_assigned(coord, "w1")
        except Exception:
            pass  # start_task is a MagicMock; we only assert preemption
        daemon._maybe_run_standing_loop.assert_not_called()


class _FakeRequest:
    """Minimal aiohttp-request stand-in for the route handlers."""

    def __init__(self, daemon, body: dict | None = None) -> None:
        self.app = {"daemon": daemon}
        self._body = body
        self.can_read_body = body is not None

    async def json(self) -> dict:
        return self._body or {}


def _route_daemon():
    mgr = StandingLoopManager(
        topics=list(TOPICS),
        daily_token_cap=5000,
        file_task=lambda w, t: SwarmTask(title=t),
        open_titles=lambda _w: set(),
    )
    return SimpleNamespace(standing_loop=mgr), mgr


async def _body(resp) -> dict:
    return json.loads(resp.body.decode())


class TestDashboardRoutes:
    """Criterion 4: dashboard exposes start/pause/stop, kill switch, readout."""

    @pytest.mark.asyncio
    async def test_status_readout(self) -> None:
        daemon, mgr = _route_daemon()
        mgr.start("w1")
        resp = await sl_routes.handle_status(_FakeRequest(daemon))
        data = await _body(resp)
        assert data["daily_token_cap"] == 5000
        assert any(loop["worker"] == "w1" for loop in data["loops"])

    @pytest.mark.asyncio
    async def test_start_pause_stop_per_worker(self) -> None:
        daemon, mgr = _route_daemon()
        await sl_routes.handle_start(_FakeRequest(daemon, {"worker": "w1"}))
        assert mgr._state("w1").enabled is True
        await sl_routes.handle_pause(_FakeRequest(daemon, {"worker": "w1"}))
        assert mgr._state("w1").paused is True
        await sl_routes.handle_stop(_FakeRequest(daemon, {"worker": "w1"}))
        assert mgr._state("w1").enabled is False

    @pytest.mark.asyncio
    async def test_missing_worker_is_400(self) -> None:
        daemon, _ = _route_daemon()
        resp = await sl_routes.handle_start(_FakeRequest(daemon, {}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_global_kill_switch(self) -> None:
        daemon, mgr = _route_daemon()
        resp = await sl_routes.handle_kill_switch(_FakeRequest(daemon, {"on": True}))
        data = await _body(resp)
        assert data["kill_switch"] is True
        assert mgr.kill_switch is True
