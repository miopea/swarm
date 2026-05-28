"""Tests for the shared RepeatNudgeGuard (task #546).

The guard caps the otherwise-unbounded watcher nudge loop: after N
consecutive no-progress nudges it returns ESCALATE once, then SILENT
until the fingerprint changes. See ``swarm.drones.nudge_guard``.
"""

from __future__ import annotations

from swarm.drones.nudge_guard import ESCALATE, NUDGE, SILENT, RepeatNudgeGuard


def test_first_nudge_is_always_nudge():
    g = RepeatNudgeGuard()
    assert g.decide("w", ("RESTING", ()), max_repeats=3) == NUDGE


def test_escalates_after_max_repeats_then_stays_silent():
    g = RepeatNudgeGuard()
    fp = ("RESTING", ((42, "active"),))
    # max_repeats=3 → 3 NUDGEs, then ESCALATE on the 4th due-nudge.
    assert [g.decide("w", fp, max_repeats=3) for _ in range(3)] == [NUDGE, NUDGE, NUDGE]
    assert g.decide("w", fp, max_repeats=3) == ESCALATE
    # Subsequent due-nudges with the SAME fingerprint stay silent (handed
    # to the operator; don't keep poking or re-escalating).
    assert g.decide("w", fp, max_repeats=3) == SILENT
    assert g.decide("w", fp, max_repeats=3) == SILENT


def test_fingerprint_change_resets_streak_and_clears_escalation():
    g = RepeatNudgeGuard()
    fp_a = ("RESTING", ((42, "active"),))
    for _ in range(4):
        g.decide("w", fp_a, max_repeats=3)
    assert g.decide("w", fp_a, max_repeats=3) == SILENT  # escalated
    # Worker made progress → fingerprint changes → reset, resume nudging.
    fp_b = ("RESTING", ((42, "done"),))
    assert g.decide("w", fp_b, max_repeats=3) == NUDGE
    # And it can escalate again on the new state if it goes stale.
    assert [g.decide("w", fp_b, max_repeats=3) for _ in range(2)] == [NUDGE, NUDGE]
    assert g.decide("w", fp_b, max_repeats=3) == ESCALATE


def test_max_repeats_zero_disables_the_cap():
    g = RepeatNudgeGuard()
    fp = ("RESTING", ())
    # Unbounded — always NUDGE, never escalates (pre-#546 behavior).
    assert all(g.decide("w", fp, max_repeats=0) == NUDGE for _ in range(20))


def test_keys_are_independent():
    g = RepeatNudgeGuard()
    fp = ("RESTING", ())
    for _ in range(4):
        g.decide("alpha", fp, max_repeats=3)
    # alpha escalated; beta is untouched and still on its first nudge.
    assert g.decide("alpha", fp, max_repeats=3) == SILENT
    assert g.decide("beta", fp, max_repeats=3) == NUDGE


def test_clear_forgets_key_state():
    g = RepeatNudgeGuard()
    fp = ("RESTING", ())
    for _ in range(4):
        g.decide("w", fp, max_repeats=3)
    assert g.decide("w", fp, max_repeats=3) == SILENT
    g.clear("w")
    # After clear the worker starts fresh.
    assert g.decide("w", fp, max_repeats=3) == NUDGE
