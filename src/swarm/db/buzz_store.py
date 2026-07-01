"""SQLite buzz log store — replaces both system.jsonl and system_log.db."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

from swarm.db.base_store import BaseStore
from swarm.logging import get_logger

if TYPE_CHECKING:
    from swarm.db.core import SwarmDB

_log = get_logger("db.buzz_store")

_DEFAULT_MAX_AGE_DAYS = 30

# Explicit column list — what ``_row_to_dict`` actually consumes.
# Pinning these avoids accidentally inflating query results when the
# buzz_log schema gains a column (added defensively after an audit
# flagged the SELECT *).
_BUZZ_COLS = (
    "id, timestamp, action, worker_name, detail, category, is_notification, metadata, repeat_count"
)


class BuzzStore(BaseStore):
    """Buzz log persistence backed by the buzz_log table in swarm.db.

    Replaces both the JSONL file and the separate system_log.db.
    Provides the same query/analytics interface as the old LogStore.
    """

    def __init__(self, db: SwarmDB) -> None:
        self._db = db

    def insert(
        self,
        *,
        timestamp: float,
        action: str,
        worker_name: str,
        detail: str = "",
        category: str = "drone",
        is_notification: bool = False,
        metadata: dict[str, Any] | None = None,
        repeat_count: int = 1,
    ) -> int:
        """Insert a log entry. Returns the row ID."""
        return self._db.insert(
            "buzz_log",
            {
                "timestamp": timestamp,
                "action": action,
                "worker_name": worker_name,
                "detail": detail,
                "category": category,
                "is_notification": 1 if is_notification else 0,
                "metadata": json.dumps(metadata) if metadata else "{}",
                "repeat_count": repeat_count,
            },
        )

    def load_recent(self, limit: int = 200) -> list[dict[str, Any]]:
        """Load the most recent entries (for startup hydration)."""
        rows = self._db.fetchall(
            f"SELECT {_BUZZ_COLS} FROM buzz_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        result = []
        for r in reversed(rows):  # Reverse to chronological order
            result.append(_row_to_dict(r))
        return result

    def query(
        self,
        *,
        worker_name: str | None = None,
        action: str | None = None,
        category: str | None = None,
        since: float | None = None,
        until: float | None = None,
        overridden: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Query log entries with filters."""
        conditions: list[str] = []
        params: list[Any] = []
        if worker_name:
            conditions.append("worker_name = ?")
            params.append(worker_name)
        if action:
            conditions.append("action = ?")
            params.append(action)
        if category:
            conditions.append("category = ?")
            params.append(category)
        if since is not None:
            conditions.append("timestamp >= ?")
            params.append(since)
        if until is not None:
            conditions.append("timestamp <= ?")
            params.append(until)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = (
            f"SELECT {_BUZZ_COLS} FROM buzz_log "
            f"WHERE {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])
        rows = self._db.fetchall(sql, tuple(params))
        return [_row_to_dict(r) for r in rows]

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Free-text search across detail and worker_name fields."""
        rows = self._db.fetchall(
            f"SELECT {_BUZZ_COLS} FROM buzz_log "
            f"WHERE detail LIKE ? OR worker_name LIKE ? "
            f"ORDER BY timestamp DESC LIMIT ?",
            (f"%{query}%", f"%{query}%", limit),
        )
        return [_row_to_dict(r) for r in rows]

    def count(
        self,
        *,
        worker_name: str | None = None,
        action: str | None = None,
        since: float | None = None,
        overridden: bool | None = None,
    ) -> int:
        """Count entries matching filters."""
        conditions: list[str] = []
        params: list[Any] = []
        if worker_name:
            conditions.append("worker_name = ?")
            params.append(worker_name)
        if action:
            conditions.append("action = ?")
            params.append(action)
        if since is not None:
            conditions.append("timestamp >= ?")
            params.append(since)

        where = " AND ".join(conditions) if conditions else "1=1"
        row = self._db.fetchone(
            f"SELECT COUNT(*) FROM buzz_log WHERE {where}",
            tuple(params),
        )
        return row[0] if row else 0

    def rule_analytics(self, *, since: float | None = None) -> list[dict[str, Any]]:
        """Aggregate per-rule firing statistics."""
        conditions = ["action IN ('CONTINUED', 'ESCALATED')"]
        params: list[Any] = []
        if since is not None:
            conditions.append("timestamp >= ?")
            params.append(since)
        where = " AND ".join(conditions)
        rows = self._db.fetchall(
            f"SELECT action, detail, COUNT(*) as count "
            f"FROM buzz_log WHERE {where} "
            f"GROUP BY action, detail ORDER BY count DESC LIMIT 50",
            tuple(params),
        )
        return [dict(r) for r in rows]

    def mark_overridden(self, row_id: int, override_action: str) -> bool:
        """Mark a log entry as overridden. No-op for now (schema lacks column)."""
        return True

    def mark_recent_overridden(
        self,
        worker_name: str,
        override_action: str,
        *,
        within_seconds: float = 300.0,
        action_filter: list[str] | None = None,
    ) -> int | None:
        """Mark the most recent matching entry as overridden."""
        return None

    def prune(self, max_age_days: int | None = None) -> int:
        """Delete entries older than max_age_days."""
        days = max_age_days or _DEFAULT_MAX_AGE_DAYS
        return self._prune_older_than("buzz_log", "timestamp", days)

    def close(self) -> None:
        """No-op — lifecycle managed by SwarmDB."""


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    meta = row["metadata"]
    if isinstance(meta, str) or meta is None:
        metadata = BaseStore._parse_json_field(meta, {})
    else:
        metadata = meta
    return {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "action": row["action"],
        "worker_name": row["worker_name"],
        "detail": row["detail"],
        "category": row["category"],
        "is_notification": bool(row["is_notification"]),
        "metadata": metadata,
        "repeat_count": row["repeat_count"] or 1,
    }
