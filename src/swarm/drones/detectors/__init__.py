"""Per-worker health detectors composed by WorkerStateTracker.

Each detector inspects a worker (typically with its current PTY content)
and acts on its own internal state — adding buzz-log entries, emitting
events, or queueing deferred actions on the shared DecisionExecutor.

This package is the home of the WorkerStateTracker refactor
(``docs/specs/state-tracker-refactor.md``). Phase 3 will add
:class:`ContextPressureCheck` to :class:`WorkerHealthDetectors`.
"""

from __future__ import annotations

from dataclasses import dataclass

from swarm.drones.detectors.context_files import ContextFileTracker
from swarm.drones.detectors.context_recovery import ContextRecoveryDetector
from swarm.drones.detectors.diminishing_returns import DiminishingReturnsDetector
from swarm.drones.detectors.rate_limit import RateLimitDetector

__all__ = [
    "ContextFileTracker",
    "ContextRecoveryDetector",
    "DiminishingReturnsDetector",
    "RateLimitDetector",
    "WorkerHealthDetectors",
]


@dataclass
class WorkerHealthDetectors:
    """Bundle of per-worker health detectors passed to WorkerStateTracker.

    Holder for the detectors composed inside
    :meth:`WorkerStateTracker._poll_single_worker`.  Construct once at
    pilot init and pass through.
    """

    context_files: ContextFileTracker
    diminishing: DiminishingReturnsDetector
    rate_limit: RateLimitDetector
    recovery: ContextRecoveryDetector
