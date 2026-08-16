"""Tests for cross-project task system — model, persistence, board, manager, validation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from swarm.drones.log import DroneLog, SystemAction
from swarm.drones.pilot import DronePilot
from swarm.server.daemon import TaskOperationError
from swarm.server.task_manager import TaskManager
from swarm.tasks.board import TaskBoard
from swarm.tasks.cross_task import (
    parse_cross_task_file,
    scan_cross_task_dir,
    validate_cross_task,
)
from swarm.tasks.history import TaskAction, TaskHistory
from swarm.tasks.store import FileTaskStore
from swarm.tasks.task import (
    DEPENDENCY_TYPE_MAP,
    STATUS_ICON,
    DependencyType,
    SwarmTask,
    TaskPriority,
    TaskStatus,
    TaskType,
)

# ---------------------------------------------------------------------------
# 1. Task Model — PROPOSED status, DependencyType, cross-project fields
# ---------------------------------------------------------------------------


class TestTaskModelCrossProject:
    def test_proposed_status_exists(self):
        assert TaskStatus.BACKLOG.value == "backlog"

    def test_proposed_before_pending(self):
        values = [s.value for s in TaskStatus]
        assert values.index("backlog") < values.index("unassigned")

    def test_dependency_type_enum(self):
        assert DependencyType.BLOCKS.value == "blocks"
        assert DependencyType.ENHANCES.value == "enhances"
        assert DependencyType.ENABLES.value == "enables"

    def test_dependency_type_map(self):
        assert DEPENDENCY_TYPE_MAP["blocks"] == DependencyType.BLOCKS
        assert DEPENDENCY_TYPE_MAP["enhances"] == DependencyType.ENHANCES
        assert DEPENDENCY_TYPE_MAP["enables"] == DependencyType.ENABLES

    def test_cross_project_fields_defaults(self):
        task = SwarmTask(title="Test")
        assert task.is_cross_project is False
        assert task.source_worker == ""
        assert task.target_worker == ""
        assert task.dependency_type == "blocks"
        assert task.acceptance_criteria == []
        assert task.context_refs == []

    def test_cross_project_fields_set(self):
        task = SwarmTask(
            title="Add API endpoint",
            is_cross_project=True,
            source_worker="hub",
            target_worker="platform",
            dependency_type="enables",
            acceptance_criteria=["Endpoint returns 200", "Auth required"],
            context_refs=["src/api/routes.ts"],
        )
        assert task.is_cross_project is True
        assert task.source_worker == "hub"
        assert task.target_worker == "platform"
        assert task.dependency_type == "enables"
        assert len(task.acceptance_criteria) == 2
        assert len(task.context_refs) == 1

    def test_proposed_status_icon(self):
        assert TaskStatus.BACKLOG in STATUS_ICON
        assert STATUS_ICON[TaskStatus.BACKLOG] == "◇"

    def test_approve_from_proposed(self):
        task = SwarmTask(title="Test", status=TaskStatus.BACKLOG)
        task.approve()
        assert task.status == TaskStatus.UNASSIGNED

    def test_approve_from_non_proposed_raises(self):
        task = SwarmTask(title="Test", status=TaskStatus.UNASSIGNED)
        with pytest.raises(AssertionError, match="Cannot approve"):
            task.approve()

    def test_reject_from_proposed(self):
        task = SwarmTask(title="Test", status=TaskStatus.BACKLOG)
        task.reject("Not needed")
        assert task.status == TaskStatus.FAILED
        assert task.resolution == "Not needed"

    def test_reject_from_non_proposed_raises(self):
        task = SwarmTask(title="Test", status=TaskStatus.ASSIGNED)
        with pytest.raises(AssertionError, match="Cannot reject"):
            task.reject()

    def test_backlog_is_not_available(self):
        """v9 vocabulary cleanup: Backlog tasks sit parked until the operator
        promotes them. The auto-assign drone only picks Unassigned tasks
        — Backlog deliberately bypasses the queue."""
        task = SwarmTask(title="Test", status=TaskStatus.BACKLOG)
        assert task.is_available is False


# ---------------------------------------------------------------------------
# 2. Store — serialize/deserialize cross-project fields
# ---------------------------------------------------------------------------


class TestStoreCrossProject:
    @pytest.fixture
    def store(self, tmp_path):
        return FileTaskStore(path=tmp_path / "tasks.json")

    def test_cross_project_roundtrip(self, store):
        task = SwarmTask(
            id="cross1",
            title="Cross-project task",
            is_cross_project=True,
            source_worker="hub",
            target_worker="platform",
            dependency_type="blocks",
            acceptance_criteria=["criterion1"],
            context_refs=["ref1"],
        )
        store.save({"cross1": task})
        loaded = store.load()
        t = loaded["cross1"]
        assert t.is_cross_project is True
        assert t.source_worker == "hub"
        assert t.target_worker == "platform"
        assert t.dependency_type == "blocks"
        assert t.acceptance_criteria == ["criterion1"]
        assert t.context_refs == ["ref1"]

    def test_backward_compat_defaults(self, store):
        """Old tasks without cross-project fields load fine."""
        task = SwarmTask(id="old1", title="Old task")
        store.save({"old1": task})
        loaded = store.load()
        t = loaded["old1"]
        assert t.is_cross_project is False
        assert t.source_worker == ""
        assert t.target_worker == ""
        assert t.dependency_type == "blocks"
        assert t.acceptance_criteria == []
        assert t.context_refs == []

    def test_proposed_status_persists(self, store):
        task = SwarmTask(id="prop1", title="Proposed", status=TaskStatus.BACKLOG)
        store.save({"prop1": task})
        loaded = store.load()
        assert loaded["prop1"].status == TaskStatus.BACKLOG


# ---------------------------------------------------------------------------
# 3. TaskBoard — cross-project methods
# ---------------------------------------------------------------------------


class TestBoardCrossProject:
    def test_create_cross_project(self):
        board = TaskBoard()
        task = board.create_cross_project(
            title="Add endpoint",
            source_worker="hub",
            target_worker="platform",
            dependency_type="blocks",
        )
        assert task.status == TaskStatus.BACKLOG
        assert task.is_cross_project is True
        assert task.source_worker == "hub"
        assert task.target_worker == "platform"
        assert task.number > 0

    def test_approve_task(self):
        board = TaskBoard()
        task = board.create_cross_project(title="Test", source_worker="a", target_worker="b")
        assert board.approve_task(task.id) is True
        assert task.status == TaskStatus.UNASSIGNED

    def test_approve_nonexistent(self):
        board = TaskBoard()
        assert board.approve_task("nonexistent") is False

    def test_approve_non_proposed(self):
        board = TaskBoard()
        task = board.create("Regular task")
        assert board.approve_task(task.id) is False

    def test_reject_task(self):
        board = TaskBoard()
        task = board.create_cross_project(title="Test", source_worker="a", target_worker="b")
        assert board.reject_task(task.id, "Not needed") is True
        assert task.status == TaskStatus.FAILED
        assert task.resolution == "Not needed"

    def test_reject_nonexistent(self):
        board = TaskBoard()
        assert board.reject_task("nonexistent") is False

    def test_proposed_tasks_property(self):
        board = TaskBoard()
        board.create("Regular task")
        cross = board.create_cross_project(title="Cross", source_worker="a", target_worker="b")
        proposed = board.proposed_tasks
        assert len(proposed) == 1
        assert proposed[0].id == cross.id

    def test_backlog_excluded_from_available(self):
        """v9 cleanup: Backlog tasks (cross-project + operator-drafted) are
        deliberately *not* available for auto-assignment. The operator must
        promote them to Unassigned via the "Hand to Queen" button first."""
        board = TaskBoard()
        board.create_cross_project(title="Cross", source_worker="a", target_worker="b")
        assert len(board.available_tasks) == 0

    def test_summary_includes_backlog(self):
        board = TaskBoard()
        board.create_cross_project(title="Cross", source_worker="a", target_worker="b")
        summary = board.summary()
        assert "1 backlog" in summary

    def test_summary_omits_proposed_when_zero(self):
        board = TaskBoard()
        board.create("Regular")
        summary = board.summary()
        assert "backlog" not in summary

    def test_cross_project_with_store(self, tmp_path):
        store = FileTaskStore(path=tmp_path / "tasks.json")
        board = TaskBoard(store=store)
        task = board.create_cross_project(
            title="Persistent cross",
            source_worker="hub",
            target_worker="platform",
        )
        # Reload from store
        board2 = TaskBoard(store=store)
        restored = board2.get(task.id)
        assert restored is not None
        assert restored.is_cross_project is True
        assert restored.status == TaskStatus.BACKLOG


# ---------------------------------------------------------------------------
# 4. TaskHistory — PROPOSED, APPROVED actions
# ---------------------------------------------------------------------------


class TestHistoryCrossProject:
    def test_proposed_action_exists(self):
        assert TaskAction.PROPOSED.value == "PROPOSED"

    def test_approved_action_exists(self):
        assert TaskAction.APPROVED.value == "APPROVED"


# ---------------------------------------------------------------------------
# 5. DroneLog — TASK_PROPOSED, TASK_APPROVED actions
# ---------------------------------------------------------------------------


class TestDroneLogCrossProject:
    def test_task_proposed_action(self):
        assert SystemAction.TASK_PROPOSED.value == "TASK_PROPOSED"

    def test_task_approved_action(self):
        assert SystemAction.TASK_APPROVED.value == "TASK_APPROVED"


# ---------------------------------------------------------------------------
# 6. Cross-task validation and file parsing
# ---------------------------------------------------------------------------


class TestCrossTaskValidation:
    def test_valid_payload(self):
        data = {
            "title": "Add API endpoint",
            "source_worker": "hub",
            "target_worker": "platform",
        }
        assert validate_cross_task(data) is None

    def test_valid_full_payload(self):
        data = {
            "title": "Add API endpoint",
            "description": "Need a new REST endpoint",
            "source_worker": "hub",
            "target_worker": "platform",
            "dependency_type": "blocks",
            "priority": "high",
            "task_type": "feature",
            "acceptance_criteria": ["Returns 200", "Auth required"],
            "context_refs": ["src/api.ts"],
        }
        assert validate_cross_task(data) is None

    def test_missing_title(self):
        data = {"source_worker": "hub", "target_worker": "platform"}
        assert validate_cross_task(data) == "title is required"

    def test_empty_title(self):
        data = {"title": "  ", "source_worker": "hub", "target_worker": "platform"}
        assert validate_cross_task(data) == "title is required"

    def test_title_too_long(self):
        data = {
            "title": "x" * 501,
            "source_worker": "hub",
            "target_worker": "platform",
        }
        assert "title exceeds" in validate_cross_task(data)

    def test_missing_source_worker(self):
        data = {"title": "Test", "target_worker": "platform"}
        assert validate_cross_task(data) == "source_worker is required"

    def test_missing_target_worker(self):
        data = {"title": "Test", "source_worker": "hub"}
        assert validate_cross_task(data) == "target_worker is required"

    def test_invalid_dependency_type(self):
        data = {
            "title": "Test",
            "source_worker": "hub",
            "target_worker": "platform",
            "dependency_type": "invalid",
        }
        assert "dependency_type must be one of" in validate_cross_task(data)

    def test_invalid_priority(self):
        data = {
            "title": "Test",
            "source_worker": "hub",
            "target_worker": "platform",
            "priority": "critical",
        }
        assert "priority must be one of" in validate_cross_task(data)

    def test_invalid_task_type(self):
        data = {
            "title": "Test",
            "source_worker": "hub",
            "target_worker": "platform",
            "task_type": "invalid",
        }
        assert "task_type must be one of" in validate_cross_task(data)

    def test_description_too_long(self):
        data = {
            "title": "Test",
            "source_worker": "hub",
            "target_worker": "platform",
            "description": "x" * 10_001,
        }
        assert "description exceeds" in validate_cross_task(data)

    def test_acceptance_criteria_not_list(self):
        data = {
            "title": "Test",
            "source_worker": "hub",
            "target_worker": "platform",
            "acceptance_criteria": "not a list",
        }
        assert "acceptance_criteria must be a list" in validate_cross_task(data)

    def test_context_refs_not_list(self):
        data = {
            "title": "Test",
            "source_worker": "hub",
            "target_worker": "platform",
            "context_refs": "not a list",
        }
        assert "context_refs must be a list" in validate_cross_task(data)

    def test_not_a_dict(self):
        assert validate_cross_task("not a dict") == "payload must be a JSON object"


class TestCrossTaskFileParsing:
    def test_parse_valid_file(self, tmp_path):
        data = {
            "title": "Test task",
            "source_worker": "hub",
            "target_worker": "platform",
        }
        path = tmp_path / "task.json"
        path.write_text(json.dumps(data))
        result = parse_cross_task_file(path)
        assert result is not None
        assert result["title"] == "Test task"

    def test_parse_invalid_json(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json{{{")
        assert parse_cross_task_file(path) is None

    def test_parse_invalid_payload(self, tmp_path):
        path = tmp_path / "missing.json"
        path.write_text(json.dumps({"title": "No workers"}))
        assert parse_cross_task_file(path) is None

    def test_scan_cross_task_dir(self, tmp_path):
        data = {
            "title": "Scan task",
            "source_worker": "hub",
            "target_worker": "platform",
        }
        (tmp_path / "task1.json").write_text(json.dumps(data))
        (tmp_path / "task2.json").write_text(json.dumps(data))
        (tmp_path / "bad.json").write_text("invalid")
        (tmp_path / "readme.txt").write_text("ignored")

        import swarm.tasks.cross_task as ct

        original = ct.CROSS_TASK_DIR
        ct.CROSS_TASK_DIR = tmp_path
        try:
            results = scan_cross_task_dir()
            assert len(results) == 2
            paths = {r[0].name for r in results}
            assert "task1.json" in paths
            assert "task2.json" in paths
        finally:
            ct.CROSS_TASK_DIR = original

    def test_scan_missing_dir(self):
        import swarm.tasks.cross_task as ct

        original = ct.CROSS_TASK_DIR
        ct.CROSS_TASK_DIR = Path("/tmp/nonexistent_swarm_cross_tasks_test")
        try:
            assert scan_cross_task_dir() == []
        finally:
            ct.CROSS_TASK_DIR = original


# ---------------------------------------------------------------------------
# 7. TaskManager — cross-project lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture
def mgr(tmp_path):
    board = TaskBoard()
    history = TaskHistory(log_file=tmp_path / "task-history.jsonl")
    drone_log = DroneLog()
    pilot = MagicMock(spec=DronePilot)
    return TaskManager(board, history, drone_log, pilot)


class TestTaskManagerCrossProject:
    def test_create_cross_task(self, mgr):
        task = mgr.create_cross_task(
            title="Add endpoint",
            description="Need REST endpoint for contacts",
            source_worker="hub",
            target_worker="platform",
            dependency_type="blocks",
            priority=TaskPriority.HIGH,
            task_type=TaskType.FEATURE,
            acceptance_criteria=["Returns 200"],
            context_refs=["src/api.ts"],
        )
        assert task.status == TaskStatus.BACKLOG
        assert task.is_cross_project is True
        assert task.source_worker == "hub"
        assert task.target_worker == "platform"
        assert task.priority == TaskPriority.HIGH

        # Check history logged
        events = mgr.task_history.get_events(task.id)
        proposed = [e for e in events if e.action == TaskAction.PROPOSED]
        assert len(proposed) == 1
        assert "hub" in proposed[0].detail

        # Check drone log
        log_entries = [e for e in mgr.drone_log.entries if e.action == SystemAction.TASK_PROPOSED]
        assert len(log_entries) == 1
        assert log_entries[0].is_notification is True

    def test_approve_cross_task(self, mgr):
        task = mgr.create_cross_task(title="Test", source_worker="hub", target_worker="platform")
        result = mgr.approve_cross_task(task.id, actor="operator")
        assert result is True
        # Auto-assigned to target_worker
        assert task.status == TaskStatus.ASSIGNED
        assert task.assigned_worker == "platform"

        events = mgr.task_history.get_events(task.id)
        approved = [e for e in events if e.action == TaskAction.APPROVED]
        assert len(approved) == 1
        assert approved[0].actor == "operator"

        log_entries = [e for e in mgr.drone_log.entries if e.action == SystemAction.TASK_APPROVED]
        assert len(log_entries) == 1

    def test_approve_non_proposed_raises(self, mgr):
        task = mgr.create_task("Regular task")
        with pytest.raises(TaskOperationError, match="cannot be modified"):
            mgr.approve_cross_task(task.id)

    def test_approve_nonexistent_raises(self, mgr):
        with pytest.raises(TaskOperationError, match="not found"):
            mgr.approve_cross_task("nonexistent")

    def test_reject_cross_task(self, mgr):
        task = mgr.create_cross_task(title="Test", source_worker="hub", target_worker="platform")
        result = mgr.reject_cross_task(task.id, actor="operator")
        assert result is True
        assert task.status == TaskStatus.FAILED

        events = mgr.task_history.get_events(task.id)
        failed = [e for e in events if e.action == TaskAction.FAILED]
        assert len(failed) == 1
        assert failed[0].detail == "rejected"

        log_entries = [e for e in mgr.drone_log.entries if e.action == SystemAction.TASK_FAILED]
        assert len(log_entries) == 1

    def test_reject_non_proposed_raises(self, mgr):
        task = mgr.create_task("Regular task")
        with pytest.raises(TaskOperationError, match="cannot be modified"):
            mgr.reject_cross_task(task.id)

    def test_full_lifecycle_propose_approve_assign(self, mgr):
        """Cross-project task: propose → approve → assign → complete."""
        task = mgr.create_cross_task(
            title="Full lifecycle", source_worker="hub", target_worker="platform"
        )
        assert task.status == TaskStatus.BACKLOG

        mgr.approve_cross_task(task.id)
        # Auto-assigned to target_worker on approval
        assert task.status == TaskStatus.ASSIGNED
        assert task.assigned_worker == "platform"

        mgr.task_board.complete(task.id, "Done")
        assert task.status == TaskStatus.DONE

    def test_full_lifecycle_propose_reject(self, mgr):
        """Cross-project task: propose → reject."""
        task = mgr.create_cross_task(
            title="Rejected task", source_worker="hub", target_worker="platform"
        )
        mgr.reject_cross_task(task.id)
        assert task.status == TaskStatus.FAILED
