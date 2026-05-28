"""Inter-worker message watcher drone — nudge idle recipients of unread messages.

Phase 3 of task #235. Phase 1 of the same ticket made messages to the
Queen auto-relay into her PTY; Phase 2 gave her a message-stream view
for triage. This watcher closes the loop for messages between workers:
when worker A sends to worker B and B is RESTING/SLEEPING, A's message
would otherwise sit in B's inbox until B happens to take a turn. That's
the failure mode the operator saw when cross-project coordination
stalled.

Deliberate boundary: workers MUST NOT be able to auto-interrupt each
other (otherwise one worker going pushy would derail the whole swarm).
The auto-interruption here is a drone/server-side concern — it only
fires when the recipient is demonstrably idle AND the message is still
unread, and every nudge is debounced per recipient so a flurry of
messages still results in at most one nudge per debounce window.

Scope mirrors :class:`swarm.drones.idle_watcher.IdleWatcher`: same
config keys (reused), same rate-limit escape hatch, same per-(worker)
debounce, same fault isolation.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from swarm.drones.log import DroneAction, LogCategory, SystemAction
from swarm.drones.nudge_guard import ESCALATE, SILENT, RepeatNudgeGuard
from swarm.logging import get_logger
from swarm.worker.worker import QUEEN_WORKER_NAME, WorkerState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from swarm.drones.log import DroneLog
    from swarm.messages.store import Message, MessageStore
    from swarm.tasks.board import TaskBoard
    from swarm.worker.worker import Worker


_log = get_logger("drones.inter_worker_watcher")


# States where a worker is "idle" and a nudge is appropriate. BUZZING =
# already working, WAITING = approval prompt (different code path), STUNG
# = process exited (revive is a separate concern).
_IDLE_STATES: frozenset[WorkerState] = frozenset({WorkerState.RESTING, WorkerState.SLEEPING})

# Message types that require action from the recipient. Nudging on
# action-required messages is the whole point of the watcher; nudging on
# informational traffic (FYI broadcasts, routine progress updates,
# side-channel notes) risks derailing a worker who has self-resolved
# the underlying concern already — see task #271 for the wifi-portal
# repro.  Operator messages never reach this path: the operator has
# direct PTY access and doesn't need a drone nudge.
_ACTION_REQUIRED_MSG_TYPES: frozenset[str] = frozenset({"dependency", "warning"})


def _nudge_message(sender: str, unread_count: int) -> str:
    """Build the PTY message sent to an idle recipient.

    Kept short and tool-centric — like the IdleWatcher's nudge, this
    points the worker at its own ``swarm_check_messages`` tool rather
    than treating the nudge as a fresh conversational prompt.
    """
    if unread_count == 1:
        return f"New message from `{sender}`. Run `swarm_check_messages` to read and process."
    return (
        f"{unread_count} new messages (latest from `{sender}`). "
        "Run `swarm_check_messages` to read and process."
    )


class InterWorkerMessageWatcher:
    """Periodic sweep: idle workers with unread messages get a nudge.

    Parameters
    ----------
    drone_config:
        Reuses ``idle_nudge_interval_seconds`` /
        ``idle_nudge_debounce_seconds`` from :class:`DroneConfig` so
        operators don't have to tune a separate knob. ``interval <= 0``
        disables.
    message_store:
        Source of truth for "does this worker have unread messages".
    drone_log:
        Every nudge is appended as ``AUTO_NUDGE_MESSAGE`` under
        ``LogCategory.DRONE``.
    send_to_worker:
        Async callable
        ``(worker_name, message, *, _log_operator=False) -> None``.
        Mirrors :meth:`SwarmDaemon.send_to_worker`.
    rate_limit_check:
        Optional ``(worker_name) -> bool``. Returning True skips the
        nudge — the worker hit the Claude 5hr quota and piling up work
        behind a dead quota is pointless.
    task_board:
        Optional :class:`TaskBoard` used to ask whether the recipient has
        an active task on the board. The actionable-types filter (#271)
        only applies WITH a task — preserving "don't distract a worker
        mid-flight with FYI chatter". Without a task, ANY unread message
        is reason to nudge: the worker is idle anyway and operators
        expect the inbox to get processed (the original complaint that
        motivated this widening). When ``task_board`` is ``None``, the
        watcher conservatively defaults to the with-task narrow filter
        so test setups without a board don't accidentally over-nudge.
    """

    def __init__(
        self,
        *,
        drone_config,
        message_store: MessageStore | None,
        drone_log: DroneLog,
        send_to_worker: Callable[..., Awaitable[None]],
        rate_limit_check: Callable[[str], bool] | None = None,
        task_board: TaskBoard | None = None,
        spawn_handoff_task: Callable[[str, Message], Awaitable[bool]] | None = None,
        escalate_to_operator: Callable[[str, str], None] | None = None,
    ) -> None:
        self._config = drone_config
        self._message_store = message_store
        self._drone_log = drone_log
        self._send_to_worker = send_to_worker
        self._rate_limit_check = rate_limit_check
        self._task_board = task_board
        # Task #546: stop nudging + escalate to operator after
        # idle_nudge_max_repeats consecutive no-progress nudges, instead of
        # re-poking a worker about the same unread inbox forever.
        self._escalate_to_operator = escalate_to_operator
        self._nudge_guard = RepeatNudgeGuard()
        # task #442: callback that turns an actionable cross-worker
        # handoff to an idle, task-less recipient into a *tracked* task
        # assigned to that recipient — so the IdleWatcher then carries
        # it to completion instead of the handoff relying on a single
        # skip-prone nudge. Injected by the daemon (None in minimal
        # setups → falls back to the nudge-only path, unchanged).
        self._spawn_handoff_task = spawn_handoff_task
        # message ids we've already spawned a backing task for, so a
        # still-unread handoff doesn't re-spawn on every sweep before
        # the board reflects the new assignment.
        self._spawned_msg_ids: set[int] = set()
        # worker_name → last-nudge monotonic timestamp
        self._last_nudge: dict[str, float] = {}
        # worker_name → last AUTO_NUDGE_MESSAGE_SKIPPED entry timestamp.
        # Separate from ``_last_nudge`` so an informational-only inbox
        # doesn't block later real nudges — debounce applies to the
        # SKIPPED entry only, and uses the same window so the buzz log
        # doesn't spam operator with repeat "informational only"
        # entries on every sweep (task #271).
        self._last_skip_log: dict[str, float] = {}
        self._last_sweep: float = 0.0

    @property
    def interval_seconds(self) -> float:
        return float(self._config.idle_nudge_interval_seconds or 0.0)

    @property
    def debounce_seconds(self) -> float:
        return float(self._config.idle_nudge_debounce_seconds or 0.0)

    @property
    def _max_repeats(self) -> int:
        """Task #546: consecutive no-progress nudges before escalate-and-quiet.
        Read live from config so hot-reload picks it up; 0 disables the cap."""
        return int(getattr(self._config, "idle_nudge_max_repeats", 0) or 0)

    @property
    def enabled(self) -> bool:
        return self.interval_seconds > 0 and self._message_store is not None

    async def sweep(self, workers: list[Worker], *, now: float | None = None) -> int:
        """Run one sweep. Returns the number of nudges actually sent.

        Safe to call more often than ``interval_seconds``; no-ops until
        the window has elapsed. Caller can force a sweep by passing a
        ``now`` that pushes past the threshold.
        """
        if not self.enabled:
            return 0
        now = now if now is not None else time.monotonic()
        if (now - self._last_sweep) < self.interval_seconds:
            return 0
        self._last_sweep = now

        sent = 0
        for worker in workers:
            if not self._should_nudge(worker, now=now):
                continue
            try:
                # get_unread is read-only (does NOT mark-read); safe to
                # call from the watcher without disturbing the worker's
                # actual swarm_check_messages flow.
                unread = self._message_store.get_unread(worker.name)
            except Exception:
                _log.debug(
                    "inter_worker_watcher: get_unread raised for %s",
                    worker.name,
                    exc_info=True,
                )
                continue
            # Filter out queen-sourced messages — the Queen's own relay
            # path (task #235 Phase 1) already injects those directly
            # into the recipient's PTY via ``queen_prompt_worker``;
            # double-nudging would just spam.
            inter_worker = [m for m in unread if m.sender and m.sender != QUEEN_WORKER_NAME]
            if not inter_worker:
                continue
            # Task #271: narrow the nudge trigger to action-required
            # message types when the worker has an active task — info
            # messages (finding / status / note) shouldn't pull a worker
            # off in-flight work.  When the worker has NO active task on
            # the board, the situation flips: the worker is idle anyway,
            # the operator wants the inbox processed, and ANY unread
            # message is reason enough to nudge.  task_board=None
            # (e.g. minimal test setups) defaults to the conservative
            # with-task path so we don't over-nudge by accident.
            has_task = self._has_active_task(worker.name)
            # task #442: a task-less idle recipient of an action-bearing
            # handoff gets a *tracked* task, not just a nudge. Done first
            # because the spawned assignment flips has_task and the
            # assign-and-start dispatch already prompts the worker, so a
            # nudge this sweep would double up.
            if not has_task and await self._maybe_spawn_handoff(worker.name, inter_worker, now=now):
                sent += 1
                continue
            if has_task:
                actionable = [m for m in inter_worker if m.msg_type in _ACTION_REQUIRED_MSG_TYPES]
            else:
                actionable = inter_worker
            if not actionable:
                # Informational-only and the worker IS on a task: skip +
                # log so the operator has visibility on why the inbox
                # sits unread (prior behaviour would have nudged and
                # potentially derailed the worker).  Debounce the skip
                # entry per worker using the same timestamp the nudge
                # would have used, so we don't spam
                # AUTO_NUDGE_MESSAGE_SKIPPED on every sweep for the same
                # inbox state.
                if not self._is_skip_logged(worker.name, now=now):
                    latest_info = max(inter_worker, key=lambda m: m.created_at)
                    type_summary = ", ".join(sorted({m.msg_type for m in inter_worker}))
                    self._drone_log.add(
                        DroneAction.AUTO_NUDGE_MESSAGE_SKIPPED,
                        worker.name,
                        (
                            f"informational only from {latest_info.sender} "
                            f"({len(inter_worker)} unread: {type_summary}) — "
                            "not nudging"
                        ),
                        category=LogCategory.DRONE,
                    )
                    self._last_skip_log[worker.name] = now
                continue
            latest = max(actionable, key=lambda m: m.created_at)
            if await self._dispatch_or_escalate(
                worker, inter_worker, actionable, latest, has_task, now=now
            ):
                sent += 1
        return sent

    async def _dispatch_or_escalate(
        self,
        worker: Worker,
        inter_worker: list[Message],
        actionable: list[Message],
        latest: Message,
        has_task: bool,
        *,
        now: float,
    ) -> bool:
        """A nudge is due; send it, or escalate + go quiet (task #546).

        Consults the repeat-guard: after ``idle_nudge_max_repeats``
        no-progress nudges (same unread-inbox fingerprint), stop poking
        and escalate to the operator once. The fingerprint is the worker
        state + unread count + newest message id, so a NEW inbound message
        (id climbs) or the inbox draining (count drops) counts as progress
        and resets the streak. Returns True only when a real nudge fired.
        """
        fingerprint = (
            worker.display_state.value,
            len(inter_worker),
            max((m.id or 0 for m in inter_worker), default=0),
        )
        decision = self._nudge_guard.decide(worker.name, fingerprint, max_repeats=self._max_repeats)
        self._last_nudge[worker.name] = now
        if decision == SILENT:
            return False
        if decision == ESCALATE:
            detail = (
                f"unread from {latest.sender} ({len(inter_worker)} msg) across "
                f"{self._max_repeats} nudges with no progress — escalated to operator"
            )
            self._drone_log.add(
                SystemAction.AUTO_NUDGE_ESCALATED,
                worker.name,
                detail,
                category=LogCategory.DRONE,
            )
            if self._escalate_to_operator is not None:
                try:
                    self._escalate_to_operator(worker.name, detail)
                except Exception:
                    _log.debug(
                        "inter_worker_watcher: escalate_to_operator raised for %s",
                        worker.name,
                        exc_info=True,
                    )
            return False
        # NUDGE → normal poke.
        message = _nudge_message(latest.sender, len(inter_worker))
        try:
            await self._send_to_worker(worker.name, message, _log_operator=False)
        except Exception:
            _log.warning(
                "inter_worker_watcher: send_to_worker failed for %s",
                worker.name,
                exc_info=True,
            )
            return False
        # Buzz-log detail is path-aware so audits can tell whether the nudge
        # fired because of an action-required message (with-task path) or
        # because the worker is idle without a task (no-task path).
        path_label = "no-task" if not has_task else "with-task"
        self._drone_log.add(
            DroneAction.AUTO_NUDGE_MESSAGE,
            worker.name,
            (
                f"unread from {latest.sender} "
                f"({len(inter_worker)} total, "
                f"{len(actionable)} actionable: {latest.msg_type}) "
                f"[{path_label}]"
            ),
            category=LogCategory.DRONE,
        )
        return True

    def _should_nudge(self, worker: Worker, *, now: float) -> bool:
        """Cheap filters applied BEFORE we query the message store."""
        if worker.name == QUEEN_WORKER_NAME:
            # The Queen gets her own inbox relay via the Phase 1 path;
            # no need to double-nudge her.
            return False
        if worker.display_state not in _IDLE_STATES:
            return False
        if self._is_debounced(worker.name, now=now):
            return False
        if self._rate_limit_check is not None:
            try:
                if self._rate_limit_check(worker.name):
                    return False
            except Exception:
                _log.debug(
                    "inter_worker_watcher: rate_limit_check raised for %s",
                    worker.name,
                    exc_info=True,
                )
                return False
        return True

    def _has_active_task(self, name: str) -> bool:
        """Return True when ``name`` has an ASSIGNED/IN_PROGRESS task.

        Mirrors :meth:`IdleWatcher` parity — same lookup, same source of
        truth. When ``task_board`` is unwired (``None``) we treat the
        worker as having a task so the with-task narrow filter applies;
        the alternative would be to widen by default in test fixtures
        that don't bother with a board, which risks surprise nudges.
        Errors from the board are swallowed for the same reason.
        """
        if self._task_board is None:
            return True
        try:
            return bool(self._task_board.active_tasks_for_worker(name))
        except Exception:
            _log.debug(
                "inter_worker_watcher: active_tasks_for_worker raised for %s",
                name,
                exc_info=True,
            )
            return True

    async def _maybe_spawn_handoff(
        self, recipient: str, inter_worker: list[Message], *, now: float
    ) -> bool:
        """task #442: turn an action-bearing handoff to a task-less,
        idle recipient into a *tracked* task assigned to them.

        A nudge alone is one-shot — a missed turn or a daemon restart
        loses it and the published work sits unconsumed with nothing
        driving it (the #985 → realtruth incident; #441 was the manual
        backfill this makes unnecessary). A spawned, assigned task is
        durable: the IdleWatcher carries it to completion. Idempotent
        per message id, so a still-unread handoff doesn't re-spawn
        before the board reflects the assignment. Returns True when a
        task was spawned (caller then skips the redundant nudge).
        """
        if self._spawn_handoff_task is None:
            return False
        handoffs = [
            m
            for m in inter_worker
            if m.msg_type in _ACTION_REQUIRED_MSG_TYPES
            and getattr(m, "id", None) is not None
            and m.id not in self._spawned_msg_ids
        ]
        if not handoffs:
            return False
        latest = max(handoffs, key=lambda m: m.created_at)
        try:
            ok = await self._spawn_handoff_task(recipient, latest)
        except Exception:
            _log.warning(
                "inter_worker_watcher: spawn_handoff_task failed for %s",
                recipient,
                exc_info=True,
            )
            return False
        if not ok:
            return False
        for m in handoffs:
            self._spawned_msg_ids.add(m.id)
        # Reuse the nudge debounce slot so the existing inter-worker
        # nudge path doesn't also fire for this worker right after.
        self._last_nudge[recipient] = now
        self._drone_log.add(
            DroneAction.AUTO_HANDOFF_TASK,
            recipient,
            (
                f"actionable handoff from {latest.sender} "
                f"({latest.msg_type}, msg #{latest.id}) → spawned a tracked "
                f"task; recipient was idle/task-less "
                f"({len(handoffs)} unread handoff msg(s))"
            ),
            category=LogCategory.DRONE,
        )
        return True

    def _is_debounced(self, name: str, *, now: float) -> bool:
        """True when this worker was nudged within the debounce window."""
        if self.debounce_seconds <= 0:
            return False
        last = self._last_nudge.get(name)
        if last is None:
            return False
        return (now - last) < self.debounce_seconds

    def _is_skip_logged(self, name: str, *, now: float) -> bool:
        """True when we've already logged an informational-only skip
        recently and shouldn't re-log on every sweep."""
        if self.debounce_seconds <= 0:
            return False
        last = self._last_skip_log.get(name)
        if last is None:
            return False
        return (now - last) < self.debounce_seconds
