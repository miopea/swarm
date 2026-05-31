"""LogStore — SQLite persistence layer for the system log.

Provides queryable storage alongside the existing JSONL file backend.
Designed as the analytics foundation for drone auto-tuning (Phase 2).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from swarm.logging import get_logger

_log = get_logger("drones.store")

_DEFAULT_DB_PATH = Path.home() / ".swarm" / "system_log.db"
_DEFAULT_MAX_AGE_DAYS = 30

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS decision_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   REAL    NOT NULL,
    action      TEXT    NOT NULL,
    worker_name TEXT    NOT NULL,
    detail      TEXT    NOT NULL DEFAULT '',
    category    TEXT    NOT NULL DEFAULT 'drone',
    is_notification INTEGER NOT NULL DEFAULT 0,
    metadata    TEXT    NOT NULL DEFAULT '{}',
    overridden  INTEGER NOT NULL DEFAULT 0,
    override_action TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_decision_log_timestamp ON decision_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_decision_log_worker ON decision_log(worker_name);
CREATE INDEX IF NOT EXISTS idx_decision_log_action ON decision_log(action);
CREATE INDEX IF NOT EXISTS idx_decision_log_overridden ON decision_log(overridden);
"""


class LogStore:
    """SQLite backend for the system log.

    Thread-safe: uses a per-instance lock since sqlite3 connections
    are not thread-safe by default.  All public methods acquire the lock.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        max_age_days: int = _DEFAULT_MAX_AGE_DAYS,
    ) -> None:
        self._db_path = db_path or _DEFAULT_DB_PATH
        self._max_age_days = max_age_days
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _init_db(self) -> None:
        """Create the database and tables if they don't exist."""
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            _log.info("log store initialized at %s", self._db_path)
        except sqlite3.Error:
            _log.warning("failed to initialize log store at %s", self._db_path, exc_info=True)
            self._conn = None

    def insert(
        self,
        *,
        timestamp: float,
        action: str,
        worker_name: str,
        detail: str = "",
        category: str = "drone",
        is_notification: bool = False,
        metadata: dict[str, object] | None = None,
    ) -> int | None:
        """Insert a log entry.  Returns the row ID or None on failure."""
        if not self._conn:
            return None
        meta_json = json.dumps(metadata) if metadata else "{}"
        with self._lock:
            try:
                cur = self._conn.execute(
                    """INSERT INTO decision_log
                       (timestamp, action, worker_name, detail, category,
                        is_notification, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        timestamp,
                        action,
                        worker_name,
                        detail,
                        category,
                        int(is_notification),
                        meta_json,
                    ),
                )
                self._conn.commit()
                return cur.lastrowid
            except sqlite3.Error:
                _log.warning("failed to insert log entry", exc_info=True)
                return None

    def mark_overridden(self, row_id: int, override_action: str) -> bool:
        """Mark a log entry as overridden by the user."""
        if not self._conn:
            return False
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE decision_log SET overridden = 1, override_action = ? WHERE id = ?",
                    (override_action, row_id),
                )
                self._conn.commit()
                return True
            except sqlite3.Error:
                _log.warning("failed to mark entry %d as overridden", row_id, exc_info=True)
                return False

    def mark_recent_overridden(
        self,
        worker_name: str,
        override_action: str,
        *,
        within_seconds: float = 300.0,
        action_filter: list[str] | None = None,
    ) -> int | None:
        """Mark the most recent matching entry for a worker as overridden.

        Returns the row ID of the matched entry, or None if no match.
        """
        if not self._conn:
            return None
        import time

        since = time.time() - within_seconds
        with self._lock:
            try:
                if action_filter:
                    placeholders = ",".join("?" for _ in action_filter)
                    row = self._conn.execute(
                        f"""SELECT id FROM decision_log
                            WHERE worker_name = ? AND timestamp >= ?
                              AND overridden = 0
                              AND action IN ({placeholders})
                            ORDER BY timestamp DESC LIMIT 1""",
                        (worker_name, since, *action_filter),
                    ).fetchone()
                else:
                    row = self._conn.execute(
                        """SELECT id FROM decision_log
                           WHERE worker_name = ? AND timestamp >= ?
                             AND overridden = 0
                           ORDER BY timestamp DESC LIMIT 1""",
                        (worker_name, since),
                    ).fetchone()
                if not row:
                    return None
                row_id = row["id"]
                self._conn.execute(
                    "UPDATE decision_log SET overridden = 1, override_action = ? WHERE id = ?",
                    (override_action, row_id),
                )
                self._conn.commit()
                return row_id
            except sqlite3.Error:
                _log.warning("failed to mark recent override for %s", worker_name, exc_info=True)
                return None

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
        """Query log entries with filters.  Returns list of dicts."""
        if not self._conn:
            return []
        conditions: list[str] = []
        params: list[object] = []

        if worker_name is not None:
            conditions.append("worker_name = ?")
            params.append(worker_name)
        if action is not None:
            conditions.append("action = ?")
            params.append(action)
        if category is not None:
            conditions.append("category = ?")
            params.append(category)
        if since is not None:
            conditions.append("timestamp >= ?")
            params.append(since)
        if until is not None:
            conditions.append("timestamp <= ?")
            params.append(until)
        if overridden is not None:
            conditions.append("overridden = ?")
            params.append(int(overridden))

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"SELECT * FROM decision_log {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._lock:
            try:
                rows = self._conn.execute(sql, params).fetchall()
                return [self._row_to_dict(r) for r in rows]
            except sqlite3.Error:
                _log.warning("failed to query log store", exc_info=True)
                return []

    def count(
        self,
        *,
        worker_name: str | None = None,
        action: str | None = None,
        since: float | None = None,
        overridden: bool | None = None,
    ) -> int:
        """Count entries matching filters."""
        if not self._conn:
            return 0
        conditions: list[str] = []
        params: list[object] = []

        if worker_name is not None:
            conditions.append("worker_name = ?")
            params.append(worker_name)
        if action is not None:
            conditions.append("action = ?")
            params.append(action)
        if since is not None:
            conditions.append("timestamp >= ?")
            params.append(since)
        if overridden is not None:
            conditions.append("overridden = ?")
            params.append(int(overridden))

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"SELECT COUNT(*) FROM decision_log {where}"

        with self._lock:
            try:
                row = self._conn.execute(sql, params).fetchone()
                return row[0] if row else 0
            except sqlite3.Error:
                return 0

    def get_by_id(self, row_id: int) -> dict[str, Any] | None:
        """Fetch a single log entry by row ID."""
        if not self._conn:
            return None
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT * FROM decision_log WHERE id = ?", (row_id,)
                ).fetchone()
                return self._row_to_dict(row) if row else None
            except sqlite3.Error:
                _log.warning("failed to get entry %d", row_id, exc_info=True)
                return None

    def rule_analytics(self, *, since: float | None = None) -> list[dict[str, Any]]:
        """Aggregate per-rule firing statistics from decision log metadata.

        Groups by (rule_pattern, source) extracted from the JSON metadata column.
        Returns a list of dicts with: rule_pattern, source, total_fires,
        approve_count, escalate_count, override_count, last_fired.
        """
        if not self._conn:
            return []

        conditions: list[str] = []
        params: list[object] = []

        # Only include entries that have a rule_pattern in metadata
        conditions.append("json_extract(metadata, '$.rule_pattern') IS NOT NULL")
        conditions.append("json_extract(metadata, '$.rule_pattern') != ''")

        if since is not None:
            conditions.append("timestamp >= ?")
            params.append(since)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        sql = f"""\
            SELECT
                json_extract(metadata, '$.rule_pattern') AS rule_pattern,
                json_extract(metadata, '$.source')       AS source,
                COUNT(*)                                  AS total_fires,
                SUM(CASE WHEN action = 'CONTINUED' THEN 1 ELSE 0 END) AS approve_count,
                SUM(CASE WHEN action = 'ESCALATED' THEN 1 ELSE 0 END) AS escalate_count,
                SUM(CASE WHEN overridden = 1 THEN 1 ELSE 0 END)       AS override_count,
                MAX(timestamp)                            AS last_fired
            FROM decision_log
            {where}
            GROUP BY rule_pattern, source
            ORDER BY total_fires DESC
        """

        with self._lock:
            try:
                rows = self._conn.execute(sql, params).fetchall()
                return [
                    {
                        "rule_pattern": row["rule_pattern"],
                        "source": row["source"] or "",
                        "total_fires": row["total_fires"],
                        "approve_count": row["approve_count"],
                        "escalate_count": row["escalate_count"],
                        "override_count": row["override_count"],
                        "last_fired": row["last_fired"],
                    }
                    for row in rows
                ]
            except sqlite3.Error:
                _log.warning("failed to query rule analytics", exc_info=True)
                return []

    def prune(self, max_age_days: int | None = None) -> int:
        """Delete entries older than max_age_days.  Returns count deleted."""
        if not self._conn:
            return 0
        import time

        days = max_age_days if max_age_days is not None else self._max_age_days
        cutoff = time.time() - (days * 86400)
        with self._lock:
            try:
                cur = self._conn.execute(
                    "DELETE FROM decision_log WHERE timestamp < ?",
                    (cutoff,),
                )
                self._conn.commit()
                deleted = cur.rowcount
                if deleted:
                    _log.info("pruned %d log entries older than %d days", deleted, days)
                return deleted
            except sqlite3.Error:
                _log.warning("failed to prune log store", exc_info=True)
                return 0

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            with self._lock:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        """Convert a sqlite3.Row to a plain dict."""
        d = dict(row)
        d["is_notification"] = bool(d.get("is_notification", 0))
        d["overridden"] = bool(d.get("overridden", 0))
        try:
            d["metadata"] = json.loads(d.get("metadata", "{}"))
        except (json.JSONDecodeError, TypeError):
            d["metadata"] = {}
        return d
