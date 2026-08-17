"""Finalize a verified Swarm Next task import without claiming work is done."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from swarm.db.core import SwarmDB

FORMAT = "swarm-next-migration"
VERSION = 1
_CLOSED = {"done", "failed", "completed"}
_MAX_TASKS = 10_000


class MigrationFinalizationError(ValueError):
    """The handoff cannot be proven safe, so nothing was changed."""


@dataclass(frozen=True)
class FinalizationPreview:
    batch_id: str
    task_ids: tuple[str, ...]
    changed_task_ids: tuple[str, ...]


@dataclass(frozen=True)
class FinalizationResult:
    batch_id: str
    finalized_tasks: int
    backup_path: Path
    applied_at: float


@dataclass(frozen=True)
class ReversalResult:
    batch_id: str
    restored_tasks: int
    reversed_at: float


def _compact(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _canonical_source(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "installation_id": source.get("installation_id"),
        "schema_version": source.get("schema_version"),
        "exported_at": source.get("exported_at"),
        "snapshot_digest": source.get("snapshot_digest"),
    }


def _canonical_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": task.get("source_id"),
        "title": task.get("title"),
        "description": task.get("description", ""),
        "status": task.get("status"),
        "priority": task.get("priority", ""),
        "assigned_worker": task.get("assigned_worker"),
        "jira_key": task.get("jira_key"),
        "block_reason": task.get("block_reason"),
        "acceptance_criteria": task.get("acceptance_criteria", []),
        "attachment_count": task.get("attachment_count", 0),
        "source_email_id": task.get("source_email_id"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    }


def canonical_bundle_digest(bundle: dict[str, Any]) -> str:
    """Reproduce ``serde_json::to_vec`` over Next's version-one struct."""
    canonical = {
        "format": bundle.get("format"),
        "version": bundle.get("version"),
        "source": _canonical_source(bundle.get("source") or {}),
        "tasks": [_canonical_task(task) for task in bundle.get("tasks") or []],
    }
    return hashlib.sha256(_compact(canonical)).hexdigest()


def legacy_installation_id(db: SwarmDB) -> str:
    """Reproduce the worker-roster identity used by Next's Legacy exporter."""
    digest = hashlib.sha256()
    for row in db.fetchall("SELECT id, name FROM workers ORDER BY id"):
        digest.update(str(row["id"]).encode())
        digest.update(b"\0")
        digest.update(str(row["name"]).encode())
        digest.update(b"\xff")
    return f"legacy-{digest.hexdigest()}"


def load_json(path: Path, *, max_bytes: int = 16 * 1024 * 1024) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > max_bytes:
        raise MigrationFinalizationError(f"Invalid or oversized migration file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationFinalizationError(f"Could not read migration file: {path}") from exc
    if not isinstance(value, dict):
        raise MigrationFinalizationError(f"Migration file must contain one JSON object: {path}")
    return value


def _json_list(value: object) -> list[object]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _record_from_row(row: Any) -> dict[str, Any]:
    return {
        "source_id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "status": row["status"],
        "priority": row["priority"],
        "assigned_worker": row["assigned_worker"],
        "jira_key": row["jira_key"],
        "block_reason": row["block_reason"],
        "acceptance_criteria": [str(item) for item in _json_list(row["acceptance_criteria"])],
        "attachment_count": len(_json_list(row["attachments"])),
        "source_email_id": row["source_email_id"],
        "created_at": int(row["created_at"]) if row["created_at"] is not None else None,
        "updated_at": int(row["updated_at"]) if row["updated_at"] is not None else None,
    }


def _task_digest(record: dict[str, Any]) -> str:
    return hashlib.sha256(_compact(_canonical_task(record))).hexdigest()


class NextMigrationFinalizer:
    """Verify, finalize, and optionally restore Legacy source tasks."""

    def __init__(self, db: SwarmDB) -> None:
        self.db = db

    def preview(self, bundle: dict[str, Any], receipt: dict[str, Any]) -> FinalizationPreview:
        tasks = bundle.get("tasks")
        source = bundle.get("source")
        if (
            bundle.get("format") != FORMAT
            or bundle.get("version") != VERSION
            or not isinstance(source, dict)
            or not isinstance(tasks, list)
            or len(tasks) > _MAX_TASKS
        ):
            raise MigrationFinalizationError("Unsupported or invalid migration bundle")

        digest = canonical_bundle_digest(bundle)
        identity = legacy_installation_id(self.db)
        if (
            receipt.get("bundle_digest") != digest
            or receipt.get("source_installation_id") != source.get("installation_id")
            or receipt.get("source_snapshot_digest") != source.get("snapshot_digest")
            or source.get("installation_id") != identity
        ):
            raise MigrationFinalizationError(
                "The Next receipt does not match this exact Legacy Hive and export"
            )

        selected = receipt.get("imported_source_ids")
        batch_id = str(receipt.get("batch_id") or "").strip()
        if not batch_id or not isinstance(selected, list) or not selected:
            raise MigrationFinalizationError("Receipt has no imported Legacy tasks")
        task_map = {
            str(task.get("source_id")): _canonical_task(task)
            for task in tasks
            if isinstance(task, dict) and task.get("source_id")
        }
        selected_ids = tuple(str(value) for value in selected)
        if len(set(selected_ids)) != len(selected_ids) or any(
            task_id not in task_map for task_id in selected_ids
        ):
            raise MigrationFinalizationError("Receipt names unknown or duplicate source tasks")

        changed: list[str] = []
        for task_id in selected_ids:
            exported = task_map[task_id]
            if exported["jira_key"] or str(exported["status"]).lower() in _CLOSED:
                raise MigrationFinalizationError(
                    f"Receipt includes a Jira or closed source task: {task_id}"
                )
            row = self.db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
            if row is None or row["archived_at"] is not None:
                changed.append(task_id)
            elif _canonical_task(_record_from_row(row)) != exported:
                changed.append(task_id)
        if changed:
            raise MigrationFinalizationError(
                "Source tasks changed since export; nothing was finalized: " + ", ".join(changed)
            )
        return FinalizationPreview(batch_id, selected_ids, tuple(changed))

    def finish(
        self,
        bundle: dict[str, Any],
        receipt: dict[str, Any],
        *,
        backup_dir: Path | None = None,
    ) -> FinalizationResult:
        batch_id = str(receipt.get("batch_id") or "").strip()
        prior = self.db.fetchone(
            "SELECT batch_id, backup_path, applied_at, reversed_at FROM next_migration_batches "
            "WHERE batch_id = ? AND bundle_digest = ?",
            (batch_id, receipt.get("bundle_digest")),
        )
        if prior is not None and prior["reversed_at"] is None:
            count = self.db.fetchone(
                "SELECT COUNT(*) AS count FROM next_migration_tasks WHERE batch_id = ?",
                (batch_id,),
            )
            assert count is not None
            return FinalizationResult(
                batch_id,
                int(count["count"]),
                Path(prior["backup_path"]),
                float(prior["applied_at"]),
            )

        preview = self.preview(bundle, receipt)
        target = backup_dir or (self.db.path.parent / "backups")
        target.mkdir(parents=True, exist_ok=True)
        backup_path = target / f"swarm-pre-next-{preview.batch_id}.db"
        if backup_path.exists():
            raise MigrationFinalizationError(f"Safety backup already exists: {backup_path}")
        self.db.backup(backup_path)
        applied_at = time.time()
        try:
            with self.db.transaction() as connection:
                connection.execute(
                    "INSERT INTO next_migration_batches "
                    "(batch_id, bundle_digest, source_installation_id, source_snapshot_digest, "
                    "receipt_json, backup_path, applied_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        preview.batch_id,
                        receipt["bundle_digest"],
                        receipt["source_installation_id"],
                        receipt["source_snapshot_digest"],
                        json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                        str(backup_path),
                        applied_at,
                    ),
                )
                for task_id in preview.task_ids:
                    row = connection.execute(
                        "SELECT * FROM tasks WHERE id = ? AND archived_at IS NULL",
                        (task_id,),
                    ).fetchone()
                    if row is None:
                        raise MigrationFinalizationError(
                            f"Source task changed during finalization: {task_id}"
                        )
                    migrated = _record_from_row(row)
                    migrated["status"] = "migrated"
                    migrated["assigned_worker"] = None
                    connection.execute(
                        "UPDATE tasks SET status = 'migrated', assigned_worker = NULL "
                        "WHERE id = ? AND archived_at IS NULL",
                        (task_id,),
                    )
                    connection.execute(
                        "INSERT INTO next_migration_tasks "
                        "(batch_id, task_id, original_status, original_assigned_worker, "
                        "migration_task_digest) VALUES (?, ?, ?, ?, ?)",
                        (
                            preview.batch_id,
                            task_id,
                            row["status"],
                            row["assigned_worker"],
                            _task_digest(migrated),
                        ),
                    )
                    connection.execute(
                        "INSERT INTO task_history (task_id, action, actor, detail, created_at) "
                        "VALUES (?, 'MIGRATED', 'migration', ?, ?)",
                        (
                            task_id,
                            f"Moved to Swarm Next; receipt batch {preview.batch_id}",
                            applied_at,
                        ),
                    )
        except Exception:
            raise
        return FinalizationResult(preview.batch_id, len(preview.task_ids), backup_path, applied_at)

    def reverse(self, batch_id: str) -> ReversalResult:
        batch = self.db.fetchone(
            "SELECT reversed_at FROM next_migration_batches WHERE batch_id = ?", (batch_id,)
        )
        if batch is None:
            raise MigrationFinalizationError(f"Unknown migration batch: {batch_id}")
        if batch["reversed_at"] is not None:
            rows = self.db.fetchone(
                "SELECT COUNT(*) AS count, MAX(reversed_at) AS reversed_at "
                "FROM next_migration_tasks WHERE batch_id = ?",
                (batch_id,),
            )
            assert rows is not None
            return ReversalResult(batch_id, int(rows["count"]), float(rows["reversed_at"]))

        links = self.db.fetchall(
            "SELECT * FROM next_migration_tasks WHERE batch_id = ? ORDER BY task_id", (batch_id,)
        )
        changed: list[str] = []
        for link in links:
            row = self.db.fetchone("SELECT * FROM tasks WHERE id = ?", (link["task_id"],))
            if (
                row is None
                or row["archived_at"] is not None
                or _task_digest(_record_from_row(row)) != link["migration_task_digest"]
            ):
                changed.append(link["task_id"])
        if changed:
            raise MigrationFinalizationError(
                "Source tasks changed after finalization; reversal stopped: " + ", ".join(changed)
            )

        reversed_at = time.time()
        with self.db.transaction() as connection:
            for link in links:
                connection.execute(
                    "UPDATE tasks SET status = ?, assigned_worker = ? WHERE id = ?",
                    (link["original_status"], link["original_assigned_worker"], link["task_id"]),
                )
                connection.execute(
                    "UPDATE next_migration_tasks SET reversed_at = ? "
                    "WHERE batch_id = ? AND task_id = ?",
                    (reversed_at, batch_id, link["task_id"]),
                )
                connection.execute(
                    "INSERT INTO task_history (task_id, action, actor, detail, created_at) "
                    "VALUES (?, 'MIGRATION_REVERSED', 'migration', ?, ?)",
                    (link["task_id"], f"Restored from Swarm Next batch {batch_id}", reversed_at),
                )
            connection.execute(
                "UPDATE next_migration_batches SET reversed_at = ? WHERE batch_id = ?",
                (reversed_at, batch_id),
            )
        return ReversalResult(batch_id, len(links), reversed_at)
