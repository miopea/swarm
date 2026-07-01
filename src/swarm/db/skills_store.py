"""Skills registry store — persist slash-command skills and their usage.

Skills are named Claude Code slash-commands (``/fix-and-ship``,
``/feature``, ``/verify``, etc.) that Swarm invokes when assigning a
task of a matching type. Before this module, the map lived as a
hardcoded ``dict[TaskType, str]`` in ``tasks/workflows.py``. Moving it
to SQLite gives operators:

  - A single place to see which skills exist and what task types they
    serve.
  - Usage counts — rarely-used skills become obvious candidates for
    retirement.
  - A data surface that future dashboard CRUD and auto-install flows
    can hang off without another migration.

This module is deliberately minimal: read/write/list/record_usage, no
web concerns and no task-type resolution — callers convert ``TaskType``
enums to their value strings before writing here.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from swarm.logging import get_logger

if TYPE_CHECKING:
    from swarm.db.core import SwarmDB

_log = get_logger("db.skills_store")


@dataclass
class SkillRecord:
    """A single skill row — what's stored in the ``skills`` table."""

    name: str
    description: str = ""
    task_types: list[str] = field(default_factory=list)
    usage_count: int = 0
    last_used_at: float | None = None
    created_at: float = field(default_factory=time.time)

    def to_api(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "task_types": list(self.task_types),
            "usage_count": self.usage_count,
            "last_used_at": self.last_used_at,
            "created_at": self.created_at,
        }


class SkillsStore:
    """CRUD + usage tracking for the ``skills`` table."""

    def __init__(self, db: SwarmDB) -> None:
        self._db = db

    def upsert(
        self,
        name: str,
        *,
        description: str = "",
        task_types: list[str] | None = None,
    ) -> SkillRecord:
        """Insert or update a skill. Usage counters are preserved on update."""
        types_json = json.dumps(task_types or [])
        now = time.time()
        # Try insert first; on conflict, update metadata without touching counters.
        self._db.execute(
            """
            INSERT INTO skills (name, description, task_types, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
              description = excluded.description,
              task_types = excluded.task_types
            """,
            (name, description, types_json, now),
        )
        self._db.commit()
        record = self.get(name)
        assert record is not None  # we just wrote it
        return record

    def get(self, name: str) -> SkillRecord | None:
        row = self._db.fetchone("SELECT * FROM skills WHERE name = ?", (name,))
        return _row_to_record(row) if row else None

    def list_all(self) -> list[SkillRecord]:
        rows = self._db.fetchall("SELECT * FROM skills ORDER BY name")
        return [_row_to_record(r) for r in rows]

    def delete(self, name: str) -> bool:
        cursor = self._db.execute("DELETE FROM skills WHERE name = ?", (name,))
        self._db.commit()
        return cursor.rowcount > 0

    def record_usage(self, name: str) -> None:
        """Increment ``usage_count`` and bump ``last_used_at``.

        Silently no-ops for unknown skill names — callers shouldn't
        have to pre-register every ad-hoc slash command they invoke.
        """
        self._db.execute(
            """
            UPDATE skills
            SET usage_count = usage_count + 1,
                last_used_at = ?
            WHERE name = ?
            """,
            (time.time(), name),
        )
        self._db.commit()

    def seed_defaults(self, defaults: dict[str, tuple[str, list[str]]]) -> int:
        """Insert any skills from ``defaults`` that aren't already present.

        ``defaults`` maps ``skill_name`` → ``(description, task_types)``.
        Returns the number of new rows created. Existing rows are not
        updated — seeding is idempotent by design.
        """
        added = 0
        for name, (description, task_types) in defaults.items():
            if self.get(name) is not None:
                continue
            self.upsert(name, description=description, task_types=task_types)
            added += 1
        if added:
            _log.info("seeded %d default skills", added)
        return added


def _row_to_record(row: sqlite3.Row) -> SkillRecord:
    return SkillRecord(
        name=row["name"],
        description=row["description"] or "",
        task_types=json.loads(row["task_types"] or "[]"),
        usage_count=int(row["usage_count"] or 0),
        last_used_at=row["last_used_at"],
        created_at=row["created_at"] or time.time(),
    )
