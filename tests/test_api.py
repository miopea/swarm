"""Tests for server/api.py — REST + WebSocket API."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from swarm.config import GroupConfig, HiveConfig, QueenConfig, WorkerConfig
from swarm.drones.log import DroneLog
from swarm.drones.pilot import DronePilot
from swarm.queen.queen import Queen
from swarm.server.analyzer import QueenAnalyzer
from swarm.server.api import create_app
from swarm.server.config_manager import ConfigManager
from swarm.server.daemon import SwarmDaemon
from swarm.server.email_service import EmailService
from swarm.server.proposals import ProposalManager
from swarm.server.task_manager import TaskManager
from swarm.server.worker_service import WorkerService
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskHistory
from swarm.tasks.proposal import AssignmentProposal, ProposalStore
from swarm.worker.worker import WORKER_KIND_QUEEN, Worker, WorkerState
from tests.fakes.process import FakeWorkerProcess

# Known password used by all test daemons, so API auth always passes.
_TEST_PASSWORD = "test-secret"
# Default headers for API requests (CSRF requires X-Requested-With)
_API_HEADERS = {"X-Requested-With": "TestClient"}
# Headers for config-mutating endpoints (require Bearer token)
_AUTH_HEADERS = {**_API_HEADERS, "Authorization": f"Bearer {_TEST_PASSWORD}"}


def _inject_session_cookie(client: TestClient, password: str = _TEST_PASSWORD) -> None:
    """Inject a valid session cookie into a test client."""
    from swarm.auth.session import _COOKIE_NAME, create_session_cookie

    cookie_val, _ = create_session_cookie(password)
    client.session.cookie_jar.update_cookies({_COOKIE_NAME: cookie_val})


@pytest.fixture
def daemon(monkeypatch):
    """Create a minimal daemon without starting it."""
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)

    cfg = HiveConfig(session_name="test", api_password=_TEST_PASSWORD)
    d = SwarmDaemon.__new__(SwarmDaemon)
    d.config = cfg
    d.workers = [
        Worker(name="api", path="/tmp/api", process=FakeWorkerProcess(name="api")),
        Worker(name="web", path="/tmp/web", process=FakeWorkerProcess(name="web")),
    ]
    import asyncio

    d._worker_lock = asyncio.Lock()
    d.drone_log = DroneLog()
    d.task_board = TaskBoard()
    d.task_history = TaskHistory(log_file=Path(tempfile.mktemp(suffix=".jsonl")))
    d.queen = Queen(config=QueenConfig(cooldown=0.0), session_name="test")

    # In-memory Queen chat store for the interactive-Queen routes that
    # landed in the foundation pass.  Uses a throwaway temp DB so the
    # fixture stays independent of any on-disk state.
    from swarm.db.core import SwarmDB
    from swarm.db.queen_chat_store import QueenChatStore

    _chat_db_path = Path(tempfile.mktemp(suffix=".db"))
    d.swarm_db = SwarmDB(_chat_db_path)
    d.queen_chat = QueenChatStore(d.swarm_db)

    from swarm.queen.queue import QueenCallQueue

    d.queen_queue = QueenCallQueue(max_concurrent=2)
    d.proposal_store = ProposalStore()
    d.notification_bus = MagicMock()
    d.pilot = MagicMock(spec=DronePilot)
    d.pilot.enabled = True
    d.pilot.toggle = MagicMock(return_value=False)
    d.pilot.get_diagnostics = MagicMock(
        return_value={
            "running": True,
            "enabled": True,
            "task_alive": True,
            "tick": 0,
            "idle_streak": 0,
        }
    )
    d._bg_tasks: set[asyncio.Task[object]] = set()
    d.broadcast_ws = MagicMock()

    from swarm.server.broadcast import BroadcastHub

    d.hub = BroadcastHub(track_task=lambda t: d._bg_tasks.add(t))
    d.hub.ws_clients = set()
    d.hub.terminal_ws_clients = set()
    d.pool = None
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
    d.graph_mgr = None
    d.email = EmailService(
        drone_log=d.drone_log,
        queen=d.queen,
        graph_mgr=d.graph_mgr,
        broadcast_ws=d.broadcast_ws,
    )
    d.tasks = TaskManager(
        task_board=d.task_board,
        task_history=d.task_history,
        drone_log=d.drone_log,
        pilot=d.pilot,
    )
    d.send_to_worker = AsyncMock()
    d._heartbeat_task = None
    d._usage_task = None
    d._heartbeat_snapshot = {}
    from swarm.server.escalation_handler import EscalationHandler

    d.escalation = EscalationHandler(
        broadcast_ws=d.broadcast_ws,
        notification_bus=d.notification_bus,
        proposal_store=d.proposal_store,
        get_analyzer=lambda: d.analyzer,
        get_queen=lambda: d.queen,
        emit=d.emit,
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
        rebuild_graph=lambda: None,
        get_worker_svc=lambda: d.worker_svc,
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
    # InvariantReconciler + PlaybookOps — extracted Phase 1+2 of
    # daemon-god-object-refactor.  __new__ skips the live __init__ wiring;
    # mirror it here so daemon delegations resolve.
    from swarm.config import PlaybookConfig
    from swarm.server.invariants import InvariantReconciler
    from swarm.server.playbook_ops import PlaybookOps

    d.blocker_store = None
    d.invariants = InvariantReconciler(
        task_board=d.task_board,
        task_history=d.task_history,
        drone_log=d.drone_log,
        blocker_store=d.blocker_store,
        get_workers=lambda: d.workers,
    )
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
    return d


@pytest.fixture
async def client(daemon):
    """Create an aiohttp test client with a valid session cookie."""
    app = create_app(daemon, enable_web=False)
    async with TestClient(TestServer(app)) as client:
        _inject_session_cookie(client)
        yield client


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/api/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert data["workers"] == 2
    assert "version" in data
    assert "build_sha" in data
    assert isinstance(data["build_sha"], str)
    # Holder-drift key is always present — daemon surfaces the pool's
    # drift state for the dashboard banner. On a fresh test daemon with
    # no pool yet it may be None; the important invariant is the field
    # is reachable without crashing the health endpoint.
    assert "holder_drift" in data


@pytest.mark.asyncio
async def test_holder_drift_endpoint_reports_pool_state(client, daemon):
    """/api/holder/drift returns the pool's holder_drift dict verbatim.

    The dashboard banner depends on this contract — drift=true means
    the holder bytecode predates holder.py on disk and Reload won't
    help. See swarm.pty.pool.ProcessPool._check_holder_version."""

    class _FakePool:
        def __init__(self) -> None:
            self.holder_drift = {
                "checked": True,
                "drift": True,
                "holder_hash": "a" * 64,
                "daemon_hash": "b" * 64,
                "holder_pid": 12345,
                "unknown": False,
            }

    daemon.pool = _FakePool()
    resp = await client.get("/api/holder/drift")
    assert resp.status == 200
    data = await resp.json()
    assert data["drift"] is True
    assert data["holder_pid"] == 12345
    assert data["holder_hash"] == "a" * 64
    assert data["daemon_hash"] == "b" * 64


@pytest.mark.asyncio
async def test_workers_list(client):
    resp = await client.get("/api/workers")
    assert resp.status == 200
    data = await resp.json()
    assert len(data["workers"]) == 2
    assert data["workers"][0]["name"] == "api"


@pytest.mark.asyncio
async def test_workers_reorder(client, daemon):
    daemon.config.workers = [WorkerConfig("api", "/tmp/api"), WorkerConfig("web", "/tmp/web")]
    resp = await client.post(
        "/api/workers/reorder",
        json={"order": ["web", "api"]},
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    # Verify new order in API response
    resp2 = await client.get("/api/workers")
    data = await resp2.json()
    assert [w["name"] for w in data["workers"]] == ["web", "api"]
    # Verify config.workers also reordered (prevents save_config_to_db from resetting sort_order)
    assert [wc.name for wc in daemon.config.workers] == ["web", "api"]


@pytest.mark.asyncio
async def test_workers_reorder_invalid(client):
    resp = await client.post(
        "/api/workers/reorder",
        json={"order": "not-a-list"},
        headers=_API_HEADERS,
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_workers_reorder_db_failure_logs_warning(client, daemon, caplog):
    """Phase 9 of #328: ``/api/workers/reorder`` no longer fails
    silently on DB write errors.

    The handler bypasses ``config_mgr.save()`` (it persists
    sort_order via raw SQL UPDATE).  Pre-fix that SQL was
    unwrapped — a transient DB lock or schema drift would surface
    as a 500 with no operator log line.  Phase 9 wraps the loop in
    try/except + WARNING log for parity with the rest of the
    config-save chain.
    """
    import logging
    from unittest.mock import patch

    daemon.config.workers = [WorkerConfig("api", "/tmp/api"), WorkerConfig("web", "/tmp/web")]

    with patch.object(daemon.swarm_db, "execute", side_effect=RuntimeError("simulated lock")):
        with caplog.at_level(logging.WARNING, logger="swarm.server.routes.workers"):
            resp = await client.post(
                "/api/workers/reorder",
                json={"order": ["web", "api"]},
                headers=_API_HEADERS,
            )
            assert resp.status == 500

    failures = [
        r
        for r in caplog.records
        if r.name == "swarm.server.routes.workers"
        and "failed to persist sort_order" in r.getMessage()
    ]
    assert failures, (
        "DB write failure must produce a WARNING for operator visibility.  "
        f"Records: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    assert failures[0].levelno >= logging.WARNING
    assert failures[0].exc_info is not None


@pytest.mark.asyncio
async def test_worker_detail_not_found(client):
    resp = await client.get("/api/workers/nonexistent")
    assert resp.status == 404


@pytest.mark.asyncio
async def test_worker_send_empty_message(client):
    resp = await client.post("/api/workers/api/send", json={"message": ""}, headers=_API_HEADERS)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_worker_send_not_string(client):
    resp = await client.post("/api/workers/api/send", json={"message": 123}, headers=_API_HEADERS)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_worker_update_rename(client, daemon):
    resp = await client.patch(
        "/api/workers/api",
        json={"name": "api-v2"},
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["worker"] == "api-v2"
    assert daemon.workers[0].name == "api-v2"


@pytest.mark.asyncio
async def test_worker_update_path(client, daemon):
    resp = await client.patch(
        "/api/workers/api",
        json={"path": "/tmp/new-api"},
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    assert daemon.workers[0].path == "/tmp/new-api"


@pytest.mark.asyncio
async def test_worker_update_not_found(client):
    resp = await client.patch(
        "/api/workers/nonexistent",
        json={"name": "foo"},
        headers=_API_HEADERS,
    )
    assert resp.status == 404


@pytest.mark.asyncio
async def test_worker_update_invalid_name(client):
    resp = await client.patch(
        "/api/workers/api",
        json={"name": "bad name!"},
        headers=_API_HEADERS,
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_worker_update_duplicate_name(client):
    # 409 Conflict — another worker already holds the requested name.
    # Phase C of the duplication-cluster sweep aligned the HTTP error
    # decorators on 409 for SwarmOperationError ("state can't proceed
    # in current state"); a duplicate name is a textbook conflict.
    resp = await client.patch(
        "/api/workers/api",
        json={"name": "web"},
        headers=_API_HEADERS,
    )
    assert resp.status == 409


@pytest.mark.asyncio
async def test_worker_continue(client):
    resp = await client.post("/api/workers/api/continue", headers=_API_HEADERS)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_worker_kill(client, monkeypatch):
    with patch("swarm.worker.manager.kill_worker", new_callable=AsyncMock):
        resp = await client.post("/api/workers/api/kill", headers=_API_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "killed"


@pytest.mark.asyncio
async def test_drone_log(client):
    resp = await client.get("/api/drones/log")
    assert resp.status == 200
    data = await resp.json()
    assert "entries" in data


@pytest.mark.asyncio
async def test_drone_log_limit_capped(client):
    resp = await client.get("/api/drones/log?limit=99999")
    assert resp.status == 200


@pytest.mark.asyncio
async def test_drone_status(client):
    resp = await client.get("/api/drones/status")
    assert resp.status == 200
    data = await resp.json()
    assert "enabled" in data


@pytest.mark.asyncio
async def test_drone_toggle(client):
    resp = await client.post("/api/drones/toggle", headers=_API_HEADERS)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_tasks_crud(client):
    # Create
    resp = await client.post("/api/tasks", json={"title": "Fix bug"}, headers=_API_HEADERS)
    assert resp.status == 201
    data = await resp.json()
    task_id = data["id"]

    # List
    resp = await client.get("/api/tasks")
    assert resp.status == 200
    data = await resp.json()
    assert len(data["tasks"]) == 1

    # Assign
    resp = await client.post(
        f"/api/tasks/{task_id}/assign", json={"worker": "api"}, headers=_API_HEADERS
    )
    assert resp.status == 200

    # Complete
    resp = await client.post(f"/api/tasks/{task_id}/complete", headers=_API_HEADERS)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_create_task_invalid_title(client):
    resp = await client.post("/api/tasks", json={"title": ""}, headers=_API_HEADERS)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_get_task_by_id_returns_full_dict(client):
    """Cleanup batch: GET /api/tasks/{id} powers the dashboard's
    showTaskEditorById helper. Must return the rich dict shape (tags,
    depends_on, attachments, status, priority, ...) — not just the
    7-field list-view summary."""
    resp = await client.post(
        "/api/tasks",
        json={"title": "deeplink", "priority": "high"},
        headers=_API_HEADERS,
    )
    data = await resp.json()
    task_id = data["id"]

    resp = await client.get(f"/api/tasks/{task_id}")
    assert resp.status == 200
    full = await resp.json()
    assert full["id"] == task_id
    assert full["title"] == "deeplink"
    assert full["priority"] == "high"
    # Rich-dict shape — fields the editor needs that the list-view
    # summary doesn't carry. Values may be empty/default but the keys
    # must exist so the JS opener can read them safely.
    for key in (
        "depends_on",
        "tags",
        "attachments",
        "resolution",
        "block_reason",
        "is_cross_project",
        "source_worker",
        "target_worker",
        "dependency_type",
        "acceptance_criteria",
        "context_refs",
        "assigned_worker",
        "status",
        "task_type",
        "number",
    ):
        assert key in full, f"missing {key!r} in task-by-id response"


@pytest.mark.asyncio
async def test_get_task_by_id_returns_404_for_unknown(client):
    resp = await client.get("/api/tasks/this-id-does-not-exist")
    assert resp.status == 404


@pytest.mark.asyncio
async def test_create_task_title_too_long(client):
    long_title = "x" * 501
    resp = await client.post("/api/tasks", json={"title": long_title}, headers=_API_HEADERS)
    assert resp.status == 400
    data = await resp.json()
    assert "too long" in data["error"].lower()


@pytest.mark.asyncio
async def test_create_task_description_too_long(client):
    long_desc = "x" * 10_001
    resp = await client.post(
        "/api/tasks",
        json={"title": "Valid", "description": long_desc},
        headers=_API_HEADERS,
    )
    assert resp.status == 400
    data = await resp.json()
    assert "too long" in data["error"].lower()


@pytest.mark.asyncio
async def test_create_task_invalid_priority(client):
    resp = await client.post(
        "/api/tasks", json={"title": "Test", "priority": "mega"}, headers=_API_HEADERS
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_assign_task_nonexistent_worker(client):
    # First create a task
    resp = await client.post("/api/tasks", json={"title": "Test"}, headers=_API_HEADERS)
    data = await resp.json()
    task_id = data["id"]

    # Assign to non-existent worker
    resp = await client.post(
        f"/api/tasks/{task_id}/assign", json={"worker": "nonexistent"}, headers=_API_HEADERS
    )
    assert resp.status == 404


@pytest.mark.asyncio
async def test_assign_task_not_found(client):
    resp = await client.post(
        "/api/tasks/nonexistent/assign", json={"worker": "api"}, headers=_API_HEADERS
    )
    assert resp.status == 404


@pytest.mark.asyncio
async def test_unassign_task(client):
    # Create and assign a task
    resp = await client.post("/api/tasks", json={"title": "Test"}, headers=_API_HEADERS)
    data = await resp.json()
    task_id = data["id"]
    await client.post(f"/api/tasks/{task_id}/assign", json={"worker": "api"}, headers=_API_HEADERS)
    # Unassign it
    resp = await client.post(f"/api/tasks/{task_id}/unassign", headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "unassigned"


@pytest.mark.asyncio
async def test_unassign_task_not_found(client):
    resp = await client.post("/api/tasks/nonexistent/unassign", headers=_API_HEADERS)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_unassign_task_wrong_status(client):
    """Cannot unassign a pending task."""
    resp = await client.post("/api/tasks", json={"title": "Test"}, headers=_API_HEADERS)
    data = await resp.json()
    task_id = data["id"]
    resp = await client.post(f"/api/tasks/{task_id}/unassign", headers=_API_HEADERS)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_complete_task_not_found(client):
    resp = await client.post("/api/tasks/nonexistent/complete", headers=_API_HEADERS)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_worker_kill_not_found(client):
    resp = await client.post("/api/workers/nonexistent/kill", headers=_API_HEADERS)
    assert resp.status == 404


# --- Config API ---


@pytest.fixture
def daemon_with_path(daemon, tmp_path):
    """Daemon with a source_path so save_config works."""
    daemon.config.source_path = str(tmp_path / "swarm.yaml")
    daemon.config.workers = [WorkerConfig("api", "/tmp/api"), WorkerConfig("web", "/tmp/web")]
    daemon.config.groups = []
    # Stub reload_config to just update config without starting async tasks
    daemon.reload_config = AsyncMock()
    return daemon


@pytest.fixture
async def config_client(daemon_with_path):
    app = create_app(daemon_with_path, enable_web=False)
    async with TestClient(TestServer(app)) as client:
        _inject_session_cookie(client)
        yield client


@pytest.mark.asyncio
async def test_get_config(config_client):
    resp = await config_client.get("/api/config")
    assert resp.status == 200
    data = await resp.json()
    assert "session_name" in data
    assert "drones" in data
    assert "queen" in data
    assert "notifications" in data
    assert "workers" in data


@pytest.mark.asyncio
async def test_update_config_drones(config_client):
    resp = await config_client.put(
        "/api/config",
        json={"drones": {"poll_interval": 15.0}},
        headers=_AUTH_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["drones"]["poll_interval"] == 15.0


@pytest.mark.asyncio
async def test_update_config_validation(config_client):
    resp = await config_client.put(
        "/api/config",
        json={"drones": {"poll_interval": "not_a_number"}},
        headers=_AUTH_HEADERS,
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_add_worker_api(config_client, tmp_path):
    """Add a worker with a valid path."""
    worker_dir = tmp_path / "new-project"
    worker_dir.mkdir()
    with patch("swarm.worker.manager.add_worker_live", new_callable=AsyncMock) as mock_add:
        mock_add.return_value = Worker(
            name="new-proj", path=str(worker_dir), process=FakeWorkerProcess(name="new-proj")
        )
        resp = await config_client.post(
            "/api/config/workers",
            json={"name": "new-proj", "path": str(worker_dir)},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["worker"] == "new-proj"


@pytest.mark.asyncio
async def test_add_worker_accepts_isolation_and_identity(config_client, tmp_path):
    """Phase 6 of #328: ``POST /api/config/workers`` should accept
    every writable WorkerConfig field, not just the cherry-picked
    ``name``/``path``/``description``/``provider``.

    Audit Phase 1 flagged ``isolation`` and ``identity`` as
    L2-4 gaps — operator-editable in the dataclass and persisted
    in the DB, but the create endpoint silently dropped them.
    Adding a worker via the API with these fields set never wrote
    them anywhere; on restart the loader produced ``WorkerConfig(
    name=..., path=..., isolation='', identity='')`` regardless of
    what the create call sent.
    """
    worker_dir = tmp_path / "isolated-worker"
    worker_dir.mkdir()
    with patch("swarm.worker.manager.add_worker_live", new_callable=AsyncMock) as mock_add:
        mock_add.return_value = Worker(
            name="iso", path=str(worker_dir), process=FakeWorkerProcess(name="iso")
        )
        resp = await config_client.post(
            "/api/config/workers",
            json={
                "name": "iso",
                "path": str(worker_dir),
                "description": "isolated worker",
                "provider": "claude",
                "isolation": "worktree",
                "identity": "~/.swarm/identities/iso.md",
            },
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 201

    # Verify the new fields landed on the in-memory config (the GET
    # endpoint serializes the same source the DB save reads from, so
    # if it shows up here it persists end-to-end).
    resp = await config_client.get("/api/config", headers=_AUTH_HEADERS)
    cfg = await resp.json()
    iso = next((w for w in cfg["workers"] if w["name"] == "iso"), None)
    assert iso is not None, f"worker 'iso' missing from config: {cfg.get('workers')}"
    assert iso.get("isolation") == "worktree", f"isolation field not persisted: {iso}"
    assert iso.get("identity") == "~/.swarm/identities/iso.md", (
        f"identity field not persisted: {iso}"
    )


@pytest.mark.asyncio
async def test_add_worker_warns_on_unknown_body_field(config_client, tmp_path, caplog):
    """Phase 6 fail-loud guard at the create-worker endpoint.

    Same ``ignoring unknown sub-key`` signal the bulk autosave already
    emits — applied to ``POST /api/config/workers``.  Future drift
    between dashboard and server (e.g. dashboard adds a new field but
    server forgets) surfaces as a default-level WARNING log.
    """
    import logging

    worker_dir = tmp_path / "warn-test"
    worker_dir.mkdir()
    with patch("swarm.worker.manager.add_worker_live", new_callable=AsyncMock) as mock_add:
        mock_add.return_value = Worker(
            name="warn", path=str(worker_dir), process=FakeWorkerProcess(name="warn")
        )
        with caplog.at_level(logging.WARNING, logger="swarm.server.config_manager"):
            resp = await config_client.post(
                "/api/config/workers",
                json={
                    "name": "warn",
                    "path": str(worker_dir),
                    "totally_not_a_real_field": "garbage",
                },
                headers=_AUTH_HEADERS,
            )
            assert resp.status == 201

    unknowns = [r for r in caplog.records if "totally_not_a_real_field" in r.getMessage()]
    assert unknowns, (
        "Unknown worker body field must produce a WARNING.  "
        f"Records: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    assert unknowns[0].levelno >= logging.WARNING


@pytest.mark.asyncio
async def test_add_worker_duplicate(config_client, tmp_path):
    worker_dir = tmp_path / "api"
    worker_dir.mkdir()
    resp = await config_client.post(
        "/api/config/workers",
        json={"name": "api", "path": str(worker_dir)},
        headers=_AUTH_HEADERS,
    )
    assert resp.status == 409


@pytest.mark.asyncio
async def test_remove_worker_api(config_client):
    with patch("swarm.worker.manager.kill_worker", new_callable=AsyncMock):
        resp = await config_client.delete("/api/config/workers/api", headers=_AUTH_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "removed"


@pytest.mark.asyncio
async def test_add_group(config_client):
    resp = await config_client.post(
        "/api/config/groups",
        json={"name": "team", "workers": ["api", "web"]},
        headers=_AUTH_HEADERS,
    )
    assert resp.status == 201


@pytest.mark.asyncio
async def test_update_group(config_client):
    # First add a group
    await config_client.post(
        "/api/config/groups",
        json={"name": "team", "workers": ["api"]},
        headers=_AUTH_HEADERS,
    )
    # Then update it
    resp = await config_client.put(
        "/api/config/groups/team",
        json={"workers": ["api", "web"]},
        headers=_AUTH_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["workers"] == ["api", "web"]


@pytest.mark.asyncio
async def test_rename_group(config_client):
    await config_client.post(
        "/api/config/groups",
        json={"name": "old-name", "workers": ["api"]},
        headers=_AUTH_HEADERS,
    )
    resp = await config_client.put(
        "/api/config/groups/old-name",
        json={"name": "new-name", "workers": ["api", "web"]},
        headers=_AUTH_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["group"] == "new-name"
    assert data["workers"] == ["api", "web"]


@pytest.mark.asyncio
async def test_rename_group_updates_default_group(daemon_with_path, tmp_path):
    daemon_with_path.config.groups = []
    daemon_with_path.config.default_group = "old-name"
    from swarm.config import GroupConfig

    daemon_with_path.config.groups.append(GroupConfig("old-name", ["api"]))
    app = create_app(daemon_with_path, enable_web=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.put(
            "/api/config/groups/old-name",
            json={"name": "new-name", "workers": ["api"]},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        assert daemon_with_path.config.default_group == "new-name"


@pytest.mark.asyncio
async def test_rename_group_duplicate(config_client):
    await config_client.post(
        "/api/config/groups",
        json={"name": "group-a", "workers": []},
        headers=_AUTH_HEADERS,
    )
    await config_client.post(
        "/api/config/groups",
        json={"name": "group-b", "workers": []},
        headers=_AUTH_HEADERS,
    )
    resp = await config_client.put(
        "/api/config/groups/group-a",
        json={"name": "group-b", "workers": []},
        headers=_AUTH_HEADERS,
    )
    assert resp.status == 409


@pytest.mark.asyncio
async def test_remove_group(config_client):
    await config_client.post(
        "/api/config/groups",
        json={"name": "disposable", "workers": []},
        headers=_AUTH_HEADERS,
    )
    resp = await config_client.delete("/api/config/groups/disposable", headers=_AUTH_HEADERS)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_add_worker_returns_apply_result(config_client, tmp_path):
    """Phase 7 of #328: every dispatch-using save endpoint includes
    ``_apply_result`` in the response so the dashboard can surface
    per-field success/failure to the operator.

    Sister of the bulk autosave's ``_apply_result`` returned from
    PUT /api/config — but for the granular CRUD endpoints.
    """
    worker_dir = tmp_path / "ar-test"
    worker_dir.mkdir()
    with patch("swarm.worker.manager.add_worker_live", new_callable=AsyncMock) as mock_add:
        mock_add.return_value = Worker(
            name="ar", path=str(worker_dir), process=FakeWorkerProcess(name="ar")
        )
        resp = await config_client.post(
            "/api/config/workers",
            json={
                "name": "ar",
                "path": str(worker_dir),
                "isolation": "worktree",
                "stale_field_xyz": "ignored",
            },
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 201
        data = await resp.json()

    assert "_apply_result" in data
    ar = data["_apply_result"]
    assert "isolation" in ar["consumed"], f"isolation should appear in consumed: {ar}"
    assert "stale_field_xyz" in ar["unknown"], f"unknown body key should appear in unknown: {ar}"


@pytest.mark.asyncio
async def test_add_group_returns_apply_result(config_client):
    """Phase 7: POST /api/config/groups returns _apply_result."""
    resp = await config_client.post(
        "/api/config/groups",
        json={"name": "ar-group", "workers": [], "phantom_field": True},
        headers=_AUTH_HEADERS,
    )
    assert resp.status == 201
    data = await resp.json()

    assert "_apply_result" in data
    ar = data["_apply_result"]
    assert "workers" in ar["consumed"]
    assert "phantom_field" in ar["unknown"]


@pytest.mark.asyncio
async def test_update_group_returns_apply_result(config_client):
    """Phase 7: PUT /api/config/groups/{name} returns _apply_result."""
    await config_client.post(
        "/api/config/groups",
        json={"name": "ar-update", "workers": []},
        headers=_AUTH_HEADERS,
    )
    resp = await config_client.put(
        "/api/config/groups/ar-update",
        json={"workers": ["api"], "another_phantom": "x"},
        headers=_AUTH_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()

    assert "_apply_result" in data
    ar = data["_apply_result"]
    assert "workers" in ar["consumed"]
    assert "another_phantom" in ar["unknown"]


@pytest.mark.asyncio
async def test_add_group_warns_on_unknown_body_field(config_client, caplog):
    """Phase 6 fail-loud guard at POST /api/config/groups.

    GroupConfig only has ``name`` + ``workers``.  Anything else the
    dashboard sends is silently ignored pre-fix; this asserts a
    WARNING surfaces naming the offending key.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="swarm.server.config_manager"):
        resp = await config_client.post(
            "/api/config/groups",
            json={
                "name": "warntest",
                "workers": [],
                "fictional_extra_field": 42,
            },
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 201

    unknowns = [r for r in caplog.records if "fictional_extra_field" in r.getMessage()]
    assert unknowns, (
        "Unknown group body field must produce a WARNING.  "
        f"Records: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_update_group_warns_on_unknown_body_field(config_client, caplog):
    """Phase 6 fail-loud guard at PUT /api/config/groups/{name}."""
    import logging

    await config_client.post(
        "/api/config/groups",
        json={"name": "warn-update", "workers": []},
        headers=_AUTH_HEADERS,
    )

    with caplog.at_level(logging.WARNING, logger="swarm.server.config_manager"):
        resp = await config_client.put(
            "/api/config/groups/warn-update",
            json={"workers": [], "another_unknown_field": True},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200

    unknowns = [r for r in caplog.records if "another_unknown_field" in r.getMessage()]
    assert unknowns, (
        "Unknown group update field must produce a WARNING.  "
        f"Records: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_create_then_immediately_edit_group_persists(config_client):
    """Regression for #328 Bug A at the API layer.

    Operator creates a group, immediately edits it to add workers,
    saves.  Both calls must succeed and the second-call response must
    confirm the new membership reached the in-memory config.  GET
    /api/config then returns the current state — used by the
    Phase 5 dashboard reconciliation as the source of truth that
    replaces the stale page-load Jinja.

    Pre-fix the dashboard's editGroup() read membership from
    page-load Jinja that didn't know about the just-created group,
    so the modal opened with empty members and Save would write
    [] to the DB.  Now editGroup() reads from a JS-side state cache
    that's mutated in lockstep with these API calls.
    """
    # Step 1: create
    resp = await config_client.post(
        "/api/config/groups",
        json={"name": "fresh", "workers": []},
        headers=_AUTH_HEADERS,
    )
    assert resp.status == 201

    # Step 2: immediately edit, no full-page reload between calls
    resp = await config_client.put(
        "/api/config/groups/fresh",
        json={"workers": ["api", "web"]},
        headers=_AUTH_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["workers"] == ["api", "web"]

    # Step 3: GET /api/config — same source the Phase 5 dashboard
    # reconciliation will use to refresh its cache after every save.
    resp = await config_client.get("/api/config", headers=_AUTH_HEADERS)
    assert resp.status == 200
    cfg = await resp.json()
    matching = [g for g in cfg.get("groups", []) if g["name"] == "fresh"]
    assert matching, f"group 'fresh' missing from /api/config: {cfg.get('groups')}"
    assert matching[0]["workers"] == ["api", "web"]


@pytest.mark.asyncio
async def test_config_auth_required(daemon_with_path, tmp_path):
    """Mutating config endpoints reject requests with wrong or missing token."""
    daemon_with_path.config.api_password = "secret"
    app = create_app(daemon_with_path, enable_web=False)
    async with TestClient(TestServer(app)) as client:
        # No Bearer token at all → 401
        resp = await client.put(
            "/api/config",
            json={"drones": {"poll_interval": 5.0}},
            headers=_API_HEADERS,
        )
        assert resp.status == 401
        # Wrong Bearer token → 401
        resp = await client.put(
            "/api/config",
            json={"drones": {"poll_interval": 5.0}},
            headers={**_API_HEADERS, "Authorization": "Bearer wrong-token"},
        )
        assert resp.status == 401


@pytest.mark.asyncio
async def test_config_auth_pass(daemon_with_path, tmp_path):
    """Correct password passes auth check."""
    daemon_with_path.config.api_password = "secret"
    app = create_app(daemon_with_path, enable_web=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.put(
            "/api/config",
            json={"drones": {"poll_interval": 5.0}},
            headers={**_API_HEADERS, "Authorization": "Bearer secret"},
        )
        assert resp.status == 200


@pytest.mark.asyncio
async def test_update_config_strips_api_password(daemon_with_path, tmp_path):
    """PUT /api/config must never leak api_password in the response."""
    daemon_with_path.config.api_password = "supersecret"
    app = create_app(daemon_with_path, enable_web=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.put(
            "/api/config",
            json={"drones": {"poll_interval": 5.0}},
            headers={**_API_HEADERS, "Authorization": "Bearer supersecret"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert "api_password" not in data


@pytest.mark.asyncio
async def test_list_projects(config_client, tmp_path):
    """GET /api/config/projects lists git repos."""
    # projects_dir is ~/projects by default, may not have repos — just check 200
    resp = await config_client.get("/api/config/projects")
    assert resp.status == 200
    data = await resp.json()
    assert "projects" in data


# --- Phase 2: New API endpoints ---


@pytest.mark.asyncio
async def test_worker_interrupt(client):
    resp = await client.post("/api/workers/api/interrupt", headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "interrupted"


@pytest.mark.asyncio
async def test_worker_interrupt_not_found(client):
    resp = await client.post("/api/workers/nonexistent/interrupt", headers=_API_HEADERS)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_worker_revive(client, daemon):
    daemon.workers[0].state = WorkerState.STUNG
    with patch("swarm.worker.manager.revive_worker", new_callable=AsyncMock):
        resp = await client.post("/api/workers/api/revive", headers=_API_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "revived"


@pytest.mark.asyncio
async def test_worker_revive_not_found(client):
    resp = await client.post("/api/workers/nonexistent/revive", headers=_API_HEADERS)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_workers_launch(client, daemon):
    daemon.config.workers = [
        WorkerConfig("new1", "/tmp/new1"),
        WorkerConfig("new2", "/tmp/new2"),
    ]
    new_worker = Worker(name="new1", path="/tmp/new1", process=FakeWorkerProcess(name="new1"))
    with patch(
        "swarm.worker.manager.add_worker_live",
        new_callable=AsyncMock,
        return_value=new_worker,
    ):
        resp = await client.post(
            "/api/workers/launch",
            json={"workers": ["new1"]},
            headers=_API_HEADERS,
        )
    assert resp.status == 201
    data = await resp.json()
    assert "new1" in data["launched"]


@pytest.mark.asyncio
async def test_workers_launch_preserves_request_order(client, daemon):
    """Workers should launch in the order specified by the request, not config order."""
    daemon.config.workers = [
        WorkerConfig("alpha", "/tmp/alpha"),
        WorkerConfig("beta", "/tmp/beta"),
        WorkerConfig("gamma", "/tmp/gamma"),
    ]
    launched_order: list[str] = []

    async def fake_add(pool, wc, workers, **kwargs):
        w = Worker(name=wc.name, path=wc.path, process=FakeWorkerProcess(name=wc.name))
        launched_order.append(wc.name)
        return w

    with patch("swarm.worker.manager.add_worker_live", side_effect=fake_add):
        resp = await client.post(
            "/api/workers/launch",
            json={"workers": ["gamma", "alpha", "beta"]},
            headers=_API_HEADERS,
        )
    assert resp.status == 201
    data = await resp.json()
    assert data["launched"] == ["gamma", "alpha", "beta"]


@pytest.mark.asyncio
async def test_workers_launch_empty(client, daemon):
    """When all workers are already running, return no_new_workers."""
    daemon.config.workers = [WorkerConfig("api", "/tmp/api")]
    resp = await client.post("/api/workers/launch", json={}, headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "no_new_workers"


@pytest.mark.asyncio
async def test_workers_spawn(client, daemon):
    new_worker = Worker(
        name="spawned", path="/tmp/spawned", process=FakeWorkerProcess(name="spawned")
    )
    with patch(
        "swarm.worker.manager.add_worker_live", new_callable=AsyncMock, return_value=new_worker
    ):
        resp = await client.post(
            "/api/workers/spawn",
            json={"name": "spawned", "path": "/tmp/spawned"},
            headers=_API_HEADERS,
        )
    assert resp.status == 201
    data = await resp.json()
    assert data["worker"] == "spawned"


@pytest.mark.asyncio
async def test_workers_spawn_invalid(client):
    resp = await client.post(
        "/api/workers/spawn", json={"name": "", "path": "/tmp/x"}, headers=_API_HEADERS
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_workers_continue_all(client, daemon):
    daemon.workers[0].state = WorkerState.RESTING
    resp = await client.post("/api/workers/continue-all", headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert data["count"] >= 1


@pytest.mark.asyncio
async def test_workers_send_all(client):
    resp = await client.post(
        "/api/workers/send-all",
        json={"message": "hello all"},
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["count"] == 2


@pytest.mark.asyncio
async def test_workers_send_all_empty_msg(client):
    resp = await client.post("/api/workers/send-all", json={"message": ""}, headers=_API_HEADERS)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_group_send(client, daemon):
    daemon.config.workers = [
        WorkerConfig("api", "/tmp/api"),
        WorkerConfig("web", "/tmp/web"),
    ]
    daemon.config.groups = [GroupConfig(name="backend", workers=["api"])]
    resp = await client.post(
        "/api/groups/backend/send",
        json={"message": "deploy"},
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["count"] == 1


@pytest.mark.asyncio
async def test_group_send_empty_msg(client, daemon):
    daemon.config.groups = [GroupConfig(name="backend", workers=["api"])]
    resp = await client.post(
        "/api/groups/backend/send",
        json={"message": ""},
        headers=_API_HEADERS,
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_worker_analyze(client, daemon, monkeypatch):
    # Ensure can_call returns True by setting _last_call far in the past
    daemon.queen._last_call = 0.0
    daemon.queen.cooldown = 0.0
    monkeypatch.setattr(
        daemon.queen, "analyze_worker", AsyncMock(return_value={"action": "continue"})
    )
    # Set the worker's process content directly instead of patching capture_pane
    worker = next(w for w in daemon.workers if w.name == "api")
    worker.process.set_content("output")
    resp = await client.post("/api/workers/api/analyze", headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert data["action"] == "continue"


@pytest.mark.asyncio
async def test_worker_analyze_bypasses_cooldown(client, daemon, monkeypatch):
    """User-initiated analyze calls bypass the Queen cooldown."""
    import time

    daemon.queen._last_call = time.time()
    daemon.queen.cooldown = 9999.0
    monkeypatch.setattr(
        daemon.queen, "analyze_worker", AsyncMock(return_value={"assessment": "ok"})
    )
    # Set the worker's process content directly instead of patching capture_pane
    worker = next(w for w in daemon.workers if w.name == "api")
    worker.process.set_content("output")
    resp = await client.post("/api/workers/api/analyze", headers=_API_HEADERS)
    assert resp.status == 200


# test_queen_coordinate* removed — /api/queen/coordinate endpoint and its
# full chain deleted in task #253 spec B. See
# docs/specs/headless-queen-architecture.md.


@pytest.mark.asyncio
async def test_queen_health_reports_offline_when_no_queen_worker(client):
    """/api/queen/health always returns a shape — offline when she isn't spawned."""
    resp = await client.get("/api/queen/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["state"] == "offline"
    assert data["pid_alive"] is False
    # Shape stability — the chat panel renders even when she isn't live.
    assert "context_fill_pct" in data
    assert "usage_5hr_pct" in data


# ---------------------------------------------------------------------------
# Queen chat API — thread CRUD + message posting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queen_threads_list_empty(client):
    resp = await client.get("/api/queen/threads")
    assert resp.status == 200
    data = await resp.json()
    assert data == {"threads": []}


@pytest.mark.asyncio
async def test_queen_threads_create_and_fetch(client):
    resp = await client.post(
        "/api/queen/threads",
        json={"title": "Why is hub stuck?", "body": "Been buzzing forever."},
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["thread"]["title"] == "Why is hub stuck?"
    assert data["message"]["role"] == "operator"
    assert data["message"]["content"] == "Been buzzing forever."
    thread_id = data["thread"]["id"]

    # Fetch it back with messages
    resp2 = await client.get(f"/api/queen/threads/{thread_id}")
    assert resp2.status == 200
    fetched = await resp2.json()
    assert fetched["thread"]["id"] == thread_id
    assert len(fetched["messages"]) == 1
    assert fetched["messages"][0]["content"] == "Been buzzing forever."


@pytest.mark.asyncio
async def test_queen_threads_create_validates_required(client):
    resp = await client.post(
        "/api/queen/threads",
        json={"title": "only-title"},
        headers=_API_HEADERS,
    )
    assert resp.status == 400
    data = await resp.json()
    assert "required" in data["error"]


@pytest.mark.asyncio
async def test_queen_threads_post_message_to_thread(client):
    resp = await client.post(
        "/api/queen/threads",
        json={"title": "t", "body": "first"},
        headers=_API_HEADERS,
    )
    thread_id = (await resp.json())["thread"]["id"]

    resp2 = await client.post(
        f"/api/queen/threads/{thread_id}/messages",
        json={"body": "follow-up"},
        headers=_API_HEADERS,
    )
    assert resp2.status == 200
    msg = (await resp2.json())["message"]
    assert msg["content"] == "follow-up"
    assert msg["role"] == "operator"

    # Thread should now have two messages
    resp3 = await client.get(f"/api/queen/threads/{thread_id}")
    assert len((await resp3.json())["messages"]) == 2


@pytest.mark.asyncio
async def test_queen_threads_post_to_missing_thread(client):
    resp = await client.post(
        "/api/queen/threads/does-not-exist/messages",
        json={"body": "x"},
        headers=_API_HEADERS,
    )
    assert resp.status == 404


@pytest.mark.asyncio
async def test_queen_threads_resolve(client):
    resp = await client.post(
        "/api/queen/threads",
        json={"title": "t", "body": "b"},
        headers=_API_HEADERS,
    )
    thread_id = (await resp.json())["thread"]["id"]

    resp2 = await client.post(
        f"/api/queen/threads/{thread_id}/resolve",
        json={"reason": "approved"},
        headers=_API_HEADERS,
    )
    assert resp2.status == 200

    # Posting to a resolved thread returns 409
    resp3 = await client.post(
        f"/api/queen/threads/{thread_id}/messages",
        json={"body": "late"},
        headers=_API_HEADERS,
    )
    assert resp3.status == 409


@pytest.mark.asyncio
async def test_queen_threads_resolve_missing(client):
    resp = await client.post(
        "/api/queen/threads/bogus/resolve",
        json={},
        headers=_API_HEADERS,
    )
    assert resp.status == 404


# ---------------------------------------------------------------------------
# Ask Queen re-route: operator threads forward to the interactive Queen PTY,
# and the response reports whether the Queen was live to receive it. When she
# is offline the message is still persisted (the operator just sees a "saved,
# she'll answer when back" notice instead of an endless spinner).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queen_thread_create_not_delivered_when_queen_offline(client):
    """No Queen worker in the list → queen_delivered False, message kept."""
    resp = await client.post(
        "/api/queen/threads",
        json={"title": "why rcg-networks?", "body": "explain the assignment", "kind": "operator"},
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["queen_delivered"] is False
    thread_id = data["thread"]["id"]
    # Message is still persisted even though the Queen wasn't reachable.
    fetched = await (await client.get(f"/api/queen/threads/{thread_id}")).json()
    assert len(fetched["messages"]) == 1
    assert fetched["messages"][0]["content"] == "explain the assignment"


@pytest.mark.asyncio
async def test_queen_thread_create_delivered_when_queen_alive(client, daemon):
    """A live Queen PTY in the worker list → forwarded, queen_delivered True."""
    daemon.workers.append(
        Worker(
            name="queen",
            path="/tmp/queen",
            kind=WORKER_KIND_QUEEN,
            process=FakeWorkerProcess(name="queen"),
        )
    )
    daemon.worker_svc.send_to_worker = AsyncMock()

    resp = await client.post(
        "/api/queen/threads",
        json={"title": "t", "body": "look at the board", "kind": "operator"},
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["queen_delivered"] is True
    daemon.worker_svc.send_to_worker.assert_awaited_once()
    assert daemon.worker_svc.send_to_worker.await_args.args[0] == "queen"


@pytest.mark.asyncio
async def test_queen_thread_post_message_forwards_to_live_queen(client, daemon):
    """Follow-up messages on an operator thread also forward to the Queen."""
    daemon.workers.append(
        Worker(
            name="queen",
            path="/tmp/queen",
            kind=WORKER_KIND_QUEEN,
            process=FakeWorkerProcess(name="queen"),
        )
    )
    daemon.worker_svc.send_to_worker = AsyncMock()

    created = await (
        await client.post(
            "/api/queen/threads",
            json={"title": "t", "body": "first", "kind": "operator"},
            headers=_API_HEADERS,
        )
    ).json()
    thread_id = created["thread"]["id"]

    resp = await client.post(
        f"/api/queen/threads/{thread_id}/messages",
        json={"body": "follow-up"},
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["queen_delivered"] is True
    # Both the create and the follow-up forwarded to the Queen PTY.
    assert daemon.worker_svc.send_to_worker.await_count == 2


@pytest.mark.asyncio
async def test_session_kill(client, daemon):
    resp = await client.post("/api/session/kill", headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "killed"


@pytest.mark.asyncio
async def test_workers_discover(client, daemon):
    resp = await client.post("/api/workers/discover", headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    # With pool=None, discover returns current workers
    assert len(data["workers"]) == 2


@pytest.mark.asyncio
async def test_drones_poll(client, daemon):
    daemon.pilot.poll_once = AsyncMock(return_value=True)
    resp = await client.post("/api/drones/poll", headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert data["had_action"] is True


@pytest.mark.asyncio
async def test_drones_poll_no_pilot(client, daemon):
    daemon.pilot = None
    resp = await client.post("/api/drones/poll", headers=_API_HEADERS)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_upload_standalone(client, daemon, tmp_path):
    """POST /api/uploads saves a file."""
    import aiohttp

    data = aiohttp.FormData()
    data.add_field("file", b"test content", filename="test.txt")
    resp = await client.post("/api/uploads", data=data, headers=_API_HEADERS)
    assert resp.status == 201
    body = await resp.json()
    assert "path" in body


# --- Proposals ---


@pytest.mark.asyncio
async def test_proposals_list(client, daemon):
    """GET /api/proposals returns pending proposals."""
    task = daemon.task_board.create(title="Fix bug")
    p = AssignmentProposal(worker_name="api", task_id=task.id, task_title=task.title)
    daemon.proposal_store.add(p)

    resp = await client.get("/api/proposals", headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert data["pending_count"] == 1
    assert len(data["proposals"]) == 1
    assert data["proposals"][0]["worker_name"] == "api"


@pytest.mark.asyncio
async def test_approve_proposal(client, daemon):
    """POST /api/proposals/{id}/approve assigns the task."""
    task = daemon.task_board.create(title="Fix bug")
    daemon.workers[0].state = WorkerState.RESTING
    p = AssignmentProposal(
        worker_name="api", task_id=task.id, task_title=task.title, message="Go fix it"
    )
    daemon.proposal_store.add(p)

    resp = await client.post(
        f"/api/proposals/{p.id}/approve",
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "approved"
    assert daemon.task_board.get(task.id).assigned_worker == "api"


@pytest.mark.asyncio
async def test_reject_proposal(client, daemon):
    """POST /api/proposals/{id}/reject rejects the proposal."""
    task = daemon.task_board.create(title="Fix bug")
    p = AssignmentProposal(worker_name="api", task_id=task.id, task_title=task.title)
    daemon.proposal_store.add(p)

    resp = await client.post(
        f"/api/proposals/{p.id}/reject",
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "rejected"
    assert len(daemon.proposal_store.pending) == 0


@pytest.mark.asyncio
async def test_reject_all_proposals(client, daemon):
    """POST /api/proposals/reject-all rejects all pending proposals."""
    t1 = daemon.task_board.create(title="Bug 1")
    t2 = daemon.task_board.create(title="Bug 2")
    daemon.proposal_store.add(
        AssignmentProposal(worker_name="api", task_id=t1.id, task_title=t1.title)
    )
    daemon.proposal_store.add(
        AssignmentProposal(worker_name="web", task_id=t2.id, task_title=t2.title)
    )

    resp = await client.post("/api/proposals/reject-all", headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert data["count"] == 2
    assert len(daemon.proposal_store.pending) == 0


@pytest.mark.asyncio
async def test_approve_proposal_not_found(client, daemon):
    resp = await client.post("/api/proposals/nonexistent/approve", headers=_API_HEADERS)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_reject_proposal_not_found(client, daemon):
    resp = await client.post("/api/proposals/nonexistent/reject", headers=_API_HEADERS)
    assert resp.status == 404


# --- Approval rules + min_confidence in config API ---


@pytest.mark.asyncio
async def test_update_config_approval_rules(config_client):
    resp = await config_client.put(
        "/api/config",
        json={
            "drones": {
                "approval_rules": [
                    {"pattern": "^Allow", "action": "approve"},
                    {"pattern": "delete|remove", "action": "escalate"},
                ]
            }
        },
        headers=_AUTH_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    rules = data["drones"]["approval_rules"]
    assert len(rules) == 2
    assert rules[0]["pattern"] == "^Allow"
    assert rules[1]["action"] == "escalate"


@pytest.mark.asyncio
async def test_update_config_approval_rules_invalid_regex(config_client):
    resp = await config_client.put(
        "/api/config",
        json={"drones": {"approval_rules": [{"pattern": "[bad", "action": "approve"}]}},
        headers=_AUTH_HEADERS,
    )
    assert resp.status == 400
    data = await resp.json()
    assert "invalid regex" in data["error"]


@pytest.mark.asyncio
async def test_update_config_approval_rules_invalid_action(config_client):
    resp = await config_client.put(
        "/api/config",
        json={"drones": {"approval_rules": [{"pattern": ".*", "action": "deny"}]}},
        headers=_AUTH_HEADERS,
    )
    assert resp.status == 400
    data = await resp.json()
    assert "action" in data["error"]


@pytest.mark.asyncio
async def test_update_config_min_confidence(config_client):
    resp = await config_client.put(
        "/api/config",
        json={"queen": {"min_confidence": 0.5}},
        headers=_AUTH_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["queen"]["min_confidence"] == 0.5


@pytest.mark.asyncio
async def test_update_config_min_confidence_invalid(config_client):
    resp = await config_client.put(
        "/api/config",
        json={"queen": {"min_confidence": 1.5}},
        headers=_AUTH_HEADERS,
    )
    assert resp.status == 400
    data = await resp.json()
    assert "min_confidence" in data["error"]


@pytest.mark.asyncio
async def test_update_config_workflows(config_client):
    from swarm.tasks.workflows import _DEFAULT_SKILL_COMMANDS, SKILL_COMMANDS

    try:
        resp = await config_client.put(
            "/api/config",
            json={"workflows": {"bug": "/my-fix", "chore": "/my-chore"}},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["workflows"]["bug"] == "/my-fix"
        assert data["workflows"]["chore"] == "/my-chore"
    finally:
        SKILL_COMMANDS.clear()
        SKILL_COMMANDS.update(_DEFAULT_SKILL_COMMANDS)


@pytest.mark.asyncio
async def test_update_config_workflows_invalid_type(config_client):
    resp = await config_client.put(
        "/api/config",
        json={"workflows": {"invalid_type": "/foo"}},
        headers=_AUTH_HEADERS,
    )
    assert resp.status == 400
    data = await resp.json()
    assert "invalid_type" in data["error"]


@pytest.mark.asyncio
async def test_server_stop(daemon):
    """POST /api/server/stop triggers the shutdown event."""
    app = create_app(daemon, enable_web=False)
    shutdown = asyncio.Event()
    app["shutdown_event"] = shutdown
    async with TestClient(TestServer(app)) as c:
        _inject_session_cookie(c)
        resp = await c.post("/api/server/stop", headers=_API_HEADERS)
        # Read status before the connection may drop
        assert resp.status == 200
    assert shutdown.is_set()


# -- WebSocket focus command --


@pytest.mark.asyncio
async def test_ws_focus_command(daemon):
    """WS focus command should call pilot.set_focused_workers()."""
    from swarm.server.api import _handle_ws_command

    daemon.pilot = MagicMock()
    daemon.pilot.enabled = True

    ws = AsyncMock()
    await _handle_ws_command(daemon, ws, {"command": "focus", "worker": "api"})
    daemon.pilot.set_focused_workers.assert_called_with({"api"})

    # Clear focus
    await _handle_ws_command(daemon, ws, {"command": "focus", "worker": ""})
    daemon.pilot.set_focused_workers.assert_called_with(set())


@pytest.mark.asyncio
async def test_ws_focus_command_no_pilot(daemon):
    """WS focus command should be a no-op when pilot is None."""
    from swarm.server.api import _handle_ws_command

    daemon.pilot = None
    ws = AsyncMock()
    # Should not raise
    await _handle_ws_command(daemon, ws, {"command": "focus", "worker": "api"})


# --- Invalid JSON handling ---


@pytest.mark.asyncio
async def test_invalid_json_returns_400(client):
    """POST endpoints should return 400 on malformed JSON, not 500."""
    endpoints = [
        "/api/workers/api/send",
        "/api/tasks",
        "/api/workers/send-all",
    ]
    for endpoint in endpoints:
        resp = await client.post(
            endpoint,
            data=b"not json{",
            headers={**_API_HEADERS, "Content-Type": "application/json"},
        )
        assert resp.status == 400, f"{endpoint} returned {resp.status}"
        data = await resp.json()
        assert "Invalid JSON" in data["error"], f"{endpoint}: {data}"


# --- WebSocket init test_mode ---


@pytest.mark.asyncio
async def test_ws_init_no_test_mode(daemon):
    """WS init message includes test_mode: false when _test_log is not set."""
    app = create_app(daemon, enable_web=False)
    async with TestClient(TestServer(app)) as c:
        _inject_session_cookie(c)
        ws = await c.ws_connect(f"/ws?token={_TEST_PASSWORD}")
        msg = await ws.receive_json()
        assert msg["type"] == "init"
        assert msg["test_mode"] is False
        assert msg["test_run_id"] is None
        await ws.close()


@pytest.mark.asyncio
async def test_ws_init_test_mode(daemon):
    """WS init message includes test_mode: true when _test_log is set."""
    daemon._test_log = MagicMock()
    daemon._test_log.run_id = "test-run-123"
    app = create_app(daemon, enable_web=False)
    async with TestClient(TestServer(app)) as c:
        _inject_session_cookie(c)
        ws = await c.ws_connect(f"/ws?token={_TEST_PASSWORD}")
        msg = await ws.receive_json()
        assert msg["type"] == "init"
        assert msg["test_mode"] is True
        assert msg["test_run_id"] == "test-run-123"
        await ws.close()


@pytest.mark.asyncio
async def test_ws_ip_counter_does_not_leak_across_connect_disconnect(daemon):
    """Regression: the per-IP WebSocket counter must return to its
    starting value after a normal connect → disconnect cycle.

    A user reported the dashboard could not reconnect after a day of
    reload cycles: the main WS was rejected with 429 while the
    terminal WS (different counter) still worked.  Root cause was a
    counter leak in handle_websocket — the increment happened before
    the outer try/finally, so any exception on ws.prepare() (hung
    client, cancelled handshake) permanently leaked the count.  After
    _MAX_WS_PER_IP leaks from the same IP, every subsequent attempt
    returned 429 "Too many WebSocket connections" until the daemon
    was restarted.

    This test opens + closes the main WS several times and asserts
    the counter ends at zero.  The ``_ws_ip_counts`` dict lives on
    ``app`` so we can inspect it directly.
    """
    app = create_app(daemon, enable_web=False)
    async with TestClient(TestServer(app)) as c:
        _inject_session_cookie(c)

        # Drive the full WS lifecycle three times.  Each iteration
        # increments the IP counter on open and should decrement on
        # close.  Before the fix, exceptions during ws.prepare() leaked
        # the counter; the explicit close+receive drain here proves
        # the happy path is also covered by the new outer try/finally.
        for _ in range(3):
            ws = await c.ws_connect(f"/ws?token={_TEST_PASSWORD}")
            msg = await ws.receive_json()
            assert msg["type"] == "init"
            await ws.close()

        # Give the server a beat to run the finally block on the last
        # disconnect (aiohttp cancels the handler coroutine on close).
        await asyncio.sleep(0.05)

        counts = app.get("_ws_ip_counts", {})
        # Every IP that touched the counter should be back at 0
        # (entries with value 0 are pruned by _ws_decrement).
        assert all(v == 0 for v in counts.values()), (
            f"IP counter leaked — expected all zeros, got {dict(counts)}"
        )


@pytest.mark.asyncio
async def test_usage_endpoint(client, daemon):
    """GET /api/usage returns per-worker, queen, and total usage."""
    from swarm.worker.worker import TokenUsage

    daemon.workers[0].usage = TokenUsage(input_tokens=1000, output_tokens=500, cost_usd=0.05)
    daemon.queen.usage = TokenUsage(input_tokens=2000, output_tokens=1000, cost_usd=0.10)

    resp = await client.get("/api/usage")
    assert resp.status == 200
    data = await resp.json()

    assert "workers" in data
    assert "queen" in data
    assert "total" in data

    assert data["workers"]["api"]["input_tokens"] == 1000
    assert data["workers"]["api"]["cost_usd"] == 0.05
    assert data["queen"]["input_tokens"] == 2000
    assert data["queen"]["cost_usd"] == 0.10
    assert data["total"]["input_tokens"] == 3000
    assert data["total"]["cost_usd"] == pytest.approx(0.15)


# --- get_client_ip ---


class TestGetClientIp:
    """Tests for get_client_ip proxy trust behaviour."""

    def _make_request(self, *, headers: dict[str, str] | None = None, remote: str = "9.9.9.9"):
        from unittest.mock import MagicMock

        req = MagicMock(spec=web.Request)
        req.headers = headers or {}
        req.remote = remote
        return req

    def test_no_trust_ignores_header(self, daemon):
        """When trust_proxy is False, X-Forwarded-For is ignored."""
        from swarm.server.api import get_client_ip

        daemon.config.trust_proxy = False
        req = self._make_request(headers={"X-Forwarded-For": "1.2.3.4, 5.6.7.8"})
        req.app = {"daemon": daemon}
        assert get_client_ip(req) == "9.9.9.9"

    def test_trust_takes_rightmost_minus_one(self, daemon):
        """When trust_proxy is True, returns the rightmost-minus-one IP."""
        from swarm.server.api import get_client_ip

        daemon.config.trust_proxy = True
        req = self._make_request(headers={"X-Forwarded-For": "1.2.3.4, 5.6.7.8, 10.0.0.1"})
        req.app = {"daemon": daemon}
        assert get_client_ip(req) == "5.6.7.8"

    def test_trust_single_ip(self, daemon):
        """When trust_proxy is True and only one IP, use that IP."""
        from swarm.server.api import get_client_ip

        daemon.config.trust_proxy = True
        req = self._make_request(headers={"X-Forwarded-For": "1.2.3.4"})
        req.app = {"daemon": daemon}
        assert get_client_ip(req) == "1.2.3.4"

    def test_no_header_fallback(self, daemon):
        """When trust_proxy is True but no header, falls back to request.remote."""
        from swarm.server.api import get_client_ip

        daemon.config.trust_proxy = True
        req = self._make_request()
        req.app = {"daemon": daemon}
        assert get_client_ip(req) == "9.9.9.9"


# --- is_same_origin ---


class TestIsSameOrigin:
    """Tests for is_same_origin CORS validation."""

    def _make_request(self, *, host: str = "localhost:9090", tunnel_url: str = ""):
        from unittest.mock import MagicMock

        from swarm.tunnel import TunnelManager

        req = MagicMock(spec=web.Request)
        req.host = host
        daemon = MagicMock()
        daemon.tunnel = MagicMock(spec=TunnelManager)
        daemon.tunnel.url = tunnel_url
        req.app = {"daemon": daemon}
        return req

    def test_no_origin_passes(self):
        """No origin header should be treated as same-origin."""
        from swarm.server.api import is_same_origin

        req = self._make_request()
        assert is_same_origin(req, "") is True

    def test_localhost_passes(self):
        """localhost origin should always pass."""
        from swarm.server.api import is_same_origin

        req = self._make_request()
        assert is_same_origin(req, "http://localhost:9090") is True

    def test_same_host_passes(self):
        """Origin matching request host should pass."""
        from swarm.server.api import is_same_origin

        req = self._make_request(host="myhost:9090")
        assert is_same_origin(req, "http://myhost:9090") is True

    def test_different_host_rejected(self):
        """Origin with a different host should be rejected."""
        from swarm.server.api import is_same_origin

        req = self._make_request(host="myhost:9090")
        assert is_same_origin(req, "http://evil.com") is False

    def test_tunnel_accepted(self):
        """Origin matching the tunnel URL should be accepted."""
        from swarm.server.api import is_same_origin

        req = self._make_request(tunnel_url="https://abc.trycloudflare.com")
        assert is_same_origin(req, "https://abc.trycloudflare.com") is True

    def test_origin_with_port(self):
        """Origin with explicit port should parse correctly."""
        from swarm.server.api import is_same_origin

        req = self._make_request(host="myhost:9090")
        assert is_same_origin(req, "http://myhost:3000") is True


# --- Rate limit with proxy ---


class TestRateLimitWithProxy:
    """Verify rate limiting uses request.remote when trust_proxy=False."""

    @pytest.mark.asyncio
    async def test_rate_limit_ignores_xff_when_no_trust(self, daemon):
        """Rate limiting should use request.remote, not XFF, when trust_proxy=False."""
        daemon.config.trust_proxy = False
        app = create_app(daemon, enable_web=False)
        async with TestClient(TestServer(app)) as client:
            _inject_session_cookie(client)
            # Send a request with a spoofed XFF header — it should be ignored
            headers = {**_API_HEADERS, "X-Forwarded-For": "spoofed.ip"}
            resp = await client.post("/api/tasks", json={"title": "Test"}, headers=headers)
            assert resp.status == 201
            # The rate limit bucket should be keyed by request.remote, not "spoofed.ip"
            rate_limits = app["rate_limits"]
            assert "spoofed.ip" not in rate_limits


# --- WebSocket auth rate limiting ---


class TestWsAuthLockout:
    """Verify per-IP lockout after repeated failed WS auth attempts."""

    def setup_method(self):
        from swarm.server.api import _ws_auth_failures

        _ws_auth_failures.clear()

    def teardown_method(self):
        from swarm.server.api import _ws_auth_failures

        _ws_auth_failures.clear()

    def test_ws_auth_lockout_after_failures(self):
        """IP is locked out after _WS_AUTH_MAX_FAILURES failures."""
        from swarm.server.api import (
            _WS_AUTH_MAX_FAILURES,
            _is_ws_auth_locked,
            record_ws_auth_failure,
        )

        ip = "10.0.0.1"
        for _ in range(_WS_AUTH_MAX_FAILURES):
            assert not _is_ws_auth_locked(ip)
            record_ws_auth_failure(ip)
        assert _is_ws_auth_locked(ip)

    def test_ws_auth_lockout_expires(self):
        """Lockout expires after _WS_AUTH_LOCKOUT_SECONDS."""
        import time as _time

        from swarm.server.api import (
            _WS_AUTH_LOCKOUT_SECONDS,
            _WS_AUTH_MAX_FAILURES,
            _is_ws_auth_locked,
            _ws_auth_failures,
        )

        ip = "10.0.0.2"
        # Record failures in the past (beyond lockout window)
        old = _time.time() - _WS_AUTH_LOCKOUT_SECONDS - 1
        _ws_auth_failures[ip] = [old] * _WS_AUTH_MAX_FAILURES
        # Should NOT be locked — all entries are expired
        assert not _is_ws_auth_locked(ip)

    def test_ws_auth_lockout_per_ip(self):
        """Failures from one IP don't affect another."""
        from swarm.server.api import (
            _WS_AUTH_MAX_FAILURES,
            _is_ws_auth_locked,
            record_ws_auth_failure,
        )

        bad_ip = "10.0.0.3"
        good_ip = "10.0.0.4"
        for _ in range(_WS_AUTH_MAX_FAILURES):
            record_ws_auth_failure(bad_ip)
        assert _is_ws_auth_locked(bad_ip)
        assert not _is_ws_auth_locked(good_ip)

    @pytest.mark.asyncio
    async def test_ws_auth_timeout_does_not_count_toward_lockout(self, daemon, monkeypatch) -> None:
        """Regression: an auth timeout / protocol error should NOT count
        toward the per-IP lockout window.

        Reported: through a Cloudflare tunnel, the dashboard's main /ws
        connection kept failing handshake while /ws/terminal worked.
        Root cause: ws_authenticate's 5-second receive timeout fires
        when the auth message gets lost or delayed in transit.  Each
        timeout was being counted as an "auth failure" — 5 of those
        within 5 minutes locked the IP out of /ws entirely (terminal
        was unaffected because it skips the lockout check).

        The fix: only count an *actual wrong token* toward the
        lockout.  Protocol-level failures (timeout, malformed message,
        missing message) are silent — the auto-reconnect loop will
        retry naturally and the operator isn't penalized for transient
        transport blips.
        """
        import json
        from unittest.mock import AsyncMock, MagicMock

        from aiohttp import web

        from swarm.server.api import (
            _is_ws_auth_locked,
            ws_authenticate,
        )
        from swarm.server.routes.websocket import _ws_auth_failures

        ip = "10.0.0.99"
        _ws_auth_failures.clear()
        monkeypatch.setattr(
            "swarm.server.routes.websocket.get_client_ip",
            lambda _req: ip,
            raising=False,
        )
        monkeypatch.setattr("swarm.server.api.get_client_ip", lambda _req: ip, raising=False)

        request = MagicMock()
        request.query = {}

        # 6 timeouts — pre-fix every one would have called
        # record_ws_auth_failure and locked the IP at attempt #5.
        for _ in range(6):
            ws = MagicMock(spec=web.WebSocketResponse)
            ws.receive = AsyncMock(side_effect=TimeoutError())
            ws.close = AsyncMock()
            result = await ws_authenticate(ws, request, "secret")
            assert result is False  # auth failed, but transport-level

        assert not _is_ws_auth_locked(ip), (
            "Auth timeouts must not contribute to per-IP lockout — "
            "only actual wrong-token failures should.  Pre-fix the "
            "outer caller blindly recorded every False as a failure, "
            "which locked operators out of /ws after 5 transient "
            "tunnel hiccups while /ws/terminal kept working."
        )

        # Now confirm a real wrong-token DOES record toward the lockout.
        for _ in range(5):
            ws_bad = MagicMock(spec=web.WebSocketResponse)
            ws_bad.receive = AsyncMock(
                return_value=MagicMock(
                    type=web.WSMsgType.TEXT,
                    data=json.dumps({"type": "auth", "token": "wrong"}),
                )
            )
            ws_bad.close = AsyncMock()
            result = await ws_authenticate(ws_bad, request, "secret")
            assert result is False

        assert _is_ws_auth_locked(ip), (
            "5 wrong-token failures should still trigger the lockout — "
            "the fix only loosens the trigger for protocol errors."
        )


# --- Health Check (/health) ---


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_basic(self, client):
        """No auth → basic fields only, no workers/queen."""
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert "uptime" in data
        assert "version" in data
        # Should NOT include detailed fields
        assert "workers" not in data
        assert "queen" not in data
        assert "drones" not in data

    @pytest.mark.asyncio
    async def test_health_check_detailed_with_auth(self, client):
        """Valid Bearer token → all detailed fields present."""
        resp = await client.get(
            "/health",
            headers={"Authorization": f"Bearer {_TEST_PASSWORD}"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert "uptime" in data
        assert "version" in data
        # Detailed fields
        assert isinstance(data["workers"], list)
        assert len(data["workers"]) == 2  # api + web from fixture
        assert data["workers"][0]["name"] == "api"
        assert "state" in data["workers"][0]
        assert "duration" in data["workers"][0]
        assert isinstance(data["queen"], dict)
        assert isinstance(data["drones"], dict)
        assert "enabled" in data["drones"]
        assert isinstance(data["pilot"], dict)
        assert "build_sha" in data

    @pytest.mark.asyncio
    async def test_health_check_wrong_token(self, client):
        """Bad token → basic response only (no 401 — probes must succeed)."""
        resp = await client.get(
            "/health",
            headers={"Authorization": "Bearer wrong-password"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert "workers" not in data
        assert "queen" not in data


# --- Dry-Run Approval Rules ---


class TestDryRunRules:
    @pytest.mark.asyncio
    async def test_dry_run_safe_builtin(self, client):
        """Read() is auto-approved as safe builtin."""
        resp = await client.post(
            "/api/config/approval-rules/dry-run",
            json={"content": "Read(src/main.py)", "rules": []},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        result = data["results"][0]
        assert result["decision"] == "approve"
        assert result["source"] == "safe_builtin"

    @pytest.mark.asyncio
    async def test_dry_run_always_escalate(self, client):
        """DROP TABLE triggers the always-escalate safety net."""
        resp = await client.post(
            "/api/config/approval-rules/dry-run",
            json={"content": "DROP TABLE users", "rules": []},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        result = data["results"][0]
        assert result["decision"] == "escalate"
        assert result["source"] == "always_escalate"

    @pytest.mark.asyncio
    async def test_dry_run_user_rule_approve(self, client):
        """Custom approve rule matches."""
        resp = await client.post(
            "/api/config/approval-rules/dry-run",
            json={
                "content": "npm install express",
                "rules": [{"pattern": "npm install", "action": "approve"}],
            },
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        result = data["results"][0]
        assert result["decision"] == "approve"
        assert result["source"] == "rule"
        assert result["rule_index"] == 0
        assert result["rule_pattern"] == "npm install"

    @pytest.mark.asyncio
    async def test_dry_run_user_rule_escalate(self, client):
        """Custom escalate rule matches."""
        resp = await client.post(
            "/api/config/approval-rules/dry-run",
            json={
                "content": "deploy to staging",
                "rules": [{"pattern": "deploy", "action": "escalate"}],
            },
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        result = data["results"][0]
        assert result["decision"] == "escalate"
        assert result["source"] == "rule"
        assert result["rule_index"] == 0

    @pytest.mark.asyncio
    async def test_dry_run_no_match_default_escalate(self, client):
        """No rule matches → default escalate."""
        resp = await client.post(
            "/api/config/approval-rules/dry-run",
            json={
                "content": "some unknown operation",
                "rules": [{"pattern": "npm", "action": "approve"}],
            },
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        result = data["results"][0]
        assert result["matched"] is False
        assert result["decision"] == "escalate"
        assert result["source"] == "default_escalate"

    @pytest.mark.asyncio
    async def test_dry_run_uses_config_rules_when_omitted(self, client, daemon):
        """Falls back to daemon config rules when 'rules' key is omitted."""
        from swarm.config import DroneApprovalRule

        daemon.config.drones.approval_rules = [
            DroneApprovalRule(pattern="pytest", action="approve")
        ]
        resp = await client.post(
            "/api/config/approval-rules/dry-run",
            json={"content": "pytest tests/"},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        result = data["results"][0]
        assert result["decision"] == "approve"
        assert result["source"] == "rule"

    @pytest.mark.asyncio
    async def test_dry_run_invalid_regex(self, client):
        """Invalid regex returns 400."""
        resp = await client.post(
            "/api/config/approval-rules/dry-run",
            json={
                "content": "test",
                "rules": [{"pattern": "[invalid", "action": "approve"}],
            },
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 400
        data = await resp.json()
        assert "invalid regex" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_dry_run_empty_content(self, client):
        """Empty content returns 400."""
        resp = await client.post(
            "/api/config/approval-rules/dry-run",
            json={"content": "", "rules": []},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_dry_run_requires_auth(self, daemon):
        """No Bearer token and no session cookie → 401 (config auth middleware)."""
        app = create_app(daemon, enable_web=False)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/api/config/approval-rules/dry-run",
                json={"content": "test", "rules": []},
                headers=_API_HEADERS,
            )
            assert resp.status == 401


# --- Rule Analytics ---


class TestRuleAnalytics:
    @pytest.fixture
    def daemon_with_store(self, daemon, tmp_path):
        """Attach a SQLite-backed DroneLog to the daemon."""
        daemon.drone_log = DroneLog(db_path=tmp_path / "log.db")
        return daemon

    @pytest.fixture
    async def store_client(self, daemon_with_store):
        app = create_app(daemon_with_store, enable_web=False)
        async with TestClient(TestServer(app)) as c:
            _inject_session_cookie(c)
            yield c

    @pytest.mark.asyncio
    async def test_analytics_empty(self, store_client):
        resp = await store_client.get("/api/drones/rules/analytics", headers=_API_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert data["analytics"] == []
        assert isinstance(data["config_rules"], list)

    @pytest.mark.asyncio
    async def test_analytics_no_store(self, client):
        """Without SQLite store, returns empty analytics."""
        resp = await client.get("/api/drones/rules/analytics", headers=_API_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert data["analytics"] == []

    @pytest.mark.asyncio
    async def test_analytics_with_data(self, daemon_with_store, store_client):
        from swarm.drones.log import DroneAction

        daemon_with_store.drone_log.add(
            DroneAction.CONTINUED,
            "api",
            "safe operation",
            metadata={"rule_pattern": r"\bBash\b", "source": "rule"},
        )
        resp = await store_client.get("/api/drones/rules/analytics", headers=_API_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert len(data["analytics"]) == 1
        assert data["analytics"][0]["total_fires"] == 1

    @pytest.mark.asyncio
    async def test_analytics_days_filter(self, store_client):
        resp = await store_client.get("/api/drones/rules/analytics?days=1", headers=_API_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data["analytics"], list)

    @pytest.mark.asyncio
    async def test_analytics_includes_config_rules(self, daemon_with_store, store_client):
        from swarm.config import DroneApprovalRule

        daemon_with_store.config.drones.approval_rules = [
            DroneApprovalRule(pattern=r"\bBash\b", action="approve"),
        ]
        resp = await store_client.get("/api/drones/rules/analytics", headers=_API_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert len(data["config_rules"]) == 1
        assert data["config_rules"][0]["pattern"] == r"\bBash\b"


# --- Approval Rate ---


class TestApprovalRateRoute:
    @pytest.mark.asyncio
    async def test_empty_log(self, client):
        resp = await client.get("/api/drones/approval-rate", headers=_API_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert data["approvals"] == 0
        assert data["escalations"] == 0
        assert data["rate"] is None
        assert data["window_hours"] == 24.0

    @pytest.mark.asyncio
    async def test_populated(self, client, daemon):
        from swarm.drones.log import DroneAction

        for _ in range(3):
            daemon.drone_log.add(DroneAction.CONTINUED, "api")
        daemon.drone_log.add(DroneAction.ESCALATED, "api")

        resp = await client.get("/api/drones/approval-rate", headers=_API_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert data["approvals"] == 3
        assert data["escalations"] == 1
        assert data["rate"] == 0.75

    @pytest.mark.asyncio
    async def test_custom_hours(self, client):
        resp = await client.get("/api/drones/approval-rate?hours=168", headers=_API_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert data["window_hours"] == 168.0

    @pytest.mark.asyncio
    async def test_invalid_hours(self, client):
        resp = await client.get("/api/drones/approval-rate?hours=abc", headers=_API_HEADERS)
        assert resp.status == 400

        resp = await client.get("/api/drones/approval-rate?hours=0", headers=_API_HEADERS)
        assert resp.status == 400


# --- Rule Suggest ---


class TestRuleSuggest:
    @pytest.mark.asyncio
    async def test_suggest_basic(self, client):
        resp = await client.post(
            "/api/drones/rules/suggest",
            json={"details": ["Bash: npm install express"]},
            headers=_API_HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        suggestion = data["suggestion"]
        assert suggestion["pattern"]
        assert suggestion["action"] == "approve"
        assert suggestion["confidence"] > 0

    @pytest.mark.asyncio
    async def test_suggest_escalate_action(self, client):
        resp = await client.post(
            "/api/drones/rules/suggest",
            json={"details": ["Bash: pytest tests/"], "action": "escalate"},
            headers=_API_HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["suggestion"]["action"] == "escalate"

    @pytest.mark.asyncio
    async def test_suggest_missing_details(self, client):
        resp = await client.post(
            "/api/drones/rules/suggest",
            json={"action": "approve"},
            headers=_API_HEADERS,
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_suggest_invalid_action(self, client):
        resp = await client.post(
            "/api/drones/rules/suggest",
            json={"details": ["Bash: test"], "action": "invalid"},
            headers=_API_HEADERS,
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_suggest_non_string_details(self, client):
        resp = await client.post(
            "/api/drones/rules/suggest",
            json={"details": [123, 456]},
            headers=_API_HEADERS,
        )
        assert resp.status == 400


# --- Add Approval Rule ---


class TestAddApprovalRule:
    @pytest.mark.asyncio
    async def test_add_rule_basic(self, client, daemon):
        resp = await client.post(
            "/api/config/approval-rules",
            json={"pattern": r"\bBash\b.*\bnpm\b", "action": "approve"},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert any(r["pattern"] == r"\bBash\b.*\bnpm\b" for r in data["rules"])

    @pytest.mark.asyncio
    async def test_add_rule_invalid_regex(self, client):
        resp = await client.post(
            "/api/config/approval-rules",
            json={"pattern": "[invalid", "action": "approve"},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 400
        data = await resp.json()
        assert "regex" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_add_rule_returns_apply_result(self, client):
        """Phase 8 of #328: approval-rules endpoint surfaces consumed
        / unknown body keys for parity with the dataclass-shaped save
        endpoints.  Operator typo-ing a body key (e.g. ``regex``
        instead of ``pattern``) shouldn't crash, but the dashboard
        should warn that the typo'd field was ignored.
        """
        resp = await client.post(
            "/api/config/approval-rules",
            json={
                "pattern": "Bash.*",
                "action": "approve",
                "phantom_field": "ignored",
            },
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        assert "_apply_result" in data
        ar = data["_apply_result"]
        assert "pattern" in ar["consumed"]
        assert "action" in ar["consumed"]
        assert "phantom_field" in ar["unknown"]


@pytest.mark.asyncio
async def test_add_worker_to_group_returns_apply_result(config_client, tmp_path):
    """Phase 8 of #328: POST /api/config/workers/{name}/add-to-group
    surfaces consumed / unknown body keys.  Body has only
    ``{group, create}``; anything else should land in ``unknown``."""
    worker_dir = tmp_path / "atg-worker"
    worker_dir.mkdir()
    with patch("swarm.worker.manager.add_worker_live", new_callable=AsyncMock) as mock_add:
        mock_add.return_value = Worker(
            name="atg", path=str(worker_dir), process=FakeWorkerProcess(name="atg")
        )
        await config_client.post(
            "/api/config/workers",
            json={"name": "atg", "path": str(worker_dir)},
            headers=_AUTH_HEADERS,
        )

    resp = await config_client.post(
        "/api/config/workers/atg/add-to-group",
        json={"group": "atg-grp", "create": True, "weird_field": 1},
        headers=_AUTH_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert "_apply_result" in data
    ar = data["_apply_result"]
    assert "group" in ar["consumed"]
    assert "create" in ar["consumed"]
    assert "weird_field" in ar["unknown"]


@pytest.mark.asyncio
async def test_save_worker_to_config_returns_apply_result(config_client, daemon, tmp_path):
    """Phase 8 of #328: POST /api/config/workers/{name}/save returns
    a structured ApplyResult.  This endpoint takes no body fields —
    it extracts data from the running worker — so consumed/unknown
    are both empty.  But the field exists for client-side parity:
    the dashboard's ``_toastApplyResult`` helper looks for
    ``_apply_result`` on every response and silently no-ops if both
    lists are empty.
    """
    # Inject a running-but-not-in-config worker directly into the
    # daemon's workers list — same shape ``handle_save_worker_to_config``
    # reads via ``d.get_worker(name)``.
    worker_dir = tmp_path / "running-only"
    worker_dir.mkdir()
    process = FakeWorkerProcess(name="running")
    worker = Worker(name="running", path=str(worker_dir), process=process)
    daemon.workers.append(worker)
    try:
        # Make sure it's not in config (the endpoint guards against duplicates).
        daemon.config.workers = [w for w in daemon.config.workers if w.name != "running"]

        resp = await config_client.post(
            "/api/config/workers/running/save",
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 201
        data = await resp.json()
        assert "_apply_result" in data
        ar = data["_apply_result"]
        assert ar["consumed"] == []
        assert ar["unknown"] == []
    finally:
        daemon.workers = [w for w in daemon.workers if w.name != "running"]

    @pytest.mark.asyncio
    async def test_add_rule_invalid_action(self, client):
        resp = await client.post(
            "/api/config/approval-rules",
            json={"pattern": r"\btest\b", "action": "destroy"},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_add_rule_with_position(self, client, daemon):
        from swarm.config import DroneApprovalRule

        daemon.config.drones.approval_rules = [
            DroneApprovalRule(pattern="first", action="approve"),
            DroneApprovalRule(pattern="last", action="approve"),
        ]
        resp = await client.post(
            "/api/config/approval-rules",
            json={"pattern": "middle", "action": "escalate", "position": 1},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["rules"][1]["pattern"] == "middle"

    @pytest.mark.asyncio
    async def test_add_rule_requires_auth(self, daemon):
        """No Bearer token and no session cookie → 401."""
        app = create_app(daemon, enable_web=False)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/api/config/approval-rules",
                json={"pattern": r"\btest\b", "action": "approve"},
                headers=_API_HEADERS,
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_add_rule_empty_pattern(self, client):
        resp = await client.post(
            "/api/config/approval-rules",
            json={"pattern": "", "action": "approve"},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 400


# ---------------------------------------------------------------------------
# Readiness probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readiness_probe(client):
    resp = await client.get("/ready")
    assert resp.status == 200
    data = await resp.json()
    assert data["ready"] is True
    assert "checks" in data


@pytest.mark.asyncio
async def test_readiness_probe_no_auth_required(daemon):
    """The /ready endpoint should work without session cookie or Bearer token."""
    app = create_app(daemon, enable_web=False)
    async with TestClient(TestServer(app)) as c:
        resp = await c.get("/ready")
        assert resp.status == 200
        data = await resp.json()
        assert data["ready"] is True


# ---------------------------------------------------------------------------
# Approve-all proposals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_all_proposals_empty(client):
    resp = await client.post("/api/proposals/approve-all", headers=_API_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "approved_all"
    assert data["count"] == 0


# ---------------------------------------------------------------------------
# Task pagination, filtering, sorting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tasks_pagination(client):
    for i in range(5):
        await client.post("/api/tasks", json={"title": f"task-{i}"}, headers=_API_HEADERS)
    resp = await client.get("/api/tasks?limit=2&offset=0")
    data = await resp.json()
    assert len(data["tasks"]) == 2
    assert data["total"] == 5
    assert data["limit"] == 2
    assert data["offset"] == 0


@pytest.mark.asyncio
async def test_tasks_filter_status(client):
    await client.post("/api/tasks", json={"title": "unassigned-task"}, headers=_API_HEADERS)
    resp = await client.get("/api/tasks?status=unassigned")
    data = await resp.json()
    assert data["total"] >= 1
    assert all(t["status"] == "unassigned" for t in data["tasks"])


@pytest.mark.asyncio
async def test_tasks_search(client):
    await client.post("/api/tasks", json={"title": "Fix the login bug"}, headers=_API_HEADERS)
    await client.post("/api/tasks", json={"title": "Add dark mode"}, headers=_API_HEADERS)
    resp = await client.get("/api/tasks?search=login")
    data = await resp.json()
    assert data["total"] == 1
    assert data["tasks"][0]["title"] == "Fix the login bug"


@pytest.mark.asyncio
async def test_tasks_default_returns_all(client):
    """No query params returns all tasks with pagination metadata."""
    resp = await client.get("/api/tasks")
    data = await resp.json()
    assert "total" in data
    assert "limit" in data
    assert "offset" in data
    assert "summary" in data


@pytest.mark.asyncio
async def test_decisions_pagination(client):
    resp = await client.get("/api/decisions?limit=10&offset=0")
    data = await resp.json()
    assert "decisions" in data
    assert "total" in data
    assert data["limit"] == 10
    assert data["offset"] == 0


# ---------------------------------------------------------------------------
# Bulk task operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_complete(client):
    ids = []
    for i in range(3):
        resp = await client.post("/api/tasks", json={"title": f"t-{i}"}, headers=_API_HEADERS)
        data = await resp.json()
        ids.append(data["id"])
        await client.post(
            f"/api/tasks/{data['id']}/assign",
            json={"worker": "api"},
            headers=_API_HEADERS,
        )
    resp = await client.post(
        "/api/tasks/bulk",
        json={"action": "complete", "task_ids": ids},
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["succeeded"] == 3
    assert data["failed"] == 0


@pytest.mark.asyncio
async def test_bulk_partial_failure(client):
    resp = await client.post("/api/tasks", json={"title": "real"}, headers=_API_HEADERS)
    real_id = (await resp.json())["id"]
    resp = await client.post(
        "/api/tasks/bulk",
        json={"action": "complete", "task_ids": [real_id, "nonexistent"]},
        headers=_API_HEADERS,
    )
    data = await resp.json()
    assert data["succeeded"] + data["failed"] == 2


@pytest.mark.asyncio
async def test_bulk_invalid_action(client):
    resp = await client.post(
        "/api/tasks/bulk",
        json={"action": "destroy", "task_ids": []},
        headers=_API_HEADERS,
    )
    assert resp.status == 400


# ---------------------------------------------------------------------------
# Request ID tracing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_id_echoed(client):
    resp = await client.get("/api/health", headers={"X-Request-ID": "test-123"})
    assert resp.headers.get("X-Request-ID") == "test-123"


@pytest.mark.asyncio
async def test_request_id_generated(client):
    resp = await client.get("/api/health")
    rid = resp.headers.get("X-Request-ID")
    assert rid is not None
    assert len(rid) == 12


# ---------------------------------------------------------------------------
# Worker memory / identity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_memory_get(client):
    resp = await client.get("/api/workers/api/memory")
    assert resp.status == 200
    data = await resp.json()
    assert "memory" in data
    assert data["worker"] == "api"


@pytest.mark.asyncio
async def test_worker_memory_nonexistent_returns_empty(client):
    resp = await client.get("/api/workers/nonexistent/memory")
    assert resp.status == 200
    data = await resp.json()
    assert data["memory"] == ""


@pytest.mark.asyncio
async def test_worker_identity_no_file(client):
    """Identity returns 404 when worker has no identity file."""
    resp = await client.get("/api/workers/api/identity")
    assert resp.status in (200, 404)  # depends on whether /tmp/api has identity


# ---------------------------------------------------------------------------
# Notification config validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_notification_validation(client):
    """Config with invalid event types should still save (validation is advisory)."""
    resp = await client.put(
        "/api/config",
        json={"notifications": {"desktop_events": ["worker_stung", "invalid_event"]}},
        headers=_AUTH_HEADERS,
    )
    # Config save should succeed (validation warnings don't block save)
    assert resp.status == 200


# ---------------------------------------------------------------------------
# Pipeline schedule and lifecycle
# ---------------------------------------------------------------------------


def test_pipeline_schedule_in_engine():
    """Pipeline steps preserve schedule field through create/serialize."""
    from swarm.pipelines.engine import PipelineEngine
    from swarm.pipelines.models import PipelineStep
    from swarm.pipelines.store import PipelineStore

    store = PipelineStore(path=Path(tempfile.mktemp(suffix=".json")))
    engine = PipelineEngine(store=store)
    p = engine.create(
        "Scheduled",
        steps=[PipelineStep(id="s1", name="Step 1", schedule="09:00")],
    )
    assert p.steps[0].schedule == "09:00"
    d = p.to_dict()
    assert d["steps"][0]["schedule"] == "09:00"


@pytest.mark.asyncio
async def test_bulk_remove(client):
    ids = []
    for i in range(2):
        resp = await client.post(
            "/api/tasks", json={"title": f"removable-{i}"}, headers=_API_HEADERS
        )
        ids.append((await resp.json())["id"])
    resp = await client.post(
        "/api/tasks/bulk",
        json={"action": "remove", "task_ids": ids},
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["succeeded"] == 2


@pytest.mark.asyncio
async def test_bulk_reopen(client):
    resp = await client.post("/api/tasks", json={"title": "bulk-reopen"}, headers=_API_HEADERS)
    tid = (await resp.json())["id"]
    await client.post(f"/api/tasks/{tid}/assign", json={"worker": "api"}, headers=_API_HEADERS)
    await client.post(
        f"/api/tasks/{tid}/complete", json={"resolution": "done"}, headers=_API_HEADERS
    )
    resp = await client.post(
        "/api/tasks/bulk",
        json={"action": "reopen", "task_ids": [tid]},
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["succeeded"] == 1


@pytest.mark.asyncio
async def test_force_complete_endpoint_closes_blocked(client, daemon):
    """#609: POST /api/tasks/{id}/force-complete closes a wedged BLOCKED task
    that the normal /complete endpoint refuses."""
    from swarm.tasks.task import TaskStatus

    t = daemon.task_board.create("wedged")
    daemon.task_board.assign(t.id, "alice")
    daemon.task_board.activate(t.id)
    daemon.task_board.block_for_operator(t.id, "operator hold")
    assert t.status == TaskStatus.BLOCKED

    resp = await client.post(
        f"/api/tasks/{t.id}/force-complete",
        json={"resolution": "done via endpoint"},
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["forced"] is True
    assert daemon.task_board.get(t.id).status == TaskStatus.DONE


@pytest.mark.asyncio
async def test_analytics_summary_endpoint(client, daemon):
    """GET /api/analytics/summary aggregates board throughput."""
    t = daemon.task_board.create("shipped thing")
    daemon.task_board.assign(t.id, "alice")
    daemon.task_board.activate(t.id)
    daemon.task_board.complete(t.id, resolution="done")

    resp = await client.get("/api/analytics/summary?days=7")
    assert resp.status == 200
    data = await resp.json()
    assert data["window_days"] == 7
    assert data["completed"] == 1
    assert data["workers"][0]["worker"] == "alice"
    assert "backlog" in data


@pytest.mark.asyncio
async def test_analytics_summary_clamps_bad_days(client):
    """Non-numeric / out-of-range days fall back safely."""
    resp = await client.get("/api/analytics/summary?days=banana")
    assert resp.status == 200
    assert (await resp.json())["window_days"] == 7
    resp = await client.get("/api/analytics/summary?days=99999")
    assert resp.status == 200
    assert (await resp.json())["window_days"] == 365


@pytest.mark.asyncio
async def test_queen_learnings_list_and_delete(client, daemon):
    """GET /api/queen/learnings lists; DELETE removes by id."""
    learning = daemon.queen_chat.add_learning(context="ctx", correction="fix", applied_to="hub")

    resp = await client.get("/api/queen/learnings?applied_to=hub")
    assert resp.status == 200
    data = await resp.json()
    assert [item["id"] for item in data["learnings"]] == [learning.id]

    resp = await client.delete(f"/api/queen/learnings/{learning.id}", headers=_API_HEADERS)
    assert resp.status == 200
    assert (await resp.json())["deleted"] == learning.id

    resp = await client.delete(f"/api/queen/learnings/{learning.id}", headers=_API_HEADERS)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_messages_delete_endpoint(client, daemon, tmp_path):
    """POST /api/messages/delete removes specific messages."""
    from swarm.messages.store import MessageStore

    daemon.message_store = MessageStore(db_path=tmp_path / "msgs.db")
    msg_id = daemon.message_store.send("alice", "bob", "finding", "obsolete")

    resp = await client.post("/api/messages/delete", json={"ids": [msg_id]}, headers=_API_HEADERS)
    assert resp.status == 200
    assert (await resp.json())["deleted"] == 1
    assert daemon.message_store.get_recent() == []

    resp = await client.post("/api/messages/delete", json={"ids": []}, headers=_API_HEADERS)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_queen_threads_list_includes_message_count(client, daemon):
    t = daemon.queen_chat.create_thread(title="counted", kind="operator")
    daemon.queen_chat.add_message(t.id, role="operator", content="one")
    daemon.queen_chat.add_message(t.id, role="queen", content="two")
    resp = await client.get("/api/queen/threads")
    assert resp.status == 200
    threads = (await resp.json())["threads"]
    row = next(r for r in threads if r["id"] == t.id)
    assert row["message_count"] == 2


@pytest.mark.asyncio
async def test_queen_threads_search_param(client, daemon):
    a = daemon.queen_chat.create_thread(title="auth deploy", kind="operator")
    daemon.queen_chat.add_message(a.id, role="operator", content="x")
    b = daemon.queen_chat.create_thread(title="unrelated", kind="operator")
    daemon.queen_chat.add_message(b.id, role="operator", content="redis migration notes")

    resp = await client.get("/api/queen/threads?q=auth")
    titles = [r["title"] for r in (await resp.json())["threads"]]
    assert titles == ["auth deploy"]

    # body match
    resp2 = await client.get("/api/queen/threads?q=redis")
    titles2 = [r["title"] for r in (await resp2.json())["threads"]]
    assert titles2 == ["unrelated"]


@pytest.mark.asyncio
async def test_queen_threads_offset_paginates(client, daemon):
    for i in range(4):
        daemon.queen_chat.create_thread(title=f"p{i}", kind="operator")
    r1 = await client.get("/api/queen/threads?limit=2&offset=0")
    r2 = await client.get("/api/queen/threads?limit=2&offset=2")
    ids1 = {t["id"] for t in (await r1.json())["threads"]}
    ids2 = {t["id"] for t in (await r2.json())["threads"]}
    assert len(ids1) == 2 and len(ids2) == 2
    assert ids1.isdisjoint(ids2)


@pytest.mark.asyncio
async def test_purge_queen_threads_uses_configured_window(daemon):
    """_purge_queen_threads passes the configured retention window and skips on 0."""
    daemon.config.queen.queen_thread_retention_days = 45
    daemon.queen_chat = MagicMock()
    daemon.queen_chat.purge_old.return_value = 3
    assert daemon._purge_queen_threads() == 3
    daemon.queen_chat.purge_old.assert_called_once_with(retention_days=45)

    # 0 = keep forever → no purge call
    daemon.queen_chat.purge_old.reset_mock()
    daemon.config.queen.queen_thread_retention_days = 0
    assert daemon._purge_queen_threads() == 0
    daemon.queen_chat.purge_old.assert_not_called()


@pytest.mark.asyncio
async def test_queen_thread_reopen_and_reply(client, daemon):
    # Create + resolve a thread
    t = daemon.queen_chat.create_thread(title="revisit", kind="operator")
    daemon.queen_chat.add_message(t.id, role="operator", content="original")
    daemon.queen_chat.resolve_thread(t.id, resolved_by="operator", reason="done")
    assert daemon.queen_chat.get_thread(t.id).status == "resolved"

    resp = await client.post(
        f"/api/queen/threads/{t.id}/reopen",
        json={"body": "actually, one more thing"},
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["thread"]["status"] == "active"
    assert data["message"]["content"] == "actually, one more thing"
    # The transcript now has both messages
    msgs = daemon.queen_chat.list_messages(t.id)
    assert [m.content for m in msgs] == ["original", "actually, one more thing"]


@pytest.mark.asyncio
async def test_queen_thread_reopen_requires_body(client, daemon):
    t = daemon.queen_chat.create_thread(title="x", kind="operator")
    daemon.queen_chat.resolve_thread(t.id, resolved_by="operator")
    resp = await client.post(f"/api/queen/threads/{t.id}/reopen", json={}, headers=_API_HEADERS)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_queen_thread_reopen_missing_404(client):
    resp = await client.post(
        "/api/queen/threads/bogus/reopen", json={"body": "hi"}, headers=_API_HEADERS
    )
    assert resp.status == 404
