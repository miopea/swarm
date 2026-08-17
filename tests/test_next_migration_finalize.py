"""Safe finalization of tasks already imported into Swarm Next.

The importer is intentionally read-only.  This companion is the explicit second
phase: prove the Next receipt belongs to this exact Legacy snapshot, back up the
database, and mark only the source tasks named by that receipt as read-only and
moved. No task is completed and no running worker is touched.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from swarm.cli import main
from swarm.db.core import SwarmDB
from swarm.migration.next_finalize import (
    MigrationFinalizationError,
    NextMigrationFinalizer,
    canonical_bundle_digest,
    legacy_installation_id,
)


def _seed(db: SwarmDB) -> None:
    db.insert(
        "workers",
        {
            "id": "worker-1",
            "name": "Swarm",
            "path": "/projects/swarm",
            "created_at": 1.0,
        },
    )
    db.insert(
        "tasks",
        {
            "id": "legacy-task-1",
            "number": 1,
            "title": "Move safely",
            "description": "Keep the audit trail",
            "status": "assigned",
            "priority": "high",
            "assigned_worker": "Swarm",
            "created_at": 100.9,
            "updated_at": 200.2,
            "acceptance_criteria": json.dumps(["receipt verified"]),
            "attachments": json.dumps([{"name": "proof.png"}]),
        },
    )


def _bundle(db: SwarmDB) -> dict[str, object]:
    source_id = legacy_installation_id(db)
    return {
        "format": "swarm-next-migration",
        "version": 1,
        "source": {
            "installation_id": source_id,
            "schema_version": 20,
            "exported_at": 300,
            "snapshot_digest": "snapshot-abc",
        },
        "tasks": [
            {
                "source_id": "legacy-task-1",
                "title": "Move safely",
                "description": "Keep the audit trail",
                "status": "assigned",
                "priority": "high",
                "assigned_worker": "Swarm",
                "jira_key": None,
                "block_reason": "",
                "acceptance_criteria": ["receipt verified"],
                "attachment_count": 1,
                "source_email_id": None,
                "created_at": 100,
                "updated_at": 200,
            }
        ],
    }


def _receipt(bundle: dict[str, object]) -> dict[str, object]:
    source = bundle["source"]
    assert isinstance(source, dict)
    return {
        "batch_id": "batch-next-1",
        "bundle_digest": canonical_bundle_digest(bundle),
        "source_installation_id": source["installation_id"],
        "source_snapshot_digest": source["snapshot_digest"],
        "imported_task_ids": ["next-task-1"],
        "imported_source_ids": ["legacy-task-1"],
        "imported_at": 400,
    }


@pytest.fixture
def db(tmp_path: Path) -> SwarmDB:
    value = SwarmDB(tmp_path / "swarm.db")
    _seed(value)
    return value


def test_digest_matches_swarm_next_struct_order() -> None:
    """Rust hashes compact JSON in struct-field order, not input file order."""
    bundle = {
        "tasks": [],
        "source": {
            "snapshot_digest": "s",
            "exported_at": 2,
            "schema_version": 19,
            "installation_id": "i",
        },
        "version": 1,
        "format": "swarm-next-migration",
    }
    canonical = (
        b'{"format":"swarm-next-migration","version":1,"source":'
        b'{"installation_id":"i","schema_version":19,"exported_at":2,'
        b'"snapshot_digest":"s"},"tasks":[]}'
    )
    assert canonical_bundle_digest(bundle) == hashlib.sha256(canonical).hexdigest()


def test_preview_proves_receipt_and_source_without_writing(db: SwarmDB) -> None:
    bundle = _bundle(db)
    preview = NextMigrationFinalizer(db).preview(bundle, _receipt(bundle))

    assert preview.batch_id == "batch-next-1"
    assert preview.task_ids == ("legacy-task-1",)
    assert preview.changed_task_ids == ()
    assert (
        db.fetchone("SELECT archived_at FROM tasks WHERE id = ?", ("legacy-task-1",))["archived_at"]
        is None
    )


def test_preview_rejects_wrong_hive_and_changed_source(db: SwarmDB) -> None:
    bundle = _bundle(db)
    receipt = _receipt(bundle)
    receipt["source_installation_id"] = "another-hive"
    with pytest.raises(MigrationFinalizationError, match="receipt does not match"):
        NextMigrationFinalizer(db).preview(bundle, receipt)

    receipt = _receipt(bundle)
    db.execute("UPDATE tasks SET title = ? WHERE id = ?", ("Changed after export", "legacy-task-1"))
    db.commit()
    with pytest.raises(MigrationFinalizationError, match="changed since export"):
        NextMigrationFinalizer(db).preview(bundle, receipt)


def test_finish_backs_up_marks_visible_read_only_and_records_audit(
    db: SwarmDB, tmp_path: Path
) -> None:
    bundle = _bundle(db)
    receipt = _receipt(bundle)
    result = NextMigrationFinalizer(db).finish(bundle, receipt, backup_dir=tmp_path / "backups")

    assert result.finalized_tasks == 1
    assert result.backup_path.exists()
    row = db.fetchone(
        "SELECT status, assigned_worker, archived_at FROM tasks WHERE id = ?",
        ("legacy-task-1",),
    )
    assert dict(row) == {"status": "migrated", "assigned_worker": None, "archived_at": None}
    event = db.fetchone(
        "SELECT action, detail FROM task_history WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        ("legacy-task-1",),
    )
    assert event["action"] == "MIGRATED"
    assert "batch-next-1" in event["detail"]

    from swarm.db.task_store import SqliteTaskStore
    from swarm.tasks.board import TaskBoard
    from swarm.tasks.task import TaskStatus

    board = TaskBoard(store=SqliteTaskStore(db))
    assert board.get("legacy-task-1").status is TaskStatus.MIGRATED
    assert board.update("legacy-task-1", title="cannot change") is False
    assert board.assign("legacy-task-1", "Swarm") is False
    assert board.force_complete("legacy-task-1", "not actually complete") is False
    assert board.fail("legacy-task-1") is False
    assert board.demote_to_backlog("legacy-task-1") is False
    assert board.release("legacy-task-1") is False
    assert board.archive("legacy-task-1") is False
    assert board.remove("legacy-task-1") is False
    assert board.remove_tasks({"legacy-task-1"}) == 0


def test_finish_is_idempotent_and_reverse_restores_untouched_task(
    db: SwarmDB, tmp_path: Path
) -> None:
    bundle = _bundle(db)
    receipt = _receipt(bundle)
    finalizer = NextMigrationFinalizer(db)
    first = finalizer.finish(bundle, receipt, backup_dir=tmp_path / "backups")
    second = finalizer.finish(bundle, receipt, backup_dir=tmp_path / "backups")
    assert second == first

    reversed_result = finalizer.reverse("batch-next-1")
    assert reversed_result.restored_tasks == 1
    row = db.fetchone(
        "SELECT status, assigned_worker, archived_at FROM tasks WHERE id = ?",
        ("legacy-task-1",),
    )
    assert dict(row) == {
        "status": "assigned",
        "assigned_worker": "Swarm",
        "archived_at": None,
    }
    event = db.fetchone(
        "SELECT action FROM task_history WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        ("legacy-task-1",),
    )
    assert event["action"] == "MIGRATION_REVERSED"


def test_reverse_refuses_a_task_changed_after_finalization(db: SwarmDB, tmp_path: Path) -> None:
    bundle = _bundle(db)
    receipt = _receipt(bundle)
    finalizer = NextMigrationFinalizer(db)
    finalizer.finish(bundle, receipt, backup_dir=tmp_path / "backups")
    db.execute("UPDATE tasks SET description = ? WHERE id = ?", ("tampered", "legacy-task-1"))
    db.commit()

    with pytest.raises(MigrationFinalizationError, match="changed after finalization"):
        finalizer.reverse("batch-next-1")
    assert db.fetchone("SELECT status FROM tasks WHERE id = ?", ("legacy-task-1",))["status"] == (
        "migrated"
    )


def test_cli_previews_and_finishes_only_after_explicit_confirmation(
    db: SwarmDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(db)
    receipt = _receipt(bundle)
    bundle_file = tmp_path / "bundle.json"
    receipt_file = tmp_path / "receipt.json"
    bundle_file.write_text(json.dumps(bundle), encoding="utf-8")
    receipt_file.write_text(json.dumps(receipt), encoding="utf-8")
    runner = CliRunner()

    preview = runner.invoke(
        main,
        [
            "migration",
            "preview",
            str(bundle_file),
            str(receipt_file),
            "--database",
            str(db.path),
        ],
    )
    assert preview.exit_code == 0, preview.output
    assert "Preview only" in preview.output

    monkeypatch.setattr("swarm.cli._require_migration_offline", lambda: None)
    finish = runner.invoke(
        main,
        [
            "migration",
            "finish",
            str(bundle_file),
            str(receipt_file),
            "--database",
            str(db.path),
        ],
        input="y\n",
    )
    assert finish.exit_code == 0, finish.output
    assert "Finalized: 1 task(s)" in finish.output
