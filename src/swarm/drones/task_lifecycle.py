"""TaskLifecycle — task completion checking and auto-assignment."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from swarm.drones.log import DroneAction, LogCategory, SystemAction
from swarm.drones.nudge_guard import operator_engaged
from swarm.logging import get_logger
from swarm.pty.process import ProcessError
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.config import DroneConfig
    from swarm.drones.log import DroneLog
    from swarm.queen.queen import Queen
    from swarm.tasks.board import TaskBoard
    from swarm.tasks.task import SwarmTask

_log = get_logger("drones.task_lifecycle")


# Tokens too generic to count as project signals. Worker names that ARE
# generic (e.g. "admin", "hub") still appear via worker.name itself; this
# set only filters tokens derived from path basenames during splitting.
_GENERIC_TOKENS: frozenset[str] = frozenset(
    {
        "src",
        "code",
        "lib",
        "main",
        "test",
        "tests",
        "docs",
        "scripts",
        "personal",
        "projects",
        "rcg",
        "core",
    }
)

_PATH_SPLIT_RE = re.compile(r"[-_/.]+")


@dataclass
class AffinityMatch:
    """Result of scoring a task against one worker's project signals."""

    worker_name: str
    score: float  # 0.0 - 1.0
    matched_token: str


class TaskLifecycle:
    """Handles task completion checks and auto-assignment via Queen.

    Extracted from :class:`~swarm.drones.pilot.DronePilot` to reduce
    pilot.py complexity.
    """

    # Default idle threshold -- overridden by drone_config.auto_complete_min_idle in __init__
    _AUTO_COMPLETE_MIN_IDLE: ClassVar[int] = 45

    # If the Queen initially rejected a completion, wait this long before
    # re-proposing.  Prevents spam while still catching tasks that are truly
    # done after the initial check said "not done".
    _COMPLETION_REPROPOSE_COOLDOWN: ClassVar[int] = 300  # 5 minutes

    # When the Queen returns ``done=False`` with confidence >= threshold, the
    # worker demonstrably hasn't finished and re-polling in 5 minutes on
    # unchanged state is pure waste.  Extend the per-task cooldown to this
    # value instead.  Reset happens naturally when ``done`` flips or confidence
    # drops below the threshold on a later analysis.  See
    # ``docs/specs/headless-queen-architecture.md`` (Task A) for context.
    _HIGH_CONF_NOT_DONE_BACKOFF: ClassVar[int] = 1800  # 30 minutes
    _HIGH_CONFIDENCE_THRESHOLD: ClassVar[float] = 0.8

    # Interval (in ticks) between stale proposed-completion cleanup sweeps
    _PROPOSED_COMPLETION_CLEANUP_INTERVAL: ClassVar[int] = 60
    # Max age (seconds) for proposed-completion entries before eviction
    _PROPOSED_COMPLETION_MAX_AGE: ClassVar[float] = 3600.0
    _PROPOSED_COMPLETION_MAX_SIZE: ClassVar[int] = 500

    def __init__(
        self,
        workers: list[Worker],
        log: DroneLog,
        task_board: TaskBoard | None,
        queen: Queen | None,
        drone_config: DroneConfig,
        proposed_completions: dict[str, float],
        idle_consecutive: dict[str, int],
        emit: Callable[..., None],
        build_context: Callable[..., str],
        pending_proposals_check: Callable[[], bool] | None,
        pending_proposals_for_worker: Callable[[str], bool] | None,
        worker_busy_check: Callable[[Worker], bool] | None = None,
    ) -> None:
        self.workers = workers
        self.log = log
        self.task_board = task_board
        self.queen = queen
        self.drone_config = drone_config
        self._proposed_completions = proposed_completions
        self._idle_consecutive = idle_consecutive
        self._emit = emit
        self._build_context = build_context
        self._pending_proposals_check = pending_proposals_check
        self._pending_proposals_for_worker = pending_proposals_for_worker
        # 2026-06-11 false-idle bug: same guards as the idle-watcher so a
        # PROPOSED_COMPLETION isn't raised against an operator-engaged or
        # genuinely-busy worker that merely READS RESTING. None disables.
        self._worker_busy_check = worker_busy_check
        self._auto_complete_min_idle = drone_config.auto_complete_min_idle
        self._needs_assign_check: bool = False
        self._saw_completion: bool = False
        # task_id -> (verdict_ts, done, confidence) — latest Queen verdict
        # per task.  Populated via ``record_completion_verdict`` from the
        # QueenAnalyzer.  Used to extend the re-propose cooldown when the
        # Queen is confidently sure the worker isn't done.
        self._completion_verdicts: dict[str, tuple[float, bool, float]] = {}

    def mark_completion_seen(self) -> None:
        """Signal that a task completion occurred during this pilot session."""
        self._saw_completion = True

    @property
    def saw_completion(self) -> bool:
        """Whether a task completion occurred during this pilot session."""
        return self._saw_completion

    @property
    def needs_assign_check(self) -> bool:
        """Whether an assign check is needed."""
        return self._needs_assign_check

    @needs_assign_check.setter
    def needs_assign_check(self, value: bool) -> None:
        self._needs_assign_check = value

    def set_auto_complete_idle(self, seconds: float) -> None:
        """Override the minimum idle time before proposing task completion."""
        self._auto_complete_min_idle = seconds

    def clear_proposed_completion(self, task_id: str) -> None:
        """Remove a task from the proposed-completions tracker.

        Called by the daemon when a completion proposal is rejected or the
        task is unassigned, allowing the pilot to re-propose later.
        """
        self._proposed_completions.pop(task_id, None)
        self._completion_verdicts.pop(task_id, None)

    def record_completion_verdict(self, task_id: str, done: bool, confidence: float) -> None:
        """Record the latest Queen verdict for a completion analysis.

        When Queen returns ``done=False`` with ``confidence >= 0.8``, the
        re-propose cooldown extends to ``_HIGH_CONF_NOT_DONE_BACKOFF`` so we
        don't re-ask the same question on unchanged state every 5 minutes.
        A ``done=True`` verdict clears the entry so the completion can
        proceed through the proposal path.
        """
        if done:
            self._completion_verdicts.pop(task_id, None)
            return
        self._completion_verdicts[task_id] = (time.time(), done, confidence)

    def _should_eager_assign(self) -> bool:
        """Check if idle-escalation or event-driven flag should trigger assign."""
        if self._needs_assign_check:
            return True
        threshold = self.drone_config.idle_assign_threshold
        if not self.task_board or not self.task_board.available_tasks:
            return False
        return any(v >= threshold for v in self._idle_consecutive.values())

    @staticmethod
    def _worker_identity_tokens(worker: Worker) -> set[str]:
        """Distinctive lowercased tokens that identify this worker's project.

        Sources:
        - ``worker.name`` (e.g. ``budgetbug``, ``platform``).
        - The basename of ``worker.path`` and its kebab/underscore segments
          (e.g. ``rcg-platform`` → {``rcg-platform``, ``platform``}).
        Generic dir names (``src``, ``rcg``, ``projects``, …) are filtered.
        """
        tokens: set[str] = set()
        if worker.name:
            tokens.add(worker.name.lower())
        if worker.path:
            basename = Path(worker.path).name.lower()
            if basename and basename not in _GENERIC_TOKENS:
                tokens.add(basename)
                for part in _PATH_SPLIT_RE.split(basename):
                    if len(part) >= 4 and part not in _GENERIC_TOKENS:
                        tokens.add(part)
        tokens.discard("")
        return tokens

    @staticmethod
    def _affinity_specificity(token: str) -> float:
        """Score 0.0-1.0 for how specific a matched token is.

        Calibrated against the default floor of 0.5: short single-word
        tokens (≤5 chars) score below the floor because words like
        ``admin``, ``hub``, ``api`` are too generic to safely auto-route
        a task that happens to mention them. Kebab/underscore tokens
        (e.g. ``rcg-admin``) and ≥7-char single words clear the floor
        because they're distinctive enough to anchor a project signal.
        """
        n = len(token)
        if "-" in token or "_" in token:
            return 1.0
        if n >= 9:
            return 1.0
        if n >= 7:
            return 0.85
        if n >= 6:
            return 0.55
        return 0.3

    def _score_affinity(self, task: SwarmTask, worker: Worker) -> AffinityMatch:
        """Project-affinity score for ``task`` against ``worker``.

        Returns the best matching token from the worker's identity set
        weighted by its specificity. ``score=0.0`` with empty token
        means no distinctive worker token appears in the task title or
        description.
        """
        tokens = self._worker_identity_tokens(worker)
        if not tokens:
            return AffinityMatch(worker_name=worker.name, score=0.0, matched_token="")
        haystack = f"{task.title} {task.description}".lower()
        best_token = ""
        best_score = 0.0
        for tok in tokens:
            if tok in haystack:
                spec = self._affinity_specificity(tok)
                if spec > best_score:
                    best_score = spec
                    best_token = tok
        return AffinityMatch(worker_name=worker.name, score=best_score, matched_token=best_token)

    def _rank_affinity(self, task: SwarmTask, idle_workers: list[Worker]) -> list[AffinityMatch]:
        """Affinity matches for *task*, sorted high-to-low by score."""
        matches = [self._score_affinity(task, w) for w in idle_workers]
        matches.sort(key=lambda m: m.score, reverse=True)
        return matches

    def _operator_engaged_worker(
        self, idle_workers: list[Worker], window_seconds: float
    ) -> Worker | None:
        """Return the idle worker whose PTY the operator most recently typed in.

        Limited to ``window_seconds``; ties resolved by most-recent input.
        Returns None if no idle worker has had operator input in the window
        or if the window is disabled (<= 0).
        """
        if window_seconds <= 0:
            return None
        best: Worker | None = None
        best_ts = 0.0
        for worker in idle_workers:
            proc = worker.process
            if proc is None:
                continue
            ts = getattr(proc, "last_user_input_at", 0.0)
            if ts <= 0.0:
                continue
            if (time.time() - ts) >= window_seconds:
                continue
            if ts > best_ts:
                best = worker
                best_ts = ts
        return best

    def _cleanup_stale_proposed_completions(self) -> None:
        """Evict proposed-completion entries older than 1 hour to prevent unbounded growth."""
        cutoff = time.time() - self._PROPOSED_COMPLETION_MAX_AGE
        if self._proposed_completions:
            stale = [k for k, ts in self._proposed_completions.items() if ts < cutoff]
            for k in stale:
                del self._proposed_completions[k]
            # Size-based safeguard: keep only the most recent entries
            if len(self._proposed_completions) > self._PROPOSED_COMPLETION_MAX_SIZE:
                sorted_keys = sorted(
                    self._proposed_completions,
                    key=lambda k: self._proposed_completions.get(k, 0.0),
                )
                for k in sorted_keys[: -self._PROPOSED_COMPLETION_MAX_SIZE]:
                    del self._proposed_completions[k]
        if self._completion_verdicts:
            stale_verdicts = [k for k, v in self._completion_verdicts.items() if v[0] < cutoff]
            for k in stale_verdicts:
                del self._completion_verdicts[k]

    def _completion_candidate(self, worker: Worker) -> bool:
        """Whether ``worker`` is eligible for a completion proposal this sweep.

        Gates, in order: must be RESTING, must have been idle at least
        ``auto_complete_min_idle``, and must not trip a false-idle guard —
        operator actively typing in its PTY, or a live PTY still showing a
        mid-turn signal despite the RESTING display_state (2026-06-11
        false-idle bug, parity with the idle-watcher).
        """
        if worker.state != WorkerState.RESTING:
            return False
        if worker.state_duration < self._auto_complete_min_idle:
            return False
        window = float(getattr(self.drone_config, "assign_operator_engagement_minutes", 0.0) or 0.0)
        if operator_engaged(worker, window * 60.0):
            return False
        if self._worker_busy_check is not None:
            try:
                if self._worker_busy_check(worker):
                    return False
            except Exception:
                _log.debug(
                    "task_lifecycle: worker_busy_check raised for %s", worker.name, exc_info=True
                )
        return True

    def _check_task_completions(self) -> bool:
        """Propose completion for tasks whose assigned worker has been idle long enough.

        Instead of auto-completing, emits a ``task_done`` event so the daemon
        can ask the Queen for an assessment and create a user-approvable proposal.

        Uses a timestamp-based cooldown so tasks aren't permanently stuck if
        the Queen initially said "not done".
        """
        if not self.task_board:
            return False

        now = time.time()
        proposed_any = False
        # Snapshot once and bucket by assignee — calling ``tasks_for_worker``
        # inside the worker loop was O(W·T) because each call re-snapshotted
        # the full task dict under the board lock. With ~10 workers and ~100
        # tasks running every poll cycle this dominated the lifecycle drone.
        tasks_by_worker: dict[str, list[SwarmTask]] = {}
        for t in self.task_board.active_tasks:
            if t.assigned_worker:
                tasks_by_worker.setdefault(t.assigned_worker, []).append(t)
        for worker in self.workers:
            if not self._completion_candidate(worker):
                continue
            active_tasks = tasks_by_worker.get(worker.name, [])
            for task in active_tasks:
                # High-confidence "not done" verdict extends the cooldown.
                # Queen was >=80% sure the worker hadn't finished; don't burn
                # an LLM call re-asking on the same state.  Resets when the
                # verdict is cleared (e.g. via ``clear_proposed_completion``)
                # or flips to done=True (see ``record_completion_verdict``).
                verdict = self._completion_verdicts.get(task.id)
                if verdict is not None:
                    verdict_ts, _done, conf = verdict
                    if (
                        conf >= self._HIGH_CONFIDENCE_THRESHOLD
                        and now - verdict_ts < self._HIGH_CONF_NOT_DONE_BACKOFF
                    ):
                        continue
                last_proposed = self._proposed_completions.get(task.id)
                if last_proposed is not None:
                    if now - last_proposed < self._COMPLETION_REPROPOSE_COOLDOWN:
                        continue
                    _log.info(
                        "re-proposing completion for task %s (%s) -- %.0fs since last attempt",
                        task.id,
                        task.title,
                        now - last_proposed,
                    )
                self._proposed_completions[task.id] = now
                self._emit("task_done", worker, task, "")
                self.log.add(
                    DroneAction.PROPOSED_COMPLETION,
                    worker.name,
                    f"task appears done: {task.title}",
                )
                _log.info(
                    "proposing completion for task %s (%s) -- worker %s idle %.0fs",
                    task.id,
                    task.title,
                    worker.name,
                    worker.state_duration,
                )
                proposed_any = True
        return proposed_any

    def _log_backlog_skip(self, task: SwarmTask, reason: str) -> None:
        """Buzz-log a task that the auto-assigner left in backlog (task #341)."""
        self.log.add(
            SystemAction.AUTO_ASSIGN_BACKLOG_SKIPPED,
            "",
            f"backlog: {task.title} — {reason}",
            category=LogCategory.DRONE,
        )
        _log.info("auto-assign backlog: %s — %s", task.title, reason)

    def _route_by_affinity(
        self, task: SwarmTask, idle_workers: list[Worker]
    ) -> tuple[Worker, str, float] | None:
        """Deterministic project-affinity routing for a task (task #341).

        Returns ``(worker, reason, confidence)`` when a deterministic
        signal pins this task to a specific worker, or ``None`` to defer
        to the Queen.  Two signal sources, in order of priority:

        1. **Project affinity** — task title/description names a worker's
           repo or distinctive identity token. Pinning requires the best
           score to clear ``assign_affinity_floor`` AND beat the runner-up
           by at least 0.2 (otherwise ambiguous tokens like ``admin`` could
           auto-route a task that legitimately belongs elsewhere).
        2. **Operator engagement** — operator typed in a worker's PTY
           within ``assign_operator_engagement_minutes``.  Used only when
           project affinity didn't pin (so an explicit "platform: …" task
           still wins over "operator was just typing in budgetbug").
        """
        floor = self.drone_config.assign_affinity_floor
        margin = 0.2

        ranked = self._rank_affinity(task, idle_workers)
        if ranked and ranked[0].score >= floor:
            top = ranked[0]
            runner = ranked[1].score if len(ranked) > 1 else 0.0
            if (top.score - runner) >= margin:
                worker = next(w for w in idle_workers if w.name == top.worker_name)
                reason = (
                    f"project affinity: task names '{top.matched_token}' (score={top.score:.2f})"
                )
                return worker, reason, top.score

        engagement_min = self.drone_config.assign_operator_engagement_minutes
        engaged = self._operator_engaged_worker(idle_workers, engagement_min * 60)
        if engaged is not None:
            # Don't override a strong affinity match for a *different* worker
            # even if engagement points elsewhere — the explicit project
            # signal in the task description must win.
            top_score = ranked[0].score if ranked else 0.0
            top_name = ranked[0].worker_name if ranked else ""
            if top_score >= floor and top_name != engaged.name:
                return None
            reason = f"operator-engaged worker (recent PTY input within {engagement_min:.0f}m)"
            return engaged, reason, max(0.7, top_score)

        return None

    def _idle_workers_for_assignment(self) -> list[Worker]:
        """Compute the list of idle workers eligible for new task assignment.

        Excludes Queen, non-RESTING workers, workers already busy with an
        active task, and workers with a pending proposal.
        """
        if not self.task_board:
            return []
        workers_with_active: set[str] = {
            t.assigned_worker for t in self.task_board.active_tasks if t.assigned_worker
        }
        return [
            w
            for w in self.workers
            if not w.is_queen
            and w.state == WorkerState.RESTING
            and w.name not in workers_with_active
            and not (
                self._pending_proposals_for_worker and self._pending_proposals_for_worker(w.name)
            )
        ]

    def _affinity_route_phase(
        self, available: list[SwarmTask], idle_workers: list[Worker]
    ) -> tuple[list[tuple[Worker, SwarmTask, str, float]], list[SwarmTask], set[str]]:
        """Phase 1 (task #341): deterministic project-affinity routing.

        Returns (deterministic_assignments, queen_eligible_tasks,
        remaining_idle_names).
        """
        deterministic: list[tuple[Worker, SwarmTask, str, float]] = []
        queen_eligible: list[SwarmTask] = []
        remaining_idle_names: set[str] = {w.name for w in idle_workers}
        for task in available:
            workers_for_routing = [w for w in idle_workers if w.name in remaining_idle_names]
            if not workers_for_routing:
                queen_eligible.append(task)
                continue
            decision = self._route_by_affinity(task, workers_for_routing)
            if decision is None:
                queen_eligible.append(task)
                continue
            worker, reason, confidence = decision
            deterministic.append((worker, task, reason, confidence))
            remaining_idle_names.discard(worker.name)
        return deterministic, queen_eligible, remaining_idle_names

    def _emit_deterministic_assignments(
        self, deterministic: list[tuple[Worker, SwarmTask, str, float]]
    ) -> bool:
        """Emit task_assigned events for deterministic affinity matches."""
        if not deterministic:
            return False
        for worker, task, reason, confidence in deterministic:
            _log.info(
                "affinity-route: %s -> %s (%s, conf=%.2f)",
                worker.name,
                task.title,
                reason,
                confidence,
            )
            self.log.add(
                DroneAction.AUTO_ASSIGNED,
                worker.name,
                f"affinity-routed: {task.title} ({reason})",
            )
            self._emit("task_assigned", worker, task, "")
            self._idle_consecutive.pop(worker.name, None)
        return True

    async def _ask_queen_for_assignments(
        self, queen_tasks: list[SwarmTask], idle_for_queen: list[Worker]
    ) -> list[dict[str, Any]] | None:
        """Phase 2: ask Queen for the remaining tasks. None on cancel/error."""
        task_dicts = [
            {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "priority": t.priority.value,
                "task_type": t.task_type.value,
                "tags": t.tags,
                "attachments": t.attachments,
            }
            for t in queen_tasks
        ]
        try:
            hive_ctx = self._build_context()
            assert self.queen is not None  # checked by caller
            return await self.queen.assign_tasks(
                [w.name for w in idle_for_queen],
                task_dicts,
                hive_context=hive_ctx,
            )
        except asyncio.CancelledError:
            _log.info("auto-assign cancelled (shutdown)")
            return None
        except (TimeoutError, RuntimeError, ProcessError, OSError):
            _log.warning("Queen assign_tasks failed", exc_info=True)
            return None

    async def _auto_assign_tasks(self) -> bool:
        """Ask Queen for assignments and emit proposals for user approval.

        Returns ``True`` if any proposals were created.
        """
        if not self.task_board or not self.queen:
            return False
        available = self.task_board.available_tasks
        if not available:
            return False
        idle_workers = self._idle_workers_for_assignment()
        if not idle_workers:
            return False

        _log.info(
            "auto-assign: %d idle workers, %d available tasks", len(idle_workers), len(available)
        )

        floor = self.drone_config.assign_affinity_floor
        auto_approve = self.drone_config.auto_approve_assignments

        deterministic, queen_eligible_tasks, remaining_idle_names = self._affinity_route_phase(
            available, idle_workers
        )
        acted = self._emit_deterministic_assignments(deterministic)

        if not queen_eligible_tasks:
            return acted

        idle_for_queen = [w for w in idle_workers if w.name in remaining_idle_names]
        if not idle_for_queen:
            for task in queen_eligible_tasks:
                self._log_backlog_skip(task, "no idle workers remain after affinity routing")
            return acted

        assignments = await self._ask_queen_for_assignments(queen_eligible_tasks, idle_for_queen)
        if assignments is None:
            return acted

        if self._process_queen_assignments(assignments, idle_for_queen, floor, auto_approve):
            acted = True
        return acted

    def _process_queen_assignments(
        self,
        assignments: list[dict[str, Any]],
        idle_for_queen: list[Worker],
        floor: float,
        auto_approve: bool,
    ) -> bool:
        """Apply Queen-returned assignments under the affinity backstop.

        Pulled out of ``_auto_assign_tasks`` to keep complexity manageable.
        Returns True if any assignment was emitted (auto-approved or
        proposed for operator review).
        """
        min_conf = getattr(self.queen, "min_confidence", 0.7)
        workers_by_name = {w.name: w for w in self.workers}
        acted = False
        for assignment in assignments:
            if not isinstance(assignment, dict):
                _log.warning("Queen returned non-dict assignment entry: %s", type(assignment))
                continue
            worker_name = assignment.get("worker", "")
            task_id = assignment.get("task_id", "")
            message = assignment.get("message", "")
            reasoning = assignment.get("reasoning", "")
            try:
                confidence = float(assignment.get("confidence", 0.8))
            except (ValueError, TypeError):
                confidence = 0.5

            worker = workers_by_name.get(worker_name)
            task = self.task_board.get(task_id) if task_id and self.task_board else None
            if not worker or not task or not task.is_available:
                continue

            if self._affinity_blocks_queen_pick(task, worker, idle_for_queen, floor):
                continue
            if self._confidence_floor_blocks(task, confidence, idle_for_queen, floor):
                continue

            if auto_approve and confidence >= min_conf:
                self._auto_approve_queen_assignment(worker, task, message, confidence)
                acted = True
                continue

            self._propose_queen_assignment(
                worker_name, task_id, task, message, reasoning, confidence
            )
            acted = True
        return acted

    def _affinity_blocks_queen_pick(
        self,
        task: SwarmTask,
        worker: Worker,
        idle_for_queen: list[Worker],
        floor: float,
    ) -> bool:
        """If a different worker has stronger affinity, park in backlog.

        Scores against ALL non-Queen workers (not just the idle pool) so
        that a busy-but-correct worker can still flag a bad pick — the
        right action is to wait, not to mis-route.
        """
        candidates = [w for w in self.workers if not w.is_queen]
        ranked = self._rank_affinity(task, candidates)
        if not ranked:
            return False
        top = ranked[0]
        if top.worker_name != worker.name and top.score >= floor:
            self._log_backlog_skip(
                task,
                f"queen picked {worker.name} but '{top.matched_token}' affinity points to "
                f"{top.worker_name} (score={top.score:.2f})",
            )
            return True
        return False

    def _confidence_floor_blocks(
        self,
        task: SwarmTask,
        queen_confidence: float,
        idle_for_queen: list[Worker],
        floor: float,
    ) -> bool:
        """Park in backlog when neither Queen nor affinity reaches the floor."""
        ranked = self._rank_affinity(task, idle_for_queen)
        top_affinity = ranked[0].score if ranked else 0.0
        if queen_confidence < floor and top_affinity < floor:
            self._log_backlog_skip(
                task,
                f"queen confidence {queen_confidence:.2f} and affinity {top_affinity:.2f} "
                f"both below floor {floor:.2f}",
            )
            return True
        return False

    def _auto_approve_queen_assignment(
        self, worker: Worker, task: SwarmTask, message: str, confidence: float
    ) -> None:
        _log.info(
            "auto-approving assignment: %s -> %s (conf=%.2f)",
            worker.name,
            task.title,
            confidence,
        )
        self.log.add(
            DroneAction.AUTO_ASSIGNED,
            worker.name,
            f"auto-assigned: {task.title} (conf={confidence:.0%})",
        )
        self._emit("task_assigned", worker, task, message)
        self._idle_consecutive.pop(worker.name, None)

    def _propose_queen_assignment(
        self,
        worker_name: str,
        task_id: str,
        task: SwarmTask,
        message: str,
        reasoning: str,
        confidence: float,
    ) -> None:
        from swarm.tasks.proposal import AssignmentProposal

        proposal = AssignmentProposal.assignment(
            worker_name=worker_name,
            task_id=task_id,
            task_title=task.title,
            message=message,
            reasoning=reasoning,
            confidence=confidence,
        )
        _log.info("Queen proposed: %s -> %s (%s)", worker_name, task.title, task_id)
        self.log.add(DroneAction.PROPOSED_ASSIGNMENT, worker_name, f"Queen proposed: {task.title}")
        self._emit("proposal", proposal)
