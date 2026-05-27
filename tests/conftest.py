"""Shared test fixtures and helpers."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Live-DB safeguard — runs at conftest import time, BEFORE any fixture or
# test code executes. Today (2026-05-06) a test fixture instantiated
# ``SwarmDB()`` with no path arg, which defaulted to ``~/.swarm/swarm.db``;
# the v9 migration ran against the operator's live data and the running
# daemon (still on old code) then DELETE'd 301 of 302 rows on its next
# persist cycle. Recovery required a backup restore. This module-level
# override pins the default to a session-wide tmp dir so the same crash
# can't recur — even from code paths that fire before the per-test
# function-scoped ``_isolate_db_secrets`` fixture below.
import swarm.db.core as _swarm_db_core

_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="swarm-tests-"))
_LIVE_DB_PATH = Path.home() / ".swarm" / "swarm.db"
_LIVE_DB_MTIME_AT_START = _LIVE_DB_PATH.stat().st_mtime if _LIVE_DB_PATH.exists() else None
_swarm_db_core._DEFAULT_DB_PATH = _TEST_DB_DIR / "session-default.db"

from swarm.worker.worker import Worker, WorkerState  # noqa: E402
from tests.fakes.process import FakeWorkerProcess  # noqa: E402

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


@pytest.fixture(autouse=True)
def _isolate_db_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-test override on top of the conftest module-level override."""
    fake_db = tmp_path / "no-swarm.db"
    monkeypatch.setattr("swarm.db.core._DEFAULT_DB_PATH", fake_db)


@pytest.fixture(autouse=True, scope="session")
def _assert_live_db_untouched():
    """Belt-and-suspenders: fail the session if the live ``~/.swarm/swarm.db``
    mtime changed during the test run. The module-level override should
    prevent this; the assertion catches any code path that bypasses it
    (e.g., a test passing the path explicitly, or a subprocess writing
    via the system default)."""
    yield
    if not _LIVE_DB_PATH.exists():
        return
    end_mtime = _LIVE_DB_PATH.stat().st_mtime
    if _LIVE_DB_MTIME_AT_START is not None and end_mtime != _LIVE_DB_MTIME_AT_START:
        raise AssertionError(
            f"LIVE-DB SAFETY: {_LIVE_DB_PATH} was modified during the test session "
            f"(mtime {_LIVE_DB_MTIME_AT_START} → {end_mtime}). "
            f"Some code path bypassed the conftest sandbox. "
            f"Find the offending test before merging."
        )


@pytest.fixture(autouse=True, scope="session")
def _isolate_logging():
    """Prevent tests from writing to the production ``~/.swarm/swarm.log``.

    CLI tests invoke click commands that call ``setup_logging()`` which
    attaches a ``RotatingFileHandler`` pointing at ``~/.swarm/swarm.log``.
    We patch ``setup_logging`` to redirect all file output to ``/dev/null``
    so test warnings never pollute the production debug log.
    """
    import swarm.cli as _cli
    import swarm.logging as _swarm_logging

    _real_setup = _swarm_logging.setup_logging

    def _test_setup(level="WARNING", log_file=None, stderr=False, json_format=False):
        return _real_setup(level=level, log_file="/dev/null", stderr=False, json_format=json_format)

    with (
        patch.object(_swarm_logging, "setup_logging", _test_setup),
        patch.object(_cli, "setup_logging", _test_setup),
    ):
        # Also neutralise the logger right now for tests that never
        # call setup_logging but still emit warnings.
        logger = logging.getLogger("swarm")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.WARNING)
        yield


def make_worker(
    name: str = "api",
    state: WorkerState = WorkerState.BUZZING,
    process: FakeWorkerProcess | None = None,
    resting_since: float | None = None,
    revive_count: int = 0,
    provider_name: str = "claude",
) -> Worker:
    """Create a Worker for testing.

    Parameters
    ----------
    name:
        Worker name.
    state:
        Initial worker state.
    process:
        Fake process for the worker. Defaults to a new ``FakeWorkerProcess``.
    resting_since:
        If set, overrides ``state_since`` (useful for escalation threshold tests).
    revive_count:
        Initial revive counter.
    provider_name:
        Provider name for the worker.
    """
    if process is None:
        process = FakeWorkerProcess(name=name)
    w = Worker(name=name, path="/tmp", provider_name=provider_name, process=process, state=state)
    if resting_since is not None:
        w.state_since = resting_since
    w.revive_count = revive_count
    return w


def make_daemon(
    monkeypatch: pytest.MonkeyPatch | None = None,
    workers: list[Worker] | None = None,
) -> SwarmDaemon:
    """Factory for a minimal SwarmDaemon suitable for unit tests.

    Stubs out Queen session persistence and creates the daemon via
    ``__new__`` (skipping ``__init__``) so no I/O occurs.
    """
    from swarm.config import HiveConfig, QueenConfig
    from swarm.drones.log import DroneLog
    from swarm.drones.pilot import DronePilot
    from swarm.queen.queen import Queen
    from swarm.queen.queue import QueenCallQueue
    from swarm.server.analyzer import QueenAnalyzer
    from swarm.server.broadcast import BroadcastHub
    from swarm.server.config_manager import ConfigManager
    from swarm.server.daemon import SwarmDaemon
    from swarm.server.jira_service import JiraService
    from swarm.server.proposals import ProposalManager
    from swarm.server.resource_monitor import ResourceMonitor
    from swarm.server.task_manager import TaskManager
    from swarm.server.test_runner import TestRunner
    from swarm.server.worker_service import WorkerService
    from swarm.tasks.board import TaskBoard
    from swarm.tasks.history import TaskHistory
    from swarm.tasks.proposal import ProposalStore
    from swarm.tunnel import TunnelManager

    if monkeypatch:
        monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
        monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)

    cfg = HiveConfig(session_name="test")
    d = SwarmDaemon.__new__(SwarmDaemon)
    d.config = cfg

    if workers is None:
        workers = [
            Worker(name="api", path="/tmp/api", process=FakeWorkerProcess(name="api")),
            Worker(name="web", path="/tmp/web", process=FakeWorkerProcess(name="web")),
        ]
    d.workers = workers
    d.pool = None
    d._worker_lock = asyncio.Lock()
    d.drone_log = DroneLog()
    d.task_board = TaskBoard()
    d.task_history = TaskHistory(log_file=Path(tempfile.mktemp(suffix=".jsonl")))
    d.queen = Queen(config=QueenConfig(cooldown=0.0), session_name="test")
    d.queen_queue = QueenCallQueue(max_concurrent=2)
    d.proposal_store = ProposalStore()
    d.notification_bus = MagicMock()
    d.pilot = MagicMock(spec=DronePilot)
    d.pilot.enabled = True
    d.pilot.toggle = MagicMock(return_value=False)
    d._bg_tasks: set[asyncio.Task[object]] = set()
    d.hub = BroadcastHub(track_task=lambda t: d._bg_tasks.add(t))
    d.hub.ws_clients = set()
    d.hub.terminal_ws_clients = set()
    d.start_time = 0.0
    d.broadcast_ws = MagicMock()
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
    d.graph_mgr = None
    d._mtime_task = None
    d._usage_task = None
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
    d.email = MagicMock()
    d.tasks = TaskManager(
        task_board=d.task_board,
        task_history=d.task_history,
        drone_log=d.drone_log,
        pilot=d.pilot,
    )
    d.config_mgr = ConfigManager(
        config=cfg,
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        apply_config=d.apply_config,
        get_pilot=lambda: d.pilot,
        rebuild_graph=lambda: None,
    )
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
    # InvariantReconciler — extracted Phase 1 of daemon-god-object-refactor.
    # The factory builds the daemon via __new__ so the live __init__ wiring
    # doesn't run; mirror it here so daemon._working_workers() /
    # daemon._run_invariant_reconciliation() delegations resolve.
    from swarm.server.invariants import InvariantReconciler

    d.blocker_store = None
    d.invariants = InvariantReconciler(
        task_board=d.task_board,
        task_history=d.task_history,
        drone_log=d.drone_log,
        blocker_store=d.blocker_store,
        get_workers=lambda: d.workers,
    )
    # PlaybookOps — extracted Phase 2 of daemon-god-object-refactor.
    # Same fixture-wiring caveat: tests reaching into
    # daemon._fire_playbook_synthesis / _recall_playbooks_for_task /
    # _consolidate_learnings need this binding.
    from swarm.config import PlaybookConfig
    from swarm.server.playbook_ops import PlaybookOps

    # synthesizer left None — pre-refactor the daemon fixture didn't bind
    # one, and recall_for_task / fire_synthesis both short-circuit when
    # store / synthesizer is absent.  Matching the old behavior keeps the
    # 4 complete_task tests' implicit assumption (no real synth fires).
    d.playbook_store = None
    d.playbook_synthesizer = None
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
    return d
