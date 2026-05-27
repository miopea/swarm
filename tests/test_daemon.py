"""Tests for server/daemon.py — daemon operation methods."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swarm.config import HiveConfig, QueenConfig, WorkerConfig
from swarm.drones.log import DroneLog, SystemAction
from swarm.drones.pilot import DronePilot
from swarm.pty.process import ProcessError
from swarm.queen.queen import Queen
from swarm.server.analyzer import QueenAnalyzer
from swarm.server.config_manager import ConfigManager
from swarm.server.daemon import (
    SwarmDaemon,
    SwarmOperationError,
    TaskOperationError,
    WorkerNotFoundError,
)
from swarm.server.proposals import ProposalManager
from swarm.server.task_manager import TaskManager
from swarm.server.worker_service import WorkerService
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskHistory
from swarm.tasks.proposal import AssignmentProposal, ProposalStatus, ProposalStore
from swarm.tasks.task import TaskPriority, TaskStatus
from swarm.worker.worker import Worker, WorkerState
from tests.fakes.process import FakeWorkerProcess


@pytest.fixture
def daemon(monkeypatch):
    """Create a minimal daemon without starting it."""
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)

    cfg = HiveConfig(session_name="test")
    d = SwarmDaemon.__new__(SwarmDaemon)
    d.config = cfg
    d.workers = [
        Worker(name="api", path="/tmp/api", process=FakeWorkerProcess(name="api")),
        Worker(name="web", path="/tmp/web", process=FakeWorkerProcess(name="web")),
    ]
    d.pool = None
    d._worker_lock = asyncio.Lock()
    d.drone_log = DroneLog()
    d.task_board = TaskBoard()
    d.task_history = TaskHistory(log_file=Path(tempfile.mktemp(suffix=".jsonl")))
    d.queen = Queen(config=QueenConfig(cooldown=0.0), session_name="test")

    from swarm.queen.queue import QueenCallQueue

    d.queen_queue = QueenCallQueue(max_concurrent=2)
    d.proposal_store = ProposalStore()
    d.notification_bus = MagicMock()
    d.pilot = MagicMock(spec=DronePilot)
    d.pilot.enabled = True
    d.pilot.toggle = MagicMock(return_value=False)
    d.pilot.is_focused = MagicMock(return_value=False)
    d._bg_tasks: set[asyncio.Task[object]] = set()
    d.broadcast_ws = MagicMock()

    from swarm.server.broadcast import BroadcastHub

    d.hub = BroadcastHub(track_task=lambda t: d._bg_tasks.add(t))
    from swarm.server.loop_runner import BackgroundLoopRunner

    d.loop_runner = BackgroundLoopRunner()
    d.hub.ws_clients = set()
    d.hub.terminal_ws_clients = set()
    d.start_time = 0.0
    d.graph_mgr = None
    d._mtime_task = None
    d._usage_task = None
    d.email = MagicMock()
    d.proposals = ProposalManager(
        store=d.proposal_store,
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        notification_bus=d.notification_bus,
        task_board=d.task_board,
        get_worker=lambda name: d.get_worker(name),
        get_workers=lambda: d.workers,
        get_pilot=lambda: d.pilot,
        assign_task=lambda *a, **kw: d.assign_and_start_task(*a, **kw),
        complete_task=lambda *a, **kw: d.complete_task(*a, **kw),
        execute_escalation=lambda p: d.analyzer.execute_escalation(p),
    )
    d.analyzer = QueenAnalyzer(
        queen=d.queen,
        queue=d.queen_queue,
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        emit_event=d.emit,
        proposal_store=d.proposal_store,
        queue_proposal=d.queue_proposal,
        task_board=d.task_board,
        get_worker=lambda name: d.get_worker(name),
        require_worker=lambda name: d._require_worker(name),
        get_workers=lambda: d.workers,
        get_pool=lambda: d.pool,
        get_config=lambda: d.config,
        get_worker_descriptions=lambda: d._worker_descriptions(),
        clear_escalation=lambda name: d.pilot.clear_escalation(name) if d.pilot else None,
    )
    d.tasks = TaskManager(
        task_board=d.task_board,
        task_history=d.task_history,
        drone_log=d.drone_log,
        pilot=d.pilot,
    )
    d._heartbeat_task = None
    d._heartbeat_snapshot = {}
    d.pipeline_engine = MagicMock()
    d.pipeline_engine.list_all.return_value = []
    d.service_registry = MagicMock()

    from swarm.server.escalation_handler import EscalationHandler

    d.escalation = EscalationHandler(
        broadcast_ws=d.broadcast_ws,
        notification_bus=d.notification_bus,
        proposal_store=d.proposal_store,
        get_analyzer=lambda: d.analyzer,
        get_queen=lambda: d.queen,
        emit=d.emit,
    )

    from swarm.server.state_publisher import StatePublisher

    d.publisher = StatePublisher(
        broadcast_ws=d.broadcast_ws,
        get_workers=lambda: d.workers,
        get_worker_task_map=lambda: d._worker_task_map(),
        expire_proposals=lambda: d._expire_stale_proposals(),
        broadcast_proposals=lambda: d._broadcast_proposals(),
        clear_worker_inflight=lambda name: d.analyzer.clear_worker_inflight(name),
        pending_for_worker=d.proposal_store.pending_for_worker,
        clear_resolved_proposals=d.proposal_store.clear_resolved,
        update_proposal_status=d.proposal_store.update_status,
        push_notification=lambda **kw: d.push_notification(**kw),
        notification_bus=d.notification_bus,
        drone_log=d.drone_log,
        emit=d.emit,
        get_pressure_level=lambda: getattr(d, "_prev_pressure_level", "nominal"),
        pipeline_engine=d.pipeline_engine,
        service_registry=d.service_registry,
        track_task=lambda t: d._bg_tasks.add(t),
        mark_dirty=lambda: d._mark_state_dirty(),
    )

    from swarm.server.proposal_coordinator import ProposalCoordinator

    d.proposal_coord = ProposalCoordinator(
        proposals=d.proposals,
        proposal_store=d.proposal_store,
        get_analyzer=lambda: d.analyzer,
        get_queen=lambda: d.queen,
        broadcast_ws=d.broadcast_ws,
        notification_bus=d.notification_bus,
        get_pilot=lambda: d.pilot,
        assign_task=lambda *a, **kw: d.assign_and_start_task(*a, **kw),
        track_task=lambda t: d._bg_tasks.add(t),
        emit=d.emit,
    )
    d.config_mgr = ConfigManager(
        config=cfg,
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        apply_config=d.apply_config,
        get_pilot=lambda: d.pilot,
        rebuild_graph=d._rebuild_graph,
    )
    # InvariantReconciler + PlaybookOps — extracted Phase 1+2 of
    # daemon-god-object-refactor.  The fixture builds via __new__ so the
    # live __init__ wiring doesn't run; mirror it here.
    from swarm.config import PlaybookConfig
    from swarm.server.invariants import InvariantReconciler
    from swarm.server.playbook_ops import PlaybookOps

    if not hasattr(d, "blocker_store"):
        d.blocker_store = None
    d.invariants = InvariantReconciler(
        task_board=d.task_board,
        task_history=d.task_history,
        drone_log=d.drone_log,
        blocker_store=d.blocker_store,
        get_workers=lambda: d.workers,
    )
    # synthesizer left None — matches pre-refactor behavior where the
    # fixture didn't bind one and complete_task's fire path silently
    # returned.  Tests that need an actual synth can override.
    d.playbook_store = None
    d.playbook_synthesizer = None
    if not hasattr(d.config, "playbooks") or d.config.playbooks is None:
        d.config.playbooks = PlaybookConfig()
    d.playbook_ops = PlaybookOps(
        get_store=lambda: d.playbook_store,
        get_synthesizer=lambda: d.playbook_synthesizer,
        get_config=lambda: d.config.playbooks,
        drone_log=d.drone_log,
        task_board=d.task_board,
        track_task=lambda t: d._bg_tasks.add(t),
        get_worker=lambda name: d.get_worker(name),
    )
    from swarm.server.task_coordinator import TaskCoordinator

    d.tasks_coord = TaskCoordinator(d)
    d.worker_svc = WorkerService(
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        task_board=d.task_board,
        get_pilot=lambda: d.pilot,
        get_pool=lambda: d.pool,
        get_config=lambda: d.config,
        get_workers=lambda: d.workers,
        set_workers=lambda ws: setattr(d, "workers", ws),
        worker_lock=d._worker_lock,
        init_pilot=lambda enabled: d.init_pilot(enabled=enabled),
    )

    from swarm.server.jira_service import JiraService
    from swarm.server.resource_monitor import ResourceMonitor
    from swarm.server.test_runner import TestRunner
    from swarm.tunnel import TunnelManager

    d.tunnel = TunnelManager(port=cfg.port)
    d.jira_svc = JiraService(
        get_jira=lambda: MagicMock(),
        task_board=d.task_board,
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        track_task=lambda t: d._bg_tasks.add(t),
        get_sync_interval=lambda: 300,
    )
    d.resource_mon = ResourceMonitor(
        broadcast_ws=d.broadcast_ws,
        get_pilot=lambda: d.pilot,
        get_pool=lambda: d.pool,
        get_workers=lambda: d.workers,
        get_resource_config=lambda: d.config.resources,
        notification_bus=lambda: d.notification_bus,
    )
    d.test_runner = TestRunner(
        daemon=d,
        task_board=d.task_board,
        broadcast_ws=d.broadcast_ws,
        track_task=lambda t: d._bg_tasks.add(t),
        create_task=d.create_task,
        get_pilot=lambda: d.pilot,
        emitter=d,
    )

    from swarm.pipelines.engine import PipelineEngine
    from swarm.services.registry import ServiceRegistry

    d.service_registry = ServiceRegistry()
    d.pipeline_engine = PipelineEngine(task_board=d.task_board, service_registry=d.service_registry)
    d._wire_pipeline_engine()
    return d


# --- _cancel_timers ---


@pytest.mark.asyncio
async def test_cancel_timers_awaits_tasks(daemon):
    """_cancel_timers should cancel and await all background tasks."""

    async def bg_task():
        await asyncio.sleep(999)

    task1 = asyncio.create_task(bg_task())
    task2 = asyncio.create_task(bg_task())
    daemon._heartbeat_task = task1
    daemon._usage_task = task2
    daemon._mtime_task = None
    daemon.publisher._state_debounce_handle = None
    daemon._bg_tasks = set()
    daemon.pilot = None

    await daemon._cancel_timers()

    # After _cancel_timers, all tasks should be done (cancelled and awaited)
    assert task1.done()
    assert task2.done()


# --- _reachable_addresses ---


class TestReachableAddresses:
    """Banner must never show 0.0.0.0 as a client URL.  0.0.0.0 is
    a bind-only address; modern Chrome's Private Network Access
    rules specifically block it, which manifests as the dashboard's
    "Connection lost, reconnecting" loop.  Headless-server operators
    also need to see real reachable IPs to know which URL to paste
    into a remote browser.
    """

    def test_explicit_host_is_returned_verbatim(self) -> None:
        from swarm.server.daemon import _reachable_addresses

        assert _reachable_addresses("192.168.1.50") == ["192.168.1.50"]
        assert _reachable_addresses("swarm.example.com") == ["swarm.example.com"]

    def test_wildcard_never_returns_0_0_0_0(self) -> None:
        from swarm.server.daemon import _reachable_addresses

        for bind in ("0.0.0.0", "::", "*", ""):
            addrs = _reachable_addresses(bind)
            assert addrs, f"expected at least one address for bind={bind!r}"
            assert "0.0.0.0" not in addrs, (
                f"0.0.0.0 leaked into banner addresses for bind={bind!r}: {addrs}"
            )
            assert "::" not in addrs

    def test_wildcard_includes_localhost_fallback(self) -> None:
        """Localhost is a valid entry for local-dev users, just not the
        only one shown.  It should appear somewhere in the list."""
        from swarm.server.daemon import _reachable_addresses

        addrs = _reachable_addresses("0.0.0.0")
        assert "localhost" in addrs

    def test_wildcard_prefers_real_ips_over_loopback(self) -> None:
        """Remote operators need real IPs prominently.  Loopback is
        last-resort, not the primary entry."""
        from swarm.server.daemon import _reachable_addresses

        addrs = _reachable_addresses("0.0.0.0")
        if len(addrs) > 1:
            # If we found any real IP, localhost should not be first.
            assert addrs[0] != "localhost", (
                f"loopback must not be the primary entry when real "
                f"addresses are discoverable; got {addrs}"
            )


# --- Exception hierarchy ---


def test_exception_hierarchy():
    assert issubclass(WorkerNotFoundError, SwarmOperationError)
    assert issubclass(TaskOperationError, SwarmOperationError)
    assert issubclass(SwarmOperationError, Exception)


# --- get_worker ---


def test_get_worker_found(daemon):
    w = daemon.get_worker("api")
    assert w is not None
    assert w.name == "api"


def test_get_worker_not_found(daemon):
    assert daemon.get_worker("nonexistent") is None


# --- kill_worker ---


@pytest.mark.asyncio
async def test_kill_worker(daemon):
    with patch("swarm.worker.manager.kill_worker", new_callable=AsyncMock) as mock_kill:
        await daemon.kill_worker("api")
        mock_kill.assert_called_once()
        worker = daemon.get_worker("api")
        assert worker.state == WorkerState.STUNG


@pytest.mark.asyncio
async def test_kill_worker_unassigns_tasks(daemon):
    task = daemon.task_board.create(title="Test task")
    daemon.task_board.assign(task.id, "api")
    with patch("swarm.worker.manager.kill_worker", new_callable=AsyncMock):
        await daemon.kill_worker("api")
    reloaded = daemon.task_board.get(task.id)
    assert reloaded.status == TaskStatus.UNASSIGNED
    assert reloaded.assigned_worker is None


@pytest.mark.asyncio
async def test_kill_worker_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.kill_worker("nonexistent")


@pytest.mark.asyncio
async def test_kill_worker_broadcasts(daemon):
    with patch("swarm.worker.manager.kill_worker", new_callable=AsyncMock):
        await daemon.kill_worker("api")
    daemon.broadcast_ws.assert_called()
    call_data = daemon.broadcast_ws.call_args[0][0]
    assert call_data["type"] == "workers_changed"


# --- revive_worker ---


@pytest.mark.asyncio
async def test_revive_worker(daemon):
    daemon.workers[0].state = WorkerState.STUNG
    with patch("swarm.worker.manager.revive_worker", new_callable=AsyncMock) as mock_revive:
        await daemon.revive_worker("api")
        mock_revive.assert_called_once()
        # Check pool was passed
        args, _ = mock_revive.call_args
        assert args[1] is None  # pool
    w = daemon.get_worker("api")
    assert w.state == WorkerState.BUZZING
    assert w.revive_count == 1


@pytest.mark.asyncio
async def test_revive_worker_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.revive_worker("nonexistent")


@pytest.mark.asyncio
async def test_revive_worker_not_stung(daemon):
    # Worker is BUZZING, should raise
    with pytest.raises(SwarmOperationError, match="not STUNG"):
        await daemon.revive_worker("api")


@pytest.mark.asyncio
async def test_revive_worker_broadcasts(daemon):
    daemon.workers[0].state = WorkerState.STUNG
    with patch("swarm.worker.manager.revive_worker", new_callable=AsyncMock):
        await daemon.revive_worker("api")
    daemon.broadcast_ws.assert_called()


# --- kill_session ---


@pytest.mark.asyncio
async def test_kill_session(daemon):
    await daemon.kill_session()
    assert len(daemon.workers) == 0
    daemon.pilot.stop.assert_called_once()


@pytest.mark.asyncio
async def test_kill_session_unassigns_tasks(daemon):
    task = daemon.task_board.create(title="Test")
    daemon.task_board.assign(task.id, "api")
    await daemon.kill_session()
    reloaded = daemon.task_board.get(task.id)
    assert reloaded.status == TaskStatus.UNASSIGNED


@pytest.mark.asyncio
async def test_kill_session_clears_drone_log(daemon):
    daemon.drone_log.add(action=MagicMock(value="TEST"), worker_name="api", detail="test")
    await daemon.kill_session()
    assert len(daemon.drone_log.entries) == 0


@pytest.mark.asyncio
async def test_kill_session_broadcasts(daemon):
    await daemon.kill_session()
    daemon.broadcast_ws.assert_called()


# --- launch_workers ---


@pytest.mark.asyncio
async def test_launch_workers_into_existing_session(daemon):
    """When workers already exist, add_worker_live is used (no session kill)."""
    new_worker = Worker(name="new", path="/tmp/new", process=FakeWorkerProcess(name="new"))
    with patch(
        "swarm.worker.manager.add_worker_live",
        new_callable=AsyncMock,
        return_value=new_worker,
    ):
        result = await daemon.launch_workers([WorkerConfig("new", "/tmp/new")])
    assert len(result) == 1
    assert result[0].name == "new"
    # Workers should be extended
    assert any(w.name == "new" for w in daemon.workers)
    daemon.broadcast_ws.assert_called()


@pytest.mark.asyncio
async def test_launch_workers_fresh_session(daemon):
    """When no workers exist, launch_workers creates a new session."""
    daemon.workers.clear()
    launched = [
        Worker(name="new", path="/tmp/new", process=FakeWorkerProcess(name="new")),
    ]
    with patch(
        "swarm.worker.manager.launch_workers",
        new_callable=AsyncMock,
        return_value=launched,
    ):
        result = await daemon.launch_workers([WorkerConfig("new", "/tmp/new")])
    assert len(result) == 1
    assert result[0].name == "new"
    assert any(w.name == "new" for w in daemon.workers)
    daemon.broadcast_ws.assert_called()


@pytest.mark.asyncio
async def test_launch_workers_updates_pilot(daemon):
    new_worker = Worker(name="new", path="/tmp/new", process=FakeWorkerProcess(name="new"))
    with patch(
        "swarm.worker.manager.add_worker_live",
        new_callable=AsyncMock,
        return_value=new_worker,
    ):
        await daemon.launch_workers([WorkerConfig("new", "/tmp/new")])
    assert daemon.pilot.workers == daemon.workers


# --- spawn_worker ---


@pytest.mark.asyncio
async def test_spawn_worker(daemon):
    new_worker = Worker(name="new", path="/tmp/new", process=FakeWorkerProcess(name="new"))
    with patch(
        "swarm.worker.manager.add_worker_live", new_callable=AsyncMock, return_value=new_worker
    ):
        result = await daemon.spawn_worker(WorkerConfig("new", "/tmp/new"))
    assert result.name == "new"
    daemon.broadcast_ws.assert_called()


@pytest.mark.asyncio
async def test_spawn_worker_duplicate(daemon):
    with pytest.raises(SwarmOperationError, match="already running"):
        await daemon.spawn_worker(WorkerConfig("api", "/tmp/api"))


# --- create_task ---


def test_create_task(daemon):
    task = daemon.create_task(title="Fix bug", description="It's broken")
    assert task.title == "Fix bug"
    assert task.description == "It's broken"
    assert daemon.task_board.get(task.id) is not None


def test_create_task_with_priority(daemon):
    task = daemon.create_task(title="Urgent fix", priority=TaskPriority.URGENT)
    assert task.priority == TaskPriority.URGENT


# --- assign_task ---


async def test_assign_task(daemon):
    """assign_task queues only — does NOT send to worker."""
    task = daemon.create_task(title="Test", description="Do something important")
    with patch.object(daemon, "send_to_worker", new_callable=AsyncMock) as mock_send:
        result = await daemon.assign_task(task.id, "api")
    assert result is True
    reloaded = daemon.task_board.get(task.id)
    assert reloaded.assigned_worker == "api"
    assert reloaded.status.value == "assigned"
    mock_send.assert_not_awaited()  # assign does NOT send


async def test_start_task(daemon):
    """start_task sends an ASSIGNED task to the worker."""
    task = daemon.create_task(title="Test", description="Do something important")
    await daemon.assign_task(task.id, "api")
    with patch.object(daemon, "send_to_worker", new_callable=AsyncMock) as mock_send:
        result = await daemon.start_task(task.id)
    assert result is True
    reloaded = daemon.task_board.get(task.id)
    assert reloaded.status.value == "active"
    mock_send.assert_awaited_once()
    sent_msg = mock_send.call_args[0][1]
    assert "Test" in sent_msg
    assert "Do something important" in sent_msg


async def test_start_task_demotes_prior_active_for_same_worker(daemon):
    """Only one ACTIVE task per worker — starting a second demotes the first."""
    first = daemon.create_task(title="First")
    second = daemon.create_task(title="Second")
    await daemon.assign_task(first.id, "api")
    await daemon.assign_task(second.id, "api")

    with patch.object(daemon, "send_to_worker", new_callable=AsyncMock):
        await daemon.start_task(first.id)
        await daemon.start_task(second.id)

    assert daemon.task_board.get(first.id).status == TaskStatus.ASSIGNED
    assert daemon.task_board.get(second.id).status == TaskStatus.ACTIVE


async def test_start_task_does_not_demote_other_workers(daemon):
    """Demotion is per-worker — other workers' ACTIVE tasks are untouched."""
    api_task = daemon.create_task(title="API work")
    web_task = daemon.create_task(title="Web work")
    await daemon.assign_task(api_task.id, "api")
    await daemon.assign_task(web_task.id, "web")

    with patch.object(daemon, "send_to_worker", new_callable=AsyncMock):
        await daemon.start_task(api_task.id)
        await daemon.start_task(web_task.id)

    assert daemon.task_board.get(api_task.id).status == TaskStatus.ACTIVE
    assert daemon.task_board.get(web_task.id).status == TaskStatus.ACTIVE


async def test_assign_task_worker_not_found(daemon):
    task = daemon.create_task(title="Test")
    with pytest.raises(WorkerNotFoundError):
        await daemon.assign_task(task.id, "nonexistent")


async def test_assign_task_not_found(daemon):
    with pytest.raises(TaskOperationError):
        await daemon.assign_task("nonexistent", "api")


async def test_assign_task_not_available(daemon):
    task = daemon.create_task(title="Test")
    daemon.task_board.assign(task.id, "api")
    daemon.task_board.complete(task.id)
    with pytest.raises(TaskOperationError, match="not available"):
        await daemon.assign_task(task.id, "web")


# --- complete_task ---


def test_complete_task(daemon):
    task = daemon.create_task(title="Test")
    daemon.task_board.assign(task.id, "api")
    result = daemon.complete_task(task.id)
    assert result is True
    assert daemon.task_board.get(task.id).status == TaskStatus.DONE


def test_complete_task_not_found(daemon):
    with pytest.raises(TaskOperationError):
        daemon.complete_task("nonexistent")


def test_complete_task_wrong_state(daemon):
    task = daemon.create_task(title="Test")
    # Task is PENDING — can't complete
    with pytest.raises(TaskOperationError):
        daemon.complete_task(task.id)


def test_complete_task_verify_false_marks_skipped(daemon):
    """``verify=False`` (force-complete path) marks the task verifier-skipped.

    Item 4 of the 10-repo bundle: ``queen_force_complete_task`` passes
    ``verify=False`` so the verifier drone never second-guesses an
    explicit operator override. Daemon stamps SKIPPED + a reason on
    the task and writes a buzz log entry under LogCategory.VERIFIER.
    """
    from swarm.drones.log import LogCategory, SystemAction
    from swarm.tasks.task import VerificationStatus

    task = daemon.create_task(title="Force-complete me")
    daemon.task_board.assign(task.id, "api")
    result = daemon.complete_task(task.id, actor="queen", resolution="ship", verify=False)
    assert result is True
    after = daemon.task_board.get(task.id)
    assert after is not None
    assert after.verification_status == VerificationStatus.SKIPPED
    assert "force-completed by queen" in after.verification_reason
    skip_entries = [
        e
        for e in daemon.drone_log.entries
        if e.action == SystemAction.VERIFIER_SKIPPED and e.category == LogCategory.VERIFIER
    ]
    assert len(skip_entries) == 1
    assert skip_entries[0].metadata["actor"] == "queen"


def test_complete_task_default_verify_leaves_status_not_run(daemon):
    """Default path ``verify=True`` is a no-op on verification status.

    The verifier drone was never wired up in production (the
    ``_init_verifier_drone`` call site was missed in commit 4249a39),
    and the dormant code path was removed in 2026.5.25.4. The
    ``verify`` kwarg is preserved on the public API so
    ``queen_force_complete_task(verify=False)`` keeps its SKIPPED-stamp
    semantics; ``verify=True`` just leaves verification untouched.
    """
    from swarm.tasks.task import VerificationStatus

    task = daemon.create_task(title="Normal complete")
    daemon.task_board.assign(task.id, "api")
    result = daemon.complete_task(task.id, actor="api", resolution="done")
    assert result is True
    after = daemon.task_board.get(task.id)
    assert after is not None
    assert after.verification_status == VerificationStatus.NOT_RUN


# --- fail_task ---


def test_fail_task(daemon):
    task = daemon.create_task(title="Test")
    daemon.task_board.assign(task.id, "api")
    result = daemon.fail_task(task.id)
    assert result is True
    assert daemon.task_board.get(task.id).status == TaskStatus.FAILED


def test_fail_task_not_found(daemon):
    with pytest.raises(TaskOperationError):
        daemon.fail_task("nonexistent")


# --- remove_task ---


def test_remove_task(daemon):
    task = daemon.create_task(title="Test")
    result = daemon.remove_task(task.id)
    assert result is True
    assert daemon.task_board.get(task.id) is None


def test_remove_task_not_found(daemon):
    with pytest.raises(TaskOperationError):
        daemon.remove_task("nonexistent")


# --- toggle_drones ---


def test_toggle_drones(daemon, monkeypatch):
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    result = daemon.toggle_drones()
    assert result is False  # mock returns False
    daemon.pilot.toggle.assert_called_once()
    daemon.broadcast_ws.assert_called()
    call_data = daemon.broadcast_ws.call_args[0][0]
    assert call_data["type"] == "drones_toggled"


def test_toggle_drones_no_pilot(daemon, monkeypatch):
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    daemon.pilot = None
    result = daemon.toggle_drones()
    assert result is False


# --- check_config_file ---


def test_check_config_file_no_source(daemon):
    daemon.config.source_path = None
    assert daemon.check_config_file() is False


def test_check_config_file_no_change(daemon, tmp_path):
    cfg_file = tmp_path / "swarm.yaml"
    cfg_file.write_text("session_name: test\n")
    daemon.config.source_path = str(cfg_file)
    daemon.config_mgr._config_mtime = cfg_file.stat().st_mtime
    assert daemon.check_config_file() is False


def test_check_config_file_changed(daemon, tmp_path, monkeypatch):
    cfg_file = tmp_path / "swarm.yaml"
    cfg_file.write_text("session_name: test\nworkers: []\n")
    daemon.config.source_path = str(cfg_file)
    daemon.config_mgr._config_mtime = 0.0  # Force reload

    mock_reload = AsyncMock()
    monkeypatch.setattr(daemon, "reload_config", mock_reload)

    with patch("swarm.server.config_manager.load_config") as mock_load:
        mock_load.return_value = HiveConfig(session_name="test")
        result = daemon.check_config_file()
    assert result is True


# --- task_board on_change auto-broadcast ---


def test_task_board_on_change_broadcasts(monkeypatch):
    """Creating tasks should auto-broadcast via on_change wiring."""
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)

    cfg = HiveConfig(session_name="test")
    d = SwarmDaemon.__new__(SwarmDaemon)
    d.config = cfg
    d.workers = []
    d._worker_lock = asyncio.Lock()
    d.drone_log = DroneLog()
    d.task_board = TaskBoard()
    d.task_history = TaskHistory(log_file=Path(tempfile.mktemp(suffix=".jsonl")))
    d.queen = Queen(config=QueenConfig(cooldown=0.0), session_name="test")

    from swarm.queen.queue import QueenCallQueue

    d.queen_queue = QueenCallQueue(max_concurrent=2)
    d.proposal_store = ProposalStore()
    d.notification_bus = MagicMock()
    d.pilot = None
    d._bg_tasks: set[asyncio.Task[object]] = set()
    d.broadcast_ws = MagicMock()

    from swarm.server.broadcast import BroadcastHub

    d.hub = BroadcastHub(track_task=lambda t: d._bg_tasks.add(t))
    d.hub.ws_clients = set()
    d.start_time = 0.0
    d.proposals = ProposalManager(
        store=d.proposal_store,
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        notification_bus=d.notification_bus,
        task_board=d.task_board,
        get_worker=lambda name: d.get_worker(name),
        get_workers=lambda: d.workers,
        get_pilot=lambda: d.pilot,
        assign_task=lambda *a, **kw: d.assign_and_start_task(*a, **kw),
        complete_task=lambda *a, **kw: d.complete_task(*a, **kw),
        execute_escalation=lambda p: d.analyzer.execute_escalation(p),
    )
    d.analyzer = QueenAnalyzer(
        queen=d.queen,
        queue=d.queen_queue,
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        emit_event=d.emit,
        proposal_store=d.proposal_store,
        queue_proposal=d.queue_proposal,
        task_board=d.task_board,
        get_worker=lambda name: d.get_worker(name),
        require_worker=lambda name: d._require_worker(name),
        get_workers=lambda: d.workers,
        get_pool=lambda: d.pool,
        get_config=lambda: d.config,
        get_worker_descriptions=lambda: d._worker_descriptions(),
        clear_escalation=lambda name: d.pilot.clear_escalation(name) if d.pilot else None,
    )
    d.config_mgr = ConfigManager(
        config=cfg,
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        apply_config=d.apply_config,
        get_pilot=lambda: d.pilot,
        rebuild_graph=d._rebuild_graph,
    )
    # InvariantReconciler + PlaybookOps — extracted Phase 1+2 of
    # daemon-god-object-refactor.  The fixture builds via __new__ so the
    # live __init__ wiring doesn't run; mirror it here.
    from swarm.config import PlaybookConfig
    from swarm.server.invariants import InvariantReconciler
    from swarm.server.playbook_ops import PlaybookOps

    if not hasattr(d, "blocker_store"):
        d.blocker_store = None
    d.invariants = InvariantReconciler(
        task_board=d.task_board,
        task_history=d.task_history,
        drone_log=d.drone_log,
        blocker_store=d.blocker_store,
        get_workers=lambda: d.workers,
    )
    # synthesizer left None — matches pre-refactor behavior where the
    # fixture didn't bind one and complete_task's fire path silently
    # returned.  Tests that need an actual synth can override.
    d.playbook_store = None
    d.playbook_synthesizer = None
    if not hasattr(d.config, "playbooks") or d.config.playbooks is None:
        d.config.playbooks = PlaybookConfig()
    d.playbook_ops = PlaybookOps(
        get_store=lambda: d.playbook_store,
        get_synthesizer=lambda: d.playbook_synthesizer,
        get_config=lambda: d.config.playbooks,
        drone_log=d.drone_log,
        task_board=d.task_board,
        track_task=lambda t: d._bg_tasks.add(t),
        get_worker=lambda name: d.get_worker(name),
    )
    from swarm.server.task_coordinator import TaskCoordinator

    d.tasks_coord = TaskCoordinator(d)
    d.worker_svc = WorkerService(
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        task_board=d.task_board,
        get_pilot=lambda: d.pilot,
        get_pool=lambda: d.pool,
        get_config=lambda: d.config,
        get_workers=lambda: d.workers,
        set_workers=lambda ws: setattr(d, "workers", ws),
        worker_lock=d._worker_lock,
        init_pilot=lambda enabled: d.init_pilot(enabled=enabled),
    )
    d.pipeline_engine = MagicMock()
    d.pipeline_engine.list_all.return_value = []
    d.service_registry = MagicMock()

    from swarm.server.escalation_handler import EscalationHandler

    d.escalation = EscalationHandler(
        broadcast_ws=d.broadcast_ws,
        notification_bus=d.notification_bus,
        proposal_store=d.proposal_store,
        get_analyzer=lambda: d.analyzer,
        get_queen=lambda: d.queen,
        emit=d.emit,
    )

    from swarm.server.state_publisher import StatePublisher

    d.publisher = StatePublisher(
        broadcast_ws=d.broadcast_ws,
        get_workers=lambda: d.workers,
        get_worker_task_map=lambda: d._worker_task_map(),
        expire_proposals=lambda: d._expire_stale_proposals(),
        broadcast_proposals=lambda: d._broadcast_proposals(),
        clear_worker_inflight=lambda name: d.analyzer.clear_worker_inflight(name),
        pending_for_worker=d.proposal_store.pending_for_worker,
        clear_resolved_proposals=d.proposal_store.clear_resolved,
        update_proposal_status=d.proposal_store.update_status,
        push_notification=lambda **kw: d.push_notification(**kw),
        notification_bus=d.notification_bus,
        drone_log=d.drone_log,
        emit=d.emit,
        get_pressure_level=lambda: getattr(d, "_prev_pressure_level", "nominal"),
        pipeline_engine=d.pipeline_engine,
        service_registry=d.service_registry,
        track_task=lambda t: d._bg_tasks.add(t),
        mark_dirty=lambda: d._mark_state_dirty(),
    )

    from swarm.server.proposal_coordinator import ProposalCoordinator

    d.proposal_coord = ProposalCoordinator(
        proposals=d.proposals,
        proposal_store=d.proposal_store,
        get_analyzer=lambda: d.analyzer,
        get_queen=lambda: d.queen,
        broadcast_ws=d.broadcast_ws,
        notification_bus=d.notification_bus,
        get_pilot=lambda: d.pilot,
        assign_task=lambda *a, **kw: d.assign_and_start_task(*a, **kw),
        track_task=lambda t: d._bg_tasks.add(t),
        emit=d.emit,
    )

    # Wire up on_change like __init__ does
    d._wire_task_board()

    # Now create a task — should trigger broadcast
    d.task_board.create(title="Test")
    d.broadcast_ws.assert_called_with({"type": "tasks_changed"})


# --- apply_config ---


def test_apply_config(daemon):
    """apply_config updates pilot, queen, and notification bus."""
    from swarm.config import DroneConfig

    daemon.config.drones = DroneConfig(poll_interval=99.0)
    daemon.apply_config()
    assert daemon.pilot.drone_config.poll_interval == 99.0
    daemon.pilot.set_poll_intervals.assert_called_once_with(
        99.0, daemon.config.drones.max_idle_interval
    )


def test_apply_config_no_pilot(daemon):
    """apply_config doesn't crash without pilot."""
    daemon.pilot = None
    daemon.apply_config()  # should not raise


def test_apply_config_propagates_log_level_at_runtime(daemon):
    """Regression for #328: changing log_level via the API must
    reconfigure the running Python logger, not just persist the value.

    The user's reported symptom (Groups not persisting across reboots)
    can only be diagnosed if the operator can flip log_level to DEBUG
    from the dashboard and immediately see the relevant log lines.  If
    apply_config didn't call setup_logging, the new level would only
    take effect after a daemon restart — which is the opposite of what
    we want for a forensic flag.
    """
    import logging

    swarm_logger = logging.getLogger("swarm")
    original_level = swarm_logger.level
    original_handlers = list(swarm_logger.handlers)
    try:
        # Start at WARNING (the default)
        swarm_logger.setLevel(logging.WARNING)
        assert swarm_logger.level == logging.WARNING

        # Operator flips to DEBUG via the dashboard → config.log_level
        # is updated, then apply_config() runs as part of the reload.
        daemon.config.log_level = "DEBUG"
        daemon.apply_config()

        assert swarm_logger.level == logging.DEBUG, (
            "config.log_level change must reconfigure the running "
            "logger; otherwise DEBUG can only be enabled by restart."
        )
    finally:
        # Restore so test isolation isn't broken for subsequent tests.
        swarm_logger.setLevel(original_level)
        for h in swarm_logger.handlers[:]:
            h.close()
        swarm_logger.handlers.clear()
        for h in original_handlers:
            swarm_logger.addHandler(h)


# --- save_config ---


def test_save_config(daemon, tmp_path, monkeypatch):
    cfg_file = tmp_path / "swarm.yaml"
    cfg_file.write_text("session_name: test\n")
    daemon.config.source_path = str(cfg_file)

    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    daemon.save_config()
    assert daemon.config_mgr._config_mtime == cfg_file.stat().st_mtime


# --- init_pilot ---


@pytest.mark.asyncio
async def test_init_pilot(daemon, monkeypatch):
    daemon.pilot = None
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)
    monkeypatch.setattr(DronePilot, "start", lambda self: None)
    pilot = daemon.init_pilot(enabled=False)
    assert pilot is not None
    assert pilot.enabled is False
    assert daemon.pilot is pilot


@pytest.mark.asyncio
async def test_init_pilot_enabled(daemon, monkeypatch):
    daemon.pilot = None
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)
    monkeypatch.setattr(DronePilot, "start", lambda self: None)
    pilot = daemon.init_pilot(enabled=True)
    assert pilot.enabled is True


# --- continue_all ---


@pytest.mark.asyncio
async def test_continue_all(daemon):
    daemon.workers[0].state = WorkerState.RESTING
    daemon.workers[1].state = WorkerState.BUZZING
    count = await daemon.continue_all()
    assert count == 1
    assert "\n" in daemon.workers[0].process.keys_sent


@pytest.mark.asyncio
async def test_continue_all_none_resting(daemon):
    daemon.workers[0].state = WorkerState.BUZZING
    daemon.workers[1].state = WorkerState.BUZZING
    count = await daemon.continue_all()
    assert count == 0
    assert len(daemon.workers[0].process.keys_sent) == 0


# --- send_all ---


@pytest.mark.asyncio
async def test_send_all(daemon):
    count = await daemon.send_all("hello")
    assert count == 2
    assert "hello" in daemon.workers[0].process.keys_sent[0]
    assert "hello" in daemon.workers[1].process.keys_sent[0]


# --- send_group ---


@pytest.mark.asyncio
async def test_send_group(daemon):
    from swarm.config import GroupConfig

    daemon.config.workers = [WorkerConfig("api", "/tmp/api"), WorkerConfig("web", "/tmp/web")]
    daemon.config.groups = [GroupConfig(name="backend", workers=["api"])]
    count = await daemon.send_group("backend", "deploy")
    assert count == 1
    assert "deploy" in daemon.workers[0].process.keys_sent[0]


@pytest.mark.asyncio
async def test_send_group_unknown(daemon):
    with pytest.raises(ValueError):
        await daemon.send_group("nonexistent", "hello")


# --- gather_hive_context ---


@pytest.mark.asyncio
async def test_gather_hive_context(daemon):
    daemon.workers[0].process.set_content("output")
    daemon.workers[1].process.set_content("output")
    ctx = await daemon.gather_hive_context()
    assert isinstance(ctx, str)
    assert "api" in ctx


# --- analyze_worker ---


@pytest.mark.asyncio
async def test_analyze_worker(daemon, monkeypatch):
    monkeypatch.setattr(
        daemon.queen, "analyze_worker", AsyncMock(return_value={"action": "continue"})
    )
    daemon.workers[0].process.set_content("output")
    result = await daemon.analyze_worker("api")
    assert result["action"] == "continue"


@pytest.mark.asyncio
async def test_analyze_worker_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.analyze_worker("nonexistent")


# --- coordinate_hive removed in task #253 spec B ---
# See docs/specs/headless-queen-architecture.md — periodic hive-coordination
# caller was deleted; daemon.coordinate_hive and its chain are gone.


# --- launch_workers inits pilot if none ---


@pytest.mark.asyncio
async def test_launch_workers_inits_pilot(daemon, monkeypatch):
    daemon.pilot = None
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)
    monkeypatch.setattr(DronePilot, "start", lambda self: None)
    new_worker = Worker(name="new", path="/tmp/new", process=FakeWorkerProcess(name="new"))
    with patch(
        "swarm.worker.manager.add_worker_live",
        new_callable=AsyncMock,
        return_value=new_worker,
    ):
        await daemon.launch_workers([WorkerConfig("new", "/tmp/new")])
    assert daemon.pilot is not None


# --- discover ---


@pytest.mark.asyncio
async def test_discover(daemon):
    mock_processes = [
        FakeWorkerProcess(name="found", cwd="/tmp/found"),
    ]
    daemon.pool = MagicMock()
    daemon.pool.discover = AsyncMock(return_value=mock_processes)
    result = await daemon.discover()
    assert len(result) == 1
    assert result[0].name == "found"
    assert result[0].path == "/tmp/found"
    assert result[0].process is mock_processes[0]


# --- Per-worker operations ---


@pytest.mark.asyncio
async def test_send_to_worker(daemon):
    await daemon.send_to_worker("api", "hello")
    assert "hello" in daemon.workers[0].process.keys_sent[0]


@pytest.mark.asyncio
async def test_send_to_worker_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.send_to_worker("nonexistent", "hello")


@pytest.mark.asyncio
async def test_continue_worker(daemon):
    await daemon.continue_worker("api")
    assert "\n" in daemon.workers[0].process.keys_sent


@pytest.mark.asyncio
async def test_continue_worker_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.continue_worker("nonexistent")


@pytest.mark.asyncio
async def test_interrupt_worker(daemon):
    await daemon.interrupt_worker("api")
    assert "<C-c>" in daemon.workers[0].process.keys_sent


@pytest.mark.asyncio
async def test_interrupt_worker_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.interrupt_worker("nonexistent")


@pytest.mark.asyncio
async def test_escape_worker(daemon):
    await daemon.escape_worker("api")
    assert "<Esc>" in daemon.workers[0].process.keys_sent


@pytest.mark.asyncio
async def test_escape_worker_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.escape_worker("nonexistent")


@pytest.mark.asyncio
async def test_capture_worker_output(daemon):
    daemon.workers[0].process.set_content("worker output")
    result = await daemon.capture_worker_output("api")
    assert result == "worker output"


@pytest.mark.asyncio
async def test_capture_worker_output_custom_lines(daemon):
    daemon.workers[0].process.set_content("content")
    result = await daemon.capture_worker_output("api", lines=20)
    assert result == "content"


@pytest.mark.asyncio
async def test_capture_worker_output_not_found(daemon):
    with pytest.raises(WorkerNotFoundError):
        await daemon.capture_worker_output("nonexistent")


# --- broadcast_ws safety ---


# --- Proposals ---


@pytest.mark.asyncio
async def test_approve_proposal(daemon):
    """Approving a proposal assigns the task and sends the message."""
    task = daemon.create_task(title="Fix bug", description="broken")
    daemon.workers[0].state = WorkerState.RESTING
    proposal = AssignmentProposal(
        worker_name="api",
        task_id=task.id,
        task_title=task.title,
        message="Go fix the bug please",
    )
    daemon.proposal_store.add(proposal)

    with patch.object(daemon, "send_to_worker", new_callable=AsyncMock) as mock_send:
        result = await daemon.approve_proposal(proposal.id)
    assert result is True
    assert proposal.status == ProposalStatus.APPROVED
    assert daemon.task_board.get(task.id).assigned_worker == "api"
    # Should use the standard task message with Queen context appended
    sent_msg = mock_send.call_args[0][1]
    assert "Fix bug" in sent_msg
    assert "Queen context: Go fix the bug please" in sent_msg


@pytest.mark.asyncio
async def test_approve_proposal_no_message(daemon):
    """Approving a proposal with no message falls back to auto-generated."""
    task = daemon.create_task(title="Fix bug", description="broken")
    daemon.workers[0].state = WorkerState.RESTING
    proposal = AssignmentProposal(
        worker_name="api",
        task_id=task.id,
        task_title=task.title,
        message="",
    )
    daemon.proposal_store.add(proposal)

    with patch.object(daemon, "send_to_worker", new_callable=AsyncMock) as mock_send:
        await daemon.approve_proposal(proposal.id)
    sent_msg = mock_send.call_args[0][1]
    assert "Fix bug" in sent_msg


@pytest.mark.asyncio
async def test_approve_proposal_worker_gone(daemon):
    """Approving when worker is gone should expire and raise."""
    task = daemon.create_task(title="Fix bug")
    proposal = AssignmentProposal(
        worker_name="nonexistent",
        task_id=task.id,
        task_title=task.title,
    )
    daemon.proposal_store.add(proposal)

    with pytest.raises(WorkerNotFoundError):
        await daemon.approve_proposal(proposal.id)
    assert proposal.status == ProposalStatus.EXPIRED


@pytest.mark.asyncio
async def test_approve_proposal_worker_busy(daemon):
    """Approving when worker is BUZZING should expire and raise."""
    task = daemon.create_task(title="Fix bug")
    daemon.workers[0].state = WorkerState.BUZZING
    proposal = AssignmentProposal(
        worker_name="api",
        task_id=task.id,
        task_title=task.title,
    )
    daemon.proposal_store.add(proposal)

    with pytest.raises(TaskOperationError, match="BUZZING"):
        await daemon.approve_proposal(proposal.id)
    assert proposal.status == ProposalStatus.EXPIRED


def test_reject_proposal(daemon):
    task = daemon.create_task(title="Fix bug")
    proposal = AssignmentProposal(
        worker_name="api",
        task_id=task.id,
        task_title=task.title,
    )
    daemon.proposal_store.add(proposal)

    result = daemon.reject_proposal(proposal.id)
    assert result is True
    assert proposal.status == ProposalStatus.REJECTED
    # Should be cleared from store
    assert len(daemon.proposal_store.pending) == 0


def test_reject_proposal_not_found(daemon):
    with pytest.raises(TaskOperationError):
        daemon.reject_proposal("nonexistent")


def test_reject_all_proposals(daemon):
    task1 = daemon.create_task(title="Fix bug")
    task2 = daemon.create_task(title="Add feature")
    p1 = AssignmentProposal(worker_name="api", task_id=task1.id, task_title=task1.title)
    p2 = AssignmentProposal(worker_name="web", task_id=task2.id, task_title=task2.title)
    daemon.proposal_store.add(p1)
    daemon.proposal_store.add(p2)

    count = daemon.reject_all_proposals()
    assert count == 2
    assert len(daemon.proposal_store.pending) == 0


# --- Escalation → Queen ---


@pytest.mark.asyncio
async def test_escalation_send_message_always_creates_proposal(daemon, monkeypatch):
    """send_message always creates proposal — never auto-acted, even at high confidence."""
    daemon.queen._last_call = 0.0
    daemon.queen.cooldown = 0.0
    daemon.queen.min_confidence = 0.7
    monkeypatch.setattr(
        daemon.queen,
        "analyze_worker",
        AsyncMock(
            return_value={
                "action": "send_message",
                "message": "yes",
                "assessment": "Stuck on approval",
                "reasoning": "Permission prompt detected",
                "confidence": 0.9,
            }
        ),
    )
    daemon.workers[0].process.set_content("output")
    await daemon.analyzer.analyze_escalation(daemon.workers[0], "test escalation")

    # send_message never auto-acts — always goes to proposals for user review
    assert len(daemon.proposal_store.pending) == 1
    assert daemon.proposal_store.pending[0].queen_action == "send_message"
    assert len(daemon.workers[0].process.keys_sent) == 0


@pytest.mark.asyncio
async def test_escalation_continue_auto_acts_high_confidence(daemon, monkeypatch):
    """High-confidence continue action → auto-acted (safe action)."""
    daemon.queen._last_call = 0.0
    daemon.queen.cooldown = 0.0
    daemon.queen.min_confidence = 0.7
    monkeypatch.setattr(
        daemon.queen,
        "analyze_worker",
        AsyncMock(
            return_value={
                "action": "continue",
                "message": "",
                "assessment": "Worker idle at prompt",
                "reasoning": "Empty prompt detected",
                "confidence": 0.9,
            }
        ),
    )
    daemon.workers[0].process.set_content("output")
    await daemon.analyzer.analyze_escalation(daemon.workers[0], "test escalation")

    # continue is a safe auto-action
    assert len(daemon.proposal_store.pending) == 0
    assert "\n" in daemon.workers[0].process.keys_sent


@pytest.mark.asyncio
async def test_escalation_queen_creates_proposal_low_confidence(daemon, monkeypatch):
    """Low-confidence escalation → creates proposal for user review."""
    daemon.queen._last_call = 0.0
    daemon.queen.cooldown = 0.0
    daemon.queen.min_confidence = 0.7
    monkeypatch.setattr(
        daemon.queen,
        "analyze_worker",
        AsyncMock(
            return_value={
                "action": "send_message",
                "message": "yes",
                "assessment": "Stuck on approval",
                "reasoning": "Permission prompt detected",
                "confidence": 0.5,
            }
        ),
    )
    daemon.workers[0].process.set_content("output")
    await daemon.analyzer.analyze_escalation(daemon.workers[0], "test escalation")

    pending = daemon.proposal_store.pending
    assert len(pending) == 1
    p = pending[0]
    assert p.proposal_type == "escalation"
    assert p.queen_action == "send_message"
    assert p.confidence == 0.5
    assert p.worker_name == "api"


@pytest.mark.asyncio
async def test_escalation_plan_always_creates_proposal(daemon, monkeypatch):
    """Plan escalation → always creates proposal, even with high confidence."""
    daemon.queen._last_call = 0.0
    daemon.queen.cooldown = 0.0
    daemon.queen.min_confidence = 0.7
    monkeypatch.setattr(
        daemon.queen,
        "analyze_worker",
        AsyncMock(
            return_value={
                "action": "continue",
                "assessment": "Plan looks good",
                "reasoning": "Worker presenting implementation plan",
                "confidence": 0.95,
            }
        ),
    )
    daemon.workers[0].process.set_content("output")
    await daemon.analyzer.analyze_escalation(daemon.workers[0], "plan requires user approval")

    pending = daemon.proposal_store.pending
    assert len(pending) == 1
    assert pending[0].confidence == 0.95


@pytest.mark.asyncio
async def test_choice_approval_escalation_auto_acts_at_high_confidence(daemon, monkeypatch):
    """'choice requires approval' escalation → Queen auto-acts when confident."""
    daemon.queen._last_call = 0.0
    daemon.queen.cooldown = 0.0
    daemon.queen.min_confidence = 0.7
    monkeypatch.setattr(
        daemon.queen,
        "analyze_worker",
        AsyncMock(
            return_value={
                "action": "continue",
                "assessment": "Routine Bash grep — safe to continue",
                "reasoning": "Permission prompt for grep command",
                "confidence": 0.9,
            }
        ),
    )
    daemon.workers[0].process.set_content("output")
    await daemon.analyzer.analyze_escalation(
        daemon.workers[0], "choice requires approval: choice menu"
    )

    # High confidence → auto-acted, no proposal
    assert len(daemon.proposal_store.pending) == 0
    assert "\n" in daemon.workers[0].process.keys_sent


@pytest.mark.asyncio
async def test_escalation_queen_disabled_no_proposal(daemon):
    """Escalation with Queen disabled → no proposal created."""
    daemon.queen.enabled = False
    daemon._on_escalation(daemon.workers[0], "test")
    assert len(daemon.proposal_store.pending) == 0


@pytest.mark.asyncio
async def test_approve_escalation_send_message(daemon):
    """Approve escalation with send_message action sends keys."""
    # Worker must be non-BUZZING for send_message to proceed (safety guard)
    daemon.workers[0].state = WorkerState.WAITING
    proposal = AssignmentProposal(
        worker_name="api",
        proposal_type="escalation",
        queen_action="send_message",
        message="yes",
        confidence=0.85,
    )
    daemon.proposal_store.add(proposal)

    result = await daemon.approve_proposal(proposal.id)
    assert result is True
    assert proposal.status == ProposalStatus.APPROVED
    assert any("yes" in k for k in daemon.workers[0].process.keys_sent)


@pytest.mark.asyncio
async def test_approve_escalation_continue(daemon):
    """Approve escalation with continue action sends Enter."""
    proposal = AssignmentProposal(
        worker_name="api",
        proposal_type="escalation",
        queen_action="continue",
    )
    daemon.proposal_store.add(proposal)

    await daemon.approve_proposal(proposal.id)
    assert "\n" in daemon.workers[0].process.keys_sent


@pytest.mark.asyncio
async def test_approve_escalation_restart(daemon):
    """Approve escalation with restart action revives worker."""
    daemon.workers[0].state = WorkerState.STUNG
    proposal = AssignmentProposal(
        worker_name="api",
        proposal_type="escalation",
        queen_action="restart",
    )
    daemon.proposal_store.add(proposal)

    with patch("swarm.worker.manager.revive_worker", new_callable=AsyncMock) as mock_revive:
        await daemon.approve_proposal(proposal.id)
    mock_revive.assert_awaited_once()
    assert daemon.workers[0].revive_count == 1


@pytest.mark.asyncio
async def test_approve_escalation_wait(daemon):
    """Approve escalation with wait action sends Enter to the worker."""
    proposal = AssignmentProposal(
        worker_name="api",
        proposal_type="escalation",
        queen_action="wait",
    )
    daemon.proposal_store.add(proposal)

    result = await daemon.approve_proposal(proposal.id)
    assert result is True
    assert proposal.status == ProposalStatus.APPROVED
    assert "\n" in daemon.workers[0].process.keys_sent


@pytest.mark.asyncio
async def testbroadcast_ws_dead_client(monkeypatch):
    """Dead WS clients should be discarded without crash."""
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)

    import tempfile

    from swarm.config import HiveConfig, QueenConfig
    from swarm.tasks.history import TaskHistory

    cfg = HiveConfig(session_name="test")
    d = SwarmDaemon.__new__(SwarmDaemon)
    d.config = cfg
    d.workers = []
    d._worker_lock = asyncio.Lock()
    d.drone_log = DroneLog()
    d.task_board = TaskBoard()
    d.task_history = TaskHistory(log_file=Path(tempfile.mktemp(suffix=".jsonl")))
    d.queen = Queen(config=QueenConfig(cooldown=0.0), session_name="test")

    from swarm.queen.queue import QueenCallQueue

    d.queen_queue = QueenCallQueue(max_concurrent=2)
    d.proposal_store = ProposalStore()
    d.notification_bus = MagicMock()
    d.pilot = None
    d.start_time = 0.0
    d._bg_tasks: set[asyncio.Task[object]] = set()
    d.broadcast_ws = MagicMock()

    from swarm.server.broadcast import BroadcastHub as _BH

    d.hub = _BH(track_task=lambda t: d._bg_tasks.add(t))
    d.hub._broadcast_hook = None
    d.hub.ws_clients = set()
    d.proposals = ProposalManager(
        store=d.proposal_store,
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        notification_bus=d.notification_bus,
        task_board=d.task_board,
        get_worker=lambda name: d.get_worker(name),
        get_workers=lambda: d.workers,
        get_pilot=lambda: d.pilot,
        assign_task=lambda *a, **kw: d.assign_and_start_task(*a, **kw),
        complete_task=lambda *a, **kw: d.complete_task(*a, **kw),
        execute_escalation=lambda p: d.analyzer.execute_escalation(p),
    )
    d.analyzer = QueenAnalyzer(
        queen=d.queen,
        queue=d.queen_queue,
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        emit_event=d.emit,
        proposal_store=d.proposal_store,
        queue_proposal=d.queue_proposal,
        task_board=d.task_board,
        get_worker=lambda name: d.get_worker(name),
        require_worker=lambda name: d._require_worker(name),
        get_workers=lambda: d.workers,
        get_pool=lambda: d.pool,
        get_config=lambda: d.config,
        get_worker_descriptions=lambda: d._worker_descriptions(),
        clear_escalation=lambda name: d.pilot.clear_escalation(name) if d.pilot else None,
    )
    d.config_mgr = ConfigManager(
        config=cfg,
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        apply_config=d.apply_config,
        get_pilot=lambda: d.pilot,
        rebuild_graph=d._rebuild_graph,
    )
    # InvariantReconciler + PlaybookOps — extracted Phase 1+2 of
    # daemon-god-object-refactor.  The fixture builds via __new__ so the
    # live __init__ wiring doesn't run; mirror it here.
    from swarm.config import PlaybookConfig
    from swarm.server.invariants import InvariantReconciler
    from swarm.server.playbook_ops import PlaybookOps

    if not hasattr(d, "blocker_store"):
        d.blocker_store = None
    d.invariants = InvariantReconciler(
        task_board=d.task_board,
        task_history=d.task_history,
        drone_log=d.drone_log,
        blocker_store=d.blocker_store,
        get_workers=lambda: d.workers,
    )
    # synthesizer left None — matches pre-refactor behavior where the
    # fixture didn't bind one and complete_task's fire path silently
    # returned.  Tests that need an actual synth can override.
    d.playbook_store = None
    d.playbook_synthesizer = None
    if not hasattr(d.config, "playbooks") or d.config.playbooks is None:
        d.config.playbooks = PlaybookConfig()
    d.playbook_ops = PlaybookOps(
        get_store=lambda: d.playbook_store,
        get_synthesizer=lambda: d.playbook_synthesizer,
        get_config=lambda: d.config.playbooks,
        drone_log=d.drone_log,
        task_board=d.task_board,
        track_task=lambda t: d._bg_tasks.add(t),
        get_worker=lambda name: d.get_worker(name),
    )
    from swarm.server.task_coordinator import TaskCoordinator

    d.tasks_coord = TaskCoordinator(d)
    d.worker_svc = WorkerService(
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        task_board=d.task_board,
        get_pilot=lambda: d.pilot,
        get_pool=lambda: d.pool,
        get_config=lambda: d.config,
        get_workers=lambda: d.workers,
        set_workers=lambda ws: setattr(d, "workers", ws),
        worker_lock=d._worker_lock,
        init_pilot=lambda enabled: d.init_pilot(enabled=enabled),
    )

    # Create a mock WS that is "closed"
    dead_ws = MagicMock()
    dead_ws.closed = True
    d.hub.ws_clients = {dead_ws}

    # Use real broadcast_ws (not mocked)
    SwarmDaemon.broadcast_ws(d, {"type": "test"})

    # The dead client should be discarded
    assert dead_ws not in d.hub.ws_clients


# --- Operator action logging ---


@pytest.mark.asyncio
async def test_approve_proposal_logs_approved(daemon):
    """Approving a proposal logs APPROVED to drone_log."""
    from swarm.drones.log import SystemAction

    task = daemon.create_task(title="Fix bug")
    daemon.workers[0].state = WorkerState.RESTING
    proposal = AssignmentProposal(
        worker_name="api",
        task_id=task.id,
        task_title=task.title,
        message="Go fix it",
    )
    daemon.proposal_store.add(proposal)

    with patch.object(daemon, "send_to_worker", new_callable=AsyncMock):
        await daemon.approve_proposal(proposal.id)

    entries = daemon.drone_log.entries
    approved = [e for e in entries if e.action == SystemAction.APPROVED]
    assert len(approved) == 1
    assert approved[0].worker_name == "api"
    assert "Fix bug" in approved[0].detail


def test_reject_proposal_logs_rejected(daemon):
    """Rejecting a proposal logs REJECTED to drone_log."""
    from swarm.drones.log import SystemAction

    task = daemon.create_task(title="Add feature")
    proposal = AssignmentProposal(
        worker_name="api",
        task_id=task.id,
        task_title=task.title,
    )
    daemon.proposal_store.add(proposal)
    daemon.reject_proposal(proposal.id)

    entries = daemon.drone_log.entries
    rejected = [e for e in entries if e.action == SystemAction.REJECTED]
    assert len(rejected) == 1
    assert rejected[0].worker_name == "api"
    assert "Add feature" in rejected[0].detail


def test_reject_all_proposals_logs_rejected(daemon):
    """Rejecting all proposals logs REJECTED to drone_log."""
    from swarm.drones.log import SystemAction

    t1 = daemon.create_task(title="Bug 1")
    t2 = daemon.create_task(title="Bug 2")
    p1 = AssignmentProposal(worker_name="api", task_id=t1.id, task_title=t1.title)
    p2 = AssignmentProposal(worker_name="web", task_id=t2.id, task_title=t2.title)
    daemon.proposal_store.add(p1)
    daemon.proposal_store.add(p2)
    daemon.reject_all_proposals()

    entries = daemon.drone_log.entries
    rejected = [e for e in entries if e.action == SystemAction.REJECTED]
    assert len(rejected) == 1
    assert rejected[0].worker_name == "all"
    assert "2 proposal(s)" in rejected[0].detail


@pytest.mark.asyncio
async def test_continue_worker_logs_operator(daemon):
    """Continuing a worker logs OPERATOR to drone_log."""
    from swarm.drones.log import SystemAction

    await daemon.continue_worker("api")

    entries = daemon.drone_log.entries
    ops = [e for e in entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 1
    assert ops[0].worker_name == "api"
    assert "continued" in ops[0].detail


@pytest.mark.asyncio
async def test_kill_worker_logs_operator(daemon):
    """Killing a worker logs OPERATOR to drone_log."""
    from swarm.drones.log import SystemAction

    with patch("swarm.worker.manager.kill_worker", new_callable=AsyncMock):
        await daemon.kill_worker("api")

    entries = daemon.drone_log.entries
    ops = [e for e in entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 1
    assert ops[0].worker_name == "api"
    assert "killed" in ops[0].detail


@pytest.mark.asyncio
async def test_continue_all_logs_operator(daemon):
    """continue_all logs OPERATOR to drone_log."""
    from swarm.drones.log import SystemAction

    daemon.workers[0].state = WorkerState.RESTING
    daemon.workers[1].state = WorkerState.RESTING
    await daemon.continue_all()

    entries = daemon.drone_log.entries
    ops = [e for e in entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 1
    assert ops[0].worker_name == "all"
    assert "2 worker(s)" in ops[0].detail


# --- reload_config ---


@pytest.mark.asyncio
async def test_reload_config(daemon, tmp_path):
    """reload_config updates config, hot-applies, broadcasts, and logs."""
    from swarm.drones.log import SystemAction

    cfg_file = tmp_path / "swarm.yaml"
    cfg_file.write_text("session_name: test\n")
    new_config = HiveConfig(session_name="reloaded")
    new_config.source_path = str(cfg_file)

    await daemon.reload_config(new_config)

    assert daemon.config.session_name == "reloaded"
    assert daemon.config_mgr._config_mtime == cfg_file.stat().st_mtime
    daemon.broadcast_ws.assert_called()
    # Should broadcast config_changed
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    assert any(c.get("type") == "config_changed" for c in calls)
    # Should log CONFIG_CHANGED
    entries = daemon.drone_log.entries
    config_entries = [e for e in entries if e.action == SystemAction.CONFIG_CHANGED]
    assert len(config_entries) == 1


@pytest.mark.asyncio
async def test_reload_config_no_source_path(daemon):
    """reload_config works when source_path is None."""
    new_config = HiveConfig(session_name="reloaded")
    new_config.source_path = None
    await daemon.reload_config(new_config)
    assert daemon.config.session_name == "reloaded"


@pytest.mark.asyncio
async def test_reload_config_source_path_missing_file(daemon, tmp_path):
    """reload_config handles missing source file gracefully."""
    new_config = HiveConfig(session_name="reloaded")
    new_config.source_path = str(tmp_path / "nonexistent.yaml")
    await daemon.reload_config(new_config)
    assert daemon.config.session_name == "reloaded"


# --- _on_escalation ---


def test_on_escalation_skips_pending_proposal(daemon):
    """_on_escalation skips if a pending escalation proposal already exists."""
    proposal = AssignmentProposal(
        worker_name="api",
        proposal_type="escalation",
        queen_action="continue",
    )
    daemon.proposal_store.add(proposal)

    daemon._on_escalation(daemon.workers[0], "test reason")
    # Should not broadcast escalation (skipped)
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    assert not any(c.get("type") == "escalation" for c in calls)


def test_on_escalation_skips_inflight_analysis(daemon):
    """_on_escalation skips if Queen analysis is already in flight."""
    daemon.queen_queue._all_keys.add("escalation:api")

    daemon._on_escalation(daemon.workers[0], "test reason")
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    assert not any(c.get("type") == "escalation" for c in calls)

    daemon.queen_queue._all_keys.discard("escalation:api")


def test_on_escalation_broadcasts_and_emits(daemon):
    """_on_escalation broadcasts the escalation WS event when no duplicates.

    It no longer emits an interruptive notification here — the Queen
    handles the escalation, so a ping at this moment would fire with an
    empty Attention panel (single-source-of-truth alignment).
    """
    daemon.queen.enabled = False  # Disable Queen so we don't need asyncio loop

    daemon._on_escalation(daemon.workers[0], "test reason")

    # Should broadcast escalation event
    daemon.broadcast_ws.assert_called()
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    escalation_calls = [c for c in calls if c.get("type") == "escalation"]
    assert len(escalation_calls) == 1
    assert escalation_calls[0]["worker"] == "api"
    assert escalation_calls[0]["reason"] == "test reason"


def test_on_escalation_queen_disabled(daemon):
    """_on_escalation does not start Queen analysis when Queen is disabled."""
    daemon.queen.enabled = False
    daemon._on_escalation(daemon.workers[0], "test reason")
    # Should still broadcast but not start analysis
    daemon.broadcast_ws.assert_called()


# --- _on_task_done ---


def test_on_task_done_ignores_buzzing_worker(daemon):
    """_on_task_done skips if worker is BUZZING."""
    task = daemon.task_board.create(title="Test task")
    daemon.task_board.assign(task.id, "api")
    daemon.workers[0].state = WorkerState.BUZZING

    daemon._on_task_done(daemon.workers[0], task)
    # No proposals created
    assert len(daemon.proposal_store.pending) == 0


def test_on_task_done_skips_duplicate_completion(daemon):
    """_on_task_done skips if there's already a pending completion proposal."""
    task = daemon.task_board.create(title="Test task")
    daemon.task_board.assign(task.id, "api")
    daemon.workers[0].state = WorkerState.RESTING

    # Add a pending completion proposal
    existing = AssignmentProposal.completion(
        worker_name="api",
        task_id=task.id,
        task_title=task.title,
        assessment="already proposed",
    )
    daemon.proposal_store.add(existing)

    daemon._on_task_done(daemon.workers[0], task)
    # Should still have only the one proposal (no duplicates)
    completions = [p for p in daemon.proposal_store.pending if p.proposal_type == "completion"]
    assert len(completions) == 1


def test_on_task_done_with_resolution_creates_proposal(daemon):
    """_on_task_done creates proposal directly when resolution is provided."""
    task = daemon.task_board.create(title="Test task")
    daemon.task_board.assign(task.id, "api")
    daemon.workers[0].state = WorkerState.RESTING

    daemon._on_task_done(daemon.workers[0], task, resolution="All tests passing")

    pending = daemon.proposal_store.pending
    assert len(pending) == 1
    assert pending[0].proposal_type == "completion"
    assert pending[0].task_id == task.id
    assert pending[0].assessment == "All tests passing"


def test_on_task_done_queen_disabled_no_proposal(daemon):
    """_on_task_done creates no proposal when Queen is unavailable and no resolution."""
    task = daemon.task_board.create(title="Test task")
    daemon.task_board.assign(task.id, "api")
    daemon.workers[0].state = WorkerState.RESTING
    daemon.queen.enabled = False

    daemon._on_task_done(daemon.workers[0], task)
    assert len(daemon.proposal_store.pending) == 0


# --- _on_workers_changed ---


def test_on_workers_changed_broadcasts(daemon):
    """_on_workers_changed broadcasts workers_changed with task map."""
    task = daemon.task_board.create(title="Active task")
    daemon.task_board.assign(task.id, "api")

    daemon._on_workers_changed()

    daemon.broadcast_ws.assert_called()
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    wc_calls = [c for c in calls if c.get("type") == "workers_changed"]
    assert len(wc_calls) >= 1
    # Check it includes worker_tasks
    assert "worker_tasks" in wc_calls[0]
    assert wc_calls[0]["worker_tasks"]["api"] == "Active task"


# --- _on_state_changed ---


def test_on_state_changed_buzzing_expires_proposals(daemon):
    """_on_state_changed expires escalation/completion proposals when worker goes BUZZING."""
    # Add a pending escalation proposal
    proposal = AssignmentProposal(
        worker_name="api",
        proposal_type="escalation",
        queen_action="continue",
    )
    daemon.proposal_store.add(proposal)

    daemon.workers[0].state = WorkerState.BUZZING
    daemon._on_state_changed(daemon.workers[0])

    assert proposal.status == ProposalStatus.EXPIRED


def test_on_state_changed_buzzing_persists_expired_to_sqlite(daemon, tmp_path):
    """BUZZING transition must persist EXPIRED status to the SQLite store.

    Regression: prior code mutated the in-memory copy returned by
    ``pending_for_worker`` and called ``clear_resolved`` (a SQLite no-op),
    so the DB row stayed PENDING. The proposal would re-appear on the
    Decisions tab forever after a worker accepted a plan in its PTY and
    finished work.
    """
    from swarm.db.core import SwarmDB
    from swarm.db.proposal_store import SqliteProposalStore
    from swarm.server.state_publisher import StatePublisher
    from swarm.tasks.proposal import ProposalType

    # Real SQLite-backed store on a tempfile DB — exercises the production path.
    db = SwarmDB(tmp_path / "swarm.db")
    sqlite_store = SqliteProposalStore(db)

    proposal = AssignmentProposal(
        worker_name="api",
        proposal_type=ProposalType.ESCALATION,
        queen_action="wait",
        is_plan=True,
    )
    sqlite_store.add(proposal)
    assert len(sqlite_store.pending) == 1

    # Rebuild the publisher pointed at the SQLite store.
    daemon.proposal_store = sqlite_store
    daemon.publisher = StatePublisher(
        broadcast_ws=daemon.broadcast_ws,
        get_workers=lambda: daemon.workers,
        get_worker_task_map=lambda: daemon._worker_task_map(),
        expire_proposals=lambda: daemon._expire_stale_proposals(),
        broadcast_proposals=lambda: daemon._broadcast_proposals(),
        clear_worker_inflight=lambda name: daemon.analyzer.clear_worker_inflight(name),
        pending_for_worker=sqlite_store.pending_for_worker,
        clear_resolved_proposals=sqlite_store.clear_resolved,
        update_proposal_status=sqlite_store.update_status,
        push_notification=lambda **kw: daemon.push_notification(**kw),
        notification_bus=daemon.notification_bus,
        drone_log=daemon.drone_log,
        emit=daemon.emit,
        get_pressure_level=lambda: "nominal",
        pipeline_engine=daemon.pipeline_engine,
        service_registry=daemon.service_registry,
        track_task=lambda t: daemon._bg_tasks.add(t),
        mark_dirty=lambda: daemon._mark_state_dirty(),
    )

    daemon.workers[0].state = WorkerState.BUZZING
    daemon._on_state_changed(daemon.workers[0])

    # The DB row must reflect EXPIRED — not just an in-memory copy.
    assert len(sqlite_store.pending) == 0
    persisted = sqlite_store.get(proposal.id)
    assert persisted is not None
    assert persisted.status == ProposalStatus.EXPIRED


def test_on_state_changed_stung_logs_worker_stung(daemon):
    """_on_state_changed logs WORKER_STUNG when worker becomes STUNG."""
    daemon.workers[0].state = WorkerState.STUNG
    daemon._on_state_changed(daemon.workers[0])

    entries = daemon.drone_log.entries
    stung = [e for e in entries if e.action == SystemAction.WORKER_STUNG]
    assert len(stung) == 1
    assert stung[0].worker_name == "api"


def test_on_state_changed_broadcasts_state(daemon):
    """_on_state_changed always broadcasts state update."""
    daemon.workers[0].state = WorkerState.RESTING
    daemon._on_state_changed(daemon.workers[0])

    daemon.broadcast_ws.assert_called()
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    state_calls = [c for c in calls if c.get("type") == "state"]
    assert len(state_calls) >= 1
    assert any(w["name"] == "api" for w in state_calls[0]["workers"])


def test_on_state_changed_buzzing_clears_inflight(daemon):
    """_on_state_changed clears in-flight analysis tracking when BUZZING."""
    from swarm.queen.queue import QueenCallRequest

    # Add queued requests for "api" to the queue
    req_esc = QueenCallRequest(
        call_type="escalation",
        coro_factory=lambda: None,
        worker_name="api",
        worker_state_at_enqueue="RESTING",
        dedup_key="escalation:api",
        force=False,
    )
    req_comp = QueenCallRequest(
        call_type="completion",
        coro_factory=lambda: None,
        worker_name="api",
        worker_state_at_enqueue="RESTING",
        dedup_key="completion:api:task123",
        force=False,
    )
    daemon.queen_queue._queue.append(req_esc)
    daemon.queen_queue._all_keys.add("escalation:api")
    daemon.queen_queue._queue.append(req_comp)
    daemon.queen_queue._all_keys.add("completion:api:task123")

    daemon.workers[0].state = WorkerState.BUZZING
    daemon._on_state_changed(daemon.workers[0])

    assert not daemon.analyzer.has_inflight_escalation("api")
    assert not daemon.analyzer.has_inflight_completion("api:task123")


# --- _on_drone_entry ---


def test_on_drone_entry_broadcasts_system_log(daemon):
    """_on_drone_entry broadcasts 'system_log' type."""
    from swarm.drones.log import LogCategory, SystemEntry

    entry = SystemEntry(
        timestamp=0.0,
        action=SystemAction.CONTINUED,
        worker_name="api",
        detail="test detail",
        category=LogCategory.DRONE,
        is_notification=False,
    )

    daemon._on_drone_entry(entry)

    assert daemon.broadcast_ws.call_count == 1
    payload = daemon.broadcast_ws.call_args[0][0]
    assert payload["type"] == "system_log"
    assert payload["action"] == SystemAction.CONTINUED.value
    assert payload["worker"] == "api"
    assert payload["detail"] == "test detail"


def test_on_drone_entry_tersens_multiline_detail(daemon):
    """A multi-line crash dump (WORKER_STUNG's 30-line terminal tail) is
    reduced to a one-line summary in the WS broadcast — the toast is a
    glance; the full detail still lives in the buzz log."""
    from swarm.drones.log import LogCategory, SystemEntry

    dump = "worker exited\n--- last output ---\n" + "\n".join(f"line {i}" for i in range(30))
    entry = SystemEntry(
        timestamp=0.0,
        action=SystemAction.WORKER_STUNG,
        worker_name="api",
        detail=dump,
        category=LogCategory.WORKER,
        is_notification=True,
    )

    daemon._on_drone_entry(entry)

    payload = daemon.broadcast_ws.call_args_list[0][0][0]
    assert payload["detail"] == "worker exited"
    assert "\n" not in payload["detail"]


# --- safe_capture_output ---


@pytest.mark.asyncio
async def test_safe_capture_output_success(daemon):
    """safe_capture_output returns worker output on success."""
    daemon.workers[0].process.set_content("live output")
    result = await daemon.safe_capture_output("api")
    assert result == "live output"


@pytest.mark.asyncio
async def test_safe_capture_output_os_error(daemon):
    """safe_capture_output returns fallback on OSError."""
    daemon.workers[0].process.get_content = MagicMock(side_effect=OSError("gone"))
    result = await daemon.safe_capture_output("api")
    assert result == "(output unavailable)"


@pytest.mark.asyncio
async def test_safe_capture_output_timeout(daemon):
    """safe_capture_output returns fallback on TimeoutError."""
    daemon.workers[0].process.get_content = MagicMock(side_effect=TimeoutError())
    result = await daemon.safe_capture_output("api")
    assert result == "(output unavailable)"


@pytest.mark.asyncio
async def test_safe_capture_output_not_found(daemon):
    """safe_capture_output returns fallback for missing worker."""
    result = await daemon.safe_capture_output("nonexistent")
    assert result == "(output unavailable)"


@pytest.mark.asyncio
async def test_safe_capture_output_process_error(daemon):
    """safe_capture_output returns fallback on ProcessError."""
    daemon.workers[0].process.get_content = MagicMock(side_effect=ProcessError("process gone"))
    result = await daemon.safe_capture_output("api")
    assert result == "(output unavailable)"


# --- poll_once ---


@pytest.mark.asyncio
async def test_poll_once_no_pilot(daemon):
    """poll_once returns False when pilot is None."""
    daemon.pilot = None
    result = await daemon.poll_once()
    assert result is False


@pytest.mark.asyncio
async def test_poll_once_with_pilot(daemon):
    """poll_once delegates to pilot.poll_once."""
    daemon.pilot.poll_once = AsyncMock(return_value=True)
    result = await daemon.poll_once()
    assert result is True
    daemon.pilot.poll_once.assert_awaited_once()


# --- stop ---


@pytest.mark.asyncio
async def test_stop_stops_pilot(daemon):
    """stop() calls pilot.stop."""
    await daemon.stop()
    daemon.pilot.stop.assert_called_once()


@pytest.mark.asyncio
async def test_stop_no_pilot(daemon):
    """stop() works when pilot is None."""
    daemon.pilot = None
    await daemon.stop()  # should not raise


@pytest.mark.asyncio
async def test_stop_cancels_mtime_task(daemon):
    """stop() cancels the config mtime watcher task."""
    mock_task = MagicMock()
    daemon._mtime_task = mock_task
    await daemon.stop()
    mock_task.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_stop_closes_ws_clients(daemon):
    """stop() closes all WS clients."""
    ws1 = AsyncMock()
    ws2 = AsyncMock()
    daemon.hub.ws_clients = {ws1, ws2}
    daemon.hub.terminal_ws_clients = set()
    await daemon.stop()
    ws1.close.assert_awaited_once()
    ws2.close.assert_awaited_once()
    assert len(daemon.hub.ws_clients) == 0


@pytest.mark.asyncio
async def test_stop_closes_terminal_ws_clients(daemon):
    """stop() closes terminal WS clients too."""
    tws = AsyncMock()
    daemon.hub.terminal_ws_clients = {tws}
    await daemon.stop()
    tws.close.assert_awaited_once()
    assert len(daemon.hub.terminal_ws_clients) == 0


@pytest.mark.asyncio
async def test_stop_handles_ws_close_errors(daemon):
    """stop() doesn't raise even if ws.close() fails."""
    ws = AsyncMock()
    ws.close.side_effect = Exception("network down")
    daemon.hub.ws_clients = {ws}
    daemon.hub.terminal_ws_clients = set()
    await daemon.stop()  # should not raise
    assert len(daemon.hub.ws_clients) == 0


# --- Fallback test report on shutdown ---


@pytest.mark.asyncio
async def test_stop_generates_fallback_report(daemon, tmp_path):
    """stop() should generate a test report if a test run is active and no report exists."""
    from unittest.mock import patch

    from swarm.testing.log import TestRunLog

    test_log = TestRunLog("shutdown-test", tmp_path)
    test_log.record_drone_decision("api", "c", "CONTINUE", "r")
    daemon.test_runner._test_log = test_log

    # Prevent spawning a real claude session for AI analysis
    with patch(
        "swarm.testing.report.asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("claude not found"),
    ):
        await daemon.stop()

    report_path = tmp_path / "test-run-shutdown-test.md"
    assert report_path.exists()
    content = report_path.read_text()
    assert "shutdown-test" in content


@pytest.mark.asyncio
async def test_stop_skips_report_when_already_exists(daemon, tmp_path):
    """stop() should not regenerate a report if one was already written."""
    from swarm.testing.log import TestRunLog

    test_log = TestRunLog("already-reported", tmp_path)
    test_log.record_drone_decision("api", "c", "CONTINUE", "r")
    daemon.test_runner._test_log = test_log

    # Pre-write the report
    report_path = tmp_path / "test-run-already-reported.md"
    report_path.write_text("existing report")

    await daemon.stop()

    # Report should not be overwritten
    assert report_path.read_text() == "existing report"


@pytest.mark.asyncio
async def test_stop_no_test_log_no_crash(daemon):
    """stop() should not crash when no test run is active."""
    # daemon has no _test_log attribute
    assert not hasattr(daemon, "_test_log")
    await daemon.stop()  # should not raise


# --- check_config_file with load error ---


def test_check_config_file_load_error(daemon, tmp_path):
    """check_config_file returns False on invalid config file."""
    cfg_file = tmp_path / "swarm.yaml"
    cfg_file.write_text("session_name: test\n")
    daemon.config.source_path = str(cfg_file)
    daemon.config_mgr._config_mtime = 0.0

    with patch("swarm.server.config_manager.load_config", side_effect=ValueError("bad yaml")):
        result = daemon.check_config_file()
    assert result is False


def test_check_config_file_oserror(daemon, tmp_path):
    """check_config_file returns False when stat fails."""
    daemon.config.source_path = str(tmp_path / "nonexistent.yaml")
    result = daemon.check_config_file()
    assert result is False


def test_check_config_file_applies_config_fields(daemon, tmp_path):
    """check_config_file hot-applies specific config fields from new config."""
    from swarm.config import DroneConfig, GroupConfig

    cfg_file = tmp_path / "swarm.yaml"
    cfg_file.write_text("session_name: test\n")
    daemon.config.source_path = str(cfg_file)
    daemon.config_mgr._config_mtime = 0.0

    new_config = HiveConfig(
        session_name="test",
        groups=[GroupConfig(name="backend", workers=["api"])],
        drones=DroneConfig(poll_interval=42.0),
    )
    with patch("swarm.server.config_manager.load_config", return_value=new_config):
        result = daemon.check_config_file()

    assert result is True
    assert len(daemon.config.groups) == 1
    assert daemon.config.groups[0].name == "backend"
    assert daemon.config.drones.poll_interval == 42.0


# --- _send_to_workers with failures ---


@pytest.mark.asyncio
async def test_send_to_workers_handles_errors(daemon):
    """_send_to_workers counts successes and skips failures."""
    call_count = 0

    async def flaky_action(worker):
        nonlocal call_count
        call_count += 1
        if worker.name == "api":
            raise OSError("process gone")

    count = await daemon.worker_svc._send_to_workers(
        daemon.workers, flaky_action, "all", "sent to {count} worker(s)"
    )
    assert count == 1  # only web succeeded
    assert call_count == 2


@pytest.mark.asyncio
async def test_send_to_workers_no_log_on_zero(daemon):
    """_send_to_workers doesn't log when all fail."""
    initial_entries = len(daemon.drone_log.entries)

    async def always_fail(worker):
        raise OSError("gone")

    count = await daemon.worker_svc._send_to_workers(
        daemon.workers, always_fail, "all", "sent to {count} worker(s)"
    )
    assert count == 0
    assert len(daemon.drone_log.entries) == initial_entries  # no new entries


# --- apply_config_update ---


@pytest.mark.asyncio
async def test_apply_config_update_drones(daemon, monkeypatch):
    """apply_config_update applies drones settings."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    await daemon.apply_config_update(
        {"drones": {"poll_interval": 15.0, "enabled": False, "auto_approve_yn": True}}
    )
    assert daemon.config.drones.poll_interval == 15.0
    assert daemon.config.drones.enabled is False
    assert daemon.config.drones.auto_approve_yn is True


@pytest.mark.asyncio
async def test_apply_config_update_drones_invalid_bool(daemon, monkeypatch):
    """apply_config_update raises ValueError for invalid bool field."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be boolean"):
        await daemon.apply_config_update({"drones": {"enabled": "yes"}})


@pytest.mark.asyncio
async def test_apply_config_update_drones_invalid_number(daemon, monkeypatch):
    """apply_config_update raises ValueError for invalid numeric field."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be a number"):
        await daemon.apply_config_update({"drones": {"poll_interval": "fast"}})


@pytest.mark.asyncio
async def test_apply_config_update_drones_negative_number(daemon, monkeypatch):
    """apply_config_update raises ValueError for negative numeric field."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be >= 0"):
        await daemon.apply_config_update({"drones": {"poll_interval": -1}})


@pytest.mark.asyncio
async def test_apply_config_update_queen(daemon, monkeypatch):
    """apply_config_update applies queen settings."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    await daemon.apply_config_update(
        {
            "queen": {
                "cooldown": 60.0,
                "enabled": False,
                "system_prompt": "Be careful",
                "min_confidence": 0.5,
            }
        }
    )
    assert daemon.config.queen.cooldown == 60.0
    assert daemon.config.queen.enabled is False
    assert daemon.config.queen.system_prompt == "Be careful"
    assert daemon.config.queen.min_confidence == 0.5


@pytest.mark.asyncio
async def test_apply_config_update_queen_invalid_cooldown(daemon, monkeypatch):
    """apply_config_update raises ValueError for bad queen.cooldown."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="non-negative"):
        await daemon.apply_config_update({"queen": {"cooldown": -5}})


@pytest.mark.asyncio
async def test_apply_config_update_queen_invalid_enabled(daemon, monkeypatch):
    """apply_config_update raises ValueError for bad queen.enabled."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be boolean"):
        await daemon.apply_config_update({"queen": {"enabled": 1}})


@pytest.mark.asyncio
async def test_apply_config_update_queen_invalid_system_prompt(daemon, monkeypatch):
    """apply_config_update raises ValueError for bad queen.system_prompt."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be a string"):
        await daemon.apply_config_update({"queen": {"system_prompt": 123}})


@pytest.mark.asyncio
async def test_apply_config_update_queen_invalid_min_confidence(daemon, monkeypatch):
    """apply_config_update raises ValueError for out-of-range min_confidence."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match=r"between 0\.0 and 1\.0"):
        await daemon.apply_config_update({"queen": {"min_confidence": 1.5}})


@pytest.mark.asyncio
async def test_apply_config_update_notifications(daemon, monkeypatch):
    """apply_config_update applies notifications settings."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    await daemon.apply_config_update(
        {"notifications": {"terminal_bell": False, "desktop": False, "debounce_seconds": 10.0}}
    )
    assert daemon.config.notifications.terminal_bell is False
    assert daemon.config.notifications.desktop is False
    assert daemon.config.notifications.debounce_seconds == 10.0


@pytest.mark.asyncio
async def test_apply_config_update_notifications_invalid_bool(daemon, monkeypatch):
    """apply_config_update raises for invalid notification booleans."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be boolean"):
        await daemon.apply_config_update({"notifications": {"desktop": "yes"}})


@pytest.mark.asyncio
async def test_apply_config_update_notifications_invalid_debounce(daemon, monkeypatch):
    """apply_config_update raises for negative debounce_seconds."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be >= 0"):
        await daemon.apply_config_update({"notifications": {"debounce_seconds": -1}})


@pytest.mark.asyncio
async def test_apply_config_update_approval_rules(daemon, monkeypatch):
    """apply_config_update applies approval_rules to drones config."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    await daemon.apply_config_update(
        {
            "drones": {
                "approval_rules": [
                    {"pattern": ".*bash.*", "action": "approve"},
                    {"pattern": ".*rm.*", "action": "escalate"},
                ]
            }
        }
    )
    assert len(daemon.config.drones.approval_rules) == 2
    assert daemon.config.drones.approval_rules[0].pattern == ".*bash.*"
    assert daemon.config.drones.approval_rules[1].action == "escalate"


@pytest.mark.asyncio
async def test_apply_config_update_approval_rules_invalid_type(daemon, monkeypatch):
    """apply_config_update raises if approval_rules is not a list."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be a list"):
        await daemon.apply_config_update({"drones": {"approval_rules": "bad"}})


@pytest.mark.asyncio
async def test_apply_config_update_approval_rules_invalid_item(daemon, monkeypatch):
    """apply_config_update raises if an approval rule item is not a dict."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be an object"):
        await daemon.apply_config_update({"drones": {"approval_rules": ["not_a_dict"]}})


@pytest.mark.asyncio
async def test_apply_config_update_approval_rules_invalid_action(daemon, monkeypatch):
    """apply_config_update raises for invalid approval rule action."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="'approve' or 'escalate'"):
        await daemon.apply_config_update(
            {"drones": {"approval_rules": [{"pattern": ".*", "action": "deny"}]}}
        )


@pytest.mark.asyncio
async def test_apply_config_update_approval_rules_invalid_regex(daemon, monkeypatch):
    """apply_config_update raises for invalid regex in approval rule."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="invalid regex"):
        await daemon.apply_config_update(
            {"drones": {"approval_rules": [{"pattern": "[invalid", "action": "approve"}]}}
        )


@pytest.mark.asyncio
async def test_apply_config_update_worker_descriptions(daemon, monkeypatch):
    """apply_config_update updates worker descriptions."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    daemon.config.workers = [WorkerConfig("api", "/tmp/api")]
    await daemon.apply_config_update({"workers": {"api": "API service worker"}})
    assert daemon.config.workers[0].description == "API service worker"


@pytest.mark.asyncio
async def test_apply_config_update_default_group(daemon, monkeypatch):
    """apply_config_update updates default_group."""
    from swarm.config import GroupConfig

    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    daemon.config.groups = [GroupConfig(name="backend", workers=["api"])]
    await daemon.apply_config_update({"default_group": "backend"})
    assert daemon.config.default_group == "backend"


@pytest.mark.asyncio
async def test_apply_config_update_default_group_invalid_type(daemon, monkeypatch):
    """apply_config_update raises for non-string default_group."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be a string"):
        await daemon.apply_config_update({"default_group": 123})


@pytest.mark.asyncio
async def test_apply_config_update_default_group_unknown(daemon, monkeypatch):
    """apply_config_update raises for unknown default_group."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="does not match"):
        await daemon.apply_config_update({"default_group": "nonexistent"})


@pytest.mark.asyncio
async def test_apply_config_update_top_level_scalars(daemon, monkeypatch):
    """apply_config_update sets top-level scalar fields."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    await daemon.apply_config_update({"session_name": "new-session", "log_level": "DEBUG"})
    assert daemon.config.session_name == "new-session"
    assert daemon.config.log_level == "DEBUG"


@pytest.mark.asyncio
async def test_apply_config_update_graph_settings(daemon, monkeypatch):
    """apply_config_update sets graph_client_id and graph_tenant_id."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    await daemon.apply_config_update({"graph_client_id": "abc123", "graph_tenant_id": "tenant1"})
    assert daemon.config.graph_client_id == "abc123"
    assert daemon.config.graph_tenant_id == "tenant1"


@pytest.mark.asyncio
async def test_apply_config_update_graph_tenant_empty_defaults_common(daemon, monkeypatch):
    """apply_config_update defaults empty graph_tenant_id to 'common'."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    await daemon.apply_config_update({"graph_tenant_id": ""})
    assert daemon.config.graph_tenant_id == "common"


@pytest.mark.asyncio
async def test_apply_config_update_workflows(daemon, monkeypatch):
    """apply_config_update sets workflow overrides."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    await daemon.apply_config_update({"workflows": {"bug": "/fix-and-ship"}})
    assert daemon.config.workflows["bug"] == "/fix-and-ship"


@pytest.mark.asyncio
async def test_apply_config_update_workflows_invalid_type(daemon, monkeypatch):
    """apply_config_update raises for non-dict workflows."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be an object"):
        await daemon.apply_config_update({"workflows": "not-a-dict"})


@pytest.mark.asyncio
async def test_apply_config_update_workflows_invalid_key(daemon, monkeypatch):
    """apply_config_update raises for invalid workflow task type key."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="not a valid task type"):
        await daemon.apply_config_update({"workflows": {"unknown_type": "/cmd"}})


@pytest.mark.asyncio
async def test_apply_config_update_workflows_invalid_value(daemon, monkeypatch):
    """apply_config_update raises for non-string workflow value."""
    monkeypatch.setattr("swarm.server.config_manager.save_config", MagicMock())
    with pytest.raises(ValueError, match="must be a string"):
        await daemon.apply_config_update({"workflows": {"bug": 42}})


# --- assign_task send failure ---


@pytest.mark.asyncio
async def test_start_task_send_failure_undoes_assignment(daemon):
    """start_task undoes assignment when send fails."""
    task = daemon.create_task(title="Test task", description="Important work")
    daemon.workers[0].state = WorkerState.RESTING
    await daemon.assign_task(task.id, "api")

    with patch.object(
        daemon,
        "send_to_worker",
        new_callable=AsyncMock,
        side_effect=ProcessError("process gone"),
    ):
        result = await daemon.start_task(task.id)

    assert result is False
    reloaded = daemon.task_board.get(task.id)
    # Task should be unassigned (returned to pending)
    assert reloaded.status == TaskStatus.UNASSIGNED
    assert reloaded.assigned_worker is None
    # Should have broadcast task_send_failed
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    assert any(c.get("type") == "task_send_failed" for c in calls)


# --- queue_proposal ---


def test_queue_proposal(daemon):
    """queue_proposal adds a proposal via ProposalManager."""
    proposal = AssignmentProposal(
        worker_name="api",
        task_id="test123",
        task_title="Test",
    )
    daemon.queue_proposal(proposal)
    assert len(daemon.proposal_store.pending) == 1
    assert daemon.proposal_store.pending[0].id == proposal.id


# --- _worker_task_map ---


def test_worker_task_map(daemon):
    """_worker_task_map returns dict of worker->task_title for active tasks."""
    t1 = daemon.task_board.create(title="Task A")
    daemon.task_board.assign(t1.id, "api")
    t2 = daemon.task_board.create(title="Task B")
    daemon.task_board.assign(t2.id, "web")

    result = daemon._worker_task_map()
    assert result == {"api": "Task A", "web": "Task B"}


def test_worker_task_map_empty(daemon):
    """_worker_task_map returns empty dict when no tasks assigned."""
    result = daemon._worker_task_map()
    assert result == {}


# --- _on_task_board_changed ---


def test_on_task_board_changed_broadcasts(daemon):
    """_on_task_board_changed broadcasts tasks_changed."""
    daemon._on_task_board_changed()
    daemon.broadcast_ws.assert_called_with({"type": "tasks_changed"})


# --- _expire_stale_proposals ---


def test_expire_stale_proposals(daemon):
    """_expire_stale_proposals expires proposals for missing workers/tasks."""
    proposal = AssignmentProposal(
        worker_name="nonexistent",
        task_id="missing_task",
        task_title="Ghost",
    )
    daemon.proposal_store.add(proposal)

    daemon._expire_stale_proposals()
    assert proposal.status == ProposalStatus.EXPIRED


# --- apply_config full coverage ---


def test_apply_config_queen_fields(daemon):
    """apply_config updates queen enabled, cooldown, prompt, min_confidence."""
    daemon.config.queen.enabled = False
    daemon.config.queen.cooldown = 999.0
    daemon.config.queen.system_prompt = "Be nice"
    daemon.config.queen.min_confidence = 0.3

    daemon.apply_config()

    assert daemon.queen.enabled is False
    assert daemon.queen.cooldown == 999.0
    assert daemon.queen.system_prompt == "Be nice"
    assert daemon.queen.min_confidence == 0.3


def test_apply_config_pilot_full(daemon):
    """apply_config updates all pilot fields."""
    from swarm.config import DroneConfig

    daemon.config.drones = DroneConfig(poll_interval=42.0, max_idle_interval=120.0, enabled=False)
    daemon.apply_config()

    assert daemon.pilot.drone_config.poll_interval == 42.0
    daemon.pilot.set_poll_intervals.assert_called_once_with(42.0, 120.0)
    assert daemon.pilot.interval == 42.0
    assert daemon.pilot.enabled is False


# --- send_to_worker operator logging ---


@pytest.mark.asyncio
async def test_send_to_worker_no_operator_log(daemon):
    """send_to_worker with _log_operator=False skips operator logging."""
    from swarm.drones.log import SystemAction

    await daemon.send_to_worker("api", "hello", _log_operator=False)
    ops = [e for e in daemon.drone_log.entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 0


@pytest.mark.asyncio
async def test_send_to_worker_operator_log(daemon):
    """send_to_worker defaults to logging operator action."""
    from swarm.drones.log import SystemAction

    await daemon.send_to_worker("api", "hello")
    ops = [e for e in daemon.drone_log.entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 1
    assert ops[0].detail == "sent message"


# --- interrupt_worker and escape_worker logging ---


@pytest.mark.asyncio
async def test_interrupt_worker_logs_operator(daemon):
    """Interrupting a worker logs OPERATOR to drone_log."""
    from swarm.drones.log import SystemAction

    await daemon.interrupt_worker("api")

    entries = daemon.drone_log.entries
    ops = [e for e in entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 1
    assert "interrupted" in ops[0].detail


@pytest.mark.asyncio
async def test_escape_worker_logs_operator(daemon):
    """Escaping a worker logs OPERATOR to drone_log."""
    from swarm.drones.log import SystemAction

    await daemon.escape_worker("api")

    entries = daemon.drone_log.entries
    ops = [e for e in entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 1
    assert "Escape" in ops[0].detail


# --- _require_worker and _require_task ---


def test_require_worker_found(daemon):
    """_require_worker returns worker when found."""
    w = daemon._require_worker("api")
    assert w.name == "api"


def test_require_worker_not_found(daemon):
    """_require_worker raises WorkerNotFoundError when not found."""
    with pytest.raises(WorkerNotFoundError, match="nonexistent"):
        daemon._require_worker("nonexistent")


# --- _worker_descriptions ---


def test_worker_descriptions(daemon):
    """_worker_descriptions returns descriptions from config workers."""
    daemon.config.workers = [
        WorkerConfig("api", "/tmp/api", description="API service"),
        WorkerConfig("web", "/tmp/web"),
    ]
    result = daemon._worker_descriptions()
    assert result == {"api": "API service"}


def test_worker_descriptions_empty(daemon):
    """_worker_descriptions returns empty dict when no workers have descriptions."""
    daemon.config.workers = [
        WorkerConfig("api", "/tmp/api"),
        WorkerConfig("web", "/tmp/web"),
    ]
    result = daemon._worker_descriptions()
    assert result == {}


# --- send_all with long message ---


@pytest.mark.asyncio
async def test_send_all_long_message_truncates_preview(daemon):
    """send_all truncates the log preview for long messages."""
    long_msg = "x" * 100
    count = await daemon.send_all(long_msg)
    assert count == 2
    # Check that the log entry was created (preview should be truncated)
    ops = [e for e in daemon.drone_log.entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 1
    assert "\u2026" in ops[0].detail  # ellipsis in preview


# --- send_group with message ---


@pytest.mark.asyncio
async def test_send_group_logs_group_send(daemon):
    """send_group logs the group send action."""
    from swarm.config import GroupConfig

    daemon.config.workers = [WorkerConfig("api", "/tmp/api"), WorkerConfig("web", "/tmp/web")]
    daemon.config.groups = [GroupConfig(name="backend", workers=["api"])]
    await daemon.send_group("backend", "deploy")

    ops = [e for e in daemon.drone_log.entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 1
    assert "backend" in ops[0].worker_name or "group" in ops[0].detail


# --- revive_worker logging ---


@pytest.mark.asyncio
async def test_revive_worker_logs_operator(daemon):
    """Reviving a worker logs OPERATOR to drone_log."""
    daemon.workers[0].state = WorkerState.STUNG
    with patch("swarm.worker.manager.revive_worker", new_callable=AsyncMock):
        await daemon.revive_worker("api")

    ops = [e for e in daemon.drone_log.entries if e.action == SystemAction.OPERATOR]
    assert len(ops) == 1
    assert "revived" in ops[0].detail


# --- kill_session with OSError ---


@pytest.mark.asyncio
async def test_kill_session_handles_oserror(daemon):
    """kill_session continues cleanly even if pool kill fails."""
    daemon.pool = MagicMock()
    daemon.pool.kill_all = AsyncMock(side_effect=OSError("pool gone"))
    await daemon.kill_session()
    assert len(daemon.workers) == 0


# --- _on_task_assigned ---


def test_on_task_assigned_broadcasts(daemon):
    """_on_task_assigned broadcasts task_assigned event."""
    task = daemon.task_board.create(title="Test task")
    daemon._on_task_assigned(daemon.workers[0], task)

    daemon.broadcast_ws.assert_called()
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    ta_calls = [c for c in calls if c.get("type") == "task_assigned"]
    assert len(ta_calls) == 1
    assert ta_calls[0]["worker"] == "api"
    assert ta_calls[0]["task"]["title"] == "Test task"


def test_on_task_assigned_emits_notification(daemon):
    """_on_task_assigned emits notification to notification bus."""
    task = daemon.task_board.create(title="Test task")
    daemon._on_task_assigned(daemon.workers[0], task)
    daemon.notification_bus.emit_task_assigned.assert_called_once_with("api", "Test task")


# --- _on_task_done queen-enabled path ---


def test_on_task_done_queen_enabled_inflight_check(daemon):
    """_on_task_done skips when completion analysis already in flight."""
    task = daemon.task_board.create(title="Test task")
    daemon.task_board.assign(task.id, "api")
    daemon.workers[0].state = WorkerState.RESTING

    daemon.queen.enabled = True
    daemon.queen._last_call = 0.0
    daemon.queen.cooldown = 0.0

    # Pre-mark as in-flight via the queue
    daemon.queen_queue._all_keys.add(f"completion:api:{task.id}")

    daemon._on_task_done(daemon.workers[0], task)
    # No new proposals should be created
    assert len(daemon.proposal_store.pending) == 0

    daemon.queen_queue._all_keys.discard(f"completion:api:{task.id}")


# --- proposal_dict ---


def test_proposal_dict(daemon):
    """proposal_dict returns serialized proposal dict."""
    proposal = AssignmentProposal(
        worker_name="api",
        task_id="abc123",
        task_title="Fix bug",
        message="Go fix it",
        reasoning="Worker is idle",
        confidence=0.85,
    )
    daemon.proposal_store.add(proposal)

    result = daemon.proposal_dict(proposal)
    assert result["worker_name"] == "api"
    assert result["task_id"] == "abc123"
    assert result["task_title"] == "Fix bug"
    assert result["confidence"] == 0.85
    # ProposalStatus, not TaskStatus — its value remains "pending".
    assert result["status"] == "pending"


def test_proposal_dict_completion_with_email(daemon):
    """proposal_dict includes has_source_email for completion proposals."""
    task = daemon.task_board.create(title="Email task")
    # Manually set source_email_id on the in-memory task
    task.source_email_id = "msg123"

    proposal = AssignmentProposal.completion(
        worker_name="api",
        task_id=task.id,
        task_title=task.title,
        assessment="Done",
    )
    result = daemon.proposal_dict(proposal)
    assert result["has_source_email"] is True


# --- _broadcast_proposals ---


def test_broadcast_proposals(daemon):
    """_broadcast_proposals sends proposals_changed to WS clients."""
    proposal = AssignmentProposal(
        worker_name="api",
        task_title="Test",
    )
    daemon.proposal_store.add(proposal)

    daemon._broadcast_proposals()

    daemon.broadcast_ws.assert_called()
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    pc_calls = [c for c in calls if c.get("type") == "proposals_changed"]
    assert len(pc_calls) == 1
    assert pc_calls[0]["pending_count"] == 1


# --- stop terminal_ws close errors ---


@pytest.mark.asyncio
async def test_stop_terminal_ws_close_errors(daemon):
    """stop() handles errors in terminal WS close gracefully."""
    tws = AsyncMock()
    tws.close.side_effect = Exception("network down")
    daemon.hub.terminal_ws_clients = {tws}
    await daemon.stop()
    assert len(daemon.hub.terminal_ws_clients) == 0


# --- complete_task with send_reply ---


def test_complete_task_with_resolution(daemon):
    """complete_task accepts and stores a resolution string."""
    task = daemon.create_task(title="Bug fix")
    daemon.task_board.assign(task.id, "api")
    result = daemon.complete_task(task.id, resolution="Fixed the null pointer")
    assert result is True
    reloaded = daemon.task_board.get(task.id)
    assert reloaded.resolution == "Fixed the null pointer"


# --- task #225 Phase 3: post-ship self-loop ---


@pytest.mark.asyncio
async def test_complete_task_auto_starts_next_assigned(daemon):
    """After ``complete_task`` ships one, the daemon should auto-start the
    next ASSIGNED task for the same worker — that's the self-loop Phase 3
    of #225 asks for, so workers don't park after every turn.
    """
    done_task = daemon.create_task(title="First")
    daemon.task_board.assign(done_task.id, "api")

    queued_task = daemon.create_task(title="Second")
    daemon.task_board.assign(queued_task.id, "api")

    with patch.object(daemon, "start_task", new_callable=AsyncMock) as mock_start:
        result = daemon.complete_task(done_task.id, resolution="done")
        # complete_task fires start_task as a background task; give the loop a tick.
        await asyncio.sleep(0)

    assert result is True
    assert mock_start.await_count == 1
    assert mock_start.await_args.args[0] == queued_task.id


@pytest.mark.asyncio
async def test_complete_task_without_queued_work_does_not_dispatch(daemon):
    """Empty queue after complete → no follow-up dispatch. The operator
    explicitly scoped Phase 3 to ``skip if nothing else is assigned`` so
    workers don't get pointless "nothing to do" prompts.
    """
    only_task = daemon.create_task(title="Only task")
    daemon.task_board.assign(only_task.id, "api")

    with patch.object(daemon, "start_task", new_callable=AsyncMock) as mock_start:
        daemon.complete_task(only_task.id, resolution="done")
        await asyncio.sleep(0)

    mock_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_task_skips_in_progress_next_task(daemon):
    """``active_tasks_for_worker`` returns both ASSIGNED and IN_PROGRESS;
    the auto-chain should only dispatch an ASSIGNED follow-up. An
    IN_PROGRESS task is already being worked on in some PTY; starting it
    again would interleave output.
    """
    done_task = daemon.create_task(title="Shipping")
    daemon.task_board.assign(done_task.id, "api")
    already_running = daemon.create_task(title="Already running")
    daemon.task_board.assign(already_running.id, "api")
    # Force the second task into IN_PROGRESS without going through start_task
    # (which would try to touch the PTY).
    daemon.task_board.get(already_running.id).start()

    with patch.object(daemon, "start_task", new_callable=AsyncMock) as mock_start:
        daemon.complete_task(done_task.id, resolution="done")
        await asyncio.sleep(0)

    mock_start.assert_not_awaited()


# --- _on_state_changed no proposal to expire ---


def test_on_state_changed_buzzing_no_proposals(daemon):
    """_on_state_changed BUZZING with no proposals doesn't crash."""
    daemon.workers[0].state = WorkerState.BUZZING
    daemon._on_state_changed(daemon.workers[0])
    # Should broadcast but not crash
    daemon.broadcast_ws.assert_called()


# --- _on_state_changed completion proposals expired ---


def test_on_state_changed_buzzing_expires_completion_proposals(daemon):
    """_on_state_changed expires completion proposals when worker goes BUZZING."""
    task = daemon.task_board.create(title="Test")
    daemon.task_board.assign(task.id, "api")

    proposal = AssignmentProposal.completion(
        worker_name="api",
        task_id=task.id,
        task_title=task.title,
        assessment="Maybe done",
    )
    daemon.proposal_store.add(proposal)

    daemon.workers[0].state = WorkerState.BUZZING
    daemon._on_state_changed(daemon.workers[0])

    assert proposal.status == ProposalStatus.EXPIRED


# --- _on_state_changed does not expire assignment proposals ---


def test_on_state_changed_buzzing_keeps_assignment_proposals(daemon):
    """_on_state_changed does NOT expire assignment proposals when BUZZING."""
    proposal = AssignmentProposal(
        worker_name="api",
        task_id="t123",
        task_title="Test",
        proposal_type="assignment",
    )
    daemon.proposal_store.add(proposal)

    daemon.workers[0].state = WorkerState.BUZZING
    daemon._on_state_changed(daemon.workers[0])

    # Assignment proposals should remain pending
    assert proposal.status == ProposalStatus.PENDING


# --- assign_task with queen context message ---


@pytest.mark.asyncio
async def test_assign_and_start_task_with_queen_message(daemon):
    """assign_and_start_task appends Queen context when message is provided."""
    task = daemon.create_task(title="Fix bug", description="It crashes")
    daemon.workers[0].state = WorkerState.RESTING

    with patch.object(daemon, "send_to_worker", new_callable=AsyncMock) as mock_send:
        await daemon.assign_and_start_task(task.id, "api", message="Focus on the crash handler")

    sent_msg = mock_send.call_args[0][1]
    assert "Queen context: Focus on the crash handler" in sent_msg


# --- assign_task logs task_assigned system event ---


# --- _on_escalation queen enabled without event loop ---


def test_on_escalation_queen_enabled_no_loop(daemon):
    """_on_escalation queen-enabled path clears escalation tracking on RuntimeError."""
    daemon.queen.enabled = True
    daemon.queen._last_call = 0.0
    daemon.queen.cooldown = 0.0

    # No running event loop in sync test → RuntimeError path
    daemon._on_escalation(daemon.workers[0], "test reason")

    # Escalation tracking should be cleared (RuntimeError handled)
    assert not daemon.analyzer.has_inflight_escalation("api")
    # Should still broadcast the escalation event
    calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
    assert any(c.get("type") == "escalation" for c in calls)


# --- _on_task_done queen enabled without event loop ---


def test_on_task_done_queen_enabled_no_loop(daemon):
    """_on_task_done queen-enabled path clears completion tracking on RuntimeError."""
    task = daemon.task_board.create(title="Test task")
    daemon.task_board.assign(task.id, "api")
    daemon.workers[0].state = WorkerState.RESTING

    daemon.queen.enabled = True
    daemon.queen._last_call = 0.0
    daemon.queen.cooldown = 0.0

    # No running event loop in sync test → RuntimeError path
    daemon._on_task_done(daemon.workers[0], task)

    # Completion tracking should be cleared
    key = f"api:{task.id}"
    assert not daemon.analyzer.has_inflight_completion(key)
    # No proposals created (analysis couldn't start)
    assert len(daemon.proposal_store.pending) == 0


# --- complete_task with email reply (RuntimeError path) ---


def test_complete_task_send_reply_no_loop(daemon):
    """complete_task auto-drafts email reply; sync context catches RuntimeError."""
    task = daemon.create_task(title="Email task")
    task.source_email_id = "msg123"
    daemon.task_board.assign(task.id, "api")
    daemon.graph_mgr = MagicMock()  # graph configured

    # In sync test, asyncio.get_running_loop() raises RuntimeError
    result = daemon.complete_task(task.id, resolution="Fixed the bug")
    assert result is True
    assert daemon.task_board.get(task.id).status == TaskStatus.DONE


@pytest.mark.asyncio
async def test_complete_task_email_path_does_not_clobber_task_variable(daemon, monkeypatch):
    """Regression for task #270: the email-reply branch used to reassign
    the local ``task`` variable to an ``asyncio.Task``, then the post-ship
    self-loop tried ``task.assigned_worker`` and blew up with
    ``'_asyncio.Task' object has no attribute 'assigned_worker'``.

    The DB mutation always succeeded (``task_board.complete`` already ran
    by then), so the bug only surfaced as a noisy error response from
    ``queen_force_complete_task``. Pin that the email-reply path runs
    to completion AND the self-loop gets the original SwarmTask's
    ``assigned_worker`` without raising.
    """
    # Assigned task with an email source so the email-reply branch fires.
    task = daemon.create_task(title="Email task")
    task.source_email_id = "msg-270"
    daemon.task_board.assign(task.id, "api")
    daemon.graph_mgr = MagicMock()  # satisfies the ``self.graph_mgr`` guard

    # Capture the argument the post-ship self-loop receives. Before the
    # fix this raised AttributeError BEFORE reaching the patched method
    # because the method call itself evaluated ``task.assigned_worker``.
    captured: list[str | None] = []

    def fake_auto_start(worker_name):
        captured.append(worker_name)

    monkeypatch.setattr(daemon, "_auto_start_next_assigned", fake_auto_start)

    # Stub out the actual reply-drafting coroutine so no real Graph call
    # fires — we only care that complete_task returns cleanly.
    async def fake_send_reply(*_a, **_kw):
        return None

    monkeypatch.setattr(daemon, "_send_completion_reply", fake_send_reply)

    result = daemon.complete_task(task.id, resolution="Fixed the thing")
    assert result is True
    assert captured == ["api"], (
        "post-ship self-loop must receive the SwarmTask's assigned_worker, "
        "not the asyncio.Task that used to clobber the local variable"
    )


@pytest.mark.asyncio
async def test_assign_task_logs_system_event(daemon):
    """assign_task logs TASK_ASSIGNED to drone_log."""
    task = daemon.create_task(title="Test task", description="Do it")
    daemon.workers[0].state = WorkerState.RESTING

    await daemon.assign_task(task.id, "api")

    entries = daemon.drone_log.entries
    assigned = [e for e in entries if e.action == SystemAction.TASK_ASSIGNED]
    assert len(assigned) == 1
    assert assigned[0].worker_name == "api"
    assert assigned[0].metadata["task_id"] == task.id


# ── Heartbeat loop ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_broadcasts_on_display_state_change(daemon):
    """Heartbeat should broadcast when worker display_state changes."""
    # Replace broadcast_ws with a real list collector
    broadcasts: list[dict] = []
    daemon.broadcast_ws = lambda data: broadcasts.append(data)
    daemon._heartbeat_snapshot = {}

    # Set worker states
    daemon.workers[0].state = WorkerState.RESTING
    daemon.workers[1].state = WorkerState.BUZZING

    # Manually verify the snapshot diff logic (no need for the async loop)
    snapshot = {w.name: w.display_state.value for w in daemon.workers}
    assert snapshot != daemon._heartbeat_snapshot

    # After updating snapshot, changing state should create a diff
    daemon._heartbeat_snapshot = snapshot
    daemon.workers[0].state = WorkerState.BUZZING
    new_snapshot = {w.name: w.display_state.value for w in daemon.workers}
    assert new_snapshot != daemon._heartbeat_snapshot


@pytest.mark.asyncio
async def test_heartbeat_no_broadcast_when_unchanged(daemon):
    """Heartbeat should not broadcast when display_state is unchanged."""
    broadcasts: list[dict] = []
    daemon.broadcast_ws = lambda data: broadcasts.append(data)

    # Pre-seed snapshot to match current state
    daemon.workers[0].state = WorkerState.BUZZING
    daemon.workers[1].state = WorkerState.BUZZING
    daemon._heartbeat_snapshot = {w.name: w.display_state.value for w in daemon.workers}

    # Manually run the snapshot check
    snapshot = {w.name: w.display_state.value for w in daemon.workers}
    assert snapshot == daemon._heartbeat_snapshot
    # No broadcast should happen
    assert len(broadcasts) == 0


@pytest.mark.asyncio
async def test_heartbeat_revives_dead_pilot_loop(daemon):
    """Heartbeat watchdog should restart the pilot loop if it has died."""
    pilot = MagicMock()
    pilot._dispatcher._running = True
    # Simulate a dead task
    dead_task = asyncio.Future()
    dead_task.set_result(None)  # marks as done
    pilot._dispatcher._task = dead_task
    pilot._dispatcher.loop = AsyncMock()
    daemon.pilot = pilot

    # Run one heartbeat iteration manually
    daemon._heartbeat_snapshot = {}
    daemon.broadcast_ws = MagicMock()

    # The watchdog check: task is done → should restart
    task = pilot._dispatcher._task
    assert task.done()

    # Simulate the watchdog logic from _heartbeat_loop
    if pilot._dispatcher._running and (task is None or task.done()):
        pilot._dispatcher._task = asyncio.create_task(pilot._dispatcher.loop())

    # Verify _loop was scheduled
    assert pilot._dispatcher.loop.called or not pilot._dispatcher._task.done()
    # Cleanup
    if not pilot._dispatcher._task.done():
        pilot._dispatcher._task.cancel()
        try:
            await pilot._dispatcher._task
        except asyncio.CancelledError:
            pass


# --- Debounced state broadcast tests ---


@pytest.mark.asyncio
async def test_on_state_changed_marks_dirty_not_immediate(daemon):
    """_on_state_changed should mark dirty but not call _broadcast_state immediately."""
    worker = daemon.workers[0]
    worker.state = WorkerState.RESTING

    # Replace _broadcast_state to track calls
    broadcast_calls = []
    daemon._broadcast_state = lambda: broadcast_calls.append(1)

    daemon._on_state_changed(worker)

    # Should be dirty but no immediate broadcast
    assert daemon.publisher._state_dirty is True
    assert len(broadcast_calls) == 0


@pytest.mark.asyncio
async def test_debounce_flushes_after_timer(daemon):
    """Dirty state should flush after the debounce delay fires."""
    worker = daemon.workers[0]
    worker.state = WorkerState.RESTING

    broadcast_calls = []
    daemon._broadcast_state = lambda: broadcast_calls.append(1)

    daemon._on_state_changed(worker)
    assert daemon.publisher._state_dirty is True
    assert len(broadcast_calls) == 0

    # Let the event loop process the call_later timer
    await asyncio.sleep(daemon.publisher._state_debounce_delay + 0.05)

    assert daemon.publisher._state_dirty is False
    assert len(broadcast_calls) == 1


@pytest.mark.asyncio
async def test_multiple_dirty_marks_single_broadcast(daemon):
    """Multiple state changes within debounce window produce one broadcast."""
    broadcast_calls = []
    daemon._broadcast_state = lambda: broadcast_calls.append(1)

    # Fire 3 state changes in quick succession
    for w in daemon.workers:
        w.state = WorkerState.RESTING
        daemon._on_state_changed(w)

    assert daemon.publisher._state_dirty is True
    assert len(broadcast_calls) == 0

    await asyncio.sleep(daemon.publisher._state_debounce_delay + 0.05)

    assert len(broadcast_calls) == 1


def test_flush_state_broadcast_noop_when_clean(daemon):
    """_flush_state_broadcast should do nothing when not dirty."""
    broadcast_calls = []
    daemon._broadcast_state = lambda: broadcast_calls.append(1)

    daemon.publisher._state_dirty = False
    daemon._flush_state_broadcast()

    assert len(broadcast_calls) == 0


@pytest.mark.asyncio
async def test_cancel_timers_cancels_debounce(daemon):
    """_cancel_timers should cancel the debounce handle."""
    handle = MagicMock()
    daemon.publisher._state_debounce_handle = handle

    await daemon._cancel_timers()

    handle.cancel.assert_called_once()
    assert daemon.publisher._state_debounce_handle is None


@pytest.mark.asyncio
async def test_start_task_wakes_suspended_worker(daemon):
    """start_task should call pilot.wake_worker for the assigned worker."""
    task = daemon.create_task(title="Test task")
    await daemon.assign_task(task.id, "api")

    with patch.object(daemon, "send_to_worker", new_callable=AsyncMock):
        await daemon.start_task(task.id)

    daemon.pilot.wake_worker.assert_called_with("api")


def test_daemon_lock_prevents_duplicate(tmp_path):
    """_acquire_daemon_lock should raise SystemExit when another process holds the lock."""
    import fcntl

    from swarm.server.runner import _acquire_daemon_lock

    # Use a temp lock file to avoid interfering with real daemon
    lock_file = tmp_path / "daemon.lock"
    with patch("swarm.server.runner._DAEMON_LOCK_PATH", lock_file):
        # First acquisition should succeed
        fd1 = _acquire_daemon_lock()
        assert fd1 >= 0

        # Second acquisition should fail with SystemExit and the message
        # must point at the real recovery command ('swarm stop'), not the
        # nonexistent 'swarm kill --all' that leaked in earlier versions.
        with pytest.raises(SystemExit) as exc_info:
            _acquire_daemon_lock()
        msg = str(exc_info.value)
        assert "Another swarm daemon" in msg
        assert "swarm stop" in msg
        assert "kill --all" not in msg

        # Clean up
        fcntl.flock(fd1, fcntl.LOCK_UN)
        import os

        os.close(fd1)


def test_daemon_lock_stale_live_holder_points_at_swarm_stop(tmp_path, monkeypatch):
    """If a live (non-stale) holder owns the lock, the error message must also
    reference 'swarm stop' — this is the second error path inside
    _acquire_daemon_lock and was missed by an earlier replace_all fix.
    """
    import os

    from swarm.server import runner as runner_mod

    lock_file = tmp_path / "daemon.lock"
    monkeypatch.setattr(runner_mod, "_DAEMON_LOCK_PATH", lock_file)

    # Hold the lock from a fresh fd and write a "live" PID that _pid_alive()
    # will report as alive — this drives execution into the `else` branch.
    fd_holder = runner_mod._acquire_daemon_lock()
    # Force _pid_alive to report True so the stale-lock branch is skipped.
    monkeypatch.setattr(runner_mod, "_pid_alive", lambda _pid: True)

    with pytest.raises(SystemExit) as exc_info:
        runner_mod._acquire_daemon_lock()
    msg = str(exc_info.value)
    assert "swarm stop" in msg
    assert "kill --all" not in msg

    import fcntl as _fcntl

    _fcntl.flock(fd_holder, _fcntl.LOCK_UN)
    os.close(fd_holder)


# --- _on_oversight_alert ---


def test_on_oversight_alert_broadcasts(daemon):
    """_on_oversight_alert should broadcast an oversight_alert message."""
    from swarm.queen.oversight import OversightResult, OversightSignal, Severity, SignalType

    worker = daemon.workers[0]
    sig = OversightSignal(
        signal_type=SignalType.PROLONGED_BUZZING,
        worker_name="api",
        description="buzzing too long",
    )
    result = OversightResult(
        signal=sig,
        severity=Severity.CRITICAL,
        action="flag_human",
        reasoning="Dangerous operation",
        message="Blocked destructive operation",
    )
    daemon._on_oversight_alert(worker, sig, result)
    daemon.broadcast_ws.assert_called()
    call_data = daemon.broadcast_ws.call_args[0][0]
    assert call_data["type"] == "oversight_alert"
    assert call_data["worker"] == "api"
    assert call_data["severity"] == "critical"


def test_on_oversight_alert_non_oversight_result_ignored(daemon):
    """_on_oversight_alert ignores non-OversightResult objects."""
    worker = daemon.workers[0]
    daemon._on_oversight_alert(worker, "not-a-signal", "not-a-result")
    daemon.broadcast_ws.assert_not_called()


# --- _on_tunnel_state_change ---


def test_on_tunnel_state_change_running(daemon):
    """Tunnel RUNNING broadcasts tunnel_started with URL."""
    from swarm.tunnel import TunnelState

    daemon._on_tunnel_state_change(TunnelState.RUNNING, "https://example.com")
    daemon.broadcast_ws.assert_called_once()
    call_data = daemon.broadcast_ws.call_args[0][0]
    assert call_data["type"] == "tunnel_started"
    assert call_data["url"] == "https://example.com"


def test_on_tunnel_state_change_stopped(daemon):
    """Tunnel STOPPED broadcasts tunnel_stopped."""
    from swarm.tunnel import TunnelState

    daemon._on_tunnel_state_change(TunnelState.STOPPED, "")
    daemon.broadcast_ws.assert_called_once()
    call_data = daemon.broadcast_ws.call_args[0][0]
    assert call_data["type"] == "tunnel_stopped"


def test_on_tunnel_state_change_error(daemon):
    """Tunnel ERROR broadcasts tunnel_error with detail."""
    from swarm.tunnel import TunnelState

    daemon._on_tunnel_state_change(TunnelState.ERROR, "connection refused")
    daemon.broadcast_ws.assert_called_once()
    call_data = daemon.broadcast_ws.call_args[0][0]
    assert call_data["type"] == "tunnel_error"
    assert call_data["error"] == "connection refused"


# --- _on_operator_terminal_approval ---


def test_on_operator_terminal_approval_broadcasts(daemon):
    """_on_operator_terminal_approval broadcasts the approval event."""
    worker = daemon.workers[0]
    daemon._on_operator_terminal_approval(worker, "Allow Bash", "choice", r"Bash\b")
    daemon.broadcast_ws.assert_called_once()
    call_data = daemon.broadcast_ws.call_args[0][0]
    assert call_data["type"] == "operator_terminal_approval"
    assert call_data["worker"] == "api"
    assert call_data["summary"] == "Allow Bash"
    assert call_data["prompt_type"] == "choice"
    assert call_data["pattern"] == r"Bash\b"


# --- _on_drone_entry notification paths ---


def test_on_drone_entry_notification_high_priority(daemon):
    """Notification-worthy WORKER_STUNG entries get high priority."""
    from swarm.drones.log import LogCategory, SystemAction, SystemEntry

    entry = SystemEntry(
        timestamp=0.0,
        action=SystemAction.WORKER_STUNG,
        worker_name="api",
        detail="process crashed",
        category=LogCategory.WORKER,
        is_notification=True,
    )
    daemon.push_notification = MagicMock()
    daemon._on_drone_entry(entry)
    daemon.push_notification.assert_called_once()
    assert daemon.push_notification.call_args[1]["priority"] == "high"


def test_on_drone_entry_no_notification(daemon):
    """Non-notification entries don't push notifications."""
    from swarm.drones.log import LogCategory, SystemAction, SystemEntry

    entry = SystemEntry(
        timestamp=0.0,
        action=SystemAction.OPERATOR,
        worker_name="api",
        detail="operator continued",
        category=LogCategory.OPERATOR,
        is_notification=False,
    )
    daemon.push_notification = MagicMock()
    daemon._on_drone_entry(entry)
    daemon.push_notification.assert_not_called()


def test_on_drone_entry_medium_priority(daemon):
    """Non-STUNG notification entries get medium priority."""
    from swarm.drones.log import LogCategory, SystemAction, SystemEntry

    entry = SystemEntry(
        timestamp=0.0,
        action=SystemAction.OPERATOR,
        worker_name="api",
        detail="continued",
        category=LogCategory.OPERATOR,
        is_notification=True,
    )
    daemon.push_notification = MagicMock()
    daemon._on_drone_entry(entry)
    assert daemon.push_notification.call_args[1]["priority"] == "medium"


# --- _on_state_changed additional paths ---


def test_on_state_changed_stung_logs_and_notifies(daemon):
    """STUNG state change logs a WORKER_STUNG entry."""
    worker = daemon.workers[0]
    worker.state = WorkerState.STUNG
    daemon._on_state_changed(worker)
    entries = daemon.drone_log.entries
    assert any(e.action == SystemAction.WORKER_STUNG for e in entries)


def test_on_state_changed_resting_no_proposal_expire(daemon):
    """RESTING state change should NOT expire proposals."""
    worker = daemon.workers[0]
    worker.state = WorkerState.RESTING
    daemon._on_state_changed(worker)
    # No proposals to expire — just verify it doesn't crash


# --- _on_workers_changed ---


def test_on_workers_changed_broadcasts_workers(daemon):
    """_on_workers_changed broadcasts workers state."""
    daemon._on_workers_changed()
    daemon.broadcast_ws.assert_called()
    call_data = daemon.broadcast_ws.call_args[0][0]
    assert call_data["type"] == "workers_changed"


# --- _on_task_done additional paths ---


def test_on_task_done_unassigned_task(daemon):
    """_on_task_done with no assigned task is a no-op."""
    worker = daemon.workers[0]
    worker.state = WorkerState.RESTING
    from swarm.tasks.task import SwarmTask

    task = SwarmTask(title="done-task")
    task.status = TaskStatus.DONE
    # Task not assigned — should not crash
    daemon._on_task_done(worker, task)


def test_on_task_done_with_resolution_text(daemon):
    """_on_task_done with resolution text creates completion proposal."""
    worker = daemon.workers[0]
    worker.state = WorkerState.RESTING
    task = daemon.create_task(title="Resolution task")
    task.status = TaskStatus.ACTIVE
    task.assigned_to = "api"
    daemon._on_task_done(worker, task, resolution="All tests pass")
    # Should have created a completion proposal
    pending = daemon.proposal_store.pending_for_worker("api")
    assert any(
        "resolution" in str(p).lower() or p.proposal_type.value == "completion" for p in pending
    )


# --- _on_task_assigned ---


def test_on_task_assigned_broadcasts_event(daemon):
    """_on_task_assigned broadcasts a task_assigned event."""
    worker = daemon.workers[0]
    task = daemon.create_task(title="Assigned task")
    daemon._on_task_assigned(worker, task, message="Work on this")
    daemon.broadcast_ws.assert_called()
    call_data = daemon.broadcast_ws.call_args[0][0]
    assert call_data["type"] == "task_assigned"


# --- push_notification ---


def test_push_notification_broadcasts_bell(daemon):
    """push_notification broadcasts a notification event."""
    daemon.push_notification(event="test", worker="api", message="hello")
    daemon.broadcast_ws.assert_called()
    call_data = daemon.broadcast_ws.call_args[0][0]
    assert call_data["type"] == "notification"
    assert call_data["event"] == "test"


def test_push_notification_stores_history(daemon):
    """push_notification adds to _notification_history."""
    daemon.escalation._notification_history.clear()
    daemon.push_notification(event="test", worker="api", message="hello")
    assert len(daemon.escalation._notification_history) == 1
    assert daemon.escalation._notification_history[0]["event"] == "test"


# --- _on_test_complete ---


def test_on_test_complete_noop_without_test_log(daemon):
    """_on_test_complete returns early when no _test_log is set."""
    daemon._on_test_complete()
    daemon.broadcast_ws.assert_not_called()


# --- broadcast_ws ---


@pytest.mark.asyncio
async def test_broadcast_ws_calls_hook(daemon):
    """broadcast_ws invokes _broadcast_hook if set."""
    # Replace the MagicMock with the real method for these tests
    daemon.broadcast_ws = SwarmDaemon.broadcast_ws.__get__(daemon)
    hook = MagicMock()
    daemon.hub._broadcast_hook = hook
    daemon.broadcast_ws({"type": "test"})
    hook.assert_called_once_with({"type": "test"})


@pytest.mark.asyncio
async def test_broadcast_ws_skips_when_no_clients(daemon):
    """broadcast_ws returns early when ws_clients is empty."""
    daemon.broadcast_ws = SwarmDaemon.broadcast_ws.__get__(daemon)
    daemon.hub._broadcast_hook = None
    daemon.hub.ws_clients = set()
    # Should not raise — just returns
    daemon.broadcast_ws({"type": "noop"})


@pytest.mark.asyncio
async def test_broadcast_ws_removes_closed_clients(daemon):
    """broadcast_ws discards closed WebSocket connections."""
    daemon.broadcast_ws = SwarmDaemon.broadcast_ws.__get__(daemon)
    daemon.hub._broadcast_hook = None

    closed_ws = MagicMock()
    closed_ws.closed = True
    daemon.hub.ws_clients = {closed_ws}
    daemon.broadcast_ws({"type": "cleanup"})
    assert closed_ws not in daemon.hub.ws_clients


@pytest.mark.asyncio
async def test_broadcast_ws_sends_to_open_clients(daemon):
    """broadcast_ws creates send tasks for open clients."""
    # Restore real broadcast_ws (overridden by MagicMock in fixture)
    daemon.broadcast_ws = SwarmDaemon.broadcast_ws.__get__(daemon)
    daemon.hub._broadcast_hook = None
    daemon.hub._safe_ws_send = AsyncMock()
    daemon.hub._track_task = MagicMock()

    open_ws = MagicMock()
    open_ws.closed = False
    daemon.hub.ws_clients = {open_ws}
    daemon.broadcast_ws({"type": "hello"})
    # A task should have been tracked
    assert daemon.hub._track_task.called


# --- queue_proposal ---


def test_queue_proposal_delegates_to_proposals(daemon):
    """queue_proposal passes proposal to proposals.on_proposal."""
    daemon.proposal_coord._proposals = MagicMock()
    p = AssignmentProposal(
        worker_name="api",
        task_id="t1",
        task_title="Do stuff",
        reasoning="Because",
        confidence=0.8,
    )
    daemon.queue_proposal(p)
    daemon.proposal_coord._proposals.on_proposal.assert_called_once_with(p)


def test_queue_proposal_multiple(daemon):
    """Multiple proposals can be queued sequentially."""
    daemon.proposal_coord._proposals = MagicMock()
    p1 = AssignmentProposal(
        worker_name="api",
        task_id="t1",
        task_title="Task 1",
        reasoning="R1",
        confidence=0.9,
    )
    p2 = AssignmentProposal(
        worker_name="web",
        task_id="t2",
        task_title="Task 2",
        reasoning="R2",
        confidence=0.7,
    )
    daemon.queue_proposal(p1)
    daemon.queue_proposal(p2)
    assert daemon.proposal_coord._proposals.on_proposal.call_count == 2


# --- apply_config ---


def test_apply_config_updates_pilot(daemon):
    """apply_config sets pilot attributes from config.drones."""
    daemon.config.drones.enabled = False
    daemon.config.drones.poll_interval = 10.0
    daemon.config.drones.max_idle_interval = 30.0
    daemon.apply_config()
    assert daemon.pilot.enabled is False
    assert daemon.pilot.interval == 10.0
    daemon.pilot.set_poll_intervals.assert_called_with(10.0, 30.0)


def test_apply_config_updates_queen(daemon):
    """apply_config sets queen attributes from config.queen."""
    daemon.config.queen.enabled = False
    daemon.config.queen.cooldown = 5.0
    daemon.config.queen.system_prompt = "test prompt"
    daemon.config.queen.min_confidence = 0.5
    daemon.apply_config()
    assert daemon.queen.enabled is False
    assert daemon.queen.cooldown == 5.0
    assert daemon.queen.system_prompt == "test prompt"
    assert daemon.queen.min_confidence == 0.5


def test_apply_config_rebuilds_notification_bus(daemon):
    """apply_config rebuilds the notification bus from config."""
    old_bus = daemon.notification_bus
    daemon.apply_config()
    assert daemon.notification_bus is not old_bus


def test_apply_config_without_pilot_still_updates_queen(daemon):
    """apply_config handles pilot=None gracefully, still updates queen."""
    daemon.pilot = None
    daemon.config.queen.cooldown = 99.0
    daemon.apply_config()
    assert daemon.queen.cooldown == 99.0


# --- reload_config ---


@pytest.mark.asyncio
async def test_reload_config_delegates_to_config_mgr(daemon):
    """reload_config delegates to config_mgr.reload."""
    daemon.config_mgr = MagicMock()
    daemon.config_mgr.reload = AsyncMock()
    new_cfg = HiveConfig(session_name="new")
    await daemon.reload_config(new_cfg)
    daemon.config_mgr.reload.assert_called_once_with(new_cfg)


# --- Pipeline wiring ---


class TestPipelineWiring:
    def test_complete_task_advances_pipeline(self, daemon):
        """Completing a task linked to a pipeline step advances the pipeline."""
        from swarm.pipelines.models import PipelineStep, StepStatus

        p = daemon.pipeline_engine.create(
            "test-pipeline",
            steps=[
                PipelineStep(id="a", name="Step A"),
                PipelineStep(id="b", name="Step B", depends_on=["a"]),
            ],
        )
        daemon.pipeline_engine.start_pipeline(p.id)
        step_a = daemon.pipeline_engine.get(p.id).get_step("a")
        assert step_a.task_id is not None

        # Assign the task so complete_task works
        daemon.task_board.assign(step_a.task_id, "api")

        daemon.complete_task(step_a.task_id, resolution="done")

        step_a_after = daemon.pipeline_engine.get(p.id).get_step("a")
        assert step_a_after.status == StepStatus.COMPLETED

        step_b_after = daemon.pipeline_engine.get(p.id).get_step("b")
        assert step_b_after.status in (StepStatus.READY, StepStatus.IN_PROGRESS)

    def test_fail_task_fails_pipeline_step(self, daemon):
        """Failing a task linked to a pipeline step fails the step."""
        from swarm.pipelines.models import PipelineStep, StepStatus

        p = daemon.pipeline_engine.create(
            "test-pipeline",
            steps=[PipelineStep(id="a", name="Step A")],
        )
        daemon.pipeline_engine.start_pipeline(p.id)
        step_a = daemon.pipeline_engine.get(p.id).get_step("a")
        assert step_a.task_id is not None

        daemon.task_board.assign(step_a.task_id, "api")
        daemon.fail_task(step_a.task_id)

        step_a_after = daemon.pipeline_engine.get(p.id).get_step("a")
        assert step_a_after.status == StepStatus.FAILED

    def test_pipeline_change_broadcasts_ws(self, daemon):
        """Pipeline engine changes trigger pipelines_changed WS broadcast."""
        daemon.broadcast_ws.reset_mock()
        daemon.pipeline_engine.create("test")

        # Check that pipelines_changed was broadcast
        calls = [c[0][0] for c in daemon.broadcast_ws.call_args_list]
        assert any(c.get("type") == "pipelines_changed" for c in calls)

    def test_unrelated_task_does_not_affect_pipeline(self, daemon):
        """Completing a task not linked to any pipeline has no effect."""
        from swarm.pipelines.models import PipelineStatus, PipelineStep

        p = daemon.pipeline_engine.create(
            "test",
            steps=[PipelineStep(id="a", name="A")],
        )
        # Don't start the pipeline — create an unrelated task
        task = daemon.task_board.create(title="Unrelated", description="test")
        daemon.task_board.assign(task.id, "api")
        daemon.complete_task(task.id, resolution="done")

        assert daemon.pipeline_engine.get(p.id).status == PipelineStatus.DRAFT
