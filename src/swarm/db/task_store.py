"""SQLite-backed task store — drop-in replacement for FileTaskStore."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

from swarm.db.base_store import BaseStore
from swarm.logging import get_logger
from swarm.tasks.task import SwarmTask, TaskPriority, TaskStatus, TaskType, VerificationStatus

if TYPE_CHECKING:
    from swarm.db.core import SwarmDB

_log = get_logger("db.task_store")

# Explicit column list — must stay in sync with :func:`_task_to_row`
# (the canonical write shape) and :func:`_row_to_task` (the canonical
# read shape).  Used by :meth:`SqliteTaskStore.load` instead of
# ``SELECT *`` so adding a column elsewhere in the schema (e.g. a v9+
# audit field we don't materialize on SwarmTask) doesn't bloat the
# in-memory rows we load at startup.
_TASK_COLUMNS = (
    "id",
    "number",
    "title",
    "description",
    "status",
    "priority",
    "task_type",
    "assigned_worker",
    "created_at",
    "updated_at",
    "completed_at",
    "started_at",
    "dispatch_requested_at",
    "resolution",
    "block_reason",
    "external_blocker_ref",
    "tags",
    "attachments",
    "depends_on",
    "source_email_id",
    "jira_key",
    "jira_exported_status",
    "is_cross_project",
    "source_worker",
    "target_worker",
    "dependency_type",
    "acceptance_criteria",
    "context_refs",
    "cost_budget",
    "cost_spent",
    "learnings",
    "resolution_note",
    "resolution_note_kind",
    "title_original",
    "verification_status",
    "verification_reason",
    "verification_reopen_count",
    "effort_tier",
)


class SqliteTaskStore(BaseStore):
    """Persist tasks to the unified swarm.db.

    Conforms to the ``TaskStore`` protocol (``save`` / ``load``),
    but also offers single-row helpers so the task board can
    update individual tasks without rewriting everything.
    """

    def __init__(self, db: SwarmDB) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # TaskStore protocol
    # ------------------------------------------------------------------

    def save(self, tasks: dict[str, SwarmTask]) -> None:
        """Write all tasks to the DB (full replace — deletes removed tasks)."""
        # ARCHIVED ROWS ARE NOT CANDIDATES FOR DELETION (#1298). An archived task is
        # deliberately absent from the in-memory board, so an unfiltered "SELECT id
        # FROM tasks" would classify it as removed and DELETE it on the very next
        # persist — cascading its task_history away and defeating the entire point of
        # a soft delete. Scoping this read to live rows is what makes archiving safe.
        existing_ids = {
            r["id"] for r in self._db.fetchall("SELECT id FROM tasks WHERE archived_at IS NULL")
        }
        removed_ids = existing_ids - set(tasks.keys())
        # Batch delete removed tasks in a single statement
        if removed_ids:
            ph = ",".join("?" for _ in removed_ids)
            self._db.execute(f"DELETE FROM tasks WHERE id IN ({ph})", tuple(removed_ids))
        # Upsert current tasks without per-row commits
        for task in tasks.values():
            data = _task_to_row(task)
            cols = ", ".join(data.keys())
            placeholders = ", ".join("?" for _ in data)
            conflict = ", ".join(f"{k} = ?" for k in data)
            sql = (
                f"INSERT INTO tasks ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {conflict}"
            )
            params = tuple(data.values()) + tuple(data.values())
            self._db.execute(sql, params)
        # Single commit for the entire batch
        self._db.commit()

    def load(self) -> dict[str, SwarmTask]:
        """Load all tasks from the DB."""
        # Archived tasks stay out of the board entirely (#1298), so every existing
        # query excludes them without a single call site changing. They remain in the
        # DB with their history intact and are reachable by direct query.
        rows = self._db.fetchall(
            f"SELECT {', '.join(_TASK_COLUMNS)} FROM tasks WHERE archived_at IS NULL"
        )
        tasks: dict[str, SwarmTask] = {}
        for row in rows:
            try:
                task = _row_to_task(row)
                tasks[task.id] = task
            except (KeyError, ValueError):
                _log.warning(
                    "skipping corrupt task row: %s",
                    row["id"] if "id" in row.keys() else "?",
                )
        _log.info("loaded %d tasks from swarm.db", len(tasks))
        return tasks

    # ------------------------------------------------------------------
    # Single-row operations
    # ------------------------------------------------------------------

    def max_number(self) -> int:
        """Highest task number in the DB, INCLUDING archived rows.

        ``load()`` deliberately hides archived tasks from the board, but their rows
        survive and ``number`` carries a UNIQUE constraint — so the board's next-number
        counter has to consider rows it cannot see. Deriving it from visible tasks alone
        reused an archived number and every create then failed with
        ``UNIQUE constraint failed: tasks.number``.
        """
        rows = self._db.fetchall("SELECT MAX(number) AS m FROM tasks")
        if not rows:
            return 0
        value = rows[0]["m"]
        return int(value) if value is not None else 0

    def jira_keys(self) -> set[str]:
        """Every non-empty ``jira_key`` in the DB, INCLUDING archived rows.

        Same shape as :meth:`max_number`, and the same reason. ``load()`` hides archived
        tasks from the board, but their rows survive and keep their identifiers — so a
        dedupe built from the board alone cannot see them. For task NUMBERS that caused
        a live outage (reused number, UNIQUE violation, all creation broken); for
        ``jira_key`` it means re-importing an archived issue silently creates a
        DUPLICATE task instead of recognising it.
        """
        # DELIBERATELY UNFILTERED — DO NOT ADD ``archived_at IS NULL``, AND DO NOT SWAP
        # THIS ONTO THE ``live_tasks`` VIEW (v23, #1840). That view exists so ad-hoc
        # queries stop counting archived rows; this query is the one place that MUST
        # count them. A dedupe blind to archived rows re-imports an archived issue as a
        # brand-new duplicate task, and nothing errors — you get two tasks for one
        # ticket. Consistency with the view would be the bug.
        rows = self._db.fetchall(
            "SELECT jira_key FROM tasks WHERE jira_key IS NOT NULL AND jira_key != ''"
        )
        return {str(r["jira_key"]) for r in rows if r["jira_key"]}

    def archive(self, task_id: str) -> bool:
        """Soft-delete *task_id*: stamp it archived, keeping the row and its history.

        Returns False if the id does not exist or was already archived, so a caller
        cannot report success for a no-op.
        """
        import time

        cur = self._db.execute(
            "UPDATE tasks SET archived_at = ? WHERE id = ? AND archived_at IS NULL",
            (time.time(), task_id),
        )
        self._db.commit()
        return bool(getattr(cur, "rowcount", 0))

    def get_archived(self, task_id: str) -> SwarmTask | None:
        """Read an ARCHIVED row back as a task. Returns None if live or absent.

        The board cannot supply this: ``load()`` filters archived rows out, so an
        archived task exists nowhere in memory. Restoring one has to start from the row.

        Deliberately refuses to return LIVE tasks. A caller that got a live task here
        would "restore" something already on the board and then insert a second copy of
        it into ``_tasks`` — silent divergence rather than a visible no-op.
        """
        rows = self._db.fetchall(
            f"SELECT {', '.join(_TASK_COLUMNS)} FROM tasks "
            "WHERE id = ? AND archived_at IS NOT NULL",
            (task_id,),
        )
        if not rows:
            return None
        try:
            return _row_to_task(rows[0])
        except (KeyError, ValueError):
            _log.warning("archived task row %s is corrupt and cannot be restored", task_id)
            return None

    def get_archived_by_number(self, number: int) -> SwarmTask | None:
        """Same as :meth:`get_archived`, keyed by DISPLAY NUMBER.

        The number is what an operator has; the id is what the write path needs, and an
        archived task is on no board to translate between them.
        """
        rows = self._db.fetchall(
            f"SELECT {', '.join(_TASK_COLUMNS)} FROM tasks "
            "WHERE number = ? AND archived_at IS NOT NULL",
            (number,),
        )
        if not rows:
            return None
        try:
            return _row_to_task(rows[0])
        except (KeyError, ValueError):
            _log.warning("archived task row #%s is corrupt and cannot be restored", number)
            return None

    def unarchive(self, task_id: str) -> bool:
        """The inverse of :meth:`archive` — clear the stamp, putting the row back in scope.

        THERE WAS NO INVERSE UNTIL #1840. Archiving was built as a one-way door, so the
        only way back was a raw UPDATE against swarm.db — which the daemon's in-memory
        board never sees, leaving the row live and the board unaware of it until a
        restart. The next ``save()`` then treats that row as removed and HARD-deletes
        it, cascading its history away. An inverse that goes through the board is the
        difference between restoring a task and quietly destroying it.

        ``AND archived_at IS NOT NULL`` so restoring an already-live task returns False
        rather than reporting success for a no-op — same contract as :meth:`archive`.

        NOTHING RECORDS WHY A TASK WAS ARCHIVED, so nothing can record why it came back
        either; the reason lives in the caller's history entry, not here.
        """
        cur = self._db.execute(
            "UPDATE tasks SET archived_at = NULL WHERE id = ? AND archived_at IS NOT NULL",
            (task_id,),
        )
        self._db.commit()
        return bool(getattr(cur, "rowcount", 0))

    def save_one(self, task: SwarmTask) -> None:
        """Insert or update a single task."""
        data = _task_to_row(task)
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        conflict = ", ".join(f"{k} = ?" for k in data)
        sql = (
            f"INSERT INTO tasks ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {conflict}"
        )
        params = tuple(data.values()) + tuple(data.values())
        self._db.execute(sql, params)
        self._db.commit()

    def delete_one(self, task_id: str) -> bool:
        """Delete a task by ID. Returns True if deleted."""
        return self._db.delete("tasks", "id = ?", (task_id,)) > 0

    def backup(self, max_backups: int = 5) -> None:
        """DB-level backup handled by SwarmDB.backup() — no-op here."""


def _safe_get(row: sqlite3.Row, key: str, default: Any) -> Any:
    """Read a row column that might not exist on legacy DBs (pre-v8)."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _task_to_row(task: SwarmTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "number": task.number,
        "title": task.title,
        "description": task.description,
        "status": task.status.value,
        "priority": task.priority.value,
        "task_type": task.task_type.value,
        "assigned_worker": task.assigned_worker,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "completed_at": task.completed_at,
        "started_at": task.started_at,
        "dispatch_requested_at": task.dispatch_requested_at,
        "resolution": task.resolution,
        "block_reason": task.block_reason,
        "external_blocker_ref": task.external_blocker_ref,
        "tags": json.dumps(task.tags),
        "attachments": json.dumps(task.attachments),
        "depends_on": json.dumps(task.depends_on),
        "source_email_id": task.source_email_id,
        "jira_key": task.jira_key,
        "jira_exported_status": task.jira_exported_status,
        "is_cross_project": 1 if task.is_cross_project else 0,
        "source_worker": task.source_worker,
        "target_worker": task.target_worker,
        "dependency_type": task.dependency_type,
        "acceptance_criteria": json.dumps(task.acceptance_criteria),
        "context_refs": json.dumps(task.context_refs),
        "cost_budget": task.cost_budget,
        "cost_spent": task.cost_spent,
        "learnings": task.learnings,
        "resolution_note": task.resolution_note,
        "resolution_note_kind": task.resolution_note_kind,
        "title_original": task.title_original,
        "verification_status": task.verification_status.value,
        "verification_reason": task.verification_reason,
        "verification_reopen_count": task.verification_reopen_count,
        "effort_tier": task.effort_tier,
    }


def _row_to_task(row: sqlite3.Row) -> SwarmTask:
    _jl = BaseStore._parse_json_field

    return SwarmTask(
        id=row["id"],
        title=row["title"],
        description=row["description"] or "",
        status=TaskStatus(row["status"]),
        priority=TaskPriority(row["priority"] or "normal"),
        task_type=TaskType(row["task_type"] or "chore"),
        assigned_worker=row["assigned_worker"],
        created_at=row["created_at"] or 0.0,
        updated_at=row["updated_at"] or 0.0,
        completed_at=row["completed_at"],
        started_at=_safe_get(row, "started_at", None),
        dispatch_requested_at=_safe_get(row, "dispatch_requested_at", None),
        depends_on=_jl(row["depends_on"], []),
        tags=_jl(row["tags"], []),
        attachments=_jl(row["attachments"], []),
        resolution=row["resolution"] or "",
        block_reason=(row["block_reason"] or "" if "block_reason" in row.keys() else ""),
        external_blocker_ref=_safe_get(row, "external_blocker_ref", ""),
        source_email_id=row["source_email_id"] or "",
        jira_key=row["jira_key"] or "",
        jira_exported_status=(
            row["jira_exported_status"] if "jira_exported_status" in row.keys() else ""
        )
        or "",
        number=row["number"] or 0,
        is_cross_project=bool(row["is_cross_project"]),
        source_worker=row["source_worker"] or "",
        target_worker=row["target_worker"] or "",
        dependency_type=row["dependency_type"] or "blocks",
        acceptance_criteria=_jl(row["acceptance_criteria"], []),
        context_refs=_jl(row["context_refs"], []),
        cost_budget=row["cost_budget"] or 0.0,
        cost_spent=row["cost_spent"] or 0.0,
        learnings=row["learnings"] or "",
        resolution_note=row["resolution_note"] or "",
        resolution_note_kind=row["resolution_note_kind"] or "",
        title_original=row["title_original"] or "",
        verification_status=VerificationStatus(_safe_get(row, "verification_status", "not_run")),
        verification_reason=_safe_get(row, "verification_reason", ""),
        verification_reopen_count=_safe_get(row, "verification_reopen_count", 0) or 0,
        effort_tier=_safe_get(row, "effort_tier", "") or "",
    )
