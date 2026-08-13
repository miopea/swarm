"""#1571 — INV-2's absence threshold is its own knob, not the display one.

THE COMPOUND. #1415 made ``drones.sleeping_threshold`` reach Worker objects, #1538 keyed
INV-2's demotion off SLEEPING, and IdleWatcher fires on SLEEPING + ASSIGNED. So a worker
that pauses long enough to LOOK asleep loses its ACTIVE row, and the demotion manufactures
the nudge condition — the worker gets poked about work it is actively holding.

MEASURED BEFORE CHOOSING A NUMBER, which is what the ticket asked for. Across 45 RESTING
episodes where the worker held an ACTIVE task and then resumed: median 130s, p90 2439s,
p95 3394s. Share of those a threshold would wrongly call absent — 300s: 37.8%, 1200s:
24.4%, 1800s: 22.2%, **3600s: 4.4%**. The knee is at an hour and there is nothing between
1200 and 1800, so the operator's 300→1200 mitigation bought 13 points and then flattened.

Confirmed live: all 15 real INV-2 demotions since #1538 shipped were followed by the same
worker returning to that same task. Not one of them was absent.

``absent_workers()`` had NO direct test coverage before this file — the computation #1538
depends on was never pinned.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from swarm.server.invariants import INV2_ABSENT_THRESHOLD_DEFAULT, InvariantReconciler
from swarm.worker.worker import WORKER_KIND_QUEEN, Worker, WorkerState


def _worker(
    name: str,
    state: WorkerState,
    *,
    resting_for: float = 0.0,
    sleeping_threshold: float = 1200.0,
    queen: bool = False,
) -> Worker:
    w = Worker(name=name, path="/tmp")
    w.state = state
    w.state_since = time.time() - resting_for
    w.sleeping_threshold = sleeping_threshold
    if queen:
        w.kind = WORKER_KIND_QUEEN
    return w


def _reconciler(workers: list[Worker], *, threshold: float | None = 3600.0):
    return InvariantReconciler(
        task_board=MagicMock(),
        task_history=MagicMock(),
        drone_log=MagicMock(),
        blocker_store=None,
        get_workers=lambda: workers,
        absent_threshold=(None if threshold is None else (lambda: threshold)),
    )


# ---------------------------------------------------------------------------
# The live case
# ---------------------------------------------------------------------------


def test_a_worker_resting_25_minutes_is_not_absent():
    """THE DEFECT, reproduced 15 times in production since #1538 shipped.

    25 minutes is past the 1200s display threshold, so this worker READS as SLEEPING and
    today loses its ACTIVE row. The measurement says workers at this duration come back:
    median time-to-resume after a real demotion was 804 seconds.
    """
    w = _worker("swarm", WorkerState.RESTING, resting_for=1500)

    assert w.display_state is WorkerState.SLEEPING, "precondition: it does look asleep"
    assert _reconciler([w]).absent_workers() == set()


def test_a_worker_resting_past_the_absence_threshold_is_absent():
    """POSITIVE CONTROL, and it is #405's whole reason for existing. Without it this
    change could pass every other test by never demoting anything again."""
    w = _worker("swarm", WorkerState.RESTING, resting_for=4000)

    assert _reconciler([w]).absent_workers() == {"swarm"}


# ---------------------------------------------------------------------------
# The decoupling — the ticket's actual ask
# ---------------------------------------------------------------------------


def test_lowering_the_display_threshold_no_longer_re_arms_demotion():
    """THE TRAP THE CONFIG CHANGE DID NOT REMOVE.

    The operator mitigated #1571 by raising ``sleeping_threshold`` 300 → 1200. That knob is
    labelled for DISPLAY, so the next person to lower it for a display reason silently
    re-arms task demotion and the false nudge. After this change the two are independent:
    the same worker, at the same duration, with the display threshold back at its old 300.
    """
    w = _worker("swarm", WorkerState.RESTING, resting_for=1500, sleeping_threshold=300)

    assert w.display_state is WorkerState.SLEEPING
    assert _reconciler([w]).absent_workers() == set()


def test_raising_the_display_threshold_does_not_shield_a_truly_absent_worker():
    """The other direction of the same decoupling, which is the one that would hide a
    real #405 violation: a display threshold set absurdly high must not suppress a
    demotion that the absence threshold has earned."""
    w = _worker("swarm", WorkerState.RESTING, resting_for=4000, sleeping_threshold=99_999)

    assert w.display_state is WorkerState.RESTING, "precondition: it does not look asleep"
    assert _reconciler([w]).absent_workers() == {"swarm"}


# ---------------------------------------------------------------------------
# Absence that is not a timer
# ---------------------------------------------------------------------------


def test_stung_is_absent_immediately():
    """A dead process is absence with nothing to wait for. Gating STUNG behind the timer
    would delay #405's repair by an hour for the one case that is certain."""
    w = _worker("swarm", WorkerState.STUNG, resting_for=1.0)

    assert _reconciler([w]).absent_workers() == {"swarm"}


@pytest.mark.parametrize("state", [WorkerState.BUZZING, WorkerState.WAITING])
def test_a_working_worker_is_never_absent(state: WorkerState):
    w = _worker("swarm", state, resting_for=99_999)

    assert _reconciler([w]).absent_workers() == set()


# ---------------------------------------------------------------------------
# The Queen
# ---------------------------------------------------------------------------


def test_the_queen_is_never_absent_while_alive():
    """This exemption used to ride on ``display_state`` never returning SLEEPING for her.
    Computing absence from ``state`` directly would have dropped it silently — she is
    always-on by design, so an idle Queen is not an absent one."""
    q = _worker("queen", WorkerState.RESTING, resting_for=99_999, queen=True)

    assert _reconciler([q]).absent_workers() == set()


def test_the_queen_is_absent_when_stung():
    """The exemption is about idleness, not immortality — a dead Queen still has her
    stale ACTIVE rows repaired."""
    q = _worker("queen", WorkerState.STUNG, queen=True)

    assert _reconciler([q]).absent_workers() == {"queen"}


# ---------------------------------------------------------------------------
# Fail-open — the direction this fleet keeps getting backwards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf"), None])
def test_an_unusable_threshold_falls_back_to_the_measured_default(bad: float | None):
    """A ZERO HERE WOULD MEAN "DEMOTE INSTANTLY" — every RESTING worker loses its task on
    the next sweep. That is the dangerous direction, so an unusable value resolves to the
    measured default rather than to zero or to infinity.

    The same direction was got backwards three times in one session: an empty worker
    roster read as "nobody exists", an absent tool schema read as "nothing allowed", and a
    missing timestamp read as "infinitely old".
    """
    threshold = (lambda: bad) if bad is not None else None
    rec = InvariantReconciler(
        task_board=MagicMock(),
        task_history=MagicMock(),
        drone_log=MagicMock(),
        blocker_store=None,
        get_workers=lambda: [],
        absent_threshold=threshold,
    )

    assert rec._absent_threshold() == INV2_ABSENT_THRESHOLD_DEFAULT

    # And it behaves like the default, rather than merely reporting it.
    default = INV2_ABSENT_THRESHOLD_DEFAULT
    just_under = _worker("swarm", WorkerState.RESTING, resting_for=default - 60)
    just_over = _worker("root", WorkerState.RESTING, resting_for=default + 60)
    rec._get_workers = lambda: [just_under, just_over]
    assert rec.absent_workers() == {"root"}


def test_a_raising_threshold_callable_does_not_break_the_sweep():
    """Config access must never be able to take out the reconciler — a broken knob that
    stops all invariant repair is worse than the knob being wrong."""

    def boom() -> float:
        raise RuntimeError("config went away")

    rec = InvariantReconciler(
        task_board=MagicMock(),
        task_history=MagicMock(),
        drone_log=MagicMock(),
        blocker_store=None,
        get_workers=lambda: [_worker("swarm", WorkerState.RESTING, resting_for=4000)],
        absent_threshold=boom,
    )

    assert rec._absent_threshold() == INV2_ABSENT_THRESHOLD_DEFAULT
    assert rec.absent_workers() == {"swarm"}


# ---------------------------------------------------------------------------
# The knob is real config, not a constant
# ---------------------------------------------------------------------------


def test_the_default_is_the_measured_value_and_config_carries_it():
    """3600 was read off the distribution, not chosen by feel. Pinned so a later tidy-up
    cannot quietly restore the coupling by defaulting it back to sleeping_threshold."""
    from swarm.config.models import DroneConfig

    assert INV2_ABSENT_THRESHOLD_DEFAULT == 3600.0
    assert DroneConfig().inv2_absent_threshold_seconds == 3600.0
    assert DroneConfig().sleeping_threshold != DroneConfig().inv2_absent_threshold_seconds


def test_the_threshold_is_read_live_so_hot_apply_works():
    """Read through a callable, not captured at construction — the operator changed
    ``sleeping_threshold`` at runtime via PUT /api/config on this very ticket, and this
    knob has to answer the same way."""
    current = {"v": 3600.0}
    w = _worker("swarm", WorkerState.RESTING, resting_for=1500)
    rec = InvariantReconciler(
        task_board=MagicMock(),
        task_history=MagicMock(),
        drone_log=MagicMock(),
        blocker_store=None,
        get_workers=lambda: [w],
        absent_threshold=lambda: current["v"],
    )

    assert rec.absent_workers() == set()
    current["v"] = 600.0
    assert rec.absent_workers() == {"swarm"}
