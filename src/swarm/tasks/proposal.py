"""AssignmentProposal — Queen-proposed task assignments awaiting user approval."""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from swarm.logging import get_logger
from swarm.tasks.task import TaskStatus

_log = get_logger("tasks.proposal")


class ProposalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ProposalType(str, Enum):
    ASSIGNMENT = "assignment"
    ESCALATION = "escalation"
    COMPLETION = "completion"
    # An ACTIVE task whose worker has stalled with no progress — looks
    # blocked on the operator (not on another task). One-click approve
    # parks it (→ BLOCKED) so the autonomous loops stand down.
    PARK = "park"


class QueenAction(str, Enum):
    CONTINUE = "continue"
    SEND_MESSAGE = "send_message"
    RESTART = "restart"
    WAIT = "wait"
    COMPLETE_TASK = "complete_task"
    ASSIGN_TASK = "assign_task"


@dataclass
class AssignmentProposal:
    """A Queen-proposed assignment of a task to a worker."""

    worker_name: str
    task_id: str = ""
    task_title: str = ""
    message: str = ""
    reasoning: str = ""
    confidence: float = 1.0
    proposal_type: ProposalType = ProposalType.ASSIGNMENT
    assessment: str = ""  # Queen's analysis (escalation only)
    queen_action: str = ""  # "continue"|"send_message"|"restart"|"wait"
    prompt_snippet: str = ""  # Terminal context at decision time
    rule_pattern: str = ""  # Pre-computed regex pattern for rule modal
    is_plan: bool = False  # True when escalation is a plan requiring user approval
    status: ProposalStatus = ProposalStatus.PENDING
    rejection_reason: str = ""  # Operator's reason for rejecting this proposal
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)

    @property
    def age(self) -> float:
        return time.time() - self.created_at

    @classmethod
    def escalation(
        cls,
        *,
        worker_name: str,
        action: str,
        assessment: str,
        message: str = "",
        reasoning: str = "",
        confidence: float = 0.6,
        prompt_snippet: str = "",
        rule_pattern: str = "",
        is_plan: bool = False,
    ) -> AssignmentProposal:
        return cls(
            worker_name=worker_name,
            proposal_type=ProposalType.ESCALATION,
            queen_action=action,
            assessment=assessment,
            message=message,
            reasoning=reasoning or assessment,
            confidence=confidence,
            prompt_snippet=prompt_snippet,
            rule_pattern=rule_pattern,
            is_plan=is_plan,
        )

    @classmethod
    def completion(
        cls,
        *,
        worker_name: str,
        task_id: str,
        task_title: str,
        assessment: str,
        reasoning: str = "",
        confidence: float = 0.8,
    ) -> AssignmentProposal:
        return cls(
            worker_name=worker_name,
            task_id=task_id,
            task_title=task_title,
            proposal_type=ProposalType.COMPLETION,
            queen_action=QueenAction.COMPLETE_TASK,
            assessment=assessment,
            reasoning=reasoning,
            confidence=confidence,
        )

    @classmethod
    def park(
        cls,
        *,
        worker_name: str,
        task_id: str,
        task_title: str,
        assessment: str,
        reasoning: str = "",
        confidence: float = 0.9,
    ) -> AssignmentProposal:
        """Propose parking a stalled, operator-blocked ACTIVE task.

        Approve → ``TaskBoard`` blocks it (→ BLOCKED, off-active) so the
        drone/oversight/completion loops stand down until the operator
        re-dispatches it. Reject → backoff before it can re-propose.
        """
        return cls(
            worker_name=worker_name,
            task_id=task_id,
            task_title=task_title,
            proposal_type=ProposalType.PARK,
            queen_action=QueenAction.WAIT,
            assessment=assessment,
            reasoning=reasoning,
            confidence=confidence,
        )

    @classmethod
    def assignment(
        cls,
        *,
        worker_name: str,
        task_id: str,
        task_title: str,
        message: str,
        reasoning: str = "",
        confidence: float = 0.8,
    ) -> AssignmentProposal:
        return cls(
            worker_name=worker_name,
            task_id=task_id,
            task_title=task_title,
            message=message,
            reasoning=reasoning,
            confidence=confidence,
        )


class ProposalStore:
    """Persistent store for assignment proposals.

    Proposals are saved to a JSON file (if *persist_path* is given)
    and reloaded on startup.  All mutation methods are serialized
    via an ``RLock`` to prevent concurrent corruption.
    """

    _HISTORY_CAP = 100
    _MAX_PROPOSAL_AGE = 3600.0  # 1 hour

    def __init__(self, persist_path: Path | str | None = None) -> None:
        self._lock = threading.RLock()
        self._proposals: dict[str, AssignmentProposal] = {}
        self._history: list[AssignmentProposal] = []
        self._persist_path: Path | None = Path(persist_path) if persist_path else None
        if self._persist_path:
            self._load()

    def add(self, proposal: AssignmentProposal) -> None:
        with self._lock:
            self._proposals[proposal.id] = proposal
            self._save()

    def get(self, proposal_id: str) -> AssignmentProposal | None:
        with self._lock:
            return self._proposals.get(proposal_id)

    def remove(self, proposal_id: str) -> bool:
        with self._lock:
            removed = self._proposals.pop(proposal_id, None) is not None
            if removed:
                self._save()
            return removed

    def update_status(
        self,
        proposal_id: str,
        status: ProposalStatus,
        rejection_reason: str = "",
    ) -> None:
        with self._lock:
            p = self._proposals.get(proposal_id)
            if p is None:
                return
            p.status = status
            if rejection_reason:
                p.rejection_reason = rejection_reason
            self._save()

    @property
    def pending(self) -> list[AssignmentProposal]:
        with self._lock:
            return [p for p in self._proposals.values() if p.status == ProposalStatus.PENDING]

    def pending_for_task(self, task_id: str) -> list[AssignmentProposal]:
        with self._lock:
            return [
                p
                for p in self._proposals.values()
                if p.status == ProposalStatus.PENDING and p.task_id == task_id
            ]

    def pending_for_worker(self, worker_name: str) -> list[AssignmentProposal]:
        with self._lock:
            return [
                p
                for p in self._proposals.values()
                if p.status == ProposalStatus.PENDING and p.worker_name == worker_name
            ]

    def has_pending_escalation(self, worker_name: str) -> bool:
        return any(
            p.proposal_type == ProposalType.ESCALATION for p in self.pending_for_worker(worker_name)
        )

    def has_pending_completion(self, worker_name: str, task_id: str) -> bool:
        return any(
            p.proposal_type == ProposalType.COMPLETION and p.task_id == task_id
            for p in self.pending_for_worker(worker_name)
        )

    def has_pending_park(self, worker_name: str, task_id: str) -> bool:
        return any(
            p.proposal_type == ProposalType.PARK and p.task_id == task_id
            for p in self.pending_for_worker(worker_name)
        )

    def expire_old(self, max_age: float | None = None) -> int:
        """Expire pending proposals older than *max_age* seconds.

        Returns the number of proposals expired.
        """
        with self._lock:
            threshold = max_age if max_age is not None else self._MAX_PROPOSAL_AGE
            count = 0
            for p in list(self._proposals.values()):
                if p.status == ProposalStatus.PENDING and p.age > threshold:
                    p.status = ProposalStatus.EXPIRED
                    count += 1
            if count:
                self._save()
            return count

    def expire_stale(
        self,
        valid_task_ids: set[str],
        valid_worker_names: set[str],
    ) -> int:
        """Expire pending proposals where the task or worker is no longer valid.

        Also expires proposals older than ``_MAX_PROPOSAL_AGE``.
        Returns the number of proposals expired.
        """
        with self._lock:
            count = 0
            for p in list(self._proposals.values()):
                if p.status != ProposalStatus.PENDING:
                    continue
                if p.worker_name not in valid_worker_names:
                    p.status = ProposalStatus.EXPIRED
                    count += 1
                elif p.task_id and p.task_id not in valid_task_ids:
                    p.status = ProposalStatus.EXPIRED
                    count += 1
            # RLock allows re-entrant call to expire_old
            count += self.expire_old()
            if count:
                self._save()
            return count

    def clear_resolved(self) -> int:
        """Move non-pending proposals to history. Returns count moved."""
        with self._lock:
            to_remove = [
                pid for pid, p in self._proposals.items() if p.status != ProposalStatus.PENDING
            ]
            for pid in to_remove:
                self._history.append(self._proposals.pop(pid))
            # Cap history size
            if len(self._history) > self._HISTORY_CAP:
                self._history = self._history[-self._HISTORY_CAP :]
            if to_remove:
                self._save()
            return len(to_remove)

    @property
    def all_proposals(self) -> list[AssignmentProposal]:
        with self._lock:
            return list(self._proposals.values())

    @property
    def history(self) -> list[AssignmentProposal]:
        """Return resolved proposals, newest first."""
        with self._lock:
            return list(reversed(self._history))

    def add_to_history(self, proposal: AssignmentProposal) -> None:
        """Add a resolved proposal directly to history (e.g. auto-actions)."""
        with self._lock:
            self._history.append(proposal)
            if len(self._history) > self._HISTORY_CAP:
                self._history = self._history[-self._HISTORY_CAP :]
            self._save()

    # --- Persistence ---

    def _serialize_proposal(self, p: AssignmentProposal) -> dict:
        return {
            "id": p.id,
            "worker_name": p.worker_name,
            "task_id": p.task_id,
            "task_title": p.task_title,
            "message": p.message,
            "reasoning": p.reasoning,
            "confidence": p.confidence,
            "proposal_type": p.proposal_type.value,
            "assessment": p.assessment,
            "queen_action": p.queen_action,
            "prompt_snippet": p.prompt_snippet,
            "rule_pattern": p.rule_pattern,
            "is_plan": p.is_plan,
            "status": p.status.value,
            "created_at": p.created_at,
        }

    def _deserialize_proposal(self, d: dict[str, object]) -> AssignmentProposal:
        return AssignmentProposal(
            id=d.get("id", uuid.uuid4().hex[:12]),
            worker_name=d.get("worker_name", ""),
            task_id=d.get("task_id", ""),
            task_title=d.get("task_title", ""),
            message=d.get("message", ""),
            reasoning=d.get("reasoning", ""),
            confidence=d.get("confidence", 1.0),
            proposal_type=ProposalType(d.get("proposal_type", "assignment")),
            assessment=d.get("assessment", ""),
            queen_action=d.get("queen_action", ""),
            prompt_snippet=d.get("prompt_snippet", ""),
            rule_pattern=d.get("rule_pattern", ""),
            is_plan=d.get("is_plan", False),
            status=ProposalStatus(d.get("status", "pending")),
            created_at=d.get("created_at", time.time()),
        )

    def save(self) -> None:
        """Persist proposals and history to disk (public API)."""
        self._save()

    def _save(self) -> None:
        if not self._persist_path:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "proposals": [self._serialize_proposal(p) for p in self._proposals.values()],
                "history": [
                    self._serialize_proposal(p) for p in self._history[-self._HISTORY_CAP :]
                ],
            }
            import os

            tmp = self._persist_path.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, self._persist_path)
        except OSError:
            _log.debug("failed to save proposals to %s", self._persist_path, exc_info=True)

    def _load(self) -> None:
        if not self._persist_path or not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text())
            for d in data.get("proposals", []):
                p = self._deserialize_proposal(d)
                self._proposals[p.id] = p
            for d in data.get("history", []):
                self._history.append(self._deserialize_proposal(d))
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            _log.warning("failed to load proposals from %s", self._persist_path, exc_info=True)


def build_worker_task_info(task_board, worker_name: str) -> str:
    """Build a task-info string for a worker's active tasks."""
    if not task_board:
        return ""
    active = [
        t
        for t in task_board.tasks_for_worker(worker_name)
        if t.status in (TaskStatus.ASSIGNED, TaskStatus.ACTIVE)
    ]
    if not active:
        return ""
    lines: list[str] = []
    for t in active:
        lines.append(f"- [{t.id[:12]}] {t.title} (status={t.status.value})")
        if t.description:
            lines.append(f"  Description: {t.description[:200]}")
        if t.acceptance_criteria:
            lines.append("  Acceptance Criteria:")
            for i, c in enumerate(t.acceptance_criteria, 1):
                lines.append(f"    {i}. {c}")
    return "\n".join(lines)
