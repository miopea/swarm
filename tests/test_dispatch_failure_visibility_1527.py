"""#1527 — a dispatch failure must be visible, and a stranded ASSIGNED row must get swept.

#1486 stopped `_fire_async` from DISCARDING the exception. It did not stop the handler
from claiming success before the dispatch ran, and it left the only trace in the daemon
log — not a state change, and not anywhere the Queen looks. So the Queen was told
"dispatched", believed it, and the operator's evidence was a worker that stayed asleep.

SPECIMEN: #1432's history read CREATED -> EDITED -> ASSIGNED with no STARTED row.

Every test here injects a real failure rather than asserting one, because the defect was
invisible at exactly the boundary a mocked assertion would have trusted.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from swarm.drones.log import SystemAction
from swarm.mcp.queen_handlers._tasks import _log_async_failure
from swarm.tasks.board import _DISPATCH_STALL_SECONDS, TaskBoard
from swarm.tasks.history import TaskAction
from swarm.tasks.task import SwarmTask, TaskStatus

# --------------------------------------------------------------------------
# (A) + (B): the failure is reported, and reported IN-BAND
# --------------------------------------------------------------------------


def _failed_task() -> asyncio.Task[Any]:
    """A completed asyncio Task carrying an exception, as the callback sees it."""

    async def boom() -> None:
        raise RuntimeError("pty write failed")

    loop = asyncio.new_event_loop()
    try:
        t = loop.create_task(boom())
        with pytest.raises(RuntimeError):
            loop.run_until_complete(t)
        return t
    finally:
        loop.close()


def test_dispatch_failure_lands_in_buzz_log_and_task_history():
    """AC2. Previously this existed ONLY as a daemon-log line nobody reads."""
    daemon = MagicMock()
    task = MagicMock()
    task.id = "abc123"
    task.number = 1432
    task.assigned_worker = "platform"

    _log_async_failure(_failed_task(), "dispatch #1432 -> platform", daemon, task)

    buzz = daemon.drone_log.add.call_args
    assert buzz.args[0] is SystemAction.TASK_DISPATCH_FAILED
    assert buzz.args[1] == "platform"
    assert "pty write failed" in buzz.args[2]

    hist = daemon.task_history.append.call_args
    assert hist.args[0] == "abc123"
    assert hist.args[1] is TaskAction.DISPATCH_FAILED
    assert "pty write failed" in hist.kwargs["detail"]


def test_a_successful_call_reports_nothing():
    """Positive control: no exception -> no failure rows.

    Without this, a callback that unconditionally logged would pass the test above
    while filling the board with phantom dispatch failures.
    """

    async def fine() -> None:
        return None

    loop = asyncio.new_event_loop()
    try:
        t = loop.create_task(fine())
        loop.run_until_complete(t)
    finally:
        loop.close()

    daemon = MagicMock()
    _log_async_failure(t, "dispatch #1 -> x", daemon, MagicMock())
    daemon.drone_log.add.assert_not_called()
    daemon.task_history.append.assert_not_called()


def test_reporting_the_failure_cannot_itself_throw():
    """This runs in an asyncio done-callback, where a raise is swallowed.

    If the buzz write explodes, the history write must still be attempted — losing
    both reports to one broken sink is how the original silence happened.
    """
    daemon = MagicMock()
    daemon.drone_log.add.side_effect = Exception("buzz sink down")
    task = MagicMock()
    task.id = "abc123"
    task.number = 1432
    task.assigned_worker = "platform"

    _log_async_failure(_failed_task(), "dispatch #1432 -> platform", daemon, task)

    daemon.task_history.append.assert_called_once()


def test_ac5_the_1486_log_line_is_still_emitted(caplog):
    """AC5. #1486's fix must survive #1527, not be replaced by it."""
    import logging

    with caplog.at_level(logging.ERROR):
        _log_async_failure(_failed_task(), "dispatch #1432 -> platform", None, None)

    assert any("pty write failed" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# (C) the invariant
# --------------------------------------------------------------------------


def _board() -> TaskBoard:
    return TaskBoard()


def _assigned(board: TaskBoard, *, claimed_ago: float | None) -> SwarmTask:
    t = board.add(SwarmTask(title="t", status=TaskStatus.ASSIGNED, assigned_worker="platform"))
    if claimed_ago is not None:
        t.dispatch_requested_at = time.time() - claimed_ago
    return t


def test_stalled_dispatch_is_swept():
    """AC3. The row #1432 became: claimed, ASSIGNED, never ACTIVE."""
    board = _board()
    task = _assigned(board, claimed_ago=_DISPATCH_STALL_SECONDS + 60)

    repairs = board.reconcile_invariants()

    stalled = [r for r in repairs if r.get("kind") == "stalled_dispatch"]
    assert len(stalled) == 1, repairs
    assert stalled[0]["task_id"] == task.id
    assert "never reached ACTIVE" in stalled[0]["reason"]


def test_queued_work_without_a_claim_is_not_swept():
    """AC3 POSITIVE CONTROL — the whole reason the marker had to be persisted.

    Ordinary ASSIGNED work is indistinguishable from a failed dispatch WITHOUT the
    marker. A rule that swept on status alone would pass the test above while
    reporting every queued task on the board as a failure.
    """
    board = _board()
    _assigned(board, claimed_ago=None)

    repairs = board.reconcile_invariants()

    assert [r for r in repairs if r.get("kind") == "stalled_dispatch"] == []


def test_a_recent_claim_is_left_alone():
    """A dispatch in flight is not a stalled one — don't cry wolf on a slow one."""
    board = _board()
    _assigned(board, claimed_ago=5)

    repairs = board.reconcile_invariants()

    assert [r for r in repairs if r.get("kind") == "stalled_dispatch"] == []


def test_it_fires_once_not_every_sweep():
    """A repeating alert on unactionable state trains operators to ignore alerts."""
    board = _board()
    _assigned(board, claimed_ago=_DISPATCH_STALL_SECONDS + 60)

    first = board.reconcile_invariants()
    second = board.reconcile_invariants()

    assert len([r for r in first if r.get("kind") == "stalled_dispatch"]) == 1
    assert [r for r in second if r.get("kind") == "stalled_dispatch"] == []


def test_activate_discharges_the_claim():
    """A dispatch that WORKED must never reach the sweep."""
    board = _board()
    task = _assigned(board, claimed_ago=_DISPATCH_STALL_SECONDS + 60)

    board.activate(task.id)

    assert task.dispatch_requested_at is None
    assert [r for r in board.reconcile_invariants() if r.get("kind") == "stalled_dispatch"] == []


def test_the_marker_survives_a_persistence_round_trip(tmp_path):
    """The sweep runs in a later process than the dispatch, so this MUST persist.

    Drives the real SQLite store rather than an in-memory board, so this also covers
    the schema v20 column and all three task_store plumbing points. Without
    persistence the marker is lost on restart and the sweep silently never fires —
    the failure mode would be indistinguishable from "no stalled dispatches".
    """
    from swarm.db.core import SwarmDB
    from swarm.db.task_store import SqliteTaskStore

    db_path = tmp_path / "swarm.db"
    board = TaskBoard(store=SqliteTaskStore(SwarmDB(db_path)))
    task = _assigned(board, claimed_ago=30)
    board.persist(task)

    reloaded = TaskBoard(store=SqliteTaskStore(SwarmDB(db_path))).get(task.id)

    assert reloaded is not None
    assert reloaded.dispatch_requested_at == pytest.approx(task.dispatch_requested_at)


# --------------------------------------------------------------------------
# (D) the label, and the v19 -> v20 upgrade
# --------------------------------------------------------------------------


def test_ac4_the_criteria_synthesis_call_passes_a_label():
    """AC4. Source-scanned because the call sits behind a `jira_key` condition.

    Unlabelled, its failures logged as "async call failed" with nothing naming the
    task or the operation — the same forensic dead end #1486 closed for dispatch.
    """
    from pathlib import Path

    src = Path("src/swarm/mcp/queen_handlers/_tasks.py").read_text(encoding="utf-8")
    i = src.index("apply_synthesized_criteria")
    window = src[max(0, i - 300) : i + 300]
    assert "label=" in window, "the criteria-synthesis _fire_async lost its label again"


def test_v20_upgrades_a_v19_database_without_losing_rows(tmp_path):
    """The column must arrive by MIGRATION, not only on fresh DBs.

    Every existing swarm.db is v19. If the ALTER never ran, the marker would silently
    fail to persist on exactly the installs that have history — and a sweep that never
    fires looks identical to a sweep with nothing to find.
    """
    import sqlite3 as _sqlite3

    from swarm.db.core import SwarmDB
    from swarm.db.schema import CURRENT_VERSION

    assert CURRENT_VERSION >= 20, "v20 must be the current schema version"

    db_path = tmp_path / "legacy.db"
    fresh = SwarmDB(db_path)
    conn = _sqlite3.connect(db_path)
    try:
        # Rewind to v19 and drop the column, so the migration has real work to do.
        conn.execute("ALTER TABLE tasks DROP COLUMN dispatch_requested_at")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version VALUES (?, ?)", (19, time.time()))
        conn.execute(
            "INSERT INTO tasks (id, number, title, status) VALUES ('legacy1', 7, 'old', 'assigned')"
        )
        conn.commit()
    finally:
        conn.close()
    fresh.close()

    # POSITIVE CONTROL. Without this, a silently-failed DROP COLUMN leaves the column
    # in place and every assertion below passes while testing nothing at all.
    probe = _sqlite3.connect(db_path)
    try:
        pre_cols = {r[1] for r in probe.execute("PRAGMA table_info(tasks)")}
    finally:
        probe.close()
    assert "dispatch_requested_at" not in pre_cols, (
        "the rewind did not actually remove the column, so this test proves nothing"
    )

    upgraded = SwarmDB(db_path)
    cols = {r[1] for r in upgraded.fetchall("PRAGMA table_info(tasks)")}
    assert "dispatch_requested_at" in cols, "v20 migration did not add the column"

    row = upgraded.fetchone("SELECT dispatch_requested_at FROM tasks WHERE id='legacy1'")
    assert row is not None, "the pre-existing row was lost by the migration"
    assert row[0] is None, "legacy rows must read back NULL — never-claimed, so never swept"
