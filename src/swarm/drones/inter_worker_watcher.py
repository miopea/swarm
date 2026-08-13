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

    from swarm.config import DroneConfig
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

# #1570: how old a handoff message may be and still spawn a task.
#
# JUSTIFIED FROM THE MEASURED DISTRIBUTION, not picked. Across 62 AUTO_HANDOFF_TASK
# events over 27 days: average age 60.4 min, 55 of 62 under an hour, but a tail out to
# 1147.9 min (19.1 HOURS) and 7 of 62 over an hour. Four hours leaves the bulk of real
# traffic untouched while excluding the stale-resurrection case that prompted this — an
# 11-hour-old already-resolved message waking five workers with no stake in it.
_HANDOFF_MAX_AGE_SECONDS = 4 * 3600.0

# #1570: how many handoff tasks one watcher pass may dispatch.
#
# THE CEILING IS PER SWEEP, NOT PER MESSAGE, and that retarget is the whole point. The
# ticket assumed one message fanned into N tasks; the record says otherwise — 62 of 62
# events produced exactly ONE task, because #1116 already records the SEND as spawned so
# a broadcast cannot re-spawn per recipient. What actually happened was a BACKLOG DRAIN:
# four tasks in six seconds from four DIFFERENT messages. A per-message cap would be dead
# code that reads like protection; bounding the sweep is what bounds concurrent builds.
_HANDOFF_MAX_PER_SWEEP = 2


def _within_age_window(message: object, now_ts: float) -> bool:
    """Is this message recent enough to spawn a handoff task? (#1570)

    FAILS OPEN ON AN UNKNOWABLE AGE, and that is the whole subtlety. A missing,
    zero or non-numeric ``created_at`` means "could not determine how old this is",
    NOT "infinitely old". Treating it as ancient would silently suppress real
    handoffs — a worse failure than the stale-resurrection this guard exists to
    stop, because a suppressed handoff is invisible while a resurrected one at
    least announces itself.

    The same direction was got backwards twice elsewhere on this fleet (an empty
    worker roster read as "nobody exists", an absent tool schema read as "nothing
    allowed"), so it is asserted by a test rather than trusted to a comment.
    """
    created = getattr(message, "created_at", None)
    if not isinstance(created, int | float) or created <= 0:
        return True
    return (now_ts - float(created)) <= _HANDOFF_MAX_AGE_SECONDS


def _source_key(m: Message) -> tuple[str, float]:
    """#1116: identify the SEND a message row came from, not the row.

    ``MessageStore.send`` fans a broadcast out into one row per recipient,
    each with its own primary key but all sharing the single ``created_at``
    stamped once for the call. Keying on ``(sender, created_at)`` therefore
    collapses a broadcast to one unit of work while leaving two genuinely
    distinct sends — even with identical text — separate, because their
    timestamps differ. Neither content nor recipient participates.
    """
    return (m.sender, float(m.created_at))


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
        drone_config: DroneConfig,
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
        # #1116: SOURCE-send keys we've already spawned for. A broadcast is
        # not one row — ``store.send`` fans it out into ONE ROW PER RECIPIENT,
        # each with its own primary key (measured: 23 rows, 23 recipients, one
        # ``created_at``). So ``_spawned_msg_ids`` above can never dedup a
        # broadcast: every idle worker sees a DIFFERENT id for the same send,
        # and each one spawns its own handoff task. That is how a single
        # rcg-dev-install broadcast became #1108/#1112/#1113, two of them
        # byte-identical.
        #
        # ``(sender, created_at)`` identifies the SEND, not the row: the
        # fan-out shares one float timestamp because ``send`` stamps ``now``
        # once. Keys on neither content nor recipient, so two genuinely
        # distinct sends — even with identical text — still spawn separately.
        #
        # #1182: BOTH sets above are an in-process FAST PATH ONLY. They are
        # recorded on the success path, so a spawn whose PTY dispatch failed
        # (task row created and kept ASSIGNED, ``ok`` False) records nothing —
        # and they are wiped by every daemon reload besides. The authoritative,
        # restart-durable guard is the source-key TAG that
        # ``TaskCoordinator.spawn_handoff_task`` writes at ``board.create``
        # time. Keep these in sync with ``_source_key``; do not treat them as
        # the dedup.
        self._spawned_sources: set[tuple[str, float]] = set()
        # #1570: reset at the top of every sweep — this bounds CONCURRENT dispatches,
        # so it must be per-pass rather than cumulative.
        self._handoff_spawns_this_sweep: int = 0
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
        self._handoff_spawns_this_sweep = 0  # #1570: per-pass ceiling
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
            # Task #271 narrowed the nudge trigger to action-required types
            # when the worker has an active task, but WIDENED it back to ANY
            # unread type when the worker had no active task ("idle anyway —
            # process the inbox"). Task #873 found that no-task widening was a
            # rate-limit amplifier: one worker broadcasting an FYI ``finding``
            # woke every idle, task-less worker in the fleet. So by default
            # (``nudge_idle_for_informational=False``) the action-required
            # filter now applies UNCONDITIONALLY — informational
            # finding/status/note never wakes an idle worker, with or without
            # a task. Operators can opt back into the legacy no-task widening
            # via the config flag. ``_maybe_spawn_handoff`` (action-bearing
            # types only) is unaffected, so genuine handoffs to a task-less
            # idle worker still become tracked tasks.
            has_task = self._has_active_task(worker.name)
            # task #442: a task-less idle recipient of an action-bearing
            # handoff gets a *tracked* task, not just a nudge. Done first
            # because the spawned assignment flips has_task and the
            # assign-and-start dispatch already prompts the worker, so a
            # nudge this sweep would double up.
            if not has_task and await self._maybe_spawn_handoff(worker.name, inter_worker, now=now):
                sent += 1
                continue
            widen_for_informational = (not has_task) and bool(
                getattr(self._config, "nudge_idle_for_informational", False)
            )
            if widen_for_informational:
                actionable = inter_worker
            else:
                actionable = [m for m in inter_worker if m.msg_type in _ACTION_REQUIRED_MSG_TYPES]
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
        and escalate to the operator once.

        #614: the fingerprint is the SET of unread inter-worker message ids —
        NOT the worker state. "Progress" means the inbox actually changed (a
        message was cleared, or a new one arrived), which is the thing a nudge
        asks the worker to do. Keying on worker state was the churn bug: a
        recipient that *responds* to a nudge oscillates RESTING↔SLEEPING↔BUZZING
        between windows, which flipped the fingerprint and reset the streak every
        sweep — so an unread message it never cleared got nudged forever (the
        aria/#1390 case: 72 nudges over 22h). Returns True only when a real nudge
        fired.
        """
        fingerprint = frozenset(m.id for m in inter_worker if m.id is not None)
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
            return bool(self._task_board.assigned_or_active_tasks_for_worker(name))
        except Exception:
            _log.debug(
                "inter_worker_watcher: assigned_or_active_tasks_for_worker raised for %s",
                name,
                exc_info=True,
            )
            return True

    def _already_answered(self, recipient: str, message: object) -> bool:
        """Has *recipient* replied to this message's sender since it arrived? (#1570)

        DELIBERATELY NOT ``read_at``, and that distinction is measured rather than
        assumed. The naive check says 62 of 62 handoff messages were "already read" —
        but all 62 carry a ``read_at`` within two seconds of the handoff, average gap
        0.0s, none earlier. THE GENERATOR SETS IT ITSELF (#894, for durable dedup), so
        ``read_at`` measures this code, not the recipient. Using it would have produced
        a guard that fires on everything while looking like it checked something.

        A reply is a message from the recipient back to the sender after the original
        arrived. Best-effort: on any store failure this returns False, so an
        unanswerable question degrades to the current behaviour rather than silently
        suppressing real handoffs.
        """
        store = getattr(self, "_message_store", None)
        sender = getattr(message, "sender", "")
        created = getattr(message, "created_at", None)
        if store is None or not sender or created is None:
            return False
        try:
            since = float(created)
            return any(
                m.sender == recipient and m.recipient == sender
                for m in store.get_recent(limit=50, since=since)
            )
        except Exception:
            _log.debug(
                "inter_worker_watcher: answered-check failed for %s", recipient, exc_info=True
            )
            return False

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
        # #1570: a sweep may only dispatch so much. See _HANDOFF_MAX_PER_SWEEP —
        # the cost that redlined the operator's machine was CONCURRENT builds, and
        # they came from a backlog draining at once, not from one message fanning out.
        if self._handoff_spawns_this_sweep >= _HANDOFF_MAX_PER_SWEEP:
            return False
        now_ts = time.time()
        handoffs = [
            m
            for m in inter_worker
            if m.msg_type in _ACTION_REQUIRED_MSG_TYPES
            and getattr(m, "id", None) is not None
            and m.id not in self._spawned_msg_ids
            and _source_key(m) not in self._spawned_sources
            # #1570: stale messages do not get resurrected as fresh work.
            and _within_age_window(m, now_ts)
            # #1570: nor do ones the recipient has already answered.
            and not self._already_answered(recipient, m)
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
        self._handoff_spawns_this_sweep += 1
        for m in handoffs:
            self._spawned_msg_ids.add(m.id)
        # #1116: record the SEND as spawned, not just this recipient's row —
        # otherwise the same broadcast spawns again for the next idle worker.
        self._spawned_sources.add(_source_key(latest))
        # #894: PERSIST the spawn-dedup by CONSUMING the source message(s).
        # ``_spawned_msg_ids`` is in-memory only, so a daemon restart wiped it
        # and the watcher re-relayed an already-handed-off (and since-retracted)
        # source message as fresh tasks over and over — the @types/node-26
        # #890/#891/#896/#897 loop hub reported. Marking the source messages
        # read is durable (DB ``read_at``): the spawned task is now the carrier,
        # so ``get_unread`` never re-surfaces them after a restart, which both
        # PURGES the offending message from the spawner queue and makes the
        # declined-task re-spawn guard survive a restart.
        try:
            self._message_store.mark_read(recipient, [m.id for m in handoffs])
        except Exception:
            _log.debug(
                "inter_worker_watcher: mark_read after handoff spawn failed for %s",
                recipient,
                exc_info=True,
            )
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
