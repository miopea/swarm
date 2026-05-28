"""System Log — structured action log for drones, queen, tasks, and system events."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from swarm.drones.store import LogStore
from swarm.events import EventEmitter
from swarm.logging import get_logger

if TYPE_CHECKING:
    from swarm.db.buzz_store import BuzzStore

_log = get_logger("drones.log")

_DEFAULT_LOG_PATH = Path.home() / ".swarm" / "system.jsonl"
_DEFAULT_MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
_DEFAULT_MAX_ROTATIONS = 2
_MAX_PENDING_WRITES = 32  # backpressure cap for async log writes


class LogCategory(Enum):
    DRONE = "drone"
    TASK = "task"
    QUEEN = "queen"
    WORKER = "worker"
    SYSTEM = "system"
    OPERATOR = "operator"
    MESSAGE = "message"
    COMPACT = "compact"
    MCP = "mcp"
    VERIFIER = "verifier"


class DroneAction(Enum):
    CONTINUED = "CONTINUED"
    REVIVED = "REVIVED"
    ESCALATED = "ESCALATED"
    OPERATOR = "OPERATOR"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    AUTO_ASSIGNED = "AUTO_ASSIGNED"
    AUTO_NUDGE = "AUTO_NUDGE"
    AUTO_NUDGE_MESSAGE = "AUTO_NUDGE_MESSAGE"
    AUTO_NUDGE_MESSAGE_SKIPPED = "AUTO_NUDGE_MESSAGE_SKIPPED"
    AUTO_HANDOFF_TASK = "AUTO_HANDOFF_TASK"
    PARK_PROPOSED = "PARK_PROPOSED"
    PROPOSED_ASSIGNMENT = "PROPOSED_ASSIGNMENT"
    PROPOSED_COMPLETION = "PROPOSED_COMPLETION"
    PROPOSED_MESSAGE = "PROPOSED_MESSAGE"
    QUEEN_CONTINUED = "QUEEN_CONTINUED"
    QUEEN_PROPOSED_DONE = "QUEEN_PROPOSED_DONE"


class SystemAction(Enum):
    # Drone actions (superset — values MUST match DroneAction; see assertion below)
    CONTINUED = DroneAction.CONTINUED.value
    REVIVED = DroneAction.REVIVED.value
    ESCALATED = DroneAction.ESCALATED.value
    OPERATOR = DroneAction.OPERATOR.value
    APPROVED = DroneAction.APPROVED.value
    REJECTED = DroneAction.REJECTED.value
    AUTO_ASSIGNED = DroneAction.AUTO_ASSIGNED.value
    AUTO_NUDGE = DroneAction.AUTO_NUDGE.value
    AUTO_NUDGE_MESSAGE = DroneAction.AUTO_NUDGE_MESSAGE.value
    AUTO_NUDGE_MESSAGE_SKIPPED = DroneAction.AUTO_NUDGE_MESSAGE_SKIPPED.value
    AUTO_HANDOFF_TASK = DroneAction.AUTO_HANDOFF_TASK.value
    PARK_PROPOSED = DroneAction.PARK_PROPOSED.value
    INBOX_AUTO_RELAY = "INBOX_AUTO_RELAY"
    PROPOSED_ASSIGNMENT = DroneAction.PROPOSED_ASSIGNMENT.value
    PROPOSED_COMPLETION = DroneAction.PROPOSED_COMPLETION.value
    PROPOSED_MESSAGE = DroneAction.PROPOSED_MESSAGE.value
    QUEEN_CONTINUED = DroneAction.QUEEN_CONTINUED.value
    QUEEN_PROPOSED_DONE = DroneAction.QUEEN_PROPOSED_DONE.value
    # Task events
    TASK_CREATED = "TASK_CREATED"
    TASK_PROPOSED = "TASK_PROPOSED"
    TASK_APPROVED = "TASK_APPROVED"
    TASK_ASSIGNED = "TASK_ASSIGNED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_REMOVED = "TASK_REMOVED"
    TASK_SEND_FAILED = "TASK_SEND_FAILED"
    # #405: an auto-repair by the task-lifecycle invariant reconciler
    # (INV-1/2/3 / operator-action). One entry per repaired record.
    TASK_RECONCILED = "TASK_RECONCILED"
    # #406: a worker proactively handed its own ACTIVE task back to
    # ASSIGNED (swarm_park_task) — intentional set-down, not a blocker.
    TASK_PARKED = "TASK_PARKED"
    # Native /goal seeded for a task at dispatch — the provider's own
    # evaluator runs the keep-working loop thereafter.
    GOAL_SET = "GOAL_SET"
    # Task #524: /goal seeding skipped because the dispatch landed on
    # the from-worker of a cross-project task. Seeding the to-worker's
    # criteria on the from-worker pinned the worker into a Stop-hook
    # loop (the from-worker's repo can't satisfy the to-worker's
    # criteria). Logged here so the suppression is auditable.
    GOAL_SKIPPED = "GOAL_SKIPPED"
    # Task #529: a worker-reported blocker was auto-cleared by the
    # IdleWatcher because either the blocker target became done /
    # failed / removed, OR a new inbox message arrived after the
    # blocker was filed. Without this entry, an operator audit can
    # only infer the clear from the absence of subsequent
    # AUTO_NUDGE_SKIPPED entries.
    BLOCKER_AUTO_CLEARED = "BLOCKER_AUTO_CLEARED"
    # Queen events
    QUEEN_PROPOSAL = "QUEEN_PROPOSAL"
    QUEEN_AUTO_ACTED = "QUEEN_AUTO_ACTED"
    QUEEN_BLOCKED = "QUEEN_BLOCKED"
    QUEEN_ESCALATION = "QUEEN_ESCALATION"
    QUEEN_COMPLETION = "QUEEN_COMPLETION"
    QUEEN_PROPOSAL_SKIPPED_FOCUSED = "QUEEN_PROPOSAL_SKIPPED_FOCUSED"
    # Worker events
    WORKER_STUNG = "WORKER_STUNG"
    STATE_TRANSITION = "STATE_TRANSITION"
    AUTO_NUDGE_SKIPPED = "AUTO_NUDGE_SKIPPED"
    # MCP-session events (task #257: client-side tools-dropped recovery)
    MCP_TOOLS_STALE = "MCP_TOOLS_STALE"
    # Oversight events
    OVERSIGHT_SIGNAL = "OVERSIGHT_SIGNAL"
    OVERSIGHT_INTERVENTION = "OVERSIGHT_INTERVENTION"
    OVERSIGHT_INTERVENTION_SKIPPED = "OVERSIGHT_INTERVENTION_SKIPPED"
    OVERSIGHT_RATE_LIMITED = "OVERSIGHT_RATE_LIMITED"
    # Auto-assign events (task #341): emitted when the deterministic
    # affinity gate parks a task in backlog rather than force-fitting it
    # to whichever worker the LLM scored highest.
    AUTO_ASSIGN_BACKLOG_SKIPPED = "AUTO_ASSIGN_BACKLOG_SKIPPED"
    # Resource pressure events
    SUSPENDED = "SUSPENDED"
    RESUMED = "RESUMED"
    # System events
    DRAFT_OK = "DRAFT_OK"
    DRAFT_FAILED = "DRAFT_FAILED"
    CONFIG_CHANGED = "CONFIG_CHANGED"
    SESSION_BOOTSTRAP = "SESSION_BOOTSTRAP"
    # User/operator audit events
    USER_APPROVE = "USER_APPROVE"
    USER_REJECT = "USER_REJECT"
    # Context management events
    COMPACT = "COMPACT"
    # Context-pressure drone events (item 3 of the 10-repo bundle).
    # Categorized under LogCategory.COMPACT — these ARE compact-lifecycle
    # events (the drone triggers /compact based on context-window fill).
    CONTEXT_COMPACT_INJECTED = "CONTEXT_COMPACT_INJECTED"
    CONTEXT_COMPACT_INTERRUPTED = "CONTEXT_COMPACT_INTERRUPTED"
    CONTEXT_COMPACT_DEFERRED = "CONTEXT_COMPACT_DEFERRED"
    # Verifier drone events (item 4 of the 10-repo bundle).
    # Categorized under LogCategory.VERIFIER — its own role, distinct
    # from headless Queen decisions per docs/specs/headless-queen-architecture.md.
    VERIFIER_TIER1_PASSED = "VERIFIER_TIER1_PASSED"
    VERIFIER_TIER1_REOPENED = "VERIFIER_TIER1_REOPENED"
    VERIFIER_TIER2_VERIFIED = "VERIFIER_TIER2_VERIFIED"
    VERIFIER_TIER2_UNCERTAIN = "VERIFIER_TIER2_UNCERTAIN"
    VERIFIER_TIER2_REOPENED = "VERIFIER_TIER2_REOPENED"
    VERIFIER_ESCALATED = "VERIFIER_ESCALATED"
    VERIFIER_SKIPPED = "VERIFIER_SKIPPED"
    # Dreamer drone events: emitted when the periodic pattern-mining sweep
    # turns a recurring failure/oversight cluster into a queen_learnings
    # row tagged ``discovered_by_dreamer:{key}``. One entry per learning
    # written; sweeps that find no patterns log nothing.
    PATTERN_DISCOVERED = "PATTERN_DISCOVERED"
    # Playbook-synthesis-loop events (docs/specs/playbook-synthesis-loop.md).
    # Categorized under LogCategory.DRONE — drone-driven, headless-Queen
    # backed procedural-memory capture. SYNTHESIZED = a playbook was
    # created/folded; SKIPPED = declined, ineligible, or rate-capped.
    PLAYBOOK_SYNTHESIZED = "PLAYBOOK_SYNTHESIZED"
    PLAYBOOK_SKIPPED = "PLAYBOOK_SKIPPED"
    # Phase 2 outcome loop: APPLIED = recalled into a task dispatch;
    # PROMOTED = candidate→active on good winrate; RETIRED = auto-pruned.
    PLAYBOOK_APPLIED = "PLAYBOOK_APPLIED"
    PLAYBOOK_PROMOTED = "PLAYBOOK_PROMOTED"
    PLAYBOOK_RETIRED = "PLAYBOOK_RETIRED"
    # Phase 3: a same-scope near-duplicate pair was merged (loser retired).
    PLAYBOOK_CONSOLIDATED = "PLAYBOOK_CONSOLIDATED"


# Map DroneAction values to SystemAction for interop
_DRONE_TO_SYSTEM: dict[str, SystemAction] = {a.value: SystemAction(a.value) for a in DroneAction}

# Guard: DroneAction must be a strict subset of SystemAction values
assert {a.value for a in DroneAction} <= {a.value for a in SystemAction}, (
    "DroneAction has values not present in SystemAction — keep them in sync"
)


@dataclass
class DroneEntry:
    timestamp: float
    action: DroneAction
    worker_name: str
    detail: str = ""

    @property
    def formatted_time(self) -> str:
        return time.strftime("%I:%M:%S %p", time.localtime(self.timestamp))

    @property
    def display(self) -> str:
        parts = [self.formatted_time, self.action.value, self.worker_name]
        if self.detail:
            parts.append(f"({self.detail})")
        return " ".join(parts)


@dataclass
class SystemEntry:
    timestamp: float
    action: SystemAction
    worker_name: str
    detail: str = ""
    category: LogCategory = field(default=LogCategory.DRONE)
    is_notification: bool = False
    metadata: dict[str, object] = field(default_factory=dict)
    overridden: bool = False
    override_action: str = ""
    store_id: int | None = None  # SQLite row ID for override tracking
    repeat_count: int = 1  # dedup: how many consecutive identical entries this represents

    @property
    def formatted_time(self) -> str:
        return time.strftime("%I:%M:%S %p", time.localtime(self.timestamp))

    @property
    def display(self) -> str:
        parts = [self.formatted_time, self.action.value, self.worker_name]
        if self.detail:
            parts.append(f"({self.detail})")
        return " ".join(parts)


def _parse_action(value: str) -> SystemAction:
    """Parse an action string into SystemAction, tolerating old DroneAction values."""
    try:
        return SystemAction(value)
    except ValueError:
        return SystemAction.OPERATOR  # safe fallback


def _parse_category(value: str | None) -> LogCategory:
    """Parse a category string, defaulting to DRONE for legacy entries."""
    if not value:
        return LogCategory.DRONE
    try:
        return LogCategory(value)
    except ValueError:
        return LogCategory.DRONE


class SystemLog(EventEmitter):
    def __init__(
        self,
        max_entries: int = 200,
        log_file: Path | None = None,
        max_file_size: int = _DEFAULT_MAX_FILE_SIZE,
        max_rotations: int = _DEFAULT_MAX_ROTATIONS,
        db_path: Path | None = None,
        buzz_store: BuzzStore | None = None,
    ) -> None:
        self.__init_emitter__()
        self._entries: list[SystemEntry] = []
        self._max = max_entries
        self._log_file = log_file
        self._max_file_size = max_file_size
        self._max_rotations = max_rotations
        self._write_semaphore = asyncio.Semaphore(_MAX_PENDING_WRITES)

        # Unified SQLite store (Phase 3) — when set, JSONL and legacy store are skipped
        self._buzz_store: BuzzStore | None = buzz_store

        # Legacy SQLite store for queryable analytics (None = disabled)
        self._store: LogStore | None = None
        if buzz_store is None and db_path is not None:
            self._store = LogStore(db_path=db_path)

        if buzz_store is not None:
            self._load_from_buzz_store()
        elif self._log_file:
            self._load_history()

    def _load_from_buzz_store(self) -> None:
        """Load recent entries from the unified buzz_log table."""
        if not self._buzz_store:
            return
        rows = self._buzz_store.load_recent(self._max)
        for d in rows:
            entry = SystemEntry(
                timestamp=d["timestamp"],
                action=_parse_action(d["action"]),
                worker_name=d["worker_name"],
                detail=d.get("detail", ""),
                category=_parse_category(d.get("category")),
                is_notification=d.get("is_notification", False),
                metadata=d.get("metadata", {}),
                store_id=d.get("id"),
                repeat_count=d.get("repeat_count", 1),
            )
            self._entries.append(entry)
        _log.info("loaded %d buzz log entries from swarm.db", len(self._entries))

    def _load_history(self) -> None:
        """Load last N entries from JSONL file on startup.

        Performs one-time migration: if the configured log file doesn't exist
        but the legacy drone.jsonl does, load from the legacy file instead.
        """
        load_path = self._log_file
        if load_path and not load_path.exists():
            # One-time migration from legacy drone.jsonl
            legacy = load_path.parent / "drone.jsonl"
            if legacy.exists():
                load_path = legacy
                _log.info("migrating legacy drone.jsonl → %s", self._log_file)

        if not load_path or not load_path.exists():
            return
        try:
            lines = load_path.read_text().strip().splitlines()
            for line in lines[-self._max :]:
                try:
                    d = json.loads(line)
                    entry = SystemEntry(
                        timestamp=d["timestamp"],
                        action=_parse_action(d["action"]),
                        worker_name=d["worker_name"],
                        detail=d.get("detail", ""),
                        category=_parse_category(d.get("category")),
                        is_notification=d.get("is_notification", False),
                        metadata=d.get("metadata", {}),
                    )
                    self._entries.append(entry)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
            _log.info(
                "loaded %d system log entries from %s",
                len(self._entries),
                load_path,
            )
        except OSError:
            _log.warning("failed to load system log from %s", load_path, exc_info=True)

    def _append_to_file(self, entry: SystemEntry) -> None:
        """Append a single entry to the JSONL log file.

        Offloads the blocking file I/O to a thread when an event loop is
        running, keeping the main async loop unblocked.  A bounded semaphore
        caps the number of in-flight write tasks to prevent unbounded growth
        when disk I/O is slow.
        """
        if not self._log_file:
            return
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._write_entry_bounded(entry))
            task.add_done_callback(lambda t: t.result() if not t.cancelled() else None)
        except RuntimeError:
            # No event loop — write synchronously (startup / tests)
            self._write_entry(entry)

    async def _write_entry_bounded(self, entry: SystemEntry) -> None:
        """Write with backpressure — at most _MAX_PENDING_WRITES concurrent."""
        if not self._write_semaphore.locked():
            async with self._write_semaphore:
                await asyncio.to_thread(self._write_entry, entry)
        else:
            # Semaphore full — try to acquire, but drop entry on contention
            acquired = False
            try:
                await asyncio.wait_for(self._write_semaphore.acquire(), timeout=2.0)
                acquired = True
                await asyncio.to_thread(self._write_entry, entry)
            except TimeoutError:
                _log.warning("log write backpressure — dropping entry: %s", entry.action)
            finally:
                if acquired:
                    self._write_semaphore.release()

    def _write_entry(self, entry: SystemEntry) -> None:
        """Synchronously write a log entry to the JSONL file."""
        import fcntl

        if not self._log_file:
            return
        try:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            record: dict[str, object] = {
                "timestamp": entry.timestamp,
                "action": entry.action.value,
                "worker_name": entry.worker_name,
                "detail": entry.detail,
                "category": entry.category.value,
                "is_notification": entry.is_notification,
            }
            if entry.metadata:
                record["metadata"] = entry.metadata
            line = json.dumps(record)
            # File lock serializes writes + rotation across threads
            with open(self._log_file, "a") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.write(line + "\n")
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            self._rotate_if_needed()
        except OSError:
            _log.warning("failed to append to system log %s", self._log_file, exc_info=True)

    def _rotate_if_needed(self) -> None:
        """Rotate log file if it exceeds max size.

        Uses file locking to prevent races when multiple threads rotate
        concurrently.
        """
        import fcntl

        if not self._log_file or not self._log_file.exists():
            return
        try:
            if self._log_file.stat().st_size <= self._max_file_size:
                return
            # Acquire exclusive lock for the rotation operation
            with open(self._log_file, "a") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    # Re-check size under lock (another thread may have rotated)
                    if self._log_file.stat().st_size <= self._max_file_size:
                        return
                    # Delete oldest rotation
                    oldest = self._log_file.with_suffix(f".jsonl.{self._max_rotations}")
                    if oldest.exists():
                        oldest.unlink()
                    # Shift existing rotations up by one
                    for i in range(self._max_rotations - 1, 0, -1):
                        src = self._log_file.with_suffix(f".jsonl.{i}")
                        dst = self._log_file.with_suffix(f".jsonl.{i + 1}")
                        if src.exists():
                            src.rename(dst)
                    # Rotate current file to .1
                    if self._log_file.exists():
                        self._log_file.rename(self._log_file.with_suffix(".jsonl.1"))
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            _log.info("rotated system log %s", self._log_file)
        except OSError:
            _log.warning("failed to rotate system log", exc_info=True)

    def add(
        self,
        action: DroneAction | SystemAction,
        worker_name: str,
        detail: str = "",
        *,
        category: LogCategory | None = None,
        is_notification: bool = False,
        metadata: dict[str, object] | None = None,
    ) -> SystemEntry:
        # Convert DroneAction to SystemAction
        if isinstance(action, DroneAction):
            sys_action = _DRONE_TO_SYSTEM[action.value]
            resolved_category = category or LogCategory.DRONE
        else:
            sys_action = action
            resolved_category = category or LogCategory.SYSTEM

        # Dedup: if last entry is identical (same action+worker+detail), bump count
        if self._entries:
            last = self._entries[-1]
            if (
                last.action == sys_action
                and last.worker_name == worker_name
                and last.detail == detail
                and last.category == resolved_category
            ):
                last.repeat_count += 1
                last.timestamp = time.time()  # update to latest occurrence
                self.emit("entry", last)
                return last

        entry = SystemEntry(
            timestamp=time.time(),
            action=sys_action,
            worker_name=worker_name,
            detail=detail,
            category=resolved_category,
            is_notification=is_notification,
            metadata=metadata or {},
        )
        self._entries.append(entry)
        if len(self._entries) > self._max:
            self._entries = self._entries[-self._max :]
        # Persist to unified buzz_log (Phase 3) or legacy JSONL + SQLite
        if self._buzz_store is not None:
            entry.store_id = self._buzz_store.insert(
                timestamp=entry.timestamp,
                action=entry.action.value,
                worker_name=entry.worker_name,
                detail=entry.detail,
                category=entry.category.value,
                is_notification=entry.is_notification,
                metadata=entry.metadata if entry.metadata else None,
            )
        else:
            self._append_to_file(entry)
            if self._store is not None:
                entry.store_id = self._store.insert(
                    timestamp=entry.timestamp,
                    action=entry.action.value,
                    worker_name=entry.worker_name,
                    detail=entry.detail,
                    category=entry.category.value,
                    is_notification=entry.is_notification,
                    metadata=entry.metadata if entry.metadata else None,
                )

        self.emit("entry", entry)
        return entry

    def on_entry(self, callback: Callable[[SystemEntry], None]) -> None:
        self.on("entry", callback)

    def close(self) -> None:
        """Close the underlying SQLite store (if any)."""
        if self._store is not None:
            self._store.close()

    def clear(self) -> None:
        """Clear all entries from memory and truncate the log file."""
        self._entries.clear()
        if self._log_file and self._log_file.exists():
            self._log_file.write_text("")
        self.emit("clear")

    def clear_since(self, since: float) -> int:
        """Remove all entries with timestamp >= *since*. Returns count removed."""
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.timestamp < since]
        removed = before - len(self._entries)
        if removed:
            self.emit("clear")
        return removed

    @property
    def entries(self) -> list[SystemEntry]:
        return list(self._entries)

    @property
    def drone_entries(self) -> list[SystemEntry]:
        """Return only drone-category entries."""
        return [e for e in self._entries if e.category == LogCategory.DRONE]

    @property
    def notification_entries(self) -> list[SystemEntry]:
        """Return only notification-worthy entries."""
        return [e for e in self._entries if e.is_notification]

    @property
    def last(self) -> SystemEntry | None:
        return self._entries[-1] if self._entries else None

    # -- Override tracking (Phase 1/2 foundation) --

    def mark_overridden(self, entry: SystemEntry, override_action: str) -> bool:
        """Mark a log entry as overridden by the user.

        Updates both the in-memory entry and the SQLite store.
        """
        entry.overridden = True
        entry.override_action = override_action
        if self._store is not None and entry.store_id is not None:
            return self._store.mark_overridden(entry.store_id, override_action)
        return True

    def mark_recent_overridden(
        self,
        worker_name: str,
        override_action: str,
        *,
        within_seconds: float = 300.0,
        action_filter: list[str] | None = None,
    ) -> bool:
        """Mark the most recent matching entry for a worker as overridden.

        Searches both in-memory entries and the SQLite store.
        """
        # Update in-memory entry
        now = time.time()
        for entry in reversed(self._entries):
            if entry.worker_name != worker_name:
                continue
            if entry.overridden:
                continue
            if now - entry.timestamp > within_seconds:
                break
            if action_filter and entry.action.value not in action_filter:
                continue
            entry.overridden = True
            entry.override_action = override_action
            break

        # Update SQLite store
        if self._store is not None:
            row_id = self._store.mark_recent_overridden(
                worker_name,
                override_action,
                within_seconds=within_seconds,
                action_filter=action_filter,
            )
            return row_id is not None
        return True

    # -- Query methods (delegate to SQLite store) --

    def query(
        self,
        *,
        worker_name: str | None = None,
        action: str | None = None,
        category: str | None = None,
        since: float | None = None,
        until: float | None = None,
        overridden: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Query log entries with filters.  Requires SQLite store."""
        if self._buzz_store is not None:
            return self._buzz_store.query(
                worker_name=worker_name,
                action=action,
                category=category,
                since=since,
                until=until,
                limit=limit,
                offset=offset,
            )
        if self._store is None:
            return []
        return self._store.query(
            worker_name=worker_name,
            action=action,
            category=category,
            since=since,
            until=until,
            overridden=overridden,
            limit=limit,
            offset=offset,
        )

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Free-text search across buzz log entries."""
        if self._buzz_store is not None:
            return self._buzz_store.search(query=query, limit=limit)
        return []

    def query_count(
        self,
        *,
        worker_name: str | None = None,
        action: str | None = None,
        since: float | None = None,
        overridden: bool | None = None,
    ) -> int:
        """Count entries matching filters.  Requires SQLite store."""
        if self._buzz_store is not None:
            return self._buzz_store.count(
                worker_name=worker_name,
                action=action,
                since=since,
            )
        if self._store is None:
            return 0
        return self._store.count(
            worker_name=worker_name,
            action=action,
            since=since,
            overridden=overridden,
        )

    def rule_analytics(self, *, since: float | None = None) -> list[dict]:
        """Aggregate per-rule firing statistics.  Requires SQLite store."""
        if self._buzz_store is not None:
            return self._buzz_store.rule_analytics(since=since)
        if self._store is None:
            return []
        return self._store.rule_analytics(since=since)

    def approval_rate(self, *, since: float | None = None) -> dict[str, int | float | None]:
        """Aggregate auto-approval rate from recent decisions.

        Returns ``{approvals, escalations, rate}`` where ``rate`` is
        ``approvals / (approvals + escalations)`` or ``None`` if no
        relevant entries exist. Counts only ``CONTINUED`` (auto-approved)
        and ``ESCALATED`` (raised to operator) actions — other events
        (task creation, revives, compactions, etc.) are ignored.

        Uses in-memory entries so this works without a SQLite store.
        For long windows that exceed ``max_entries`` (default 200), pair
        with a SQLite-backed ``query_count()`` instead.
        """
        approvals = 0
        escalations = 0
        for entry in self._entries:
            if since is not None and entry.timestamp < since:
                continue
            if entry.action == SystemAction.CONTINUED:
                approvals += entry.repeat_count
            elif entry.action == SystemAction.ESCALATED:
                escalations += entry.repeat_count
        total = approvals + escalations
        rate: float | None = round(approvals / total, 3) if total else None
        return {"approvals": approvals, "escalations": escalations, "rate": rate}

    def prune_store(self, max_age_days: int | None = None) -> int:
        """Prune old entries from the SQLite store."""
        if self._buzz_store is not None:
            return self._buzz_store.prune(max_age_days)
        if self._store is None:
            return 0
        return self._store.prune(max_age_days)

    @property
    def store(self) -> LogStore | None:
        """Access the underlying SQLite store (if configured)."""
        return self._store


# Backward-compat alias
DroneLog = SystemLog
