"""QueenAnalyzer — handles Queen escalation/completion analysis and hive coordination."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from swarm.drones.log import DroneLog, LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.pty.process import ProcessError
from swarm.queen.queue import QueenCallQueue, QueenCallRequest
from swarm.tasks.board import TaskBoard
from swarm.tasks.proposal import (
    AssignmentProposal,
    ProposalStatus,
    ProposalStore,
    QueenAction,
    build_worker_task_info,
)
from swarm.worker.worker import Worker, WorkerState, format_duration

if TYPE_CHECKING:
    from swarm.config import HiveConfig
    from swarm.events import ProposalCallback
    from swarm.pty.provider import WorkerProcessProvider
    from swarm.queen.queen import Queen
    from swarm.tasks.task import SwarmTask

_log = get_logger("server.analyzer")

_IDLE_ESCALATION_THRESHOLD = 60  # seconds
_LOG_DETAIL_MAX_LEN = 120


def _build_criteria_section(criteria: list[str]) -> str:
    """Build the acceptance-criteria prompt section for completion analysis."""
    if not criteria:
        return ""
    numbered = "\n".join(f"  {i}. {c}" for i, c in enumerate(criteria, 1))
    return (
        f"\n  Acceptance Criteria:\n{numbered}\n\n"
        "IMPORTANT: Evaluate EACH acceptance criterion individually. "
        "For each criterion, cite specific evidence from the output that "
        "it was met, or state that no evidence was found. "
        "Set done=false if ANY criterion lacks evidence of completion.\n"
        'Include a "criteria_met" field in your JSON: a list of objects '
        'with "criterion" (the text) and "met" (true/false) and '
        '"evidence" (what you found or "no evidence").\n'
    )


class QueenAnalyzer:
    """Manages Queen analysis: escalations, completions, and hive coordination."""

    def __init__(
        self,
        queen: Queen,
        queue: QueenCallQueue,
        broadcast_ws: Callable[[dict[str, Any]], None],
        drone_log: DroneLog,
        emit_event: Callable[..., None],
        proposal_store: ProposalStore,
        queue_proposal: ProposalCallback,
        task_board: TaskBoard,
        get_worker: Callable[[str], Worker | None],
        require_worker: Callable[[str], Worker],
        get_workers: Callable[[], list[Worker]],
        get_pool: Callable[[], WorkerProcessProvider | None],
        get_config: Callable[[], HiveConfig],
        get_worker_descriptions: Callable[[], dict[str, str]],
        clear_escalation: Callable[[str], None],
        record_completion_verdict: Callable[[str, bool, float], None] | None = None,
    ) -> None:
        self.queen = queen
        self._queue = queue
        self._broadcast_ws = broadcast_ws
        self._drone_log = drone_log
        self._emit_event = emit_event
        self._proposal_store = proposal_store
        self._queue_proposal = queue_proposal
        self._task_board = task_board
        self._get_worker = get_worker
        self._require_worker = require_worker
        self._get_workers = get_workers
        self._get_pool = get_pool
        self._get_config = get_config
        self._get_worker_descriptions = get_worker_descriptions
        self._clear_escalation = clear_escalation
        self._record_completion_verdict = record_completion_verdict
        self._background_tasks: set[asyncio.Task[None]] = set()

    def has_inflight_escalation(self, worker_name: str) -> bool:
        """Check if there's an in-flight Queen escalation for this worker."""
        return self._queue.has_pending(f"escalation:{worker_name}")

    def has_inflight_completion(self, key: str) -> bool:
        """Check if there's an in-flight Queen completion for this key."""
        return self._queue.has_pending(f"completion:{key}")

    def start_escalation(self, worker: Worker, reason: str) -> None:
        """Submit an escalation analysis to the queen call queue.

        Safe to call without an event loop (no-ops in that case).
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return

        req = QueenCallRequest(
            call_type="escalation",
            coro_factory=lambda w=worker, r=reason: self.analyze_escalation(w, r),
            worker_name=worker.name,
            worker_state_at_enqueue=worker.state.value,
            dedup_key=f"escalation:{worker.name}",
            force=False,
        )
        task = asyncio.create_task(self._queue.submit(req))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def start_completion(self, worker: Worker, task: SwarmTask) -> None:
        """Submit a completion analysis to the queen call queue.

        Safe to call without an event loop (no-ops in that case).
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return

        key = f"{worker.name}:{task.id}"
        req = QueenCallRequest(
            call_type="completion",
            coro_factory=lambda w=worker, t=task: self.analyze_completion(w, t),
            worker_name=worker.name,
            worker_state_at_enqueue=worker.state.value,
            dedup_key=f"completion:{key}",
            force=False,
        )
        bg_task = asyncio.create_task(self._queue.submit(req))
        self._background_tasks.add(bg_task)
        bg_task.add_done_callback(self._background_tasks.discard)

    def clear_worker_inflight(self, worker_name: str) -> None:
        """Clear all queued calls for a worker (when it resumes BUZZING)."""
        self._queue.clear_worker(worker_name)

    async def analyze_escalation(self, worker: Worker, reason: str) -> None:
        """Ask Queen to analyze an escalated worker and act or propose.

        High-confidence actions are executed immediately. Low-confidence
        actions (and plans) are surfaced to the user as proposals.
        """
        _start = time.time()
        try:
            content = worker.process.get_content()
            hive_ctx = await self.gather_context()
            result = await self.queen.analyze_worker(
                worker.name,
                content,
                hive_context=hive_ctx,
                idle_duration_seconds=worker.resting_duration,
                worker_state=worker.state.value,
            )
        except asyncio.CancelledError:
            _log.info("Queen escalation analysis cancelled for %s", worker.name)
            return
        except (ProcessError, OSError):
            _log.warning(
                "Queen escalation analysis failed for %s",
                worker.name,
                exc_info=True,
            )
            return

        if not isinstance(result, dict):
            return

        # Queen timeout/error — clear escalation so the pilot can retry
        if "error" in result:
            _log.warning("Queen escalation error for %s: %s", worker.name, result["error"])
            self._clear_escalation(worker.name)
            return

        action = result.get("action", "wait")
        confidence = float(result.get("confidence", 0.8))

        # Hard guardrail: clamp confidence for short-idle "wait" actions.
        # The Queen prompt mandates <0.50 for <60s idle, but LLMs occasionally
        # ignore this.  Enforce in code to prevent premature escalations.
        # Skip for WAITING workers — they're blocked on a prompt, not between steps.
        idle_s = worker.resting_duration
        is_waiting = worker.state == WorkerState.WAITING
        if (
            not is_waiting
            and idle_s < _IDLE_ESCALATION_THRESHOLD
            and confidence >= 0.50
            and action == QueenAction.WAIT
        ):
            _log.info(
                "clamping Queen confidence %.2f -> 0.47 for %s (idle %.0fs < 60s)",
                confidence,
                worker.name,
                idle_s,
            )
            confidence = 0.47

        reason_lower = reason.lower()
        # User questions and plans always require user approval — the Queen
        # must never auto-act on these.  Match exact drone reason strings
        # (from _decide_idle_state and _decide_choice in rules.py) to avoid
        # false positives when the word "plan" appears in other contexts.
        requires_user = reason_lower == "plan requires user approval" or reason_lower.startswith(
            "user question"
        )

        assessment = result.get("assessment", "")
        reasoning = result.get("reasoning", "")
        message = result.get("message", "")

        self._drone_log.add(
            SystemAction.QUEEN_ESCALATION,
            worker.name,
            f"analyzed: {action} (conf={confidence:.0%})",
            category=LogCategory.QUEEN,
            metadata={
                "queen_action": action,
                "confidence": confidence,
                "assessment": (assessment or reasoning)[:200],
                "duration_s": round(time.time() - _start, 1),
            },
        )
        self._emit_event(
            "queen_analysis",
            worker.name,
            action,
            assessment or reasoning,
            confidence,
        )

        # Reject proposals with no actionable content — useless to the user
        if not assessment and not reasoning and not message:
            _log.info(
                "Queen returned empty escalation analysis for %s — dropping",
                worker.name,
            )
            return

        # Drop "no action needed" for actively working workers — these just
        # clutter the proposal queue with noise the operator must dismiss.
        if action == QueenAction.WAIT and worker.state == WorkerState.BUZZING:
            _log.info(
                "Queen says wait for BUZZING %s — suppressing proposal",
                worker.name,
            )
            return

        from swarm.drones.pilot import extract_prompt_snippet
        from swarm.drones.state_tracker import WorkerStateTracker
        from swarm.providers import get_provider

        snippet = extract_prompt_snippet(content)
        provider = get_provider(worker.provider_name)
        rule_pattern = WorkerStateTracker._suggest_approval_pattern(content, provider)

        is_plan = reason_lower == "plan requires user approval"

        proposal = AssignmentProposal.escalation(
            worker_name=worker.name,
            action=action,
            assessment=assessment or reasoning or f"Escalation: {reason}",
            message=message,
            reasoning=reasoning or assessment,
            confidence=confidence,
            prompt_snippet=snippet,
            rule_pattern=rule_pattern,
            is_plan=is_plan,
        )

        # Race guard: another escalation may have created a proposal while Queen was thinking
        if self._proposal_store.has_pending_escalation(worker.name):
            _log.debug("dropping duplicate Queen proposal for %s", worker.name)
            return

        # Only auto-execute for routine escalations (unrecognized state, etc.)
        # where the Queen is confident. User-facing decisions always go to the user.
        # Never auto-execute send_message — injecting arbitrary text into a worker
        # terminal is too dangerous without human review.
        safe_auto_actions = (QueenAction.CONTINUE, QueenAction.RESTART)
        if (
            not requires_user
            and confidence >= self.queen.min_confidence
            and action in safe_auto_actions
        ):
            _log.info(
                "Queen auto-acting on %s: %s (confidence=%.0f%%)",
                worker.name,
                action,
                confidence * 100,
            )
            await self.execute_escalation(proposal)
            proposal.status = ProposalStatus.APPROVED
            self._proposal_store.add_to_history(proposal)
            self._drone_log.add(
                SystemAction.QUEEN_AUTO_ACTED,
                worker.name,
                f"{action} ({confidence * 100:.0f}%): {assessment[:_LOG_DETAIL_MAX_LEN]}",
                category=LogCategory.QUEEN,
                is_notification=True,
            )
            self._broadcast_ws(
                {
                    "type": "queen_auto_acted",
                    "worker": worker.name,
                    "action": action,
                    "confidence": confidence,
                    "assessment": result.get("assessment", ""),
                }
            )
        else:
            self._queue_proposal(proposal)

    async def execute_escalation(self, proposal: AssignmentProposal) -> bool:
        """Execute an approved escalation proposal's recommended action."""
        from swarm.worker.manager import revive_worker

        worker = self._get_worker(proposal.worker_name)
        if not worker:
            return False
        proc = worker.process
        if not proc:
            return False

        action = proposal.queen_action
        if proc.is_user_active:
            _log.info(
                "skipping escalation %s for %s: user active in terminal",
                action,
                proposal.worker_name,
            )
            return False
        if action == QueenAction.SEND_MESSAGE and proposal.message:
            # Only send messages when worker is at a prompt (WAITING/RESTING).
            # Sending text to a BUZZING worker injects into the active subprocess,
            # which can crash Claude or be interpreted as bash commands.
            if worker.state == WorkerState.BUZZING:
                _log.info(
                    "skipping escalation send_message for %s: worker is BUZZING",
                    proposal.worker_name,
                )
                return False
            if not proc.is_alive:
                _log.info(
                    "skipping escalation send_message for %s: process not alive",
                    proposal.worker_name,
                )
                return False
            await proc.send_keys(proposal.message)
        elif action == QueenAction.CONTINUE:
            if worker.state != WorkerState.BUZZING:
                _log.info(
                    "skipping escalation continue for %s: state is %s",
                    proposal.worker_name,
                    worker.state.value,
                )
                return False
            await proc.send_enter()
        elif action == QueenAction.RESTART:
            await revive_worker(worker, self._get_pool())
            worker.record_revive()
        # "wait" is a no-op
        return True

    async def analyze_completion(self, worker: Worker, task: SwarmTask) -> None:
        """Ask Queen to assess whether a task is complete and draft resolution."""
        _start = time.time()
        # Re-check: worker may have resumed working since the event was queued
        if not worker.process or worker.state == WorkerState.BUZZING:
            return

        try:
            content = worker.process.get_content(100)
            criteria_section = _build_criteria_section(task.acceptance_criteria)

            result = await self.queen.ask(
                f"Worker '{worker.name}' was assigned task:\n"
                f"  Title: {task.title}\n"
                f"  Description: {task.description or 'N/A'}\n"
                f"  Type: {getattr(task.task_type, 'value', task.task_type)}\n"
                f"{criteria_section}\n"
                f"The worker has been idle for {format_duration(worker.state_duration)}.\n\n"
                f"Recent worker output (last 100 lines):\n{content}\n\n"
                "Analyze the output carefully. Look for concrete evidence:\n"
                "- Commits, pushes, or PRs created\n"
                "- Tests passing or failing\n"
                "- Error messages or unresolved issues\n"
                "- The worker explicitly stating it finished or got stuck\n\n"
                "Do NOT restate the task title as the resolution. Instead describe "
                "what the worker ACTUALLY DID based on the output evidence.\n\n"
                "Return JSON:\n"
                '{"done": true/false, "resolution": "what the worker actually accomplished '
                'or what remains unfinished — cite specific evidence from the output", '
                '"confidence": 0.0 to 1.0}\n\n'
                "Set done=false unless you see clear evidence of completion "
                "(commit, tests passing, worker saying done). When in doubt, say not done."
            )
        except asyncio.CancelledError:
            _log.info("Queen completion analysis cancelled for %s", worker.name)
            return
        except (ProcessError, OSError):
            _log.warning(
                "Queen completion analysis failed for %s",
                worker.name,
                exc_info=True,
            )
            return

        done = result.get("done", False) if isinstance(result, dict) else False
        resolution = (
            result.get("resolution", f"Worker idle for {format_duration(worker.state_duration)}")
            if isinstance(result, dict)
            else f"Worker idle for {format_duration(worker.state_duration)}"
        )
        confidence = float(result.get("confidence", 0.3)) if isinstance(result, dict) else 0.3

        self._drone_log.add(
            SystemAction.QUEEN_COMPLETION,
            worker.name,
            f"completion: done={done} conf={confidence:.0%}",
            category=LogCategory.QUEEN,
            metadata={
                "done": done,
                "confidence": confidence,
                "resolution": resolution[:200],
                "task_id": task.id,
                "duration_s": round(time.time() - _start, 1),
            },
        )
        # Feed the verdict back to task_lifecycle so it can extend the
        # re-propose cooldown when Queen is confidently sure the worker
        # hasn't finished (see task A in
        # docs/specs/headless-queen-architecture.md).
        if self._record_completion_verdict is not None:
            try:
                self._record_completion_verdict(task.id, done, confidence)
            except Exception:
                _log.debug("completion-verdict callback failed", exc_info=True)
        self._emit_event(
            "queen_analysis",
            worker.name,
            "complete_task" if done else "wait",
            resolution,
            confidence,
        )

        # Reject idle-fallback resolutions — Queen didn't provide real analysis
        if re.match(r"^worker\s+\S*\s*(?:idle|has been idle)\s+for\s+\d+", resolution, re.I):
            _log.info(
                "Queen returned idle-fallback resolution for task '%s' — not proposing",
                task.title,
            )
            return

        # Sanity check: if the resolution text contradicts "done", override
        _NOT_DONE_PHRASES = (
            "could not be verified",
            "not verified",
            "could not confirm",
            "unable to confirm",
            "unable to verify",
            "not complete",
            "not done",
            "not finished",
            "needs more work",
            "went idle without",
            "did not complete",
            "hasn't been completed",
            "recommend re-running",
        )
        res_lower = resolution.lower()
        if done and any(phrase in res_lower for phrase in _NOT_DONE_PHRASES):
            _log.info(
                "Queen said done but resolution contradicts — overriding to not done: %s",
                resolution[:120],
            )
            done = False

        if not done:
            _log.info("Queen says task '%s' is NOT done for %s", task.title, worker.name)
            return

        if confidence < 0.6:
            _log.info(
                "Queen confidence too low (%.2f) for task '%s' — skipping proposal",
                confidence,
                task.title,
            )
            return

        # Race guard: worker may have resumed while Queen was thinking
        if worker.state == WorkerState.BUZZING:
            _log.info(
                "Worker %s resumed (BUZZING) — dropping completion proposal for '%s'",
                worker.name,
                task.title,
            )
            return

        # Race guard: duplicate proposal check
        if self._proposal_store.has_pending_completion(worker.name, task.id):
            return

        proposal = AssignmentProposal.completion(
            worker_name=worker.name,
            task_id=task.id,
            task_title=task.title,
            assessment=resolution,
            reasoning=f"Worker idle for {format_duration(worker.state_duration)}",
            confidence=confidence,
        )
        self._queue_proposal(proposal)

    async def gather_context(self) -> str:
        """Capture all worker outputs and build hive context string for the Queen."""
        from swarm.queen.context import build_hive_context

        worker_outputs: dict[str, str] = {}
        workers = self._get_workers()
        for w in list(workers):
            try:
                worker_outputs[w.name] = w.process.get_content(60)
            except (ProcessError, OSError):
                _log.debug("failed to capture output for %s in queen flow", w.name)
        config = self._get_config()
        return build_hive_context(
            list(workers),
            worker_outputs=worker_outputs,
            drone_log=self._drone_log,
            task_board=self._task_board,
            worker_descriptions=self._get_worker_descriptions(),
            approval_rules=config.drones.approval_rules or None,
        )

    async def analyze_worker(self, worker_name: str, *, force: bool = False) -> dict[str, Any]:
        """Run Queen analysis on a specific worker. Returns Queen's analysis dict.

        Does NOT include full hive context — per-worker analysis should focus
        on just that worker's output.  Includes assigned task info so the
        Queen can recommend ``complete_task`` when appropriate.
        Use ``coordinate()`` for hive-wide analysis.
        """
        worker = self._require_worker(worker_name)
        proc = worker.process
        content = proc.get_content() if proc else ""

        task_info = build_worker_task_info(self._task_board, worker.name)

        return await self.queen.analyze_worker(
            worker.name,
            content,
            force=force,
            task_info=task_info,
            idle_duration_seconds=worker.state_duration,
        )

    # coordinate() removed with the hive-coordination caller (task #253 spec
    # B).  Previously wrapped gather_context + queen.coordinate_hive; both
    # gone.  See ``docs/specs/headless-queen-architecture.md``.
