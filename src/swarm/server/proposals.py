"""ProposalManager — handles Queen proposal lifecycle."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from swarm.drones.log import DroneAction, DroneLog, LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.notify.bus import NotificationBus
from swarm.tasks.board import TaskBoard
from swarm.tasks.proposal import (
    AssignmentProposal,
    ProposalStatus,
    ProposalStore,
    ProposalType,
    QueenAction,
)
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from swarm.drones.pilot import DronePilot
    from swarm.events import ProposalCallback

_log = get_logger("server.proposals")


class ProposalManager:
    """Manages Queen proposal lifecycle: creation, approval, rejection, expiry."""

    def __init__(
        self,
        store: ProposalStore,
        broadcast_ws: Callable[[dict[str, Any]], None],
        drone_log: DroneLog,
        notification_bus: NotificationBus,
        task_board: TaskBoard,
        get_worker: Callable[[str], Worker | None],
        get_workers: Callable[[], list[Worker]],
        get_pilot: Callable[[], DronePilot | None],
        assign_task: Callable[..., Awaitable[None]],
        complete_task: Callable[..., None],
        execute_escalation: Callable[[AssignmentProposal], Awaitable[bool]],
    ) -> None:
        self.store = store
        self._broadcast_ws = broadcast_ws
        self._drone_log = drone_log
        self._notification_bus = notification_bus
        self._task_board = task_board
        self._get_worker = get_worker
        self._get_workers = get_workers
        self._get_pilot = get_pilot
        self._assign_task = assign_task
        self._complete_task = complete_task
        self._execute_escalation = execute_escalation
        self._on_new_proposal: ProposalCallback | None = None

    @property
    def pending(self) -> list[AssignmentProposal]:
        return self.store.pending

    def on_proposal(self, proposal: AssignmentProposal) -> None:
        """Accept a new proposal: dedup, store, log, broadcast, notify.

        Drops the proposal when the operator is currently viewing the
        target worker in the dashboard — a focused worker means the
        operator is hands-on, and a Queen proposal modal would just
        get in the way.
        """
        if self.is_focused(proposal.worker_name):
            self._log_skipped_focused(proposal)
            return
        if self._is_duplicate(proposal):
            return
        self.store.add(proposal)
        if self._on_new_proposal:
            self._on_new_proposal(proposal)
        self._log_proposal(proposal)
        self._broadcast_proposal_created(proposal)
        self._notify_proposal(proposal)
        self._broadcast_modal(proposal)

    def is_focused(self, worker_name: str) -> bool:
        """True if the operator is currently viewing this worker.

        Public so the QueenAnalyzer can consult the same focus source
        *before* invoking the Queen, avoiding a wasted headless call on a
        proposal that on_proposal would only drop here.
        """
        pilot = self._get_pilot()
        return bool(pilot and pilot.is_focused(worker_name))

    def _log_skipped_focused(self, proposal: AssignmentProposal) -> None:
        """Log that a proposal was skipped because the operator is focused on the worker."""
        detail = (
            f"{proposal.proposal_type.value} skipped — operator focused: "
            f"{proposal.task_title or proposal.assessment or 'proposal'}"
        )
        self._drone_log.add(
            SystemAction.QUEEN_PROPOSAL_SKIPPED_FOCUSED,
            proposal.worker_name,
            detail,
            category=LogCategory.QUEEN,
        )
        _log.debug(
            "skipping %s proposal for %s — operator focused",
            proposal.proposal_type.value,
            proposal.worker_name,
        )

    def _is_duplicate(self, proposal: AssignmentProposal) -> bool:
        """Check if a matching pending proposal already exists."""
        pending = self.store.pending_for_worker(proposal.worker_name)
        for p in pending:
            if p.proposal_type != proposal.proposal_type:
                continue
            if proposal.task_id and p.task_id == proposal.task_id:
                _log.debug(
                    "dropping duplicate %s proposal for %s (task %s)",
                    proposal.proposal_type,
                    proposal.worker_name,
                    proposal.task_id,
                )
                return True
            if not proposal.task_id and proposal.proposal_type == ProposalType.ESCALATION:
                _log.debug(
                    "dropping duplicate escalation proposal for %s",
                    proposal.worker_name,
                )
                return True
        return False

    def _log_proposal(self, proposal: AssignmentProposal) -> None:
        """Log proposal to the drone system log."""
        action_map = {
            ProposalType.ESCALATION: (
                SystemAction.QUEEN_ESCALATION,
                proposal.assessment or proposal.reasoning or "escalation",
            ),
            ProposalType.COMPLETION: (
                SystemAction.QUEEN_COMPLETION,
                proposal.task_title or "completion",
            ),
        }
        action, detail = action_map.get(
            proposal.proposal_type,
            (SystemAction.QUEEN_PROPOSAL, proposal.task_title or proposal.assessment or "proposal"),
        )
        # Log to drone system log but do NOT mark as notification —
        # _broadcast_proposal_created() already sends a toast-triggering
        # WS event, and _notify_proposal() handles push notifications.
        # Setting is_notification=True here would cause 2 extra toasts
        # (system_log + notification) on top of the proposal_created toast.
        self._drone_log.add(
            action,
            proposal.worker_name,
            detail,
            category=LogCategory.QUEEN,
        )

    def _broadcast_proposal_created(self, proposal: AssignmentProposal) -> None:
        """Push proposal_created event to all WS clients."""
        self._broadcast_ws(
            {
                "type": "proposal_created",
                "proposal": self.proposal_dict(proposal),
                "pending_count": len(self.store.pending),
            }
        )

    def _notify_proposal(self, proposal: AssignmentProposal) -> None:
        """Emit a push notification only for proposals that are an actual
        operator-decision surface.

        ESCALATION proposals are the Queen explicitly asking for input:
        they raise a banner now and become a decision card in the
        exception queue — notifying is correct. ASSIGNMENT / COMPLETION
        proposals sit in the autonomous-approval window (the "handled"
        drawer) for ~180s and may be auto-resolved with no operator
        action; an interruptive ping on creation is the "notification
        with an empty Attention panel" bug. If such a proposal survives
        the window it becomes a decision card and the classifier-derived
        maybeNotifyAttention pings then — single source of truth.
        """
        if proposal.proposal_type == ProposalType.ESCALATION:
            self._notification_bus.emit_escalation(
                proposal.worker_name,
                f"Queen escalation: {proposal.assessment or proposal.task_title}",
            )

    def _broadcast_modal(self, proposal: AssignmentProposal) -> None:
        """Broadcast modal-triggering WS event for escalation/completion proposals."""
        if proposal.proposal_type == ProposalType.ESCALATION:
            self._broadcast_ws(
                {
                    "type": "queen_escalation",
                    "proposal_id": proposal.id,
                    "worker": proposal.worker_name,
                    "assessment": proposal.assessment,
                    "reasoning": proposal.reasoning,
                    "action": proposal.queen_action,
                    "message": proposal.message,
                    "confidence": proposal.confidence,
                    "prompt_snippet": proposal.prompt_snippet,
                    "rule_pattern": proposal.rule_pattern,
                    "is_plan": proposal.is_plan,
                }
            )
        elif proposal.proposal_type == ProposalType.COMPLETION:
            task = self._task_board.get(proposal.task_id)
            has_email = bool(task and task.source_email_id)
            self._broadcast_ws(
                {
                    "type": "queen_completion",
                    "proposal_id": proposal.id,
                    "worker": proposal.worker_name,
                    "task_id": proposal.task_id,
                    "task_title": proposal.task_title,
                    "assessment": proposal.assessment,
                    "reasoning": proposal.reasoning,
                    "confidence": proposal.confidence,
                    "has_source_email": has_email,
                }
            )

    def expire_stale(self) -> None:
        """Expire proposals where the task or worker is no longer valid."""
        # Snapshot collections before iterating — available_tasks already
        # returns a locked copy; list(workers) guards against mutations
        # during set comprehension if this ever moves to threaded code.
        available = self._task_board.available_tasks
        workers = list(self._get_workers())
        valid_task_ids = {t.id for t in available}
        valid_worker_names = {w.name for w in workers}
        expired = self.store.expire_stale(valid_task_ids, valid_worker_names)
        if expired:
            self._clear_and_broadcast()

    def proposal_dict(self, proposal: AssignmentProposal) -> dict[str, Any]:
        """Serialize a proposal for WebSocket / JSON responses."""
        result: dict[str, Any] = {
            "id": proposal.id,
            "worker_name": proposal.worker_name,
            "task_id": proposal.task_id,
            "task_title": proposal.task_title,
            "message": proposal.message,
            "reasoning": proposal.reasoning,
            "confidence": proposal.confidence,
            "proposal_type": proposal.proposal_type,
            "assessment": proposal.assessment,
            "queen_action": proposal.queen_action,
            "prompt_snippet": proposal.prompt_snippet,
            "rule_pattern": proposal.rule_pattern,
            "is_plan": proposal.is_plan,
            "status": proposal.status.value,
            "created_at": proposal.created_at,
            "age": round(proposal.age, 1),
        }
        if proposal.proposal_type == ProposalType.COMPLETION and proposal.task_id:
            task = self._task_board.get(proposal.task_id)
            result["has_source_email"] = bool(task and task.source_email_id)
        return result

    def broadcast(self) -> None:
        """Push current proposals to all WS clients."""
        pending = self.store.pending
        self._broadcast_ws(
            {
                "type": "proposals_changed",
                "proposals": [self.proposal_dict(p) for p in pending],
                "pending_count": len(pending),
            }
        )

    def _persist_status(self, proposal: AssignmentProposal) -> None:
        """Write a proposal's status change to the store (DB or JSON)."""
        if hasattr(self.store, "update_status"):
            self.store.update_status(proposal.id, proposal.status, proposal.rejection_reason)

    def _clear_and_broadcast(self) -> None:
        """Clear resolved proposals and broadcast updated list to WS clients."""
        self.store.clear_resolved()
        self.broadcast()

    async def approve(self, proposal_id: str) -> bool:
        """Approve a Queen proposal: assign task or execute escalation action."""
        from swarm.server.daemon import TaskOperationError, WorkerNotFoundError

        proposal = self.store.get(proposal_id)
        if not proposal or proposal.status != ProposalStatus.PENDING:
            raise TaskOperationError(f"Proposal '{proposal_id}' not found or not pending")

        worker = self._get_worker(proposal.worker_name)
        if not worker:
            proposal.status = ProposalStatus.EXPIRED
            self._persist_status(proposal)
            self._clear_and_broadcast()
            raise WorkerNotFoundError(f"Worker '{proposal.worker_name}' no longer exists")

        # Dispatch to type-specific handler
        handlers = {
            ProposalType.ESCALATION: self._approve_escalation,
            ProposalType.COMPLETION: self._approve_completion,
            ProposalType.PARK: self._approve_park,
        }
        handler = handlers.get(proposal.proposal_type, self._approve_assignment)
        log_detail = await handler(proposal, worker)

        proposal.status = ProposalStatus.APPROVED
        self._persist_status(proposal)
        # Clear escalation tracker so pilot can re-escalate if needed
        pilot = self._get_pilot()
        if pilot:
            pilot.clear_escalation(proposal.worker_name)
        cat = (
            LogCategory.QUEEN
            if proposal.proposal_type
            in (ProposalType.ESCALATION, ProposalType.COMPLETION, ProposalType.PARK)
            else LogCategory.DRONE
        )
        self._drone_log.add(DroneAction.APPROVED, proposal.worker_name, log_detail, category=cat)
        self._clear_and_broadcast()
        return True

    async def _approve_escalation(
        self,
        proposal: AssignmentProposal,
        worker: Worker,
        **_kwargs: object,
    ) -> str:
        """Execute an escalation proposal. Returns log detail string."""
        action = proposal.queen_action
        await self._execute_escalation(proposal)
        # "wait" is a no-op in execute_escalation.  If the operator approved it,
        # they want to proceed.  Prefer sending the Queen's message (e.g. "1"
        # for a numbered choice) over a bare Enter so numbered prompts work.
        proc = worker.process
        if action == QueenAction.WAIT and proc:
            if not proc.is_user_active:
                if proposal.message:
                    await proc.send_keys(proposal.message)
                else:
                    await proc.send_enter()
        return f"escalation approved: {action}"

    async def _approve_completion(
        self,
        proposal: AssignmentProposal,
        worker: Worker,
        **_kwargs: object,
    ) -> str:
        """Complete the task from a completion proposal. Returns log detail string."""
        resolution = proposal.assessment or proposal.reasoning or ""
        self._complete_task(proposal.task_id, actor="queen", resolution=resolution)
        return f"task completed: {proposal.task_title}"

    async def _approve_park(
        self,
        proposal: AssignmentProposal,
        worker: Worker,
        **_kwargs: object,
    ) -> str:
        """Park a stalled, operator-blocked task: ACTIVE → BLOCKED so the
        autonomous loops stand down. Returns log detail string."""
        from swarm.server.daemon import TaskOperationError

        reason = f"operator-blocked: {proposal.assessment or proposal.reasoning or 'stalled'}"
        if not self._task_board.block_for_operator(proposal.task_id, reason):
            # Stall resolved before approval (task left ACTIVE) — park moot.
            raise TaskOperationError(f"Cannot park '{proposal.task_title}' — task no longer ACTIVE")
        return f"task parked (operator-blocked): {proposal.task_title}"

    async def _approve_assignment(
        self,
        proposal: AssignmentProposal,
        worker: Worker,
        **_kwargs: object,
    ) -> str:
        """Assign a task from an assignment proposal. Returns log detail string."""
        from swarm.server.daemon import TaskOperationError

        if worker.state not in (WorkerState.RESTING, WorkerState.WAITING):
            proposal.status = ProposalStatus.EXPIRED
            self._clear_and_broadcast()
            raise TaskOperationError(
                f"Worker '{proposal.worker_name}' is {worker.state.value}, not idle"
            )

        await self._assign_task(
            proposal.task_id,
            proposal.worker_name,
            actor="queen",
            message=proposal.message or None,
        )
        return f"proposal approved: {proposal.task_title}"

    def reject(self, proposal_id: str, reason: str = "") -> bool:
        """Reject a Queen proposal, optionally capturing the operator's reason."""
        from swarm.server.daemon import TaskOperationError

        proposal = self.store.get(proposal_id)
        if not proposal or proposal.status != ProposalStatus.PENDING:
            raise TaskOperationError(f"Proposal '{proposal_id}' not found or not pending")
        proposal.status = ProposalStatus.REJECTED
        if reason:
            proposal.rejection_reason = reason
        self._persist_status(proposal)
        # Allow pilot to re-escalate/re-propose if the condition persists
        pilot = self._get_pilot()
        if pilot:
            pilot.clear_escalation(proposal.worker_name)
            if proposal.proposal_type == ProposalType.COMPLETION and proposal.task_id:
                pilot._task_lifecycle.clear_proposed_completion(proposal.task_id)
            if proposal.proposal_type == ProposalType.PARK and proposal.task_id:
                # Operator says "not operator-blocked" — back off so
                # oversight doesn't immediately re-propose the same park.
                pilot.note_park_rejected(proposal.worker_name, proposal.task_id)
        cat = (
            LogCategory.QUEEN
            if proposal.proposal_type
            in (ProposalType.ESCALATION, ProposalType.COMPLETION, ProposalType.PARK)
            else LogCategory.DRONE
        )
        detail = f"proposal rejected: {proposal.task_title}"
        if reason:
            detail += f" — reason: {reason}"
        self._drone_log.add(
            DroneAction.REJECTED,
            proposal.worker_name,
            detail,
            category=cat,
        )
        self._clear_and_broadcast()
        return True

    def reject_all(self) -> int:
        """Reject all pending proposals. Returns count rejected."""
        pilot = self._get_pilot()
        pending = self.store.pending
        for p in pending:
            p.status = ProposalStatus.REJECTED
            self._persist_status(p)
            # Allow pilot to re-escalate/re-propose if condition persists
            if pilot:
                pilot.clear_escalation(p.worker_name)
                if p.proposal_type == ProposalType.COMPLETION and p.task_id:
                    pilot._task_lifecycle.clear_proposed_completion(p.task_id)
        count = len(pending)
        if count:
            self._drone_log.add(
                DroneAction.REJECTED,
                "all",
                f"rejected {count} proposal(s)",
                category=LogCategory.QUEEN,
            )
            self._clear_and_broadcast()
        return count
