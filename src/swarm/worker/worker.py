"""Worker dataclass — represents a single Claude Code agent."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, TypedDict

_log = logging.getLogger("swarm.worker")

if TYPE_CHECKING:
    from swarm.pty.process import WorkerProcess


class WorkerDict(TypedDict):
    """Typed shape of Worker.to_api_dict() output."""

    name: str
    path: str
    provider: str
    worker_id: str
    kind: str
    state: str
    state_duration: float
    revive_count: int
    usage: dict[str, object]
    cost_usd: float
    repo_path: str
    worktree_branch: str
    context_pct: float
    recent_tools: list[dict[str, str]]
    cache_ratio: float
    needs_operator_input: bool
    crash_tail: str
    exit_code: int | None


# WAITING within this grace window is likely a transient prompt the
# drones are about to auto-approve — don't surface it as "needs input"
# on the dashboard. Past this, the operator should see a distinct cue.
_NEEDS_INPUT_GRACE_SECONDS = 15.0


# (indicator, css_class, priority) keyed by state value
_STATE_PROPS: dict[str, tuple[str, str, int]] = {
    "BUZZING": (".", "text-leaf", 2),
    "WAITING": ("?", "text-honey", 1),
    "RESTING": ("~", "text-lavender", 4),
    "SLEEPING": ("z", "text-muted", 3),
    "STUNG": ("!", "text-poppy", 0),
}


class WorkerState(Enum):
    BUZZING = "BUZZING"  # Actively working (Claude processing)
    WAITING = "WAITING"  # Actionable prompt (choice/plan/empty) — needs attention
    RESTING = "RESTING"  # Idle, waiting for input
    SLEEPING = "SLEEPING"  # Display-only: RESTING for >= SLEEPING_THRESHOLD
    STUNG = "STUNG"  # Exited / crashed

    @property
    def indicator(self) -> str:
        return _STATE_PROPS[self.value][0]

    @property
    def display(self) -> str:
        return self.value.lower()

    @property
    def css_class(self) -> str:
        """CSS class for dashboard rendering."""
        return _STATE_PROPS[self.value][1]

    @property
    def priority(self) -> int:
        """Sort priority for group worst-state display (lower = more urgent)."""
        return _STATE_PROPS[self.value][2]


# Valid state transitions — unlisted transitions are logged as warnings.
_VALID_TRANSITIONS: dict[WorkerState, set[WorkerState]] = {
    WorkerState.BUZZING: {
        WorkerState.RESTING,
        WorkerState.WAITING,
        WorkerState.STUNG,
    },
    WorkerState.WAITING: {
        WorkerState.BUZZING,
        WorkerState.RESTING,
        WorkerState.STUNG,
    },
    WorkerState.RESTING: {
        WorkerState.BUZZING,
        WorkerState.WAITING,
        WorkerState.SLEEPING,
        WorkerState.STUNG,
    },
    WorkerState.SLEEPING: {
        WorkerState.BUZZING,
        WorkerState.WAITING,
        WorkerState.RESTING,
        WorkerState.STUNG,
    },
    WorkerState.STUNG: {WorkerState.BUZZING},  # revive only
}

# Workers RESTING for longer than this become SLEEPING (display-only).
SLEEPING_THRESHOLD = 1200.0  # 20 minutes

# STUNG workers are auto-removed after this many seconds.
STUNG_REAP_TIMEOUT = 30.0


def format_duration(seconds: float) -> str:
    """Format a duration as a compact human-readable string."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


@dataclass
class TokenUsage:
    """Accumulated token usage for a worker or the queen."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    # Last turn's input_tokens — best proxy for current context window fill.
    last_turn_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: TokenUsage) -> None:
        """Accumulate usage from another TokenUsage."""
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.cost_usd += other.cost_usd

    def to_dict(self) -> dict[str, object]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }


WORKER_KIND_WORKER = "worker"
WORKER_KIND_QUEEN = "queen"

# #1357: what the dashboard publishes for a worker nothing has measured yet.
# Deliberately NOT a WorkerState member — see Worker.published_state for why an
# enum variant would have leaked an unhandled case into INV-2 and every other
# state comparison. Presentation only.
UNCLASSIFIED_STATE = "UNCLASSIFIED"

# The Queen is a singleton per-swarm; her PTY process always uses
# this literal name so both the discover path and the MCP server's
# worker_name gate can recognize her without a config lookup.
QUEEN_WORKER_NAME = "queen"


def infer_worker_kind(name: str) -> str:
    """Infer worker kind from its name.  Used by discover / spawn paths."""
    if name == QUEEN_WORKER_NAME:
        return WORKER_KIND_QUEEN
    return WORKER_KIND_WORKER


@dataclass
class Worker:
    name: str
    path: str
    provider_name: str = "claude"
    # "worker" = regular task-executing worker (default).
    # "queen"  = the swarm's conversational coordinator.  Exempt from
    #            task assignment and SLEEPING; offline = banner, not
    #            task handoff.  See docs/specs/interactive-queen.md §4.1.
    kind: str = WORKER_KIND_WORKER
    process: WorkerProcess | None = field(default=None, repr=False)
    state: WorkerState = WorkerState.BUZZING
    # #1357: has anything actually MEASURED this worker yet?
    #
    # ``state`` above defaults to BUZZING — the most active state — so a freshly
    # started daemon asserted a fully-busy swarm for the 4-6s before the pilot's
    # first poll. The operator's screenshot showed all 16 workers reading
    # "BUZZING — 4m": one identical stale figure, which is the tell that nothing
    # measured any of them. "Everything is working" is the single most misleading
    # thing to assert while you do not know.
    #
    # DISPLAY-ONLY BY DESIGN. This gates what ``to_api_dict`` publishes; it never
    # changes ``state`` or ``display_state``, so INV-2, the reconcilers, the idle
    # watcher and every ``== RESTING`` / ``== BUZZING`` comparison are untouched.
    # That is why this is a bool rather than a WorkerState.UNKNOWN member — the
    # enum variant would put an unhandled case into every one of those comparisons.
    state_known: bool = False
    state_since: float = field(default_factory=time.time)
    revive_count: int = field(default=0, repr=False)
    usage: TokenUsage = field(default_factory=TokenUsage, repr=False)
    repo_path: str = ""  # original repo path (set when using worktree isolation)
    worktree_branch: str = ""  # branch name (e.g. "swarm/api")
    context_pct: float = 0.0  # estimated context window usage (0.0 - 1.0)
    sleeping_threshold: float = field(default=SLEEPING_THRESHOLD, repr=False)
    stung_reap_timeout: float = field(default=STUNG_REAP_TIMEOUT, repr=False)
    # Configurable hysteresis thresholds (set from DroneConfig.state_thresholds)
    buzzing_confirm_count: int = field(default=3, repr=False)
    stung_confirm_count: int = field(default=2, repr=False)
    revive_grace: float = field(default=15.0, repr=False)
    _resting_confirmations: int = field(default=0, repr=False)
    _stung_confirmations: int = field(default=0, repr=False)
    _revive_at: float = field(default=0.0, repr=False)
    # Phase 1: diminishing returns tracking
    _prev_input_tokens: int = field(default=0, repr=False)
    _low_delta_streak: int = field(default=0, repr=False)
    # Phase 1: context compaction tracking
    compacting: bool = field(default=False, repr=False)
    _context_warned: bool = field(default=False, repr=False)
    # Tokens captured at PreCompact so PostCompact can log the delta.
    # Not persisted — this is session-local telemetry only.
    _compact_tokens_before: int = field(default=0, repr=False)
    # Phase 1: context restoration on revive
    last_context_files: list[str] = field(default_factory=list, repr=False)
    # Phase 2: recent tool activity (last 5 tool calls)
    recent_tools: list[dict[str, str]] = field(default_factory=list, repr=False)
    # Phase 3: prompt cache efficiency (0.0 - 1.0, higher = better cache reuse)
    cache_ratio: float = field(default=0.0, repr=False)
    # Phase 3: speculative task preparation
    speculating_task_id: str | None = field(default=None, repr=False)
    # Phase 3: tiered context recovery
    recovery_attempts: int = field(default=0, repr=False)
    _api_dict_cache: WorkerDict | None = field(default=None, repr=False)
    _api_dict_cache_time: float = field(default=0.0, repr=False)

    def _apply_state_transition(
        self, new_state: WorkerState, *, clear_revive_window: bool = False
    ) -> None:
        """Commit a confirmed state change. Shared by update_state (debounced)
        and force_state (immediate). ``clear_revive_window`` also resets the
        STUNG/revive-grace fields — force_state's extra step on confirmed death.
        """
        # Reset revive count when worker starts working successfully
        if new_state == WorkerState.BUZZING and self.state != WorkerState.BUZZING:
            self.revive_count = 0
        self.state = new_state
        # #1357: the single chokepoint where a CONFIRMED classification lands, so
        # it is the one place that can honestly say the worker has been measured.
        # Both callers (debounced update_state and immediate force_state) route
        # through here, which is why the flag does not need setting at either.
        self.state_known = True
        self.state_since = time.time()
        self._resting_confirmations = 0
        self._api_dict_cache = None
        if clear_revive_window:
            self._stung_confirmations = 0
            self._revive_at = 0.0

    def update_state(self, new_state: WorkerState) -> bool:
        """Update state, return True if state changed.

        Applies hysteresis: requires 3 consecutive RESTING readings before
        accepting BUZZING→RESTING (prevents flicker).  BUZZING→WAITING
        transitions immediately (1 confirmation) because prompt detection
        is a strong signal that doesn't false-positive.

        After a revive, ignores STUNG readings for ``_REVIVE_GRACE`` seconds
        so Claude has time to start before the poll loop re-marks the worker.
        """
        # Grace period: ignore STUNG right after a revive
        if (
            new_state == WorkerState.STUNG
            and self._revive_at > 0
            and time.time() - self._revive_at < self.revive_grace
        ):
            return False

        # STUNG hysteresis: require N consecutive STUNG readings to prevent
        # spurious revives when Claude Code briefly exits between operations
        # (shell becomes foreground for one poll cycle).
        if new_state == WorkerState.STUNG:
            self._stung_confirmations += 1
            if self._stung_confirmations < self.stung_confirm_count:
                return False
        else:
            self._stung_confirmations = 0

        _idle_states = (WorkerState.RESTING, WorkerState.WAITING)
        if new_state in _idle_states and self.state == WorkerState.BUZZING:
            self._resting_confirmations += 1
            # WAITING (prompt detected) is a strong signal — no flicker risk.
            # RESTING needs buzzing_confirm_count confirmations to prevent
            # BUZZING↔RESTING flicker.
            needed = 1 if new_state == WorkerState.WAITING else self.buzzing_confirm_count
            if self._resting_confirmations < needed:
                return False
        # Preserve resting confirmations on idle→idle transitions (RESTING↔WAITING)
        # so the counter isn't reset by flicker between idle states
        if new_state not in _idle_states:
            self._resting_confirmations = 0
        if self.state != new_state:
            # Validate transition
            valid = _VALID_TRANSITIONS.get(self.state, set())
            if new_state not in valid:
                _log.warning(
                    "%s: invalid transition %s → %s",
                    self.name,
                    self.state.value,
                    new_state.value,
                )
            self._apply_state_transition(new_state)
            return True
        return False

    def force_state(self, new_state: WorkerState) -> None:
        """Set state directly, bypassing hysteresis and grace period.

        Used when the holder confirms a process death — no debounce needed.
        Clears the revive grace window so STUNG detection isn't suppressed.
        """
        if self.state != new_state:
            self._apply_state_transition(new_state, clear_revive_window=True)

    def record_revive(self) -> None:
        """Record a revive attempt."""
        self.revive_count += 1
        self._revive_at = time.time()

    @property
    def resting_duration(self) -> float:
        if self.state in (WorkerState.RESTING, WorkerState.WAITING):
            return time.time() - self.state_since
        return 0.0

    @property
    def state_duration(self) -> float:
        """How long the worker has been in its current state."""
        return time.time() - self.state_since

    @property
    def is_queen(self) -> bool:
        """True if this is the swarm's coordinator Queen (not a task worker)."""
        return self.kind == WORKER_KIND_QUEEN

    @property
    def published_state(self) -> str:
        """What the DASHBOARD is allowed to claim about this worker (#1357).

        ``UNCLASSIFIED`` until something has actually measured the worker — either
        the pilot classifying its PTY output, or a remembered state restored from a
        previous run (an older measurement is still a measurement, and suppressing
        it would make the persistence in ``db/worker_state_store.py`` pointless).

        Deliberately a STRING, not a ``WorkerState``. Every consumer that makes a
        DECISION reads ``state`` / ``display_state`` and keeps seeing a real enum
        member; only the presentation layer sees this. Adding an enum variant
        instead would have put an unhandled case into INV-2's demotion and every
        other state comparison in the codebase.
        """
        return self.display_state.value if self.state_known else UNCLASSIFIED_STATE

    @property
    def display_state(self) -> WorkerState:
        """State for display purposes: RESTING becomes SLEEPING after threshold.

        The Queen is never displayed as SLEEPING — she's always-on by design.
        """
        if self.is_queen:
            return self.state
        if self.state == WorkerState.RESTING and self.state_duration >= self.sleeping_threshold:
            return WorkerState.SLEEPING
        return self.state

    _API_DICT_TTL = 1.0  # seconds

    @property
    def needs_operator_input(self) -> bool:
        """True when this worker is in WAITING and has been waiting long
        enough that drones haven't auto-resolved the prompt — the
        dashboard uses this to show a distinct "needs you" pill on the
        tile (separate from the plain WAITING state colour).

        Other states (BUZZING, RESTING, SLEEPING, STUNG) never surface
        here — STUNG has its own revive affordance, RESTING is idle by
        definition, and BUZZING is active work.
        """
        if self.state != WorkerState.WAITING:
            return False
        return self.state_duration >= _NEEDS_INPUT_GRACE_SECONDS

    _CRASH_TAIL_LINES = 5

    def _crash_diagnostics(self) -> tuple[str, int | None]:
        """Return (PTY tail, exit code) for a STUNG worker.

        The ring buffer outlives the dead process, so the tail shows the
        operator *why* the worker died instead of a bare "Crashed or
        exited" — repeated revive-crash loops were undiagnosable from
        the dashboard without it.
        """
        if self.state != WorkerState.STUNG or self.process is None:
            return "", None
        try:
            content = self.process.get_content(self._CRASH_TAIL_LINES * 4)
        except Exception:
            return "", self.process.exit_code
        lines = [ln.rstrip() for ln in content.splitlines() if ln.strip()]
        tail = "\n".join(lines[-self._CRASH_TAIL_LINES :])
        return tail, self.process.exit_code

    def to_api_dict(self) -> WorkerDict:
        """Serialize worker state for API/WebSocket responses.

        Returns a cached result if less than 1 second old to reduce
        serialization overhead during high-frequency WS broadcasts.
        """
        now = time.time()
        cached = self._api_dict_cache
        if cached is not None and (now - self._api_dict_cache_time) < self._API_DICT_TTL:
            return cached
        crash_tail, exit_code = self._crash_diagnostics()
        result = WorkerDict(
            name=self.name,
            path=self.path,
            provider=self.provider_name,
            worker_id=self.name,
            kind=self.kind,
            state=self.published_state,
            state_duration=round(self.state_duration, 1),
            revive_count=self.revive_count,
            usage=self.usage.to_dict(),
            cost_usd=round(self.usage.cost_usd, 4),
            repo_path=self.repo_path,
            worktree_branch=self.worktree_branch,
            context_pct=round(self.context_pct, 3),
            recent_tools=self.recent_tools[-5:],
            cache_ratio=round(self.cache_ratio, 3),
            needs_operator_input=self.needs_operator_input,
            crash_tail=crash_tail,
            exit_code=exit_code,
        )
        self._api_dict_cache = result
        self._api_dict_cache_time = now
        return result


def worker_state_counts(workers: list[Worker]) -> dict[str, int]:
    """Count workers by display state (single pass)."""
    counts: dict[str, int] = {
        "total": len(workers),
        "buzzing": 0,
        "waiting": 0,
        "resting": 0,
        "sleeping": 0,
        "stung": 0,
    }
    for w in workers:
        key = w.display_state.value.lower()
        if key in counts:
            counts[key] += 1
    return counts
