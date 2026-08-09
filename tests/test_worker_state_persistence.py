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
    assert store.load() == {"api": "RESTING", "web": "WAITING"}


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
    assert store.load() == {"api": "RESTING"}


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
