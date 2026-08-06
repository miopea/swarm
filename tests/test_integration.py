"""Integration tests — full flow with mocked PTY processes."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from swarm.config import DroneConfig
from swarm.drones.log import DroneLog, SystemAction
from swarm.drones.pilot import DronePilot
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import TaskPriority, TaskStatus
from swarm.worker.worker import Worker, WorkerState
from tests.fakes.process import FakeWorkerProcess


def _make_worker(name: str, *, content: str = "esc to interrupt") -> Worker:
    """Create a Worker with a FakeWorkerProcess pre-loaded with content."""
    proc = FakeWorkerProcess(name=name)
    proc.set_content(content)
    return Worker(name=name, path=f"/tmp/{name}", process=proc)


@pytest.fixture
def mock_revive(monkeypatch):
    """Mock revive_worker so pilot can revive without a real ProcessPool."""
    mock = AsyncMock()
    monkeypatch.setattr(
        "swarm.drones.pilot.revive_worker",
        mock,
    )
    return mock


@pytest.mark.asyncio
async def test_full_poll_cycle(mock_revive):
    """Full poll cycle: BUZZING workers need no action."""
    workers = [
        _make_worker("api"),
        _make_worker("web"),
    ]
    log = DroneLog()
    board = TaskBoard()
    pilot = DronePilot(
        workers,
        log,
        interval=1.0,
        drone_config=DroneConfig(),
        task_board=board,
    )
    pilot.enabled = True

    # Both workers have "esc to interrupt" content → BUZZING
    await pilot.poll_once()
    assert len(log.entries) == 0  # No actions needed


@pytest.mark.asyncio
async def test_stung_to_revive_to_buzzing(mock_revive):
    """Test lifecycle: STUNG -> revive -> BUZZING."""
    worker = _make_worker("api")
    workers = [worker]
    proc: FakeWorkerProcess = worker.process  # type: ignore[assignment]
    log = DroneLog()
    pilot = DronePilot(
        workers,
        log,
        interval=1.0,
        drone_config=DroneConfig(),
    )
    pilot.enabled = True

    # Phase 1: Worker process dies → STUNG on first poll
    proc._alive = False

    await pilot.poll_once()  # transitions to STUNG
    assert worker.state == WorkerState.STUNG

    await pilot.poll_once()  # STUNG → decide → REVIVE
    assert any(e.action == SystemAction.REVIVED for e in log.entries)

    # Phase 2: Worker comes back (BUZZING)
    proc._alive = True
    proc._foreground_command = "claude"
    proc.set_content("esc to interrupt")

    await pilot.poll_once()
    assert worker.state == WorkerState.BUZZING
    assert worker.revive_count == 0  # Reset on BUZZING transition


@pytest.mark.asyncio
async def test_task_lifecycle():
    """Test task lifecycle: create -> assign -> complete."""
    board = TaskBoard()

    # Create
    task = board.create("Fix API bug", priority=TaskPriority.HIGH)
    assert task.status == TaskStatus.UNASSIGNED
    assert len(board.available_tasks) == 1

    # Assign
    board.assign(task.id, "api")
    assert task.status == TaskStatus.ASSIGNED
    assert len(board.available_tasks) == 0
    assert len(board.assigned_or_active_tasks) == 1

    # Complete
    board.complete(task.id)
    assert task.status == TaskStatus.DONE
    assert len(board.assigned_or_active_tasks) == 0


@pytest.mark.asyncio
async def test_task_dependency_flow():
    """Dependent tasks become available when dependencies complete."""
    board = TaskBoard()

    t1 = board.create("Build API")
    t2 = board.create("Build frontend", depends_on=[t1.id])
    t3 = board.create("Run tests", depends_on=[t1.id, t2.id])

    # Only t1 is available initially
    available = board.available_tasks
    assert t1 in available
    assert t2 not in available
    assert t3 not in available

    # Assign and complete t1 -> t2 becomes available
    board.assign(t1.id, "worker-1")
    board.complete(t1.id)
    available = board.available_tasks
    assert t2 in available
    assert t3 not in available

    # Assign and complete t2 -> t3 becomes available
    board.assign(t2.id, "worker-1")
    board.complete(t2.id)
    available = board.available_tasks
    assert t3 in available


@pytest.mark.asyncio
async def test_dead_worker_unassigns_tasks():
    """When a worker dies, its tasks should be unassigned."""
    board = TaskBoard()
    task = board.create("Fix bug")
    board.assign(task.id, "api")
    assert task.assigned_worker == "api"

    board.unassign_worker("api")
    assert task.status == TaskStatus.UNASSIGNED
    assert task.assigned_worker is None


@pytest.mark.asyncio
async def test_worker_state_change_callbacks(mock_revive):
    """State change callbacks should fire correctly."""
    worker = _make_worker("api")
    workers = [worker]
    proc: FakeWorkerProcess = worker.process  # type: ignore[assignment]
    log = DroneLog()
    pilot = DronePilot(
        workers,
        log,
        interval=1.0,
        drone_config=DroneConfig(),
    )

    # Kill process → STUNG on first poll
    proc._alive = False

    state_changes: list[tuple[str, WorkerState]] = []
    pilot.on_state_changed(
        lambda w: state_changes.append((w.name, w.state)),
    )

    await pilot.poll_once()  # transitions to STUNG, fires callback
    assert len(state_changes) == 1
    assert state_changes[0] == ("api", WorkerState.STUNG)


@pytest.mark.asyncio
async def test_cannot_complete_failed_task():
    """Cannot complete a task that's already failed."""
    board = TaskBoard()
    task = board.create("Doomed task")
    board.assign(task.id, "api")
    board.fail(task.id)
    assert task.status == TaskStatus.FAILED

    result = board.complete(task.id)
    assert result is False
    assert task.status == TaskStatus.FAILED
