"""DecisionExecutor — drone decision evaluation and deferred action execution."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from swarm.drones.log import DroneAction, LogCategory, SystemAction
from swarm.drones.rules import Decision, decide
from swarm.logging import get_logger
from swarm.pty.process import ProcessError
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Any

    from swarm.config import DroneConfig
    from swarm.drones.directives import DirectiveExecutor
    from swarm.drones.log import DroneLog
    from swarm.drones.rules import DroneDecision
    from swarm.providers import LLMProvider
    from swarm.pty.provider import WorkerProcessProvider

_log = get_logger("drones.decision_executor")


class DecisionExecutor:
    """Evaluates drone decisions and executes deferred actions.

    Extracted from :class:`~swarm.drones.pilot.DronePilot` to reduce
    pilot.py complexity.
    """

    def __init__(
        self,
        workers: list[Worker],
        log: DroneLog,
        pool: WorkerProcessProvider | None,
        drone_config: DroneConfig,
        emit: Callable[..., None],
        get_provider: Callable[[Worker], LLMProvider],
        directive_executor: DirectiveExecutor,
        escalated: dict[str, float],
        revive_history: dict[str, list[float]],
    ) -> None:
        self.workers = workers
        self.log = log
        self.pool = pool
        self.drone_config = drone_config
        self._emit = emit
        self._get_provider = get_provider
        self._directive_executor = directive_executor
        self._escalated = escalated
        self._revive_history = revive_history
        # Deferred actions collected during polling, executed after the poll loop
        self._deferred_actions: list[tuple[Any, ...]] = []
        st = drone_config.state_thresholds
        self._revive_loop_max: int = st.buzzing_confirm_count
        self._revive_loop_window: float = st.revive_grace * 4
        self._escalation_timeout: float = 180.0  # 3 minutes
        # Track whether any substantive (non-escalation) action happened this tick.
        self._had_substantive_action: bool = False
        # Test mode: emit drone_decision events with full context
        self._emit_decisions: bool = False
        # Per-worker config lookup (set externally by pilot)
        self._worker_configs: dict[str, Any] = {}
        # Escalation spam: track consecutive identical escalations per worker
        self._consecutive_escalations: dict[str, tuple[int, str]] = {}

    def set_emit_decisions(self, enabled: bool) -> None:
        """Enable/disable emission of drone_decision events (for test mode)."""
        self._emit_decisions = enabled

    def clear_escalation_spam(self, worker_name: str) -> None:
        """Reset consecutive escalation counter for a worker (on state change)."""
        self._consecutive_escalations.pop(worker_name, None)

    def _should_skip_decide(self, worker: Worker, changed: bool, enabled: bool) -> bool:
        """Return True if the decision engine should be skipped for this worker."""
        import time as _time

        # Skip when drones are disabled
        if not enabled:
            return True
        # Skip already-escalated workers with no state change,
        # but auto-clear stale escalations after the timeout
        if worker.name in self._escalated and not changed:
            esc_age = _time.monotonic() - self._escalated[worker.name]
            if esc_age < self._escalation_timeout:
                return True
            # Escalation expired -- clear it and re-evaluate
            self._escalated.pop(worker.name, None)
            _log.info("escalation expired for %s after %.0fs", worker.name, esc_age)
        return False

    def _run_decision_sync(
        self, worker: Worker, content: str, events: list[Any] | None = None
    ) -> bool:
        """Evaluate the drone decision for a worker (sync -- actions deferred)."""
        from swarm.drones.pilot import extract_prompt_snippet

        # Look up per-worker approval rules from config
        worker_rules = None
        if self._worker_configs:
            wc = self._worker_configs.get(worker.name)
            if wc is not None and wc.approval_rules:
                worker_rules = wc.approval_rules

        decision = decide(
            worker,
            content,
            self.drone_config,
            escalated=self._escalated,
            provider=self._get_provider(worker),
            events=events,
            worker_rules=worker_rules,
        )

        if self._emit_decisions:
            self._emit("drone_decision", worker, content, decision)

        if decision.decision == Decision.CONTINUE:
            self._deferred_actions.append(
                ("continue", worker, decision, worker.state, worker.process, content)
            )
            return True
        if decision.decision == Decision.REVIVE:
            self._deferred_actions.append(
                ("revive", worker, decision, worker.state, worker.process)
            )
            return True
        if decision.decision == Decision.ESCALATE:
            # Escalation spam detection: suppress after 3+ consecutive identical
            prev_count, prev_reason = self._consecutive_escalations.get(worker.name, (0, ""))
            if decision.reason == prev_reason:
                count = prev_count + 1
            else:
                count = 1
            self._consecutive_escalations[worker.name] = (count, decision.reason)

            if count > 3:
                # Already logged systematic issue — suppress further noise
                return True
            if count == 3:
                self.log.add(
                    DroneAction.ESCALATED,
                    worker.name,
                    f"systematic: {decision.reason} (3+ consecutive, suppressing)",
                    metadata={"source": "spam_detection"},
                )
                self._emit("escalate", worker, decision.reason)
                return True

            self.log.add(
                DroneAction.ESCALATED,
                worker.name,
                decision.reason,
                metadata={
                    "source": decision.source,
                    "rule_pattern": decision.rule_pattern,
                    "prompt_snippet": extract_prompt_snippet(content),
                },
            )
            self._emit("escalate", worker, decision.reason)
            return True
        return False

    def _is_revive_loop(self, name: str) -> bool:
        """Return True if *name* has been revived too many times within the window."""
        now = time.monotonic()
        history = self._revive_history.get(name, [])
        # Prune entries outside the window
        recent = [t for t in history if now - t < self._revive_loop_window]
        self._revive_history[name] = recent
        return len(recent) >= self._revive_loop_max

    def _record_revive(self, name: str) -> None:
        """Record a successful revive for loop detection."""
        self._revive_history.setdefault(name, []).append(time.monotonic())
        # Periodically prune history for workers that no longer exist
        live_names = {w.name for w in self.workers}
        dead_names = [k for k in self._revive_history if k not in live_names]
        for k in dead_names:
            del self._revive_history[k]

    async def _execute_deferred_actions(self) -> None:
        """Execute deferred async actions from the sync poll loop.

        Each action carries a snapshot of ``worker.state`` and
        ``worker.process`` from decision time.  If either has changed
        by execution time the action is skipped -- this prevents
        operating on a different process or in an unexpected state.
        """
        # Lazy import: tests monkeypatch swarm.drones.pilot.revive_worker
        from swarm.drones.pilot import revive_worker

        for entry in self._deferred_actions:
            action_type, worker, decision, state_at_decision, proc_at_decision = entry[:5]
            # "continue" tuples carry content as a 6th element; "revive" does not.
            content_at_decision = entry[5] if len(entry) > 5 else ""
            if action_type == "continue":
                await self._execute_deferred_continue(
                    worker, decision, state_at_decision, proc_at_decision, content_at_decision
                )
            elif action_type == "compact":
                await self._execute_deferred_compact(worker, proc_at_decision)
            elif action_type == "revive":
                if worker.state != state_at_decision:
                    _log.info(
                        "skipping deferred revive for %s: state changed %s -> %s",
                        worker.name,
                        state_at_decision.value,
                        worker.state.value,
                    )
                    continue
                if self._is_revive_loop(worker.name):
                    reason = (
                        f"revive loop — {self._revive_loop_max} revives "
                        f"in {self._revive_loop_window:.0f}s window"
                    )
                    _log.warning("%s: %s, escalating", worker.name, reason)
                    self.log.add(DroneAction.ESCALATED, worker.name, reason)
                    self._emit("escalate", worker, reason)
                elif await self._safe_worker_action(
                    worker,
                    revive_worker(worker, self.pool),
                    DroneAction.REVIVED,
                    decision,
                ):
                    worker.record_revive()
                    self._record_revive(worker.name)
                    await self._inject_context_restoration(worker)
        self._deferred_actions.clear()

    async def _execute_deferred_continue(
        self,
        worker: Worker,
        decision: DroneDecision,
        state_at_decision: WorkerState,
        proc_at_decision: object | None,
        content: str = "",
    ) -> None:
        """Execute a single deferred CONTINUE action with safety checks."""
        from swarm.drones.pilot import extract_prompt_snippet

        if worker.state != state_at_decision:
            _log.info(
                "skipping deferred continue for %s: state changed %s -> %s",
                worker.name,
                state_at_decision.value,
                worker.state.value,
            )
            return
        proc = worker.process
        if proc and proc.is_user_active:
            _log.info(
                "skipping deferred continue for %s: user active in terminal",
                worker.name,
            )
            return
        if worker.state in (WorkerState.RESTING, WorkerState.SLEEPING):
            _log.info(
                "skipping deferred continue for %s: worker is %s",
                worker.name,
                worker.state.value,
            )
            self.log.add(
                SystemAction.QUEEN_BLOCKED,
                worker.name,
                f"deferred continue blocked — worker is {worker.state.value}",
                category=LogCategory.DRONE,
            )
            return
        if self._has_idle_prompt(worker):
            _log.info(
                "skipping deferred continue for %s: idle/suggested prompt",
                worker.name,
            )
            self.log.add(
                SystemAction.QUEEN_BLOCKED,
                worker.name,
                "deferred continue blocked — suggested prompt requires operator",
                category=LogCategory.DRONE,
            )
            return
        if self._has_operator_text_at_prompt(worker):
            _log.info(
                "skipping deferred continue for %s: operator text at prompt",
                worker.name,
            )
            self.log.add(
                SystemAction.QUEEN_BLOCKED,
                worker.name,
                "deferred continue blocked — operator text at prompt",
                category=LogCategory.DRONE,
            )
            return
        # Use the process from decision time if the current process differs
        target_proc = proc_at_decision if proc_at_decision is not None else proc
        if target_proc is None:
            _log.warning(
                "skipping deferred continue for %s: no process available",
                worker.name,
            )
            return
        provider = self._get_provider(worker)
        if await self._safe_worker_action(
            worker,
            target_proc.send_keys(provider.approval_response(True), enter=False, automated=True),
            DroneAction.CONTINUED,
            decision,
            include_rule_pattern=True,
            prompt_snippet=extract_prompt_snippet(content),
        ):
            self._drone_continued_callback(worker.name)

    async def _inject_context_restoration(self, worker: Worker) -> None:
        """After revive, send a context summary so the worker doesn't start cold."""
        if not worker.last_context_files and not worker.name:
            return
        proc = worker.process
        if not proc:
            return
        # Build context message from tracked files and assigned task
        parts = []
        if worker.last_context_files:
            files = ", ".join(worker.last_context_files[-5:])
            parts.append(f"Key files from your previous session: {files}")
        if parts:
            msg = "Context restoration: " + ". ".join(parts)
            # Wait briefly for Claude to initialize
            import asyncio

            await asyncio.sleep(3)
            try:
                await proc.send_keys(msg, enter=True, automated=True)
                _log.info("injected context restoration for %s", worker.name)
            except (ProcessError, OSError):
                _log.debug("context restoration failed for %s", worker.name)
        # Clear tracked files after restoration
        worker.last_context_files.clear()

    async def _execute_deferred_compact(
        self,
        worker: Worker,
        proc_at_decision: object | None,
    ) -> None:
        """Inject /compact into a worker that hit the context critical threshold."""
        proc = worker.process
        target = proc_at_decision if proc_at_decision is not None else proc
        if target is None:
            worker.compacting = False
            return
        if worker.state != WorkerState.BUZZING:
            worker.compacting = False
            return
        try:
            await target.send_keys("/compact", enter=True, automated=True)
            _log.info("injected /compact for %s", worker.name)
        except (ProcessError, OSError):
            _log.warning("failed to inject /compact for %s", worker.name)
            worker.compacting = False

    def _has_pending_bash_approval(self, worker: Worker) -> bool:
        """Check if a worker's terminal shows a bash/command approval prompt."""
        return self._directive_executor.has_pending_bash_approval(worker)

    def _has_idle_prompt(self, worker: Worker) -> bool:
        """Check if worker's terminal shows an idle/suggested prompt."""
        return self._directive_executor.has_idle_prompt(worker)

    def _has_operator_text_at_prompt(self, worker: Worker) -> bool:
        """Check if worker has operator-typed text at a prompt."""
        return self._directive_executor.has_operator_text_at_prompt(worker)

    async def _safe_worker_action(
        self,
        worker: Worker,
        coro: Awaitable[None],
        action: DroneAction,
        decision: DroneDecision | None = None,
        *,
        include_rule_pattern: bool = False,
        reason: str | None = None,
        prompt_snippet: str = "",
    ) -> bool:
        """Execute *coro* for *worker*, log on success, warn on failure.

        Returns ``True`` on success.  Sets ``_had_substantive_action`` so
        the adaptive backoff resets correctly.
        """
        try:
            await coro
        except (ProcessError, OSError):
            _log.warning("failed %s for %s", action.value, worker.name, exc_info=True)
            return False
        metadata: dict[str, str] = {}
        if decision is not None:
            metadata["source"] = decision.source
            if include_rule_pattern and decision.rule_pattern:
                metadata["rule_pattern"] = decision.rule_pattern
        if prompt_snippet:
            metadata["prompt_snippet"] = prompt_snippet
        log_reason = reason or (decision.reason if decision else "")
        self.log.add(action, worker.name, log_reason, metadata=metadata)
        self._had_substantive_action = True
        return True

    @staticmethod
    def _drone_continued_callback(name: str) -> None:
        """No-op default — overwritten via set_drone_continued_callback."""

    def set_drone_continued_callback(self, callback: Callable[[str], None]) -> None:
        """Register callback to track which worker was drone-continued."""
        self._drone_continued_callback = callback
