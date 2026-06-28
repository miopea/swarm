"""Engagement awareness + duplicate-work detection (task #913).

On 2026-06-26 the Queen fired ``queen_prompt_worker`` at a worker already
engaged on the same P1 via a drone handoff — two coordination paths stacked
redundant injections on one busy worker. This module is the shared, pure core
for closing that gap:

- :func:`engagement_snapshot` — what is this worker currently engaged on
  (its single ACTIVE task + age, its assigned-task count, any recent inbound
  handoff). Surfaced to the Queen so she sees live engagement BEFORE prompting
  (advisory — the prompt always sends).
- :func:`is_duplicate_work` — does an incoming task/handoff duplicate work the
  worker already holds? Deterministic + CONSERVATIVE: matches only on
  structured fields (number / jira_key / same source-worker + high title
  similarity), NEVER on freeform content. Used to suppress duplicate
  auto-handoff spawns.

Both are defensive: a ``None`` board / store or any internal exception yields
an empty snapshot / no match rather than crashing a tool call or a drone sweep.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from swarm.messages.store import Message, MessageStore
    from swarm.tasks.board import TaskBoard
    from swarm.tasks.task import SwarmTask

# Message types that represent action-required inbound coordination (a worker
# being handed work), as opposed to FYI findings / status updates.
_HANDOFF_MSG_TYPES = frozenset({"dependency", "warning"})

_WS_RE = re.compile(r"\s+")


def _normalize_tokens(text: str) -> set[str]:
    """Whitespace-normalize + lowercase + tokenize — same normalization
    precedent as ``messages.send_guard._fingerprint``, but kept as a token
    SET for Jaccard overlap rather than hashed."""
    return set(_WS_RE.sub(" ", (text or "").strip().lower()).split())


def _title_similarity(a: str, b: str) -> float:
    """Jaccard token overlap of two titles in [0.0, 1.0]."""
    ta, tb = _normalize_tokens(a), _normalize_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class EngagementInfo:
    """A worker's live engagement at a point in time. All fields default to
    the 'not engaged / unknown' value so a defensive empty snapshot is valid."""

    worker: str = ""
    active_task: SwarmTask | None = None
    active_started_ago: float | None = None  # seconds since the ACTIVE task started
    assigned_count: int = 0  # ASSIGNED + ACTIVE tasks owned by the worker
    recent_handoff: Message | None = None  # most-recent unread inbound dependency/warning
    recent_handoff_ago: float | None = None  # seconds since that handoff arrived
    # #939: the worker's live PROCESS state (BUZZING/WAITING/RESTING/SLEEPING)
    # + how long it's held it. A worker with NO board task can still be BUSY
    # (e.g. a task-less /audit-docs run while BUZZING) — "no ACTIVE task" must
    # not be misread as "idle/free". Empty/None when the caller didn't supply
    # state (e.g. a defensive snapshot built without a worker handle).
    process_state: str = ""
    process_state_ago: float | None = None  # seconds in the current process state

    def collides_within(self, window_seconds: float) -> bool:
        """Soft collision: the worker became engaged within ``window_seconds``
        — either its ACTIVE task started that recently, or a handoff arrived
        that recently. ``window_seconds <= 0`` disables (never collides)."""
        if window_seconds <= 0:
            return False
        if self.active_started_ago is not None and self.active_started_ago <= window_seconds:
            return True
        if self.recent_handoff_ago is not None and self.recent_handoff_ago <= window_seconds:
            return True
        return False

    def summary(self) -> str:
        """One-line human summary for the tool result + buzz log."""
        parts: list[str] = []
        # Lead with the live process state so "busy but task-less" reads as
        # busy — the #939 misread was treating "no ACTIVE task" as "free".
        if self.process_state:
            ago = f" {int(self.process_state_ago)}s" if self.process_state_ago is not None else ""
            parts.append(f"{self.process_state}{ago}")
        if self.active_task is not None:
            num = getattr(self.active_task, "number", 0)
            title = (getattr(self.active_task, "title", "") or "")[:60]
            ago = (
                f", started {int(self.active_started_ago)}s ago"
                if self.active_started_ago is not None
                else ""
            )
            parts.append(f'ACTIVE #{num} "{title}"{ago}')
        else:
            parts.append("no ACTIVE task")
        if self.assigned_count:
            parts.append(f"{self.assigned_count} assigned/active task(s)")
        if self.recent_handoff is not None:
            sender = getattr(self.recent_handoff, "sender", "?")
            mtype = getattr(self.recent_handoff, "msg_type", "?")
            ago = (
                f" {int(self.recent_handoff_ago)}s ago"
                if self.recent_handoff_ago is not None
                else ""
            )
            parts.append(f"recent inbound {mtype} from {sender}{ago}")
        return "; ".join(parts)


def engagement_snapshot(
    board: TaskBoard | None,
    message_store: MessageStore | None,
    worker_name: str,
    *,
    now: float,
    process_state: str | None = None,
    process_state_ago: float | None = None,
) -> EngagementInfo:
    """Build an :class:`EngagementInfo` for ``worker_name``. Never raises —
    a ``None`` board/store or any internal error yields an empty snapshot.

    ``process_state`` / ``process_state_ago`` (#939) carry the worker's live
    PTY state so prompt-time awareness reflects ACTUAL busyness, not just
    board-task assignment. Optional — callers without a worker handle (defensive
    snapshots) omit them and the state simply isn't surfaced."""
    info = EngagementInfo(worker=worker_name or "")
    info.process_state = (process_state or "").strip()
    info.process_state_ago = process_state_ago
    if not worker_name:
        return info
    if board is not None:
        try:
            active = board.current_task_for_worker(worker_name)
            if active is not None:
                info.active_task = active
                started = getattr(active, "started_at", None)
                if started:
                    info.active_started_ago = max(0.0, now - float(started))
            assigned = board.active_tasks_for_worker(worker_name)
            info.assigned_count = len(assigned or [])
        except Exception:
            pass
    if message_store is not None:
        try:
            unread = message_store.get_unread(worker_name) or []
            handoffs = [m for m in unread if getattr(m, "msg_type", "") in _HANDOFF_MSG_TYPES]
            if handoffs:
                latest = max(handoffs, key=lambda m: getattr(m, "created_at", 0.0) or 0.0)
                info.recent_handoff = latest
                created = getattr(latest, "created_at", None)
                if created:
                    info.recent_handoff_ago = max(0.0, now - float(created))
        except Exception:
            pass
    return info


def is_duplicate_work(
    incoming: Any,
    existing: list[SwarmTask] | None,
    *,
    similarity: float = 0.8,
) -> SwarmTask | None:
    """Return the first task in ``existing`` that ``incoming`` duplicates, or
    ``None``. CONSERVATIVE + deterministic — matches ONLY on structured fields,
    in priority order per candidate:

    1. same non-zero ``number``;
    2. equal non-empty ``jira_key``;
    3. same non-empty ``source_worker`` AND title Jaccard >= ``similarity``.

    Never matches on freeform content. ``incoming`` only needs the attributes
    ``number`` / ``jira_key`` / ``source_worker`` / ``title`` (a not-yet-created
    handoff descriptor is fine)."""
    if not existing:
        return None
    in_num = getattr(incoming, "number", 0) or 0
    in_jira = (getattr(incoming, "jira_key", "") or "").strip()
    in_src = (getattr(incoming, "source_worker", "") or "").strip()
    in_title = getattr(incoming, "title", "") or ""
    for t in existing:
        if in_num and (getattr(t, "number", 0) or 0) == in_num:
            return t
        if in_jira and (getattr(t, "jira_key", "") or "").strip() == in_jira:
            return t
        t_src = (getattr(t, "source_worker", "") or "").strip()
        if in_src and t_src and in_src == t_src:
            if _title_similarity(in_title, getattr(t, "title", "") or "") >= similarity:
                return t
    return None
