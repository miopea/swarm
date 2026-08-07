"""Archiving a task must not destroy its history, and must exist on every surface (#1298).

THE GAP. Deleting a task was reachable from the dashboard and nowhere else — 0 of 22
worker verbs, 0 of 17 Queen verbs, 0 CLI actions, against a ``DELETE /api/tasks/{id}``
route that had existed all along. That is the class this repo keeps re-finding: an
operation present on one surface and silently absent from the others (#1288 In Progress,
#1286 parked-start, #1280 blocked exits, #1270/#1281 the HOLD class). Every earlier
instance was found by the operator rather than by looking.

THE SILENT DATA LOSS UNDERNEATH IT, which is worse than the gap. ``task_history.task_id``
is ``REFERENCES tasks(id) ON DELETE CASCADE`` and ``PRAGMA foreign_keys=ON`` is applied
per connection, so the dashboard's × did not merely remove a row — it destroyed every
history entry for that task, the audit trail that ``swarm_get_learnings`` and playbook
synthesis read. Worse, ``TaskManager.remove_task`` appended a ``REMOVED`` event AFTER
the delete, so the one record guaranteed to be missing was the record of the removal
itself: the insert referenced an already-deleted parent and violated the constraint.

THE DESIGN, and why it touches so little. An archived task is stamped with
``tasks.archived_at`` and dropped from the board's in-memory dict, while the row stays
in SQLite. Every existing query reads that dict, so ~40 ``all_tasks`` call sites exclude
archived work without one of them changing. Two store reads make that safe and are the
whole trick: ``load()`` skips archived rows so a restart does not resurrect them, and
``save()`` scopes its "which rows disappeared?" query to live rows — unscoped, it would
classify every archived task as removed and hard-delete it on the next persist,
cascading the history away and defeating the entire mechanism.

Operator decisions, 2026-08-06: expose it to the worker (own, unstarted only) and the
Queen (unrestricted), and switch the existing dashboard delete to soft-delete.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from swarm.db.core import SwarmDB
from swarm.db.task_history import SqliteTaskHistory
from swarm.db.task_store import SqliteTaskStore
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskAction
from swarm.tasks.task import SwarmTask, TaskStatus


@pytest.fixture
def db(tmp_path: Path) -> SwarmDB:
    return SwarmDB(tmp_path / "swarm.db")


@pytest.fixture
def board(db: SwarmDB) -> TaskBoard:
    return TaskBoard(store=SqliteTaskStore(db))


def _seed(board: TaskBoard, worker: str = "api", status: TaskStatus = TaskStatus.ASSIGNED):
    task = board.add(SwarmTask(title="throwaway", description="probe"))
    if status is not TaskStatus.BACKLOG:
        board.assign(task.id, worker)
    if status is TaskStatus.ACTIVE:
        board.activate(task.id)
    return task


# --- the data-loss property, which is the reason this is a soft delete --------


def test_foreign_keys_are_actually_enforced(db: SwarmDB):
    """Positive control for the whole premise. If FK enforcement were OFF the cascade
    would never fire, the 'archiving protects history' rationale would be wrong, and
    the test below would pass for the wrong reason."""
    row = db.fetchall("PRAGMA foreign_keys")
    assert row and next(iter(row[0])) == 1, (
        "PRAGMA foreign_keys is not ON for this connection, so ON DELETE CASCADE would "
        "not fire and this file's premise needs re-checking"
    )


def test_archiving_keeps_the_task_history_a_hard_delete_would_cascade_away(db: SwarmDB):
    """The core property. Both halves are asserted on the SAME row so the comparison is
    real: hard delete destroys the history, archive preserves it."""
    board = TaskBoard(store=SqliteTaskStore(db))
    history = SqliteTaskHistory(db)
    task = _seed(board)
    history.append(task.id, TaskAction.CREATED, actor="api", detail="filed")
    assert len(history.get_events(task.id)) == 1, "positive control: the event must exist first"

    assert board.archive(task.id) is True
    kept = history.get_events(task.id)
    assert len(kept) == 1, (
        f"archiving destroyed the task's history ({len(kept)} rows left); the row must "
        f"survive so ON DELETE CASCADE never fires"
    )
    rows = db.fetchall("SELECT archived_at FROM tasks WHERE id = ?", (task.id,))
    assert rows and rows[0]["archived_at"] is not None, "the row is gone or unstamped"


def test_a_hard_delete_really_would_have_lost_it(db: SwarmDB):
    """The negative half, proving the previous test is not vacuous. If a hard delete did
    NOT cascade, archiving would be protecting against nothing."""
    board = TaskBoard(store=SqliteTaskStore(db))
    history = SqliteTaskHistory(db)
    task = _seed(board)
    history.append(task.id, TaskAction.CREATED, actor="api", detail="filed")

    db.execute("DELETE FROM tasks WHERE id = ?", (task.id,))
    db.commit()
    assert history.get_events(task.id) == [], (
        "a hard delete did NOT cascade the history away, so this file's rationale is "
        "wrong and the design should be revisited"
    )


# --- archived work leaves the board, and stays gone ---------------------------


def test_an_archived_task_leaves_the_board(board: TaskBoard):
    task = _seed(board)
    assert task.number in {t.number for t in board.all_tasks}, "positive control"
    board.archive(task.id)
    assert task.number not in {t.number for t in board.all_tasks}, (
        "the archived task is still on the board, so every query that reads all_tasks still sees it"
    )


def test_a_restart_does_not_resurrect_an_archived_task(db: SwarmDB):
    """``load()`` must skip archived rows. Without that the task returns on the next
    daemon start and the operator deletes it again, forever."""
    board = TaskBoard(store=SqliteTaskStore(db))
    task = _seed(board)
    board.archive(task.id)
    reloaded = TaskBoard(store=SqliteTaskStore(db))
    assert task.number not in {t.number for t in reloaded.all_tasks}, (
        "an archived task came back after reload — load() is not filtering archived rows"
    )


def test_persisting_after_archiving_does_not_hard_delete_the_row(db: SwarmDB):
    """THE TRAP THIS DESIGN HAD TO AVOID, and it is not obvious. ``save()`` treats any
    DB row missing from memory as removed and DELETEs it. An archived task is
    deliberately missing from memory, so an unscoped query would hard-delete it on the
    very next persist — cascading the history away and undoing the whole point."""
    board = TaskBoard(store=SqliteTaskStore(db))
    history = SqliteTaskHistory(db)
    task = _seed(board)
    history.append(task.id, TaskAction.CREATED, actor="api")
    board.archive(task.id)

    # Any subsequent board mutation triggers a full save.
    _seed(board, worker="web")
    _seed(board, worker="web")

    rows = db.fetchall("SELECT id FROM tasks WHERE id = ?", (task.id,))
    assert rows, (
        "the archived row was hard-deleted by a later persist; save() must scope its "
        "existing-ids query to `WHERE archived_at IS NULL`"
    )
    assert len(history.get_events(task.id)) == 1, "history was cascaded away by that delete"


# --- the worker verb's two preconditions --------------------------------------


def _archive(d, worker: str, **args):
    from swarm.mcp.handlers._archive import _handle_archive_task

    return " ".join(p.get("text", "") for p in _handle_archive_task(d, worker, args))


class _D:
    """Daemon stand-in carrying a REAL TaskManager.

    Deliberately not a stub for the manager: the archive verbs now route through
    TaskManager.archive_task, which is where the blocker-row obligation lives, and a
    stub would let a handler "archive" while skipping every shared rule — testing the
    substitute instead of the thing.
    """

    def __init__(self, board: TaskBoard, history: SqliteTaskHistory | None = None, store=None):
        from swarm.drones.log import DroneLog
        from swarm.server.task_manager import TaskManager

        self.task_board = board
        self.task_history = history or _NullHistory()
        self.drone_log = DroneLog()
        self.blocker_store = store
        self.tasks = TaskManager(
            task_board=board,
            task_history=self.task_history,
            drone_log=self.drone_log,
            blocker_store=store,
        )


class _NullHistory:
    """History sink for boards built without one — records nothing, refuses nothing."""

    def append(self, *a, **kw):
        return None


def test_a_worker_can_archive_its_own_unstarted_task(board: TaskBoard):
    task = _seed(board, worker="api")
    out = _archive(_D(board), "api", number=task.number, reason="duplicate of #1")
    assert "archived" in out.lower(), out
    assert task.number not in {t.number for t in board.all_tasks}


def test_a_worker_cannot_archive_another_workers_task(board: TaskBoard):
    task = _seed(board, worker="web")
    out = _archive(_D(board), "api", number=task.number, reason="tidying")
    assert "not yours" in out.lower(), out
    assert task.number in {t.number for t in board.all_tasks}, "it was archived anyway"
    assert "queen_archive_task" in out, "the refusal must name what would resolve it (#1057)"


def test_a_worker_cannot_archive_work_in_progress(board: TaskBoard):
    task = _seed(board, worker="api", status=TaskStatus.ACTIVE)
    out = _archive(_D(board), "api", number=task.number, reason="changed my mind")
    assert "cannot be archived" in out.lower(), out
    assert task.number in {t.number for t in board.all_tasks}
    assert "swarm_park_task" in out or "swarm_complete_task" in out, (
        "the refusal must name the verb that applies instead"
    )


def test_a_worker_cannot_archive_a_closed_task(board: TaskBoard):
    """A closed resolution may already have been served to other workers as a learning.
    The correction path is annotation (#1274), never removal."""
    task = _seed(board, worker="api")
    board.complete(task.id, "done")
    out = _archive(_D(board), "api", number=task.number, reason="tidying")
    assert "cannot be archived" in out.lower(), out
    assert "swarm_annotate_resolution" in out, "must point at the annotation path"


def test_the_reason_is_required(board: TaskBoard):
    task = _seed(board, worker="api")
    out = _archive(_D(board), "api", number=task.number, reason="  ")
    assert "required" in out.lower(), out
    assert task.number in {t.number for t in board.all_tasks}, "archived without a reason"


# --- AC-5: the cross-surface property -----------------------------------------


def test_archiving_exists_on_every_surface_that_can_reach_the_board():
    """AC-5, and the reason this ticket existed at all. Asserted as a PROPERTY over the
    surfaces rather than as "the two verbs I added exist", so a future surface added
    without archiving fails here instead of being discovered by the operator — which is
    how #1270, #1281 and #1286 became three tickets for one class."""
    from swarm.mcp.queen_tools import QUEEN_HANDLERS
    from swarm.mcp.tools import _HANDLERS as HANDLERS

    assert "swarm_archive_task" in HANDLERS, "the worker surface cannot archive a task"
    assert "queen_archive_task" in QUEEN_HANDLERS, "the Queen surface cannot archive a task"

    # Positive control: the lookup really does see a populated registry, so a rename
    # that emptied it could not pass the assertions above.
    assert len(HANDLERS) > 15, f"worker registry looks wrong ({len(HANDLERS)} verbs)"
    assert len(QUEEN_HANDLERS) > 10, f"queen registry looks wrong ({len(QUEEN_HANDLERS)})"


def test_the_dashboard_delete_route_archives_rather_than_hard_deletes():
    """The operator-facing × must go through the soft path too, otherwise the surface
    that people actually use is the one that still destroys history."""
    src = Path("src/swarm/server/task_manager.py").read_text()
    body = src[src.index("def remove_task(") : src.index("def edit_task(")]
    code = "\n".join(ln for ln in body.split("\n") if not ln.strip().startswith(("#", '"""', "*")))
    # Follows the DELEGATION rather than pinning an implementation: remove_task now
    # calls archive_task, the single write path every surface shares, and that is where
    # the board call plus the blocker-row obligation live. Asserting "task_board.archive
    # appears in this function" would have failed on a refactor that made the behaviour
    # MORE correct, which is a test measuring the wrong thing.
    assert "archive_task(" in code, (
        "TaskManager.remove_task no longer routes through archive_task; the dashboard "
        "delete would skip the shared archive obligations"
    )
    assert "task_board.remove(" not in code, "it still hard-deletes"
    manager_src = src[src.index("def archive_task(") : src.index("def remove_task(")]
    assert "task_board.archive(" in manager_src, "archive_task does not archive"
    assert "clear_blocking(" in manager_src, (
        "archive_task does not clear rows where this task is the BLOCKER — a worker can "
        "be left blocked on a task that has left the board"
    )


def test_hard_remove_survives_for_the_test_harness(board: TaskBoard):
    """``remove()`` is deliberately kept as a HARD delete. Test tasks are artefacts with
    no history worth preserving, and testing/operator.py + server/test_runner.py rely on
    them actually leaving the database."""
    task = _seed(board)
    assert board.remove(task.id) is True
    assert task.number not in {t.number for t in board.all_tasks}


def test_archive_is_a_no_op_on_an_unknown_id(board: TaskBoard):
    assert board.archive("no-such-id") is False


def test_archiving_twice_reports_failure_the_second_time(db: SwarmDB):
    """A second archive must not report success — a caller that cannot distinguish
    'archived it' from 'it was already gone' is how a no-op gets logged as an action."""
    board = TaskBoard(store=SqliteTaskStore(db))
    task = _seed(board)
    assert board.archive(task.id) is True
    assert board.archive(task.id) is False


def test_the_migration_is_idempotent(tmp_path: Path):
    """Opening an existing DB twice must not fail on the ALTER — the v18 pattern every
    sibling migration follows."""
    path = tmp_path / "twice.db"
    SwarmDB(path)
    SwarmDB(path)  # must not raise
    con = sqlite3.connect(path)
    cols = [r[1] for r in con.execute("PRAGMA table_info(tasks)")]
    assert "archived_at" in cols, f"archived_at missing after migration: {cols}"


# --- the number counter must not reuse an archived row's number --------------


def test_archiving_the_highest_task_does_not_break_the_next_creation(db: SwarmDB):
    """REGRESSION, and it was a live outage. Archiving keeps the row — including its
    ``number``, which carries a UNIQUE constraint — but ``load()`` excludes archived
    rows, and ``TaskBoard.__init__`` derives ``_next_number`` from the LOADED tasks. So
    archiving the highest-numbered task made the next daemon start hand that number out
    again, and every subsequent create died with
    ``UNIQUE constraint failed: tasks.number``.

    Found by hitting it: after archiving #1305 the board's max live number was 1304
    while the DB still held 1305, and swarm_create_task failed outright. Task creation
    is the swarm's most basic operation, so this is asserted across a RESTART — the
    in-memory counter hides the bug until the board is rebuilt from the store.
    """
    board = TaskBoard(store=SqliteTaskStore(db))
    first = board.add(SwarmTask(title="a", description=""))
    highest = board.add(SwarmTask(title="b", description=""))
    assert highest.number > first.number, "positive control: numbers must increase"

    board.archive(highest.id)

    # Rebuild exactly as a daemon restart does: from the store.
    reloaded = TaskBoard(store=SqliteTaskStore(db))
    created = reloaded.add(SwarmTask(title="after archive", description=""))

    assert created.number != highest.number, (
        f"the new task reused #{highest.number}, which still exists as an archived row "
        f"under a UNIQUE constraint — every create fails from here"
    )
    rows = db.fetchall("SELECT number FROM tasks WHERE number = ?", (highest.number,))
    assert len(rows) == 1, f"number {highest.number} is now duplicated across {len(rows)} rows"


def test_the_counter_accounts_for_archived_rows_after_reload(db: SwarmDB):
    """The property behind the regression: the counter must consider EVERY row that
    holds a number, not merely the ones the board can see."""
    board = TaskBoard(store=SqliteTaskStore(db))
    for i in range(3):
        board.add(SwarmTask(title=f"t{i}", description=""))
    top = max(t.number for t in board.all_tasks)
    board.archive(next(t.id for t in board.all_tasks if t.number == top))

    reloaded = TaskBoard(store=SqliteTaskStore(db))
    nxt = reloaded.add(SwarmTask(title="next", description="")).number
    assert nxt > top, (
        f"next number {nxt} did not clear the archived high-water mark {top}; the "
        f"counter is derived only from visible tasks"
    )


# --- what an archived row still owns (#2 of the four-item audit) --------------


def test_archiving_clears_blocker_rows_in_BOTH_directions(db: SwarmDB):
    """THE GAP the audit found, and the second direction is the dangerous one.

    ``clear_for_task`` removes rows where the task is BLOCKED. Nothing removed rows
    where it is the BLOCKER — so archiving a task that others wait on left them blocked
    on something they can no longer see or clear, with the IdleWatcher nudging about it
    forever. That is #529's shape, made reachable again by archive: the row survives,
    but the task leaves the board.

    It went missing from all three archive surfaces at once because each did its own
    board call and there was no shared place for the obligation to live.
    """
    from swarm.tasks.blockers import BlockerStore

    store = BlockerStore(db)
    board = TaskBoard(store=SqliteTaskStore(db))
    victim = _seed(board, worker="api")
    other = _seed(board, worker="web")

    store.report("web", other.number, victim.number, "waiting on the victim")
    store.report("api", victim.number, other.number, "victim waits on other")
    assert store.list_for_worker("web"), "positive control: the blocker row must exist"

    _D(board, store=store).tasks.archive_task(victim.id, actor="api", reason="probe")

    blocked_on_archived = [
        b for b in store.list_for_worker("web") if b.blocked_by_task == victim.number
    ]
    assert not blocked_on_archived, (
        "a worker is still blocked ON the archived task — it waits on something that "
        "has left the board and cannot be seen or cleared"
    )
    assert not store.list_for_worker("api"), "rows where the archived task was blocked survived"


def test_archiving_scrubs_dependencies_pointing_at_it(db: SwarmDB):
    """The other reference an archived row owns. A live task depending on an invisible
    one can never satisfy that dependency."""
    board = TaskBoard(store=SqliteTaskStore(db))
    dep = _seed(board)
    downstream = board.add(SwarmTask(title="downstream", description="", depends_on=[dep.id]))
    assert dep.id in downstream.depends_on, "positive control"

    board.archive(dep.id)
    assert dep.id not in downstream.depends_on, (
        "a live task still depends on an archived one, which can never complete from "
        "its point of view"
    )


def test_every_archive_surface_uses_the_single_write_path():
    """#1's principle applied to archive: three surfaces, one obligation. Asserted as a
    PROPERTY over the surfaces rather than per-handler, so a fourth surface that calls
    board.archive directly — and therefore skips the blocker rows — fails here."""
    worker = Path("src/swarm/mcp/handlers/_archive.py").read_text()
    queen = Path("src/swarm/mcp/queen_handlers/_tasks.py").read_text()
    manager = Path("src/swarm/server/task_manager.py").read_text()

    for name, src in (("worker verb", worker), ("queen verb", queen)):
        assert "archive_task(" in src, f"{name} does not route through TaskManager.archive_task"
        assert "board.archive(" not in src, (
            f"{name} still calls board.archive directly, bypassing the blocker-row "
            f"obligation that lives in archive_task"
        )
    assert manager.count("def archive_task(") == 1, "there must be exactly one archive path"


def test_archive_keeps_provenance_fields():
    """Deliberately NOT cleared. jira_key and the cross-project fields record where the
    task came from, which stays true after it leaves the board — and an external system
    may still reference it. Pinned so a future 'tidy up on archive' is a decision."""
    src = Path("src/swarm/server/task_manager.py").read_text()
    body = src[src.index("def archive_task(") : src.index("def remove_task(")]
    for field in ("jira_key", "source_worker", "target_worker"):
        assert f"{field} =" not in body, f"archive_task now clears {field}; that is provenance"
