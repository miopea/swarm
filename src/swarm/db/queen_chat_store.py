"""SQLite-backed store for Queen chat threads, messages, and learnings.

Interactive Queen central-command surface.  Threads are UI grouping
metadata over the single persistent Queen Claude session — the
conversation stream is unified at the LLM layer; threads partition it
for the operator's UI.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from swarm.db.base_store import BaseStore
from swarm.logging import get_logger

if TYPE_CHECKING:
    from swarm.db.core import SwarmDB

_log = get_logger("db.queen_chat")

THREAD_KINDS = (
    "operator",
    "oversight",
    "proposal",
    "escalation",
    "anomaly",
    "worker-message",
    "queen-escalation",
)
THREAD_STATUSES = ("active", "resolved", "archived")
MESSAGE_ROLES = ("queen", "operator", "system")
RESOLVER_KINDS = ("operator", "queen")

RETENTION_DAYS = 30


@dataclass
class QueenThread:
    id: str
    title: str
    kind: str
    status: str
    worker_name: str | None
    task_id: str | None
    created_at: float
    updated_at: float
    resolved_at: float | None = None
    resolved_by: str | None = None
    resolution_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "status": self.status,
            "worker_name": self.worker_name,
            "task_id": self.task_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "resolved_at": self.resolved_at,
            "resolved_by": self.resolved_by,
            "resolution_reason": self.resolution_reason,
        }


@dataclass
class QueenMessage:
    id: int
    thread_id: str
    role: str
    content: str
    widgets: list[dict[str, Any]]
    ts: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "role": self.role,
            "content": self.content,
            "widgets": self.widgets,
            "ts": self.ts,
        }


@dataclass
class QueenLearning:
    id: int
    context: str
    correction: str
    applied_to: str
    thread_id: str | None
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "context": self.context,
            "correction": self.correction,
            "applied_to": self.applied_to,
            "thread_id": self.thread_id,
            "created_at": self.created_at,
        }


class QueenChatStore(BaseStore):
    """Thread/message/learning store for the interactive Queen.

    Thread-safe via RLock so the daemon (async loops) and MCP handlers
    (separate threads) can read and write without coordination burden
    at the call site.
    """

    def __init__(self, db: SwarmDB) -> None:
        self._db = db
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Threads
    # ------------------------------------------------------------------

    def create_thread(
        self,
        *,
        title: str,
        kind: str = "operator",
        worker_name: str | None = None,
        task_id: str | None = None,
    ) -> QueenThread:
        if kind not in THREAD_KINDS:
            raise ValueError(f"invalid thread kind: {kind!r}")
        now = time.time()
        thread = QueenThread(
            id=uuid.uuid4().hex[:16],
            title=title,
            kind=kind,
            status="active",
            worker_name=worker_name,
            task_id=task_id,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._db.insert(
                "queen_threads",
                {
                    "id": thread.id,
                    "title": thread.title,
                    "kind": thread.kind,
                    "status": thread.status,
                    "worker_name": thread.worker_name,
                    "task_id": thread.task_id,
                    "created_at": thread.created_at,
                    "updated_at": thread.updated_at,
                },
            )
        return thread

    def get_thread(self, thread_id: str) -> QueenThread | None:
        with self._lock:
            row = self._db.fetchone("SELECT * FROM queen_threads WHERE id = ?", (thread_id,))
            return _row_to_thread(row) if row else None

    def list_threads(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        worker_name: str | None = None,
        search: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[QueenThread]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if worker_name:
            clauses.append("worker_name = ?")
            params.append(worker_name)
        if search:
            # EXISTS (not JOIN+DISTINCT) so a thread matching multiple messages
            # still returns exactly once and the row shape stays clean.
            clauses.append(
                "(title LIKE ? OR EXISTS (SELECT 1 FROM queen_messages m "
                "WHERE m.thread_id = queen_threads.id AND m.content LIKE ?))"
            )
            needle = f"%{search}%"
            params.extend([needle, needle])
        if since is not None:
            clauses.append("updated_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("updated_at <= ?")
            params.append(until)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM queen_threads {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            rows = self._db.fetchall(sql, tuple(params))
            return [_row_to_thread(r) for r in rows]

    def message_counts(self, thread_ids: list[str]) -> dict[str, int]:
        """Batch message count per thread — one query for a whole page.

        Threads with no messages are absent from the result (callers
        default to 0). Keeps ``list_threads`` returning plain QueenThread
        objects while the route assembles ``to_dict() | {message_count}``.
        """
        if not thread_ids:
            return {}
        placeholders = ",".join("?" for _ in thread_ids)
        sql = (
            f"SELECT thread_id, COUNT(*) AS n FROM queen_messages "
            f"WHERE thread_id IN ({placeholders}) GROUP BY thread_id"
        )
        with self._lock:
            rows = self._db.fetchall(sql, tuple(thread_ids))
        return {r["thread_id"]: r["n"] for r in rows}

    def resolve_thread(
        self,
        thread_id: str,
        *,
        resolved_by: str,
        reason: str = "",
    ) -> bool:
        if resolved_by not in RESOLVER_KINDS:
            raise ValueError(f"invalid resolver: {resolved_by!r}")
        now = time.time()
        with self._lock:
            affected = self._db.update(
                "queen_threads",
                {
                    "status": "resolved",
                    "resolved_at": now,
                    "resolved_by": resolved_by,
                    "resolution_reason": reason,
                    "updated_at": now,
                },
                "id = ? AND status != 'resolved'",
                (thread_id,),
            )
        return affected > 0

    def touch_thread(self, thread_id: str) -> None:
        with self._lock:
            self._db.update(
                "queen_threads",
                {"updated_at": time.time()},
                "id = ?",
                (thread_id,),
            )

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def add_message(
        self,
        thread_id: str,
        *,
        role: str,
        content: str,
        widgets: list[dict[str, Any]] | None = None,
    ) -> QueenMessage:
        if role not in MESSAGE_ROLES:
            raise ValueError(f"invalid message role: {role!r}")
        now = time.time()
        serialized = json.dumps(widgets or [])
        with self._lock:
            msg_id = self._db.insert(
                "queen_messages",
                {
                    "thread_id": thread_id,
                    "role": role,
                    "content": content,
                    "widgets": serialized,
                    "ts": now,
                },
            )
            self._db.update(
                "queen_threads",
                {"updated_at": now},
                "id = ?",
                (thread_id,),
            )
        return QueenMessage(
            id=msg_id,
            thread_id=thread_id,
            role=role,
            content=content,
            widgets=widgets or [],
            ts=now,
        )

    def list_messages(self, thread_id: str, *, limit: int = 500) -> list[QueenMessage]:
        with self._lock:
            rows = self._db.fetchall(
                "SELECT * FROM queen_messages WHERE thread_id = ? ORDER BY ts LIMIT ?",
                (thread_id, limit),
            )
            return [_row_to_message(r, self) for r in rows]

    def latest_message(self, thread_id: str) -> QueenMessage | None:
        """Return the most recent message in a thread, or None.

        Cheaper than ``list_messages(...)[-1]`` for callers (e.g. the
        attention queue) that only need the latest line — fetches one row
        instead of the whole thread.
        """
        with self._lock:
            rows = self._db.fetchall(
                "SELECT * FROM queen_messages WHERE thread_id = ? ORDER BY ts DESC LIMIT 1",
                (thread_id,),
            )
            return _row_to_message(rows[0], self) if rows else None

    # ------------------------------------------------------------------
    # Learnings
    # ------------------------------------------------------------------

    def add_learning(
        self,
        *,
        context: str,
        correction: str,
        applied_to: str = "",
        thread_id: str | None = None,
    ) -> QueenLearning:
        now = time.time()
        with self._lock:
            learning_id = self._db.insert(
                "queen_learnings",
                {
                    "context": context,
                    "correction": correction,
                    "applied_to": applied_to,
                    "thread_id": thread_id,
                    "created_at": now,
                },
            )
        return QueenLearning(
            id=learning_id,
            context=context,
            correction=correction,
            applied_to=applied_to,
            thread_id=thread_id,
            created_at=now,
        )

    def query_learnings(
        self,
        *,
        applied_to: str | None = None,
        search: str | None = None,
        limit: int = 50,
    ) -> list[QueenLearning]:
        clauses: list[str] = []
        params: list[Any] = []
        if applied_to:
            clauses.append("applied_to = ?")
            params.append(applied_to)
        if search:
            clauses.append("(context LIKE ? OR correction LIKE ?)")
            needle = f"%{search}%"
            params.extend([needle, needle])
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM queen_learnings {where} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._db.fetchall(sql, tuple(params))
            return [_row_to_learning(r) for r in rows]

    def delete_learning(self, learning_id: int) -> bool:
        """Delete a learning by id — operator cleanup of stale corrections."""
        with self._lock:
            removed = self._db.delete("queen_learnings", "id = ?", (learning_id,))
        return removed > 0

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def purge_old(self, retention_days: int = RETENTION_DAYS) -> int:
        """Delete threads (and cascaded messages) older than retention_days.

        Learnings are not purged — they're small and high-value.
        """
        cutoff = time.time() - (retention_days * 86400)
        with self._lock:
            removed = self._db.delete(
                "queen_threads",
                "status = 'resolved' AND (resolved_at IS NULL OR resolved_at < ?)",
                (cutoff,),
            )
        if removed:
            _log.info("purged %d resolved queen_threads older than %dd", removed, retention_days)
        return removed


# ----------------------------------------------------------------------
# Row → dataclass conversion
# ----------------------------------------------------------------------


def _row_to_thread(row: Any) -> QueenThread:
    return QueenThread(
        id=row["id"],
        title=row["title"] or "",
        kind=row["kind"],
        status=row["status"],
        worker_name=row["worker_name"],
        task_id=row["task_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        resolved_at=row["resolved_at"],
        resolved_by=row["resolved_by"],
        resolution_reason=row["resolution_reason"],
    )


def _row_to_message(row: Any, store: QueenChatStore) -> QueenMessage:
    widgets = store._parse_json_field(row["widgets"], [])
    return QueenMessage(
        id=row["id"],
        thread_id=row["thread_id"],
        role=row["role"],
        content=row["content"],
        widgets=widgets,
        ts=row["ts"],
    )


def _row_to_learning(row: Any) -> QueenLearning:
    return QueenLearning(
        id=row["id"],
        context=row["context"],
        correction=row["correction"],
        applied_to=row["applied_to"] or "",
        thread_id=row["thread_id"],
        created_at=row["created_at"],
    )
