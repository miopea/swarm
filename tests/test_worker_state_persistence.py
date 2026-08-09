"""Worker state survives a daemon restart (#1357).

OPERATOR-REPORTED with a screenshot: after every reload — and in the popped-out task
window — all sixteen workers showed BUZZING for four to six seconds before snapping to
their real states, every one reading an identical "BUZZING — 4m".

BOTH HALVES WERE ONE LINE. `Worker.state` defaults to WorkerState.BUZZING and nothing
persisted it, so each daemon start constructed every worker as actively working and the
dashboard rendered that belief faithfully until the pilot's first poll.

"Everything is working" is the worst thing to assert while you do not know.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from swarm.db.core import SwarmDB
from swarm.db.worker_state_store import WorkerStateStore
from swarm.server.worker_service import _remembered_states, _restore_state
from swarm.worker.worker import Worker, WorkerState


@pytest.fixture
def store(tmp_path: Path) -> WorkerStateStore:
    return WorkerStateStore(SwarmDB(tmp_path / "swarm.db"))


def _worker(name: str = "api") -> Worker:
    return Worker(name=name, path="/tmp")


# --- the store ------------------------------------------------------------------


def test_states_survive_a_round_trip(store: WorkerStateStore):
    store.save({"api": "RESTING", "web": "WAITING"})
    # load() returns RememberedState now (it also carries state_since, so a worker the
    # operator put to sleep comes back SLEEPING). Same intent as before: both workers
    # come back with the state they went in as.
    assert {k: v.state for k, v in store.load().items()} == {"api": "RESTING", "web": "WAITING"}


def test_nothing_saved_yet_is_not_an_error(store: WorkerStateStore):
    assert store.load() == {}


def test_stale_states_are_DISCARDED_not_shown(store: WorkerStateStore):
    """A daemon that has been down overnight knows nothing useful about what its workers
    are doing. Stale state presented as current is the quieter version of the very bug
    being fixed."""
    store.save({"api": "RESTING"})
    store._db.execute(
        "UPDATE config SET value = ? WHERE key = 'worker_states'",
        (json.dumps({"at": time.time() - 60 * 60 * 6, "states": {"api": "RESTING"}}),),
    )
    store._db.commit()

    assert store.load() == {}, "six-hour-old state was restored as if it were current"


def test_a_corrupt_value_yields_nothing_rather_than_raising(store: WorkerStateStore):
    store.save({"api": "RESTING"})
    store._db.execute("UPDATE config SET value = 'not json' WHERE key = 'worker_states'")
    store._db.commit()
    assert store.load() == {}


def test_saving_nothing_does_not_wipe_what_is_there(store: WorkerStateStore):
    """A rebuild that momentarily sees no workers must not erase the memory."""
    store.save({"api": "RESTING"})
    store.save({})
    assert {k: v.state for k, v in store.load().items()} == {"api": "RESTING"}


# --- restoring onto a freshly adopted worker -------------------------------------


def test_a_remembered_state_replaces_the_BUZZING_default():
    """THE BUG. Without this the worker keeps the dataclass default and the dashboard
    reports it as actively working."""
    w = _worker()
    assert w.state is WorkerState.BUZZING, "the default changed; this test's premise is stale"

    _restore_state(w, {"api": "RESTING"})

    assert w.state is WorkerState.RESTING


def test_an_unknown_worker_keeps_the_default():
    w = _worker("brand-new")
    _restore_state(w, {"api": "RESTING"})
    assert w.state is WorkerState.BUZZING


def test_STUNG_is_never_restored():
    """A worker that had crashed may well have been revived BY the restart. Showing a
    dead worker as dead when it is alive is the same class of error as showing an idle
    one as busy — just in the other direction."""
    w = _worker()
    _restore_state(w, {"api": "STUNG"})
    assert w.state is not WorkerState.STUNG


def test_an_unrecognised_value_is_ignored():
    """The enum could change under a stored map; that must not raise during adoption."""
    w = _worker()
    _restore_state(w, {"api": "DANCING"})
    assert w.state is WorkerState.BUZZING


def test_a_missing_loader_costs_the_head_start_not_the_roster():
    """Adoption runs on paths where nothing is wired. It must degrade to today's
    behaviour rather than fail."""
    assert _remembered_states(None) == {}


def test_a_raising_loader_is_swallowed():
    def _boom() -> dict[str, str]:
        raise RuntimeError("db gone")

    assert _remembered_states(_boom) == {}


# --- the wiring -------------------------------------------------------------------


def test_the_daemon_wires_both_directions():
    """Save and load are separate call sites; one without the other is a memory that
    never fills or never empties."""
    src = Path("src/swarm/server/daemon.py").read_text()
    assert "save_worker_states=" in src, "state is never persisted"
    assert "load_worker_states=" in src, "persisted state is never restored"


def test_persistence_is_triggered_by_a_state_CHANGE():
    """On change, not on a timer: sixteen workers polled continuously would otherwise
    write constantly for no new information."""
    import ast

    src = Path("src/swarm/server/state_publisher.py").read_text()
    fn = next(
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "on_state_changed"
    )
    assert "_persist_worker_states" in ast.unparse(fn)


# --- "some to sleep ... only public-website was resting" ----------------------------


def test_sleeping_survives_a_restart_because_state_since_is_persisted():
    """OPERATOR-REPORTED, after the first version of this store shipped.

    SLEEPING is not a stored state — ``display_state`` derives it from how long the
    worker has been RESTING — and the operator's "put to sleep" action works by setting
    RESTING and BACKDATING ``state_since`` past the threshold. Persisting the state
    alone therefore threw away the only thing that made it SLEEPING, and every slept
    worker came back as plain RESTING.

    Asserted through ``display_state`` rather than through the stored timestamp, because
    the timestamp is the mechanism and SLEEPING is the property the operator sees.
    """
    import time as _time

    from swarm.db.worker_state_store import RememberedState
    from swarm.server.worker_service import _restore_state
    from swarm.worker.worker import SLEEPING_THRESHOLD, Worker, WorkerState

    slept_at = _time.time() - SLEEPING_THRESHOLD - 60
    w = Worker(name="public-website", path="/tmp", provider_name="claude")
    _restore_state(w, {"public-website": RememberedState(state="RESTING", since=slept_at)})

    assert w.state is WorkerState.RESTING
    assert w.display_state is WorkerState.SLEEPING, (
        "a worker the operator put to sleep came back as plain RESTING"
    )


def test_a_recently_rested_worker_is_not_promoted_to_sleeping():
    """The other direction — restoring a timestamp must not INVENT sleep."""
    import time as _time

    from swarm.db.worker_state_store import RememberedState
    from swarm.server.worker_service import _restore_state
    from swarm.worker.worker import Worker, WorkerState

    w = Worker(name="hub", path="/tmp", provider_name="claude")
    _restore_state(w, {"hub": RememberedState(state="RESTING", since=_time.time() - 5)})

    assert w.display_state is WorkerState.RESTING


def test_a_future_timestamp_is_refused():
    """Clock skew between writes would make state_duration negative, which reads as a
    worker that has been idle for a negative time. Keep the worker's own value."""
    import time as _time

    from swarm.db.worker_state_store import RememberedState
    from swarm.server.worker_service import _restore_state
    from swarm.worker.worker import Worker

    w = Worker(name="hub", path="/tmp", provider_name="claude")
    before = w.state_since
    _restore_state(w, {"hub": RememberedState(state="RESTING", since=_time.time() + 9999)})

    assert w.state_since == before


def test_the_old_bare_string_payload_is_still_read():
    """A daemon updating mid-cycle finds the previous format in the DB. Discarding it
    would cost exactly the restart this store exists to protect."""
    from swarm.db.worker_state_store import WorkerStateStore

    class _DB:
        def fetchone(self, *_a, **_k):
            import json
            import time as _t

            return {"value": json.dumps({"at": _t.time(), "states": {"hub": "RESTING"}})}

    loaded = WorkerStateStore(_DB()).load()
    assert loaded["hub"].state == "RESTING"
    assert loaded["hub"].since is None, "an absent timestamp must not be invented"


# --- putting a worker to sleep must OUTLIVE the daemon -------------------------------


@pytest.mark.asyncio
async def test_putting_a_worker_to_sleep_persists_it():
    """THE SECOND HALF of "some to sleep ... didn't stick".

    Carrying ``state_since`` in the store was necessary but not sufficient: nothing was
    writing it. Every other state change goes through the pilot and emits state_changed,
    which is what the publisher persists on — but ``sleep_worker`` assigns the attribute
    directly, and on an ALREADY-RESTING worker there is no transition to emit. The
    backdated timestamp therefore lived only in memory.

    The worker here starts RESTING deliberately: that is the case with no state change
    at all, so a fix that merely emitted an event on transition would still fail it.
    """

    from swarm.worker.worker import SLEEPING_THRESHOLD, Worker, WorkerState

    saved: list[dict] = []
    w = Worker(name="hub", path="/tmp", provider_name="claude", state=WorkerState.RESTING)

    svc = _make_service_for_sleep(w, saved.append)
    await svc.sleep_worker("hub")

    assert saved, "putting a worker to sleep wrote nothing — it cannot survive a reload"
    entry = saved[-1]["hub"]
    assert entry.state == "RESTING"
    age = time.time() - entry.since
    assert age > SLEEPING_THRESHOLD, (
        f"persisted state_since is only {age:.0f}s old, so this worker comes back "
        "RESTING rather than SLEEPING"
    )


def _make_service_for_sleep(worker, on_save):
    """Minimal WorkerService wired for sleep_worker and nothing else."""
    from unittest.mock import MagicMock

    from swarm.server.worker_service import WorkerService

    svc = WorkerService.__new__(WorkerService)
    svc._get_workers = lambda: [worker]
    svc.require_worker = lambda _n: worker
    svc._save_worker_states = on_save
    svc._drone_log = MagicMock()
    svc._broadcast_ws = MagicMock()
    return svc


def test_a_persist_failure_never_breaks_the_operator_action():
    """Best effort: a store that raises must cost the next restart its head start, not
    the sleep the operator just asked for."""

    from swarm.worker.worker import Worker, WorkerState

    w = Worker(name="hub", path="/tmp", provider_name="claude", state=WorkerState.RESTING)

    def _boom(_states):
        raise RuntimeError("disk full")

    svc = _make_service_for_sleep(w, _boom)
    svc._persist_worker_states()  # must not raise
