"""#1840 — archiving had no inverse, and raw SQL had no correct form.

TWO DEFECTS, ONE ROOT. ``archived_at`` is the authority for whether a task counts, and
``status`` is deliberately left untouched on archive (#1839, operator ruling — the status
records what the task WAS). That is right, and it has two consequences nobody built for:

1. **No way back.** ``archive()`` existed; nothing undid it. Restoring a task meant a raw
   ``UPDATE swarm.db`` — which the daemon's in-memory board never sees. The row goes live,
   the board stays unaware, and the next ``save()`` classifies it as removed and HARD
   deletes it, cascading its task_history away. The one-way door's only exit destroyed the
   thing walking through it.

2. **Raw SQL had no obvious correct form.** ``SELECT ... WHERE status='assigned'`` returned
   22 and was read as a congested board. 15 were archived; the board was showing 7. Not an
   error — a CONFIDENT WRONG COUNT, which is the shape that survives review.

The view fixes (2) by making the correct query the obvious one. It must NOT be applied to
``jira_keys``, which is the single place that must see archived rows; a dedupe blind to
them silently creates a DUPLICATE task on re-import.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from swarm.db.core import SwarmDB
from swarm.db.schema import CURRENT_VERSION
from swarm.db.task_store import SqliteTaskStore
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask, TaskStatus

_SRC = Path(__file__).resolve().parent.parent / "src" / "swarm"


@pytest.fixture
def store(tmp_path):
    db = SwarmDB(tmp_path / "swarm.db")
    yield SqliteTaskStore(db)
    db.close()


def _task(number: int, *, jira_key: str = "", status: TaskStatus = TaskStatus.UNASSIGNED):
    return SwarmTask(
        id=f"t{number}",
        number=number,
        title=f"task {number}",
        description="",
        status=status,
        jira_key=jira_key,
        created_at=time.time(),
    )


# ---------------------------------------------------------------------------
# AC1 — the inverse exists, and it is symmetric with archive()
# ---------------------------------------------------------------------------


def test_unarchive_clears_the_stamp_and_the_row_returns_to_load(store):
    store.save({"t1": _task(1)})
    assert store.archive("t1") is True
    assert "t1" not in store.load()

    assert store.unarchive("t1") is True

    assert "t1" in store.load()


def test_unarchive_reports_no_op_rather_than_success(store):
    """Same contract as ``archive``: a caller must not be able to report success for a
    row it did not change. Both misses return False, for different reasons."""
    store.save({"t1": _task(1)})

    assert store.unarchive("t1") is False, "a LIVE task was reported as restored"
    assert store.unarchive("nosuch") is False, "a MISSING task was reported as restored"


def test_get_archived_refuses_to_return_a_live_task(store):
    """A reader that returned live tasks would let a restore insert a SECOND copy of
    something already on the board — divergence rather than a visible no-op."""
    store.save({"t1": _task(1)})

    assert store.get_archived("t1") is None

    store.archive("t1")
    restored = store.get_archived("t1")

    assert restored is not None and restored.number == 1


def test_the_restored_task_keeps_the_status_it_was_archived_with(store):
    """#1839's ruling, pinned from the other end. Archiving does not overwrite ``status``,
    so a restore has nothing to reconstruct — and must not invent a fresh one."""
    store.save({"t1": _task(1, status=TaskStatus.ASSIGNED)})
    store.archive("t1")

    store.unarchive("t1")

    assert store.load()["t1"].status is TaskStatus.ASSIGNED


def test_lookup_by_display_number_finds_only_archived_rows(store):
    store.save({"t1": _task(1), "t2": _task(2)})
    store.archive("t2")

    assert store.get_archived_by_number(1) is None
    found = store.get_archived_by_number(2)
    assert found is not None and found.id == "t2"


# ---------------------------------------------------------------------------
# The board layer — the reason a raw UPDATE is not an acceptable substitute
# ---------------------------------------------------------------------------


def test_a_restored_task_survives_the_next_persist(store):
    """THE LOAD-BEARING TEST. ``save()`` deletes any live row missing from memory. A raw
    UPDATE clears the stamp without telling the board, so the very next persist HARD
    deletes the task — the restore destroys what it was restoring. Going through the
    board is what makes that impossible."""
    board = TaskBoard(store=store)
    board.add(_task(1))
    board.archive("t1")

    assert board.unarchive("t1") is True

    board._persist()
    assert "t1" in store.load(), "the restored row was deleted by the next persist"
    assert board.get("t1") is not None


def test_the_raw_update_failure_mode_is_real_not_theoretical(store):
    """POSITIVE CONTROL for the test above: prove the hazard exists by reproducing it.
    Without this, 'the restored row survived' says nothing — it would also pass if
    persist never deleted anything."""
    board = TaskBoard(store=store)
    board.add(_task(1))
    board.archive("t1")

    # What a direct swarm.db edit does: the row goes live, the board never hears.
    store._db.execute("UPDATE tasks SET archived_at = NULL WHERE id = ?", ("t1",))
    store._db.commit()
    assert "t1" in store.load()

    board._persist()

    assert "t1" not in store.load(), (
        "the raw-UPDATE hazard did not reproduce — this test is no longer a control and "
        "the one above proves nothing"
    )


def test_the_board_refuses_to_restore_something_already_on_it(store):
    board = TaskBoard(store=store)
    board.add(_task(1))

    assert board.unarchive("t1") is False


def test_find_archived_does_not_see_live_tasks(store):
    board = TaskBoard(store=store)
    board.add(_task(7))

    assert board.find_archived(7) is None
    board.archive("t7")
    assert board.find_archived(7) is not None


# ---------------------------------------------------------------------------
# AC2 — the live_tasks view
# ---------------------------------------------------------------------------


def test_the_view_exists_at_the_current_schema_version(store):
    row = store._db.fetchall("SELECT MAX(version) AS v FROM schema_version")[0]
    assert row["v"] == CURRENT_VERSION

    views = store._db.fetchall("SELECT name FROM sqlite_master WHERE type = 'view'")
    assert [v["name"] for v in views] == ["live_tasks"]


def test_the_view_and_a_raw_status_query_disagree_exactly_as_the_incident_did(store):
    """The incident, reproduced small: a status query counts archived rows and the board
    does not. The view is the affordance that makes the second count the easy one."""
    store.save({f"t{i}": _task(i, status=TaskStatus.ASSIGNED) for i in range(1, 11)})
    for i in range(1, 8):
        store.archive(f"t{i}")

    raw = store._db.fetchall("SELECT COUNT(*) AS n FROM tasks WHERE status = 'assigned'")
    live = store._db.fetchall("SELECT COUNT(*) AS n FROM live_tasks WHERE status = 'assigned'")

    assert raw[0]["n"] == 10
    assert live[0]["n"] == 3 == len(store.load())


def test_an_existing_db_gains_the_view_by_migration(tmp_path):
    """AC2 covers live databases, not only fresh ones. A view that only appears in the
    fresh DDL would be absent from every DB that matters."""
    path = tmp_path / "old.db"
    SwarmDB(path).close()

    # Rewind it to the version before the view existed. DELETE first: the table keeps a
    # row per applied version and the check reads MAX(version), so merely inserting an
    # older row leaves the DB looking current and nothing migrates.
    raw = sqlite3.connect(path)
    try:
        raw.execute("DROP VIEW IF EXISTS live_tasks")
        raw.execute("DELETE FROM schema_version")
        raw.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (CURRENT_VERSION - 1, time.time()),
        )
        raw.commit()
        before = raw.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='view'").fetchone()
    finally:
        raw.close()
    assert before[0] == 0, "the rewind did not take — this test would pass without migrating"

    migrated = SwarmDB(path)
    try:
        views = migrated.fetchall("SELECT name FROM sqlite_master WHERE type = 'view'")
        assert [v["name"] for v in views] == ["live_tasks"]
    finally:
        migrated.close()


# ---------------------------------------------------------------------------
# AC3 — the one query that must stay unfiltered
# ---------------------------------------------------------------------------


def test_jira_key_dedupe_still_sees_archived_rows(store):
    """OPERATOR RULING, PINNED: task_store's jira_key sweep is deliberately unfiltered.
    An archived task keeps its key, and a dedupe that cannot see it re-imports the issue
    as a brand-new duplicate task. Nothing errors — you just get two."""
    store.save({"t1": _task(1, jira_key="WWD-1"), "t2": _task(2, jira_key="WWD-2")})
    store.archive("t1")

    assert "t1" not in store.load()
    assert store.jira_keys() == {"WWD-1", "WWD-2"}


def test_nobody_swapped_the_dedupe_onto_the_view():
    """The failure this file exists to prevent LATER: someone tidying for consistency
    points jira_keys at live_tasks. Behaviour above catches it today; this catches the
    edit, which is the actual risk once a view makes the swap look like an improvement."""
    import inspect

    from swarm.db import task_store as mod

    body = inspect.getsource(mod.SqliteTaskStore.jira_keys)
    # Strip the docstring AND the comments: both deliberately name ``live_tasks`` in
    # order to say it must not be used here, and a sweep that tripped on its own
    # warning would be a guard nobody could document.
    after_doc = body.split('"""')[2] if body.count('"""') >= 2 else body
    code = "\n".join(ln for ln in after_doc.splitlines() if not ln.strip().startswith("#"))

    assert "live_tasks" not in code, (
        "jira_keys was pointed at live_tasks — archived jira_keys are now invisible to "
        "the dedupe, and re-importing an archived issue silently creates a duplicate task"
    )
    assert "archived_at" not in code, "jira_keys grew an archived_at filter — same defect"


def test_the_deliberate_choice_is_documented_at_the_query():
    """A note there was rated more valuable than the view itself, because the view is
    what makes the wrong edit look right."""
    src = (_SRC / "db" / "task_store.py").read_text()
    note = src.split("def jira_keys")[1].split("def archive")[0]

    assert "DELIBERATELY UNFILTERED" in note
    assert "live_tasks" in note
