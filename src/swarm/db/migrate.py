"""Auto-migration from legacy file-based storage to swarm.db.

On first startup, imports data from:
- tasks.json → tasks table
- task_history.jsonl → task_history table
- proposals.json → proposals table
- messages.db → messages table
- system_log.db → buzz_log table
- config.yaml → config, workers, groups, approval_rules tables
- pipelines.json → pipelines, pipeline_stages tables
- graph_tokens.json → secrets table
- jira_tokens.json → secrets table
- passkeys.json → secrets table
- queen session files → queen_sessions table

After successful import, legacy files are renamed to *.migrated.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING

from swarm.logging import get_logger
from swarm.paths import state_dir

if TYPE_CHECKING:
    from swarm.db.core import SwarmDB

_log = get_logger("db.migrate")


def auto_migrate(db: SwarmDB, swarm_dir: Path | None = None) -> int:
    """Run all migrations for legacy files. Returns count of files migrated."""
    swarm_dir = swarm_dir or state_dir()
    migrated = 0

    migrated += _migrate_tasks(db, swarm_dir / "tasks.json")
    migrated += _migrate_task_history(db, swarm_dir / "task_history.jsonl")
    migrated += _migrate_proposals(db, swarm_dir / "proposals.json")
    migrated += _migrate_messages(db, swarm_dir / "messages.db")
    migrated += _migrate_system_log(db, swarm_dir / "system_log.db")
    migrated += _migrate_pipelines(db, swarm_dir / "pipelines.json")
    migrated += _migrate_secrets(db, swarm_dir)
    migrated += _migrate_queen_sessions(db, swarm_dir / "queen")
    migrated += _migrate_config(db, swarm_dir)

    if migrated:
        _log.warning(
            "Migrated %d legacy file(s) to swarm.db — original files renamed to *.migrated",
            migrated,
        )
    return migrated


def _rename_migrated(path: Path) -> None:
    """Rename a file to *.migrated."""
    dest = path.with_suffix(path.suffix + ".migrated")
    try:
        path.rename(dest)
        _log.info("renamed %s → %s", path.name, dest.name)
    except OSError:
        _log.warning("could not rename %s", path)


_LEGACY_STATUS_MAP = {
    "proposed": "backlog",
    "pending": "unassigned",
    "in_progress": "active",
    "completed": "done",
    # 'assigned' and 'failed' unchanged in the v9 vocabulary cleanup.
}


def _normalize_status(raw: object) -> str:
    """Translate pre-v9 status strings to the new vocabulary."""
    val = (str(raw) if raw else "unassigned").strip() or "unassigned"
    return _LEGACY_STATUS_MAP.get(val, val)


def _migrate_tasks(db: SwarmDB, path: Path) -> int:
    """Import tasks.json into the tasks table."""
    if not path.exists():
        return 0
    # Skip if tasks table already has data
    row = db.fetchone("SELECT COUNT(*) FROM tasks")
    if row and row[0] > 0:
        return 0

    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        _log.warning("failed to read %s", path)
        return 0

    if not isinstance(data, list) or not data:
        return 0

    count = 0
    for t in data:
        try:
            db.execute(
                """INSERT OR IGNORE INTO tasks (
                    id, number, title, description, status, priority, task_type,
                    assigned_worker, created_at, updated_at, completed_at,
                    resolution, tags, attachments, depends_on,
                    source_email_id, jira_key, is_cross_project,
                    source_worker, target_worker, dependency_type,
                    acceptance_criteria, context_refs,
                    cost_budget, cost_spent, learnings
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )""",
                (
                    t.get("id", ""),
                    t.get("number"),
                    t.get("title", ""),
                    t.get("description", ""),
                    _normalize_status(t.get("status")),
                    t.get("priority", "normal"),
                    t.get("task_type", "chore"),
                    t.get("assigned_worker"),
                    t.get("created_at"),
                    t.get("updated_at"),
                    t.get("completed_at"),
                    t.get("resolution", ""),
                    json.dumps(t.get("tags", [])),
                    json.dumps(t.get("attachments", [])),
                    json.dumps(t.get("depends_on", [])),
                    t.get("source_email_id"),
                    t.get("jira_key"),
                    1 if t.get("is_cross_project") else 0,
                    t.get("source_worker"),
                    t.get("target_worker"),
                    t.get("dependency_type"),
                    json.dumps(t.get("acceptance_criteria", [])),
                    json.dumps(t.get("context_refs", [])),
                    t.get("cost_budget"),
                    t.get("cost_spent", 0),
                    t.get("learnings", ""),
                ),
            )
            count += 1
        except sqlite3.Error:
            _log.warning("failed to import task %s", t.get("id", "?"), exc_info=True)
    db.commit()
    _log.info("imported %d tasks from %s", count, path.name)
    _rename_migrated(path)
    # Also rename backup files
    for bak in path.parent.glob("tasks.json.bak.*"):
        _rename_migrated(bak)
    return 1


def _migrate_task_history(db: SwarmDB, path: Path) -> int:
    """Import task_history.jsonl into the task_history table."""
    if not path.exists():
        return 0
    row = db.fetchone("SELECT COUNT(*) FROM task_history")
    if row and row[0] > 0:
        return 0

    count = 0
    try:
        for line in path.read_text().strip().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                db.execute(
                    """INSERT INTO task_history (task_id, action, actor, detail, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        entry.get("task_id", ""),
                        entry.get("action", ""),
                        entry.get("actor", ""),
                        entry.get("detail", ""),
                        entry.get("timestamp", time.time()),
                    ),
                )
                count += 1
            except (json.JSONDecodeError, sqlite3.Error):
                continue
    except OSError:
        _log.warning("failed to read %s", path)
        return 0

    db.commit()
    _log.info("imported %d task history entries from %s", count, path.name)
    _rename_migrated(path)
    # Rotate files too
    for rot in path.parent.glob("task_history.jsonl.*"):
        if not rot.name.endswith(".migrated"):
            _rename_migrated(rot)
    return 1


def _migrate_proposals(db: SwarmDB, path: Path) -> int:
    """Import proposals.json into the proposals table."""
    if not path.exists():
        return 0
    row = db.fetchone("SELECT COUNT(*) FROM proposals")
    if row and row[0] > 0:
        return 0

    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return 0

    count = 0
    for source in ("proposals", "history"):
        for p in data.get(source, []):
            try:
                status = p.get("status", "pending")
                if source == "history" and status == "pending":
                    status = "expired"
                db.execute(
                    """INSERT OR IGNORE INTO proposals (
                        id, worker_name, task_id, task_title, proposal_type,
                        status, confidence, assessment, message, reasoning,
                        queen_action, prompt_snippet, rule_pattern, is_plan,
                        rejection_reason, created_at, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        p.get("id", ""),
                        p.get("worker_name"),
                        p.get("task_id"),
                        p.get("task_title"),
                        p.get("proposal_type"),
                        status,
                        p.get("confidence"),
                        p.get("assessment"),
                        p.get("message"),
                        p.get("reasoning"),
                        p.get("queen_action"),
                        p.get("prompt_snippet"),
                        p.get("rule_pattern"),
                        1 if p.get("is_plan") else 0,
                        p.get("rejection_reason"),
                        p.get("created_at"),
                        p.get("resolved_at"),
                    ),
                )
                count += 1
            except sqlite3.Error:
                continue
    db.commit()
    if count:
        _log.info("imported %d proposals from %s", count, path.name)
        _rename_migrated(path)
        return 1
    return 0


def _migrate_messages(db: SwarmDB, path: Path) -> int:
    """Import messages from separate messages.db into swarm.db."""
    if not path.exists():
        return 0
    row = db.fetchone("SELECT COUNT(*) FROM messages")
    if row and row[0] > 0:
        return 0

    try:
        src = sqlite3.connect(str(path))
        src.row_factory = sqlite3.Row
        rows = src.execute(
            "SELECT sender, recipient, msg_type, content, created_at, read_at FROM messages"
        ).fetchall()
        src.close()
    except sqlite3.Error:
        _log.warning("failed to read %s", path)
        return 0

    if not rows:
        _rename_migrated(path)
        return 1

    for r in rows:
        try:
            db.execute(
                """INSERT INTO messages (sender, recipient, msg_type, content, created_at, read_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    r["sender"],
                    r["recipient"],
                    r["msg_type"],
                    r["content"],
                    r["created_at"],
                    r["read_at"],
                ),
            )
        except sqlite3.Error:
            continue
    db.commit()
    _log.info("imported %d messages from %s", len(rows), path.name)
    _rename_migrated(path)
    return 1


def _migrate_system_log(db: SwarmDB, path: Path) -> int:
    """Import system_log.db decision_log into buzz_log table."""
    if not path.exists():
        return 0
    row = db.fetchone("SELECT COUNT(*) FROM buzz_log")
    if row and row[0] > 0:
        return 0

    try:
        src = sqlite3.connect(str(path))
        src.row_factory = sqlite3.Row
        rows = src.execute(
            """SELECT timestamp, action, worker_name, detail, category,
                      is_notification, metadata
               FROM decision_log ORDER BY timestamp"""
        ).fetchall()
        src.close()
    except sqlite3.Error:
        _log.warning("failed to read %s", path)
        return 0

    if not rows:
        _rename_migrated(path)
        return 1

    for r in rows:
        try:
            db.execute(
                """INSERT INTO buzz_log (timestamp, action, worker_name, detail,
                                        category, is_notification, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["timestamp"],
                    r["action"],
                    r["worker_name"],
                    r["detail"],
                    r["category"],
                    r["is_notification"],
                    r["metadata"],
                ),
            )
        except sqlite3.Error:
            continue
    db.commit()
    _log.info("imported %d buzz log entries from %s", len(rows), path.name)
    _rename_migrated(path)
    # Clean up WAL/SHM files
    for suffix in ("-wal", "-shm"):
        wal = Path(str(path) + suffix)
        if wal.exists():
            _rename_migrated(wal)
    return 1


def _migrate_pipelines(db: SwarmDB, path: Path) -> int:
    """Import pipelines.json."""
    if not path.exists():
        return 0
    row = db.fetchone("SELECT COUNT(*) FROM pipelines")
    if row and row[0] > 0:
        return 0

    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return 0

    if not isinstance(data, list) or not data:
        _rename_migrated(path)
        return 1

    for p in data:
        try:
            db.execute(
                """INSERT OR IGNORE INTO pipelines
                   (id, name, description, enabled,
                    schedule, config, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    p.get("id", ""),
                    p.get("name", ""),
                    p.get("description", ""),
                    1 if p.get("enabled", True) else 0,
                    p.get("schedule"),
                    json.dumps(p.get("config", {})),
                    p.get("created_at"),
                    p.get("updated_at"),
                ),
            )
            for i, stage in enumerate(p.get("stages", [])):
                db.execute(
                    """INSERT OR IGNORE INTO pipeline_stages
                       (pipeline_id, stage_order, name,
                        action, config)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        p.get("id", ""),
                        i,
                        stage.get("name", ""),
                        stage.get("action", ""),
                        json.dumps(stage.get("config", {})),
                    ),
                )
        except sqlite3.Error:
            continue
    db.commit()
    _log.info("imported pipelines from %s", path.name)
    _rename_migrated(path)
    return 1


def _migrate_secrets(db: SwarmDB, swarm_dir: Path) -> int:
    """Import OAuth tokens and passkeys into secrets table."""
    row = db.fetchone("SELECT COUNT(*) FROM secrets")
    if row and row[0] > 0:
        return 0

    migrated = 0
    secret_files = {
        "graph_tokens": swarm_dir / "graph_tokens.json",
        "jira_tokens": swarm_dir / "jira_tokens.json",
        "passkeys": swarm_dir / "passkeys.json",
    }
    for key, path in secret_files.items():
        if not path.exists():
            continue
        try:
            value = path.read_text().strip()
            json.loads(value)  # validate JSON
            db.execute(
                "INSERT OR REPLACE INTO secrets (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, time.time()),
            )
            db.commit()
            _rename_migrated(path)
            migrated += 1
        except (json.JSONDecodeError, OSError, sqlite3.Error):
            _log.warning("failed to migrate %s", path)
    return migrated


def _migrate_queen_sessions(db: SwarmDB, queen_dir: Path) -> int:
    """Import queen session files."""
    if not queen_dir.is_dir():
        return 0
    row = db.fetchone("SELECT COUNT(*) FROM queen_sessions")
    if row and row[0] > 0:
        return 0

    count = 0
    for f in queen_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            session_id = data.get("session_id", "")
            if session_id:
                name = f.stem
                db.execute(
                    "INSERT OR IGNORE INTO queen_sessions "
                    "(name, session_id, created_at) "
                    "VALUES (?, ?, ?)",
                    (name, session_id, time.time()),
                )
                count += 1
                _rename_migrated(f)
        except (json.JSONDecodeError, OSError, sqlite3.Error):
            continue
    if count:
        db.commit()
        _log.info("imported %d queen sessions", count)
    return 1 if count else 0


def _migrate_config(db: SwarmDB, swarm_dir: Path | None = None) -> int:
    """Import config.yaml into normalized DB tables — **only** on a
    fully-empty DB.

    Historical bug: the guard used to check only the ``workers`` table
    count.  If a user had rules / groups / config rows in the DB but
    ``workers`` happened to be empty (e.g. after the older
    save_config_to_db data-loss bug wiped it, or after manual SQL),
    this migration would re-run against a YAML that lacked the DB-only
    rules, and save_config_to_db(sync_approval_rules=True) would then
    DESTROY those rules.  Non-destructive rule: migration only runs
    when the DB has **no user data at all** in any of the tables
    save_config_to_db would touch — workers, groups, config
    (minus ``update_cache``), or approval_rules.
    """
    config_dir = (
        swarm_dir.parent / ".config" / "swarm" if swarm_dir else Path.home() / ".config" / "swarm"
    )
    config_paths = [config_dir / "config.yaml"]
    for path in config_paths:
        if not path.exists():
            continue

        # Skip if the DB already contains ANY user data — we must not
        # overwrite it from YAML.
        counts = db.fetchone(
            "SELECT "
            "  (SELECT COUNT(*) FROM workers) AS w,"
            "  (SELECT COUNT(*) FROM groups) AS g,"
            "  (SELECT COUNT(*) FROM config WHERE key != 'update_cache') AS c,"
            "  (SELECT COUNT(*) FROM approval_rules) AS r"
        )
        if counts and (counts["w"] or counts["g"] or counts["c"] or counts["r"]):
            _log.info(
                "skipping YAML→DB migration: DB already has data "
                "(workers=%d groups=%d config=%d rules=%d)",
                counts["w"],
                counts["g"],
                counts["c"],
                counts["r"],
            )
            return 0

        try:
            from swarm.config.loader import load_config
            from swarm.db.config_store import save_config_to_db

            config = load_config(str(path))
            # One-time YAML→DB migration: the DB is empty, so this is the
            # only moment we want the full state (including approval
            # rules) to be transferred wholesale.  Pass
            # sync_approval_rules=True explicitly — save_config_to_db
            # defaults to False so routine saves can't wipe rules.
            save_config_to_db(db, config, sync_approval_rules=True)
            # Keep config.yaml in place — systemd unit references it by path.
            # DB is now the source of truth; YAML is a read-only artifact.
            _log.info("migrated config.yaml to swarm.db (YAML kept as reference)")
            return 1
        except Exception:
            _log.warning("failed to migrate config from %s", path, exc_info=True)
            return 0
    return 0
