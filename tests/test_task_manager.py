from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from swarm.config.models import HiveConfig
from swarm.drones.log import DroneLog, LogCategory, SystemAction
from swarm.drones.pilot import DronePilot
from swarm.server.daemon import TaskOperationError
from swarm.server.task_manager import TaskManager
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskAction, TaskHistory
from swarm.tasks.task import TaskPriority, TaskStatus, TaskType


@pytest.fixture
def mgr():
    board = TaskBoard()
    history = TaskHistory(log_file=Path(tempfile.mktemp(suffix=".jsonl")))
    drone_log = DroneLog()
    pilot = MagicMock(spec=DronePilot)
    config = HiveConfig()
    # Disable LLM criteria synthesis by default so create_task_smart tests
    # don't spawn a real headless subprocess. The synthesis wiring has its
    # own dedicated mocked tests below.
    config.drones.verifier_criteria_synthesis = False
    return TaskManager(board, history, drone_log, pilot, config=config)


def test_require_task_found(mgr):
    task = mgr.create_task("Test Task")
    result = mgr.require_task(task.id)
    assert result == task


def test_require_task_not_found(mgr):
    with pytest.raises(TaskOperationError, match="not found"):
        mgr.require_task("nonexistent")


def test_require_task_wrong_status(mgr):
    task = mgr.create_task("Test Task")
    with pytest.raises(TaskOperationError, match="cannot be modified"):
        mgr.require_task(task.id, {TaskStatus.DONE})


def test_create_task_basic(mgr):
    task = mgr.create_task(
        title="Basic Task",
        description="Description",
        priority=TaskPriority.HIGH,
        task_type=TaskType.BUG,
        tags=["tag1", "tag2"],
        actor="test-actor",
    )
    assert task.title == "Basic Task"
    assert task.description == "Description"
    assert task.priority == TaskPriority.HIGH
    assert task.task_type == TaskType.BUG
    assert task.tags == ["tag1", "tag2"]

    history_entries = mgr.task_history.get_events(task.id)
    assert len(history_entries) == 1
    assert history_entries[0].action == TaskAction.CREATED
    assert history_entries[0].actor == "test-actor"

    log_entries = mgr.drone_log.entries
    task_created = [e for e in log_entries if e.action == SystemAction.TASK_CREATED]
    assert len(task_created) == 1
    assert task_created[0].worker_name == "test-actor"
    assert task_created[0].detail == "Basic Task"
    assert task_created[0].category == LogCategory.TASK


@pytest.mark.asyncio
async def test_create_task_smart_with_title(mgr):
    task = await mgr.create_task_smart(
        title="Smart Task",
        description="Some description",
        priority=TaskPriority.LOW,
        actor="smart-actor",
    )
    assert task.title == "Smart Task"
    assert task.description == "Some description"
    assert task.priority == TaskPriority.LOW
    assert task.task_type in list(TaskType)


@pytest.mark.asyncio
async def test_create_task_smart_without_title(mgr):
    with patch("swarm.server.task_manager.smart_title") as mock_smart_title:
        mock_smart_title.return_value = "Generated Title"
        task = await mgr.create_task_smart(
            description="Description without title",
            actor="smart-actor",
        )
        assert task.title == "Generated Title"
        mock_smart_title.assert_called_once_with("Description without title")


@pytest.mark.asyncio
async def test_create_task_smart_no_title_no_description(mgr):
    # ValueError (not SwarmOperationError) — input-validation failure
    # maps to HTTP 400 via handle_errors.  See Phase C of the
    # duplication-cluster sweep.
    with pytest.raises(ValueError, match="title or description required"):
        await mgr.create_task_smart()


@pytest.mark.asyncio
async def test_create_task_smart_explicit_type(mgr):
    task = await mgr.create_task_smart(
        title="Explicit Type Task",
        task_type=TaskType.FEATURE,
    )
    assert task.task_type == TaskType.FEATURE


@pytest.mark.asyncio
async def test_create_task_smart_auto_classify_type(mgr):
    with patch("swarm.server.task_manager.auto_classify_type") as mock_classify:
        mock_classify.return_value = TaskType.BUG
        task = await mgr.create_task_smart(
            title="Fix something broken",
            description="It's broken",
        )
        assert task.task_type == TaskType.BUG
        mock_classify.assert_called_once_with("Fix something broken", "It's broken")


# --- Acceptance-criteria synthesis (Outcomes rubric) ---------------------


def _synth_mgr(enabled: bool) -> TaskManager:
    board = TaskBoard()
    history = TaskHistory(log_file=Path(tempfile.mktemp(suffix=".jsonl")))
    config = HiveConfig()
    config.drones.verifier_criteria_synthesis = enabled
    return TaskManager(board, history, DroneLog(), MagicMock(spec=DronePilot), config=config)


@pytest.mark.asyncio
async def test_synthesis_disabled_skips_llm():
    mgr = _synth_mgr(enabled=False)
    task = mgr.create_task("Do a thing", description="details")
    with patch("swarm.tasks.task.synthesize_acceptance_criteria") as synth:
        await mgr.apply_synthesized_criteria(task)
        synth.assert_not_called()
    assert task.acceptance_criteria == []
    assert task.effort_tier == ""


@pytest.mark.asyncio
async def test_synthesis_populates_criteria_and_tier():
    mgr = _synth_mgr(enabled=True)
    task = mgr.create_task("Add endpoint", description="add GET /widgets")
    with patch(
        "swarm.tasks.task.synthesize_acceptance_criteria",
        return_value=(["Returns 200 for valid id", "Test added"], "medium"),
    ):
        await mgr.apply_synthesized_criteria(task)
    reloaded = mgr.task_board.get(task.id)
    assert reloaded.acceptance_criteria == ["Returns 200 for valid id", "Test added"]
    assert reloaded.effort_tier == "medium"


@pytest.mark.asyncio
async def test_synthesis_skips_standing_loop_tasks():
    from swarm.drones.standing_loop import STANDING_LOOP_TAG

    mgr = _synth_mgr(enabled=True)
    task = mgr.create_task("Idle filler", description="x", tags=[STANDING_LOOP_TAG])
    with patch("swarm.tasks.task.synthesize_acceptance_criteria") as synth:
        await mgr.apply_synthesized_criteria(task)
        synth.assert_not_called()
    assert task.acceptance_criteria == []


@pytest.mark.asyncio
async def test_synthesis_skips_when_criteria_already_present():
    mgr = _synth_mgr(enabled=True)
    task = mgr.create_task("Has criteria", description="x")
    mgr.edit_task(task.id, acceptance_criteria=["worker-supplied"])
    with patch("swarm.tasks.task.synthesize_acceptance_criteria") as synth:
        await mgr.apply_synthesized_criteria(mgr.task_board.get(task.id))
        synth.assert_not_called()
    assert mgr.task_board.get(task.id).acceptance_criteria == ["worker-supplied"]


@pytest.mark.asyncio
async def test_synthesis_empty_result_leaves_task_untouched():
    mgr = _synth_mgr(enabled=True)
    task = mgr.create_task("Open-ended", description="investigate the flakiness")
    with patch("swarm.tasks.task.synthesize_acceptance_criteria", return_value=([], "")):
        await mgr.apply_synthesized_criteria(task)
    assert mgr.task_board.get(task.id).acceptance_criteria == []
    assert mgr.task_board.get(task.id).effort_tier == ""


def test_coerce_criteria_caps_and_cleans():
    from swarm.tasks.task import _coerce_criteria

    assert _coerce_criteria(["a", " b ", "", None, "c", "d", "e"]) == ["a", "b", "c", "d"]
    assert _coerce_criteria("not a list") == []
    assert _coerce_criteria(None) == []
    # long strings are truncated to 300 chars
    assert len(_coerce_criteria(["x" * 500])[0]) == 300


def test_coerce_tier_validates():
    from swarm.tasks.task import _coerce_tier

    assert _coerce_tier("HIGH") == "high"
    assert _coerce_tier("medium") == "medium"
    assert _coerce_tier("bogus") == ""
    assert _coerce_tier(None) == ""


@pytest.mark.asyncio
async def test_synthesize_blank_description_returns_empty():
    from swarm.tasks.task import synthesize_acceptance_criteria

    # No subprocess should be spawned for a blank description.
    assert await synthesize_acceptance_criteria("title", "   ") == ([], "")


def test_unassign_task_success(mgr):
    task = mgr.create_task("Assigned Task")
    mgr.task_board.assign(task.id, "worker1")

    result = mgr.unassign_task(task.id, actor="test-actor")

    assert result is True
    # Unassign returns the task to the auto-assignable pool — Unassigned, not
    # Backlog (which is the operator's parked-ideas lane).
    assert task.status == TaskStatus.UNASSIGNED
    mgr._pilot.clear_proposed_completion.assert_called_once_with(task.id)

    history_entries = mgr.task_history.get_events(task.id)
    edited = [e for e in history_entries if e.action == TaskAction.EDITED]
    assert len(edited) == 1
    assert edited[0].detail == "unassigned"


def test_unassign_task_wrong_status(mgr):
    task = mgr.create_task("Pending Task")
    with pytest.raises(TaskOperationError, match="cannot be modified"):
        mgr.unassign_task(task.id)


def test_reopen_task_from_completed(mgr):
    task = mgr.create_task("Completed Task")
    mgr.task_board.assign(task.id, "worker1")
    mgr.task_board.complete(task.id)

    result = mgr.reopen_task(task.id, actor="test-actor")

    assert result is True
    # v9 cleanup: reopen lands in Backlog
    assert task.status == TaskStatus.BACKLOG
    mgr._pilot.clear_proposed_completion.assert_called_once_with(task.id)

    history_entries = mgr.task_history.get_events(task.id)
    reopened = [e for e in history_entries if e.action == TaskAction.REOPENED]
    assert len(reopened) == 1
    assert reopened[0].actor == "test-actor"


def test_reopen_task_from_failed(mgr):
    task = mgr.create_task("Failed Task")
    mgr.task_board.fail(task.id)

    result = mgr.reopen_task(task.id, actor="test-actor")

    assert result is True
    # v9 cleanup: reopen lands in Backlog
    assert task.status == TaskStatus.BACKLOG


def test_reopen_task_wrong_status(mgr):
    task = mgr.create_task("Pending Task")
    with pytest.raises(TaskOperationError, match="cannot be modified"):
        mgr.reopen_task(task.id)


def test_fail_task_success(mgr):
    task = mgr.create_task("Task to Fail")

    result = mgr.fail_task(task.id, actor="test-actor")

    assert result is True
    assert task.status == TaskStatus.FAILED

    history_entries = mgr.task_history.get_events(task.id)
    failed = [e for e in history_entries if e.action == TaskAction.FAILED]
    assert len(failed) == 1
    assert failed[0].actor == "test-actor"

    log_entries = mgr.drone_log.entries
    task_failed = [e for e in log_entries if e.action == SystemAction.TASK_FAILED]
    assert len(task_failed) == 1
    assert task_failed[0].detail == "Task to Fail"
    assert task_failed[0].category == LogCategory.TASK
    assert task_failed[0].is_notification is True


def test_fail_task_not_found(mgr):
    with pytest.raises(TaskOperationError, match="not found"):
        mgr.fail_task("nonexistent")


def test_remove_task_success(mgr):
    task = mgr.create_task("Task to Remove")

    result = mgr.remove_task(task.id, actor="test-actor")

    assert result is True
    assert mgr.task_board.get(task.id) is None

    history_entries = mgr.task_history.get_events(task.id)
    removed = [e for e in history_entries if e.action == TaskAction.REMOVED]
    assert len(removed) == 1

    log_entries = mgr.drone_log.entries
    task_removed = [e for e in log_entries if e.action == SystemAction.TASK_REMOVED]
    assert len(task_removed) == 1
    assert task_removed[0].detail == "Task to Remove"
    assert task_removed[0].category == LogCategory.TASK


def test_remove_task_not_found(mgr):
    with pytest.raises(TaskOperationError, match="not found"):
        mgr.remove_task("nonexistent")


def test_edit_task_success(mgr):
    task = mgr.create_task("Original Title", description="Original description")

    result = mgr.edit_task(
        task.id,
        title="Updated Title",
        description="Updated description",
        priority=TaskPriority.HIGH,
        task_type=TaskType.FEATURE,
        tags=["new-tag"],
        actor="test-actor",
    )

    assert result is True
    assert task.title == "Updated Title"
    assert task.description == "Updated description"
    assert task.priority == TaskPriority.HIGH
    assert task.task_type == TaskType.FEATURE
    assert task.tags == ["new-tag"]

    history_entries = mgr.task_history.get_events(task.id)
    edited = [e for e in history_entries if e.action == TaskAction.EDITED]
    assert len(edited) == 1
    assert edited[0].actor == "test-actor"


def test_edit_task_partial_update(mgr):
    task = mgr.create_task("Original Title", description="Original description")

    result = mgr.edit_task(task.id, title="New Title")

    assert result is True
    assert task.title == "New Title"
    assert task.description == "Original description"


def test_edit_task_cross_task_fields(mgr):
    task = mgr.create_task("Cross Task", description="Needs cross-task fields")

    result = mgr.edit_task(
        task.id,
        source_worker="hub",
        target_worker="platform",
        dependency_type="blocks",
        acceptance_criteria=["criterion 1", "criterion 2"],
        context_refs=["src/foo.py"],
    )

    assert result is True
    assert task.source_worker == "hub"
    assert task.target_worker == "platform"
    assert task.dependency_type == "blocks"
    assert task.acceptance_criteria == ["criterion 1", "criterion 2"]
    assert task.context_refs == ["src/foo.py"]


def test_edit_task_not_found(mgr):
    with pytest.raises(TaskOperationError, match="not found"):
        mgr.edit_task("nonexistent", title="New Title")


def test_manager_without_pilot(mgr):
    mgr_no_pilot = TaskManager(mgr.task_board, mgr.task_history, mgr.drone_log, pilot=None)

    task = mgr_no_pilot.create_task("Test Task")
    mgr_no_pilot.task_board.assign(task.id, "worker1")

    result = mgr_no_pilot.unassign_task(task.id)
    assert result is True


def test_unassign_task_no_pilot_no_crash(mgr):
    mgr._pilot = None
    task = mgr.create_task("Test Task")
    mgr.task_board.assign(task.id, "worker1")

    result = mgr.unassign_task(task.id)
    assert result is True


def test_reopen_task_no_pilot_no_crash(mgr):
    mgr._pilot = None
    task = mgr.create_task("Test Task")
    mgr.task_board.assign(task.id, "worker1")
    mgr.task_board.complete(task.id)

    result = mgr.reopen_task(task.id)
    assert result is True


def test_fail_task_emits_notification(mgr):
    bus = MagicMock()
    mgr._notification_bus = bus
    task = mgr.create_task("Doomed Task")
    mgr.task_board.assign(task.id, "alice")
    mgr.task_board.activate(task.id)
    assert mgr.fail_task(task.id) is True
    bus.emit_task_failed.assert_called_once_with("alice", "Doomed Task")


def test_reopen_task_emits_notification(mgr):
    bus = MagicMock()
    mgr._notification_bus = bus
    task = mgr.create_task("Round Two")
    mgr.task_board.assign(task.id, "bob")
    mgr.task_board.activate(task.id)
    mgr.task_board.complete(task.id, resolution="done")
    assert mgr.reopen_task(task.id) is True
    bus.emit_task_reopened.assert_called_once_with("bob", "Round Two")


def test_fail_task_without_bus_no_crash(mgr):
    task = mgr.create_task("No Bus")
    assert mgr.fail_task(task.id) is True
