"""TestOperator — Queen as simulated operator for auto-resolving proposals."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from swarm.logging import get_logger
from swarm.server.daemon import TaskOperationError as _TaskOperationError
from swarm.testing.config import TestConfig
from swarm.testing.log import TestRunLog

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon
    from swarm.tasks.proposal import AssignmentProposal

_log = get_logger("testing.operator")


class TestOperator:
    """Auto-resolves proposals using the Queen for genuine approve/reject decisions.

    Proposals still appear in the dashboard for real-time observation.
    After ``auto_resolve_delay`` seconds, the Queen evaluates each proposal
    and calls the daemon's approve/reject codepath.
    """

    def __init__(
        self,
        daemon: SwarmDaemon,
        test_log: TestRunLog,
        config: TestConfig,
    ) -> None:
        self._daemon = daemon
        self._test_log = test_log
        self._config = config
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._start_time: float = 0.0
        self._test_task_ids: set[str] = set()

    def start(self) -> None:
        """Wire the proposal hook and start the resolve loop."""
        self._start_time = time.time()
        self._daemon.proposals._on_new_proposal = self._on_proposal
        # Track tasks created during the test run
        board = getattr(self._daemon, "task_board", None)
        if board:
            board.on("change", self._snapshot_task_ids)
            self._snapshot_task_ids()
        self._task = asyncio.create_task(self._resolve_loop())

    def _snapshot_task_ids(self) -> None:
        """Record task IDs that exist during the test run."""
        board = getattr(self._daemon, "task_board", None)
        if board:
            for t in board.all_tasks:
                if t.created_at >= self._start_time:
                    self._test_task_ids.add(t.id)

    def stop(self) -> None:
        """Stop the resolve loop and clean up test artifacts."""
        if self._task and not self._task.done():
            self._task.cancel()
        self._cleanup()

    def _cleanup(self) -> None:
        """Remove tasks and log entries created during the test run."""
        board = getattr(self._daemon, "task_board", None)
        if board and self._test_task_ids:
            removed = board.remove_tasks(self._test_task_ids)
            _log.info("test cleanup: removed %d test tasks", removed)

        drone_log = getattr(self._daemon, "drone_log", None)
        if drone_log and self._start_time:
            cleared = drone_log.clear_since(self._start_time)
            _log.info("test cleanup: cleared %d log entries", cleared)

    def _on_proposal(self, proposal: AssignmentProposal) -> None:
        """Called synchronously when a new proposal is created."""
        self._queue.put_nowait(proposal.id)

    async def _resolve_loop(self) -> None:
        """Consume proposals from the queue, wait, then resolve."""
        try:
            while True:
                proposal_id = await self._queue.get()
                await asyncio.sleep(self._config.auto_resolve_delay)
                await self._resolve(proposal_id)
        except asyncio.CancelledError:
            _log.debug("test operator resolve loop cancelled")
            raise

    async def _resolve(self, proposal_id: str) -> None:
        """Ask the Queen to evaluate the proposal and approve/reject it."""
        start = time.monotonic()
        proposal = self._daemon.proposal_store.get(proposal_id)
        if not proposal:
            _log.debug("proposal %s gone before resolution", proposal_id)
            return

        from swarm.tasks.proposal import ProposalStatus

        if proposal.status != ProposalStatus.PENDING:
            _log.debug("proposal %s already resolved (%s)", proposal_id, proposal.status.value)
            return

        # Use the Queen to make a genuine decision
        approved = True
        reasoning = ""
        confidence = 0.8

        queen = self._daemon.queen
        try:
            min_conf = float(queen.min_confidence)
        except (TypeError, ValueError, AttributeError):
            min_conf = 0.9

        # Fast-path: trust the original Queen confidence when it's high enough.
        # The analyzer already evaluated this proposal — re-evaluating wastes
        # a full Queen call (~18s) on decisions that are overwhelmingly correct.
        if proposal.confidence >= min_conf:
            approved = True
            reasoning = (
                f"auto-approved: original confidence {proposal.confidence:.0%} "
                f">= threshold {min_conf:.0%}"
            )
            confidence = proposal.confidence
            _log.info(
                "fast-path auto-approve for proposal %s (confidence=%.0f%%)",
                proposal_id,
                confidence * 100,
            )
        elif queen.enabled and queen.can_call:
            try:
                result = await self._queen_evaluate(proposal)
                approved = result.get("approved", True)
                reasoning = result.get("reasoning", "")
                confidence = float(result.get("confidence", 0.8))
            except (TimeoutError, RuntimeError):
                _log.warning(
                    "Queen evaluation failed for proposal %s — auto-approving", proposal_id
                )
                approved = True
                reasoning = "Queen unavailable — auto-approved"
        else:
            reasoning = "Queen disabled — auto-approved"

        latency_ms = (time.monotonic() - start) * 1000

        try:
            if approved:
                await self._daemon.proposals.approve(proposal_id)
            else:
                self._daemon.proposals.reject(proposal_id)
        except _TaskOperationError:
            # Race condition: proposal expired or resolved between our check and action
            _log.debug("proposal %s already resolved (race)", proposal_id)
            return
        except Exception:
            _log.warning("failed to resolve proposal %s", proposal_id, exc_info=True)
            return

        self._test_log.record_operator_decision(
            proposal_id=proposal_id,
            proposal_type=proposal.proposal_type,
            worker_name=proposal.worker_name,
            approved=approved,
            reasoning=reasoning,
            confidence=confidence,
            latency_ms=latency_ms,
        )

        _log.info(
            "test operator %s proposal %s for %s: %s",
            "approved" if approved else "rejected",
            proposal_id,
            proposal.worker_name,
            reasoning,
        )

    async def _queen_evaluate(self, proposal: AssignmentProposal) -> dict[str, Any]:
        """Ask the Queen to evaluate whether a proposal should be approved."""
        queen = self._daemon.queen

        prompt = (
            "You are evaluating a Queen proposal in test mode. "
            "Decide whether to APPROVE or REJECT this proposal.\n\n"
            f"Proposal type: {proposal.proposal_type}\n"
            f"Worker: {proposal.worker_name}\n"
            f"Task: {proposal.task_title}\n"
            f"Message: {proposal.message}\n"
            f"Reasoning: {proposal.reasoning}\n"
            f"Assessment: {proposal.assessment}\n"
            f"Queen action: {proposal.queen_action}\n"
            f"Confidence: {proposal.confidence}\n\n"
            "## Evaluation Guidelines\n"
            "- REJECT escalations for workers idle < 120 seconds — short pauses are normal.\n"
            "- APPROVE 'complete_task' only with concrete evidence "
            "(commit pushed, tests passing, explicit 'done').\n"
            "- APPROVE 'continue' for standard tool permissions and choice prompts.\n"
            "- When in doubt, REJECT — premature action is worse than a short delay.\n\n"
            "Respond with JSON: "
            '{"approved": true/false, "reasoning": "...", "confidence": 0.0-1.0}'
        )

        result = await queen.ask(prompt, stateless=True, force=True)
        if "error" in result:
            return {
                "approved": True,
                "reasoning": f"Queen error: {result['error']}",
                "confidence": 0.5,
            }
        return {
            "approved": result.get("approved", True),
            "reasoning": result.get("reasoning", ""),
            "confidence": float(result.get("confidence", 0.8)),
        }
