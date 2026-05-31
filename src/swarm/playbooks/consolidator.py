"""PlaybookConsolidator — merge same-scope near-duplicate playbooks.

Phase 3. A low-frequency sweep (driven by the daemon's consolidation
loop) finds near-duplicate ACTIVE playbooks within the SAME scope and
asks the **headless** Queen (decision shape #8) whether one truly
subsumes the other. On merge: the kept playbook absorbs a merged
body/trigger (version++ , unioned provenance) and the loser is retired.

Subscription-safe (headless ``claude -p`` only). Best-effort — a sweep
failure never propagates. Never merges across scope.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol

from swarm.drones.log import LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.playbooks.models import Playbook, PlaybookStatus

if TYPE_CHECKING:
    from swarm.db.playbook_store import PlaybookStore
    from swarm.drones.log import SystemLog

_log = get_logger("playbooks.consolidator")


class _Queen(Protocol):
    async def ask(self, prompt: str, **kwargs: Any) -> dict[str, Any]: ...


class PlaybookConsolidator:
    def __init__(
        self,
        *,
        queen: _Queen,
        store: PlaybookStore,
        drone_log: SystemLog | None = None,
    ) -> None:
        self._queen = queen
        self._store = store
        self._drone_log = drone_log

    def _buzz(self, detail: str) -> None:
        if self._drone_log is None:
            return
        try:
            self._drone_log.add(
                SystemAction.PLAYBOOK_CONSOLIDATED, "queen", detail, category=LogCategory.DRONE
            )
        except Exception:
            _log.debug("consolidation buzz failed", exc_info=True)

    def _prompt(self, a: Playbook, b: Playbook) -> str:
        return (
            "DECISION SHAPE: Playbook consolidation. Two same-scope playbooks "
            "may be near-duplicates.\n\n"
            f"A [{a.name}] title={a.title!r}\nWhen: {a.trigger}\n{a.body}\n\n"
            f"B [{b.name}] title={b.title!r}\nWhen: {b.trigger}\n{b.body}\n\n"
            "If one truly subsumes the other, merge. Distinct procedures that "
            "merely share keywords are NOT a merge. Reply with the strict JSON "
            "from decision shape #8 — no prose."
        )

    async def consolidate_once(self, *, max_merges: int = 3) -> int:
        """One sweep. Returns the number of merges applied. Never raises
        except ``asyncio.CancelledError``."""
        merges = 0
        handled: set[str] = set()
        try:
            actives = self._store.list(status=PlaybookStatus.ACTIVE, limit=500)
        except Exception:
            _log.warning("consolidation: list failed", exc_info=True)
            return 0
        for pb in actives:
            if merges >= max_merges:
                break
            if pb.name in handled:
                continue
            try:
                other = self._store.find_near_duplicate(
                    pb.body, scope=pb.scope, exclude_name=pb.name
                )
            except Exception:
                # Best-effort sweep — one bad row shouldn't poison the
                # remaining merges, but it must be visible in logs so the
                # operator can investigate index corruption / store bugs.
                _log.warning(
                    "consolidation: find_near_duplicate failed for %s",
                    pb.name,
                    exc_info=True,
                )
                continue
            if (
                other is None
                or other.status != PlaybookStatus.ACTIVE
                or other.scope != pb.scope  # never cross-scope
                or other.name in handled
            ):
                continue
            if await self._maybe_merge(pb, other):
                handled.add(pb.name)
                handled.add(other.name)
                merges += 1
        return merges

    async def _maybe_merge(self, a, b) -> bool:
        try:
            result = await self._queen.ask(self._prompt(a, b), stateless=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("consolidation: queen call failed", exc_info=True)
            return False
        if not isinstance(result, dict) or result.get("error"):
            return False
        if not result.get("merge"):
            return False
        keep_sel = str(result.get("keep") or "").strip().upper()
        if keep_sel == "A":
            keep, loser = a, b
        elif keep_sel == "B":
            keep, loser = b, a
        else:
            return False
        body = str(result.get("body") or keep.body).strip()
        trigger = str(result.get("trigger") or keep.trigger).strip()
        if not body:
            return False
        try:
            ok = self._store.consolidate_into(
                keep.name,
                loser.name,
                body=body,
                trigger=trigger,
                reason=f"consolidated into {keep.name}",
            )
        except Exception:
            _log.warning("consolidate_into failed", exc_info=True)
            return False
        if ok:
            self._buzz(f"{loser.name} → {keep.name} (scope={keep.scope})")
        return ok
