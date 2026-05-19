"""Tests for server/proposals.py — ProposalManager approval/rejection/logging."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.drones.log import DroneLog, LogCategory, SystemAction
from swarm.server.proposals import ProposalManager
from swarm.tasks.proposal import AssignmentProposal, ProposalStore
from swarm.worker.worker import WorkerState
from tests.conftest import make_worker as _conftest_make_worker


def _make_mgr():
    """Build a ProposalManager with explicit mock dependencies.

    Returns (mgr, store, drone_log, get_worker) — the objects tests
    need for assertions and mock setup.
    """
    drone_log = DroneLog()
    get_worker = MagicMock()
    store = ProposalStore()
    mgr = ProposalManager(
        store=store,
        broadcast_ws=MagicMock(),
        drone_log=drone_log,
        notification_bus=MagicMock(),
        task_board=MagicMock(),
        get_worker=get_worker,
        get_workers=MagicMock(return_value=[]),
        get_pilot=MagicMock(),
        assign_task=AsyncMock(),
        complete_task=MagicMock(),
        execute_escalation=AsyncMock(),
    )
    return mgr, store, drone_log, get_worker


def _make_worker(name: str = "api"):
    return _conftest_make_worker(name=name, state=WorkerState.RESTING)


class TestApproveEscalationWait:
    """Issue 1: approving a 'wait' escalation sends Enter to the worker process."""

    @pytest.mark.asyncio
    async def test_wait_approval_sends_enter(self):
        """When action='wait' is approved, send_enter should be called."""
        mgr, store, drone_log, get_worker = _make_mgr()

        proposal = AssignmentProposal.escalation(
            worker_name="api",
            action="wait",
            assessment="Worker showing plan prompt",
        )
        store.add(proposal)
        worker = _make_worker("api")
        get_worker.return_value = worker

        await mgr.approve(proposal.id)
        assert "\n" in worker.process.keys_sent

    @pytest.mark.asyncio
    async def test_non_wait_approval_does_not_send_enter(self):
        """When action='continue', send_enter should NOT be called from _approve_escalation."""
        mgr, store, drone_log, get_worker = _make_mgr()

        proposal = AssignmentProposal.escalation(
            worker_name="api",
            action="continue",
            assessment="Worker stuck on prompt",
        )
        store.add(proposal)
        worker = _make_worker("api")
        get_worker.return_value = worker

        await mgr.approve(proposal.id)
        # send_enter is NOT called from _approve_escalation for non-wait actions.
        # The analyzer.execute_escalation mock handles the action instead.
        assert "\n" not in worker.process.keys_sent

    @pytest.mark.asyncio
    async def test_wait_approval_sends_message_when_present(self):
        """When action='wait' has a message, send the message instead of bare Enter."""
        mgr, store, drone_log, get_worker = _make_mgr()

        proposal = AssignmentProposal.escalation(
            worker_name="api",
            action="wait",
            assessment="Safe find command",
            message="1",
        )
        store.add(proposal)
        worker = _make_worker("api")
        get_worker.return_value = worker

        await mgr.approve(proposal.id)
        # send_keys appends "\n" by default, so "1" becomes "1\n"
        assert "1\n" in worker.process.keys_sent

    @pytest.mark.asyncio
    async def test_wait_approval_falls_back_to_enter_without_message(self):
        """When action='wait' has no message, fall back to sending Enter."""
        mgr, store, drone_log, get_worker = _make_mgr()

        proposal = AssignmentProposal.escalation(
            worker_name="api",
            action="wait",
            assessment="Worker showing plan prompt",
        )
        store.add(proposal)
        worker = _make_worker("api")
        get_worker.return_value = worker

        await mgr.approve(proposal.id)
        # No message → just Enter
        assert "\n" in worker.process.keys_sent
        assert worker.process.keys_sent.count("\n") == 1


class TestLogCategories:
    """Issue 4: escalation/completion proposals log with QUEEN category."""

    @pytest.mark.asyncio
    async def test_approve_escalation_uses_queen_category(self):
        mgr, store, drone_log, get_worker = _make_mgr()

        proposal = AssignmentProposal.escalation(
            worker_name="api",
            action="wait",
            assessment="Plan prompt",
        )
        store.add(proposal)
        get_worker.return_value = _make_worker("api")

        await mgr.approve(proposal.id)

        # Find the APPROVED log entry
        approved = [e for e in drone_log.entries if e.action == SystemAction.APPROVED]
        assert len(approved) == 1
        assert approved[0].category == LogCategory.QUEEN

    @pytest.mark.asyncio
    async def test_approve_completion_uses_queen_category(self):
        mgr, store, drone_log, get_worker = _make_mgr()

        proposal = AssignmentProposal.completion(
            worker_name="api",
            task_id="t1",
            task_title="Fix bug",
            assessment="All done",
        )
        store.add(proposal)
        get_worker.return_value = _make_worker("api")

        await mgr.approve(proposal.id)

        approved = [e for e in drone_log.entries if e.action == SystemAction.APPROVED]
        assert len(approved) == 1
        assert approved[0].category == LogCategory.QUEEN

    @pytest.mark.asyncio
    async def test_approve_assignment_uses_drone_category(self):
        mgr, store, drone_log, get_worker = _make_mgr()

        proposal = AssignmentProposal.assignment(
            worker_name="api",
            task_id="t1",
            task_title="Build API",
            message="Do it",
        )
        store.add(proposal)
        worker = _make_worker("api")
        get_worker.return_value = worker

        await mgr.approve(proposal.id)

        approved = [e for e in drone_log.entries if e.action == SystemAction.APPROVED]
        assert len(approved) == 1
        assert approved[0].category == LogCategory.DRONE

    def test_reject_escalation_uses_queen_category(self):
        mgr, store, drone_log, get_worker = _make_mgr()

        proposal = AssignmentProposal.escalation(
            worker_name="api",
            action="send_message",
            assessment="Worker stuck",
        )
        store.add(proposal)

        mgr.reject(proposal.id)

        rejected = [e for e in drone_log.entries if e.action == SystemAction.REJECTED]
        assert len(rejected) == 1
        assert rejected[0].category == LogCategory.QUEEN

    def test_reject_assignment_uses_drone_category(self):
        mgr, store, drone_log, get_worker = _make_mgr()

        proposal = AssignmentProposal.assignment(
            worker_name="api",
            task_id="t1",
            task_title="Build API",
            message="Do it",
        )
        store.add(proposal)

        mgr.reject(proposal.id)

        rejected = [e for e in drone_log.entries if e.action == SystemAction.REJECTED]
        assert len(rejected) == 1
        assert rejected[0].category == LogCategory.DRONE

    def test_reject_all_uses_queen_category(self):
        mgr, store, drone_log, get_worker = _make_mgr()

        p1 = AssignmentProposal.escalation(worker_name="api", action="wait", assessment="Plan")
        p2 = AssignmentProposal.assignment(
            worker_name="web", task_id="t1", task_title="Bug", message="Fix"
        )
        store.add(p1)
        store.add(p2)

        count = mgr.reject_all()
        assert count == 2

        rejected = [e for e in drone_log.entries if e.action == SystemAction.REJECTED]
        assert len(rejected) == 1
        assert rejected[0].category == LogCategory.QUEEN


class TestFocusGate:
    """When the operator is viewing a worker, the Queen must not surface
    proposals for that worker — they get in the way of hands-on work."""

    def _mgr_with_focus(self, focused: set[str]):
        """Build a manager whose pilot mock reports the given workers as focused."""
        mgr, store, drone_log, get_worker = _make_mgr()
        pilot = MagicMock()
        pilot.is_focused = lambda name: name in focused
        mgr._get_pilot = MagicMock(return_value=pilot)
        return mgr, store, drone_log

    def test_skips_escalation_for_focused_worker(self):
        mgr, store, drone_log = self._mgr_with_focus({"api"})

        proposal = AssignmentProposal.escalation(
            worker_name="api", action="wait", assessment="Plan prompt"
        )
        mgr.on_proposal(proposal)

        assert store.pending == []
        skipped = [
            e for e in drone_log.entries if e.action == SystemAction.QUEEN_PROPOSAL_SKIPPED_FOCUSED
        ]
        assert len(skipped) == 1
        assert skipped[0].worker_name == "api"
        assert skipped[0].category == LogCategory.QUEEN

    def test_skips_completion_for_focused_worker(self):
        mgr, store, drone_log = self._mgr_with_focus({"api"})

        proposal = AssignmentProposal.completion(
            worker_name="api", task_id="t1", task_title="Fix bug", assessment="All done"
        )
        mgr.on_proposal(proposal)

        assert store.pending == []
        assert any(
            e.action == SystemAction.QUEEN_PROPOSAL_SKIPPED_FOCUSED for e in drone_log.entries
        )

    def test_skips_assignment_for_focused_worker(self):
        mgr, store, drone_log = self._mgr_with_focus({"api"})

        proposal = AssignmentProposal.assignment(
            worker_name="api", task_id="t1", task_title="Build API", message="Do it"
        )
        mgr.on_proposal(proposal)

        assert store.pending == []

    def test_proceeds_when_other_worker_is_focused(self):
        mgr, store, drone_log = self._mgr_with_focus({"web"})

        proposal = AssignmentProposal.escalation(
            worker_name="api", action="wait", assessment="Plan prompt"
        )
        mgr.on_proposal(proposal)

        assert len(store.pending) == 1
        assert store.pending[0].worker_name == "api"

    def test_proceeds_when_no_pilot(self):
        """No pilot wired (e.g. early startup) → proposals flow through normally."""
        mgr, store, _, _ = _make_mgr()
        mgr._get_pilot = MagicMock(return_value=None)

        proposal = AssignmentProposal.escalation(
            worker_name="api", action="wait", assessment="Plan prompt"
        )
        mgr.on_proposal(proposal)

        assert len(store.pending) == 1

    def test_proceeds_when_no_workers_focused(self):
        mgr, store, _ = self._mgr_with_focus(set())

        proposal = AssignmentProposal.escalation(
            worker_name="api", action="wait", assessment="Plan prompt"
        )
        mgr.on_proposal(proposal)

        assert len(store.pending) == 1


class TestApproveRejectPark:
    """Auto-park: PARK proposal approve → board.block_for_operator;
    reject → pilot.note_park_rejected (backoff)."""

    @pytest.mark.asyncio
    async def test_approve_park_blocks_task(self):
        mgr, store, drone_log, get_worker = _make_mgr()
        mgr._task_board.block_for_operator = MagicMock(return_value=True)
        p = AssignmentProposal.park(
            worker_name="api",
            task_id="t1",
            task_title="Renovate rollout",
            assessment="no progress 30m — operator-blocked",
        )
        store.add(p)
        get_worker.return_value = _make_worker("api")

        assert await mgr.approve(p.id) is True
        mgr._task_board.block_for_operator.assert_called_once()
        args = mgr._task_board.block_for_operator.call_args[0]
        assert args[0] == "t1"
        assert "operator-blocked" in args[1]

    @pytest.mark.asyncio
    async def test_approve_park_raises_when_no_longer_active(self):
        from swarm.server.daemon import TaskOperationError

        mgr, store, drone_log, get_worker = _make_mgr()
        mgr._task_board.block_for_operator = MagicMock(return_value=False)
        p = AssignmentProposal.park(worker_name="api", task_id="t1", task_title="T", assessment="x")
        store.add(p)
        get_worker.return_value = _make_worker("api")
        with pytest.raises(TaskOperationError):
            await mgr.approve(p.id)

    def test_reject_park_arms_backoff(self):
        mgr, store, drone_log, get_worker = _make_mgr()
        pilot = MagicMock()
        mgr._get_pilot = MagicMock(return_value=pilot)
        p = AssignmentProposal.park(worker_name="api", task_id="t1", task_title="T", assessment="x")
        store.add(p)

        assert mgr.reject(p.id, "not actually blocked") is True
        pilot.note_park_rejected.assert_called_once_with("api", "t1")
