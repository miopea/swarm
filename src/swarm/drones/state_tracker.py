"""WorkerStateTracker — worker state classification, tracking, and polling.

Per-worker health detectors (context files, diminishing returns, rate
limits) live in :mod:`swarm.drones.detectors` and are passed in via
:class:`~swarm.drones.detectors.WorkerHealthDetectors`.  See
``docs/specs/state-tracker-refactor.md`` for the staged extraction
plan.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from swarm.drones.log import DroneAction, LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.pty.process import ProcessError
from swarm.tasks.task import TaskStatus
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from swarm.config import DroneConfig
    from swarm.drones.decision_executor import DecisionExecutor
    from swarm.drones.detectors import WorkerHealthDetectors
    from swarm.drones.log import DroneLog
    from swarm.providers import LLMProvider
    from swarm.providers.styled import StyledContent
    from swarm.tasks.board import TaskBoard

_log = get_logger("drones.state_tracker")

# classify_worker_output examines <=30 lines; 35 gives margin for context.
_STATE_DETECT_LINES = 35

# Commands that should never be pre-populated as suggested approval patterns.
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

# Wrapper commands -- include the next word to form the pattern
_WRAPPER_CMDS = frozenset({"uv", "npx", "bunx", "pipx", "nix"})


def _build_safe_pattern(words: list[str]) -> str:
    """Build a safe, specific approval pattern from command words."""
    if not words:
        return ""
    root = words[0]
    root_base = root.split(".")[0]
    if root in _DANGEROUS_CMDS or root_base in _DANGEROUS_CMDS:
        return ""
    if root in _WRAPPER_CMDS and len(words) >= 3 and words[1] == "run":
        key = " ".join(words[:3])
    elif len(words) >= 2:
        if words[1] in _DANGEROUS_CMDS:
            return ""
        key = " ".join(words[:2])
    else:
        key = root
    return r"\b" + re.escape(key) + r"\b"


class WorkerStateTracker:
    """Tracks worker state, classifies output, handles state transitions and polling.

    Extracted from :class:`~swarm.drones.pilot.DronePilot` to reduce
    pilot.py complexity.
    """

    def __init__(
        self,
        workers: list[Worker],
        log: DroneLog,
        task_board: TaskBoard | None,
        drone_config: DroneConfig,
        get_provider: Callable[[Worker], LLMProvider],
        emit: Callable[..., None],
        decision_executor: DecisionExecutor,
        prev_states: dict[str, WorkerState],
        idle_consecutive: dict[str, int],
        escalated: dict[str, float],
        suspended: set[str],
        suspended_at: dict[str, float],
        focused_workers: set[str],
        revive_history: dict[str, list[float]],
        detectors: WorkerHealthDetectors,
    ) -> None:
        self.workers = workers
        self.log = log
        self.task_board = task_board
        self.drone_config = drone_config
        self._get_provider = get_provider
        self._emit = emit
        self._decision_executor = decision_executor
        self._prev_states = prev_states
        self._idle_consecutive = idle_consecutive
        self._escalated = escalated
        self._suspended = suspended
        self._suspended_at = suspended_at
        self._focused_workers = focused_workers
        self._revive_history = revive_history
        self._detectors = detectors
        # Content fingerprinting: hash of last 5 lines to detect unchanged output
        self._content_fingerprints: dict[str, int] = {}
        self._unchanged_streak: dict[str, int] = {}
        # Suspension safety-net poll interval
        self._suspend_safety_interval: float = 60.0
        # Per-worker last full-poll timestamp (for sleeping worker throttling)
        self._last_full_poll: dict[str, float] = {}
        # Terminal-approval detection: track who continued a WAITING worker
        self._waiting_content: dict[str, str] = {}
        self._drone_continued: set[str] = set()
        self._operator_continued: set[str] = set()
        # Track whether any worker transitioned TO BUZZING this tick.
        self._any_became_active: bool = False
        # Event-driven assign: set when a worker transitions to RESTING
        self._needs_assign_check: bool = False

    @property
    def any_became_active(self) -> bool:
        """Whether any worker transitioned to BUZZING this tick."""
        return self._any_became_active

    @any_became_active.setter
    def any_became_active(self, value: bool) -> None:
        self._any_became_active = value

    @property
    def needs_assign_check(self) -> bool:
        """Whether an assign check is needed."""
        return self._needs_assign_check

    @needs_assign_check.setter
    def needs_assign_check(self, value: bool) -> None:
        self._needs_assign_check = value

    def mark_operator_continue(self, name: str) -> None:
        """Record that the operator continued this worker via the dashboard button."""
        self._operator_continued.add(name)

    def mark_drone_continued(self, name: str) -> None:
        """Record that the drone continued this worker."""
        self._drone_continued.add(name)

    def wake_worker(self, name: str) -> bool:
        """Wake a suspended worker so it's polled on the next tick.

        Returns ``True`` if the worker was actually suspended.
        """
        if name not in self._suspended:
            return False
        self._suspended.discard(name)
        self._suspended_at.pop(name, None)
        # Clear content fingerprint + unchanged streak to force full classify
        self._content_fingerprints.pop(name, None)
        self._unchanged_streak.pop(name, None)
        _log.info("woke suspended worker: %s", name)
        return True

    def _maybe_suspend_worker(self, worker: Worker) -> None:
        """Suspend a sleeping worker if it has been unchanged long enough."""
        if worker.display_state != WorkerState.SLEEPING:
            return
        if worker.name in self._focused_workers:
            return
        if self._unchanged_streak.get(worker.name, 0) < 3:
            return
        if worker.name in self._suspended:
            return
        self._suspended.add(worker.name)
        self._suspended_at[worker.name] = time.time()
        _log.info("suspended sleeping worker: %s", worker.name)

    def _classify_worker_state(
        self,
        worker: Worker,
        cmd: str,
        content: str,
        styled: StyledContent | None = None,
    ) -> tuple[WorkerState, list[Any] | None]:
        """Classify worker output into a state, with exception safety."""
        try:
            provider = self._get_provider(worker)
            if styled is not None:
                new_state, events = provider.classify_styled_with_events(cmd, styled)
            else:
                new_state, events = provider.classify_with_events(cmd, content)
        except Exception:
            _log.warning(
                "classify_output failed for %s -- keeping previous state",
                worker.name,
                exc_info=True,
            )
            return worker.state, None
        # Shell fallback: CLI exited but the wrapper shell is still alive.
        proc = worker.process
        if new_state == WorkerState.STUNG and proc and proc.is_alive:
            new_state = WorkerState.RESTING
        return new_state, events

    def _sync_display_state(self, worker: Worker, state_changed: bool) -> bool:
        """Emit state_changed for display-only transitions (e.g. RESTING->SLEEPING)."""
        display_val = worker.display_state.value
        prev_display = self._prev_states.get(f"_display_{worker.name}")
        if prev_display != display_val:
            self._prev_states[f"_display_{worker.name}"] = display_val
            if not state_changed:
                self._emit("state_changed", worker)
                state_changed = True
        return state_changed

    def _track_idle(self, worker: Worker) -> None:
        """Update per-worker idle-consecutive counter."""
        if worker.state == WorkerState.RESTING:
            self._idle_consecutive[worker.name] = self._idle_consecutive.get(worker.name, 0) + 1
        else:
            self._idle_consecutive.pop(worker.name, None)

    def _handle_state_change(self, worker: Worker, prev: WorkerState) -> tuple[bool, bool]:
        """Process a worker state change. Returns (transitioned_to_resting, state_changed)."""
        self._emit("state_changed", worker)
        # Task #233: buzz-log every state transition with PTY signal
        # context so mis-classifications (e.g. "RESTING while demonstrably
        # BUZZING") leave a diagnostic trail instead of requiring a live
        # operator to catch them in the act.
        self._log_state_transition(worker, prev)
        # Wake from suspension on any real state transition
        self.wake_worker(worker.name)
        # Clear escalation spam counter on state change
        self._decision_executor.clear_escalation_spam(worker.name)
        # Clear escalation tracking when worker leaves WAITING
        if prev == WorkerState.WAITING and worker.state in (
            WorkerState.RESTING,
            WorkerState.BUZZING,
        ):
            self._escalated.pop(worker.name, None)
        # Track who approved a WAITING worker (terminal vs drone vs button)
        self._handle_waiting_exit(worker, prev)
        if worker.state == WorkerState.BUZZING:
            self._any_became_active = True
            # Clear speculation flag when worker starts working
            worker.speculating_task_id = None
            # Transition assigned tasks to IN_PROGRESS
            if self.task_board:
                for task in self.task_board.all_tasks:
                    if task.assigned_worker == worker.name and task.status == TaskStatus.ASSIGNED:
                        task.start()
                        self._emit("state_changed", worker)
        transitioned = False
        if prev == WorkerState.BUZZING and worker.state in (
            WorkerState.RESTING,
            WorkerState.WAITING,
        ):
            transitioned = True
            self._needs_assign_check = True
        return transitioned, True

    def _log_state_transition(self, worker: Worker, prev: WorkerState) -> None:
        """Record a state transition in the buzz log with diagnostic context.

        Task #233: previously the only trace of a state change was the
        ``state_changed`` event, which is consumed by the dashboard and
        then gone. When a worker misclassified (showed RESTING while
        mid-Bash), reconstructing why required replaying the PTY by
        hand. The buzz entry now captures every transition with the
        specific PTY signals the classifier saw:
          * ``esc_to_interrupt`` — whether the "esc to interrupt"
            indicator was present in the tail at classify time.
          * ``pty_delta_30s`` — approximate PTY byte churn from the
            rollup counter; 0 on a window with no output.
          * ``streak`` — unchanged-content streak (the fingerprint
            counter that gates the RESTING short-circuit).
          * ``suspended`` — was the worker suspended before the
            transition fired (pressure cycle guard).
        """
        proc = worker.process
        # Pull a tight PTY tail (cheap — already in the ring buffer) so
        # we can report whether the classifier's key signal was visible.
        try:
            tail = proc.get_content(5) if proc else ""
        except Exception:
            tail = ""
        esc_visible = "esc to interrupt" in tail
        streak = self._unchanged_streak.get(worker.name, 0)
        suspended = worker.name in self._suspended
        pty_delta = (
            getattr(proc, "_trace_bytes_in", 0) + getattr(proc, "_trace_bytes_input", 0)
            if proc
            else 0
        )
        self.log.add(
            SystemAction.STATE_TRANSITION,
            worker.name,
            f"{prev.value} → {worker.state.value}",
            category=LogCategory.SYSTEM,
            metadata={
                "from": prev.value,
                "to": worker.state.value,
                "esc_to_interrupt": esc_visible,
                "pty_delta_bytes": pty_delta,
                "unchanged_streak": streak,
                "suspended": suspended,
            },
        )

    def _handle_waiting_exit(self, worker: Worker, prev: WorkerState) -> None:
        """Detect who approved a WAITING worker and clean up cached content."""
        if prev != WorkerState.WAITING:
            return
        if worker.state == WorkerState.BUZZING:
            if worker.name in self._drone_continued:
                self._drone_continued.discard(worker.name)
            else:
                # Both terminal and button approvals get the banner
                self._operator_continued.discard(worker.name)
                self._detect_operator_terminal_approval(worker)
        self._waiting_content.pop(worker.name, None)

    def _detect_operator_terminal_approval(self, worker: Worker) -> None:
        """Emit an event when the operator approved a prompt via the terminal."""
        from swarm.drones.pilot import extract_prompt_snippet

        cached = self._waiting_content.get(worker.name, "")
        if not cached:
            return

        provider = self._get_provider(worker)

        # Plans and user questions should never be automated
        if provider.has_plan_prompt(cached) or provider.is_user_question(cached):
            return

        if provider.has_choice_prompt(cached):
            prompt_type = "choice"
            summary = provider.get_choice_summary(cached) or "choice prompt"
        elif provider.has_accept_edits_prompt(cached):
            prompt_type = "accept_edits"
            summary = "accept edits"
        else:
            return  # Unknown/idle prompt -- not actionable

        pattern = self._suggest_approval_pattern(cached, provider)
        snippet = extract_prompt_snippet(cached)

        self.log.add(
            DroneAction.OPERATOR,
            worker.name,
            f"terminal approval: {summary}",
            category=LogCategory.OPERATOR,
            metadata={"prompt_snippet": snippet} if snippet else {},
        )

        self._emit("operator_terminal_approval", worker, summary, prompt_type, pattern, snippet)

    @staticmethod
    def _suggest_approval_pattern(content: str, provider: LLMProvider) -> str:
        """Extract a suggested regex pattern from the raw PTY content.

        Scans the tail of the terminal buffer for recognisable tool-call
        patterns (old ``Bash(cmd ...)`` and new ``Bash command\\n  cmd ...``
        formats) and for ``accept edits`` prompts.

        Returns a more specific multi-word pattern (e.g. ``\\bnpm test\\b``
        instead of ``\\bnpm\\b``) and returns ``""`` for dangerous commands
        so the modal opens empty and the user must type deliberately.
        """
        lines = content.strip().splitlines()
        tail = "\n".join(lines[-25:])

        # Old format: Bash(npm test --coverage)
        m = re.search(r"(Bash)\((.+?)[\)\n]", tail)
        if m:
            words = m.group(2).strip().split()
            if words:
                pattern = _build_safe_pattern(words)
                return pattern

        # New format: "Bash command\n  npm test ..."
        m = re.search(r"Bash command\s*\n\s*(.+)", tail)
        if m:
            words = m.group(1).strip().split()
            if words:
                return _build_safe_pattern(words)

        # Accept-edits prompt: ">> accept edits on 3 files"
        if re.search(r">>\s*accept edits", tail, re.IGNORECASE):
            return "accept edits"

        # Fallback: extract tool name from the choice question line
        summary = provider.get_choice_summary(content)
        if summary:
            # Match tool names like "Edit", "Write", "NotebookEdit"
            m = re.search(r"\b(Edit|Write|NotebookEdit|Bash)\b", summary)
            if m:
                return r"\b" + re.escape(m.group(1)) + r"\b"

        return ""

    def _should_throttle_sleeping(self, worker: Worker, now: float | None = None) -> bool:
        """Check if a sleeping worker's full poll should be skipped (throttled)."""
        if worker.display_state != WorkerState.SLEEPING:
            return False
        if worker.name in self._focused_workers:
            return False
        last = self._last_full_poll.get(worker.name, 0.0)
        return (now or time.time()) - last < self.drone_config.sleeping_poll_interval

    def _update_content_fingerprint(self, name: str, content: str) -> None:
        """Update content fingerprint and unchanged streak for a worker."""
        fp = format(hash(content[-200:]), "x") if content else ""
        if fp == self._content_fingerprints.get(name):
            self._unchanged_streak[name] = self._unchanged_streak.get(name, 0) + 1
        else:
            self._unchanged_streak[name] = 0
        self._content_fingerprints[name] = fp

    # -- Task #236: stuck-BUZZING safety net --
    # Conservative floor — the operator observed workers stuck BUZZING
    # for 90+ minutes post-deploy. 10 minutes is long enough to avoid
    # racing legitimate long-running turns (Playwright installs, heavy
    # verification runs) but short enough that the dashboard isn't
    # misleading for an hour+.
    _STUCK_BUZZING_THRESHOLD: float = 600.0

    def _has_active_turn_signal(self, content: str) -> bool:
        """Narrow check: does the PTY tail prove the worker is mid-turn?

        Only inspects the last ``TAIL_NARROW`` lines — the active-turn
        indicators are always on the bottom of Claude Code's TUI,
        whereas stale subagent / background-work patterns tend to drift
        higher in the scrollback once their turn completes. Checking the
        narrow tail intentionally rejects those stale matches.
        """
        from swarm.providers.base import TAIL_NARROW
        from swarm.providers.claude import _RE_BACKGROUND_RUNNING, _RE_SUBAGENT_ACTIVE

        if not content:
            return False
        tail = "\n".join(content.strip().splitlines()[-TAIL_NARROW:])
        if "esc to interrupt" in tail:
            return True
        if _RE_BACKGROUND_RUNNING.search(tail):
            return True
        if _RE_SUBAGENT_ACTIVE.search(tail):
            return True
        return False

    def _check_context_pressure(self, worker: Worker) -> None:
        """Warn or inject /compact when context fill exceeds thresholds."""
        if worker.state != WorkerState.BUZZING or worker.compacting:
            return
        pct = worker.context_pct
        if pct <= 0:
            return

        cfg = self.drone_config
        critical = cfg.context_critical_threshold
        warning = cfg.context_warning_threshold

        if pct >= critical:
            # Inject /compact via deferred action
            from swarm.drones.log import LogCategory, SystemAction

            self.log.add(
                SystemAction.QUEEN_BLOCKED,
                worker.name,
                f"context critical ({pct:.0%}) — injecting /compact",
                category=LogCategory.DRONE,
            )
            worker.compacting = True
            self._decision_executor._deferred_actions.append(
                ("compact", worker, None, worker.state, worker.process)
            )
        elif pct >= warning and not worker._context_warned:
            from swarm.drones.log import LogCategory, SystemAction

            self.log.add(
                SystemAction.QUEEN_BLOCKED,
                worker.name,
                f"context warning ({pct:.0%}) — approaching limit",
                category=LogCategory.DRONE,
            )
            worker._context_warned = True

    def _poll_sleeping_throttled(self, worker: Worker, cmd: str) -> tuple[bool, bool] | None:
        """Lightweight poll for throttled sleeping workers.

        Returns ``(had_action, state_changed)`` if handled, or ``None`` to
        fall through to full poll.
        """
        from swarm.providers.styled import StyledContent

        if not self._should_throttle_sleeping(worker):
            return None
        proc = worker.process
        if proc:
            content, styled_rows = proc.get_styled_content(5)
        else:
            content, styled_rows = "", []
        styled = StyledContent(text=content, rows=styled_rows)
        new_state = self._get_provider(worker).classify_styled_output(cmd, styled)
        if new_state in (WorkerState.WAITING, WorkerState.BUZZING):
            return None  # State changed -- fall through to full poll
        self._update_content_fingerprint(worker.name, content)
        state_changed = self._sync_display_state(worker, False)
        self._maybe_suspend_worker(worker)
        return False, state_changed

    def _poll_dead_worker(
        self,
        worker: Worker,
        dead_workers: list[Worker],
    ) -> tuple[bool, bool, bool]:
        """Handle polling for a worker whose process is dead or missing."""
        if worker.state == WorkerState.STUNG:
            if worker.state_duration >= worker.stung_reap_timeout:
                _log.info("reaping stung worker %s (%.0fs)", worker.name, worker.state_duration)
                dead_workers.append(worker)
                return True, False, False
            # Run decision engine on STUNG worker (may trigger REVIVE)
            proc = worker.process
            content = proc.get_content(_STATE_DETECT_LINES) if proc else ""
            had_action = self._decision_executor._run_decision_sync(worker, content)
            return had_action, False, False
        # Process confirmed dead -- force STUNG (bypass hysteresis)
        _log.info("process gone for worker %s -- marking STUNG", worker.name)
        worker.force_state(WorkerState.STUNG)
        self._emit("state_changed", worker)
        self._sync_display_state(worker, True)
        return True, False, True

    def _poll_single_worker(
        self,
        worker: Worker,
        dead_workers: list[Worker],
        now: float | None = None,
        enabled: bool = True,
    ) -> tuple[bool, bool, bool]:
        """Poll one worker. Returns (had_action, transitioned_to_resting, state_changed)."""
        from swarm.providers.styled import StyledContent

        had_action = False
        transitioned = False
        state_changed = False

        proc = worker.process
        if not proc or not proc.is_alive:
            return self._poll_dead_worker(worker, dead_workers)

        cmd = proc.get_child_foreground_command()

        # Throttle sleeping workers: lightweight state check instead of full poll
        throttle_result = self._poll_sleeping_throttled(worker, cmd)
        if throttle_result is not None:
            return False, False, throttle_result[1]
        if worker.display_state == WorkerState.SLEEPING:
            self._last_full_poll[worker.name] = now or time.time()

        try:
            content, styled_rows = proc.get_styled_content(_STATE_DETECT_LINES)
        except (ProcessError, OSError):
            raise  # let circuit breaker in _poll_once_locked handle these

        styled = StyledContent(text=content, rows=styled_rows)

        # Content fingerprinting: when a RESTING worker's output hasn't
        # changed for 3+ consecutive polls, skip classify + decide.
        self._update_content_fingerprint(worker.name, content)

        if worker.state == WorkerState.RESTING and self._unchanged_streak.get(worker.name, 0) >= 3:
            state_changed = self._sync_display_state(worker, False)
            # Clear poll failures on successful read
            return False, False, state_changed

        new_state, events = self._classify_worker_state(worker, cmd, content, styled=styled)
        # Task #236: stuck-BUZZING safety net. If the classifier says
        # BUZZING but the narrow PTY tail has none of the actual
        # "mid-turn" signals (esc-to-interrupt, spinner, monitor), and
        # the worker has already been BUZZING past
        # ``_STUCK_BUZZING_THRESHOLD``, flip the call to RESTING. This
        # catches the "stuck BUZZING for 90+ min at ❯ prompt" failure
        # mode observed after pressure oscillation, where a stale
        # scrollback pattern (e.g. a recently-completed subagent's
        # ``↓ N tokens`` line) keeps matching the active-turn regex.
        if (
            new_state == WorkerState.BUZZING
            and worker.state == WorkerState.BUZZING
            and worker.state_duration >= self._STUCK_BUZZING_THRESHOLD
            and not self._has_active_turn_signal(content)
        ):
            _log.info(
                "stuck-BUZZING safety net fired for %s after %.0fs — forcing RESTING",
                worker.name,
                worker.state_duration,
            )
            new_state = WorkerState.RESTING
        prev = self._prev_states.get(worker.name, worker.state)
        changed = worker.update_state(new_state)

        # Cache content while worker is WAITING (for terminal-approval detection)
        if worker.state == WorkerState.WAITING or new_state == WorkerState.WAITING:
            self._waiting_content[worker.name] = content

        if changed:
            transitioned, state_changed = self._handle_state_change(worker, prev)

        self._track_idle(worker)

        # Sync display_state -- handles RESTING->SLEEPING transitions
        state_changed = self._sync_display_state(worker, state_changed)

        self._prev_states[worker.name] = worker.state

        # Per-worker health detectors live in ``swarm.drones.detectors``;
        # ``_check_context_pressure`` is the last inline check pending
        # Phase 3 of the state-tracker-refactor spec.
        self._detectors.context_files.check(worker, content)
        self._detectors.diminishing.check(worker)
        self._check_context_pressure(worker)
        self._detectors.recovery.check(worker, content)
        self._detectors.rate_limit.check(worker, content)

        if self._decision_executor._should_skip_decide(worker, changed, enabled):
            return had_action, transitioned, state_changed

        had_action = self._decision_executor._run_decision_sync(worker, content, events=events)
        return had_action, transitioned, state_changed

    def _is_suspended_skip(self, worker: Worker, now: float | None = None) -> bool:
        """Return True if this worker should be skipped (suspended, safety-net not elapsed)."""
        if worker.name not in self._suspended:
            return False
        suspended_since = self._suspended_at.get(worker.name, 0.0)
        now = now or time.time()
        if now - suspended_since < self._suspend_safety_interval:
            return True
        # Safety-net: reset timer and fall through to normal poll
        self._suspended_at[worker.name] = now
        return False

    def cleanup_dead_worker(self, dw: Worker) -> None:
        """Remove tracking state for a dead worker."""
        self._prev_states.pop(dw.name, None)
        self._escalated.pop(dw.name, None)
        self._idle_consecutive.pop(dw.name, None)
        self._content_fingerprints.pop(dw.name, None)
        self._unchanged_streak.pop(dw.name, None)
        self._suspended.discard(dw.name)
        self._suspended_at.pop(dw.name, None)
        self._revive_history.pop(dw.name, None)
        self._last_full_poll.pop(dw.name, None)
        self._waiting_content.pop(dw.name, None)
        self._drone_continued.discard(dw.name)
        self._operator_continued.discard(dw.name)
        # Detectors with per-worker state need their own cleanup hook.
        self._detectors.rate_limit.forget(dw.name)
