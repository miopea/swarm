"""Tests for :class:`swarm.server.state_publisher.StatePublisher`.

Unit-level coverage of the broadcast layer that ferries worker /
task / pipeline state to WS clients. The publisher is constructed
with ~17 callbacks injected by the daemon, so these tests build it
with `MagicMock`s for every dependency and assert the broadcast
payload + side-effect calls. Integration via the live daemon is
covered separately in `tests/test_daemon.py`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from swarm.drones.log import LogCategory, SystemAction
from swarm.server.state_publisher import StatePublisher, _terse_detail
from swarm.tunnel import TunnelState
from swarm.worker.worker import Worker, WorkerState


def _make_worker(
    name: str = "alpha",
    state: WorkerState = WorkerState.RESTING,
) -> Worker:
    w = Worker(name=name, path=f"/tmp/{name}")
    w.state = state
    return w


def _make_publisher(
    *,
    workers: list[Worker] | None = None,
    worker_task_map: dict[str, str] | None = None,
    pending: list[Any] | None = None,
    pressure_level: str = "nominal",
) -> tuple[StatePublisher, dict[str, MagicMock]]:
    """Build a publisher with MagicMock callbacks; return (publisher, mocks)."""
    workers = workers if workers is not None else []
    mocks = {
        "broadcast_ws": MagicMock(),
        "expire_proposals": MagicMock(),
        "broadcast_proposals": MagicMock(),
        "clear_worker_inflight": MagicMock(),
        "pending_for_worker": MagicMock(return_value=pending or []),
        "clear_resolved_proposals": MagicMock(),
        "update_proposal_status": MagicMock(),
        "push_notification": MagicMock(),
        "notification_bus": MagicMock(),
        "drone_log": MagicMock(),
        "emit": MagicMock(),
        "track_task": MagicMock(),
        "mark_dirty": MagicMock(),
    }
    pub = StatePublisher(
        broadcast_ws=mocks["broadcast_ws"],
        get_workers=lambda: workers,
        get_worker_task_map=lambda: worker_task_map or {},
        expire_proposals=mocks["expire_proposals"],
        broadcast_proposals=mocks["broadcast_proposals"],
        clear_worker_inflight=mocks["clear_worker_inflight"],
        pending_for_worker=mocks["pending_for_worker"],
        clear_resolved_proposals=mocks["clear_resolved_proposals"],
        update_proposal_status=mocks["update_proposal_status"],
        push_notification=mocks["push_notification"],
        notification_bus=mocks["notification_bus"],
        drone_log=mocks["drone_log"],
        emit=mocks["emit"],
        get_pressure_level=lambda: pressure_level,
        pipeline_engine=MagicMock(),
        service_registry=MagicMock(),
        track_task=mocks["track_task"],
        mark_dirty=mocks["mark_dirty"],
    )
    return pub, mocks


# ---------------------------------------------------------------------------
# _terse_detail helper
# ---------------------------------------------------------------------------


class TestTerseDetail:
    def test_empty_input_returns_empty(self) -> None:
        assert _terse_detail(None) == ""
        assert _terse_detail("") == ""

    def test_collapses_whitespace_on_first_line(self) -> None:
        assert _terse_detail("hello   world  \t there") == "hello world there"

    def test_picks_first_non_empty_line(self) -> None:
        # WORKER_STUNG ships a multi-line tail; we want the first
        # meaningful line.
        assert _terse_detail("\n\n  \nfirst real\nsecond line") == "first real"

    def test_caps_at_160_chars(self) -> None:
        long = "x" * 500
        result = _terse_detail(long)
        assert len(result) == 160
        assert result.endswith("…")

    def test_under_cap_unchanged(self) -> None:
        s = "x" * 159
        assert _terse_detail(s) == s


# ---------------------------------------------------------------------------
# Task board / pipeline / tunnel — single-shot broadcasts
# ---------------------------------------------------------------------------


class TestSimpleBroadcasts:
    def test_on_task_board_changed_broadcasts_and_expires(self) -> None:
        pub, m = _make_publisher()
        pub.on_task_board_changed()
        m["broadcast_ws"].assert_called_once_with({"type": "tasks_changed"})
        m["expire_proposals"].assert_called_once()

    def test_on_workers_changed_includes_state_and_task_map(self) -> None:
        workers = [_make_worker("alpha"), _make_worker("beta", state=WorkerState.BUZZING)]
        task_map = {"alpha": "task-1"}
        pub, m = _make_publisher(workers=workers, worker_task_map=task_map)
        pub.on_workers_changed()
        payload = m["broadcast_ws"].call_args.args[0]
        assert payload["type"] == "workers_changed"
        assert {w["name"] for w in payload["workers"]} == {"alpha", "beta"}
        assert payload["worker_tasks"] == task_map
        m["expire_proposals"].assert_called_once()
        m["emit"].assert_called_once_with("workers_changed")

    def test_broadcast_state_includes_pressure_level(self) -> None:
        workers = [_make_worker("alpha")]
        pub, m = _make_publisher(workers=workers, pressure_level="high")
        pub.broadcast_state()
        payload = m["broadcast_ws"].call_args.args[0]
        assert payload["type"] == "state"
        assert payload["pressure_level"] == "high"
        assert payload["workers"][0]["name"] == "alpha"

    def test_broadcast_usage_aggregates_across_workers(self) -> None:
        w1, w2 = _make_worker("a"), _make_worker("b")
        w1.usage.cost_usd = 0.10
        w1.usage.input_tokens = 100
        w2.usage.cost_usd = 0.25
        w2.usage.input_tokens = 250
        pub, m = _make_publisher(workers=[w1, w2])
        pub.broadcast_usage()
        payload = m["broadcast_ws"].call_args.args[0]
        assert payload["type"] == "usage_updated"
        assert payload["total"]["cost_usd"] == 0.35
        # Total tokens is `w.usage.total_tokens` — derived. Just verify
        # both workers contributed.
        assert set(payload["workers"].keys()) == {"a", "b"}


class TestTunnelStateChange:
    def test_running_broadcasts_url(self) -> None:
        pub, m = _make_publisher()
        pub.on_tunnel_state_change(TunnelState.RUNNING, "https://x.tunnel")
        m["broadcast_ws"].assert_called_once_with(
            {"type": "tunnel_started", "url": "https://x.tunnel"}
        )

    def test_stopped_broadcasts_without_url(self) -> None:
        pub, m = _make_publisher()
        pub.on_tunnel_state_change(TunnelState.STOPPED, "")
        m["broadcast_ws"].assert_called_once_with({"type": "tunnel_stopped"})

    def test_error_broadcasts_message(self) -> None:
        pub, m = _make_publisher()
        pub.on_tunnel_state_change(TunnelState.ERROR, "boom")
        m["broadcast_ws"].assert_called_once_with({"type": "tunnel_error", "error": "boom"})


# ---------------------------------------------------------------------------
# Drone-log → toast / notification routing
# ---------------------------------------------------------------------------


class TestOnDroneEntry:
    def _entry(
        self,
        action: SystemAction = SystemAction.OPERATOR,
        detail: str = "hello",
        is_notification: bool = False,
        category: LogCategory = LogCategory.OPERATOR,
    ) -> MagicMock:
        e = MagicMock()
        e.action = action
        e.worker_name = "alpha"
        e.detail = detail
        e.category = category
        e.is_notification = is_notification
        return e

    def test_non_notification_broadcasts_only(self) -> None:
        pub, m = _make_publisher()
        pub.on_drone_entry(self._entry(detail="line1\nline2", is_notification=False))
        m["broadcast_ws"].assert_called_once()
        payload = m["broadcast_ws"].call_args.args[0]
        assert payload["type"] == "system_log"
        assert payload["detail"] == "line1"  # terse
        m["push_notification"].assert_not_called()

    def test_notification_also_pushes(self) -> None:
        pub, m = _make_publisher()
        pub.on_drone_entry(self._entry(is_notification=True))
        m["push_notification"].assert_called_once()
        kwargs = m["push_notification"].call_args.kwargs
        assert kwargs["worker"] == "alpha"
        assert kwargs["message"] == "hello"
        assert kwargs["priority"] == "medium"

    def test_stung_is_high_priority(self) -> None:
        pub, m = _make_publisher()
        pub.on_drone_entry(self._entry(action=SystemAction.WORKER_STUNG, is_notification=True))
        assert m["push_notification"].call_args.kwargs["priority"] == "high"

    def test_failed_task_is_high_priority(self) -> None:
        pub, m = _make_publisher()
        pub.on_drone_entry(self._entry(action=SystemAction.TASK_FAILED, is_notification=True))
        assert m["push_notification"].call_args.kwargs["priority"] == "high"


# ---------------------------------------------------------------------------
# State-change side effects (the BUZZING expire-proposals path + STUNG log)
# ---------------------------------------------------------------------------


class TestOnStateChanged:
    def test_buzzing_clears_inflight_and_expires_stale_proposals(self) -> None:
        from swarm.tasks.proposal import ProposalStatus, ProposalType

        stale_escalation = MagicMock()
        stale_escalation.id = "p-esc"
        stale_escalation.proposal_type = ProposalType.ESCALATION
        stale_escalation.status = ProposalStatus.PENDING
        stale_completion = MagicMock()
        stale_completion.id = "p-comp"
        stale_completion.proposal_type = ProposalType.COMPLETION
        stale_completion.status = ProposalStatus.PENDING
        unrelated = MagicMock()
        unrelated.id = "p-other"
        unrelated.proposal_type = ProposalType.ASSIGNMENT
        unrelated.status = ProposalStatus.PENDING

        pub, m = _make_publisher(pending=[stale_escalation, stale_completion, unrelated])
        worker = _make_worker("alpha", state=WorkerState.BUZZING)
        pub.on_state_changed(worker)

        m["clear_worker_inflight"].assert_called_once_with("alpha")
        # Only the escalation + completion get expired
        expired_ids = {c.args[0] for c in m["update_proposal_status"].call_args_list}
        assert expired_ids == {"p-esc", "p-comp"}
        m["clear_resolved_proposals"].assert_called_once()
        m["broadcast_proposals"].assert_called_once()

    def test_buzzing_with_no_stale_does_not_clear_or_broadcast(self) -> None:
        pub, m = _make_publisher(pending=[])
        worker = _make_worker("alpha", state=WorkerState.BUZZING)
        pub.on_state_changed(worker)
        m["clear_worker_inflight"].assert_called_once()
        m["clear_resolved_proposals"].assert_not_called()
        m["broadcast_proposals"].assert_not_called()

    def test_resting_does_not_touch_proposals(self) -> None:
        pub, m = _make_publisher()
        worker = _make_worker("alpha", state=WorkerState.RESTING)
        pub.on_state_changed(worker)
        m["clear_worker_inflight"].assert_not_called()
        m["pending_for_worker"].assert_not_called()

    def test_stung_logs_to_drone_log(self) -> None:
        pub, m = _make_publisher()
        worker = _make_worker("alpha", state=WorkerState.STUNG)
        worker.process = MagicMock()
        worker.process.get_content.return_value = "last-line"
        pub.on_state_changed(worker)
        m["drone_log"].add.assert_called_once()
        args = m["drone_log"].add.call_args
        assert args.args[0] == SystemAction.WORKER_STUNG
        assert args.args[1] == "alpha"
        assert "last-line" in args.args[2]

    def test_state_change_invokes_mark_dirty_callback(self) -> None:
        pub, m = _make_publisher()
        worker = _make_worker("alpha", state=WorkerState.RESTING)
        pub.on_state_changed(worker)
        m["mark_dirty"].assert_called_once()


# ---------------------------------------------------------------------------
# Internal debounce (when mark_dirty callback is NOT injected)
# ---------------------------------------------------------------------------


class TestInternalDebounce:
    def _make_no_external_cb(self) -> tuple[StatePublisher, dict[str, MagicMock]]:
        pub, mocks = _make_publisher()
        # Force the publisher to use its internal _mark_state_dirty path
        pub._mark_dirty_cb = None
        return pub, mocks

    def test_flush_when_clean_is_noop(self) -> None:
        pub, m = self._make_no_external_cb()
        # publisher is clean by default
        pub._flush_state_broadcast()
        # No state broadcast should have been emitted
        m["broadcast_ws"].assert_not_called()

    def test_mark_state_dirty_outside_event_loop_flushes_immediately(self) -> None:
        # In sync context with no running loop, _mark_state_dirty falls
        # through to immediate flush.
        pub, m = self._make_no_external_cb()
        worker = _make_worker("alpha")
        pub.on_state_changed(worker)
        # Should have flushed: state broadcast fired
        state_calls = [c for c in m["broadcast_ws"].call_args_list if c.args[0]["type"] == "state"]
        assert len(state_calls) == 1
        assert pub._state_dirty is False
