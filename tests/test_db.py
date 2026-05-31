"""Tests for the unified SQLite storage module."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from swarm.db.core import SwarmDB
from swarm.db.migrate import auto_migrate
from swarm.db.schema import CURRENT_VERSION


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def db(db_path: Path) -> SwarmDB:
    return SwarmDB(db_path)


class TestSwarmDB:
    def test_creates_db_file(self, db: SwarmDB, db_path: Path) -> None:
        assert db_path.exists()
        assert db.connected

    def test_schema_version(self, db: SwarmDB) -> None:
        row = db.fetchone("SELECT MAX(version) FROM schema_version")
        assert row is not None
        assert row[0] == CURRENT_VERSION

    def test_tables_exist(self, db: SwarmDB) -> None:
        tables = db.fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        names = {r[0] for r in tables}
        expected = {
            "schema_version",
            "config",
            "workers",
            "groups",
            "group_workers",
            "config_overrides",
            "approval_rules",
            "tasks",
            "task_history",
            "proposals",
            "buzz_log",
            "messages",
            "pipelines",
            "pipeline_stages",
            "secrets",
            "queen_sessions",
            "queen_threads",
            "queen_messages",
            "queen_learnings",
        }
        assert expected.issubset(names)

    def test_insert_and_fetch(self, db: SwarmDB) -> None:
        db.insert("config", {"key": "port", "value": "9090", "updated_at": time.time()})
        row = db.fetchone("SELECT value FROM config WHERE key = ?", ("port",))
        assert row is not None
        assert row[0] == "9090"

    def test_update(self, db: SwarmDB) -> None:
        db.insert("config", {"key": "port", "value": "9090", "updated_at": time.time()})
        affected = db.update("config", {"value": "8080"}, "key = ?", ("port",))
        assert affected == 1
        row = db.fetchone("SELECT value FROM config WHERE key = ?", ("port",))
        assert row is not None
        assert row[0] == "8080"

    def test_delete(self, db: SwarmDB) -> None:
        db.insert("config", {"key": "test", "value": "x", "updated_at": time.time()})
        affected = db.delete("config", "key = ?", ("test",))
        assert affected == 1
        row = db.fetchone("SELECT value FROM config WHERE key = ?", ("test",))
        assert row is None

    def test_insert_task(self, db: SwarmDB) -> None:
        db.insert(
            "tasks",
            {
                "id": "abc123",
                "number": 1,
                "title": "Test task",
                "status": "unassigned",
                "priority": "normal",
                "task_type": "chore",
                "created_at": time.time(),
            },
        )
        row = db.fetchone("SELECT title FROM tasks WHERE id = ?", ("abc123",))
        assert row is not None
        assert row[0] == "Test task"

    def test_foreign_key_task_history(self, db: SwarmDB) -> None:
        db.insert(
            "tasks",
            {"id": "t1", "number": 1, "title": "T1", "created_at": time.time()},
        )
        db.insert(
            "task_history",
            {"task_id": "t1", "action": "CREATED", "actor": "user", "created_at": time.time()},
        )
        rows = db.fetchall("SELECT action FROM task_history WHERE task_id = ?", ("t1",))
        assert len(rows) == 1

    def test_stats(self, db: SwarmDB) -> None:
        stats = db.stats()
        assert "tasks" in stats
        assert "config" in stats
        assert all(v >= 0 for v in stats.values())

    def test_db_size(self, db: SwarmDB, db_path: Path) -> None:
        size = db.db_size()
        assert size > 0

    def test_integrity_check(self, db: SwarmDB) -> None:
        assert db.integrity_check()

    def test_backup(self, db: SwarmDB, tmp_path: Path) -> None:
        db.insert("config", {"key": "test", "value": "v", "updated_at": time.time()})
        bak_path = tmp_path / "backup.db"
        result = db.backup(bak_path)
        assert result == bak_path
        assert bak_path.exists()
        # Verify backup has the data
        bak = sqlite3.connect(str(bak_path))
        row = bak.execute("SELECT value FROM config WHERE key = 'test'").fetchone()
        bak.close()
        assert row is not None
        assert row[0] == "v"

    def test_checkpoint(self, db: SwarmDB) -> None:
        db.checkpoint()  # Should not raise

    def test_close_and_reopen(self, db_path: Path) -> None:
        db1 = SwarmDB(db_path)
        db1.insert("config", {"key": "x", "value": "1", "updated_at": time.time()})
        db1.close()
        assert not db1.connected
        db2 = SwarmDB(db_path)
        row = db2.fetchone("SELECT value FROM config WHERE key = ?", ("x",))
        assert row is not None
        assert row[0] == "1"
        db2.close()

    def test_wal_mode(self, db: SwarmDB) -> None:
        row = db.fetchone("PRAGMA journal_mode")
        assert row is not None
        assert row[0] == "wal"

    def test_permissions(self, db_path: Path) -> None:
        import os
        import stat

        db = SwarmDB(db_path)
        mode = os.stat(db_path).st_mode
        assert not (mode & stat.S_IROTH)
        assert not (mode & stat.S_IWOTH)
        db.close()


class TestMigration:
    def test_migrate_tasks(self, db: SwarmDB, tmp_path: Path) -> None:
        tasks_file = tmp_path / "tasks.json"
        tasks_file.write_text(
            json.dumps(
                [
                    {
                        "id": "task1",
                        "number": 42,
                        "title": "Fix bug",
                        # Legacy v8 spelling — _normalize_status maps to "done"
                        "status": "completed",
                        "priority": "high",
                        "task_type": "bug",
                        "resolution": "Fixed it",
                        "created_at": 1000.0,
                    }
                ]
            )
        )
        from swarm.db.migrate import _migrate_tasks

        result = _migrate_tasks(db, tasks_file)
        assert result == 1
        row = db.fetchone("SELECT title, status FROM tasks WHERE id = ?", ("task1",))
        assert row is not None
        assert row[0] == "Fix bug"
        assert row[1] == "done"
        assert tasks_file.with_suffix(".json.migrated").exists()

    def test_migrate_messages(self, db: SwarmDB, tmp_path: Path) -> None:
        msg_db_path = tmp_path / "messages.db"
        conn = sqlite3.connect(str(msg_db_path))
        conn.execute(
            """CREATE TABLE messages (
                id INTEGER PRIMARY KEY, sender TEXT, recipient TEXT,
                msg_type TEXT, content TEXT, created_at REAL, read_at REAL
            )"""
        )
        conn.execute(
            "INSERT INTO messages VALUES (1, 'alice', 'bob', 'warning', 'hi', 100.0, NULL)"
        )
        conn.commit()
        conn.close()

        from swarm.db.migrate import _migrate_messages

        result = _migrate_messages(db, msg_db_path)
        assert result == 1
        row = db.fetchone("SELECT sender, content FROM messages WHERE recipient = ?", ("bob",))
        assert row is not None
        assert row[0] == "alice"
        assert row[1] == "hi"

    def test_migrate_secrets(self, db: SwarmDB, tmp_path: Path) -> None:
        tokens = tmp_path / "graph_tokens.json"
        tokens.write_text('{"access_token": "secret123"}')

        from swarm.db.migrate import _migrate_secrets

        result = _migrate_secrets(db, tmp_path)
        assert result == 1
        row = db.fetchone("SELECT value FROM secrets WHERE key = ?", ("graph_tokens",))
        assert row is not None
        data = json.loads(row[0])
        assert data["access_token"] == "secret123"

    def test_migrate_skips_if_data_exists(self, db: SwarmDB, tmp_path: Path) -> None:
        db.insert(
            "tasks",
            {"id": "existing", "number": 1, "title": "Already here", "created_at": time.time()},
        )
        tasks_file = tmp_path / "tasks.json"
        tasks_file.write_text(json.dumps([{"id": "new", "number": 2, "title": "New"}]))

        from swarm.db.migrate import _migrate_tasks

        result = _migrate_tasks(db, tasks_file)
        assert result == 0  # Skipped — data already exists
        assert not tasks_file.with_suffix(".json.migrated").exists()

    def test_auto_migrate_no_files(self, db: SwarmDB, tmp_path: Path) -> None:
        count = auto_migrate(db, tmp_path)
        assert count == 0

    def test_migrate_config_skips_when_rules_exist(self, db: SwarmDB, tmp_path: Path) -> None:
        """Non-destructive migration: if the DB already has approval_rules
        (even with no workers), _migrate_config must NOT re-import from
        YAML — doing so would call save_config_to_db and wipe the rules.

        Regression for the reported "my approval rules keep disappearing"
        bug: a user's DB had rules but workers had been cleared, and the
        old workers-only guard let migration re-run and destroy them.
        """
        # Seed rules directly in the DB (simulates user-added rules
        # from the dashboard, with no workers).
        db.execute(
            "INSERT INTO approval_rules "
            "(owner_type, owner_id, pattern, action, sort_order) "
            "VALUES ('global', NULL, 'KeepMe.*', 'approve', 0)"
        )
        db.commit()

        # Create a YAML that has NO approval rules (the dangerous case).
        config_dir = tmp_path / ".config" / "swarm"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text("session_name: test\nworkers: []\ngroups: []\n")

        from swarm.db.migrate import _migrate_config

        result = _migrate_config(db, tmp_path / ".swarm")
        assert result == 0, "must skip migration when DB already has user data"

        # The rule must survive intact.
        rows = db.fetchall("SELECT pattern FROM approval_rules WHERE owner_type = 'global'")
        assert [r["pattern"] for r in rows] == ["KeepMe.*"]

    def test_migrate_config_runs_on_truly_empty_db(self, db: SwarmDB, tmp_path: Path) -> None:
        """Opt-in path: migration proceeds normally on a blank DB."""
        config_dir = tmp_path / ".config" / "swarm"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text(
            "session_name: test\n"
            "workers:\n"
            "  - name: api\n"
            "    path: /tmp/api\n"
            "groups:\n"
            "  - name: all\n"
            "    workers: [api]\n"
        )

        from swarm.db.migrate import _migrate_config

        result = _migrate_config(db, tmp_path / ".swarm")
        assert result == 1
        row = db.fetchone("SELECT COUNT(*) FROM workers")
        assert row is not None and row[0] == 1


class TestSqliteTaskStore:
    def test_save_and_load(self, db: SwarmDB) -> None:
        from swarm.db.task_store import SqliteTaskStore
        from swarm.tasks.task import SwarmTask

        store = SqliteTaskStore(db)
        task = SwarmTask(
            id="t1",
            title="Test",
            number=1,
            created_at=time.time(),
        )
        store.save_one(task)
        loaded = store.load()
        assert "t1" in loaded
        assert loaded["t1"].title == "Test"

    def test_save_all(self, db: SwarmDB) -> None:
        from swarm.db.task_store import SqliteTaskStore
        from swarm.tasks.task import SwarmTask

        store = SqliteTaskStore(db)
        tasks = {
            "a": SwarmTask(id="a", title="A", number=1, created_at=time.time()),
            "b": SwarmTask(id="b", title="B", number=2, created_at=time.time()),
        }
        store.save(tasks)
        loaded = store.load()
        assert len(loaded) == 2

    def test_delete_one(self, db: SwarmDB) -> None:
        from swarm.db.task_store import SqliteTaskStore
        from swarm.tasks.task import SwarmTask

        store = SqliteTaskStore(db)
        store.save_one(SwarmTask(id="d1", title="D", number=1, created_at=time.time()))
        assert store.delete_one("d1")
        assert "d1" not in store.load()

    def test_upsert(self, db: SwarmDB) -> None:
        from swarm.db.task_store import SqliteTaskStore
        from swarm.tasks.task import SwarmTask, TaskStatus

        store = SqliteTaskStore(db)
        task = SwarmTask(id="u1", title="Original", number=1, created_at=time.time())
        store.save_one(task)
        task.title = "Updated"
        task.status = TaskStatus.DONE
        store.save_one(task)
        loaded = store.load()
        assert loaded["u1"].title == "Updated"
        assert loaded["u1"].status == TaskStatus.DONE


class TestSqliteTaskHistory:
    def test_append_and_get(self, db: SwarmDB) -> None:
        from swarm.db.task_history import SqliteTaskHistory
        from swarm.tasks.history import TaskAction

        hist = SqliteTaskHistory(db)
        # Need a task for FK
        db.insert("tasks", {"id": "th1", "number": 1, "title": "T", "created_at": time.time()})
        hist.append("th1", TaskAction.CREATED, actor="user")
        hist.append("th1", TaskAction.ASSIGNED, actor="queen", detail="platform")
        events = hist.get_events("th1")
        assert len(events) == 2
        assert events[0].action == TaskAction.CREATED
        assert events[1].action == TaskAction.ASSIGNED

    def test_prune(self, db: SwarmDB) -> None:
        from swarm.db.task_history import SqliteTaskHistory

        hist = SqliteTaskHistory(db)
        db.insert("tasks", {"id": "tp1", "number": 1, "title": "T", "created_at": time.time()})
        # Insert an old event directly
        db.insert(
            "task_history",
            {
                "task_id": "tp1",
                "action": "CREATED",
                "actor": "user",
                "created_at": 1000.0,
            },
        )
        deleted = hist.prune(max_age_days=1)
        assert deleted == 1


class TestSqliteProposalStore:
    def test_add_and_get(self, db: SwarmDB) -> None:
        from swarm.db.proposal_store import SqliteProposalStore
        from swarm.tasks.proposal import AssignmentProposal

        store = SqliteProposalStore(db)
        p = AssignmentProposal(worker_name="platform", task_title="Test")
        store.add(p)
        got = store.get(p.id)
        assert got is not None
        assert got.worker_name == "platform"

    def test_pending(self, db: SwarmDB) -> None:
        from swarm.db.proposal_store import SqliteProposalStore
        from swarm.tasks.proposal import AssignmentProposal

        store = SqliteProposalStore(db)
        p1 = AssignmentProposal(worker_name="w1", task_title="T1")
        p2 = AssignmentProposal(worker_name="w2", task_title="T2")
        store.add(p1)
        store.add(p2)
        assert len(store.pending) == 2

    def test_remove(self, db: SwarmDB) -> None:
        from swarm.db.proposal_store import SqliteProposalStore
        from swarm.tasks.proposal import AssignmentProposal

        store = SqliteProposalStore(db)
        p = AssignmentProposal(worker_name="w1", task_title="T")
        store.add(p)
        assert store.remove(p.id)
        assert store.get(p.id) is None

    def test_has_pending_escalation(self, db: SwarmDB) -> None:
        from swarm.db.proposal_store import SqliteProposalStore
        from swarm.tasks.proposal import AssignmentProposal, ProposalType

        store = SqliteProposalStore(db)
        p = AssignmentProposal(
            worker_name="w1",
            proposal_type=ProposalType.ESCALATION,
        )
        store.add(p)
        assert store.has_pending_escalation("w1")
        assert not store.has_pending_escalation("w2")

    def test_expire_old(self, db: SwarmDB) -> None:
        from swarm.db.proposal_store import SqliteProposalStore
        from swarm.tasks.proposal import AssignmentProposal

        store = SqliteProposalStore(db)
        p = AssignmentProposal(worker_name="w1", task_title="Old")
        p.created_at = time.time() - 7200  # 2 hours ago
        store.add(p)
        expired = store.expire_old(max_age=3600)
        assert expired >= 1
        assert len(store.pending) == 0

    def test_add_to_history(self, db: SwarmDB) -> None:
        from swarm.db.proposal_store import SqliteProposalStore
        from swarm.tasks.proposal import AssignmentProposal, ProposalStatus

        store = SqliteProposalStore(db)
        p = AssignmentProposal(worker_name="w1", task_title="Done")
        p.status = ProposalStatus.APPROVED
        store.add_to_history(p)
        assert len(store.history) == 1
        assert store.history[0].status == ProposalStatus.APPROVED


class TestQueenChatStore:
    """Interactive Queen thread / message / learning store."""

    def test_create_and_get_thread(self, db: SwarmDB) -> None:
        from swarm.db.queen_chat_store import QueenChatStore

        store = QueenChatStore(db)
        t = store.create_thread(title="Hub is stuck", kind="oversight", worker_name="hub")
        fetched = store.get_thread(t.id)
        assert fetched is not None
        assert fetched.title == "Hub is stuck"
        assert fetched.status == "active"
        assert fetched.worker_name == "hub"

    def test_list_threads_filters(self, db: SwarmDB) -> None:
        from swarm.db.queen_chat_store import QueenChatStore

        store = QueenChatStore(db)
        store.create_thread(title="T1", kind="operator")
        store.create_thread(title="T2", kind="oversight", worker_name="hub")
        store.create_thread(title="T3", kind="oversight", worker_name="platform")

        all_threads = store.list_threads()
        assert len(all_threads) == 3
        oversight = store.list_threads(kind="oversight")
        assert {t.title for t in oversight} == {"T2", "T3"}
        hub_only = store.list_threads(worker_name="hub")
        assert len(hub_only) == 1 and hub_only[0].title == "T2"

    def test_message_append_updates_thread(self, db: SwarmDB) -> None:
        from swarm.db.queen_chat_store import QueenChatStore

        store = QueenChatStore(db)
        t = store.create_thread(title="Chat")
        original_updated = t.updated_at
        # Sleep-free: explicit clock skew via a forced update; just add and re-fetch.
        msg = store.add_message(t.id, role="operator", content="hello")
        assert msg.role == "operator"
        assert msg.content == "hello"

        fetched = store.get_thread(t.id)
        assert fetched is not None
        assert fetched.updated_at >= original_updated

        msgs = store.list_messages(t.id)
        assert len(msgs) == 1
        assert msgs[0].content == "hello"

    def test_message_rejects_invalid_role(self, db: SwarmDB) -> None:
        from swarm.db.queen_chat_store import QueenChatStore

        store = QueenChatStore(db)
        t = store.create_thread(title="x")
        with pytest.raises(ValueError):
            store.add_message(t.id, role="bogus", content="x")

    def test_resolve_thread(self, db: SwarmDB) -> None:
        from swarm.db.queen_chat_store import QueenChatStore

        store = QueenChatStore(db)
        t = store.create_thread(title="Resolvable")
        ok = store.resolve_thread(t.id, resolved_by="operator", reason="approved")
        assert ok is True

        fetched = store.get_thread(t.id)
        assert fetched is not None
        assert fetched.status == "resolved"
        assert fetched.resolved_by == "operator"
        assert fetched.resolution_reason == "approved"

        # Second resolve is a no-op
        again = store.resolve_thread(t.id, resolved_by="operator")
        assert again is False

    def test_resolve_rejects_invalid_resolver(self, db: SwarmDB) -> None:
        from swarm.db.queen_chat_store import QueenChatStore

        store = QueenChatStore(db)
        t = store.create_thread(title="x")
        with pytest.raises(ValueError):
            store.resolve_thread(t.id, resolved_by="bogus")

    def test_learnings_crud_and_query(self, db: SwarmDB) -> None:
        from swarm.db.queen_chat_store import QueenChatStore

        store = QueenChatStore(db)
        store.add_learning(
            context="wrong worker blamed",
            correction="it was hub, not platform",
            applied_to="oversight",
        )
        store.add_learning(
            context="auth rewrite decision",
            correction="never touch middleware",
            applied_to="proposal",
        )
        all_l = store.query_learnings()
        assert len(all_l) == 2
        filtered = store.query_learnings(applied_to="oversight")
        assert len(filtered) == 1
        assert filtered[0].applied_to == "oversight"
        matched = store.query_learnings(search="auth")
        assert len(matched) == 1

    def test_widgets_roundtrip(self, db: SwarmDB) -> None:
        from swarm.db.queen_chat_store import QueenChatStore

        store = QueenChatStore(db)
        t = store.create_thread(title="x")
        widgets = [{"type": "approve_buttons", "thread_id": t.id}]
        store.add_message(t.id, role="queen", content="Need approval", widgets=widgets)
        msgs = store.list_messages(t.id)
        assert msgs[0].widgets == widgets

    def test_proposals_has_thread_id_column(self, db: SwarmDB) -> None:
        """v6 schema migration added a thread_id column to proposals."""
        cols = db.fetchall("PRAGMA table_info(proposals)")
        names = {c["name"] for c in cols}
        assert "thread_id" in names


class TestSchemaConsistency:
    """Fresh-vs-migrated divergence guard.

    Every column an ``ALTER TABLE ... ADD COLUMN`` migration introduces, and
    every ``CREATE INDEX`` a migration creates, MUST also exist in the
    fresh-create schema (SCHEMA_V1). If not, a fresh install and an upgraded
    install end up with different schemas — the single most dangerous DB bug
    class. This test introspects the migration source, so any future migration
    is covered automatically (it fails if you add a migration column/index but
    forget to mirror it into schema.py).
    """

    @staticmethod
    def _migration_source() -> str:
        import inspect

        from swarm.db import core, migrate

        return inspect.getsource(core) + inspect.getsource(migrate)

    def test_migration_added_columns_present_in_fresh_schema(self, db: SwarmDB) -> None:
        import re

        adds = re.findall(
            r"ALTER TABLE\s+(\w+)\s+ADD COLUMN\s+(\w+)",
            self._migration_source(),
            re.IGNORECASE,
        )
        assert adds, "expected to find ADD COLUMN migrations to check"
        missing = []
        for table, col in adds:
            cols = {r["name"] for r in db.fetchall(f"PRAGMA table_info({table})")}
            if col not in cols:
                missing.append(f"{table}.{col}")
        assert not missing, f"migration columns absent from fresh schema (divergence): {missing}"

    def test_migration_created_indexes_present_in_fresh_schema(self, db: SwarmDB) -> None:
        import re

        names = re.findall(
            r"CREATE INDEX IF NOT EXISTS\s+(\w+)",
            self._migration_source(),
            re.IGNORECASE,
        )
        assert names, "expected to find CREATE INDEX migrations to check"
        fresh = {
            r["name"] for r in db.fetchall("SELECT name FROM sqlite_master WHERE type='index'")
        }
        missing = [n for n in names if n not in fresh]
        assert not missing, f"migration indexes absent from fresh schema (divergence): {missing}"
