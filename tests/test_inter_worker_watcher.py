"""Tests for the InterWorkerMessageWatcher drone (task #235 Phase 3)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.config import DroneConfig
from swarm.drones.inter_worker_watcher import InterWorkerMessageWatcher
from swarm.drones.log import DroneAction
from swarm.worker.worker import WorkerState


def _worker(name: str, state: WorkerState) -> MagicMock:
    w = MagicMock()
    w.name = name
    w.display_state = state
    w.state = state
    return w


def _message(
    sender: str,
    recipient: str,
    content: str = "x",
    ts: float = 0.0,
    msg_type: str = "dependency",
    msg_id: int = 1,
) -> MagicMock:
    """Construct a fake Message. Defaults to ``msg_type='dependency'``
    since task #271 narrowed the nudge trigger to action-required types
    (``dependency`` / ``warning``); tests that want to exercise the
    nudge-fires path can rely on the default, tests that want to pin
    the skip-on-informational path should pass ``msg_type='finding'``
    (or similar)."""
    m = MagicMock()
    m.id = msg_id
    m.sender = sender
    m.recipient = recipient
    m.content = content
    m.created_at = ts
    m.read_at = None
    m.msg_type = msg_type
    return m


def _store(unread_by_worker: dict[str, list[MagicMock]]) -> MagicMock:
    s = MagicMock()

    def get_unread(name: str) -> list[MagicMock]:
        return unread_by_worker.get(name, [])

    s.get_unread = MagicMock(side_effect=get_unread)
    return s


class _Sender:
    def __init__(self, *, raise_for: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._raise_for = raise_for or set()

    async def __call__(self, name: str, message: str, **kwargs: Any) -> None:
        if name in self._raise_for:
            raise OSError(f"PTY gone for {name}")
        self.calls.append((name, message, kwargs))


def _watcher(
    *,
    store: MagicMock,
    interval: float = 60.0,
    debounce: float = 900.0,
    rate_limit_check=None,
    sender: _Sender | None = None,
    task_board: MagicMock | None = None,
    spawn_handoff_task=None,
) -> tuple[InterWorkerMessageWatcher, _Sender, MagicMock]:
    sender = sender if sender is not None else _Sender()
    drone_log = MagicMock()
    cfg = DroneConfig(
        idle_nudge_interval_seconds=interval,
        idle_nudge_debounce_seconds=debounce,
    )
    w = InterWorkerMessageWatcher(
        drone_config=cfg,
        message_store=store,
        drone_log=drone_log,
        send_to_worker=sender,
        rate_limit_check=rate_limit_check,
        task_board=task_board,
        spawn_handoff_task=spawn_handoff_task,
    )
    return w, sender, drone_log


def _task_board(workers_with_tasks: set[str] | None = None) -> MagicMock:
    """Fake TaskBoard whose ``active_tasks_for_worker`` returns a non-empty
    list for any worker name in ``workers_with_tasks``, empty otherwise.

    Lets tests pin the task-aware filter widening: a worker in the set
    triggers the with-task narrow filter (only ``dependency`` /
    ``warning`` types nudge); a worker NOT in the set triggers the
    no-task widening (any unread type nudges).
    """
    workers_with_tasks = workers_with_tasks or set()
    board = MagicMock()

    def active_tasks_for_worker(name: str) -> list[object]:
        return [MagicMock()] if name in workers_with_tasks else []

    board.active_tasks_for_worker = MagicMock(side_effect=active_tasks_for_worker)
    return board


# ---------------------------------------------------------------------------
# Core sweep behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resting_recipient_with_unread_gets_nudged() -> None:
    """Happy path: inter-worker message to an idle recipient → one
    PTY nudge + one AUTO_NUDGE_MESSAGE buzz entry."""
    store = _store({"hub": [_message("platform", "hub", "fix the thing")]})
    watcher, sender, drone_log = _watcher(store=store)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 1
    assert len(sender.calls) == 1
    name, message, kwargs = sender.calls[0]
    assert name == "hub"
    assert "platform" in message
    assert "swarm_check_messages" in message
    assert kwargs.get("_log_operator") is False
    drone_log.add.assert_called_once()
    entry = drone_log.add.call_args
    assert entry.args[0] is DroneAction.AUTO_NUDGE_MESSAGE
    assert entry.args[1] == "hub"


@pytest.mark.asyncio
async def test_buzzing_recipient_is_skipped() -> None:
    store = _store({"hub": [_message("platform", "hub")]})
    watcher, sender, _ = _watcher(store=store)

    sent = await watcher.sweep([_worker("hub", WorkerState.BUZZING)], now=1000.0)

    assert sent == 0
    assert sender.calls == []


@pytest.mark.asyncio
async def test_no_unread_messages_means_no_nudge() -> None:
    store = _store({})
    watcher, sender, _ = _watcher(store=store)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 0
    assert sender.calls == []


@pytest.mark.asyncio
async def test_queen_sourced_messages_dont_trigger_watcher() -> None:
    """Messages FROM the queen should not trigger the watcher — the
    queen already has her own prompt-worker path. Double-nudging would
    spam the recipient."""
    store = _store({"hub": [_message("queen", "hub")]})
    watcher, sender, _ = _watcher(store=store)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 0


@pytest.mark.asyncio
async def test_queen_recipient_is_skipped() -> None:
    """The queen gets her own inbox relay via Phase 1; don't double-nudge."""
    store = _store({"queen": [_message("hub", "queen")]})
    watcher, sender, _ = _watcher(store=store)

    sent = await watcher.sweep([_worker("queen", WorkerState.RESTING)], now=1000.0)

    assert sent == 0


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debounce_suppresses_repeat_nudge_within_window() -> None:
    store = _store({"hub": [_message("platform", "hub")]})
    watcher, sender, _ = _watcher(store=store, interval=1.0, debounce=900.0)

    await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)
    await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1500.0)

    assert len(sender.calls) == 1


@pytest.mark.asyncio
async def test_debounce_allows_repeat_after_window() -> None:
    store = _store({"hub": [_message("platform", "hub")]})
    watcher, sender, _ = _watcher(store=store, interval=1.0, debounce=900.0)

    await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)
    await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=2500.0)

    assert len(sender.calls) == 2


# ---------------------------------------------------------------------------
# Rate-limit escape hatch + fault isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limited_worker_is_skipped() -> None:
    store = _store({"hub": [_message("platform", "hub")]})
    watcher, sender, drone_log = _watcher(store=store, rate_limit_check=lambda name: name == "hub")

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 0
    assert sender.calls == []
    drone_log.add.assert_not_called()


@pytest.mark.asyncio
async def test_send_failure_for_one_recipient_does_not_stop_sweep() -> None:
    store = _store(
        {
            "alpha": [_message("platform", "alpha")],
            "bravo": [_message("platform", "bravo")],
        }
    )
    sender = _Sender(raise_for={"alpha"})
    watcher, _, drone_log = _watcher(store=store, sender=sender)

    workers = [
        _worker("alpha", WorkerState.RESTING),
        _worker("bravo", WorkerState.RESTING),
    ]
    sent = await watcher.sweep(workers, now=1000.0)

    assert sent == 1
    assert len(sender.calls) == 1
    assert sender.calls[0][0] == "bravo"
    assert drone_log.add.call_count == 1


# ---------------------------------------------------------------------------
# Interval / enabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interval_zero_disables_sweep() -> None:
    store = _store({"hub": [_message("platform", "hub")]})
    watcher, sender, _ = _watcher(store=store, interval=0.0)

    assert watcher.enabled is False
    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)
    assert sent == 0
    assert sender.calls == []


@pytest.mark.asyncio
async def test_no_message_store_disables_sweep() -> None:
    """An operator without a message store shouldn't crash the sweep."""
    watcher, sender, _ = _watcher(store=None)  # type: ignore[arg-type]
    assert watcher.enabled is False

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)
    assert sent == 0
    assert sender.calls == []


# ---------------------------------------------------------------------------
# Task #271: narrow trigger by message type.  Informational types
# (finding / status / note) should not pull a worker off its current
# task; only action-required types (dependency / warning) nudge.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finding_alone_does_not_trigger_nudge() -> None:
    """wifi-portal repro: an FYI finding from public-website on an idle
    worker who has already self-resolved the underlying concern must
    NOT trigger a PTY nudge.  Drone logs an
    ``AUTO_NUDGE_MESSAGE_SKIPPED`` entry instead so the operator has
    telemetry on the suppression."""
    store = _store({"wifi-portal": [_message("public-website", "wifi-portal", msg_type="finding")]})
    watcher, sender, drone_log = _watcher(store=store)

    sent = await watcher.sweep([_worker("wifi-portal", WorkerState.RESTING)], now=1000.0)

    assert sent == 0
    assert sender.calls == []
    # One SKIPPED entry, zero nudge entries.
    actions = [call.args[0] for call in drone_log.add.call_args_list]
    assert DroneAction.AUTO_NUDGE_MESSAGE_SKIPPED in actions
    assert DroneAction.AUTO_NUDGE_MESSAGE not in actions


@pytest.mark.asyncio
async def test_status_alone_does_not_trigger_nudge() -> None:
    """Routine progress-update messages are informational; skip the nudge."""
    store = _store({"hub": [_message("platform", "hub", msg_type="status")]})
    watcher, sender, drone_log = _watcher(store=store)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 0
    assert sender.calls == []
    actions = [call.args[0] for call in drone_log.add.call_args_list]
    assert DroneAction.AUTO_NUDGE_MESSAGE_SKIPPED in actions


@pytest.mark.asyncio
async def test_note_alone_does_not_trigger_nudge() -> None:
    """Side-channel notes (task #248 msg_type) are informational."""
    store = _store({"hub": [_message("platform", "hub", msg_type="note")]})
    watcher, sender, drone_log = _watcher(store=store)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 0
    assert sender.calls == []
    actions = [call.args[0] for call in drone_log.add.call_args_list]
    assert DroneAction.AUTO_NUDGE_MESSAGE_SKIPPED in actions


@pytest.mark.asyncio
async def test_dependency_triggers_nudge() -> None:
    """Baseline: a ``dependency`` message — the canonical action-
    required type — still fires a nudge."""
    store = _store({"hub": [_message("platform", "hub", msg_type="dependency")]})
    watcher, sender, drone_log = _watcher(store=store)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 1
    assert len(sender.calls) == 1
    actions = [call.args[0] for call in drone_log.add.call_args_list]
    assert DroneAction.AUTO_NUDGE_MESSAGE in actions
    assert DroneAction.AUTO_NUDGE_MESSAGE_SKIPPED not in actions


@pytest.mark.asyncio
async def test_warning_triggers_nudge() -> None:
    """``warning`` is also an action-required type and should nudge."""
    store = _store({"hub": [_message("platform", "hub", msg_type="warning")]})
    watcher, sender, _ = _watcher(store=store)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 1
    assert len(sender.calls) == 1


@pytest.mark.asyncio
async def test_mixed_inbox_nudges_on_action_required_surfaces_full_count() -> None:
    """When at least one action-required message exists, nudge fires
    and the nudge wording surfaces the full unread count (not just the
    action-required subset) so the worker knows what awaits in the
    inbox."""
    store = _store(
        {
            "hub": [
                _message("platform", "hub", msg_type="finding", ts=1.0),
                _message("admin", "hub", msg_type="status", ts=2.0),
                _message("nexus", "hub", msg_type="dependency", ts=3.0),
            ]
        }
    )
    watcher, sender, drone_log = _watcher(store=store)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 1
    assert len(sender.calls) == 1
    # Nudge text still references total unread count.
    assert "3 new messages" in sender.calls[0][1]
    # Buzz log entry names the action-required one that drove the nudge.
    nudge_entries = [
        c for c in drone_log.add.call_args_list if c.args[0] is DroneAction.AUTO_NUDGE_MESSAGE
    ]
    assert len(nudge_entries) == 1
    detail = nudge_entries[0].args[2]
    assert "nexus" in detail  # sender of the action-required msg
    assert "dependency" in detail


@pytest.mark.asyncio
async def test_skipped_entry_debounced_per_worker() -> None:
    """Back-to-back sweeps over the same informational-only inbox
    should log AUTO_NUDGE_MESSAGE_SKIPPED at most once per debounce
    window (avoids spamming the buzz log)."""
    store = _store({"hub": [_message("platform", "hub", msg_type="finding")]})
    watcher, sender, drone_log = _watcher(store=store, interval=1.0)

    await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)
    await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1100.0)

    skipped_entries = [
        c
        for c in drone_log.add.call_args_list
        if c.args[0] is DroneAction.AUTO_NUDGE_MESSAGE_SKIPPED
    ]
    assert len(skipped_entries) == 1, (
        "second sweep should not re-log AUTO_NUDGE_MESSAGE_SKIPPED within the debounce window"
    )
    assert sender.calls == []


# ---------------------------------------------------------------------------
# No-task widening: idle worker without an active task → ANY unread nudges
# ---------------------------------------------------------------------------
#
# Closes the gap that prompted this change: operators were having to
# manually tell idle workers "check your messages" because the actionable
# filter (#271) skipped FYI-style messages. With no active task, the
# worker is idle anyway and the operator wants the inbox processed.


@pytest.mark.asyncio
async def test_no_task_resting_with_finding_nudges() -> None:
    """RESTING worker, no active task, only a `finding` in inbox →
    nudge fires (the no-task widening). Pre-fix, this would hit the
    informational-only skip path."""
    store = _store({"hub": [_message("platform", "hub", msg_type="finding")]})
    board = _task_board()  # no workers have tasks
    watcher, sender, drone_log = _watcher(store=store, task_board=board)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 1
    assert len(sender.calls) == 1
    assert "platform" in sender.calls[0][1]
    assert "swarm_check_messages" in sender.calls[0][1]
    nudge_entries = [
        c for c in drone_log.add.call_args_list if c.args[0] is DroneAction.AUTO_NUDGE_MESSAGE
    ]
    assert len(nudge_entries) == 1
    # Buzz log surfaces which path fired so audits can tell apart
    # narrow (#271) vs widened (no-task) nudges.
    assert "no-task" in nudge_entries[0].args[2]


@pytest.mark.asyncio
async def test_no_task_resting_with_status_nudges() -> None:
    """`status` is the lowest-signal type; widened path fires anyway
    when the worker is idle without a task."""
    store = _store({"hub": [_message("platform", "hub", msg_type="status")]})
    board = _task_board()
    watcher, sender, _ = _watcher(store=store, task_board=board)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 1
    assert len(sender.calls) == 1


@pytest.mark.asyncio
async def test_no_task_resting_with_note_nudges() -> None:
    """`note` (Queen-side annotations, msg_type from #248) reaches the
    inter-worker watcher via cross-worker notes; widened path fires."""
    store = _store({"hub": [_message("platform", "hub", msg_type="note")]})
    board = _task_board()
    watcher, sender, _ = _watcher(store=store, task_board=board)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 1


@pytest.mark.asyncio
async def test_no_task_sleeping_with_finding_nudges() -> None:
    """SLEEPING is treated identically to RESTING for the widened path."""
    store = _store({"hub": [_message("platform", "hub", msg_type="finding")]})
    board = _task_board()
    watcher, sender, _ = _watcher(store=store, task_board=board)

    sent = await watcher.sweep([_worker("hub", WorkerState.SLEEPING)], now=1000.0)

    assert sent == 1


@pytest.mark.asyncio
async def test_no_task_with_dependency_still_nudges() -> None:
    """Regression pin: action-required types still nudge in the no-task
    path (they're a subset of "any unread"). Buzz log labels [no-task]."""
    store = _store({"hub": [_message("platform", "hub", msg_type="dependency")]})
    board = _task_board()
    watcher, sender, drone_log = _watcher(store=store, task_board=board)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 1
    nudge = next(
        c for c in drone_log.add.call_args_list if c.args[0] is DroneAction.AUTO_NUDGE_MESSAGE
    )
    assert "no-task" in nudge.args[2]


@pytest.mark.asyncio
async def test_with_task_resting_with_finding_does_not_nudge() -> None:
    """Regression pin for #271: when the worker HAS an active task, an
    informational-only inbox still hits the SKIPPED path. The widening
    only applies to the no-task case."""
    store = _store({"hub": [_message("platform", "hub", msg_type="finding")]})
    board = _task_board(workers_with_tasks={"hub"})
    watcher, sender, drone_log = _watcher(store=store, task_board=board)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 0
    assert sender.calls == []
    skipped = [
        c
        for c in drone_log.add.call_args_list
        if c.args[0] is DroneAction.AUTO_NUDGE_MESSAGE_SKIPPED
    ]
    assert len(skipped) == 1


@pytest.mark.asyncio
async def test_with_task_resting_with_warning_nudges_with_label() -> None:
    """Regression pin: action-required types nudge in the with-task path,
    and the buzz log label says [with-task]."""
    store = _store({"hub": [_message("platform", "hub", msg_type="warning")]})
    board = _task_board(workers_with_tasks={"hub"})
    watcher, sender, drone_log = _watcher(store=store, task_board=board)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 1
    nudge = next(
        c for c in drone_log.add.call_args_list if c.args[0] is DroneAction.AUTO_NUDGE_MESSAGE
    )
    assert "with-task" in nudge.args[2]


@pytest.mark.asyncio
async def test_no_task_buzzing_does_not_nudge() -> None:
    """State gate is independent of the task-aware widening: a BUZZING
    worker is never nudged regardless of inbox or task state."""
    store = _store({"hub": [_message("platform", "hub", msg_type="finding")]})
    board = _task_board()
    watcher, sender, _ = _watcher(store=store, task_board=board)

    sent = await watcher.sweep([_worker("hub", WorkerState.BUZZING)], now=1000.0)

    assert sent == 0
    assert sender.calls == []


@pytest.mark.asyncio
async def test_task_board_unwired_defaults_to_with_task_filter() -> None:
    """When ``task_board=None`` (eager-init, minimal test fixtures),
    the watcher conservatively applies the with-task narrow filter so
    setups without a board don't accidentally over-nudge."""
    store = _store({"hub": [_message("platform", "hub", msg_type="finding")]})
    watcher, sender, _ = _watcher(store=store, task_board=None)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    # No nudge — conservative default treats the worker as having a task.
    assert sent == 0
    assert sender.calls == []


@pytest.mark.asyncio
async def test_task_board_raising_falls_back_to_with_task_filter() -> None:
    """Errors from the board shouldn't widen the filter — same conservative
    default as ``task_board=None``."""
    store = _store({"hub": [_message("platform", "hub", msg_type="finding")]})
    board = MagicMock()
    board.active_tasks_for_worker = MagicMock(side_effect=RuntimeError("board exploded"))
    watcher, sender, _ = _watcher(store=store, task_board=board)

    sent = await watcher.sweep([_worker("hub", WorkerState.RESTING)], now=1000.0)

    assert sent == 0
    assert sender.calls == []


# ---------------------------------------------------------------------------
# task #442 — actionable handoff → idle task-less recipient spawns a
# *tracked* task instead of relying on a skip-prone one-shot nudge.
# Repro reference: public-website msg #985 → realtruth (idle/task-less).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_to_idle_taskless_recipient_spawns_tracked_task() -> None:
    """The #985 pattern: idle, task-less recipient + unread dependency
    handoff → a tracked task is spawned (not just a nudge)."""
    msg = _message("public-website", "realtruth", msg_type="dependency", msg_id=985)
    store = _store({"realtruth": [msg]})
    spawn = AsyncMock(return_value=True)
    board = _task_board()  # realtruth not in set → task-less
    watcher, sender, drone_log = _watcher(store=store, task_board=board, spawn_handoff_task=spawn)

    sent = await watcher.sweep([_worker("realtruth", WorkerState.RESTING)], now=1000.0)

    assert sent == 1
    spawn.assert_awaited_once_with("realtruth", msg)
    # Spawn replaces the nudge this sweep — assign-and-start prompts the
    # worker, so a nudge would double up.
    assert sender.calls == []
    action = drone_log.add.call_args[0][0]
    assert action is DroneAction.AUTO_HANDOFF_TASK


@pytest.mark.asyncio
async def test_handoff_task_not_spawned_twice_for_same_message() -> None:
    """A still-unread handoff must not re-spawn a task every sweep."""
    msg = _message("public-website", "realtruth", msg_type="dependency", msg_id=985)
    store = _store({"realtruth": [msg]})
    spawn = AsyncMock(return_value=True)
    watcher, _, _ = _watcher(store=store, task_board=_task_board(), spawn_handoff_task=spawn)

    await watcher.sweep([_worker("realtruth", WorkerState.RESTING)], now=1000.0)
    await watcher.sweep([_worker("realtruth", WorkerState.RESTING)], now=2000.0)

    assert spawn.await_count == 1


@pytest.mark.asyncio
async def test_no_handoff_task_when_recipient_has_active_task() -> None:
    """With an active task the IdleWatcher already carries the worker —
    don't spawn; the existing with-task nudge path handles it."""
    msg = _message("public-website", "realtruth", msg_type="dependency", msg_id=985)
    store = _store({"realtruth": [msg]})
    spawn = AsyncMock(return_value=True)
    board = _task_board(workers_with_tasks={"realtruth"})
    watcher, sender, _ = _watcher(store=store, task_board=board, spawn_handoff_task=spawn)

    sent = await watcher.sweep([_worker("realtruth", WorkerState.RESTING)], now=1000.0)

    spawn.assert_not_awaited()
    assert sent == 1  # fell through to the normal with-task nudge
    assert len(sender.calls) == 1


@pytest.mark.asyncio
async def test_no_handoff_task_for_informational_only_message() -> None:
    """Spawn is scoped to action-bearing types — a status/finding ping
    to a task-less worker still nudges (no-task widening) but does NOT
    spawn a task (would flood the board with FYI chatter)."""
    msg = _message("public-website", "realtruth", msg_type="status", msg_id=985)
    store = _store({"realtruth": [msg]})
    spawn = AsyncMock(return_value=True)
    watcher, sender, _ = _watcher(store=store, task_board=_task_board(), spawn_handoff_task=spawn)

    sent = await watcher.sweep([_worker("realtruth", WorkerState.RESTING)], now=1000.0)

    spawn.assert_not_awaited()
    assert sent == 1  # no-task widening still nudges on any unread
    assert len(sender.calls) == 1


@pytest.mark.asyncio
async def test_no_spawn_callback_falls_back_to_nudge() -> None:
    """When the daemon hasn't wired the spawn callback the watcher
    degrades to the prior nudge-only behaviour (no crash)."""
    msg = _message("public-website", "realtruth", msg_type="dependency", msg_id=985)
    store = _store({"realtruth": [msg]})
    watcher, sender, _ = _watcher(store=store, task_board=_task_board(), spawn_handoff_task=None)

    sent = await watcher.sweep([_worker("realtruth", WorkerState.RESTING)], now=1000.0)

    assert sent == 1
    assert len(sender.calls) == 1


# ---------------------------------------------------------------------------
# #614: nudge "no-progress" must track the unread-message set, not worker state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalates_despite_worker_state_oscillation() -> None:
    """#614 repro: a recipient that RESPONDS to each nudge (its state flips
    RESTING↔SLEEPING between windows) but never CLEARS the unread message must
    still hit max_repeats → escalate once → go silent. The progress signal is
    the unread-message set, not worker state (which legitimately oscillates).

    This is the aria/#1390 churn: with the old state-in-fingerprint rule, every
    state flip reset the streak so the worker was nudged forever (72×/22h here)."""
    msg = _message("project-root", "aria", msg_type="dependency", msg_id=1390)
    store = _store({"aria": [msg]})  # same unread message forever — never cleared
    cfg = DroneConfig(
        idle_nudge_interval_seconds=60.0,
        idle_nudge_debounce_seconds=0.0,  # no debounce gate; every sweep is a due nudge
        idle_nudge_max_repeats=3,
    )
    sender = _Sender()
    watcher = InterWorkerMessageWatcher(
        drone_config=cfg,
        message_store=store,
        drone_log=MagicMock(),
        send_to_worker=sender,
        task_board=_task_board(),  # no active task → any unread nudges
    )

    # Worker state alternates each sweep (it keeps responding without clearing).
    states = [WorkerState.RESTING, WorkerState.SLEEPING] * 4
    for i, st in enumerate(states):
        await watcher.sweep([_worker("aria", st)], now=1000.0 + i * 100)

    # Only max_repeats real nudges fire, then escalate-and-quiet — NOT one per
    # sweep. (Old behaviour: state flip reset the streak every sweep → 8 nudges.)
    assert len(sender.calls) == 3


@pytest.mark.asyncio
async def test_new_unread_message_resets_streak() -> None:
    """A genuinely new inbound message (the unread set changes) is real
    progress → resets the streak so the worker is nudged again."""
    m1 = _message("project-root", "aria", msg_type="dependency", msg_id=1)
    unread = {"aria": [m1]}
    store = _store(unread)
    cfg = DroneConfig(
        idle_nudge_interval_seconds=60.0,
        idle_nudge_debounce_seconds=0.0,
        idle_nudge_max_repeats=2,
    )
    sender = _Sender()
    watcher = InterWorkerMessageWatcher(
        drone_config=cfg,
        message_store=store,
        drone_log=MagicMock(),
        send_to_worker=sender,
        task_board=_task_board(),
    )
    # Two nudges exhaust max_repeats; third sweep escalates (no send).
    for i in range(3):
        await watcher.sweep([_worker("aria", WorkerState.RESTING)], now=1000.0 + i * 100)
    assert len(sender.calls) == 2
    # A NEW message arrives → unread set changes → streak resets → nudge again.
    unread["aria"] = [m1, _message("project-root", "aria", msg_type="dependency", msg_id=2)]
    await watcher.sweep([_worker("aria", WorkerState.RESTING)], now=2000.0)
    assert len(sender.calls) == 3
