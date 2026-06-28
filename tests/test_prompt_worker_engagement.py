"""Task #913 PATH 1: queen_prompt_worker is engagement-AWARE but NEVER blocking.

The load-bearing guarantee: the prompt ALWAYS sends — engagement context is
advisory only. The Queen must be able to reach a busy worker (P1, pause, scope
correction).
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from swarm.config.models import DroneConfig, HiveConfig
from swarm.mcp.queen_handlers._workers import _handle_prompt_worker
from swarm.worker.worker import QUEEN_WORKER_NAME, WorkerState

_NOTE = "NOTE: target appears freshly engaged"


def _worker(
    name: str, state: WorkerState = WorkerState.RESTING, duration: float = 0.0
) -> MagicMock:
    w = MagicMock()
    w.name = name
    w.state = state
    # #939: the handler now reads live process state for the engagement snapshot.
    w.display_state = state  # WorkerState exposes .value
    w.state_duration = duration
    return w


def _daemon(
    *,
    target_state=WorkerState.RESTING,
    target_duration=0.0,
    active=None,
    assigned=None,
    unread=None,
    window=300.0,
) -> MagicMock:
    d = MagicMock()
    d.workers = [_worker("alice", target_state, target_duration)]
    d.drone_log = MagicMock()
    d.config = HiveConfig(drones=DroneConfig(prompt_collision_window_seconds=window))
    board = MagicMock()
    board.current_task_for_worker.return_value = active
    board.active_tasks_for_worker.return_value = assigned if assigned is not None else []
    d.task_board = board
    store = MagicMock()
    store.get_unread.return_value = unread if unread is not None else []
    d.message_store = store
    d.worker_svc = MagicMock()  # send_to_worker(...) returns a MagicMock; _fire_async drops it
    return d


def _args(**kw):
    return {"worker": "alice", "prompt": "p", "reason": "r", **kw}


def _prompt(d, *, caller=QUEEN_WORKER_NAME, **kw):
    return _handle_prompt_worker(d, caller, _args(**kw))


def _text(result) -> str:
    return result[0]["text"]


def _task(number=7, title="P1 incident", started_at=None):
    return SimpleNamespace(
        number=number, title=title, started_at=started_at, jira_key="", source_worker=""
    )


def _sent(d) -> bool:
    return d.worker_svc.send_to_worker.called


def test_idle_target_no_flag_send_fires():
    d = _daemon(active=None)
    res = _prompt(d, prompt="go")
    assert _sent(d)  # prompt sent
    assert _NOTE not in _text(res)
    assert "no ACTIVE task" in _text(res)


def test_active_fresh_task_flags_but_still_sends():
    # ACTIVE task started 30s ago, window 300 → collision flagged.
    d = _daemon(active=_task(number=7, started_at=time.time() - 30))
    res = _prompt(d, prompt="same P1", reason="heads-up")
    # LOAD-BEARING: the prompt still fired despite the collision.
    assert _sent(d)
    txt = _text(res)
    assert "#7" in txt and "started" in txt  # engagement surfaced
    assert _NOTE in txt  # advisory present


def test_acknowledge_engaged_suppresses_advisory_and_logs_ack():
    d = _daemon(active=_task(started_at=time.time() - 30))
    res = _prompt(d, acknowledge_engaged=True)
    assert _sent(d)
    assert _NOTE not in _text(res)  # advisory suppressed
    logged = " ".join(str(c.args) for c in d.drone_log.add.call_args_list)
    assert "ack-engaged" in logged  # ack recorded in buzz log


def test_stale_active_task_no_flag():
    # started 10 min ago, window 5 min → no collision.
    d = _daemon(active=_task(started_at=time.time() - 600), window=300.0)
    res = _prompt(d)
    assert _sent(d)
    assert _NOTE not in _text(res)


def test_recent_inbound_handoff_surfaced_and_flagged():
    handoff = SimpleNamespace(sender="platform", msg_type="dependency", created_at=time.time() - 20)
    d = _daemon(active=None, unread=[handoff])
    res = _prompt(d)
    assert _sent(d)
    txt = _text(res)
    assert "recent inbound dependency from platform" in txt
    assert _NOTE in txt  # handoff within window → flag


def test_stung_still_refused():
    d = _daemon(target_state=WorkerState.STUNG)
    res = _prompt(d)
    assert "STUNG" in _text(res)
    assert not _sent(d)  # STUNG is the one hard refusal — never sends


def test_non_queen_refused():
    d = _daemon()
    _prompt(d, caller="alice", worker="bob")
    assert not _sent(d)  # permission denied, no send


def test_window_zero_disables_flag():
    d = _daemon(active=_task(started_at=time.time() - 5), window=0.0)
    res = _prompt(d)
    assert _sent(d)
    assert _NOTE not in _text(res)
