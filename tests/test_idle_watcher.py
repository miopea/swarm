"""Tests for the idle-watcher drone (task #225 Phase 2)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from swarm.config import DroneConfig
from swarm.drones.idle_watcher import IdleWatcher
from swarm.drones.log import DroneAction
from swarm.worker.worker import WorkerState


def _worker(name: str, state: WorkerState) -> MagicMock:
    """Minimal Worker fake: display_state is what the watcher reads."""
    w = MagicMock()
    w.name = name
    w.display_state = state
    w.state = state
    return w


def _task(number: int, task_id: str) -> MagicMock:
    t = MagicMock()
    t.number = number
    t.id = task_id
    return t


def _board(active_by_worker: dict[str, list[MagicMock]]) -> MagicMock:
    b = MagicMock()

    def active(name: str) -> list[MagicMock]:
        return active_by_worker.get(name, [])

    b.active_tasks_for_worker = MagicMock(side_effect=active)
    # IdleWatcher.sweep now snapshots ``active_tasks`` once and buckets by
    # ``assigned_worker`` — give the mock board a flat list + assignee on
    # each mock task so the bucketing finds them.
    flat: list[MagicMock] = []
    for name, tasks in active_by_worker.items():
        for t in tasks:
            t.assigned_worker = name
            flat.append(t)
    b.active_tasks = flat
    return b


class _Sender:
    """Async collaborator stub — records every PTY send.

    ``raise_for`` is a set of worker names that should raise ``OSError``
    instead of being recorded — used to simulate a dead PTY on one
    worker without affecting the rest of the sweep.
    """

    def __init__(self, *, raise_for: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._raise_for = raise_for or set()

    async def __call__(self, name: str, message: str, **kwargs: Any) -> None:
        if name in self._raise_for:
            raise OSError(f"PTY gone for {name}")
        self.calls.append((name, message, kwargs))


def _watcher(
    *,
    board: MagicMock,
    interval: float = 180.0,
    debounce: float = 900.0,
    rate_limit_check=None,
    sender: _Sender | None = None,
) -> tuple[IdleWatcher, _Sender, MagicMock]:
    sender = sender if sender is not None else _Sender()
    drone_log = MagicMock()
    cfg = DroneConfig(
        idle_nudge_interval_seconds=interval,
        idle_nudge_debounce_seconds=debounce,
    )
    w = IdleWatcher(
        drone_config=cfg,
        task_board=board,
        drone_log=drone_log,
        send_to_worker=sender,
        rate_limit_check=rate_limit_check,
    )
    return w, sender, drone_log


# ---------------------------------------------------------------------------
# Core sweep behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resting_worker_with_active_task_gets_nudged() -> None:
    """Happy path: idle + has an active task → exactly one PTY send and one log entry."""
    board = _board({"alpha": [_task(42, "t-42")]})
    watcher, sender, drone_log = _watcher(board=board)

    sent = await watcher.sweep([_worker("alpha", WorkerState.RESTING)], now=1000.0)

    assert sent == 1
    assert len(sender.calls) == 1
    name, message, kwargs = sender.calls[0]
    assert name == "alpha"
    assert "#42" in message
    assert kwargs.get("_log_operator") is False
    drone_log.add.assert_called_once()
    call = drone_log.add.call_args
    assert call.args[0] is DroneAction.AUTO_NUDGE
    assert call.args[1] == "alpha"


@pytest.mark.asyncio
async def test_sleeping_worker_is_still_nudged() -> None:
    """SLEEPING is just long-idle RESTING — watcher must cover it."""
    board = _board({"bravo": [_task(43, "t-43")]})
    watcher, sender, _ = _watcher(board=board)

    sent = await watcher.sweep([_worker("bravo", WorkerState.SLEEPING)], now=1000.0)

    assert sent == 1
    assert sender.calls[0][0] == "bravo"


@pytest.mark.asyncio
async def test_buzzing_worker_is_skipped() -> None:
    """Workers that are actively producing output are not idle."""
    board = _board({"charlie": [_task(44, "t-44")]})
    watcher, sender, _ = _watcher(board=board)

    sent = await watcher.sweep([_worker("charlie", WorkerState.BUZZING)], now=1000.0)

    assert sent == 0
    assert sender.calls == []


@pytest.mark.asyncio
async def test_stung_worker_is_skipped() -> None:
    """STUNG workers need a revive, not a nudge — different code path owns that."""
    board = _board({"delta": [_task(45, "t-45")]})
    watcher, sender, _ = _watcher(board=board)

    sent = await watcher.sweep([_worker("delta", WorkerState.STUNG)], now=1000.0)

    assert sent == 0


@pytest.mark.asyncio
async def test_idle_worker_without_tasks_is_skipped() -> None:
    """No active task → nothing to nudge about; stays quiet."""
    board = _board({})  # no active tasks anywhere
    watcher, sender, _ = _watcher(board=board)

    sent = await watcher.sweep([_worker("echo", WorkerState.RESTING)], now=1000.0)

    assert sent == 0


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debounce_suppresses_repeat_nudge_within_window() -> None:
    """Two sweeps inside the debounce window → still only one nudge for the same task."""
    board = _board({"alpha": [_task(42, "t-42")]})
    # Interval 1s so two sweeps both trigger; debounce 900s so the second
    # sweep's nudge is suppressed as a duplicate.
    watcher, sender, _ = _watcher(board=board, interval=1.0, debounce=900.0)

    await watcher.sweep([_worker("alpha", WorkerState.RESTING)], now=1000.0)
    await watcher.sweep([_worker("alpha", WorkerState.RESTING)], now=1300.0)

    assert len(sender.calls) == 1


@pytest.mark.asyncio
async def test_debounce_allows_repeat_after_window_elapses() -> None:
    """Beyond the debounce window, re-nudging is expected — the worker may be stuck."""
    board = _board({"alpha": [_task(42, "t-42")]})
    watcher, sender, _ = _watcher(board=board, interval=1.0, debounce=900.0)

    await watcher.sweep([_worker("alpha", WorkerState.RESTING)], now=1000.0)
    await watcher.sweep([_worker("alpha", WorkerState.RESTING)], now=2500.0)  # +1500s

    assert len(sender.calls) == 2


# ---------------------------------------------------------------------------
# Interval / due()
# ---------------------------------------------------------------------------


def test_due_flips_after_interval_elapses() -> None:
    """``due()`` tracks wall time since last sweep; test both sides of the threshold."""
    watcher, _, _ = _watcher(board=_board({}), interval=180.0)
    # At construction time last_sweep == 0, so "due" means "time >= interval".
    assert watcher.due(now=100.0) is False
    assert watcher.due(now=180.0) is True


@pytest.mark.asyncio
async def test_interval_zero_disables_sweep() -> None:
    """Operators can disable the watcher by setting interval to 0 in swarm.yaml."""
    board = _board({"alpha": [_task(42, "t-42")]})
    watcher, sender, _ = _watcher(board=board, interval=0.0)

    assert watcher.enabled is False
    sent = await watcher.sweep([_worker("alpha", WorkerState.RESTING)], now=1000.0)
    assert sent == 0
    assert sender.calls == []


@pytest.mark.asyncio
async def test_sweep_noop_when_not_yet_due() -> None:
    board = _board({"alpha": [_task(42, "t-42")]})
    watcher, sender, _ = _watcher(board=board, interval=180.0)

    await watcher.sweep([_worker("alpha", WorkerState.RESTING)], now=1000.0)
    # Second sweep 10s later, well inside the interval — must not fire.
    sent = await watcher.sweep([_worker("alpha", WorkerState.RESTING)], now=1010.0)

    assert sent == 0
    assert len(sender.calls) == 1


# ---------------------------------------------------------------------------
# Rate-limit escape hatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limited_worker_is_skipped() -> None:
    """When the 5hr quota is blown, nudging queues work against a dead window."""
    board = _board({"alpha": [_task(42, "t-42")]})
    watcher, sender, drone_log = _watcher(
        board=board, rate_limit_check=lambda name: name == "alpha"
    )

    sent = await watcher.sweep([_worker("alpha", WorkerState.RESTING)], now=1000.0)

    assert sent == 0
    assert sender.calls == []
    drone_log.add.assert_not_called()


# ---------------------------------------------------------------------------
# Fault isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_failure_for_one_worker_does_not_stop_sweep() -> None:
    """Broken PTY on one worker mustn't silence every subsequent nudge."""
    board = _board(
        {
            "alpha": [_task(42, "t-42")],
            "bravo": [_task(43, "t-43")],
        }
    )
    sender = _Sender(raise_for={"alpha"})
    watcher, _, drone_log = _watcher(board=board, sender=sender)

    workers = [
        _worker("alpha", WorkerState.RESTING),
        _worker("bravo", WorkerState.RESTING),
    ]
    sent = await watcher.sweep(workers, now=1000.0)

    # Only bravo was delivered; alpha raised.
    assert sent == 1
    assert len(sender.calls) == 1
    assert sender.calls[0][0] == "bravo"
    # And only bravo's nudge got logged — we don't claim to have nudged a
    # worker whose send failed.
    assert drone_log.add.call_count == 1
    assert drone_log.add.call_args.args[1] == "bravo"
