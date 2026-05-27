"""Tests for ``swarm.db.migrate`` — legacy file → swarm.db auto-migration.

This module had ~537 LOC of migration logic with no test coverage. The
migration runs once on first daemon startup against the operator's
``~/.swarm/`` directory, so a failure here is a data-loss class: legacy
tasks, proposals, and history could disappear into renamed files without
ever landing in the DB.

We exercise the happy paths for the structured migrations (tasks,
task_history, proposals, pipelines), then the resilience paths: corrupt
JSON, missing files, idempotent re-runs (the DB-already-populated guard),
and the *.migrated rename behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarm.db.core import SwarmDB
from swarm.db.migrate import (
    _normalize_status,
    auto_migrate,
)


@pytest.fixture
def fresh_db(tmp_path: Path) -> SwarmDB:
    return SwarmDB(tmp_path / "swarm.db")


@pytest.fixture
def swarm_dir(tmp_path: Path) -> Path:
    d = tmp_path / "swarm-home"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# _normalize_status — pre-v9 status vocabulary translation
# ---------------------------------------------------------------------------


def test_normalize_status_translates_legacy_vocabulary() -> None:
    """The v9 cleanup renamed proposed→backlog, pending→unassigned, etc."""
    assert _normalize_status("proposed") == "backlog"
    assert _normalize_status("pending") == "unassigned"
    assert _normalize_status("in_progress") == "active"
    assert _normalize_status("completed") == "done"


def test_normalize_status_passes_through_current_vocabulary() -> None:
    """Already-current values aren't double-mapped."""
    assert _normalize_status("assigned") == "assigned"
    assert _normalize_status("done") == "done"
    assert _normalize_status("failed") == "failed"


def test_normalize_status_handles_empty_and_none() -> None:
    """Empty / None / missing legacy field defaults to 'unassigned'."""
    assert _normalize_status(None) == "unassigned"
    assert _normalize_status("") == "unassigned"
    assert _normalize_status("   ") == "unassigned"


# ---------------------------------------------------------------------------
# auto_migrate — happy path: tasks.json → tasks table
# ---------------------------------------------------------------------------


def test_migrates_tasks_json_to_tasks_table(fresh_db: SwarmDB, swarm_dir: Path) -> None:
    """Legacy tasks.json lands in the tasks table; the file is renamed."""
    legacy = swarm_dir / "tasks.json"
    legacy.write_text(
        json.dumps(
            [
                {
                    "id": "t-1",
                    "number": 1,
                    "title": "Task One",
                    "description": "first",
                    "status": "in_progress",  # legacy vocabulary
                    "priority": "high",
                    "task_type": "bug",
                    "assigned_worker": "swarm",
                    "cost_spent": 0.42,
                },
                {
                    "id": "t-2",
                    "number": 2,
                    "title": "Task Two",
                    "description": "second",
                    "status": "completed",  # legacy vocabulary
                    "priority": "normal",
                    "task_type": "chore",
                    "assigned_worker": None,
                },
            ]
        )
    )

    n = auto_migrate(fresh_db, swarm_dir)

    assert n >= 1
    rows = fresh_db.fetchall("SELECT id, status FROM tasks ORDER BY id")
    statuses = {r[0]: r[1] for r in rows}
    # Legacy 'in_progress' -> v9 'active'; 'completed' -> 'done'.
    assert statuses["t-1"] == "active"
    assert statuses["t-2"] == "done"
    # File renamed so a second run doesn't re-import it.
    assert not legacy.exists()
    assert (swarm_dir / "tasks.json.migrated").exists()


def test_skip_tasks_migration_when_target_already_populated(
    fresh_db: SwarmDB, swarm_dir: Path
) -> None:
    """If tasks table already has rows, migration is a no-op and leaves
    the legacy file in place — the operator can decide what to do."""
    fresh_db.execute(
        "INSERT INTO tasks (id, number, title, status) VALUES (?, ?, ?, ?)",
        ("existing", 99, "preexisting", "unassigned"),
    )
    fresh_db.commit()

    legacy = swarm_dir / "tasks.json"
    legacy.write_text(json.dumps([{"id": "t-1", "title": "should not import"}]))

    auto_migrate(fresh_db, swarm_dir)

    # The legacy task is NOT imported (preexisting row was the guard).
    rows = fresh_db.fetchall("SELECT id FROM tasks")
    assert {r[0] for r in rows} == {"existing"}
    # And the file is left in place — the guard skipped the rename.
    assert legacy.exists()


def test_corrupt_tasks_json_does_not_raise(fresh_db: SwarmDB, swarm_dir: Path) -> None:
    """A corrupt tasks.json is logged, skipped, and migration continues."""
    (swarm_dir / "tasks.json").write_text("{not valid json")

    # Should not raise — migration is best-effort per file.
    auto_migrate(fresh_db, swarm_dir)

    # Nothing landed in the table.
    rows = fresh_db.fetchall("SELECT id FROM tasks")
    assert rows == []


def test_missing_legacy_files_yields_zero(fresh_db: SwarmDB, swarm_dir: Path) -> None:
    """No legacy files → migration is a complete no-op (returns 0)."""
    result = auto_migrate(fresh_db, swarm_dir)
    assert result == 0


# ---------------------------------------------------------------------------
# auto_migrate — proposals.json
# ---------------------------------------------------------------------------


def test_migrates_proposals_json(fresh_db: SwarmDB, swarm_dir: Path) -> None:
    """Legacy proposals.json is a dict with 'proposals' and 'history' lists —
    not a flat list. ``history`` items with status='pending' get rewritten
    to 'expired' since the system already finalized them."""
    legacy = swarm_dir / "proposals.json"
    legacy.write_text(
        json.dumps(
            {
                "proposals": [
                    {
                        "id": "p-active",
                        "worker_name": "swarm",
                        "task_id": "t-1",
                        "task_title": "Active proposal",
                        "proposal_type": "completion",
                        "status": "pending",
                    }
                ],
                "history": [
                    {
                        "id": "p-stale",
                        "worker_name": "swarm",
                        "task_id": "t-2",
                        "task_title": "Stale proposal",
                        "proposal_type": "completion",
                        # 'pending' in history is rewritten to 'expired'
                        # by the migration so it can't be re-resolved.
                        "status": "pending",
                    }
                ],
            }
        )
    )

    auto_migrate(fresh_db, swarm_dir)

    rows = fresh_db.fetchall("SELECT id, status FROM proposals ORDER BY id")
    by_id = {r[0]: r[1] for r in rows}
    assert by_id == {"p-active": "pending", "p-stale": "expired"}
    assert (swarm_dir / "proposals.json.migrated").exists()


# ---------------------------------------------------------------------------
# auto_migrate — idempotency across reruns
# ---------------------------------------------------------------------------


def test_idempotent_rerun_does_not_duplicate(fresh_db: SwarmDB, swarm_dir: Path) -> None:
    """Running auto_migrate twice produces the same DB state. Once a file
    is renamed to ``.migrated`` it isn't picked up on the second sweep."""
    legacy = swarm_dir / "tasks.json"
    legacy.write_text(
        json.dumps([{"id": "t-1", "number": 1, "title": "once", "status": "unassigned"}])
    )

    auto_migrate(fresh_db, swarm_dir)
    first_count = fresh_db.fetchone("SELECT COUNT(*) FROM tasks")[0]

    # Second pass — legacy file is now .migrated, so nothing to do.
    auto_migrate(fresh_db, swarm_dir)
    second_count = fresh_db.fetchone("SELECT COUNT(*) FROM tasks")[0]

    assert first_count == 1
    assert second_count == 1


# ---------------------------------------------------------------------------
# auto_migrate — task_history.jsonl line-by-line
# ---------------------------------------------------------------------------


def test_migrates_task_history_jsonl(fresh_db: SwarmDB, swarm_dir: Path) -> None:
    """History rows reference tasks via FK with ON DELETE CASCADE — the
    parent task must exist or the insert silently no-ops."""
    fresh_db.execute(
        "INSERT INTO tasks (id, number, title, status) VALUES (?, ?, ?, ?)",
        ("t-1", 1, "parent", "active"),
    )
    fresh_db.commit()

    legacy = swarm_dir / "task_history.jsonl"
    legacy.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "task_id": "t-1",
                        "action": "created",
                        "actor": "operator",
                        "detail": "manual",
                        "timestamp": 1_700_000_000.0,
                    }
                ),
                json.dumps(
                    {
                        "task_id": "t-1",
                        "action": "assigned",
                        "actor": "drone",
                        "detail": "swarm",
                        "timestamp": 1_700_000_100.0,
                    }
                ),
                "",  # blank line — must be skipped silently
                "{corrupt",  # malformed line — skipped, no raise
            ]
        )
    )

    auto_migrate(fresh_db, swarm_dir)

    rows = fresh_db.fetchall("SELECT task_id, action FROM task_history ORDER BY created_at")
    # SwarmDB uses sqlite3.Row factory — compare via indexed access.
    assert [(r[0], r[1]) for r in rows] == [("t-1", "created"), ("t-1", "assigned")]
    assert (swarm_dir / "task_history.jsonl.migrated").exists()
