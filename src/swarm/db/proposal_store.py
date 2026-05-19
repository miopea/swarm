"""SQLite-backed proposal store — drop-in replacement for ProposalStore."""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from typing import TYPE_CHECKING

from swarm.db.base_store import BaseStore
from swarm.logging import get_logger
from swarm.tasks.proposal import (
    AssignmentProposal,
    ProposalStatus,
    ProposalType,
)

if TYPE_CHECKING:
    from swarm.db.core import SwarmDB

_log = get_logger("db.proposal_store")

_MAX_PROPOSAL_AGE = 3600.0  # 1 hour
_HISTORY_PRUNE_DAYS = 30


class SqliteProposalStore(BaseStore):
    """Proposal store backed by the proposals table in swarm.db.

    Drop-in replacement for :class:`~swarm.tasks.proposal.ProposalStore`.
    Thread-safe via RLock (matches original API contract).
    """

    def __init__(self, db: SwarmDB) -> None:
        self._db = db
        self._lock = threading.RLock()

    def add(self, proposal: AssignmentProposal) -> None:
        with self._lock:
            self._db.insert("proposals", _proposal_to_row(proposal))

    def get(self, proposal_id: str) -> AssignmentProposal | None:
        with self._lock:
            row = self._db.fetchone("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
            return _row_to_proposal(row) if row else None

    def remove(self, proposal_id: str) -> bool:
        with self._lock:
            return self._db.delete("proposals", "id = ?", (proposal_id,)) > 0

    def update_status(
        self,
        proposal_id: str,
        status: ProposalStatus,
        rejection_reason: str = "",
    ) -> None:
        """Update a proposal's status in the DB."""
        with self._lock:
            data: dict[str, object] = {
                "status": status.value,
                "resolved_at": time.time(),
            }
            if rejection_reason:
                data["rejection_reason"] = rejection_reason
            self._db.update("proposals", data, "id = ?", (proposal_id,))

    @property
    def pending(self) -> list[AssignmentProposal]:
        with self._lock:
            rows = self._db.fetchall(
                "SELECT * FROM proposals WHERE status = 'pending' ORDER BY created_at"
            )
            return [_row_to_proposal(r) for r in rows]

    def pending_for_task(self, task_id: str) -> list[AssignmentProposal]:
        with self._lock:
            rows = self._db.fetchall(
                "SELECT * FROM proposals WHERE status = 'pending' AND task_id = ?",
                (task_id,),
            )
            return [_row_to_proposal(r) for r in rows]

    def pending_for_worker(self, worker_name: str) -> list[AssignmentProposal]:
        with self._lock:
            rows = self._db.fetchall(
                "SELECT * FROM proposals WHERE status = 'pending' AND worker_name = ?",
                (worker_name,),
            )
            return [_row_to_proposal(r) for r in rows]

    def has_pending_escalation(self, worker_name: str) -> bool:
        with self._lock:
            row = self._db.fetchone(
                "SELECT 1 FROM proposals WHERE status = 'pending' "
                "AND worker_name = ? AND proposal_type = 'escalation' "
                "LIMIT 1",
                (worker_name,),
            )
            return row is not None

    def has_pending_completion(self, worker_name: str, task_id: str) -> bool:
        with self._lock:
            row = self._db.fetchone(
                "SELECT 1 FROM proposals WHERE status = 'pending' "
                "AND worker_name = ? AND task_id = ? "
                "AND proposal_type = 'completion' LIMIT 1",
                (worker_name, task_id),
            )
            return row is not None

    def has_pending_park(self, worker_name: str, task_id: str) -> bool:
        with self._lock:
            row = self._db.fetchone(
                "SELECT 1 FROM proposals WHERE status = 'pending' "
                "AND worker_name = ? AND task_id = ? "
                "AND proposal_type = 'park' LIMIT 1",
                (worker_name, task_id),
            )
            return row is not None

    def expire_old(self, max_age: float | None = None) -> int:
        threshold = max_age if max_age is not None else _MAX_PROPOSAL_AGE
        cutoff = time.time() - threshold
        with self._lock:
            return self._db.update(
                "proposals",
                {"status": "expired", "resolved_at": time.time()},
                "status = 'pending' AND created_at < ?",
                (cutoff,),
            )

    def expire_stale(
        self,
        valid_task_ids: set[str],
        valid_worker_names: set[str],
    ) -> int:
        with self._lock:
            now = time.time()
            expired = 0
            # Expire proposals for workers that no longer exist
            if valid_worker_names:
                w_ph = ",".join("?" for _ in valid_worker_names)
                expired += self._db.execute(
                    "UPDATE proposals SET status = 'expired', resolved_at = ?"
                    f" WHERE status = 'pending' AND worker_name NOT IN ({w_ph})",
                    (now, *valid_worker_names),
                ).rowcount
            else:
                expired += self._db.execute(
                    "UPDATE proposals SET status = 'expired', resolved_at = ?"
                    " WHERE status = 'pending'",
                    (now,),
                ).rowcount
            # Expire proposals for tasks that no longer exist
            if valid_task_ids:
                t_ph = ",".join("?" for _ in valid_task_ids)
                expired += self._db.execute(
                    "UPDATE proposals SET status = 'expired', resolved_at = ?"
                    " WHERE status = 'pending' AND task_id IS NOT NULL"
                    f" AND task_id != '' AND task_id NOT IN ({t_ph})",
                    (now, *valid_task_ids),
                ).rowcount
            self._db.commit()
            expired += self.expire_old()
        return expired

    def clear_resolved(self) -> int:
        """No-op for SQLite — resolved proposals stay in the table.

        Pruning is handled by ``prune_history()``.
        """
        return 0

    @property
    def all_proposals(self) -> list[AssignmentProposal]:
        with self._lock:
            rows = self._db.fetchall(
                "SELECT * FROM proposals WHERE status = 'pending' ORDER BY created_at"
            )
            return [_row_to_proposal(r) for r in rows]

    @property
    def history(self) -> list[AssignmentProposal]:
        with self._lock:
            rows = self._db.fetchall(
                "SELECT * FROM proposals WHERE status != 'pending' "
                "ORDER BY resolved_at DESC LIMIT 100"
            )
            return [_row_to_proposal(r) for r in rows]

    def add_to_history(self, proposal: AssignmentProposal) -> None:
        """Add a resolved proposal (e.g. auto-actions)."""
        with self._lock:
            row = _proposal_to_row(proposal)
            if proposal.status == ProposalStatus.PENDING:
                row["status"] = "approved"
            row["resolved_at"] = time.time()
            try:
                self._db.insert("proposals", row)
            except sqlite3.IntegrityError:
                self._db.update(
                    "proposals",
                    {"status": row["status"], "resolved_at": row["resolved_at"]},
                    "id = ?",
                    (proposal.id,),
                )

    def save(self) -> None:
        """No-op — SQLite auto-commits on each operation."""

    def prune_history(self, max_age_days: int = _HISTORY_PRUNE_DAYS) -> int:
        """Delete resolved proposals older than max_age_days."""
        with self._lock:
            return self._prune_older_than(
                "proposals",
                "resolved_at",
                max_age_days,
                extra_where="status != 'pending'",
            )


def _proposal_to_row(p: AssignmentProposal) -> dict:
    return {
        "id": p.id,
        "worker_name": p.worker_name,
        "task_id": p.task_id,
        "task_title": p.task_title,
        "proposal_type": p.proposal_type.value,
        "status": p.status.value,
        "confidence": p.confidence,
        "assessment": p.assessment,
        "message": p.message,
        "reasoning": p.reasoning,
        "queen_action": p.queen_action
        if isinstance(p.queen_action, str)
        else p.queen_action.value
        if hasattr(p.queen_action, "value")
        else str(p.queen_action),
        "prompt_snippet": p.prompt_snippet,
        "rule_pattern": p.rule_pattern,
        "is_plan": 1 if p.is_plan else 0,
        "rejection_reason": p.rejection_reason,
        "created_at": p.created_at,
        "resolved_at": None,
    }


def _row_to_proposal(row: dict) -> AssignmentProposal:
    return AssignmentProposal(
        id=row["id"] or uuid.uuid4().hex[:12],
        worker_name=row["worker_name"] or "",
        task_id=row["task_id"] or "",
        task_title=row["task_title"] or "",
        message=row["message"] or "",
        reasoning=row["reasoning"] or "",
        confidence=row["confidence"] or 1.0,
        proposal_type=ProposalType(row["proposal_type"] or "assignment"),
        assessment=row["assessment"] or "",
        queen_action=row["queen_action"] or "",
        prompt_snippet=row["prompt_snippet"] or "",
        rule_pattern=row["rule_pattern"] or "",
        is_plan=bool(row["is_plan"]),
        status=ProposalStatus(row["status"] or "pending"),
        rejection_reason=row["rejection_reason"] or "",
        created_at=row["created_at"] or time.time(),
    )
