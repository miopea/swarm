"""DirectiveExecutor — handles Queen directive dispatch and action execution."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, ClassVar

from swarm.drones.log import DroneAction, LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.tasks.proposal import QueenAction
from swarm.tasks.task import TaskStatus
from swarm.worker.manager import revive_worker
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Any

    from swarm.drones.log import DroneLog
    from swarm.providers import LLMProvider
    from swarm.pty.provider import WorkerProcessProvider
    from swarm.queen.queen import Queen
    from swarm.tasks.board import TaskBoard

_log = get_logger("drones.directives")

# classify_worker_output examines <=30 lines; 35 gives margin for context.
_STATE_DETECT_LINES = 35

# Matches a prompt line with operator-typed text: "> /verify", "❯ fix the bug"
# (Also defined in pilot.py; kept module-local here to avoid a pilot↔directives
# circular import.)
_RE_PROMPT_WITH_TEXT = re.compile(r"^[>❯]\s+\S")


class DirectiveExecutor:
    """Handles Queen directive dispatch and action execution.

    Extracted from :class:`~swarm.drones.pilot.DronePilot` to reduce
    pilot.py complexity.  Owns the ``_ACTION_HANDLERS`` dispatch table
    and all individual ``_handle_*`` methods.
    """

    def __init__(
        self,
        workers: list[Worker],
        log: DroneLog,
        pool: WorkerProcessProvider | None,
        queen: Queen | None,
        task_board: TaskBoard | None,
        emit: Callable[..., None],
        classify_worker_state: Callable[..., tuple[WorkerState, list[Any]]],
        get_provider: Callable[[Worker], LLMProvider],
        safe_worker_action: Callable[..., Awaitable[bool]],
        pending_proposals_check: Callable[[], bool] | None,
        proposed_completions: dict[str, float],
    ) -> None:
        self.workers = workers
        self.log = log
        self.pool = pool
        self.queen = queen
        self.task_board = task_board
        self._emit = emit
        self._classify_worker_state = classify_worker_state
        self._get_provider = get_provider
        self._safe_worker_action = safe_worker_action
        self._pending_proposals_check = pending_proposals_check
        self._proposed_completions = proposed_completions

    # --- Static helpers ---

    @staticmethod
    def has_operator_text_at_prompt(worker: Worker) -> bool:
        """Check if worker has operator-typed text at a prompt."""
        if not worker.process:
            return False
        tail = worker.process.get_content(3)
        if not tail:
            return False
        for line in reversed(tail.strip().splitlines()):
            stripped = line.strip()
            if stripped:
                if _RE_PROMPT_WITH_TEXT.match(stripped):
                    return True
                break
        return False

    @staticmethod
    def has_pending_bash_approval(worker: Worker) -> bool:
        """Check if a worker's terminal shows a bash/command approval prompt."""
        if not worker.process:
            return False
        tail = worker.process.get_content(10).lower()
        if "bash" in tail and "accept edits" in tail:
            return True
        if "bash(" in tail and ("allow" in tail or "deny" in tail or ">>>" in tail):
            return True
        return False

    @staticmethod
    def has_idle_prompt(worker: Worker) -> bool:
        """Check if worker's terminal shows an idle/suggested prompt."""
        if not worker.process:
            return False
        tail = worker.process.get_content(5).lower()
        return "? for shortcuts" in tail or "ctrl+t to hide" in tail

    # --- Directive action handlers ---

    async def _handle_send_message(self, directive: dict[str, Any], worker: Worker) -> bool:
        """Handle Queen 'send_message' directive via proposal system."""
        message = directive.get("message", "")
        if not message:
            return False
        if self._pending_proposals_check and self._pending_proposals_check():
            _log.info("Ignoring send_message for %s: pending proposals exist", worker.name)
            return False
        from swarm.tasks.proposal import AssignmentProposal

        reason = directive.get("reason", "")
        proposal = AssignmentProposal.escalation(
            worker_name=worker.name,
            action=QueenAction.SEND_MESSAGE,
            assessment=reason,
            message=message,
            reasoning=reason,
        )
        self._emit("proposal", proposal)
        self.log.add(DroneAction.PROPOSED_MESSAGE, worker.name, f"Queen proposes message: {reason}")
        return True

    async def _handle_continue(self, directive: dict[str, Any], worker: Worker) -> bool:
        """Handle Queen 'continue' directive — send Enter to worker."""
        proc = worker.process
        if not proc:
            return False
        if proc.is_user_active:
            _log.info(
                "skipping Queen continue for %s: user active in terminal",
                worker.name,
            )
            return False
        if worker.state != WorkerState.BUZZING:
            _log.info(
                "blocking Queen continue for %s: worker is %s",
                worker.name,
                worker.state.value,
            )
            self.log.add(
                SystemAction.QUEEN_BLOCKED,
                worker.name,
                f"Queen continue blocked — worker is {worker.state.value}",
                category=LogCategory.QUEEN,
            )
            return False
        # Fresh state check: re-read PTY and re-classify.
        from swarm.providers.styled import StyledContent as _StyledContent

        content, styled_rows = proc.get_styled_content(_STATE_DETECT_LINES)
        cmd = proc.get_child_foreground_command()
        styled = _StyledContent(text=content, rows=styled_rows)
        fresh_state, _events = self._classify_worker_state(worker, cmd, content, styled=styled)
        if fresh_state != WorkerState.BUZZING:
            _log.info(
                "blocking Queen continue for %s: fresh state %s (cached %s)",
                worker.name,
                fresh_state.value,
                worker.state.value,
            )
            self.log.add(
                SystemAction.QUEEN_BLOCKED,
                worker.name,
                f"Queen continue blocked — fresh state {fresh_state.value}"
                f" (cached {worker.state.value})",
                category=LogCategory.QUEEN,
            )
            return False
        if self.has_operator_text_at_prompt(worker):
            _log.info(
                "blocking Queen continue for %s: operator text at prompt",
                worker.name,
            )
            self.log.add(
                SystemAction.QUEEN_BLOCKED,
                worker.name,
                "Queen continue blocked — operator text at prompt",
                category=LogCategory.QUEEN,
            )
            return False
        if self.has_pending_bash_approval(worker):
            _log.info(
                "blocking Queen continue for %s: bash approval requires operator",
                worker.name,
            )
            self.log.add(
                SystemAction.QUEEN_BLOCKED,
                worker.name,
                "Queen continue blocked — bash approval requires operator",
                category=LogCategory.QUEEN,
            )
            return False
        reason = directive.get("reason", "")
        conf = directive.get("_confidence", 0.0)
        provider = self._get_provider(worker)
        return await self._safe_worker_action(
            worker,
            worker.process.send_keys(provider.approval_response(True), enter=False),
            DroneAction.QUEEN_CONTINUED,
            reason=f"Queen ({conf:.0%}): {reason}",
        )

    async def _handle_restart(self, directive: dict[str, Any], worker: Worker) -> bool:
        """Handle Queen 'restart' directive — revive the worker process."""
        if not self.pool:
            return False
        # Only restart workers that have actually exited (STUNG).
        # Restarting a BUZZING/RESTING worker would kill it mid-work.
        if worker.state != WorkerState.STUNG:
            _log.info(
                "blocking Queen restart for %s: worker is %s (must be STUNG)",
                worker.name,
                worker.state.value,
            )
            self.log.add(
                SystemAction.QUEEN_BLOCKED,
                worker.name,
                f"Queen restart blocked — worker is {worker.state.value}, not STUNG",
                category=LogCategory.QUEEN,
            )
            return False
        reason = directive.get("reason", "")
        conf = directive.get("_confidence", 0.0)
        return await self._safe_worker_action(
            worker,
            revive_worker(worker, self.pool),
            DroneAction.REVIVED,
            reason=f"Queen ({conf:.0%}): {reason}",
        )

    async def _handle_complete_task(self, directive: dict[str, Any], worker: Worker) -> bool:
        """Handle Queen 'complete_task' directive — propose task completion."""
        task_id = directive.get("task_id", "")
        reason = directive.get("reason", "")
        resolution = directive.get("resolution", reason)
        if worker.state != WorkerState.RESTING:
            _log.info(
                "Ignoring complete_task for %s: worker %s is %s, not RESTING",
                task_id,
                worker.name,
                worker.state.value,
            )
            return False
        task = self.task_board.get(task_id) if task_id and self.task_board else None
        if not task or task.status not in (TaskStatus.ASSIGNED, TaskStatus.ACTIVE):
            return False
        if task_id in self._proposed_completions:
            return False
        self._proposed_completions[task_id] = time.time()
        self._emit("task_done", worker, task, resolution)
        self.log.add(DroneAction.QUEEN_PROPOSED_DONE, worker.name, f"Queen proposes done: {reason}")
        _log.info("Queen proposes task %s done for %s", task_id, worker.name)
        return True

    async def _handle_assign_task(self, directive: dict[str, Any], worker: Worker) -> bool:
        """Handle Queen 'assign_task' directive — propose task assignment."""
        task_id = directive.get("task_id", "")
        message = directive.get("message", "")
        reason = directive.get("reason", "")
        if not task_id or not self.task_board or not message:
            return False
        task = self.task_board.get(task_id)
        if not task or not task.is_available:
            _log.info("Ignoring assign_task for %s: task %s not available", worker.name, task_id)
            return False
        assigned_or_active_tasks = self.task_board.assigned_or_active_tasks_for_worker(worker.name)
        if assigned_or_active_tasks:
            _log.info(
                "Ignoring assign_task for %s: worker already has %d active task(s)",
                worker.name,
                len(assigned_or_active_tasks),
            )
            return False
        if self._pending_proposals_check and self._pending_proposals_check():
            _log.info("Ignoring assign_task for %s: pending proposals exist", worker.name)
            return False
        from swarm.tasks.proposal import AssignmentProposal

        proposal = AssignmentProposal.assignment(
            worker_name=worker.name,
            task_id=task_id,
            task_title=task.title,
            message=message,
            reasoning=reason,
        )
        self._emit("proposal", proposal)
        self.log.add(DroneAction.PROPOSED_ASSIGNMENT, worker.name, f"Queen proposed: {task.title}")
        return True

    async def _handle_wait(self, _directive: dict[str, object], _worker: Worker) -> bool:
        """No-op: Queen says to wait and observe."""
        return False

    _ACTION_HANDLERS: ClassVar[dict[str, str]] = {
        QueenAction.SEND_MESSAGE: "_handle_send_message",
        QueenAction.CONTINUE: "_handle_continue",
        QueenAction.RESTART: "_handle_restart",
        QueenAction.COMPLETE_TASK: "_handle_complete_task",
        QueenAction.ASSIGN_TASK: "_handle_assign_task",
        QueenAction.WAIT: "_handle_wait",
    }

    # Actions that execute immediately (no proposal) and therefore
    # require meeting the Queen's min_confidence threshold.
    _AUTO_EXEC_ACTIONS: ClassVar[set[str]] = {QueenAction.CONTINUE, QueenAction.RESTART}

    async def execute_directives(
        self, directives: list[dict[str, Any]], confidence: float = 0.0
    ) -> bool:
        """Dispatch a list of Queen directives to the appropriate handlers."""
        min_conf = getattr(self.queen, "min_confidence", 0.7) if self.queen else 0.7
        had_directive = False
        # Build name→worker index once — avoids O(directives × workers) linear scan.
        workers_by_name = {w.name: w for w in self.workers}
        for directive in directives:
            if not isinstance(directive, dict):
                _log.warning("Queen returned non-dict directive entry: %s", type(directive))
                continue
            worker_name = directive.get("worker", "")
            action = directive.get("action", "")
            reason = directive.get("reason", "")

            worker = workers_by_name.get(worker_name)
            if not worker:
                continue

            directive["_confidence"] = confidence

            if action in self._AUTO_EXEC_ACTIONS and confidence < min_conf:
                _log.info(
                    "Queen directive %s → %s BLOCKED: confidence %.0f%% < %.0f%% threshold",
                    worker_name,
                    action,
                    confidence * 100,
                    min_conf * 100,
                )
                self.log.add(
                    SystemAction.QUEEN_BLOCKED,
                    worker_name,
                    f"Queen {action} BLOCKED (conf={confidence:.0%} < {min_conf:.0%}) — {reason}",
                    category=LogCategory.QUEEN,
                )
                continue

            _log.info(
                "Queen directive: %s → %s (conf=%.0f%%, %s)",
                worker_name,
                action,
                confidence * 100,
                reason,
            )

            handler_name = self._ACTION_HANDLERS.get(action)
            if handler_name:
                handler = getattr(self, handler_name)
                if await handler(directive, worker):
                    had_directive = True
            else:
                _log.warning("Unknown Queen directive action: %r for %s", action, worker_name)
        return had_directive
