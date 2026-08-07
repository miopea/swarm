"""Re-importing an archived Jira issue must not duplicate it (Jira blocker 3).

THE SHAPE, and it is one I created and then hit within hours. Archiving is a SOFT
delete: the row survives so its history survives. But ``load()`` hides archived rows
from the board, so anything that derives uniqueness from the board is blind to
identifiers those rows still own.

That already caused a live outage on 2026-08-07. ``TaskBoard.__init__`` derived the
next task number from the loaded tasks, archiving the highest-numbered task made the
counter hand that number out again, and ``swarm_create_task`` died with
``UNIQUE constraint failed: tasks.number`` — all task creation broken swarm-wide.

``jira_key`` is the same fact wearing different clothes, and it was found by looking
rather than by breaking: ``JiraService`` deduped imports against
``self._task_board.all_tasks``, so archiving a Jira-linked task and re-running the
import would create a SECOND task pointing at the same issue. Worse than the number
case in one way — no constraint stops it, so it fails silently and leaves two tasks
tracking one ticket.

Fixed the same way: the store answers for every row it holds, and the board unions
that with what it can see.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm.db.core import SwarmDB
from swarm.db.task_store import SqliteTaskStore
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask


@pytest.fixture
def db(tmp_path: Path) -> SwarmDB:
    return SwarmDB(tmp_path / "swarm.db")


def _linked(board: TaskBoard, key: str) -> SwarmTask:
    task = board.add(SwarmTask(title=f"from {key}", description=""))
    board.set_jira_key(task.id, key)
    return task


def test_known_jira_keys_includes_archived_rows(db: SwarmDB):
    board = TaskBoard(store=SqliteTaskStore(db))
    task = _linked(board, "RCG-101")
    assert "RCG-101" in board.known_jira_keys(), "positive control: a live key must be known"

    board.archive(task.id)
    assert "RCG-101" not in {t.jira_key for t in board.all_tasks}, (
        "positive control: the archived task must be off the board, or this proves nothing"
    )
    assert "RCG-101" in board.known_jira_keys(), (
        "an archived task's jira_key is no longer known, so a re-import would create a "
        "second task pointing at the same issue"
    )


def test_the_key_survives_a_restart(db: SwarmDB):
    """The in-memory set hides the bug until the board is rebuilt from the store —
    exactly how the task-number reuse stayed invisible until a daemon restart."""
    board = TaskBoard(store=SqliteTaskStore(db))
    task = _linked(board, "RCG-202")
    board.archive(task.id)

    reloaded = TaskBoard(store=SqliteTaskStore(db))
    assert "RCG-202" in reloaded.known_jira_keys(), (
        "after a restart the archived key is forgotten entirely; the next import "
        "duplicates the issue"
    )


def test_a_live_key_is_still_known(db: SwarmDB):
    """The ordinary case must keep working — a fix that only ever returns archived keys
    would pass the tests above and break every normal dedupe."""
    board = TaskBoard(store=SqliteTaskStore(db))
    _linked(board, "RCG-303")
    keys = board.known_jira_keys()
    assert keys == {"RCG-303"}, f"expected exactly the live key, got {keys}"


def test_tasks_without_a_jira_key_contribute_nothing(db: SwarmDB):
    board = TaskBoard(store=SqliteTaskStore(db))
    board.add(SwarmTask(title="local work", description=""))
    assert board.known_jira_keys() == set(), (
        "an empty jira_key leaked into the known set, which would make the dedupe "
        "reject unrelated issues"
    )


@pytest.mark.asyncio
async def test_the_client_unions_the_extra_keys():
    """The client must actually honour what the service passes; the service reading the
    right set is useless if the dedupe ignores it."""
    from swarm.integrations.jira import JiraClient

    src = Path("src/swarm/integrations/jira.py").read_text()
    body = src[src.index("async def import_issues") : src.index("async def import_issues") + 2000]
    assert "extra_known_keys" in body, "import_issues no longer accepts the archived keys"
    assert "known_keys |= set(extra_known_keys)" in body, (
        "the extra keys are accepted but never unioned into the dedupe set"
    )
    assert JiraClient is not None


def test_the_service_passes_archived_keys_on_both_import_paths():
    """Both entry points matter: the scheduled sweep AND the drag-one-issue path. Fixing
    one and leaving the other is how #1270/#1281/#1286 became three tickets."""
    src = Path("src/swarm/server/jira_service.py").read_text()
    code = "\n".join(ln for ln in src.split("\n") if not ln.strip().startswith("#"))

    run_import = code[code.index("async def run_import") : code.index("async def import_one")]
    assert "known_jira_keys()" in run_import, (
        "run_import still dedupes from the board alone, which cannot see archived rows"
    )

    import_one = code[code.index("async def import_one") : code.index("async def export_status")]
    assert "known_jira_keys()" in import_one, (
        "import_one still dedupes from all_tasks, so dragging in an archived issue "
        "creates a duplicate"
    )
    assert "all_tasks if t.jira_key" not in code, (
        "a dedupe built from all_tasks survives somewhere in this module"
    )


def test_a_known_but_archived_key_is_reported_rather_than_silently_ignored():
    """Returning None reads as 'nothing happened' in the UI and leaves the operator
    re-importing an issue that was deliberately archived."""
    src = Path("src/swarm/server/jira_service.py").read_text()
    body = src[src.index("async def import_one") : src.index("async def export_status")]
    assert '"archived": True' in body, (
        "an archived-but-known issue returns None, which is indistinguishable from a failed import"
    )
