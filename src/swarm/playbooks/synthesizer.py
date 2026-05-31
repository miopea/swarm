"""PlaybookSynthesizer — turn a shipped task into procedural memory.

On a successful completion the daemon hands the task here. We gate hard
(eligible task type, resolution substance, per-(worker,task) memoization,
per-hour cap), ask the **headless** Queen whether the task encodes a
generalizable procedure, and persist a candidate playbook if so.

Subscription-safe: the only model call is ``queen.ask`` (headless
``claude -p``). Never raises into the caller — a synthesis failure must
not affect task completion.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any, Protocol

from swarm.drones.log import LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.playbooks.models import (
    SCOPE_GLOBAL,
    Playbook,
    PlaybookStatus,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.config.models import PlaybookConfig
    from swarm.db.playbook_store import PlaybookStore
    from swarm.drones.log import SystemLog
    from swarm.tasks.task import SwarmTask

_log = get_logger("playbooks.synthesizer")

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SCOPE_RE = re.compile(r"^(global|project:[\w.-]+|worker:[\w.-]+)$")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")[:64]


class _Queen(Protocol):
    async def ask(self, prompt: str, **kwargs: Any) -> dict[str, Any]: ...


class PlaybookSynthesizer:
    def __init__(
        self,
        *,
        queen: _Queen,
        store: PlaybookStore,
        config: PlaybookConfig,
        drone_log: SystemLog | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._queen = queen
        self._store = store
        self._cfg = config
        self._drone_log = drone_log
        self._now = now
        # (worker, task_id) → one synthesis attempt ever (process-lifetime).
        self._seen: set[tuple[str, str]] = set()
        # Sliding-window call timestamps for the per-hour cap.
        self._calls: list[float] = []

    # -- gating --------------------------------------------------------

    def _eligible(self, task: SwarmTask, resolution: str) -> bool:
        if not self._cfg.enabled:
            return False
        ttype = getattr(task.task_type, "value", task.task_type)
        if str(ttype) not in self._cfg.eligible_task_types:
            return False
        if len((resolution or "").strip()) < self._cfg.min_resolution_chars:
            return False
        return True

    def _under_rate(self) -> bool:
        cutoff = self._now() - 3600
        self._calls = [t for t in self._calls if t >= cutoff]
        return len(self._calls) < self._cfg.max_synth_per_hour

    def _buzz(self, action: SystemAction, worker: str, detail: str) -> None:
        if self._drone_log is None:
            return
        try:
            self._drone_log.add(action, worker, detail, category=LogCategory.DRONE)
        except Exception:
            _log.debug("playbook buzz log failed", exc_info=True)

    # -- prompt --------------------------------------------------------

    def _build_prompt(self, task: SwarmTask, resolution: str) -> str:
        ttype = getattr(task.task_type, "value", task.task_type)
        repo = getattr(task, "repo", "") or getattr(task, "project", "")
        return (
            "DECISION SHAPE: Playbook synthesis.\n"
            f"A task just shipped successfully on worker repo '{repo or 'unknown'}'.\n\n"
            f"Title: {task.title}\n"
            f"Type: {ttype}\n"
            f"Description: {getattr(task, 'description', '') or 'N/A'}\n"
            f"Resolution (what was actually done):\n{resolution}\n\n"
            "Does this encode a GENERALIZABLE, reusable procedure that would "
            "help a DIFFERENT future task? Decline (synthesize:false) for "
            "one-off bug specifics, pure config edits, or anything you cannot "
            "state as reusable steps. Reply with the strict JSON contract from "
            "decision shape #7 — no prose."
        )

    # -- main ----------------------------------------------------------

    async def synthesize(self, task: SwarmTask, *, worker: str, resolution: str) -> Playbook | None:
        """Best-effort. Returns the saved Playbook or None. Never raises
        except ``asyncio.CancelledError`` (cooperative shutdown)."""
        key = (worker, str(getattr(task, "id", "")))
        if key in self._seen:
            return None
        if not self._eligible(task, resolution):
            self._seen.add(key)
            return None
        if not self._under_rate():
            self._seen.add(key)
            self._buzz(SystemAction.PLAYBOOK_SKIPPED, worker, "rate cap reached")
            return None

        # Reserve the slot BEFORE the call so a concurrent/repeat fire for
        # the same (worker, task) cannot double-spend a Queen call.
        self._seen.add(key)
        self._calls.append(self._now())

        try:
            result = await self._queen.ask(self._build_prompt(task, resolution), stateless=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("playbook synthesis: queen call failed", exc_info=True)
            self._buzz(SystemAction.PLAYBOOK_SKIPPED, worker, "queen error")
            return None

        pb = self._verdict_to_playbook(result, task, worker)
        if pb is None:
            return None
        try:
            saved = self._store.create(pb)
        except Exception:
            _log.warning("playbook persist failed", exc_info=True)
            self._buzz(SystemAction.PLAYBOOK_SKIPPED, worker, "persist error")
            return None

        self._buzz(
            SystemAction.PLAYBOOK_SYNTHESIZED,
            worker,
            f"{saved.name} (conf={pb.confidence:.2f}, scope={pb.scope})",
        )
        return saved

    def _verdict_to_playbook(
        self, result: dict[str, Any], task: SwarmTask, worker: str
    ) -> Playbook | None:
        """Validate the Queen's JSON verdict into a candidate Playbook,
        or None (logging the skip reason). Kept separate so ``synthesize``
        stays within the cyclomatic budget."""
        if not isinstance(result, dict) or result.get("error"):
            self._buzz(SystemAction.PLAYBOOK_SKIPPED, worker, "no usable verdict")
            return None
        if not result.get("synthesize"):
            self._buzz(SystemAction.PLAYBOOK_SKIPPED, worker, "queen declined")
            return None
        name = _slug(str(result.get("name") or result.get("title") or task.title))
        body = str(result.get("body") or "").strip()
        if not name or not body:
            self._buzz(SystemAction.PLAYBOOK_SKIPPED, worker, "empty name/body")
            return None
        scope = str(result.get("scope") or SCOPE_GLOBAL)
        if not _SCOPE_RE.match(scope):
            scope = SCOPE_GLOBAL
        try:
            confidence = float(result.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        return Playbook(
            name=name,
            title=str(result.get("title") or task.title)[:200],
            scope=scope,
            trigger=str(result.get("trigger") or "")[:500],
            body=body,
            provenance_task_ids=[str(getattr(task, "id", ""))],
            source_worker=worker,
            confidence=confidence,
            status=PlaybookStatus.CANDIDATE,
        )
