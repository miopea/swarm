"""Worker blocker store — persist "I'm blocked on task X" signals (task #250).

Workers call ``swarm_report_blocker`` to tell the IdleWatcher drone
(task #225 Phase 2) to stop nudging them on a specific task until the
blocking dependency clears. Two auto-clear triggers:

  1. The ``blocked_by_task`` flips to ``completed`` on the task board.
  2. A new message lands in the worker's inbox after the blocker was
     declared — something else happened worth paying attention to.

Either trigger clears the blocker without a second MCP call; the
watcher purges the row as part of its sweep.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from swarm.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.db.core import SwarmDB


_log = get_logger("tasks.blockers")


@dataclass(frozen=True)
class Blocker:
    """A declared blocker row. Immutable — updates go through replace."""

    worker: str
    task_number: int
    blocked_by_task: int
    reason: str
    created_at: float


class BlockerStore:
    """Thread-safe wrapper around the ``worker_blockers`` SQLite table.

    Shares the SwarmDB connection and lock so writes serialize
    alongside everything else (messages, tasks, buzz log).
    """

    def __init__(self, swarm_db: SwarmDB) -> None:
        self._db = swarm_db

    def report(
        self,
        worker: str,
        task_number: int,
        blocked_by_task: int,
        reason: str = "",
        *,
        now: float | None = None,
    ) -> Blocker:
        """Insert (or replace) a blocker for ``(worker, task_number)``.

        Re-reporting the same pair updates the reason + refreshes the
        ``created_at`` timestamp. The refreshed timestamp matters for
        the message-based auto-clear: without it, the worker could
        never reset the "no new messages since" window after their
        first report.
        """
        created = now if now is not None else time.time()
        conn = self._db._conn
        if conn is None:
            raise RuntimeError("BlockerStore: SwarmDB connection is not open")
        with self._db._lock:
            conn.execute(
                "INSERT OR REPLACE INTO worker_blockers"
                " (worker, task_number, blocked_by_task, reason, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (worker, int(task_number), int(blocked_by_task), reason or "", created),
            )
            conn.commit()
        _log.info(
            "blocker reported: worker=%s task=#%d blocked_by=#%d",
            worker,
            task_number,
            blocked_by_task,
        )
        return Blocker(
            worker=worker,
            task_number=int(task_number),
            blocked_by_task=int(blocked_by_task),
            reason=reason or "",
            created_at=created,
        )

    def list_for_worker(self, worker: str) -> list[Blocker]:
        """Return every active blocker for ``worker`` (no auto-clear logic)."""
        conn = self._db._conn
        if conn is None:
            return []
        try:
            with self._db._lock:
                rows = conn.execute(
                    "SELECT worker, task_number, blocked_by_task, reason, created_at"
                    " FROM worker_blockers WHERE worker = ?"
                    " ORDER BY created_at DESC",
                    (worker,),
                ).fetchall()
        except sqlite3.Error:
            _log.warning("blocker list_for_worker failed", exc_info=True)
            return []
        return [
            Blocker(
                worker=r[0],
                task_number=int(r[1]),
                blocked_by_task=int(r[2]),
                reason=r[3] or "",
                created_at=float(r[4]),
            )
            for r in rows
        ]

    def clear(self, worker: str, task_number: int) -> bool:
        """Remove the blocker for ``(worker, task_number)``. Returns True if a row was deleted."""
        conn = self._db._conn
        if conn is None:
            return False
        with self._db._lock:
            cur = conn.execute(
                "DELETE FROM worker_blockers WHERE worker = ? AND task_number = ?",
                (worker, int(task_number)),
            )
            conn.commit()
            deleted = cur.rowcount > 0
        if deleted:
            _log.info("blocker cleared: worker=%s task=#%d", worker, task_number)
        return deleted

    def has_active_blocker(
        self,
        worker: str,
        *,
        is_task_completed: Callable[[int], bool] | None = None,
        has_message_since: Callable[[str, float], bool] | None = None,
        on_auto_clear: Callable[[Blocker, str], None] | None = None,
    ) -> Blocker | None:
        """Return a live blocker for ``worker`` that hasn't been auto-cleared yet.

        A blocker is considered cleared (and purged) when EITHER:

          * ``is_task_completed(blocked_by_task)`` returns True — the
            dependency has shipped.
          * ``has_message_since(worker, blocker.created_at)`` returns
            True — the worker has unread inbox traffic newer than the
            blocker, which the operator treats as "something changed;
            check your messages".

        Either callable may be ``None``; in that case the corresponding
        auto-clear is skipped (useful for tests that only want to
        exercise one path).

        ``on_auto_clear(blocker, reason)`` fires once per cleared
        blocker, with ``reason`` in ``{"target_done", "message_since"}``.
        Added in task #529 so the IdleWatcher can buzz-log the clear
        — without it, an operator audit can only infer the clear from
        the absence of subsequent ``AUTO_NUDGE_SKIPPED`` entries.
        Callback exceptions are swallowed so a callback bug never
        breaks the auto-clear chain.

        Returns the first still-active blocker found, or ``None`` if
        the worker is clear to nudge.
        """

        def _do_clear(b: Blocker, reason: str) -> None:
            """Clear the row + fire the operator-facing callback once."""
            self.clear(worker, b.task_number)
            if on_auto_clear is not None:
                try:
                    on_auto_clear(b, reason)
                except Exception:
                    _log.debug("on_auto_clear raised", exc_info=True)

        blockers = self.list_for_worker(worker)
        if not blockers:
            return None
        for b in blockers:
            if self._check_target_done(b, is_task_completed):
                _do_clear(b, "target_done")
                continue
            if self._check_message_since(worker, b, has_message_since):
                _do_clear(b, "message_since")
                continue
            # Survived both auto-clear checks → still blocked.
            return b
        return None

    @staticmethod
    def _check_target_done(b: Blocker, is_task_completed: Callable[[int], bool] | None) -> bool:
        """Did the blocker target task become terminal? Errors → False."""
        if is_task_completed is None:
            return False
        try:
            return bool(is_task_completed(b.blocked_by_task))
        except Exception:
            _log.debug("is_task_completed raised for #%d", b.blocked_by_task, exc_info=True)
            return False

    @staticmethod
    def _check_message_since(
        worker: str,
        b: Blocker,
        has_message_since: Callable[[str, float], bool] | None,
    ) -> bool:
        """Has the worker received new inbox traffic since the blocker filed?"""
        if has_message_since is None:
            return False
        try:
            return bool(has_message_since(worker, b.created_at))
        except Exception:
            _log.debug(
                "has_message_since raised for %s @ %.0f",
                worker,
                b.created_at,
                exc_info=True,
            )
            return False
