"""Shared repeat-nudge guard for the idle / inter-worker watchers (task #546).

Both ``IdleWatcher`` and ``InterWorkerMessageWatcher`` poke RESTING/idle
workers that have outstanding work. The debounce (``_last_nudge`` +
``idle_nudge_debounce_seconds``) caps how *often* a worker is nudged but
never *stops* — so a worker idle on a task it cannot progress (e.g. a
shipped fix awaiting operator verification, or a genuinely-stuck worker
unaware it's looping) gets poked every debounce window forever, burning
tokens (the #543/#546 incident; cousin of the #529 ~$51 stale-blocker
burn).

This guard adds the missing terminal state: after ``max_repeats``
consecutive no-progress nudges (the worker's "fingerprint" — state +
outstanding-work signature — unchanged between nudges), it returns
ESCALATE once (caller surfaces a single operator-facing attention item
and stops poking), then SILENT until the fingerprint changes (the
worker made progress / the operator acted), at which point it resets
and resumes normal nudging.

Both failure modes — awaiting-verification and genuinely-stuck — resolve
to the same correct action: stop poking the worker, hand it to the
operator. The guard is fingerprint-agnostic: each watcher computes
whatever "did anything change?" signature fits its nudge trigger and
passes it in.
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from swarm.worker.worker import Worker

# Decision outcomes returned by :meth:`RepeatNudgeGuard.decide`.
NUDGE = "nudge"
ESCALATE = "escalate"
SILENT = "silent"


def operator_engaged(worker: Worker, window_seconds: float) -> bool:
    """True when the operator typed in ``worker``'s PTY within ``window_seconds``.

    Shared nudge-suppression signal for the idle-watcher (AUTO_NUDGE) and
    task-lifecycle (PROPOSED_COMPLETION) drones: neither should poke a
    worker the operator is actively driving. Mirrors the affinity-router's
    ``assign_operator_engagement_minutes`` window — the same evidence that
    routes new work away from an engaged worker should also silence nudges
    into it.

    Added after a 2026-06-11 incident where the idle-watcher fired an
    AUTO_NUDGE into d365-solutions while the operator was mid-keystroke in
    its PTY (the engagement signal already existed; the watcher just wasn't
    consulting it). Defensive by construction: a missing process, a
    disabled window (``<= 0``), or a raising provider all resolve to
    "not engaged" so the guard can never crash a sweep.
    """
    if window_seconds <= 0:
        return False
    proc = getattr(worker, "process", None)
    if proc is None:
        return False
    try:
        return bool(proc.operator_engaged_within(window_seconds))
    except Exception:
        return False


class RepeatNudgeGuard:
    """Per-key consecutive-no-progress nudge tracker.

    ``key`` is whatever the caller nudges on (worker name in both current
    callers). ``fingerprint`` is any hashable that changes when the
    worker has made progress worth re-nudging about. See module docstring.
    """

    def __init__(self) -> None:
        self._streak: dict[Hashable, int] = {}
        self._fingerprint: dict[Hashable, Hashable] = {}
        self._escalated: set[Hashable] = set()

    def decide(self, key: Hashable, fingerprint: Hashable, *, max_repeats: int) -> str:
        """Record a due nudge for ``key`` and return what to do.

        Call this ONCE per key per sweep where a nudge is actually due
        (i.e. after the caller's own debounce gate has elapsed) — calling
        it on every sweep would advance the streak faster than the
        debounce intends.

        Returns one of ``NUDGE`` (send as normal), ``ESCALATE`` (fire the
        operator escalation once + stop poking) or ``SILENT`` (already
        escalated; stay quiet until the fingerprint changes).

        ``max_repeats <= 0`` disables the guard entirely (always NUDGE) —
        the opt-out that preserves pre-#546 unbounded behavior.
        """
        if max_repeats <= 0:
            return NUDGE
        if self._fingerprint.get(key) != fingerprint:
            # Worker made progress (or first sighting) → reset + nudge.
            self._fingerprint[key] = fingerprint
            self._streak[key] = 1
            self._escalated.discard(key)
            return NUDGE
        if key in self._escalated:
            # Already handed to the operator; stay quiet until something
            # changes (which flips the fingerprint branch above).
            return SILENT
        self._streak[key] = self._streak.get(key, 0) + 1
        if self._streak[key] > max_repeats:
            self._escalated.add(key)
            return ESCALATE
        return NUDGE

    def clear(self, key: Hashable) -> None:
        """Forget all state for ``key`` (e.g. worker removed / task done)."""
        self._streak.pop(key, None)
        self._fingerprint.pop(key, None)
        self._escalated.discard(key)
