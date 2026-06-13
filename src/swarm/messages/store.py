"""SQLite-backed message store for inter-worker communication."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swarm.logging import get_logger

if TYPE_CHECKING:
    from swarm.db.core import SwarmDB

_log = get_logger("messages.store")

_DEFAULT_DB_PATH = Path.home() / ".swarm" / "messages.db"
_DEDUP_WINDOW = 60.0  # seconds — same (sender, recipient, type) within window is merged

_VALID_MSG_TYPES = frozenset({"finding", "warning", "dependency", "status", "operator", "note"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sender      TEXT    NOT NULL,
    recipient   TEXT    NOT NULL,
    msg_type    TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    created_at  REAL    NOT NULL,
    read_at     REAL
);
-- Keep these indexes in sync with the canonical `messages` table in
-- db/schema.py. This standalone DDL only runs for the non-shared
-- (test/legacy messages.db) path; production shares the SwarmDB connection
-- where db/schema.py owns the table. They must not drift — the dedup index
-- in particular is what send()/broadcast()'s dedup probe relies on.
CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient);
CREATE INDEX IF NOT EXISTS idx_messages_unread ON messages(recipient, read_at);
CREATE INDEX IF NOT EXISTS idx_messages_dedup
  ON messages(sender, recipient, msg_type, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);
"""


@dataclass
class Message:
    """A single inter-worker message."""

    id: int
    sender: str
    recipient: str
    msg_type: str
    content: str
    created_at: float
    read_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "from": self.sender,
            "to": self.recipient,
            "type": self.msg_type,
            "content": self.content,
            "created_at": self.created_at,
            "read_at": self.read_at,
        }


class MessageStore:
    """Thread-safe SQLite store for inter-worker messages."""

    def __init__(
        self,
        db_path: Path | None = None,
        swarm_db: SwarmDB | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._owns_conn = True
        if swarm_db is not None and hasattr(swarm_db, "_conn") and swarm_db._conn:
            # Share the SwarmDB connection (messages table already exists)
            self._conn = swarm_db._conn
            self._owns_conn = False
            self._lock = swarm_db._lock
        else:
            self._db_path = db_path or _DEFAULT_DB_PATH
            self._init_db()

    def _init_db(self) -> None:
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.executescript(_SCHEMA)
        except sqlite3.Error:
            _log.warning("failed to init message store at %s", self._db_path, exc_info=True)
            self._conn = None

    def send(
        self,
        sender: str,
        recipient: str,
        msg_type: str,
        content: str,
    ) -> int | None:
        """Send a message. Returns message ID, or None if deduped/failed.

        Rate limiting: if an identical (sender, recipient, msg_type) exists
        within the last 60 seconds, update its content and timestamp instead
        of inserting a new row.
        """
        if not self._conn:
            return None
        if msg_type not in _VALID_MSG_TYPES:
            msg_type = "finding"

        now = time.time()
        cutoff = now - _DEDUP_WINDOW

        with self._lock:
            try:
                # Check for recent duplicate
                row = self._conn.execute(
                    "SELECT id FROM messages"
                    " WHERE sender = ? AND recipient = ? AND msg_type = ?"
                    " AND created_at > ? AND read_at IS NULL"
                    " ORDER BY created_at DESC LIMIT 1",
                    (sender, recipient, msg_type, cutoff),
                ).fetchone()
                if row:
                    # Merge: update content and timestamp
                    self._conn.execute(
                        "UPDATE messages SET content = ?, created_at = ? WHERE id = ?",
                        (content, now, row[0]),
                    )
                    self._conn.commit()
                    return row[0]
                # Insert new message
                cur = self._conn.execute(
                    "INSERT INTO messages (sender, recipient, msg_type, content, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (sender, recipient, msg_type, content, now),
                )
                self._conn.commit()
                return cur.lastrowid
            except sqlite3.Error:
                _log.warning("failed to send message", exc_info=True)
                return None

    def broadcast(
        self,
        sender: str,
        recipients: list[str],
        msg_type: str,
        content: str,
    ) -> list[int]:
        """Fan-out send: write one row per recipient.

        This is the correct wildcard path.  The older ``send(sender, "*", …)``
        wrote a single shared row with ``recipient="*"`` which made read
        state first-come-first-served — whichever worker read it first
        marked it read and every subsequent worker saw nothing.

        By writing one row per recipient, each worker has its own
        ``read_at`` tracking and the broadcast reaches the full roster.

        ``sender`` is excluded from the fan-out so a worker broadcasting
        doesn't re-receive its own message.  Each row is still subject to
        the 60-second dedup window: if an unread message with the same
        (sender, recipient, msg_type) already exists within the window,
        its content + timestamp are merged in place rather than inserting
        a new row.

        Returns the list of row ids (one per actual delivery).  Empty
        recipient list returns an empty list (not an error).

        Implementation note: replaces the previous per-recipient call to
        :meth:`send` (which ran one dedup SELECT per row) with a single
        batched lookup followed by per-recipient UPDATE/INSERT.  Saves
        N-1 round-trips on N-recipient broadcasts (the common case is
        every worker).
        """
        if not self._conn or not recipients:
            return []
        if msg_type not in _VALID_MSG_TYPES:
            msg_type = "finding"

        # De-dup the input + drop sender/blanks in a single pass while
        # preserving caller order (Python 3.7+ dict ordering).
        targets: list[str] = []
        seen: set[str] = set()
        for r in recipients:
            if not r or r == sender or r in seen:
                continue
            seen.add(r)
            targets.append(r)
        if not targets:
            return []

        now = time.time()
        cutoff = now - _DEDUP_WINDOW

        with self._lock:
            try:
                # One SELECT to find the most-recent unread row per recipient
                # that falls inside the dedup window.  Without v12's
                # idx_messages_dedup this would be a full table scan; with
                # it the planner walks the (sender, recipient, msg_type,
                # created_at) prefix.
                placeholders = ",".join("?" * len(targets))
                rows = self._conn.execute(
                    f"SELECT recipient, MAX(id) FROM messages"
                    f" WHERE sender = ? AND msg_type = ?"
                    f" AND recipient IN ({placeholders})"
                    f" AND created_at > ? AND read_at IS NULL"
                    f" GROUP BY recipient",
                    (sender, msg_type, *targets, cutoff),
                ).fetchall()
                # Positional indexing — the standalone-file MessageStore
                # path uses a raw sqlite3.Connection without row_factory.
                existing: dict[str, int] = {r[0]: r[1] for r in rows}

                ids: list[int] = []
                for recipient in targets:
                    existing_id = existing.get(recipient)
                    if existing_id is not None:
                        # Merge: update content + bump timestamp
                        self._conn.execute(
                            "UPDATE messages SET content = ?, created_at = ? WHERE id = ?",
                            (content, now, existing_id),
                        )
                        ids.append(existing_id)
                    else:
                        cur = self._conn.execute(
                            "INSERT INTO messages "
                            "(sender, recipient, msg_type, content, created_at)"
                            " VALUES (?, ?, ?, ?, ?)",
                            (sender, recipient, msg_type, content, now),
                        )
                        if cur.lastrowid is not None:
                            ids.append(cur.lastrowid)
                self._conn.commit()
                return ids
            except sqlite3.Error:
                _log.warning("failed to broadcast message", exc_info=True)
                return []

    def get_unread(self, recipient: str, limit: int = 20) -> list[Message]:
        """Get unread messages for a worker."""
        if not self._conn:
            return []
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT id, sender, recipient, msg_type, content, created_at, read_at"
                    " FROM messages"
                    " WHERE (recipient = ? OR recipient = '*')"
                    " AND read_at IS NULL"
                    " ORDER BY created_at ASC LIMIT ?",
                    (recipient, limit),
                ).fetchall()
                return [Message(*r) for r in rows]
            except sqlite3.Error:
                _log.warning("failed to get messages", exc_info=True)
                return []

    def mark_read(self, recipient: str, message_ids: list[int] | None = None) -> int:
        """Mark messages as read. Returns count of messages marked."""
        if not self._conn:
            return 0
        now = time.time()
        with self._lock:
            try:
                if message_ids:
                    placeholders = ",".join("?" * len(message_ids))
                    cur = self._conn.execute(
                        f"UPDATE messages SET read_at = ?"
                        f" WHERE id IN ({placeholders}) AND read_at IS NULL",
                        [now, *message_ids],
                    )
                else:
                    cur = self._conn.execute(
                        "UPDATE messages SET read_at = ?"
                        " WHERE (recipient = ? OR recipient = '*')"
                        " AND read_at IS NULL",
                        (now, recipient),
                    )
                self._conn.commit()
                return cur.rowcount
            except sqlite3.Error:
                _log.warning("failed to mark messages read", exc_info=True)
                return 0

    def get_recent(self, limit: int = 50) -> list[Message]:
        """Get recent messages (all, for dashboard display)."""
        if not self._conn:
            return []
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT id, sender, recipient, msg_type, content, created_at, read_at"
                    " FROM messages ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [Message(*r) for r in rows]
            except sqlite3.Error:
                _log.warning("failed to get recent messages", exc_info=True)
                return []

    def delete(self, message_ids: list[int]) -> int:
        """Delete specific messages by id. Returns count deleted."""
        if not self._conn or not message_ids:
            return 0
        placeholders = ",".join("?" for _ in message_ids)
        with self._lock:
            try:
                cur = self._conn.execute(
                    f"DELETE FROM messages WHERE id IN ({placeholders})",
                    tuple(message_ids),
                )
                self._conn.commit()
                return cur.rowcount
            except sqlite3.Error:
                _log.warning("failed to delete messages", exc_info=True)
                return 0

    def prune(self, max_age_days: int = 7) -> int:
        """Delete messages older than max_age_days. Returns count deleted."""
        if not self._conn:
            return 0
        cutoff = time.time() - (max_age_days * 86400)
        with self._lock:
            try:
                cur = self._conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
                self._conn.commit()
                return cur.rowcount
            except sqlite3.Error:
                _log.warning("failed to prune messages", exc_info=True)
                return 0
