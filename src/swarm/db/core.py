"""SwarmDB — unified SQLite storage for all swarm state.

Single connection, thread-safe via lock, WAL mode for concurrent reads.
All public methods acquire the lock before accessing the connection.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from swarm.db.schema import CURRENT_VERSION, PRAGMAS, SCHEMA_V1
from swarm.logging import get_logger

_log = get_logger("db")

_DEFAULT_DB_PATH = Path.home() / ".swarm" / "swarm.db"


class SwarmDB:
    """Unified SQLite storage backend.

    Usage::

        db = SwarmDB()          # opens ~/.swarm/swarm.db
        db = SwarmDB(path)      # custom path (tests)

    The database is created with all tables on first open.  On subsequent
    opens the schema version is checked and migrations are applied if needed.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _DEFAULT_DB_PATH
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._open()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _open(self) -> None:
        """Open (or create) the database and apply schema."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(
                str(self.path),
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            # Apply pragmas line-by-line (executescript auto-commits)
            for line in PRAGMAS.strip().splitlines():
                line = line.strip()
                if line and not line.startswith("--"):
                    self._conn.execute(line)
            self._ensure_schema()
            # Secure permissions — secrets live in this DB
            try:
                os.chmod(str(self.path), 0o600)
            except OSError:
                pass
            _log.info("swarm.db opened at %s (v%d)", self.path, CURRENT_VERSION)
        except sqlite3.Error:
            _log.error("failed to open swarm.db at %s", self.path, exc_info=True)
            self._conn = None

    def _ensure_schema(self) -> None:
        """Create tables if needed, track schema version."""
        assert self._conn is not None
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        if cur.fetchone() is None:
            # Fresh DB — create everything
            self._conn.executescript(SCHEMA_V1)
            self._conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (CURRENT_VERSION, time.time()),
            )
            self._conn.commit()
            _log.info("created schema v%d", CURRENT_VERSION)
            return

        # Check version for future migrations
        row = self._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        db_version = row[0] if row and row[0] else 0
        if db_version < CURRENT_VERSION:
            self._apply_migrations(db_version)

    def _apply_migrations(self, from_version: int) -> None:
        """Apply incremental migrations.

        Data-driven registry: each entry is ``(version, migrate_fn)`` and runs
        when the DB is older than that version. Append new migrations here and
        bump ``CURRENT_VERSION`` in schema.py (plus the matching fresh DDL).
        """
        assert self._conn is not None
        _log.info("migrating schema from v%d to v%d", from_version, CURRENT_VERSION)
        migrations: list[tuple[int, Callable[[], None]]] = [
            (2, self._migrate_v2_indexes),
            (3, self._migrate_v3_group_worker_order),
            (4, self._migrate_v4_composite_index),
            (5, self._migrate_v5_skills),
            (6, self._migrate_v6_queen_chat),
            (7, self._migrate_v7_worker_blockers),
            (8, self._migrate_v8_verification_fields),
            (9, self._migrate_v9_status_rename),
            (10, self._migrate_v10_playbooks),
            (11, self._migrate_v11_block_reason),
            (12, self._migrate_v12_messages_dedup_index),
            (13, self._migrate_v13_query_indexes),
            (14, self._migrate_v14_started_at),
            (15, self._migrate_v15_external_blocker_ref),
        ]
        for version, migrate in migrations:
            if from_version < version:
                migrate()
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (CURRENT_VERSION, time.time()),
        )
        self._conn.commit()

    def _migrate_v2_indexes(self) -> None:
        """v2: add indexes for approval_rules, proposals, and buzz_log."""
        assert self._conn is not None
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_approval_rules_owner"
            " ON approval_rules(owner_type, owner_id)"
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_proposals_task ON proposals(task_id)")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_proposals_status_time ON proposals(status, created_at)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_buzz_worker_time ON buzz_log(worker_name, timestamp)"
        )
        _log.info("v2: added 4 indexes")

    def _migrate_v3_group_worker_order(self) -> None:
        """v3: add sort_order to group_workers for member ordering."""
        assert self._conn is not None
        try:
            self._conn.execute(
                "ALTER TABLE group_workers ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
            )
            _log.info("v3: added sort_order to group_workers")
        except Exception:
            _log.debug("v3 migration: sort_order column likely already exists")

    def _migrate_v4_composite_index(self) -> None:
        """v4: add composite index on tasks(assigned_worker, status)."""
        assert self._conn is not None
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_assigned_status ON tasks(assigned_worker, status)"
        )
        _log.info("v4: added composite index idx_tasks_assigned_status")

    def _migrate_v5_skills(self) -> None:
        """v5: add skills registry table."""
        assert self._conn is not None
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS skills (
              name           TEXT PRIMARY KEY,
              description    TEXT NOT NULL DEFAULT '',
              task_types     TEXT NOT NULL DEFAULT '[]',
              usage_count    INTEGER NOT NULL DEFAULT 0,
              last_used_at   REAL,
              created_at     REAL NOT NULL
            )
            """
        )
        _log.info("v5: added skills registry table")

    def _migrate_v6_queen_chat(self) -> None:
        """v6: interactive Queen chat — threads, messages, learnings + proposals.thread_id."""
        assert self._conn is not None
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS queen_threads (
              id                 TEXT PRIMARY KEY,
              title              TEXT NOT NULL DEFAULT '',
              kind               TEXT NOT NULL DEFAULT 'operator',
              status             TEXT NOT NULL DEFAULT 'active',
              worker_name        TEXT,
              task_id            TEXT,
              created_at         REAL NOT NULL,
              updated_at         REAL NOT NULL,
              resolved_at        REAL,
              resolved_by        TEXT,
              resolution_reason  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_queen_threads_status
              ON queen_threads(status);
            CREATE INDEX IF NOT EXISTS idx_queen_threads_kind
              ON queen_threads(kind);
            CREATE INDEX IF NOT EXISTS idx_queen_threads_worker
              ON queen_threads(worker_name);
            CREATE INDEX IF NOT EXISTS idx_queen_threads_updated
              ON queen_threads(updated_at);

            CREATE TABLE IF NOT EXISTS queen_messages (
              id          INTEGER PRIMARY KEY,
              thread_id   TEXT NOT NULL
                REFERENCES queen_threads(id) ON DELETE CASCADE,
              role        TEXT NOT NULL,
              content     TEXT NOT NULL,
              widgets     TEXT NOT NULL DEFAULT '[]',
              ts          REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_queen_messages_thread
              ON queen_messages(thread_id);
            CREATE INDEX IF NOT EXISTS idx_queen_messages_ts
              ON queen_messages(ts);

            CREATE TABLE IF NOT EXISTS queen_learnings (
              id          INTEGER PRIMARY KEY,
              context     TEXT NOT NULL,
              correction  TEXT NOT NULL,
              applied_to  TEXT NOT NULL DEFAULT '',
              thread_id   TEXT,
              created_at  REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_queen_learnings_applied
              ON queen_learnings(applied_to);
            """
        )
        # Add thread_id to proposals if missing (ALTER is idempotent-guarded
        # via a try/except because SQLite has no IF NOT EXISTS on columns).
        try:
            self._conn.execute("ALTER TABLE proposals ADD COLUMN thread_id TEXT")
        except sqlite3.OperationalError:
            # Column likely already exists (fresh DB path already has it).
            _log.debug("v6 migration: proposals.thread_id column likely already exists")
        _log.info("v6: added queen_threads, queen_messages, queen_learnings + proposals.thread_id")

    def _migrate_v7_worker_blockers(self) -> None:
        """v7: worker_blockers table for task #250 (IdleWatcher respects
        reported blockers). IF NOT EXISTS guards make this idempotent
        across fresh-schema + migration paths.
        """
        assert self._conn is not None
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS worker_blockers (
              worker           TEXT    NOT NULL,
              task_number      INTEGER NOT NULL,
              blocked_by_task  INTEGER NOT NULL,
              reason           TEXT    NOT NULL DEFAULT '',
              created_at       REAL    NOT NULL,
              PRIMARY KEY (worker, task_number)
            );
            CREATE INDEX IF NOT EXISTS idx_worker_blockers_worker
              ON worker_blockers(worker);
            """
        )
        _log.info("v7: added worker_blockers table")

    def _migrate_v8_verification_fields(self) -> None:
        """v8: add verifier-drone fields to tasks (item 4 of 10-repo bundle).

        Three new columns track the verifier's verdict per task:

        * ``verification_status`` — NOT_RUN / VERIFIED / REOPENED /
          ESCALATED / SKIPPED.
        * ``verification_reason`` — most recent verifier rationale.
        * ``verification_reopen_count`` — self-loop guard counter.

        ALTER TABLE is wrapped in try/except because SQLite has no
        IF NOT EXISTS on columns; freshly-created tables (via the v8
        schema) already include them.
        """
        assert self._conn is not None
        for stmt in (
            "ALTER TABLE tasks ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'not_run'",
            "ALTER TABLE tasks ADD COLUMN verification_reason TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE tasks ADD COLUMN verification_reopen_count INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError:
                _log.debug("v8 migration: column likely already exists (%s)", stmt)
        _log.info("v8: added verification_* fields to tasks")

    def _migrate_v9_status_rename(self) -> None:
        """v9: rename task status enum values to the operator-facing vocabulary.

        Maps the legacy four values that changed (``proposed``, ``pending``,
        ``in_progress``, ``completed``) to their new spellings (``backlog``,
        ``unassigned``, ``active``, ``done``). ``assigned`` and ``failed``
        already match. The migration is idempotent: re-running on a v9 DB is
        a no-op because the source values no longer exist.
        """
        assert self._conn is not None
        renames = (
            ("proposed", "backlog"),
            ("pending", "unassigned"),
            ("in_progress", "active"),
            ("completed", "done"),
        )
        for old, new in renames:
            self._conn.execute("UPDATE tasks SET status = ? WHERE status = ?", (new, old))
        _log.info("v9: renamed task statuses to backlog/unassigned/active/done")

    def _migrate_v10_playbooks(self) -> None:
        """v10: playbook-synthesis-loop tables (Phase 1).

        ``playbooks`` (self-improving procedural memory synthesized from
        successful tasks) + ``playbook_events`` (audit/refinement signal).
        Distinct from the v5 ``skills`` registry. FTS is layered on by
        ``PlaybookStore`` at runtime, not here, so a missing-fts5 build
        cannot break this migration. ``CREATE TABLE IF NOT EXISTS`` keeps
        it idempotent on re-run.
        """
        assert self._conn is not None
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS playbooks (
              id                   TEXT PRIMARY KEY,
              name                 TEXT NOT NULL UNIQUE,
              title                TEXT NOT NULL DEFAULT '',
              scope                TEXT NOT NULL DEFAULT 'global',
              trigger              TEXT NOT NULL DEFAULT '',
              body                 TEXT NOT NULL DEFAULT '',
              provenance_task_ids  TEXT NOT NULL DEFAULT '[]',
              source_worker        TEXT NOT NULL DEFAULT '',
              confidence           REAL NOT NULL DEFAULT 0.0,
              uses                 INTEGER NOT NULL DEFAULT 0,
              wins                 INTEGER NOT NULL DEFAULT 0,
              losses               INTEGER NOT NULL DEFAULT 0,
              status               TEXT NOT NULL DEFAULT 'candidate',
              version              INTEGER NOT NULL DEFAULT 1,
              content_hash         TEXT NOT NULL DEFAULT '',
              created_at           REAL NOT NULL,
              updated_at           REAL NOT NULL,
              last_used_at         REAL,
              retired_reason       TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_playbooks_scope_status
              ON playbooks(scope, status);
            CREATE INDEX IF NOT EXISTS idx_playbooks_content_hash
              ON playbooks(content_hash);
            CREATE TABLE IF NOT EXISTS playbook_events (
              id           INTEGER PRIMARY KEY,
              playbook_id  TEXT NOT NULL,
              task_id      TEXT NOT NULL DEFAULT '',
              worker       TEXT NOT NULL DEFAULT '',
              event        TEXT NOT NULL,
              ts           REAL NOT NULL,
              detail       TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_playbook_events_pb
              ON playbook_events(playbook_id, ts);
            """
        )
        _log.info("v10: added playbooks + playbook_events tables")

    def _migrate_v11_block_reason(self) -> None:
        """v11 (#405): add ``tasks.block_reason`` for the new BLOCKED
        status. ALTER wrapped in try/except — SQLite has no ADD COLUMN
        IF NOT EXISTS and fresh DBs already have it via SCHEMA_V1.
        """
        assert self._conn is not None
        try:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN block_reason TEXT NOT NULL DEFAULT ''")
            _log.info("v11: added tasks.block_reason")
        except sqlite3.OperationalError:
            _log.debug("v11 migration: block_reason column likely already exists")

    def _migrate_v15_external_blocker_ref(self) -> None:
        """v15 (#876): add ``tasks.external_blocker_ref`` — a free-text
        reference to the EXTERNAL/upstream dependency a BLOCKED task is
        waiting on (no internal task number exists for it). Defaults to ''
        for legacy rows. ALTER wrapped in try/except — fresh DBs already have
        it via SCHEMA_V1.
        """
        assert self._conn is not None
        try:
            self._conn.execute(
                "ALTER TABLE tasks ADD COLUMN external_blocker_ref TEXT NOT NULL DEFAULT ''"
            )
            _log.info("v15: added tasks.external_blocker_ref")
        except sqlite3.OperationalError:
            _log.debug("v15 migration: external_blocker_ref column likely already exists")

    def _migrate_v14_started_at(self) -> None:
        """v14 (#611 P2): add ``tasks.started_at`` (when the task last went
        ACTIVE) for the INV-1 earliest-started tiebreak. Nullable — legacy rows
        stay NULL and fall back to ``created_at``. ALTER wrapped in try/except —
        fresh DBs already have it via SCHEMA_V1.
        """
        assert self._conn is not None
        try:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN started_at REAL")
            _log.info("v14: added tasks.started_at")
        except sqlite3.OperationalError:
            _log.debug("v14 migration: started_at column likely already exists")

    def _migrate_v12_messages_dedup_index(self) -> None:
        """v12: composite index matching MessageStore.send()'s dedup probe.

        Every inter-worker message runs ``WHERE sender=? AND recipient=?
        AND msg_type=? AND created_at > ?``.  The pre-v12 indexes
        (``idx_messages_recipient`` and ``idx_messages_unread``) only
        covered ``recipient``, so dedup was effectively a full table
        scan against the leftmost-column-only index.  This index makes
        the hot path index-covered.

        Wrapped in try/except so legacy-bootstrap paths whose DBs
        pre-date the ``messages`` table (older v8-era schemas built
        by hand in tests) don't crash the migration chain — fresh DBs
        always have the table via SCHEMA_V1.
        """
        assert self._conn is not None
        try:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_dedup"
                " ON messages(sender, recipient, msg_type, created_at)"
            )
            _log.info("v12: added idx_messages_dedup composite index")
        except sqlite3.OperationalError:
            _log.debug("v12 migration: messages table missing, skipping index")

    def _migrate_v13_query_indexes(self) -> None:
        """v13: indexes for the Queen's triage scans over growing tables.

        ``buzz_log`` is filtered by ``category`` + ``timestamp`` (the
        drone-actions view) and ``messages`` by a bare ``created_at`` range
        (the message-stream view) — neither was index-covered. Both tables
        grow unbounded, so these scans degrade over a long-running daemon.

        ``CREATE INDEX IF NOT EXISTS`` is idempotent; wrapped in try/except so
        legacy-bootstrap DBs missing a table don't break the chain (fresh DBs
        always have both via SCHEMA_V1).
        """
        assert self._conn is not None
        try:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_buzz_category_time ON buzz_log(category, timestamp)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at)"
            )
            _log.info("v13: added idx_buzz_category_time + idx_messages_created_at")
        except sqlite3.OperationalError:
            _log.debug("v13 migration: a target table is missing, skipping index")

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    @property
    def connected(self) -> bool:
        return self._conn is not None

    # ------------------------------------------------------------------
    # Low-level access (all acquire lock)
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> sqlite3.Cursor:
        """Execute a single SQL statement. Returns cursor."""
        with self._lock:
            if not self._conn:
                raise RuntimeError("SwarmDB is not connected")
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, params_seq: list[tuple[Any, ...]]) -> sqlite3.Cursor:
        """Execute SQL for each parameter set."""
        with self._lock:
            if not self._conn:
                raise RuntimeError("SwarmDB is not connected")
            return self._conn.executemany(sql, params_seq)

    def executescript(self, sql: str) -> None:
        """Execute multiple SQL statements."""
        with self._lock:
            if not self._conn:
                raise RuntimeError("SwarmDB is not connected")
            self._conn.executescript(sql)

    def commit(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.commit()

    def fetchone(
        self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()
    ) -> sqlite3.Row | None:
        """Execute and return first row."""
        with self._lock:
            if not self._conn:
                raise RuntimeError("SwarmDB is not connected")
            return self._conn.execute(sql, params).fetchone()

    def fetchall(
        self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()
    ) -> list[sqlite3.Row]:
        """Execute and return all rows."""
        with self._lock:
            if not self._conn:
                raise RuntimeError("SwarmDB is not connected")
            return self._conn.execute(sql, params).fetchall()

    def insert(self, table: str, data: dict[str, Any]) -> int:
        """Insert a row and return lastrowid. Auto-commits."""
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        with self._lock:
            if not self._conn:
                raise RuntimeError("SwarmDB is not connected")
            cur = self._conn.execute(sql, tuple(data.values()))
            self._conn.commit()
            return cur.lastrowid or 0

    def update(
        self, table: str, data: dict[str, Any], where: str, where_params: tuple[Any, ...]
    ) -> int:
        """Update rows matching where clause. Returns rows affected."""
        set_clause = ", ".join(f"{k} = ?" for k in data)
        sql = f"UPDATE {table} SET {set_clause} WHERE {where}"
        params = tuple(data.values()) + where_params
        with self._lock:
            if not self._conn:
                raise RuntimeError("SwarmDB is not connected")
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount

    def delete(self, table: str, where: str, where_params: tuple[Any, ...]) -> int:
        """Delete rows matching where clause. Returns rows affected."""
        sql = f"DELETE FROM {table} WHERE {where}"
        with self._lock:
            if not self._conn:
                raise RuntimeError("SwarmDB is not connected")
            cur = self._conn.execute(sql, where_params)
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def checkpoint(self) -> None:
        """Run a WAL checkpoint to consolidate the WAL file."""
        with self._lock:
            if self._conn:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def backup(self, dest: Path | None = None) -> Path:
        """Create a backup of the database. Returns backup path."""
        dest = dest or self.path.with_suffix(".db.bak")
        with self._lock:
            if self._conn:
                # Use SQLite backup API for consistency
                bak = sqlite3.connect(str(dest))
                try:
                    self._conn.backup(bak)
                finally:
                    bak.close()
        try:
            os.chmod(str(dest), 0o600)
        except OSError:
            pass
        _log.info("database backed up to %s", dest)
        return dest

    def integrity_check(self) -> bool:
        """Run PRAGMA integrity_check. Returns True if OK."""
        with self._lock:
            if not self._conn:
                return False
            result = self._conn.execute("PRAGMA integrity_check").fetchone()
            ok = result is not None and result[0] == "ok"
            if not ok:
                _log.error("integrity check failed: %s", result)
            return ok

    def stats(self) -> dict[str, int]:
        """Return row counts for all tables."""
        tables = [
            "config",
            "workers",
            "groups",
            "approval_rules",
            "tasks",
            "task_history",
            "proposals",
            "buzz_log",
            "messages",
            "pipelines",
            "secrets",
            "queen_sessions",
            "queen_threads",
            "queen_messages",
            "queen_learnings",
        ]
        result: dict[str, int] = {}
        with self._lock:
            if not self._conn:
                return result
            for table in tables:
                try:
                    row = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    result[table] = row[0] if row else 0
                except sqlite3.OperationalError:
                    result[table] = -1
        return result

    def db_size(self) -> int:
        """Return total size of DB + WAL files in bytes."""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total


# ----------------------------------------------------------------------
# Backup restore — module-level so the CLI can run it without a live
# SwarmDB handle on the target (the target file gets replaced).
# ----------------------------------------------------------------------


def find_latest_backup(backup_dir: Path) -> Path | None:
    """Return the newest ``swarm_*.db`` backup in ``backup_dir``, or None."""
    try:
        candidates = sorted(backup_dir.glob("swarm_*.db"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    return candidates[-1] if candidates else None


def restore_backup(backup: Path, db_path: Path | None = None) -> Path:
    """Replace the database file with a verified backup copy.

    The current database is kept at ``<db>.pre-restore`` so a bad restore
    is itself reversible. WAL/SHM sidecars are removed — they belong to
    the replaced file and would corrupt the restored one.

    Raises ``FileNotFoundError`` if the backup is missing and
    ``ValueError`` if it fails SQLite's integrity check.
    """
    import shutil

    db_path = db_path or _DEFAULT_DB_PATH
    if not backup.exists():
        raise FileNotFoundError(f"backup not found: {backup}")

    # Verify the backup is a healthy SQLite database BEFORE touching the live file.
    try:
        conn = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise ValueError(f"backup is not a readable SQLite database: {e}") from e
    if row is None or row[0] != "ok":
        raise ValueError(f"backup failed integrity check: {row[0] if row else 'no result'}")

    if db_path.exists():
        pre = db_path.with_suffix(".db.pre-restore")
        shutil.copy2(db_path, pre)
        try:
            os.chmod(str(pre), 0o600)
        except OSError:
            pass
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)

    shutil.copy2(backup, db_path)
    try:
        os.chmod(str(db_path), 0o600)
    except OSError:
        pass
    _log.warning("database restored from %s", backup)
    return db_path
