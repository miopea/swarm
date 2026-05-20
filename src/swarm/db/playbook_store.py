"""PlaybookStore — persistence for synthesized procedural memory.

Backs the v10 ``playbooks`` / ``playbook_events`` tables. Distinct from
``SkillsStore`` (the v5 slash-command registry) — see
``docs/specs/playbook-synthesis-loop.md``.

FTS is *optional acceleration*: a ``playbooks_fts`` virtual table is
created at init when the SQLite build has fts5, and kept in sync by this
store's own writes. When fts5 is unavailable, ``search`` /
``find_near_duplicate`` fall back to ``LIKE`` so the feature degrades
rather than breaks.
"""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from typing import TYPE_CHECKING

from swarm.db.base_store import BaseStore
from swarm.logging import get_logger
from swarm.playbooks.models import Playbook, PlaybookStatus

if TYPE_CHECKING:
    from swarm.db.core import SwarmDB

_log = get_logger("db.playbook_store")

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def _fts_query(text: str) -> str:
    """Turn arbitrary text into a safe fts5 OR-query of quoted tokens."""
    tokens = _WORD_RE.findall(text or "")
    return " OR ".join(f'"{t}"' for t in tokens)


class PlaybookStore(BaseStore):
    """CRUD + FTS search + exact-dup rejection for ``playbooks``."""

    def __init__(self, db: SwarmDB) -> None:
        self._db = db
        self._fts = self._ensure_fts()

    # -- FTS bootstrap -------------------------------------------------

    def _ensure_fts(self) -> bool:
        try:
            self._db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS playbooks_fts "
                "USING fts5(name UNINDEXED, title, trigger, body)"
            )
            self._db.commit()
            return True
        except sqlite3.OperationalError:
            _log.info("fts5 unavailable — PlaybookStore falls back to LIKE search")
            return False
        except Exception:
            _log.warning("playbooks_fts init failed — using LIKE search", exc_info=True)
            return False

    def _fts_upsert(self, pb: Playbook) -> None:
        if not self._fts:
            return
        try:
            self._db.execute("DELETE FROM playbooks_fts WHERE name = ?", (pb.name,))
            self._db.execute(
                "INSERT INTO playbooks_fts (name, title, trigger, body) VALUES (?, ?, ?, ?)",
                (pb.name, pb.title, pb.trigger, pb.body),
            )
        except Exception:
            _log.debug("playbooks_fts upsert failed for %s", pb.name, exc_info=True)

    # -- writes --------------------------------------------------------

    def create(self, pb: Playbook) -> Playbook:
        """Insert a playbook, or fold an exact duplicate into the existing.

        Exact dup = same ``content_hash``. Rather than a second row we
        append provenance + bump ``uses`` on the incumbent and return it
        (Hermes-style "reject duplicate memory"). The caller can tell it
        was a dup because the returned ``id`` is not ``pb.id``.
        """
        existing = self._db.fetchone(
            "SELECT * FROM playbooks WHERE content_hash = ?", (pb.content_hash,)
        )
        if existing is not None:
            incumbent = _row_to_pb(existing)
            merged = sorted(set(incumbent.provenance_task_ids) | set(pb.provenance_task_ids))
            self._db.execute(
                "UPDATE playbooks SET provenance_task_ids = ?, uses = uses + 1, "
                "updated_at = ? WHERE id = ?",
                (self._json(merged), time.time(), incumbent.id),
            )
            self._db.commit()
            self.record_event(
                incumbent.id,
                "synthesized",
                worker=pb.source_worker,
                detail="exact-duplicate folded",
            )
            refreshed = self._db.fetchone("SELECT * FROM playbooks WHERE id = ?", (incumbent.id,))
            return _row_to_pb(refreshed)

        pb.id = pb.id or uuid.uuid4().hex
        now = time.time()
        pb.created_at = pb.created_at or now
        pb.updated_at = now
        self._db.execute(
            """
            INSERT INTO playbooks
              (id, name, title, scope, trigger, body, provenance_task_ids,
               source_worker, confidence, uses, wins, losses, status, version,
               content_hash, created_at, updated_at, last_used_at, retired_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pb.id,
                pb.name,
                pb.title,
                pb.scope,
                pb.trigger,
                pb.body,
                self._json(pb.provenance_task_ids),
                pb.source_worker,
                pb.confidence,
                pb.uses,
                pb.wins,
                pb.losses,
                pb.status.value,
                pb.version,
                pb.content_hash,
                pb.created_at,
                pb.updated_at,
                pb.last_used_at,
                pb.retired_reason,
            ),
        )
        self._fts_upsert(pb)
        self._db.commit()
        self.record_event(pb.id, "synthesized", worker=pb.source_worker)
        return pb

    def record_event(
        self,
        playbook_id: str,
        event: str,
        *,
        task_id: str = "",
        worker: str = "",
        detail: str = "",
    ) -> None:
        self._db.execute(
            "INSERT INTO playbook_events (playbook_id, task_id, worker, event, ts, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (playbook_id, task_id, worker, event, time.time(), detail),
        )
        self._db.commit()

    # -- Phase 2: applied tracking + outcomes + lifecycle --------------

    def mark_applied(self, playbook_id: str, *, task_id: str, worker: str) -> None:
        """Record that a playbook was injected into a task's dispatch.

        Bumps ``uses`` + ``last_used_at`` and writes an ``applied`` event
        so outcome attribution can later find which playbooks a task
        used.
        """
        now = time.time()
        self._db.execute(
            "UPDATE playbooks SET uses = uses + 1, last_used_at = ?, updated_at = ? WHERE id = ?",
            (now, now, playbook_id),
        )
        self._db.commit()
        self.record_event(playbook_id, "applied", task_id=task_id, worker=worker)

    def playbooks_applied_to_task(self, task_id: str) -> list[str]:
        rows = self._db.fetchall(
            "SELECT DISTINCT playbook_id FROM playbook_events "
            "WHERE event = 'applied' AND task_id = ?",
            (task_id,),
        )
        return [r["playbook_id"] for r in rows]

    def record_outcome(self, playbook_id: str, win: bool, *, task_id: str = "") -> None:
        col = "wins" if win else "losses"
        self._db.execute(
            f"UPDATE playbooks SET {col} = {col} + 1, updated_at = ? WHERE id = ?",
            (time.time(), playbook_id),
        )
        self._db.commit()
        self.record_event(playbook_id, "win" if win else "loss", task_id=task_id)

    def promote(self, name: str) -> bool:
        """candidate → active. Returns False if missing or already active."""
        pb = self.get(name)
        if pb is None or pb.status == PlaybookStatus.ACTIVE:
            return False
        self._db.execute(
            "UPDATE playbooks SET status = ?, updated_at = ? WHERE name = ?",
            (PlaybookStatus.ACTIVE.value, time.time(), name),
        )
        self._db.commit()
        self.record_event(pb.id, "promoted")
        return True

    def retire(self, name: str, reason: str) -> bool:
        """→ retired with reason. Returns False if missing or already retired."""
        pb = self.get(name)
        if pb is None or pb.status == PlaybookStatus.RETIRED:
            return False
        self._db.execute(
            "UPDATE playbooks SET status = ?, retired_reason = ?, updated_at = ? WHERE name = ?",
            (PlaybookStatus.RETIRED.value, reason, time.time(), name),
        )
        self._db.commit()
        self.record_event(pb.id, "retired", detail=reason)
        return True

    def consolidate_into(
        self, keep_name: str, loser_name: str, *, body: str, trigger: str, reason: str
    ) -> bool:
        """Merge ``loser`` into ``keep``: rewrite keep's body/trigger,
        bump version, union provenance, recompute content_hash + FTS,
        then retire the loser. No-op (False) unless both exist, differ,
        and are not already retired.
        """
        from swarm.playbooks.models import content_hash

        keep = self.get(keep_name)
        loser = self.get(loser_name)
        if keep is None or loser is None or keep.name == loser.name:
            return False
        if PlaybookStatus.RETIRED in (keep.status, loser.status):
            return False
        merged_prov = sorted(set(keep.provenance_task_ids) | set(loser.provenance_task_ids))
        self._db.execute(
            "UPDATE playbooks SET body = ?, trigger = ?, version = version + 1, "
            "content_hash = ?, provenance_task_ids = ?, updated_at = ? WHERE name = ?",
            (
                body,
                trigger,
                content_hash(body),
                self._json(merged_prov),
                time.time(),
                keep_name,
            ),
        )
        self._db.commit()
        keep = self.get(keep_name)
        if keep is not None:
            self._fts_upsert(keep)
            self._db.commit()
            self.record_event(keep.id, "consolidated", detail=f"absorbed {loser_name}")
        self.retire(loser_name, reason)
        return True

    def evaluate_lifecycle(
        self,
        name: str,
        *,
        promote_uses: int,
        promote_winrate: float,
        prune_uses: int,
        prune_winrate: float,
    ) -> str | None:
        """Apply auto-promote / prune rules to one playbook.

        Config-free by design — the daemon passes thresholds from
        ``PlaybookConfig`` so the store has no config dependency.
        Returns ``"promoted"`` / ``"retired"`` / ``None``. Never prunes
        on a 0.0 winrate that simply reflects no decided outcomes yet.
        """
        pb = self.get(name)
        if pb is None:
            return None
        if (
            pb.status == PlaybookStatus.CANDIDATE
            and pb.uses >= promote_uses
            and pb.winrate >= promote_winrate
        ):
            return "promoted" if self.promote(name) else None
        decided = pb.wins + pb.losses
        if (
            pb.status != PlaybookStatus.RETIRED
            and pb.uses >= prune_uses
            and decided > 0
            and pb.winrate < prune_winrate
        ):
            return "retired" if self.retire(name, "auto-pruned: low win rate") else None
        return None

    # -- reads ---------------------------------------------------------

    def get(self, name: str) -> Playbook | None:
        row = self._db.fetchone("SELECT * FROM playbooks WHERE name = ?", (name,))
        return _row_to_pb(row) if row else None

    def get_by_id(self, pb_id: str) -> Playbook | None:
        row = self._db.fetchone("SELECT * FROM playbooks WHERE id = ?", (pb_id,))
        return _row_to_pb(row) if row else None

    def list(
        self,
        *,
        scope: str | None = None,
        status: PlaybookStatus | None = None,
        limit: int = 200,
    ) -> list[Playbook]:
        sql = "SELECT * FROM playbooks"
        clauses: list[str] = []
        params: list[object] = []
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return [_row_to_pb(r) for r in self._db.fetchall(sql, tuple(params))]

    def search(
        self,
        query: str,
        *,
        scope: str | None = None,
        status: PlaybookStatus | None = PlaybookStatus.ACTIVE,
        limit: int = 10,
    ) -> list[Playbook]:
        """Rank playbooks by relevance to *query* (fts5, LIKE fallback)."""
        rows: list[sqlite3.Row] = []
        if self._fts and (fq := _fts_query(query)):
            try:
                rows = self._db.fetchall(
                    "SELECT p.* FROM playbooks_fts f JOIN playbooks p ON p.name = f.name "
                    "WHERE playbooks_fts MATCH ? ORDER BY rank LIMIT ?",
                    (fq, limit * 4),
                )
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            like = f"%{query.strip()}%"
            rows = self._db.fetchall(
                "SELECT * FROM playbooks WHERE title LIKE ? OR trigger LIKE ? OR body LIKE ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (like, like, like, limit * 4),
            )
        out: list[Playbook] = []
        for r in rows:
            pb = _row_to_pb(r)
            if scope is not None and pb.scope != scope:
                continue
            if status is not None and pb.status != status:
                continue
            out.append(pb)
            if len(out) >= limit:
                break
        return out

    def find_near_duplicate(
        self, body: str, *, scope: str | None = None, exclude_name: str | None = None
    ) -> Playbook | None:
        """Best existing playbook that overlaps *body* — consolidation hint.

        Phase 1 uses it only as a signal for the synthesizer to prefer
        updating an incumbent over creating a near-twin; the merge logic
        itself lands in Phase 2/3.
        """
        # Fetch several: when *body* is a playbook's own text the top FTS
        # hit is that playbook itself, so a limit=1 + self-exclude would
        # always yield nothing. Return the first ranked non-excluded hit.
        for pb in self.search(body, scope=scope, status=None, limit=5):
            if exclude_name and pb.name == exclude_name:
                continue
            return pb
        return None

    # -- analytics (P4) ------------------------------------------------
    #
    # The dashboard's Playbooks tab needs more than a flat list: aggregate
    # counts by status/scope, top movers by usage and winrate, recent
    # event timeline per playbook, and total event volume in a rolling
    # window. Everything below is read-only and rides the existing
    # (playbook_id, ts) index on playbook_events — no schema changes.

    def get_events_for_playbook(
        self,
        playbook_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        """Recent events for one playbook, newest first."""
        rows = self._db.fetchall(
            "SELECT event, ts, task_id, worker, detail "
            "FROM playbook_events WHERE playbook_id = ? "
            "ORDER BY ts DESC LIMIT ?",
            (playbook_id, max(1, min(limit, 1000))),
        )
        return [
            {
                "event": r["event"],
                "ts": float(r["ts"] or 0.0),
                "task_id": r["task_id"] or "",
                "worker": r["worker"] or "",
                "detail": r["detail"] or "",
            }
            for r in rows
        ]

    def get_analytics(self, *, since_ts: float | None = None) -> dict[str, object]:
        """Aggregate counts + top movers for the dashboard analytics pane.

        Returns a dict with:
          - ``totals`` — count by status (active, candidate, retired)
          - ``scope_breakdown`` — per-scope-prefix count / uses / winrate
          - ``event_counts`` — events recorded since ``since_ts``
            (defaults to last 24h) grouped by event type
          - ``top_by_uses`` — 5 most-used playbooks (any status)
          - ``top_by_winrate`` — 5 highest-winrate playbooks with
            ``uses >= 3`` (so a single lucky win doesn't dominate)

        Pure aggregation queries — no per-playbook fanout, so this runs
        in O(playbooks + events_in_window) regardless of fleet size.
        """
        if since_ts is None:
            since_ts = time.time() - 24 * 3600

        # Status totals — straightforward GROUP BY.
        totals = {"active": 0, "candidate": 0, "retired": 0}
        for row in self._db.fetchall("SELECT status, COUNT(*) AS n FROM playbooks GROUP BY status"):
            status = row["status"] or "candidate"
            totals[status] = int(row["n"])

        # Scope breakdown: each playbook scope is either "global" or
        # something like "project:rcg-hub" / "worker:nexus". We bucket by
        # the prefix-before-colon so the operator sees totals per family.
        scope_breakdown: dict[str, dict[str, float]] = {}
        for row in self._db.fetchall("SELECT scope, uses, wins, losses FROM playbooks"):
            scope = row["scope"] or "global"
            prefix = scope.split(":", 1)[0]
            bucket = scope_breakdown.setdefault(
                prefix, {"count": 0.0, "uses": 0.0, "wins": 0.0, "losses": 0.0}
            )
            bucket["count"] += 1
            bucket["uses"] += int(row["uses"] or 0)
            bucket["wins"] += int(row["wins"] or 0)
            bucket["losses"] += int(row["losses"] or 0)
        # Derive winrate per scope. winrate = wins / (wins + losses);
        # NULL when no attribution recorded yet so the UI can render "—".
        for bucket in scope_breakdown.values():
            attributed = bucket["wins"] + bucket["losses"]
            bucket["winrate"] = bucket["wins"] / attributed if attributed > 0 else -1.0

        # Event counts in the window.
        event_counts: dict[str, int] = {}
        for row in self._db.fetchall(
            "SELECT event, COUNT(*) AS n FROM playbook_events WHERE ts >= ? GROUP BY event",
            (since_ts,),
        ):
            event_counts[row["event"]] = int(row["n"])

        # Top by uses — straight ORDER BY. Excludes retired so the list
        # tracks what the fleet is *actively leaning on*.
        top_by_uses: list[dict[str, object]] = []
        for row in self._db.fetchall(
            "SELECT name, title, scope, uses, wins, losses, status "
            "FROM playbooks WHERE status != 'retired' "
            "ORDER BY uses DESC LIMIT 5"
        ):
            wins = int(row["wins"] or 0)
            losses = int(row["losses"] or 0)
            attributed = wins + losses
            top_by_uses.append(
                {
                    "name": row["name"],
                    "title": row["title"] or row["name"],
                    "scope": row["scope"] or "global",
                    "status": row["status"],
                    "uses": int(row["uses"] or 0),
                    "winrate": (wins / attributed) if attributed > 0 else -1.0,
                }
            )

        # Top by winrate — same shape, gated on uses >= 3 so a 1-win-0-loss
        # candidate doesn't outrank a 50-and-10 active.
        top_by_winrate: list[dict[str, object]] = []
        for row in self._db.fetchall(
            "SELECT name, title, scope, uses, wins, losses, status FROM playbooks "
            "WHERE status != 'retired' AND uses >= 3 AND (wins + losses) > 0 "
            "ORDER BY (1.0 * wins / (wins + losses)) DESC, uses DESC LIMIT 5"
        ):
            wins = int(row["wins"] or 0)
            losses = int(row["losses"] or 0)
            attributed = wins + losses
            top_by_winrate.append(
                {
                    "name": row["name"],
                    "title": row["title"] or row["name"],
                    "scope": row["scope"] or "global",
                    "status": row["status"],
                    "uses": int(row["uses"] or 0),
                    "winrate": (wins / attributed) if attributed > 0 else -1.0,
                }
            )

        return {
            "totals": totals,
            "scope_breakdown": scope_breakdown,
            "event_counts": event_counts,
            "since_ts": since_ts,
            "top_by_uses": top_by_uses,
            "top_by_winrate": top_by_winrate,
        }

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _json(value: object) -> str:
        import json

        return json.dumps(value)


def _row_to_pb(row: sqlite3.Row) -> Playbook:
    return Playbook(
        id=row["id"],
        name=row["name"],
        title=row["title"] or "",
        scope=row["scope"] or "global",
        trigger=row["trigger"] or "",
        body=row["body"] or "",
        provenance_task_ids=BaseStore._parse_json_field(row["provenance_task_ids"], []),
        source_worker=row["source_worker"] or "",
        confidence=float(row["confidence"] or 0.0),
        uses=int(row["uses"] or 0),
        wins=int(row["wins"] or 0),
        losses=int(row["losses"] or 0),
        status=PlaybookStatus(row["status"] or "candidate"),
        version=int(row["version"] or 1),
        content_hash=row["content_hash"] or "",
        created_at=float(row["created_at"] or time.time()),
        updated_at=float(row["updated_at"] or time.time()),
        last_used_at=row["last_used_at"],
        retired_reason=row["retired_reason"] or "",
    )
