"""#1415 — `drones.sleeping_threshold` must actually control RESTING → SLEEPING.

The config field is LABELLED "Seconds idle before RESTING → SLEEPING" and controlled
nothing. `Worker.display_state` compares against the Worker's own `sleeping_threshold`
field, which defaulted to the module constant 1200.0, and nothing ever assigned the
configured value onto a Worker. So the effective threshold was always 1200s.

Measured before the fix: config 300.0, worker 1200.0, and a worker idle 400s still
displayed RESTING.

THIS IS NO LONGER DISPLAY-ONLY. #1538 keyed INV-2's task demotion off SLEEPING, which
derives from this threshold — so the value now also decides how long a paused worker
keeps its ACTIVE task. That is why the positive control below matters: a test that
passed because the value was ignored in a convenient direction would hide a knob that
now demotes real work.
"""

from __future__ import annotations

import time

from swarm.worker.worker import SLEEPING_THRESHOLD, Worker, WorkerState


def _resting_for(seconds: float, *, threshold: float | None = None) -> Worker:
    kwargs = {} if threshold is None else {"sleeping_threshold": threshold}
    w = Worker(name="w", path="/tmp", **kwargs)
    w.state = WorkerState.RESTING
    w.state_known = True
    w.state_since = time.time() - seconds
    return w


def test_a_configured_threshold_reaches_display_state():
    """AC6 — the regression this ticket exists for. Pins that the value reaches
    `display_state`, not merely the drone rules (which never consumed it at all)."""
    w = _resting_for(400, threshold=300.0)

    assert w.display_state is WorkerState.SLEEPING


def test_a_higher_threshold_keeps_the_same_worker_resting():
    """POSITIVE CONTROL, and it is the one that matters.

    Identical idle duration, higher configured threshold. Without this, the test
    above would also pass if the threshold were ignored and something else happened
    to report SLEEPING — i.e. it would pass for the wrong reason, which is exactly
    the failure mode this whole ticket is an instance of.
    """
    w = _resting_for(400, threshold=900.0)

    assert w.display_state is WorkerState.RESTING


def test_a_worker_built_without_a_config_uses_the_module_fallback():
    """The behaviour README documents for a Worker constructed with no config."""
    w = _resting_for(400)

    assert w.sleeping_threshold == SLEEPING_THRESHOLD
    assert w.display_state is WorkerState.RESTING  # 400s < 1200s


def test_put_to_sleep_and_the_display_cannot_disagree():
    """AC3. The operator action backdates by `worker.sleeping_threshold` — the SAME
    field `display_state` compares against — so the two agree by construction.

    Driven at a NON-DEFAULT threshold on purpose: if either side ever hardcoded 1200
    this would fail, whereas a test at the default would pass either way.
    """
    w = Worker(name="w", path="/tmp", sleeping_threshold=300.0)
    w.state = WorkerState.RESTING
    w.state_known = True

    # This is what worker_service's put-to-sleep does.
    w.state_since = time.time() - w.sleeping_threshold - 1

    assert w.display_state is WorkerState.SLEEPING


def test_the_construction_site_passes_the_configured_value():
    """Source-scanned: the fix is one keyword at one construction site, and a future
    refactor that drops it would silently restore the 1200s hardcode with every unit
    test above still passing — none of them go through worker_service."""
    from pathlib import Path

    src = Path("src/swarm/server/worker_service.py").read_text(encoding="utf-8")
    i = src.index("w = Worker(")
    assert "sleeping_threshold=config.drones.sleeping_threshold" in src[i : i + 1400]
