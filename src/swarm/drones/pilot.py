"""Drone background drones — async polling loop + decision engine."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, ClassVar

from swarm.config import DroneConfig
from swarm.drones.context_pressure import ContextPressureWatcher
from swarm.drones.coordination import CoordinationHandler
from swarm.drones.decision_executor import DecisionExecutor as _DecisionExecutor
from swarm.drones.detectors import (
    ContextFileTracker,
    ContextPressureCheck,
    ContextRecoveryDetector,
    DiminishingReturnsDetector,
    RateLimitDetector,
    WorkerHealthDetectors,
)
from swarm.drones.directives import DirectiveExecutor
from swarm.drones.dreamer import Dreamer
from swarm.drones.idle_watcher import IdleWatcher
from swarm.drones.inter_worker_watcher import InterWorkerMessageWatcher
from swarm.drones.log import DroneAction, DroneLog
from swarm.drones.oversight_handler import OversightHandler
from swarm.drones.poll_dispatcher import PollDispatcher
from swarm.drones.pressure import PressureManager
from swarm.drones.state_tracker import WorkerStateTracker
from swarm.drones.task_lifecycle import TaskLifecycle
from swarm.events import EventEmitter
from swarm.logging import get_logger
from swarm.worker.manager import revive_worker  # noqa: F401 — monkeypatched by tests
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Any

    from swarm.drones.rules import DroneDecision
    from swarm.events import (
        EscalateCallback,
        ProposalCallback,
        TaskAssignedCallback,
        TaskDoneCallback,
        VoidCallback,
        WorkerCallback,
    )
    from swarm.providers import LLMProvider
    from swarm.pty.provider import WorkerProcessProvider
    from swarm.queen.oversight import OversightMonitor
    from swarm.queen.queen import Queen
    from swarm.tasks.board import TaskBoard

_log = get_logger("drones.pilot")


async def _noop_sender(name: str, message: str, **kwargs: Any) -> None:
    """Placeholder send-to-worker used before the daemon wires the real one.

    Runs the idle_watcher's machinery (debounce, filter, log entry) during
    early startup or in tests without a daemon, but never actually touches
    a PTY.
    """
    return None


async def _noop_interrupt(name: str) -> None:
    """Placeholder Ctrl-C used before the daemon wires the real one."""
    return None


def extract_prompt_snippet(content: str, max_lines: int = 15) -> str:
    """Extract the prompt area from PTY content for rule creation context."""
    lines = content.strip().splitlines()
    return "\n".join(lines[-max_lines:])


# Run Queen coordination every N poll cycles (default: every 12 cycles = ~60s at 5s interval)
_COORDINATION_INTERVAL = 12

# classify_worker_output examines <=30 lines; 35 gives margin for context.
_STATE_DETECT_LINES = 35

# Commands that should never be pre-populated as suggested approval patterns.
# Returning "" forces the user to type a pattern deliberately.
_DANGEROUS_CMDS = frozenset(
    {
        "rm",
        "rmdir",
        "kill",
        "killall",
        "pkill",
        "dd",
        "mkfs",
        "fdisk",
        "parted",
        "chmod",
        "chown",
        "chgrp",
        "sudo",
        "su",
        "doas",
        "reboot",
        "shutdown",
        "halt",
        "poweroff",
        "init",
        "mv",
    }
)

# Wrapper commands — include the next word to form the pattern
# (e.g. "uv run pytest" → 3 words, not just "uv")
_WRAPPER_CMDS = frozenset({"uv", "npx", "bunx", "pipx", "nix"})


def _build_safe_pattern(words: list[str]) -> str:
    """Build a safe, specific approval pattern from command words.

    Returns ``""`` if the root command is in :data:`_DANGEROUS_CMDS`.
    Otherwise returns a ``\\b``-delimited pattern using the first two
    meaningful words (three for wrapper commands like ``uv run``).
    """
    if not words:
        return ""

    root = words[0]
    # Handle variants like "mkfs.ext4" → check "mkfs"
    root_base = root.split(".")[0]
    if root in _DANGEROUS_CMDS or root_base in _DANGEROUS_CMDS:
        return ""

    # For wrapper commands like "uv run pytest", take 3 words
    if root in _WRAPPER_CMDS and len(words) >= 3 and words[1] == "run":
        key = " ".join(words[:3])
    elif len(words) >= 2:
        # Check if the second word is also dangerous (e.g. "sudo rm")
        if words[1] in _DANGEROUS_CMDS:
            return ""
        key = " ".join(words[:2])
    else:
        key = root

    return r"\b" + re.escape(key) + r"\b"


# Matches a prompt line with operator-typed text: "> /verify", "❯ fix the bug"
_RE_PROMPT_WITH_TEXT = re.compile(r"^[>❯]\s+\S")


class DronePilot(EventEmitter):
    def __init__(
        self,
        workers: list[Worker],
        log: DroneLog,
        interval: float = 5.0,
        pool: WorkerProcessProvider | None = None,
        drone_config: DroneConfig | None = None,
        task_board: TaskBoard | None = None,
        queen: Queen | None = None,
        worker_descriptions: dict[str, str] | None = None,
        context_builder: Callable[..., str] | None = None,
    ) -> None:
        self.__init_emitter__()
        self.workers = workers
        self.log = log
        self.interval = interval
        self.pool = pool
        self._drone_config = drone_config or DroneConfig()
        self._worker_configs: dict[str, object] = {}  # name → WorkerConfig
        self._provider_cache: dict[str, LLMProvider] = {}
        self._task_board = task_board
        self._queen = queen
        self.worker_descriptions = worker_descriptions or {}
        self._context_builder = context_builder
        self.enabled = False
        self._base_interval: float = interval
        self._max_interval: float = self._drone_config.max_idle_interval
        # Focus tracking: when a user is viewing a worker, poll faster
        self._focused_workers: set[str] = set()
        self._focus_interval: float = 2.0
        # Shared mutable state containers
        self._prev_states: dict[str, WorkerState] = {}
        self._escalated: dict[str, float] = {}  # name → monotonic escalation time
        self._revive_history: dict[str, list[float]] = {}
        self._idle_consecutive: dict[str, int] = {}
        self._proposed_completions: dict[str, float] = {}
        self._suspended: set[str] = set()
        self._suspended_at: dict[str, float] = {}
        # Proposal support: callback to check if pending proposals exist
        self._pending_proposals_check: Callable[[], bool] | None = None
        # Per-worker proposal check: returns True if the named worker has pending proposals
        self._pending_proposals_for_worker: Callable[[str], bool] | None = None
        # Oversight monitor (initialized externally via set_oversight)
        self._oversight: OversightMonitor | None = None
        # Oversight check interval in ticks (separate from coordination)
        self._oversight_interval: int = 24  # ~2 min at 5s poll

        # --- Sub-handlers (extracted for complexity reduction) ---
        self._directives = DirectiveExecutor(
            workers=self.workers,
            log=self.log,
            pool=self.pool,
            queen=self.queen,
            task_board=self.task_board,
            emit=self.emit,
            # Late-bind: ``_state_tracker`` is constructed below, and
            # ``DirectiveExecutor`` only invokes this callback after init
            # completes. A lambda lets us preserve the construction order
            # (directives → state_tracker → decision_exec is the natural
            # dependency order, but state_tracker uses decision_exec, so
            # we'd loop) without re-introducing a delegation method that
            # has no other purpose.
            classify_worker_state=lambda *a, **kw: self._state_tracker._classify_worker_state(
                *a, **kw
            ),
            get_provider=self._get_provider,
            safe_worker_action=self._safe_worker_action,
            pending_proposals_check=self._pending_proposals_check,
            proposed_completions=self._proposed_completions,
        )
        self._coordination = CoordinationHandler(
            workers=self.workers,
            escalated=self._escalated,
        )
        self._oversight_handler = OversightHandler(
            workers=self.workers,
            log=self.log,
            queen=self.queen,
            task_board=self.task_board,
            oversight_monitor=self._oversight,
            emit=self.emit,
            capture_outputs=self._coordination.capture_worker_outputs,
            get_provider=self._get_provider,
        )
        self._pressure_mgr = PressureManager(
            workers=self.workers,
            log=self.log,
            pool=self.pool,
            suspended=self._suspended,
            suspended_at=self._suspended_at,
            emit=self.emit,
        )
        self._decision_exec = _DecisionExecutor(
            workers=self.workers,
            log=self.log,
            pool=self.pool,
            drone_config=self.drone_config,
            emit=self.emit,
            get_provider=self._get_provider,
            directive_executor=self._directives,
            escalated=self._escalated,
            revive_history=self._revive_history,
        )
        # Per-worker health detectors composed into the tracker (state-
        # tracker-refactor Phases 1 + 2 + 3). Constructed eagerly so they
        # can be passed in via WorkerHealthDetectors; the tracker's poll
        # loop invokes each ``check()`` in sequence.
        self._detectors = WorkerHealthDetectors(
            context_files=ContextFileTracker(),
            diminishing=DiminishingReturnsDetector(log=self.log, emit=self.emit),
            rate_limit=RateLimitDetector(log=self.log, emit=self.emit),
            recovery=ContextRecoveryDetector(
                log=self.log,
                decision_executor=self._decision_exec,
                emit=self.emit,
            ),
            pressure=ContextPressureCheck(
                log=self.log,
                decision_executor=self._decision_exec,
                drone_config=self.drone_config,
            ),
        )
        self._state_tracker = WorkerStateTracker(
            workers=self.workers,
            log=self.log,
            task_board=self.task_board,
            drone_config=self.drone_config,
            get_provider=self._get_provider,
            emit=self.emit,
            decision_executor=self._decision_exec,
            prev_states=self._prev_states,
            idle_consecutive=self._idle_consecutive,
            escalated=self._escalated,
            suspended=self._suspended,
            suspended_at=self._suspended_at,
            focused_workers=self._focused_workers,
            revive_history=self._revive_history,
            detectors=self._detectors,
        )
        self._task_lifecycle = TaskLifecycle(
            workers=self.workers,
            log=self.log,
            task_board=self.task_board,
            queen=self.queen,
            drone_config=self.drone_config,
            proposed_completions=self._proposed_completions,
            idle_consecutive=self._idle_consecutive,
            emit=self.emit,
            build_context=self._build_context,
            pending_proposals_check=self._pending_proposals_check,
            pending_proposals_for_worker=self._pending_proposals_for_worker,
            worker_busy_check=self._worker_busy,
        )
        # Idle-watcher (task #225 Phase 2): created eagerly with a null
        # sender so ``self.idle_watcher`` is never None in tests or code
        # paths that inspect it. The daemon swaps in the real
        # ``send_to_worker`` callback via ``set_idle_nudge_sender()``
        # after construction; until then sweeps are still safe — they
        # call the stub, which is a no-op.
        self.idle_watcher: IdleWatcher = IdleWatcher(
            drone_config=self._drone_config,
            task_board=self._task_board,
            drone_log=self.log,
            send_to_worker=_noop_sender,
        )
        self._idle_watcher_last_tick: int = 0
        # Inter-worker message watcher (task #235 Phase 3). Same null-
        # sender bootstrap as IdleWatcher — the daemon wires the real
        # ``send_to_worker`` via ``set_idle_nudge_sender`` after
        # construction. The message store comes from the daemon too;
        # constructed eagerly with None so the attribute exists in
        # tests that don't spin a store.
        self.inter_worker_watcher: InterWorkerMessageWatcher = InterWorkerMessageWatcher(
            drone_config=self._drone_config,
            message_store=None,
            drone_log=self.log,
            send_to_worker=_noop_sender,
            task_board=self._task_board,
        )
        # Context-pressure watcher (item 3 of the 10-repo bundle).
        # Bootstrap with no-op senders; daemon swaps in real
        # ``send_to_worker`` + ``interrupt_worker`` via
        # ``set_idle_nudge_sender``.
        self.context_pressure_watcher: ContextPressureWatcher = ContextPressureWatcher(
            drone_config=self._drone_config,
            drone_log=self.log,
            send_to_worker=_noop_sender,
            interrupt_worker=_noop_interrupt,
        )
        # Dreamer drone — periodic pattern-mining over the buzz log.
        # Same bootstrap idiom: constructed eagerly with ``None`` stores
        # so ``self.dreamer`` is never absent in tests, then rebound by
        # the daemon via ``set_dreamer_stores`` once the buzz store and
        # learnings store are live. Sweeps stay no-op until both are
        # wired (see :attr:`Dreamer.enabled`).
        self.dreamer: Dreamer = Dreamer(
            drone_config=self._drone_config,
            buzz_store=None,
            learnings_store=None,
            drone_log=self.log,
        )
        self._dispatcher = PollDispatcher(self)
        # Wire the drone-continued callback
        self._decision_exec.set_drone_continued_callback(self._state_tracker.mark_drone_continued)
        # Wire per-worker config lookup for worker-scoped approval rules
        self._decision_exec._worker_configs = self._worker_configs
        # Task #233: route pressure RESUME through the state tracker's
        # wake_worker so fingerprints get cleared. PressureManager is
        # constructed before the state tracker, so the callback is
        # attached here after both exist.
        self._pressure_mgr._wake_worker = self._state_tracker.wake_worker

    @property
    def task_board(self) -> TaskBoard | None:
        """Return the task board."""
        return self._task_board

    @task_board.setter
    def task_board(self, value: TaskBoard | None) -> None:
        """Set the task board, propagating to sub-handlers."""
        self._task_board = value
        for attr in ("_task_lifecycle", "_directives", "_oversight_handler"):
            handler = getattr(self, attr, None)
            if handler is not None:
                handler.task_board = value

    @property
    def queen(self) -> Queen | None:
        """Return the Queen instance."""
        return self._queen

    @queen.setter
    def queen(self, value: Queen | None) -> None:
        """Set the Queen instance, propagating to sub-handlers."""
        self._queen = value
        if hasattr(self, "_directives"):
            self._directives.queen = value
        if hasattr(self, "_oversight_handler"):
            self._oversight_handler.queen = value
        if hasattr(self, "_task_lifecycle"):
            self._task_lifecycle.queen = value

    @property
    def drone_config(self) -> DroneConfig:
        """Return the drone config."""
        return self._drone_config

    @drone_config.setter
    def drone_config(self, value: DroneConfig) -> None:
        """Set the drone config, propagating to sub-handlers."""
        self._drone_config = value
        for attr in ("_decision_exec", "_state_tracker", "_task_lifecycle", "_pressure_mgr"):
            handler = getattr(self, attr, None)
            if handler is not None:
                handler.drone_config = value

    @property
    def pressure_suspended_workers(self) -> list[str]:
        """Return sorted list of workers currently suspended due to resource pressure."""
        return self._pressure_mgr.pressure_suspended_workers

    # Class-level constants — delegate to _task_lifecycle for runtime access
    _AUTO_COMPLETE_MIN_IDLE: ClassVar[int] = 45
    _PROPOSED_COMPLETION_CLEANUP_INTERVAL: ClassVar[int] = 60
    _PROPOSED_COMPLETION_MAX_AGE: ClassVar[float] = 3600.0
    _PROPOSED_COMPLETION_MAX_SIZE: ClassVar[int] = 500

    def _get_provider(self, worker: Worker) -> LLMProvider:
        """Return the LLMProvider for a worker, caching by provider name."""
        name = worker.provider_name
        if name not in self._provider_cache:
            from swarm.providers import get_provider

            self._provider_cache[name] = get_provider(name)
        return self._provider_cache[name]

    def invalidate_provider_cache(self) -> None:
        """Clear cached providers so tuning changes take effect."""
        self._provider_cache.clear()

    def _worker_busy(self, worker: Worker) -> bool:
        """Live-PTY busy check shared by the idle / completion nudge guards.

        Delegates to the state tracker, which re-reads the PTY for a
        mid-turn signal (esc-to-interrupt, background shell, subagent,
        dynamic workflow) so a worker mid a long *quiet* foreground command
        isn't nudged just because its display_state went stale (2026-06-11
        false-AUTO_NUDGE bug, trigger #2). Defensive — no tracker yet ⇒
        not busy.
        """
        tracker = getattr(self, "_state_tracker", None)
        if tracker is None:
            return False
        return tracker.worker_has_active_turn(worker)

    def _build_context(self, **kwargs: object) -> str:
        """Build hive context string via the injected context_builder."""
        if self._context_builder is None:
            from swarm.queen.context import build_hive_context

            self._context_builder = build_hive_context
        # Collect worker identities from configs
        identities: dict[str, str] = {}
        for name, wc in self._worker_configs.items():
            if hasattr(wc, "load_identity"):
                identity = wc.load_identity()
                if identity:
                    identities[name] = identity
        return self._context_builder(
            list(self.workers),
            drone_log=self.log,
            task_board=self.task_board,
            worker_descriptions=self.worker_descriptions,
            worker_identities=identities or None,
            **kwargs,
        )

    # --- Public encapsulation methods ---

    def get_diagnostics(self) -> dict[str, object]:
        """Return pilot health/diagnostic info for status endpoints."""
        dispatcher = self._dispatcher
        task = dispatcher._task
        info: dict[str, object] = {
            "running": dispatcher._running,
            "enabled": self.enabled,
            "task_alive": task is not None and not task.done(),
            "tick": dispatcher._tick,
            "idle_streak": dispatcher._idle_streak,
            "suspended_count": len(self._suspended),
            "suspended_workers": sorted(self._suspended),
        }
        if task and task.done():
            try:
                exc = task.exception() if not task.cancelled() else "cancelled"
            except Exception:
                exc = "unknown"
            info["task_exception"] = str(exc) if exc else None
        return info

    def set_focused_workers(self, workers: set[str]) -> None:
        """Set which workers should be polled at accelerated interval."""
        # Wake any newly focused workers that are suspended
        for name in workers - self._focused_workers:
            self.wake_worker(name)
        self._focused_workers = workers
        # Propagate to state tracker
        self._state_tracker._focused_workers = workers

    def is_focused(self, worker_name: str) -> bool:
        """True if the operator is currently viewing this worker in the dashboard."""
        return worker_name in self._focused_workers

    def set_pending_proposals_check(self, callback: Callable[[], bool] | None) -> None:
        """Register callback to check if pending proposals exist."""
        self._pending_proposals_check = callback
        self._directives._pending_proposals_check = callback
        self._task_lifecycle._pending_proposals_check = callback

    def set_pending_proposals_for_worker(self, callback: Callable[[str], bool] | None) -> None:
        """Register callback to check if a specific worker has pending proposals."""
        self._pending_proposals_for_worker = callback
        self._task_lifecycle._pending_proposals_for_worker = callback

    def set_poll_intervals(self, base: float, max_val: float) -> None:
        """Update polling intervals without restarting the poll loop."""
        self._base_interval = base
        self._max_interval = max_val

    def set_emit_decisions(self, enabled: bool) -> None:
        """Enable/disable emission of drone_decision events (for test mode)."""
        self._decision_exec.set_emit_decisions(enabled)

    def set_auto_complete_idle(self, seconds: float) -> None:
        """Override the minimum idle time before proposing task completion."""
        self._task_lifecycle.set_auto_complete_idle(seconds)

    def mark_completion_seen(self) -> None:
        """Signal that a task completion occurred during this pilot session."""
        self._task_lifecycle.mark_completion_seen()

    def set_oversight(self, monitor: OversightMonitor) -> None:
        """Set the oversight monitor."""
        self._oversight = monitor
        self._oversight_handler.set_oversight(monitor)

    def set_idle_nudge_sender(
        self,
        send_to_worker: Callable[..., Awaitable[None]],
        *,
        rate_limit_check: Callable[[str], bool] | None = None,
        message_store: Any | None = None,
        blocker_store: Any | None = None,
        mcp_activity_lookup: Callable[[str], float | None] | None = None,
        daemon_start_time: float | None = None,
        interrupt_worker: Callable[[str], Awaitable[None]] | None = None,
        spawn_handoff_task: Callable[[str, Any], Awaitable[bool]] | None = None,
        escalate_to_operator: Callable[[str, str], None] | None = None,
    ) -> None:
        """Wire both idle-watcher and inter-worker-watcher callbacks.

        Called by the daemon once it has a live ``send_to_worker`` — until
        then both watchers use a no-op sender so sweeps are safe but
        produce no PTY traffic. ``message_store`` feeds the
        :class:`InterWorkerMessageWatcher` (task #235 Phase 3) AND the
        idle-watcher's blocker auto-clear (task #250 — "new message
        lands in inbox" trigger). ``blocker_store`` enables the idle-
        watcher's reported-blocker skip.  ``mcp_activity_lookup`` +
        ``daemon_start_time`` enable the idle-watcher's MCP tools-
        dropped recovery path (task #257 — inject ``/mcp`` into a
        worker whose client registry is stale after a daemon reload).
        When any of these are None, the corresponding feature stays off
        but sweeps still run.
        """
        message_has_newer: Callable[[str, float], bool] | None = None
        if message_store is not None:

            def _newer(worker: str, since_ts: float) -> bool:
                try:
                    return any(m.created_at > since_ts for m in message_store.get_unread(worker))
                except Exception:
                    return False

            message_has_newer = _newer

        self.idle_watcher = IdleWatcher(
            drone_config=self._drone_config,
            task_board=self._task_board,
            drone_log=self.log,
            send_to_worker=send_to_worker,
            rate_limit_check=rate_limit_check,
            blocker_store=blocker_store,
            message_has_newer=message_has_newer,
            mcp_activity_lookup=mcp_activity_lookup,
            daemon_start_time=daemon_start_time,
            escalate_to_operator=escalate_to_operator,
            worker_busy_check=self._worker_busy,
        )
        self.inter_worker_watcher = InterWorkerMessageWatcher(
            drone_config=self._drone_config,
            message_store=message_store,
            drone_log=self.log,
            send_to_worker=send_to_worker,
            rate_limit_check=rate_limit_check,
            task_board=self._task_board,
            spawn_handoff_task=spawn_handoff_task,
            escalate_to_operator=escalate_to_operator,
        )
        self.context_pressure_watcher = ContextPressureWatcher(
            drone_config=self._drone_config,
            drone_log=self.log,
            send_to_worker=send_to_worker,
            interrupt_worker=interrupt_worker or _noop_interrupt,
        )

    def set_dreamer_stores(
        self,
        *,
        buzz_store: Any,
        learnings_store: Any,
    ) -> None:
        """Rebind the Dreamer's read sources once the daemon has them.

        Mirrors the ``set_idle_nudge_sender`` bootstrap pattern: the
        Dreamer is constructed eagerly with ``None`` stores so the
        attribute always exists, then this setter swaps in live
        instances after the daemon spins them up. Sweeps stay no-op
        while either store is ``None`` (see
        :attr:`Dreamer.enabled`).
        """
        self.dreamer = Dreamer(
            drone_config=self._drone_config,
            buzz_store=buzz_store,
            learnings_store=learnings_store,
            drone_log=self.log,
        )

    # --- Delegate to WorkerStateTracker (kept: load-bearing pilot public API) ---

    def mark_operator_continue(self, name: str) -> None:
        """Record that the operator continued this worker via the dashboard button."""
        self._state_tracker.mark_operator_continue(name)

    def wake_worker(self, name: str) -> bool:
        """Wake a suspended worker so it's polled on the next tick."""
        return self._state_tracker.wake_worker(name)

    # --- Delegate to TaskLifecycle (kept: task_manager service facade) ---

    def clear_proposed_completion(self, task_id: str) -> None:
        """Remove a task from the proposed-completions tracker.

        Kept as a pilot facade method because :class:`TaskManager` takes
        ``pilot`` as a constructor dependency and uses
        ``MagicMock(spec=DronePilot)`` in tests. Reaching into
        ``pilot._task_lifecycle`` from another service would leak the
        pilot's sub-handler structure into its consumers and break
        spec-based mocks. The other ``_task_lifecycle`` delegations have
        no service-side callers and were removed.
        """
        self._task_lifecycle.clear_proposed_completion(task_id)

    # --- Delegate to DecisionExecutor (kept: late-binding callback) ---

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
        """Execute *coro* for *worker*, log on success, warn on failure."""
        return await self._decision_exec._safe_worker_action(
            worker,
            coro,
            action,
            decision,
            include_rule_pattern=include_rule_pattern,
            reason=reason,
            prompt_snippet=prompt_snippet,
        )

    # --- Park-rejection routing (oversight has the suppression window) ---

    def note_park_rejected(self, worker_name: str, task_id: str) -> None:
        """Operator rejected a park proposal — tell oversight to back off
        re-proposing park for this (worker, task) for the configured
        window (and reset its no-progress streak)."""
        if self._oversight is not None:
            self._oversight.note_park_rejected(worker_name, task_id)

    # --- Lifecycle / event registration ---

    def clear_escalation(self, worker_name: str) -> None:
        """Remove a worker from the escalation tracker."""
        self._escalated.pop(worker_name, None)

    def on_proposal(self, callback: ProposalCallback) -> None:
        """Register callback for when the Queen proposes an assignment."""
        self.on("proposal", callback)

    def on_escalate(self, callback: EscalateCallback) -> None:
        """Register callback for escalation events."""
        self.on("escalate", callback)

    def on_workers_changed(self, callback: VoidCallback) -> None:
        """Register callback for when workers list changes (add/remove)."""
        self.on("workers_changed", callback)

    def on_task_assigned(self, callback: TaskAssignedCallback) -> None:
        """Register callback for when a task is auto-assigned to a worker."""
        self.on("task_assigned", callback)

    def on_task_done(self, callback: TaskDoneCallback) -> None:
        """Register callback for when a task appears complete."""
        self.on("task_done", callback)

    def on_state_changed(self, callback: WorkerCallback) -> None:
        """Register callback for any worker state change."""
        self.on("state_changed", callback)

    def on_hive_empty(self, callback: VoidCallback) -> None:
        """Register callback for when all workers are gone."""
        self.on("hive_empty", callback)

    def on_hive_complete(self, callback: VoidCallback) -> None:
        """Register callback for when all tasks are done and workers idle."""
        self.on("hive_complete", callback)

    def is_loop_running(self) -> bool:
        """Check if the pilot poll loop task is currently executing."""
        dispatcher = self._dispatcher
        return dispatcher._running and dispatcher._task is not None and not dispatcher._task.done()

    def needs_restart(self) -> bool:
        """True when the pilot should be running but the loop task has died."""
        return self._dispatcher._running and not self.is_loop_running()

    async def restart_loop(self) -> None:
        """Restart the poll loop task. Safe to call if already running."""
        task = self._dispatcher._task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._dispatcher.start()

    def start(self) -> None:
        self.enabled = True
        self._dispatcher.start()

    def stop(self) -> None:
        """Fully stop the pilot — kills the poll loop."""
        self.enabled = False
        self._dispatcher.stop()

    def toggle(self) -> bool:
        """Toggle drone actions on/off. State detection keeps running."""
        self.enabled = not self.enabled
        # Ensure the poll loop is alive even when drones are disabled
        if self._dispatcher._task is None or self._dispatcher._task.done():
            self._dispatcher.start()
        return self.enabled

    async def poll_once(self) -> bool:
        """Run one poll cycle across all workers."""
        return await self._dispatcher.poll_once()
