"""Queen oversight — signal-triggered monitoring and intervention for workers."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from swarm.config import OversightConfig
from swarm.logging import get_logger
from swarm.worker.worker import WorkerState

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.queen.queen import Queen
    from swarm.tasks.board import TaskBoard
    from swarm.tasks.task import SwarmTask
    from swarm.worker.worker import Worker

_log = get_logger("queen.oversight")


class SignalType(Enum):
    PROLONGED_BUZZING = "prolonged_buzzing"
    TASK_DRIFT = "task_drift"
    RESOURCE_PRESSURE = "resource_pressure"


class Severity(Enum):
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


@dataclass
class OversightSignal:
    """A detected oversight signal requiring evaluation."""

    signal_type: SignalType
    worker_name: str
    description: str
    task_id: str = ""


@dataclass
class OversightResult:
    """Result of a Queen oversight evaluation."""

    signal: OversightSignal
    severity: Severity
    action: str  # "note", "redirect", "flag_human"
    message: str
    reasoning: str
    confidence: float = 0.0
    # For action=="redirect": a verbatim line from the task description that
    # the worker's PTY activity contradicts. Empty when missing or when the
    # action isn't a redirect. A redirect with no cited contradiction is
    # downgraded to ``note`` by ``evaluate_signal`` — surface-keyword
    # mismatch is not drift.
    cited_contradiction: str = ""


class OversightMonitor:
    """Monitors workers for oversight signals and coordinates Queen evaluation.

    Signals are detected via cheap heuristics (state duration, content checks).
    When a signal fires, the Queen is consulted for semantic analysis and the
    intervention severity is determined.
    """

    def __init__(self, config: OversightConfig) -> None:
        self._config = config
        self._call_timestamps: list[float] = []
        # Workers already flagged for prolonged buzzing (reset when state changes)
        self._buzzing_notified: set[str] = set()
        # Per-worker last drift check timestamp
        self._last_drift_check: dict[str, float] = {}
        # Track interventions for status reporting
        self._interventions: list[dict[str, Any]] = []
        # Auto-park (operator-blocked-stall guard): per (worker, task_id)
        # consecutive no-progress drift cycles, last-seen task.updated_at,
        # own cadence timer, and post-reject backoff.
        self._no_progress_streak: dict[tuple[str, str], int] = {}
        self._last_task_updated: dict[tuple[str, str], float] = {}
        self._last_stall_check: dict[str, float] = {}
        self._park_reject_backoff: dict[tuple[str, str], float] = {}

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def _within_rate_limit(self) -> bool:
        """Check if we can make another Queen oversight call this hour."""
        now = time.time()
        hour_ago = now - 3600
        self._call_timestamps = [t for t in self._call_timestamps if t > hour_ago]
        return len(self._call_timestamps) < self._config.max_calls_per_hour

    def _record_call(self) -> None:
        self._call_timestamps.append(time.time())

    def check_prolonged_buzzing(
        self, worker: Worker, task: SwarmTask | None, tool_active: bool = False
    ) -> OversightSignal | None:
        """Check if a worker has been BUZZING too long without progress.

        ``tool_active`` is ``True`` when the worker's PTY shows an in-flight
        long-running tool (background shell/monitor, active subagent, or a
        dynamic workflow). Such work legitimately holds the worker in BUZZING
        for the tool's whole duration, so we suppress the signal — firing it
        would burn a Queen oversight call and possibly inject a note into a
        worker that is making real progress. ``_buzzing_notified`` is left
        untouched so a genuine stall after the tool completes still fires.
        """
        threshold_s = self._config.buzzing_threshold_minutes * 60

        if worker.state != WorkerState.BUZZING:
            # Worker is no longer buzzing — clear the notification flag
            self._buzzing_notified.discard(worker.name)
            return None

        if tool_active:
            return None

        if worker.state_duration < threshold_s:
            return None

        # Already notified for this buzzing period
        if worker.name in self._buzzing_notified:
            return None

        self._buzzing_notified.add(worker.name)
        minutes = worker.state_duration / 60
        desc = f"Worker has been BUZZING for {minutes:.0f} minutes"
        if task:
            desc += f" on task '{task.title}'"

        return OversightSignal(
            signal_type=SignalType.PROLONGED_BUZZING,
            worker_name=worker.name,
            description=desc,
            task_id=task.id if task else "",
        )

    def check_task_drift(
        self,
        worker: Worker,
        task: SwarmTask | None,
        worker_output: str,
    ) -> OversightSignal | None:
        """Check if a worker may have drifted from its assigned task."""
        if not task or not worker_output:
            return None

        # Only check workers actively working
        if worker.state not in (WorkerState.BUZZING, WorkerState.RESTING):
            return None

        now = time.time()
        interval_s = self._config.drift_check_interval_minutes * 60
        last = self._last_drift_check.get(worker.name, 0.0)
        if now - last < interval_s:
            return None

        self._last_drift_check[worker.name] = now

        return OversightSignal(
            signal_type=SignalType.TASK_DRIFT,
            worker_name=worker.name,
            description=f"Periodic drift check for task '{task.title}'",
            task_id=task.id,
        )

    def check_resource_pressure(
        self,
        pressure_level: str,
        duration_seconds: float,
    ) -> OversightSignal | None:
        """Fire when HIGH/CRITICAL pressure persists for >2 minutes."""
        if pressure_level not in ("high", "critical"):
            return None
        if duration_seconds < 120.0:
            return None
        return OversightSignal(
            signal_type=SignalType.RESOURCE_PRESSURE,
            worker_name="",
            description=(
                f"System under {pressure_level} memory pressure for "
                f"{duration_seconds:.0f}s — consider redistributing workers"
            ),
        )

    def collect_signals(
        self,
        workers: list[Worker],
        task_board: TaskBoard | None,
        worker_outputs: dict[str, str] | None = None,
        is_long_running: Callable[[Worker, str], bool] | None = None,
    ) -> list[OversightSignal]:
        """Run all heuristic checks and return detected signals.

        ``is_long_running`` lets the caller (which owns the per-worker provider)
        report whether a worker's PTY shows an in-flight long-running tool, so
        prolonged-BUZZING is suppressed for it. Passing the provider predicate
        in keeps oversight provider-neutral (no CLI-specific imports here).
        """
        if not self.enabled:
            return []

        signals: list[OversightSignal] = []
        worker_outputs = worker_outputs or {}

        for worker in workers:
            task = None
            if task_board:
                active = task_board.active_tasks_for_worker(worker.name)
                task = active[0] if active else None

            output = worker_outputs.get(worker.name, "")

            # Signal 1: prolonged buzzing (suppressed while a long-running
            # tool — e.g. a dynamic workflow — legitimately holds BUZZING)
            tool_active = bool(is_long_running and is_long_running(worker, output))
            sig = self.check_prolonged_buzzing(worker, task, tool_active=tool_active)
            if sig:
                signals.append(sig)

            # Signal 2: task drift (only with task + output)
            sig = self.check_task_drift(worker, task, output)
            if sig:
                signals.append(sig)

        return signals

    async def evaluate_signal(
        self,
        signal: OversightSignal,
        queen: Queen,
        worker_output: str,
        task_info: str = "",
    ) -> OversightResult | None:
        """Ask the Queen to evaluate a signal and recommend an intervention.

        Returns None if rate-limited or Queen call fails.
        """
        if not self._within_rate_limit():
            _log.info(
                "oversight rate limited for %s (%s)",
                signal.worker_name,
                signal.signal_type.value,
            )
            return None

        prompt = self._build_evaluation_prompt(signal, worker_output, task_info)
        self._record_call()

        try:
            result = await queen.ask(prompt, stateless=True, force=True)
        except Exception:
            _log.warning("oversight Queen call failed for %s", signal.worker_name, exc_info=True)
            return None

        if not isinstance(result, dict) or "error" in result:
            _log.warning("oversight Queen returned error: %s", result)
            return None

        severity_str = result.get("severity", "minor")
        try:
            severity = Severity(severity_str)
        except ValueError:
            severity = Severity.MINOR

        action = result.get("action", "note")
        message = result.get("message", "")
        reasoning = result.get("reasoning", "")
        confidence = float(result.get("confidence", 0.0))
        cited_contradiction = str(result.get("cited_contradiction", "")).strip()

        # Contradiction-required redirect (task #340): a `redirect` without a
        # quoted, contradicted line from the task description is topical
        # mismatch — not drift. Downgrade to `note` so a periodic drift
        # signal can't interrupt a worker on a plausible vehicle for the
        # actual task (e.g. an admin endpoint exposing a backup feature).
        if action == "redirect" and not cited_contradiction:
            _log.info(
                "oversight redirect downgraded to note for %s — no cited contradiction",
                signal.worker_name,
            )
            action = "note"
            severity = Severity.MINOR

        oversight_result = OversightResult(
            signal=signal,
            severity=severity,
            action=action,
            message=message,
            reasoning=reasoning,
            confidence=confidence,
            cited_contradiction=cited_contradiction,
        )

        self._interventions.append(
            {
                "timestamp": time.time(),
                "worker": signal.worker_name,
                "signal": signal.signal_type.value,
                "severity": severity.value,
                "action": action,
                "message": message,
            }
        )
        # Keep only last 50 interventions
        if len(self._interventions) > 50:
            self._interventions = self._interventions[-50:]

        return oversight_result

    def _build_evaluation_prompt(
        self,
        signal: OversightSignal,
        worker_output: str,
        task_info: str,
    ) -> str:
        """Build the Queen prompt for evaluating an oversight signal."""
        task_section = ""
        if task_info:
            task_section = f"\n## Assigned Task\n{task_info}\n"

        signal_desc = {
            SignalType.PROLONGED_BUZZING: (
                "This worker has been actively processing (BUZZING) for an unusually "
                "long time without committing. They may be stuck, going in circles, "
                "or tackling an overly complex approach."
            ),
            SignalType.TASK_DRIFT: (
                "This worker may have drifted from their assigned task. Review their "
                "recent output to determine if they are still working on the correct "
                "objective or have gone off-track."
            ),
        }

        return f"""You are the Queen performing oversight on a worker.

## Signal Detected
Type: {signal.signal_type.value}
Worker: {signal.worker_name}
{signal.description}

{signal_desc.get(signal.signal_type, "")}
{task_section}
## Recent Worker Output
```
{worker_output[-3000:]}
```

Evaluate the situation and respond with ONLY a JSON object:
{{
  "severity": "minor" | "major" | "critical",
  "action": "note" | "redirect" | "flag_human",
  "message": "message to send to the worker or human operator",
  "reasoning": "why you chose this severity and action",
  "confidence": 0.0 to 1.0,
  "cited_contradiction": "verbatim line from task desc the PTY contradicts (required for redirect)"
}}

Severity guide:
- "minor": Worker is slightly off-track but recoverable with a gentle note
- "major": Worker has gone significantly off-track and needs redirection (pause + new instructions)
- "critical": Requires human attention (security concern, data loss risk, uncertainty)

Action guide:
- "note": Send a corrective note to the worker (minor issues)
- "redirect": Pause the worker and send redirect instructions (major issues)
- "flag_human": Flag for human review on the dashboard (critical issues)

REDIRECT REQUIRES A CITED CONTRADICTION:
A redirect interrupts the worker mid-flow and is reserved for clear drift —
not topical mismatch. To choose action="redirect" you MUST quote a specific
line from the assigned task description that the worker's current PTY
activity contradicts, and put it in the "cited_contradiction" field.

Surface-keyword divergence is NOT drift. Plausible vehicles for the task —
admin endpoints exposing a backup feature, refactors enabling the requested
behavior, maintenance routes touching the same subsystem — are routine.
If you cannot quote a contradicted line, choose "note" with an empty
"cited_contradiction" field. The caller will downgrade an uncited redirect
to a note automatically.

IMPORTANT: If the worker appears to be making genuine progress (even if slow),
use severity "minor" with action "note" and a supportive message. Only escalate
if there is clear evidence of being stuck or drifting."""

    def get_status(self) -> dict[str, Any]:
        """Return oversight monitor status for API/dashboard."""
        now = time.time()
        hour_ago = now - 3600
        recent_calls = [t for t in self._call_timestamps if t > hour_ago]

        return {
            "enabled": self._config.enabled,
            "calls_this_hour": len(recent_calls),
            "max_calls_per_hour": self._config.max_calls_per_hour,
            "buzzing_threshold_minutes": self._config.buzzing_threshold_minutes,
            "drift_check_interval_minutes": self._config.drift_check_interval_minutes,
            "buzzing_notified": sorted(self._buzzing_notified),
            "recent_interventions": self._interventions[-10:],
        }

    def note_park_rejected(self, worker_name: str, task_id: str) -> None:
        """Operator rejected a park proposal — back off re-proposing for
        ``(worker, task)`` for ``auto_park_reject_backoff_seconds`` and
        reset its no-progress streak."""
        key = (worker_name, task_id)
        self._park_reject_backoff[key] = time.time()
        self._no_progress_streak.pop(key, None)

    def collect_park_proposals(
        self,
        workers: list[Worker],
        task_board: TaskBoard | None,
    ) -> list[tuple[str, str, str]]:
        """Operator-blocked-stall guard. Returns ``(worker, task_id,
        reason)`` for each ACTIVE task that has shown NO progress
        (``task.updated_at`` frozen) across ``auto_park_no_progress_checks``
        consecutive drift-cadence checks — the signal that a worker is
        standing by on something blocked on the operator. Deterministic
        and Queen-free so it still fires during an oversight rate-limit
        storm (the #443 failure mode). The caller raises ONE park
        proposal per entry; ``has_pending_park`` dedupes while pending.
        """
        if not self._config.auto_park_enabled or task_board is None:
            return []
        now = time.time()
        interval_s = self._config.drift_check_interval_minutes * 60
        out: list[tuple[str, str, str]] = []
        for worker in workers:
            cand = self._park_candidate_for(worker, task_board, now, interval_s)
            if cand is not None:
                out.append(cand)
        return out

    def _park_candidate_for(
        self,
        worker: Worker,
        task_board: TaskBoard,
        now: float,
        interval_s: float,
    ) -> tuple[str, str, str] | None:
        from swarm.tasks.task import TaskStatus

        active = task_board.active_tasks_for_worker(worker.name)
        task = active[0] if active else None
        if task is None or task.status != TaskStatus.ACTIVE:
            # Not ACTIVE → cannot be an operator-blocked stall; forget any
            # streak so a later ACTIVE stint starts clean.
            for k in [k for k in self._no_progress_streak if k[0] == worker.name]:
                self._no_progress_streak.pop(k, None)
                self._last_task_updated.pop(k, None)
            return None
        # Own cadence (same rhythm as drift; independent timestamp).
        if now - self._last_stall_check.get(worker.name, 0.0) < interval_s:
            return None
        self._last_stall_check[worker.name] = now
        key = (worker.name, task.id)
        rej = self._park_reject_backoff.get(key)
        if rej is not None and now - rej < self._config.auto_park_reject_backoff_seconds:
            return None
        prev = self._last_task_updated.get(key)
        self._last_task_updated[key] = task.updated_at
        if prev is None or task.updated_at > prev:
            self._no_progress_streak[key] = 0  # progress (or first observation)
            return None
        streak = self._no_progress_streak.get(key, 0) + 1
        self._no_progress_streak[key] = streak
        if streak < self._config.auto_park_no_progress_checks:
            return None
        # Threshold crossed — propose park. Reset the streak; the pending
        # proposal (has_pending_park) is the steady-state dedupe, and a
        # reject arms the backoff.
        self._no_progress_streak[key] = 0
        mins = int(streak * interval_s / 60)
        reason = (
            f"No task progress across {streak} oversight checks (~{mins}m) while "
            f"{worker.name} idled on ACTIVE task '{task.title}' — looks blocked "
            "on the operator (not on another task)."
        )
        return (worker.name, task.id, reason)

    def reset_worker(self, worker_name: str) -> None:
        """Reset oversight state for a worker (e.g., after state change)."""
        self._buzzing_notified.discard(worker_name)
        self._last_drift_check.pop(worker_name, None)
        self._last_stall_check.pop(worker_name, None)
        for store in (
            self._no_progress_streak,
            self._last_task_updated,
            self._park_reject_backoff,
        ):
            for k in [k for k in store if k[0] == worker_name]:
                store.pop(k, None)
