"""Tests for queen/context.py — hive context builder."""

from swarm.drones.log import DroneAction, DroneLog
from swarm.queen.context import build_hive_context
from swarm.tasks.board import TaskBoard
from swarm.tasks.proposal import AssignmentProposal, ProposalStatus
from swarm.tasks.task import TaskPriority
from swarm.worker.worker import Worker, WorkerState
from tests.fakes.process import FakeWorkerProcess


def _make_workers() -> list[Worker]:
    return [
        Worker(
            name="api",
            path="/tmp/api",
            process=FakeWorkerProcess(name="api"),
            state=WorkerState.BUZZING,
        ),
        Worker(
            name="web",
            path="/tmp/web",
            process=FakeWorkerProcess(name="web"),
            state=WorkerState.RESTING,
        ),
        Worker(
            name="tests",
            path="/tmp/tests",
            process=FakeWorkerProcess(name="tests"),
            state=WorkerState.STUNG,
        ),
    ]


class TestBuildHiveContext:
    def test_includes_all_workers(self):
        workers = _make_workers()
        ctx = build_hive_context(workers)
        assert "api" in ctx
        assert "web" in ctx
        assert "tests" in ctx

    def test_includes_state_info(self):
        workers = _make_workers()
        ctx = build_hive_context(workers)
        assert "buzzing" in ctx
        assert "resting" in ctx
        assert "stung" in ctx

    def test_includes_worker_outputs(self):
        workers = _make_workers()
        outputs = {"api": "Processing files...\nDone.", "web": "> idle prompt"}
        ctx = build_hive_context(workers, worker_outputs=outputs)
        assert "Processing files" in ctx
        assert "idle prompt" in ctx

    def test_includes_drone_log(self):
        workers = _make_workers()
        log = DroneLog()
        log.add(DroneAction.CONTINUED, "api", "choice menu")
        log.add(DroneAction.REVIVED, "tests", "worker exited")
        ctx = build_hive_context(workers, drone_log=log)
        assert "CONTINUED" in ctx
        assert "REVIVED" in ctx

    def test_includes_stats(self):
        workers = _make_workers()
        ctx = build_hive_context(workers)
        assert "Total workers: 3" in ctx
        assert "Buzzing (working): 1" in ctx

    def test_truncates_long_output(self):
        workers = _make_workers()
        long_output = "\n".join(f"line {i}" for i in range(100))
        ctx = build_hive_context(workers, worker_outputs={"api": long_output}, max_output_lines=5)
        # Should only have last 5 lines
        assert "line 95" in ctx
        assert "line 96" in ctx
        assert "line 0" not in ctx

    def test_revive_count_shown(self):
        workers = _make_workers()
        workers[2].revive_count = 3
        ctx = build_hive_context(workers)
        assert "revived 3x" in ctx

    def test_includes_task_board(self):
        workers = _make_workers()
        board = TaskBoard()
        board.create("Fix login bug", priority=TaskPriority.HIGH)
        board.create("Add tests", priority=TaskPriority.NORMAL)
        t3 = board.create("Deploy")
        board.assign(t3.id, "api")
        ctx = build_hive_context(workers, task_board=board)
        assert "Task Board" in ctx
        assert "Fix login bug" in ctx
        assert "Add tests" in ctx
        assert "Deploy" in ctx
        assert "3 tasks" in ctx

    def test_no_task_board_section_when_none(self):
        workers = _make_workers()
        ctx = build_hive_context(workers)
        assert "Task Board" not in ctx

    def test_rejection_feedback_section_renders_escalation(self):
        """A rejected escalation in proposal_history surfaces as operator feedback.

        Escalations have no task_title — the section must fall back to
        rule_pattern / assessment and name the worker so the Queen can tell
        what she was overruled on.
        """
        workers = _make_workers()
        rejected = AssignmentProposal.escalation(
            worker_name="api",
            action="send_message",
            assessment="approve grep",
            rule_pattern="grep -rn foo",
        )
        rejected.status = ProposalStatus.REJECTED
        rejected.rejection_reason = "operator will handle manually"
        ctx = build_hive_context(workers, proposal_history=[rejected])
        assert "Recent Proposal Rejections" in ctx
        assert "api" in ctx
        assert "grep -rn foo" in ctx
        assert "operator will handle manually" in ctx

    def test_no_rejection_section_when_history_empty(self):
        workers = _make_workers()
        ctx = build_hive_context(workers, proposal_history=[])
        assert "Recent Proposal Rejections" not in ctx

    def test_completed_tasks_capped_at_5(self):
        """Completed tasks should be capped at 5 to reduce Queen token usage."""
        workers = _make_workers()
        board = TaskBoard()
        # Create and complete 10 tasks
        for i in range(10):
            t = board.create(f"Task {i}")
            board.assign(t.id, "api")
            board.complete(t.id, resolution=f"Done {i}")
        ctx = build_hive_context(workers, task_board=board)
        assert "Completed" in ctx
        # Only last 5 should appear
        assert "showing last 5 of 10" in ctx
        assert "Task 9" in ctx
        assert "Task 5" in ctx
        # First tasks should NOT appear
        assert "Task 0" not in ctx
        assert "Task 4" not in ctx
