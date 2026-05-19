"""ProposalCoordinator — task-done handling, assignment delivery, and proposal lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from swarm.logging import get_logger
from swarm.pty.process import ProcessError
from swarm.server.task_utils import log_task_exception as _log_task_exception
from swarm.tasks.proposal import AssignmentProposal
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from swarm.notify.bus import NotificationBus
    from swarm.queen.queen import Queen
    from swarm.server.analyzer import QueenAnalyzer
    from swarm.server.proposals import ProposalManager
    from swarm.tasks.proposal import ProposalStore
    from swarm.tasks.task import SwarmTask

_log = get_logger("server.proposal_coordinator")


class ProposalCoordinator:
    """Owns task-done handling, auto-assignment delivery, and proposal lifecycle.

    Extracted from SwarmDaemon to satisfy single-responsibility principle.
    All business logic is identical to the original daemon methods.
    """

    def __init__(
        self,
        *,
        proposals: ProposalManager,
        proposal_store: ProposalStore,
        get_analyzer: Callable[[], QueenAnalyzer],
        get_queen: Callable[[], Queen],
        broadcast_ws: Callable[[dict[str, Any]], None],
        notification_bus: NotificationBus,
        get_pilot: Callable[[], Any],
        assign_task: Callable[..., Awaitable[None]],
        track_task: Callable[[asyncio.Task[object]], None],
        emit: Callable[..., None],
    ) -> None:
        self._proposals = proposals
        self._proposal_store = proposal_store
        self._get_analyzer = get_analyzer
        self._get_queen = get_queen
        self._broadcast_ws = broadcast_ws
        self._notification_bus = notification_bus
        self._get_pilot = get_pilot
        self._assign_task = assign_task
        self._track_task = track_task
        self._emit = emit

    # --- Task done / assignment ---

    def on_task_done(self, worker: Worker, task: SwarmTask, resolution: str = "") -> None:
        """Handle a task that appears complete — create a proposal for user approval."""
        # Guard: worker must still be idle — if it resumed working, skip
        if worker.state == WorkerState.BUZZING:
            _log.info(
                "Ignoring task_done for '%s': worker %s is BUZZING",
                task.title,
                worker.name,
            )
            return

        # Skip if already pending
        if self._proposal_store.has_pending_completion(worker.name, task.id):
            return

        queen = self._get_queen()
        if resolution:
            # Queen coordination already provided a resolution — create proposal directly
            proposal = AssignmentProposal.completion(
                worker_name=worker.name,
                task_id=task.id,
                task_title=task.title,
                assessment=resolution,
                reasoning=f"Worker {worker.name} idle for {worker.state_duration:.0f}s",
            )
            self.queue_proposal(proposal)
        elif queen.enabled and queen.can_call:
            key = f"{worker.name}:{task.id}"
            analyzer = self._get_analyzer()
            if analyzer.has_inflight_completion(key):
                _log.debug("skipping completion analysis for %s — already in flight", key)
                return
            analyzer.start_completion(worker, task)
        else:
            # Queen unavailable — skip proposal (no way to assess completion)
            _log.info(
                "Queen unavailable — cannot assess completion for task '%s' on %s",
                task.title,
                worker.name,
            )

    def on_park_proposal(self, worker: Worker, task: SwarmTask, reason: str = "") -> None:
        """Oversight detected an ACTIVE task stalled with no progress
        (operator-blocked pattern) — raise ONE park proposal for the
        operator. Mirrors on_task_done: skip if the worker resumed
        (BUZZING = real progress, not parked) or a park is already
        pending (dedupe — the freeze that stops the churn while pending).
        """
        if worker.state == WorkerState.BUZZING:
            _log.info(
                "Ignoring park_proposal for '%s': worker %s is BUZZING (resumed)",
                task.title,
                worker.name,
            )
            return
        if self._proposal_store.has_pending_park(worker.name, task.id):
            return
        proposal = AssignmentProposal.park(
            worker_name=worker.name,
            task_id=task.id,
            task_title=task.title,
            assessment=reason or "stalled on an ACTIVE task with no progress",
            reasoning=f"No task progress across repeated oversight drift checks "
            f"while {worker.name} idled — looks blocked on the operator.",
        )
        self.queue_proposal(proposal)

    def on_task_assigned(self, worker: Worker, task: SwarmTask, message: str = "") -> None:
        """Handle pilot auto-approved task assignment."""
        # When the pilot auto-approved, actually assign & send the message
        if task.is_available:
            try:
                asyncio.get_running_loop()
                task_ = asyncio.create_task(self._deliver_auto_assignment(worker, task, message))
                task_.add_done_callback(_log_task_exception)
                self._track_task(task_)
            except RuntimeError:
                pass  # No event loop (sync test context)
        self._notification_bus.emit_task_assigned(worker.name, task.title)
        self._broadcast_ws(
            {
                "type": "task_assigned",
                "worker": worker.name,
                "task": {"id": task.id, "title": task.title},
            }
        )
        self._emit("task_assigned", worker, task)

    async def _deliver_auto_assignment(self, worker: Worker, task: SwarmTask, message: str) -> None:
        """Deliver an auto-approved task assignment via the standard assign_task path."""
        from swarm.server.daemon import SwarmOperationError

        try:
            await self._assign_task(task.id, worker.name, actor="queen", message=message)
        except (SwarmOperationError, ProcessError, OSError):
            _log.warning(
                "auto-assign delivery failed: %s → %s",
                worker.name,
                task.title,
                exc_info=True,
            )

    # --- Proposal lifecycle ---

    def queue_proposal(self, proposal: AssignmentProposal) -> None:
        """Accept a new Queen proposal for user review."""
        self._proposals.on_proposal(proposal)

    def expire_stale(self) -> None:
        """Expire stale proposals."""
        self._proposals.expire_stale()

    def proposal_dict(self, proposal: AssignmentProposal) -> dict[str, Any]:
        """Serialize a proposal to a dict for API/WebSocket responses."""
        return self._proposals.proposal_dict(proposal)

    def broadcast(self) -> None:
        """Broadcast proposals to WS clients."""
        self._proposals.broadcast()

    async def approve(self, proposal_id: str) -> bool:
        """Approve a Queen proposal."""
        return await self._proposals.approve(proposal_id)

    def reject(self, proposal_id: str, reason: str = "") -> bool:
        """Reject a Queen proposal."""
        return self._proposals.reject(proposal_id, reason=reason)

    def reject_all(self) -> int:
        """Reject all pending proposals."""
        return self._proposals.reject_all()

    async def approve_all(self) -> int:
        """Approve all pending proposals. Returns count approved."""
        pending = list(self._proposal_store.pending)
        count = 0
        for p in pending:
            try:
                await self._proposals.approve(p.id)
                count += 1
            except Exception:
                _log.debug("skipping proposal %s during approve-all", p.id, exc_info=True)
        return count
