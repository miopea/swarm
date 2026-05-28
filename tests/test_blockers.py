"""Tests for the worker-blocker store + IdleWatcher skip-on-blocker path (task #250)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from swarm.config import DroneConfig
from swarm.db.core import SwarmDB
from swarm.drones.idle_watcher import IdleWatcher
from swarm.drones.log import SystemAction
from swarm.tasks.blockers import BlockerStore
from swarm.worker.worker import WorkerState

# ---------------------------------------------------------------------------
# BlockerStore — persistence + auto-clear logic
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    db = SwarmDB(Path(tmp_path) / "swarm.db")
    return BlockerStore(db)


class TestBlockerStore:
    def test_report_and_list(self, store):
        store.report("admin", 246, 245, "waiting on platform field")
        rows = store.list_for_worker("admin")
        assert len(rows) == 1
        assert rows[0].worker == "admin"
        assert rows[0].task_number == 246
        assert rows[0].blocked_by_task == 245
        assert rows[0].reason == "waiting on platform field"

    def test_report_replaces_existing(self, store):
        """Re-reporting the same (worker, task) pair should overwrite
        the row and refresh ``created_at``. Without the refresh, the
        worker couldn't reset the "no new messages since" window after
        their first report."""
        first = store.report("admin", 246, 245, "initial", now=1000.0)
        second = store.report("admin", 246, 245, "updated", now=2000.0)
        rows = store.list_for_worker("admin")
        assert len(rows) == 1
        assert rows[0].reason == "updated"
        assert rows[0].created_at == 2000.0
        assert first.created_at != second.created_at

    def test_clear(self, store):
        store.report("admin", 246, 245)
        assert store.clear("admin", 246) is True
        assert store.list_for_worker("admin") == []
        # Re-clearing is a no-op (returns False).
        assert store.clear("admin", 246) is False

    def test_has_active_blocker_noop_when_none_reported(self, store):
        assert store.has_active_blocker("admin") is None

    def test_auto_clears_when_blocked_task_completed(self, store):
        store.report("admin", 246, 245, now=1000.0)

        def completed(n: int) -> bool:
            return n == 245

        assert store.has_active_blocker("admin", is_task_completed=completed) is None
        # Row is gone after the auto-clear.
        assert store.list_for_worker("admin") == []

    def test_auto_clears_when_new_message_arrives(self, store):
        store.report("admin", 246, 245, now=1000.0)

        def newer(worker: str, since: float) -> bool:
            return worker == "admin" and since < 1500.0

        assert store.has_active_blocker("admin", has_message_since=newer) is None
        assert store.list_for_worker("admin") == []

    def test_survives_when_no_auto_clear_condition_met(self, store):
        store.report("admin", 246, 245, now=1000.0)

        def never_completed(_n: int) -> bool:
            return False

        def no_messages(_w: str, _since: float) -> bool:
            return False

        b = store.has_active_blocker(
            "admin",
            is_task_completed=never_completed,
            has_message_since=no_messages,
        )
        assert b is not None
        assert b.task_number == 246

    def test_on_auto_clear_invoked_with_target_done_reason(self, store):
        """Task #529: when the blocker target task auto-clears, the
        ``on_auto_clear`` callback fires with reason='target_done'. The
        IdleWatcher uses this to emit a BLOCKER_AUTO_CLEARED buzz entry
        so an operator audit can see why the worker is being nudged
        again."""
        store.report("admin", 246, 245, now=1000.0)
        cleared: list[tuple[int, int, str]] = []

        def spy(b, reason: str) -> None:
            cleared.append((b.task_number, b.blocked_by_task, reason))

        assert (
            store.has_active_blocker(
                "admin",
                is_task_completed=lambda n: n == 245,
                on_auto_clear=spy,
            )
            is None
        )
        assert cleared == [(246, 245, "target_done")]
        # Row is gone (clear ran before the callback).
        assert store.list_for_worker("admin") == []

    def test_on_auto_clear_invoked_with_message_since_reason(self, store):
        """Same callback wiring, but the auto-clear trigger was a new
        inbox message rather than the target completing."""
        store.report("admin", 246, 245, now=1000.0)
        cleared: list[tuple[int, int, str]] = []

        def spy(b, reason: str) -> None:
            cleared.append((b.task_number, b.blocked_by_task, reason))

        assert (
            store.has_active_blocker(
                "admin",
                has_message_since=lambda _w, since: since < 1500.0,
                on_auto_clear=spy,
            )
            is None
        )
        assert cleared == [(246, 245, "message_since")]

    def test_on_auto_clear_exceptions_do_not_break_clear(self, store):
        """Callback bugs must not block the auto-clear path — operator
        observability is best-effort, the clear itself is load-bearing."""
        store.report("admin", 246, 245, now=1000.0)

        def boom(_b, _reason: str) -> None:
            raise RuntimeError("callback bug")

        # No exception escapes; the clear still ran.
        assert (
            store.has_active_blocker(
                "admin",
                is_task_completed=lambda n: n == 245,
                on_auto_clear=boom,
            )
            is None
        )
        assert store.list_for_worker("admin") == []


# ---------------------------------------------------------------------------
# IdleWatcher integration — skip nudges on reported blocker
# ---------------------------------------------------------------------------


def _worker(name: str, state: WorkerState) -> MagicMock:
    w = MagicMock()
    w.name = name
    w.display_state = state
    w.state = state
    return w


def _task(number: int, task_id: str) -> MagicMock:
    t = MagicMock()
    t.number = number
    t.id = task_id
    t.status = MagicMock()
    t.status.value = "active"
    return t


def _task_board(tasks_by_worker: dict[str, list[MagicMock]], *, all_tasks=None) -> MagicMock:
    b = MagicMock()

    def active(name: str) -> list[MagicMock]:
        return tasks_by_worker.get(name, [])

    b.active_tasks_for_worker = MagicMock(side_effect=active)
    # IdleWatcher.sweep now snapshots ``active_tasks`` once and buckets by
    # ``assigned_worker`` (perf fix to avoid O(W·T) per-worker scans), so
    # the mock needs to expose both the flat list AND the assignee on
    # each mock task.
    flat: list[MagicMock] = []
    for name, tasks in tasks_by_worker.items():
        for t in tasks:
            t.assigned_worker = name
            flat.append(t)
    b.active_tasks = flat
    b.all_tasks = all_tasks or flat
    return b


class _Sender:
    def __init__(self) -> None:
        self.calls = []

    async def __call__(self, name: str, message: str, **kwargs) -> None:
        self.calls.append((name, message, kwargs))


def _watcher(
    *,
    board,
    blocker_store=None,
    message_has_newer=None,
    interval=60.0,
):
    sender = _Sender()
    drone_log = MagicMock()
    drone_log.entries = []

    def add(action, worker, detail, category=None, **_):
        entry = MagicMock()
        entry.action = action
        entry.worker_name = worker
        entry.detail = detail
        drone_log.entries.append(entry)

    drone_log.add = MagicMock(side_effect=add)
    cfg = DroneConfig(idle_nudge_interval_seconds=interval, idle_nudge_debounce_seconds=60.0)
    w = IdleWatcher(
        drone_config=cfg,
        task_board=board,
        drone_log=drone_log,
        send_to_worker=sender,
        blocker_store=blocker_store,
        message_has_newer=message_has_newer,
    )
    return w, sender, drone_log


@pytest.mark.asyncio
async def test_idle_watcher_skips_nudge_on_reported_blocker(tmp_path):
    """Task #250 acceptance #4: admin reports blocker on #246 blocked
    by #245 → watcher sweep does not nudge admin; buzz log contains an
    ``AUTO_NUDGE_SKIPPED`` entry naming the blocker.
    """
    db = SwarmDB(Path(tmp_path) / "swarm.db")
    store = BlockerStore(db)
    store.report("admin", 246, 245, "waiting on platform field")

    # Task board reports admin has #246 in-progress, plus the blocker
    # task #245 which is NOT yet completed.
    blocked_task = _task(246, "t-246")
    upstream = _task(245, "t-245")
    upstream.status.value = "active"
    board = _task_board(
        {"admin": [blocked_task]},
        all_tasks=[blocked_task, upstream],
    )

    watcher, sender, log = _watcher(
        board=board,
        blocker_store=store,
        message_has_newer=lambda _w, _s: False,
    )
    sent = await watcher.sweep([_worker("admin", WorkerState.RESTING)], now=1000.0)

    assert sent == 0
    assert sender.calls == []
    # Skip entry logged with the blocker details.
    skipped = [e for e in log.entries if e.action == SystemAction.AUTO_NUDGE_SKIPPED]
    assert len(skipped) == 1
    assert "#246" in skipped[0].detail
    assert "#245" in skipped[0].detail


@pytest.mark.asyncio
async def test_idle_watcher_resumes_nudges_when_blocker_task_completes(tmp_path):
    """Acceptance #4 part 2: once the blocking task flips to
    completed, the blocker auto-clears and the watcher nudges again."""
    db = SwarmDB(Path(tmp_path) / "swarm.db")
    store = BlockerStore(db)
    store.report("admin", 246, 245, now=1000.0)

    blocked_task = _task(246, "t-246")
    upstream = _task(245, "t-245")
    upstream.status.value = "done"  # <-- the auto-clear trigger
    board = _task_board(
        {"admin": [blocked_task]},
        all_tasks=[blocked_task, upstream],
    )

    watcher, sender, _log = _watcher(
        board=board,
        blocker_store=store,
        message_has_newer=lambda _w, _s: False,
    )
    sent = await watcher.sweep([_worker("admin", WorkerState.RESTING)], now=2000.0)

    assert sent == 1
    assert len(sender.calls) == 1
    assert sender.calls[0][0] == "admin"
    # Store row is purged — blocker auto-cleared in place.
    assert store.list_for_worker("admin") == []


@pytest.mark.asyncio
async def test_idle_watcher_resumes_nudges_when_new_message_arrives(tmp_path):
    """``has_message_since`` returning True on a blocker's worker must
    also clear the blocker and let the nudge through."""
    db = SwarmDB(Path(tmp_path) / "swarm.db")
    store = BlockerStore(db)
    store.report("admin", 246, 245, now=1000.0)

    blocked_task = _task(246, "t-246")
    upstream = _task(245, "t-245")
    board = _task_board(
        {"admin": [blocked_task]},
        all_tasks=[blocked_task, upstream],
    )
    # Messages available newer than the blocker's created_at of 1000.
    watcher, sender, _log = _watcher(
        board=board,
        blocker_store=store,
        message_has_newer=lambda w, since: w == "admin" and since < 1500.0,
    )
    sent = await watcher.sweep([_worker("admin", WorkerState.RESTING)], now=2000.0)

    assert sent == 1
    assert store.list_for_worker("admin") == []


@pytest.mark.asyncio
async def test_idle_watcher_without_blocker_store_behaves_as_before(tmp_path):
    """Existing deployments wire no blocker_store — watcher sweeps
    work the same as pre-#250."""
    blocked_task = _task(246, "t-246")
    board = _task_board(
        {"admin": [blocked_task]},
        all_tasks=[blocked_task],
    )
    watcher, sender, _log = _watcher(board=board, blocker_store=None)
    sent = await watcher.sweep([_worker("admin", WorkerState.RESTING)], now=1000.0)
    assert sent == 1


@pytest.mark.asyncio
async def test_nudge_returns_after_refreshed_blocker_expires(tmp_path):
    """Re-reporting a blocker refreshes its ``created_at`` so the
    message-since window is measured from the LATEST report, not the
    first. Otherwise workers who refreshed their status right before a
    message arrived would auto-clear on old messages."""
    db = SwarmDB(Path(tmp_path) / "swarm.db")
    store = BlockerStore(db)
    # First report at t=1000 with a stale message from t=500.
    store.report("admin", 246, 245, now=1000.0)

    blocked_task = _task(246, "t-246")
    upstream = _task(245, "t-245")
    upstream.status.value = "active"
    board = _task_board(
        {"admin": [blocked_task]},
        all_tasks=[blocked_task, upstream],
    )
    # First sweep: a message at t=500 is OLDER than the 1000 report —
    # blocker holds.
    watcher, sender, _ = _watcher(
        board=board,
        blocker_store=store,
        message_has_newer=lambda w, since: w == "admin" and since < 500.0,
    )
    sent = await watcher.sweep([_worker("admin", WorkerState.RESTING)], now=1100.0)
    assert sent == 0
    assert store.list_for_worker("admin") != []


@pytest.mark.asyncio
async def test_multiple_active_tasks_still_skipped_on_single_blocker(tmp_path):
    """If a worker has one blocked task and one non-blocked task
    in-progress, the nudge is still skipped — the nudge message names
    ALL active tasks, and sending it would re-surface the blocked
    task and defeat the point of the blocker declaration."""
    db = SwarmDB(Path(tmp_path) / "swarm.db")
    store = BlockerStore(db)
    store.report("admin", 246, 245)

    t_blocked = _task(246, "t-246")
    t_other = _task(250, "t-250")
    upstream = _task(245, "t-245")
    upstream.status.value = "active"
    board = _task_board(
        {"admin": [t_blocked, t_other]},
        all_tasks=[t_blocked, t_other, upstream],
    )
    watcher, sender, log = _watcher(
        board=board,
        blocker_store=store,
        message_has_newer=lambda _w, _s: False,
    )
    sent = await watcher.sweep([_worker("admin", WorkerState.RESTING)], now=1000.0)
    assert sent == 0
    assert sender.calls == []
    skipped = [e for e in log.entries if e.action == SystemAction.AUTO_NUDGE_SKIPPED]
    assert len(skipped) == 1


# ---------------------------------------------------------------------------
# Task #529: BLOCKER_AUTO_CLEARED buzz emission on IdleWatcher path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_watcher_emits_blocker_auto_cleared_buzz_when_target_done(tmp_path):
    """The #526/#528 repro at the IdleWatcher level. Before #529 the
    auto-clear was silent in the buzz log — operators could only infer it
    from the absence of subsequent ``AUTO_NUDGE_SKIPPED`` entries (and
    rcg-networks didn't realize its repeated re-filings were being
    silently no-op'd, burning ~$51 in tokens). The new
    ``BLOCKER_AUTO_CLEARED`` SystemAction makes the clear auditable.

    Setup mirrors the prior ``test_idle_watcher_resumes_nudges_when_blocker_task_completes``
    but adds the buzz-emission assertion.
    """
    db = SwarmDB(Path(tmp_path) / "swarm.db")
    store = BlockerStore(db)
    store.report("admin", 246, 245)

    t_blocked = _task(246, "t-246")
    upstream_done = _task(245, "t-245")
    upstream_done.status.value = "done"  # blocker target is now terminal
    board = _task_board(
        {"admin": [t_blocked]},
        all_tasks=[t_blocked, upstream_done],
    )

    watcher, sender, log = _watcher(
        board=board,
        blocker_store=store,
        message_has_newer=lambda _w, _s: False,
    )
    sent = await watcher.sweep([_worker("admin", WorkerState.RESTING)], now=1000.0)

    # Worker IS nudged this sweep (blocker auto-cleared in the same call).
    assert sent == 1
    assert len(sender.calls) == 1

    # BLOCKER_AUTO_CLEARED buzz entry was emitted with target_done reason.
    cleared = [e for e in log.entries if e.action == SystemAction.BLOCKER_AUTO_CLEARED]
    assert len(cleared) == 1
    assert "#246" in cleared[0].detail
    assert "target_done" in cleared[0].detail
    assert "#245" in cleared[0].detail

    # AUTO_NUDGE_SKIPPED should NOT have fired (the blocker was cleared
    # before the skip check returned).
    skipped = [e for e in log.entries if e.action == SystemAction.AUTO_NUDGE_SKIPPED]
    assert skipped == []

    # Blocker row is gone from the store.
    assert store.list_for_worker("admin") == []
