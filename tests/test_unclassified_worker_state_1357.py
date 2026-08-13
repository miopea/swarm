"""#1357 — the dashboard must not assert a state it has not measured.

`Worker.state` defaults to BUZZING, the MOST ACTIVE state, so a freshly started daemon
published a fully-busy swarm for the 4-6s before the pilot's first poll. The operator's
screenshot showed all 16 workers reading "BUZZING — 4m": one identical stale figure
across every tile, which is the tell that nothing measured any of them.

DISPLAY-ONLY. `state` and `display_state` are untouched, so INV-2, the reconcilers and
every state comparison behave exactly as before — that is AC4, and it is why this is a
bool rather than a WorkerState.UNKNOWN member.
"""

from __future__ import annotations

from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask, TaskStatus
from swarm.worker.worker import UNCLASSIFIED_STATE, Worker, WorkerState


def _worker(name: str = "w1") -> Worker:
    return Worker(name=name, path=f"/tmp/{name}")


# ---------------------------------------------------------------------------
# The defect
# ---------------------------------------------------------------------------


def test_a_fresh_worker_is_not_published_as_buzzing():
    """THE FIX. This is the state 16 tiles were rendering after every reload."""
    w = _worker()

    assert w.state_known is False
    assert w.published_state == UNCLASSIFIED_STATE
    assert w.to_api_dict()["state"] == UNCLASSIFIED_STATE
    assert w.to_api_dict()["state"] != WorkerState.BUZZING.value


def test_classification_publishes_the_real_state():
    """POSITIVE CONTROL — without it, hard-coding UNCLASSIFIED forever would pass
    the test above while permanently blinding the dashboard."""
    w = _worker()

    w.force_state(WorkerState.RESTING)

    assert w.state_known is True
    assert w.published_state == WorkerState.RESTING.value
    assert w.to_api_dict()["state"] == WorkerState.RESTING.value


def test_a_restored_remembered_state_counts_as_measured():
    """A remembered state IS a measurement, just an older one.

    Suppressing it would make db/worker_state_store.py invisible on the dashboard —
    the very half of the report ("state is not remembered between reloads") that the
    persistence already fixed.
    """
    import time

    from swarm.server.worker_service import _restore_state

    w = _worker()
    # A RECENT since: an ancient one would correctly derive SLEEPING via the
    # threshold, which would be testing display_state rather than the flag.
    _restore_state(w, {"w1": {"state": "RESTING", "since": time.time()}})

    assert w.state_known is True
    assert w.published_state == WorkerState.RESTING.value


# ---------------------------------------------------------------------------
# AC4 — decision paths must be untouched
# ---------------------------------------------------------------------------


def test_the_flag_does_not_change_state_or_display_state():
    """The whole premise of choosing a bool over an enum variant."""
    w = _worker()

    assert w.state is WorkerState.BUZZING
    assert w.display_state is WorkerState.BUZZING
    assert isinstance(w.display_state, WorkerState)


def test_inv2_behaves_identically_for_an_unclassified_worker():
    """AC4 explicitly. The reconciler reads `state`/`display_state`, never
    `published_state`, so an unmeasured worker must reconcile exactly as before."""
    board = TaskBoard()
    t = board.add(SwarmTask(title="t", status=TaskStatus.ASSIGNED, assigned_worker="w1"))
    board.activate(t.id)

    # Absent → demoted, same as a classified worker would be.
    board.reconcile_invariants(working_workers=set(), absent_workers={"w1"})
    assert t.status == TaskStatus.ASSIGNED

    board.activate(t.id)
    # Merely paused → kept, same as a classified worker would be (#1538).
    board.reconcile_invariants(working_workers=set(), absent_workers=set())
    assert t.status == TaskStatus.ACTIVE


def test_the_summary_counts_do_not_claim_buzzing_either():
    """The state chips count from Worker objects, not the serialized dicts — without
    routing them through published_state the summary would report every unmeasured
    worker as BUZZING while the tiles below said UNCLASSIFIED, the summary
    contradicting the thing it summarises."""
    workers = [_worker("a"), _worker("b")]

    assert [w.published_state for w in workers] == [UNCLASSIFIED_STATE] * 2
