"""Idle-watcher drone — nudge RESTING workers sitting on assigned tasks.

Phase 2 of task #225. Phase 1 of the same ticket fixed the common case —
``swarm_create_task(target_worker=X)`` now dispatches the task into X's PTY
on assignment. But that only covers the happy path. If a worker drops a
task mid-turn (crash, compact, network hiccup) or the Queen hand-assigns
via a path that doesn't go through ``assign_and_start_task``, the worker
can still end up RESTING with an ASSIGNED/IN_PROGRESS task it's not
actually working on. This watcher sweeps periodically and catches those.

Scope: intentionally narrow. The watcher doesn't diagnose — it just pokes
the worker with a pointer at its own tools (``swarm_task_status mine``,
``swarm_check_messages``) so the worker can decide whether to resume or
report a blocker. Every nudge is logged to the buzz log so the operator
can tune cadence or catch runaway prompting.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swarm.drones.log import DroneAction, LogCategory, SystemAction
from swarm.drones.nudge_guard import ESCALATE, SILENT, RepeatNudgeGuard, operator_engaged
from swarm.logging import get_logger
from swarm.worker.worker import WorkerState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from swarm.config import DroneConfig
    from swarm.drones.log import DroneLog
    from swarm.tasks.blockers import Blocker, BlockerStore
    from swarm.tasks.board import TaskBoard
    from swarm.tasks.task import SwarmTask
    from swarm.worker.worker import Worker


_log = get_logger("drones.idle_watcher")

# After firing /mcp the worker spends a moment showing/dismissing the MCP
# dialog. Wait this many seconds before sending the regular task nudge so
# Claude Code has time to settle back at an empty prompt and re-establish
# its MCP transport. Without the follow-up the worker would sit idle until
# the next sweep (default 180s) — task #315.
_MCP_FOLLOWUP_DELAY_SECONDS = 5.0


# States where a worker is "idle" from the watcher's perspective.  BUZZING
# means the worker is already producing output so we leave it alone.
# WAITING is an approval prompt — operator/drone rules handle that path.
# STUNG means the worker process has exited; revive is a separate concern.
_IDLE_STATES: frozenset[WorkerState] = frozenset({WorkerState.RESTING, WorkerState.SLEEPING})

# #1910b: how often an UNCHANGED stranding re-reports. Hourly, on the Queen's ruling.
# The number is a compromise between two real failure modes: every sweep mutes the signal
# (which is why the original debounced at all), and never re-emitting makes a recent-window
# check read as "not firing" (which is what actually happened, twice).
_UNSENT_REEMIT_SECONDS = 3600.0


def _format_duration(seconds: float) -> str:
    """Human duration for a stranding — "12.9h" carries what "stranded" does not.

    Duration is the field that tells a reader at a glance whether a finding is old or new,
    and it is free: the debounce already holds first_seen.
    """
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _nudge_message(task_numbers: list[int], *, all_active: bool = False) -> str:
    """Build the PTY message sent to an idle worker.

    Kept short and tool-centric — we want the worker to call its existing
    status + message MCP tools rather than treat this like a new prompt.
    """
    if len(task_numbers) == 1:
        task_ref = f"#{task_numbers[0]}"
    else:
        task_ref = ", ".join(f"#{n}" for n in task_numbers)
    # "open", not "active": these are bucketed from ``task_board.assigned_or_active_tasks``,
    # which is ASSIGNED **or** ACTIVE, so calling them all active told the worker
    # the board said something it did not — the same conflation that put queued
    # tasks in the worker title bar (#1282).
    #
    # The start-verb hint rides on THIS message rather than getting one of its own.
    # docs/specs/worker-asserted-active.md rejected a dedicated nudge about
    # unasserted tasks, and rightly: repeated nudges about unactionable state are
    # how operators learn to ignore nudges. This nudge already fires, and the
    # worker it reaches is exactly the one that can resolve the ambiguity.
    # #1664: DO NOT PRESCRIBE A VERB THE RECIPIENT CANNOT FOLLOW. When every bucketed task
    # is already ACTIVE, `swarm_start_task` answers "already in progress — nothing changed",
    # and the sentence asserts the board shows the task as queued when the board shows it
    # ACTIVE. That is a state claim inherited from the "assigned OR active" bucket rather
    # than re-read at send time, and a worker acting on it wastes a turn discovering the
    # message was wrong. Reported by sculpt-studio after three such nudges on #1656.
    if all_active:
        return (
            f"You have {task_ref} in progress but appear idle. "
            "Run `swarm_task_status filter=mine` and `swarm_check_messages`, "
            "then continue it, complete it, or report a blocker."
        )
    return (
        f"You have {task_ref} open but appear idle. "
        "Run `swarm_task_status filter=mine` and `swarm_check_messages`, "
        "then resume or report a blocker. If you are actually working one of them, "
        "call `swarm_start_task` so the board shows it in progress rather than queued."
    )


def _all_active(tasks: list[Any]) -> bool:
    """True when EVERY bucketed task is already ACTIVE (#1664).

    Read from the task objects AT SEND TIME rather than inherited from the
    ``assigned_or_active_tasks`` bucket, which by construction cannot tell the two apart.
    That conflation is what let the nudge tell a worker its ACTIVE task was "queued" and
    prescribe a start verb that answers "already in progress".

    Defensive about the status shape (enum or bare string) because this decides only the
    WORDING — a misread here should soften the message, never suppress the nudge.
    """
    if not tasks:
        return False
    for t in tasks:
        status = getattr(t, "status", None)
        value = getattr(status, "value", status)
        if str(value).lower() != "active":
            return False
    return True


def commit_age_seconds(worker: Any) -> float | None:
    """Seconds since this worker's repo last recorded a commit, or None if unknowable.

    USES FILE MTIMES, NOT ``git log``, AND THE REASON IS THE EVENT LOOP. This is consulted
    from ``_suppression_reason``, which is synchronous and runs inside the sweep coroutine
    — a subprocess there would block PTY polling for however long git takes, and blocking
    IO on the loop is a defect this codebase has already had to hunt down once. A stat is
    microseconds.

    ``.git/COMMIT_EDITMSG`` is rewritten by every commit, which makes its mtime a direct
    commit clock. ``.git/HEAD`` moves on checkout/commit and is the fallback. Handles the
    worktree case where ``.git`` is a FILE pointing at the real gitdir, because several
    workers run on worktrees.

    Returns None on anything unexpected — no repo, no permission, a path that is not a
    checkout. None means "could not tell" and the caller must treat it as such.
    """
    path = getattr(worker, "path", None)
    if not path or not isinstance(path, str):
        return None
    try:
        root = Path(os.path.expanduser(path))
        git = root / ".git"
        if git.is_file():
            # Worktree: `.git` is a file containing `gitdir: /abs/path`.
            text = git.read_text(errors="replace").strip()
            if not text.startswith("gitdir:"):
                return None
            git = Path(text.split(":", 1)[1].strip())
        if not git.is_dir():
            return None
        newest: float | None = None
        for name in ("COMMIT_EDITMSG", "HEAD"):
            candidate = git / name
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            newest = mtime if newest is None else max(newest, mtime)
        if newest is None:
            return None
        return max(0.0, time.time() - newest)
    except Exception:
        _log.debug("idle_watcher: commit_age_seconds failed for %s", path, exc_info=True)
        return None


class IdleWatcher:
    """Periodic sweep: idle workers with active tasks get a nudge.

    Parameters
    ----------
    drone_config:
        Owns ``idle_nudge_interval_seconds`` and
        ``idle_nudge_debounce_seconds``. ``interval <= 0`` disables the
        watcher entirely.
    task_board:
        Source of truth for "does this worker have an active task".
    drone_log:
        Every nudge is appended as ``AUTO_NUDGE`` under ``LogCategory.DRONE``.
    send_to_worker:
        Async callable ``(worker_name, message, *, _log_operator=False) -> None``.
        Mirrors ``SwarmDaemon.send_to_worker`` — injected so tests can
        substitute a fake without dragging in a full daemon.
    rate_limit_check:
        Optional ``(worker_name) -> bool``.  Returning ``True`` skips
        the nudge for that worker (e.g. hit the 5hr Claude quota —
        prompting would stack stale work behind a dead quota).
    loop_armed_check:
        Optional ``(worker_name) -> float | None``.  Returns the seconds
        a worker is parked between native ``/loop`` fires (task #761), or
        ``None`` when it isn't loop-armed.  A positive value skips the
        nudge so a worker waiting to resume its own loop is left alone.
    """

    def __init__(
        self,
        *,
        drone_config: DroneConfig,
        task_board: TaskBoard | None,
        drone_log: DroneLog,
        send_to_worker: Callable[..., Awaitable[None]],
        rate_limit_check: Callable[[str], bool] | None = None,
        blocker_store: BlockerStore | None = None,
        message_has_newer: Callable[[str, float], bool] | None = None,
        mcp_activity_lookup: Callable[[str], float | None] | None = None,
        daemon_start_time: float | None = None,
        mcp_followup_delay_seconds: float = _MCP_FOLLOWUP_DELAY_SECONDS,
        escalate_to_operator: Callable[[str, str], None] | None = None,
        worker_busy_check: Callable[[Worker], bool] | None = None,
        loop_armed_check: Callable[[str], float | None] | None = None,
        commit_activity_check: Callable[[Worker], float | None] | None = None,
        unsent_input_check: Callable[[Worker], str] | None = None,
    ) -> None:
        self._config = drone_config
        self._task_board = task_board
        self._drone_log = drone_log
        self._send_to_worker = send_to_worker
        self._rate_limit_check = rate_limit_check
        self._loop_armed_check = loop_armed_check
        # #1664: seconds since this worker's repo last received a commit, or None when it
        # cannot be determined. The state machine cannot see an editor, a test run or a
        # long build — a worker resting past the activity window while committing is
        # working, and #1656 was nudged three times in that exact state.
        self._commit_activity_check = commit_activity_check
        # Task #315: how long to wait between firing /mcp and the
        # follow-up task nudge. Overridable so tests can run with 0
        # without sleeping for real wall time.
        self._mcp_followup_delay = mcp_followup_delay_seconds
        # Track in-flight follow-up tasks so they aren't garbage-collected
        # mid-sleep and so daemon shutdown can cancel them cleanly.
        self._mcp_followups: set[asyncio.Task[None]] = set()
        # Task #250: worker-reported blockers. When a worker calls
        # ``swarm_report_blocker`` we store "worker X is blocked on task
        # #Y until Y completes OR a new message lands"; the watcher
        # skips that worker's nudge until one of those clears.
        # ``message_has_newer(worker, since_ts)`` returns True if the
        # worker has any message newer than ``since_ts`` — typically
        # wired to ``message_store`` at the daemon level, left None in
        # tests that don't exercise the message-clear path.
        self._blocker_store = blocker_store
        self._message_has_newer = message_has_newer
        # Task #257: MCP tools-dropped detection. When the daemon reloads,
        # Claude Code's HTTP MCP transport can give up reconnecting after
        # its retry ceiling. If a worker sits idle through a reload, its
        # client-side tool registry is empty and the normal nudge above is
        # useless (worker can't call swarm_check_messages / task_status).
        # Recovery: detect the state (no MCP activity since daemon start
        # *and* unread inbox) and inject ``/mcp\n`` via PTY to force
        # re-initialize client-side.  ``mcp_activity_lookup(worker_name)``
        # returns the worker's most recent MCP dispatch timestamp (wall
        # time) or None.  ``daemon_start_time`` is the daemon's own boot
        # timestamp.  Both None = feature disabled.
        self._mcp_activity_lookup = mcp_activity_lookup
        self._daemon_start_time = daemon_start_time
        # (worker_name, task_id) → last-nudge monotonic timestamp
        self._last_nudge: dict[tuple[str, str], float] = {}
        # worker_name → monotonic timestamp of last MCP-refresh injection.
        # Debounced separately from the regular nudge because we want at
        # most one ``/mcp`` injection per worker per boot cycle.
        self._mcp_refresh_fired: set[str] = set()
        # Two-strike rule (operator feedback 2026-05-01): "no MCP activity
        # since daemon boot" alone is too coarse — a worker that's just
        # legitimately parked on a task (no tool call yet) trips the same
        # signal as a worker whose Claude Code transport actually died.
        # First sweep records the strike and falls through to the normal
        # task nudge; if the transport is fine the worker answers the
        # nudge with an MCP call and ``_needs_mcp_refresh`` flips to
        # False. Only a second sweep that *still* sees zero activity fires
        # ``/mcp``.
        self._mcp_first_strike: set[str] = set()
        self._last_sweep: float = 0.0
        # Task #546: stop nudging + escalate to operator after
        # idle_nudge_max_repeats consecutive no-progress nudges, instead
        # of looping forever on a task the worker can't progress.
        # ``escalate_to_operator(worker_name, detail)`` surfaces one
        # operator-facing attention item; None disables escalation (the
        # guard then still caps the loop by going SILENT, just without an
        # operator ping — e.g. in tests).
        self._escalate_to_operator = escalate_to_operator
        # 2026-06-11 false-AUTO_NUDGE bug, trigger #2: ``display_state`` can
        # read RESTING for a worker that's actually mid a long *quiet*
        # foreground command (``gh run watch`` on a deploy). This predicate
        # re-reads the live PTY for a mid-turn signal so such a worker is not
        # nudged. None disables the check (tests / non-Claude providers).
        self._worker_busy_check = worker_busy_check
        # #1858: reads the live input line. None disables the check entirely, so a
        # deployment without it reports nothing rather than reporting everything clean.
        self._unsent_input_check = unsent_input_check
        # Debounce per worker on the TEXT, not the worker: re-reporting the same
        # stranded line every sweep is how a real signal gets muted, but a worker
        # that strands a SECOND, different instruction is a new finding.
        # #1910b: (text, first_seen, last_reported) per worker. The first version stored
        # only the TEXT, which made a stranding a POINT EVENT: reported once, then silent
        # while the condition continued. A reader samples WINDOWS, not edges, so "already
        # reported, still stranded" and "nothing to report" produced identical output —
        # and the Queen concluded the detector was broken twice, reading the log
        # correctly both times. first_seen is what makes a duration possible.
        self._last_unsent_seen: dict[str, tuple[str, float, float]] = {}
        self._nudge_guard = RepeatNudgeGuard()

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
        return self.interval_seconds > 0 and self._task_board is not None

    def due(self, *, now: float | None = None) -> bool:
        """Has enough wall time elapsed since the last sweep?"""
        if not self.enabled:
            return False
        now = now if now is not None else time.monotonic()
        return (now - self._last_sweep) >= self.interval_seconds

    async def sweep(self, workers: list[Worker], *, now: float | None = None) -> int:
        """Run one sweep.  Returns the number of nudges actually sent.

        Safe to call more often than ``interval_seconds``; no-ops when not
        due. Caller can force a sweep by passing a ``now`` value that pushes
        past the threshold.
        """
        if not self.enabled:
            return 0
        now = now if now is not None else time.monotonic()
        if (now - self._last_sweep) < self.interval_seconds:
            return 0
        self._last_sweep = now

        sent = 0
        tasks_by_worker = self._bucket_active_tasks_by_worker()
        for worker in workers:
            # #1858 — RUNS FOR EVERY WORKER, BEFORE the nudge filters. Deliberately not
            # gated on having an active task: platform-data sat 8.6 HOURS holding "add
            # the same hook to nexus's package.json", and a check that only looked at
            # task-carrying workers is a check that would have missed it.
            self._check_unsent_input(worker)
            if not self._should_nudge(worker, now=now):
                continue
            active = tasks_by_worker.get(worker.name, [])
            if not active:
                continue
            # 2026-06-11 false-idle guards: don't nudge a worker the
            # operator is actively driving (trigger #1) or one that's
            # genuinely busy despite a stale RESTING display_state
            # (trigger #2). Logged so the audit trail shows WHY no nudge
            # fired, mirroring the reported-blocker skip below.
            suppression = self._suppression_reason(worker)
            if suppression is not None:
                self._drone_log.add(
                    SystemAction.AUTO_NUDGE_SKIPPED,
                    worker.name,
                    suppression,
                    category=LogCategory.DRONE,
                )
                continue
            # Task #250: worker-reported blocker takes precedence over
            # the nudge. If the blocker store says this worker is still
            # blocked (task not completed, no new messages since the
            # report), skip the nudge + log an AUTO_NUDGE_SKIPPED entry
            # so the audit trail shows WHY the worker wasn't nudged.
            blocker = self._active_blocker(worker.name)
            if blocker is not None:
                self._drone_log.add(
                    SystemAction.AUTO_NUDGE_SKIPPED,
                    worker.name,
                    f"reported blocker on #{blocker.task_number} "
                    f"(waiting on #{blocker.blocked_by_task})",
                    category=LogCategory.DRONE,
                )
                continue
            # Task #257: detect the "client-side MCP tools dropped after
            # daemon reload" state.  If this worker hasn't made any MCP
            # calls since the daemon started, the normal nudge is
            # useless (the worker can't call ``swarm_check_messages`` or
            # ``swarm_task_status`` — the client tool registry is empty).
            # Two-strike rule: the first sighting falls through to the
            # normal nudge so a worker with a healthy transport gets a
            # chance to answer (its MCP call clears the stale signal).
            # Only the second consecutive sighting injects ``/mcp``.
            if self._needs_mcp_refresh(worker.name):
                if worker.name in self._mcp_first_strike:
                    await self._fire_mcp_refresh(worker.name)
                    continue
                self._mcp_first_strike.add(worker.name)
            numbers = sorted({t.number for t in active})
            # Debounce per (worker, task_id) — don't spam the same work.
            task_ids = [t.id for t in active]
            fresh_keys = [
                (worker.name, tid) for tid in task_ids if self._is_fresh(worker.name, tid, now=now)
            ]
            if not fresh_keys:
                continue
            if await self._dispatch_or_escalate(worker, active, numbers, fresh_keys, now=now):
                sent += 1
        return sent

    async def _dispatch_or_escalate(
        self,
        worker: Worker,
        active: list[SwarmTask],
        numbers: list[int],
        fresh_keys: list[tuple[str, str]],
        *,
        now: float,
    ) -> bool:
        """A nudge is due for ``worker``; send it, or escalate + go quiet.

        Task #546: consult the repeat-guard. If the worker has been nudged
        ``idle_nudge_max_repeats`` times with no progress, stop poking and
        escalate to the operator once (then stay SILENT until something
        changes). Otherwise send the normal nudge. Returns True only when
        a real nudge was sent (so the caller's ``sent`` tally stays
        accurate). The fingerprint captures "did anything change worth
        re-nudging": worker display-state + each active task's
        (number, status).
        """
        fingerprint = (
            worker.display_state.value,
            tuple(sorted((t.number, t.status.value) for t in active)),
        )
        decision = self._nudge_guard.decide(worker.name, fingerprint, max_repeats=self._max_repeats)
        # Mark the debounce in all branches so the guard is re-consulted at
        # most once per debounce window, not on every sweep.
        for key in fresh_keys:
            self._last_nudge[key] = now
        if decision == SILENT:
            return False  # already escalated; quiet until fingerprint changes
        if decision == ESCALATE:
            self._escalate(worker.name, numbers)
            return False
        # NUDGE → normal poke. #1664: re-read the statuses HERE rather than inheriting
        # "assigned or active" from the bucket, so the message describes the board as it
        # is at send time.
        message = _nudge_message(numbers, all_active=_all_active(active))
        try:
            await self._send_to_worker(worker.name, message, _log_operator=False)
        except Exception:
            # Don't let one failed worker kill the sweep — log and move on.
            _log.warning("idle_watcher: send_to_worker failed for %s", worker.name, exc_info=True)
            return False
        self._drone_log.add(
            DroneAction.AUTO_NUDGE,
            worker.name,
            f"idle with active task(s): {', '.join(f'#{n}' for n in numbers)}",
            category=LogCategory.DRONE,
        )
        return True

    def _check_unsent_input(self, worker: Worker) -> None:
        """Report a worker sitting on unsubmitted text. NEVER submits it (#1858).

        AUTO-SUBMIT IS RULED OUT, NOT MERELY UNIMPLEMENTED. Two of the three observed
        instances were production deploy approvals — "ship it" and "merge it, deploys
        straight to production". Firing a buffered instruction would execute a deploy
        nobody confirmed, and nothing here can tell "typed it and meant it" from "typed
        it and thought better of it". That ambiguity is why the Queen declined to submit
        them by hand three times in one night, and it does not get easier for a drone.

        DETECTION BEATS PREVENTION HERE: whatever writes the text, the resulting state is
        trivially observable and was observed by nobody. This makes it a buzz-log line
        the Queen already reads, instead of a raw PTY tail she has to go and read.

        REPORTS ON A HEARTBEAT, NOT ONLY ON THE EDGE (#1910b). The original debounced on
        the TEXT alone, which was right about noise and wrong about state: re-reporting
        every sweep really would mute a real signal, but suppressing forever made the
        condition unobservable between edges. A 12.9-hour stranding left one row 2.6 hours
        old and silence since, so a recent-window check read as "not firing".

        The correct half of that mechanism is kept: NEW text on the same worker is a new
        finding and fires immediately. Only the unchanged case re-emits, hourly, carrying
        how long — duration is the field that distinguishes old from new at a glance.
        """
        if self._unsent_input_check is None:
            return
        try:
            text = self._unsent_input_check(worker)
        except Exception:
            _log.warning(
                "idle_watcher: unsent-input check raised for %s", worker.name, exc_info=True
            )
            return
        now = time.time()
        if not text:
            # Cleared: forget it, so the SAME text stranded again later reports again.
            self._last_unsent_seen.pop(worker.name, None)
            return

        prior = self._last_unsent_seen.get(worker.name)
        if prior is not None and prior[0] == text:
            first_seen, last_reported = prior[1], prior[2]
            if (now - last_reported) < _UNSENT_REEMIT_SECONDS:
                return  # same text, reported recently — stay quiet
            held = _format_duration(now - first_seen)
            detail = (
                f"STILL idle with the same UNSENT text after {held} "
                f"(not submitted, not auto-submitted): {text[:160]}"
            )
        else:
            first_seen = now
            detail = (
                f"idle with UNSENT text on the input line "
                f"(not submitted, not auto-submitted): {text[:160]}"
            )
        self._last_unsent_seen[worker.name] = (text, first_seen, now)
        self._drone_log.add(
            DroneAction.UNSENT_INPUT_DETECTED,
            worker.name,
            detail,
            category=LogCategory.DRONE,
        )

    def stranded_now(self, workers: list[Worker]) -> list[tuple[str, str, float]]:
        """Who is holding unsent input RIGHT NOW — a live read, not a report of reports.

        #1910b half (a), and the thing the Queen actually needed both times she asked.
        Deliberately re-runs the detector against every worker rather than returning
        ``_last_unsent_seen``: that dict records what has been REPORTED, which is a
        different question and is exactly the confusion this whole ticket is about. A
        cached answer would drift from the truth and look authoritative doing it.

        Duration comes from the debounce's first_seen where we have one, and is 0.0 for a
        stranding this read is the first to see — 0.0 means "just found", never "no data".
        """
        out: list[tuple[str, str, float]] = []
        if self._unsent_input_check is None:
            return out
        now = time.time()
        for worker in workers:
            try:
                text = self._unsent_input_check(worker)
            except Exception:
                _log.warning(
                    "stranded_now: check raised for %s", getattr(worker, "name", "?"), exc_info=True
                )
                continue
            if not text:
                continue
            prior = self._last_unsent_seen.get(worker.name)
            held = now - prior[1] if prior is not None and prior[0] == text else 0.0
            out.append((worker.name, text, held))
        return sorted(out, key=lambda row: row[2], reverse=True)

    def _escalate(self, worker_name: str, numbers: list[int]) -> None:
        """Stop nudging ``worker_name`` and surface one operator attention
        item (task #546). Best-effort — a callback failure must not break
        the sweep."""
        detail = (
            f"idle on {', '.join(f'#{n}' for n in numbers)} across "
            f"{self._max_repeats} nudges with no progress — escalated to operator"
        )
        self._drone_log.add(
            SystemAction.AUTO_NUDGE_ESCALATED,
            worker_name,
            detail,
            category=LogCategory.DRONE,
        )
        if self._escalate_to_operator is not None:
            try:
                self._escalate_to_operator(worker_name, detail)
            except Exception:
                _log.debug(
                    "idle_watcher: escalate_to_operator raised for %s",
                    worker_name,
                    exc_info=True,
                )

    def _bucket_active_tasks_by_worker(self) -> dict[str, list]:
        """Snapshot the board's active tasks once and group by assignee.

        Calling ``assigned_or_active_tasks_for_worker`` inside the sweep loop was O(W·T) —
        each call re-snapshotted the full task dict under the board lock.
        One pass over ``assigned_or_active_tasks`` plus dict lookups in the loop drops
        that to O(T) regardless of worker count.
        """
        bucketed: dict[str, list] = {}
        for t in self._task_board.assigned_or_active_tasks:
            # #1015: a deliberately-parked task is not work the worker is
            # neglecting — it's work they set down on purpose. Nudging about
            # it is exactly the "repeated idle-watcher nudges" symptom.
            if t.assigned_worker and not t.is_on_hold:
                bucketed.setdefault(t.assigned_worker, []).append(t)
        return bucketed

    def _should_nudge(self, worker: Worker, *, now: float) -> bool:
        """Cheap filters applied BEFORE we look at the task board."""
        if worker.display_state not in _IDLE_STATES:
            return False
        if self._rate_limit_check is not None:
            try:
                if self._rate_limit_check(worker.name):
                    return False
            except Exception:
                _log.debug(
                    "idle_watcher: rate_limit_check raised for %s", worker.name, exc_info=True
                )
                return False
        return True

    def _suppression_reason(self, worker: Worker) -> str | None:
        """Why this worker should NOT be nudged right now, or None.

        Two false-idle guards added after the 2026-06-11 AUTO_NUDGE
        incident (display_state already filtered RESTING/SLEEPING upstream,
        but both signals below fire on workers that READ idle yet aren't):

        (a) operator-engaged — the operator typed in this worker's PTY
            within ``assign_operator_engagement_minutes`` (trigger #1).
        (b) worker-busy — the live PTY shows a mid-turn signal even though
            display_state went stale (trigger #2; long quiet foreground
            command). Gated on actual PTY state, not output-quiet time.

        Returns a short audit reason (logged as AUTO_NUDGE_SKIPPED) or None.
        """
        window = float(getattr(self._config, "assign_operator_engagement_minutes", 0.0) or 0.0)
        if window > 0 and operator_engaged(worker, window * 60.0):
            return f"operator engaged within {window:.0f}m"
        if self._worker_busy_check is not None:
            try:
                if self._worker_busy_check(worker):
                    return "worker busy (active turn / long-running tool)"
            except Exception:
                _log.debug(
                    "idle_watcher: worker_busy_check raised for %s", worker.name, exc_info=True
                )
        # #1615 REPLACED #1610's SIGNAL, AND THE MEASUREMENT IS WHY.
        #
        # #1610 keyed this on `mcp_activity_lookup` — the worker's last MCP dispatch.
        # Correct code, wrong signal: it never sees Bash, Edit, Read or Write, so a
        # worker running a four-minute test suite and writing a commit makes ZERO MCP
        # calls while being unambiguously busy. Measured over two hours: 27 MCP-ish
        # events against 91 worker-attributed ones — under a third of the activity.
        # Replayed against 8 real nudges it would have suppressed 2; the signal below
        # suppresses 7.
        #
        # `state_duration` IS ALREADY HERE — no lookup, no durable store, and none of
        # #1610's reload-reset gap, because it is derived from the state machine rather
        # than an in-memory map that empties on restart.
        #
        # WHAT IT MEANS: a short RESTING duration means the worker just FINISHED A TURN
        # and is at its prompt between pieces of work. A long one means it stopped and
        # did not come back — which is the worker #225 exists to catch. SLEEPING is
        # RESTING past `sleeping_threshold` (1200s), so a slept worker always exceeds
        # this window and is never suppressed.
        #
        # AC5's WORRY DOES NOT APPLY HERE, and it is worth saying rather than assuming:
        # a worker looping on a failing check stays BUZZING, and `_should_nudge` only
        # admits RESTING/SLEEPING — so it is never nudged by this watcher in the first
        # place, and this cannot suppress it.
        window = float(getattr(self._config, "idle_nudge_activity_window_seconds", 0.0) or 0.0)
        resting_for = getattr(worker, "state_duration", None)
        # isinstance, NOT float(): `float(MagicMock())` returns 1.0, so a coercing check
        # silently suppressed every mock-backed worker — 29 existing tests turned red and
        # showed it. A value that is not genuinely numeric is not evidence the worker just
        # finished a turn, so it falls through to NUDGING, which is the safe direction.
        numeric = isinstance(resting_for, int | float) and not isinstance(resting_for, bool)
        if window > 0 and numeric and resting_for < window:
            return f"finished a turn {resting_for:.0f}s ago (within {window:.0f}s window)"

        # #1664: COMMITS ARE ACTIVITY THE STATE MACHINE CANNOT SEE. The guard above keys
        # on `state_duration`, which measures time since the PTY last changed state — so a
        # worker mid-build, mid-edit or simply thinking for longer than the window reads as
        # idle. sculpt-studio was suppressed three times as it approached 600s and nudged
        # the moment it crossed, while its task was ACTIVE and it was working.
        #
        # Same fail-safe shape as the guard above, and for the same reason: `None` means
        # "could not tell", NOT "recently active", and a non-numeric value is not evidence
        # of a commit. Both fall through to NUDGING, because absence of evidence must not
        # become evidence of work — that is exactly how `float(MagicMock())` returning 1.0
        # silently suppressed every mock-backed worker in #1615.
        if window > 0 and self._commit_activity_check is not None:
            try:
                since_commit = self._commit_activity_check(worker)
            except Exception:
                _log.debug(
                    "idle_watcher: commit_activity_check raised for %s", worker.name, exc_info=True
                )
                since_commit = None
            commit_numeric = isinstance(since_commit, int | float) and not isinstance(
                since_commit, bool
            )
            if commit_numeric and since_commit < window:
                return f"committed {since_commit:.0f}s ago (within {window:.0f}s window)"

        # Native /loop coexistence (task #761): a worker that self-scheduled
        # its next loop tick is parked, not free — leave it until it re-wakes.
        if self._loop_armed_check is not None:
            try:
                remaining = self._loop_armed_check(worker.name)
                if remaining is not None and remaining > 0:
                    return f"native /loop armed (next tick in ~{remaining:.0f}s)"
            except Exception:
                _log.debug(
                    "idle_watcher: loop_armed_check raised for %s", worker.name, exc_info=True
                )
        return None

    def _active_blocker(self, worker_name: str) -> Blocker | None:
        """Return the first still-active blocker for ``worker_name``, or None.

        Delegates to :meth:`BlockerStore.has_active_blocker`, wiring in
        "is this task-number completed?" via the task board and "has
        a new message arrived?" via ``message_has_newer``. Both
        auto-clear paths run inside the store call.
        """
        if self._blocker_store is None:
            return None

        def _is_completed(task_number: int) -> bool:
            board = self._task_board
            if board is None:
                return False
            for t in getattr(board, "all_tasks", []):
                if t.number == task_number:
                    return t.status.value == "done"
            return False

        def _on_auto_clear(b: Blocker, reason: str) -> None:
            """Task #529: surface the auto-clear in the buzz log so an
            operator audit can see WHY a previously-blocked worker is
            being nudged again (without this, the only signal is the
            ABSENCE of subsequent AUTO_NUDGE_SKIPPED entries — easy to
            miss). ``reason`` is one of ``target_done`` (the blocker
            target task became done/etc.) or ``message_since`` (new
            inbox traffic landed after the blocker was filed)."""
            self._drone_log.add(
                SystemAction.BLOCKER_AUTO_CLEARED,
                worker_name,
                (
                    f"blocker on #{b.task_number} cleared "
                    f"(reason={reason}, target=#{b.blocked_by_task})"
                ),
                category=LogCategory.DRONE,
            )

        return self._blocker_store.has_active_blocker(
            worker_name,
            is_task_completed=_is_completed,
            has_message_since=self._message_has_newer,
            on_auto_clear=_on_auto_clear,
        )

    def _is_fresh(self, worker_name: str, task_id: str, *, now: float) -> bool:
        """True when ``(worker, task)`` hasn't been nudged within the debounce."""
        last = self._last_nudge.get((worker_name, task_id))
        if last is None:
            return True
        if self.debounce_seconds <= 0:
            return True
        return (now - last) >= self.debounce_seconds

    def _needs_mcp_refresh(self, worker_name: str) -> bool:
        """True when ``worker_name`` has probably lost its client-side MCP tools.

        Criteria (all must hold):
        - MCP-activity tracking is wired (``mcp_activity_lookup`` +
          ``daemon_start_time`` both set).
        - This boot cycle hasn't already fired a refresh for this worker.
        - The worker has made zero MCP calls since the daemon started
          (either no record at all, or the last timestamp predates
          ``daemon_start_time``).

        The "worker has active tasks" check is done by the caller — we
        only fire the refresh on workers the watcher would have nudged
        anyway, so a genuinely idle-with-nothing-to-do worker doesn't
        get pinged for no reason.
        """
        if self._mcp_activity_lookup is None or self._daemon_start_time is None:
            return False
        if worker_name in self._mcp_refresh_fired:
            return False
        last_mcp = self._mcp_activity_lookup(worker_name)
        if last_mcp is None:
            return True
        return last_mcp < self._daemon_start_time

    async def _fire_mcp_refresh(self, worker_name: str) -> None:
        """Inject ``/mcp`` into the worker's PTY and log the intervention.

        Claude Code's ``/mcp`` slash command forces a full MCP client
        re-initialize (re-fetches ``tools/list``, reconnects transports,
        refreshes the tool registry). On success the worker's tool
        surface is restored and future sweeps will trip normal nudge
        behaviour instead of landing here.

        After firing /mcp we schedule a delayed follow-up that sends the
        regular task nudge (task #315). Without it the worker would sit
        at an empty post-dialog prompt until the next sweep —
        ``idle_nudge_interval_seconds`` (default 180s) — which the
        operator perceives as the worker being "stranded".
        """
        self._mcp_refresh_fired.add(worker_name)
        try:
            await self._send_to_worker(worker_name, "/mcp", _log_operator=False)
        except Exception:
            _log.warning("idle_watcher: mcp refresh send failed for %s", worker_name, exc_info=True)
            # Don't leave the refresh flag set on failure — next sweep
            # can retry rather than silently giving up.
            self._mcp_refresh_fired.discard(worker_name)
            return
        self._drone_log.add(
            SystemAction.MCP_TOOLS_STALE,
            worker_name,
            "no MCP activity since daemon start — injected /mcp to force re-init",
            category=LogCategory.MCP,
        )
        # Schedule the follow-up nudge so the worker doesn't sit idle for
        # a full sweep interval after dismissing the dialog. Fire-and-
        # forget; we hold a reference in ``_mcp_followups`` so the task
        # isn't garbage-collected before it runs.
        followup = asyncio.create_task(self._followup_nudge_after_mcp(worker_name))
        self._mcp_followups.add(followup)
        followup.add_done_callback(self._mcp_followups.discard)

    async def _followup_nudge_after_mcp(self, worker_name: str) -> None:
        """Send the regular task nudge a few seconds after ``/mcp`` fires.

        Re-queries the task board at fire time so a task completed/cancelled
        in the interim is respected. Updates ``_last_nudge`` so the regular
        sweep debounce treats this as the worker's most recent nudge.
        """
        try:
            if self._mcp_followup_delay > 0:
                await asyncio.sleep(self._mcp_followup_delay)
        except asyncio.CancelledError:
            return
        if self._task_board is None:
            return
        active = self._task_board.assigned_or_active_tasks_for_worker(worker_name)
        if not active:
            return
        numbers = sorted({t.number for t in active})
        task_ids = [t.id for t in active]
        message = _nudge_message(numbers, all_active=_all_active(active))
        try:
            await self._send_to_worker(worker_name, message, _log_operator=False)
        except Exception:
            _log.warning(
                "idle_watcher: post-/mcp follow-up nudge failed for %s",
                worker_name,
                exc_info=True,
            )
            return
        now = time.monotonic()
        for tid in task_ids:
            self._last_nudge[(worker_name, tid)] = now
        self._drone_log.add(
            DroneAction.AUTO_NUDGE,
            worker_name,
            f"post-/mcp follow-up: active task(s) {', '.join(f'#{n}' for n in numbers)}",
            category=LogCategory.DRONE,
        )
