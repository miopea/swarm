"""ContextRecoveryDetector — tiered recovery for context-window errors.

Extracted from :class:`~swarm.drones.state_tracker.WorkerStateTracker`
(Phase 2 of ``docs/specs/state-tracker-refactor.md``).  Detects the
specific error strings Claude Code emits when it refuses a prompt for
size, and walks three escalation tiers per worker:

* **Tier 1** — inject ``/compact`` via a deferred action on the shared
  ``DecisionExecutor``.  The worker is marked ``compacting=True`` so
  subsequent polls don't stack additional ``/compact``s on top.
* **Tier 2** — REVIVE the worker with a context summary.
* **Tier 3** — escalate to the operator via the ``escalate`` event and
  reset the recovery counter.

The counter lives on ``Worker.recovery_attempts`` (worker-side state,
not detector-local) so it persists across detector lifetimes and is
visible to the dashboard.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from swarm.drones.log import LogCategory, SystemAction

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.drones.decision_executor import DecisionExecutor
    from swarm.drones.log import DroneLog
    from swarm.worker.worker import Worker


# Tightened from ``"context window"`` — that bare phrase was too loose
# (common in normal chats about LLMs). Now we require the full error
# strings Claude Code emits when it actually refuses a prompt for size.
_RE_CONTEXT_ERROR = re.compile(
    r"prompt is too long"
    r"|context window (?:is full|exceeded|limit reached)"
    r"|maximum context length"
    r"|token limit exceeded",
    re.IGNORECASE,
)


class ContextRecoveryDetector:
    """Scan PTY output for context-window errors; walk recovery tiers."""

    def __init__(
        self,
        log: DroneLog,
        decision_executor: DecisionExecutor,
        emit: Callable[..., None],
    ) -> None:
        self._log = log
        self._decision_executor = decision_executor
        self._emit = emit

    def check(self, worker: Worker, content: str) -> None:
        from swarm.worker.worker import WorkerState

        if worker.state != WorkerState.BUZZING:
            # Reset the per-worker counter when the worker isn't actively
            # in a turn — a state change generally means whatever was
            # happening when the error appeared has resolved.
            if worker.recovery_attempts > 0:
                worker.recovery_attempts = 0
            return
        if not _RE_CONTEXT_ERROR.search(content):
            return
        # Already compacting (either via pct-threshold or a prior tier-1)?
        # Don't stack another /compact on top — that's how we ended up
        # with six queued /compacts in the worker's pending-message
        # buffer when auto mode was still processing the previous turn.
        if worker.compacting:
            return
        worker.recovery_attempts += 1

        if worker.recovery_attempts == 1:
            # Tier 1: inject /compact
            self._log.add(
                SystemAction.QUEEN_BLOCKED,
                worker.name,
                "context error — tier 1 recovery: injecting /compact",
                category=LogCategory.DRONE,
            )
            worker.compacting = True
            self._decision_executor._deferred_actions.append(
                ("compact", worker, None, worker.state, worker.process)
            )
        elif worker.recovery_attempts == 2:
            # Tier 2: revive with context summary
            self._log.add(
                SystemAction.QUEEN_BLOCKED,
                worker.name,
                "context error — tier 2 recovery: reviving with context",
                category=LogCategory.DRONE,
            )
            from swarm.drones.rules import Decision, DroneDecision

            decision = DroneDecision(Decision.REVIVE, "context recovery tier 2")
            self._decision_executor._deferred_actions.append(
                ("revive", worker, decision, worker.state, worker.process)
            )
        else:
            # Tier 3: escalate
            self._log.add(
                SystemAction.QUEEN_BLOCKED,
                worker.name,
                "context error — recovery failed, escalating",
                category=LogCategory.DRONE,
                is_notification=True,
            )
            self._emit("escalate", worker, "context recovery failed after 2 attempts")
            worker.recovery_attempts = 0
