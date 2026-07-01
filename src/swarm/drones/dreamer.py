"""Dreamer drone — periodic pattern-mining over the buzz log → learnings.

The Dreamer scans recent buzz log activity for recurring failure /
oversight signatures (verifier reopens, task failures, oversight
interventions, blockers) and writes high-confidence patterns into the
``queen_learnings`` table tagged ``discovered_by_dreamer:{key}`` so the
existing learnings tools (`swarm_get_learnings`, `queen_query_learnings`)
surface them naturally.

This is the "Dreaming" pattern Anthropic shipped 2026-05-06: instead of
relying on the operator to manually save lessons after every incident,
the system notices recurring patterns and auto-curates the memory.

v1 is *deterministic* — no LLM call, hermetic tests, zero per-sweep
cost. v2 (out of scope) can layer the headless Queen on top to write
better summaries once we see whether the deterministic version is
hitting useful patterns.

Bucketing rules:

* Filter by action set (failure-side signals only — see
  :data:`_PATTERN_ACTIONS`).  Success-side bucketing was considered but
  deferred to v2 — pattern *failures* are the high-leverage signal.
* Normalize ``detail`` (lowercase, strip task numbers like ``#123``,
  strip ISO timestamps, collapse whitespace) and take the first 80
  chars as the signature key.
* A bucket becomes a learning when (a) it crosses
  ``dreamer_min_pattern_count`` AND (b) it involves at least 2 distinct
  workers — guards against a single chatty worker manufacturing
  "patterns" through repetition.

Dedupe:

* Each emitted learning's ``applied_to`` is
  ``discovered_by_dreamer:{action}:{sig_hash}``.
* Pre-write, the dreamer checks for an existing row with the same tag.
  If found and < 7 days old → skip.  If found and ≥ 7 days old →
  refresh (write a new row; the old one stays as historical record).
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from swarm.drones.log import LogCategory, SystemAction
from swarm.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.config.models import DroneConfig
    from swarm.drones.log import SystemLog

_log = get_logger("drones.dreamer")


# Failure-side signals worth mining. Success-side patterns
# (TASK_COMPLETED, VERIFIER_TIER1_PASSED) deferred to v2 — failures are
# higher leverage to surface as learnings.
_PATTERN_ACTIONS: frozenset[str] = frozenset(
    {
        "VERIFIER_TIER1_REOPENED",
        "VERIFIER_TIER2_REOPENED",
        "VERIFIER_TIER2_UNCERTAIN",
        "TASK_FAILED",
        "OVERSIGHT_INTERVENTION",
        "AUTO_NUDGE_SKIPPED",  # worker-reported blocker — recurring blockers are a signal
    }
)

# Refresh window: a dreamer learning older than this gets re-written
# (the old row stays as a historical record). Picked to balance "don't
# spam queen_learnings" against "don't let the lesson go stale".
_REFRESH_AFTER_SECONDS: float = 7 * 86400.0

# Signature normalization: each is applied in order to the raw detail
# string. Order matters — strip the noisy bits first, collapse last.
_TASK_NUMBER_RE = re.compile(r"#\d+")
# ``[ Tt]`` accommodates the case-folding done before this regex runs.
_ISO_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[ Tt]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|z|[+-]\d{2}:?\d{2})?"
)
_HEX_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)  # short SHAs and hex IDs
_DECIMAL_RE = re.compile(r"\b\d+(?:\.\d+)?\b")  # bare numbers (latencies, counts)
_WHITESPACE_RE = re.compile(r"\s+")

_SIG_PREFIX_LEN = 80


class _BuzzStoreProto(Protocol):
    def query(
        self,
        *,
        worker_name: str | None = ...,
        action: str | None = ...,
        category: str | None = ...,
        since: float | None = ...,
        until: float | None = ...,
        limit: int = ...,
        offset: int = ...,
    ) -> list[dict[str, Any]]: ...


class _LearningsStoreProto(Protocol):
    def add_learning(
        self,
        *,
        context: str,
        correction: str,
        applied_to: str = ...,
        thread_id: str | None = ...,
    ) -> Any: ...

    def query_learnings(
        self,
        *,
        applied_to: str | None = ...,
        search: str | None = ...,
        limit: int = ...,
    ) -> list[Any]: ...


@dataclass
class _Bucket:
    """One signature bucket accumulated during a sweep."""

    action: str
    sig_key: str
    sample_detail: str
    workers: set[str]
    count: int = 0


def _normalize(detail: str) -> str:
    """Strip task numbers, timestamps, hex/decimals; collapse whitespace.

    The output feeds into the signature key. Two log entries that
    describe the same failure mode but differ only in task number,
    timestamp, or affected worker count must produce the same
    normalized string.
    """
    s = detail.lower()
    s = _ISO_TIMESTAMP_RE.sub("<ts>", s)
    s = _TASK_NUMBER_RE.sub("<task>", s)
    s = _HEX_RE.sub("<hex>", s)
    s = _DECIMAL_RE.sub("<n>", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s


class Dreamer:
    """Periodic pattern-miner over the buzz log.

    Parameters
    ----------
    drone_config:
        Owns ``dreamer_interval_seconds`` (``<= 0`` disables),
        ``dreamer_lookback_hours``, ``dreamer_min_pattern_count``.
    buzz_store:
        Read-only source of buzz log rows. Any object exposing
        ``query(category=..., since=..., limit=...)`` works — the
        watcher is dependency-injected for hermetic tests.
    learnings_store:
        Object exposing ``add_learning(...)`` and
        ``query_learnings(applied_to=...)``. In production this is the
        :class:`QueenChatStore`.
    drone_log:
        :class:`SystemLog` for emitting ``PATTERN_DISCOVERED`` entries
        on each successful write.
    wall_clock:
        Optional ``() -> float`` returning seconds-since-epoch; lets
        tests pin "now" for the lookback window without monkey-patching
        :mod:`time`. Defaults to ``time.time``.
    """

    def __init__(
        self,
        *,
        drone_config: DroneConfig,
        buzz_store: _BuzzStoreProto | None,
        learnings_store: _LearningsStoreProto | None,
        drone_log: SystemLog,
        wall_clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = drone_config
        self._buzz_store = buzz_store
        self._learnings_store = learnings_store
        self._drone_log = drone_log
        self._wall_clock = wall_clock if wall_clock is not None else time.time
        self._last_sweep_monotonic: float = 0.0

    @property
    def interval_seconds(self) -> float:
        return float(getattr(self._config, "dreamer_interval_seconds", 0.0) or 0.0)

    @property
    def lookback_hours(self) -> float:
        return float(getattr(self._config, "dreamer_lookback_hours", 24.0) or 24.0)

    @property
    def min_pattern_count(self) -> int:
        return int(getattr(self._config, "dreamer_min_pattern_count", 3) or 3)

    @property
    def enabled(self) -> bool:
        # Mirror the IdleWatcher / InterWorkerMessageWatcher bootstrap idiom:
        # constructed eagerly so ``pilot.dreamer`` is never None, but stays
        # disabled until the daemon wires both stores in.
        return (
            self.interval_seconds > 0
            and self._buzz_store is not None
            and self._learnings_store is not None
        )

    def due(self, *, now: float | None = None) -> bool:
        """True when enough monotonic time has elapsed since the last sweep."""
        if not self.enabled:
            return False
        now = now if now is not None else time.monotonic()
        return (now - self._last_sweep_monotonic) >= self.interval_seconds

    def signature_key(self, action: str, detail: str) -> str:
        """Stable, dedupe-friendly key for one (action, detail) pair.

        The key is intentionally short (8 hex chars of a SHA-1 over the
        normalized prefix) so it survives in ``applied_to`` without
        bloating the index.
        """
        normalized = _normalize(detail)[:_SIG_PREFIX_LEN]
        digest = hashlib.sha1(f"{action}|{normalized}".encode()).hexdigest()
        return digest[:8]

    async def sweep(self, *, now: float | None = None) -> int:
        """Run one sweep. Returns number of new learnings written.

        Safe to call more often than ``interval_seconds``; no-ops when
        not yet due.
        """
        if not self.enabled:
            return 0
        now_mono = now if now is not None else time.monotonic()
        if (now_mono - self._last_sweep_monotonic) < self.interval_seconds:
            return 0
        self._last_sweep_monotonic = now_mono

        wall_now = self._wall_clock()
        since = wall_now - (self.lookback_hours * 3600.0)

        rows = self._fetch_recent_rows(since=since)
        buckets = self._bucket_rows(rows)
        promoted = [b for b in buckets if self._is_promotable(b)]

        written = 0
        for bucket in promoted:
            tag = f"discovered_by_dreamer:{bucket.action}:{bucket.sig_key}"
            if self._is_recently_seen(tag, wall_now=wall_now):
                continue
            try:
                self._learnings_store.add_learning(
                    context=self._render_context(bucket),
                    correction=self._render_correction(bucket),
                    applied_to=tag,
                )
            except Exception:
                _log.warning("dreamer: add_learning failed for %s", tag, exc_info=True)
                continue
            self._drone_log.add(
                SystemAction.PATTERN_DISCOVERED,
                "swarm",
                f"{bucket.action} ×{bucket.count} across "
                f"{len(bucket.workers)} workers — {bucket.sample_detail[:120]}",
                category=LogCategory.DRONE,
            )
            written += 1
        return written

    def _fetch_recent_rows(self, *, since: float) -> list[dict[str, Any]]:
        try:
            return self._buzz_store.query(since=since, limit=2000)
        except Exception:
            _log.warning("dreamer: buzz_store.query failed", exc_info=True)
            return []

    def _bucket_rows(self, rows: list[dict[str, Any]]) -> list[_Bucket]:
        buckets: dict[tuple[str, str], _Bucket] = {}
        for row in rows:
            action = row.get("action") or ""
            if action not in _PATTERN_ACTIONS:
                continue
            detail = row.get("detail") or ""
            worker = row.get("worker_name") or ""
            sig = self.signature_key(action, detail)
            key = (action, sig)
            bucket = buckets.get(key)
            if bucket is None:
                buckets[key] = _Bucket(
                    action=action,
                    sig_key=sig,
                    sample_detail=detail,
                    workers={worker} if worker else set(),
                    count=1,
                )
            else:
                bucket.count += 1
                if worker:
                    bucket.workers.add(worker)
        return list(buckets.values())

    def _is_promotable(self, bucket: _Bucket) -> bool:
        return bucket.count >= self.min_pattern_count and len(bucket.workers) >= 2

    def _is_recently_seen(self, tag: str, *, wall_now: float) -> bool:
        """True when the same dreamer tag was minted within the refresh window.

        The check tolerates store failures by returning False (i.e.
        "go ahead and write"), reasoning that an extra duplicate is a
        smaller harm than silently dropping new patterns.
        """
        try:
            existing = self._learnings_store.query_learnings(applied_to=tag, limit=1)
        except Exception:
            _log.warning(
                "dreamer: query_learnings failed for %s — proceeding without dedupe",
                tag,
                exc_info=True,
            )
            return False
        if not existing:
            return False
        latest = existing[0]
        created = float(getattr(latest, "created_at", 0.0) or 0.0)
        return (wall_now - created) < _REFRESH_AFTER_SECONDS

    def _render_context(self, bucket: _Bucket) -> str:
        sample = bucket.sample_detail.strip()
        if len(sample) > 200:
            sample = sample[:200] + "…"
        return (
            f"Recurring {bucket.action.lower().replace('_', ' ')} "
            f"observed {bucket.count}× across {len(bucket.workers)} "
            f"workers in the last {int(self.lookback_hours)}h. "
            f"Sample: {sample}"
        )

    def _render_correction(self, bucket: _Bucket) -> str:
        workers = ", ".join(sorted(bucket.workers))
        return (
            f"Pattern auto-discovered by the dreamer drone. "
            f"Affected workers: {workers}. "
            f"Investigate root cause; consider an explicit operator learning "
            f"to override or refine this if the pattern is expected."
        )
