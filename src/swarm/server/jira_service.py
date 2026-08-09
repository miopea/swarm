"""JiraService — Jira import/export/sync operations extracted from SwarmDaemon."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from swarm.logging import get_logger
from swarm.tasks.task import HOLD_TAG, TaskStatus
from swarm.tasks.worklog import active_seconds

if TYPE_CHECKING:
    from swarm.drones.log import SystemLog
    from swarm.integrations.jira import JiraSyncService
    from swarm.tasks.board import TaskBoard
    from swarm.tasks.task import TaskStatus

_log = get_logger("server.jira_service")


def _is_disowned(task: Any) -> bool:
    """True for a task this swarm has RELEASED because Jira reassigned its ticket.

    Releasing changes the task's status, which by itself creates an export divergence —
    so without this the sweep keeps pushing Swarm's status onto a ticket the swarm no
    longer owns, forever, and could genuinely transition someone else's work.

    UNASSIGNED + on hold + no owner is the shape ``reconcile_ownership`` leaves behind.
    It is also the shape of an ordinary parked backlog item, which is equally not ours
    to be reporting into Jira.
    """
    return task.status == TaskStatus.UNASSIGNED and task.is_on_hold and not task.assigned_worker


class JiraService:
    """Manages Jira import/export/sync operations."""

    def __init__(
        self,
        *,
        get_jira: Callable[[], JiraSyncService],
        task_board: TaskBoard,
        broadcast_ws: Callable[[dict[str, Any]], None],
        drone_log: SystemLog,
        track_task: Callable[[asyncio.Task[object]], None],
        get_sync_interval: Callable[[], int],
        message_store: Any = None,
        task_history: Any = None,
    ) -> None:
        self._get_jira = get_jira
        self._task_board = task_board
        self._broadcast_ws = broadcast_ws
        self._drone_log = drone_log
        self._track_task = track_task
        self._get_sync_interval = get_sync_interval
        # Defaulted: three test fixtures build a JiraService for flows that never touch
        # messaging, and a missing store only costs the notification — the refresh
        # itself still happens and still logs.
        self._message_store = message_store
        self._task_history = task_history
        # Tasks we have posted a blocker note for — see reconcile_blockers.
        self._blocker_noted: set[str] = set()
        self._blocker_pass_done = False

    async def run_import(self) -> int:
        """Execute a single Jira import cycle. Returns count of new tasks."""
        from swarm.drones.log import LogCategory, SystemAction

        jira = self._get_jira()
        existing = {t.id: t for t in self._task_board.all_tasks}
        # Archived tasks keep their jira_key but are not on the board — pass the
        # full set so a re-import recognises them instead of duplicating.
        new_tasks = await jira.import_issues(
            existing, extra_known_keys=self._task_board.known_jira_keys()
        )
        for task in new_tasks:
            self._task_board.add(task)
            self._drone_log.add(
                SystemAction.TASK_CREATED,
                "system",
                detail=f"imported from Jira: {task.jira_key}",
                category=LogCategory.SYSTEM,
            )
        if new_tasks:
            self._broadcast_ws({"type": "jira_import", "count": len(new_tasks)})
        return len(new_tasks)

    async def import_one(self, issue_key: str) -> dict[str, Any] | None:
        """Import a single Jira issue by key. Returns task summary or None."""
        from swarm.drones.log import LogCategory, SystemAction

        jira = self._get_jira()
        if not jira or not jira.enabled:
            return None
        # Includes archived rows — see TaskBoard.known_jira_keys.
        existing_keys = self._task_board.known_jira_keys()
        task = await jira.import_one(issue_key, existing_keys)
        if not task:
            # Surface "already imported" so the UI can navigate to the existing task.
            for t in self._task_board.all_tasks:
                if t.jira_key == issue_key:
                    return {
                        "id": t.id,
                        "title": t.title,
                        "jira_key": t.jira_key,
                        "duplicate": True,
                    }
            # Known but not on the board = it was archived. Say so rather than
            # returning None, which the UI reads as "nothing happened" and which would
            # leave the operator re-importing an issue that is deliberately gone.
            if issue_key in existing_keys:
                return {
                    "id": "",
                    "title": "",
                    "jira_key": issue_key,
                    "duplicate": True,
                    "archived": True,
                }
            return None
        self._task_board.add(task)
        self._drone_log.add(
            SystemAction.TASK_CREATED,
            "system",
            detail=f"imported from Jira drag: {task.jira_key}",
            category=LogCategory.SYSTEM,
        )
        self._broadcast_ws({"type": "jira_import", "count": 1})
        return {
            "id": task.id,
            "title": task.title,
            "jira_key": task.jira_key,
            "duplicate": False,
        }

    async def export_status(self, task_id: str, new_status: TaskStatus) -> bool:
        """Export a task status change to Jira, recording what Jira acknowledged.

        The acknowledgement is the whole point. Before this, a failed export left no
        trace of the divergence — Jira showed a ticket open while the swarm had it
        done, and nothing could tell. Writing the confirmed status back means
        ``status != jira_exported_status`` is a comparable fact the reconciler acts on.
        """
        task = self._task_board.get(task_id)
        if not task or not task.jira_key:
            return False
        jira = self._get_jira()
        ok = await jira.export_status(task, new_status)
        if ok:
            self._task_board.record_jira_export(task_id, new_status.value)
        else:
            _log.warning(
                "jira export of #%s -> %s was not accepted; the ticket and the board "
                "now disagree and the reconciler will retry",
                task.number,
                new_status.value,
            )
        return ok

    async def refresh_task(self, task_id: str) -> bool:
        """Pull comments + attachments from Jira into an existing task.

        Returns ``True`` when the task was found, linked to Jira, and the
        sync succeeded. The board is persisted via ``TaskBoard.update`` so
        the change survives daemon restarts and the WS clients see it.
        """
        task = self._task_board.get(task_id)
        if not task or not task.jira_key:
            return False
        jira = self._get_jira()
        if not jira or not jira.enabled:
            return False
        ok = await jira.refresh_task(task)
        if not ok:
            return False
        # Persist refreshed description + attachments through the board so
        # the change is written to disk and broadcast to WS clients.
        self._task_board.update(
            task_id,
            description=task.description,
            attachments=list(task.attachments),
        )
        self._broadcast_ws({"type": "task_updated", "task_id": task_id})
        return True

    def fire_jira(self, task_id: str, action: str, coro_factory: Callable[..., Any]) -> None:
        """Schedule a Jira operation as fire-and-forget background task.

        Shared guard: checks Jira is enabled and task has a Jira key.
        """
        jira = self._get_jira()
        if not jira or not jira.enabled:
            return
        task = self._task_board.get(task_id)
        if not task or not task.jira_key:
            return

        async def _do() -> None:
            try:
                # THE RETURN VALUE IS LOAD-BEARING. This used to ignore it, so an
                # export that ran and simply did not take produced no exception, no
                # log and no record — the silent-success shape (#1159) on the one path
                # where the two systems can drift apart unnoticed.
                result = await coro_factory(jira, task)
                if result is False:
                    _log.warning(
                        "jira %s for #%s returned False — not an error, but it did not "
                        "take; leaving the task out of sync for the reconciler",
                        action,
                        task.number,
                    )
            except Exception:
                _log.warning("jira %s failed for %s", action, task_id, exc_info=True)

        self._track_task(asyncio.create_task(_do()))

    def fire_export(self, task_id: str, new_status: str) -> None:
        """Schedule Jira status export as fire-and-forget background task."""
        from swarm.tasks.task import TaskStatus

        status = TaskStatus(new_status)
        self.fire_jira(task_id, "export", lambda jira, task: jira.export_status(task, status))

    def fire_assign(self, task_id: str) -> None:
        """Schedule Jira issue assignment as fire-and-forget background task."""
        self.fire_jira(task_id, "assign", lambda jira, task: jira.assign_to_me(task))

    def fire_completion(self, task_id: str) -> None:
        """Schedule Jira completion comment as fire-and-forget background task."""
        self.fire_jira(
            task_id,
            "comment",
            lambda jira, task: jira.post_completion_comment(task),
        )
        self.fire_worklog(task_id)

    def fire_worklog(self, task_id: str) -> None:
        """Log the time this task was actually worked against its ticket.

        Fired alongside the completion comment, but as its own background task: a
        worklog failure must not swallow the comment, nor the reverse.

        The duration is reconstructed from task HISTORY rather than from
        ``completed_at - started_at`` — ``activate`` resets ``started_at``, so that
        subtraction reports only the final stretch and would under-bill any task that
        was parked and resumed. Where the history cannot substantiate a duration,
        nothing is logged: an invented timesheet entry is worse than an absent one.
        """

        async def _work(jira: Any, task: Any) -> bool:
            seconds = self._worked_seconds(task)
            if not seconds:
                _log.debug("no substantiated active time for #%s; nothing logged", task.number)
                return False
            return await jira.log_work(task, seconds)

        self.fire_jira(task_id, "worklog", _work)

    def _worked_seconds(self, task: Any) -> float | None:
        """Reconstructed ACTIVE time for a task, or None when it cannot be substantiated."""
        history = getattr(self, "_task_history", None)
        if history is None:
            return None
        return active_seconds(history.get_events(task.id, limit=500))

    # A task closed while its project was unconfirmed has its time refused, and nothing
    # retried once the operator confirmed — the work was simply never billed. Bounded so
    # the backfill cannot become a per-cycle scan of the whole board.
    _WORKLOG_BACKFILL_WINDOW = 7 * 24 * 3600
    _WORKLOG_BACKFILL_PER_CYCLE = 10

    async def backfill_worklogs(self) -> int:
        """Log time for recently-closed tasks whose worklog never made it. Returns count.

        THE GAP: log_work refuses when the ticket's project is unconfirmed, which is
        correct — a worklog is a write to a shared tracker. But nothing ever tried again,
        so every task closed before the operator confirmed that project lost its time
        permanently. Confirming a workflow should not silently forfeit the work already
        done under it.

        IDEMPOTENT BY REUSE, not by new bookkeeping: log_work already reads the ticket's
        existing worklogs and skips its own marker, so re-offering a task that was
        already billed writes nothing. That also means this needs no "already backfilled"
        flag, and it survives a restart.

        BOUNDED TWICE — a seven-day window and a per-cycle cap — because the check costs
        one worklog read per candidate. Unbounded, a board with hundreds of closed linked
        tasks would re-read all of them every five minutes forever.
        """
        jira = self._get_jira()
        if not jira or not jira.enabled:
            return 0
        cutoff = time.time() - self._WORKLOG_BACKFILL_WINDOW
        candidates = [
            t
            for t in self._task_board.all_tasks
            if t.jira_key and t.status is TaskStatus.DONE and (t.completed_at or 0) >= cutoff
        ]
        candidates.sort(key=lambda t: t.completed_at or 0, reverse=True)

        written = 0
        for task in candidates[: self._WORKLOG_BACKFILL_PER_CYCLE]:
            seconds = self._worked_seconds(task)
            if not seconds:
                continue
            try:
                if await jira.log_work(task, seconds):
                    written += 1
                    _log.warning(
                        "jira: backfilled a missing worklog on %s for #%s",
                        task.jira_key,
                        task.number,
                    )
            except Exception:
                _log.warning("jira: worklog backfill for %s raised", task.jira_key, exc_info=True)
        return written

    def plan_exports(self) -> list[dict[str, Any]]:
        """What a reconcile WOULD change, without touching Jira. The dry run.

        Returns one entry per outstanding task: its number, ticket, the status Jira
        last acknowledged, the status it would be moved to, and whether that project
        has been confirmed.

        Exists because enabling an integration must not be a bulk write to someone
        else's tracker. On 2026-08-07 a schema change made 25 tasks look unacknowledged
        and the reconciler transitioned 14 real tickets before anyone had looked. A
        settings toggle should never have that blast radius.
        """
        board = self._task_board
        plan: list[dict[str, Any]] = []
        for task in board.all_tasks:
            if not task.jira_key or task.jira_exported_status == task.status.value:
                continue
            project = task.jira_key.split("-")[0] if "-" in task.jira_key else ""
            plan.append(
                {
                    "number": task.number,
                    "jira_key": task.jira_key,
                    "project": project,
                    "acknowledged": task.jira_exported_status or None,
                    "would_become": task.status.value,
                    "project_confirmed": self._project_confirmed(project),
                }
            )
        return plan

    def _project_confirmed(self, project_key: str) -> bool:
        """Has this project's discovered workflow been confirmed by the operator?"""
        cfg = getattr(self._get_jira(), "_config", None)
        if cfg is None or not hasattr(cfg, "is_confirmed"):
            return True  # pre-v2 config: do not gate an install that has no concept of it
        return bool(cfg.is_confirmed(project_key))

    async def reconcile_blockers(self) -> int:
        """Make each linked ticket state whether Swarm is blocked on it. Returns count.

        THE GAP: when a worker blocks, the ticket said nothing. A PM looking at the
        board saw idle work with no explanation, and the reason lived only inside Swarm.
        This is what makes Swarm legible to people who never open it.

        RECONCILED, not hooked onto the block/unblock call sites. There are four of
        those (two MCP verbs, the coordinator, the board) and hooking each means the
        fifth one added later silently does not report. Comparing state each cycle also
        self-heals a note that failed to post, which a fire-and-forget hook cannot.

        Only tasks whose blocked-ness has something to say: a task that is not blocked
        and never had a note posted produces no comment at all.
        """
        jira = self._get_jira()
        if not jira or not jira.enabled:
            return 0

        # ONE full pass per daemon start, cheap thereafter.
        #
        # Reading every open linked ticket's comments every cycle cost one API call per
        # task forever, to discover nothing for tasks that have never been blocked. But
        # narrowing purely to "is blocked now" would strand a note left by a PREVIOUS
        # daemon instance, since the set of known notes lives in memory — so the first
        # pass after a restart still checks everything and rebuilds it.
        known = self._blocker_noted
        first_pass = not self._blocker_pass_done

        updated = 0
        for task in self._task_board.all_tasks:
            if not task.jira_key or task.status in (TaskStatus.DONE, TaskStatus.FAILED):
                continue
            blocked_now = task.status is TaskStatus.BLOCKED
            if not first_pass and not blocked_now and task.id not in known:
                # Not blocked and carries no note we posted: nothing to say, and asking
                # Jira would only confirm that.
                continue
            reason = ""
            if task.status is TaskStatus.BLOCKED:
                reason = (
                    getattr(task, "block_reason", "")
                    or getattr(task, "external_blocker_ref", "")
                    or "blocked; no reason recorded"
                )
            try:
                changed = await jira.sync_blocker_note(task, reason)
                if reason:
                    known.add(task.id)
                else:
                    known.discard(task.id)
                if changed:
                    updated += 1
                    _log.warning(
                        "jira: %s blocker note %s — #%s",
                        task.jira_key,
                        "posted" if reason else "cleared",
                        task.number,
                    )
            except Exception:
                _log.warning("jira: blocker note for %s raised", task.jira_key, exc_info=True)
        self._blocker_pass_done = True
        return updated

    async def refresh_linked_tasks(self) -> int:
        """Pull new comments and attachments onto OPEN linked tasks. Returns count.

        THE GAP THIS CLOSES. ``import_issues`` dedupes on ``jira_key`` and SKIPS tasks
        that already exist, so comments and attachments were mirrored exactly once, at
        creation, and never again. On a service desk the comment thread IS the
        requirement: a stakeholder writes "actually the customer needs X" on the ticket
        and the worker never saw it, because nothing ever looked again.

        Only OPEN tasks, and only tasks this swarm still owns — a released task is
        somebody else's problem now, and a finished one cannot act on new information.

        The refresh itself is additive (see ``refresh_synced_content``); this layer adds
        the SIGNAL, because mirroring a comment into a description nobody re-reads is
        only half an answer. The assigned worker gets a message rather than a PTY
        interrupt: it lands in their inbox instead of cutting across whatever they are
        mid-way through saying.
        """
        jira = self._get_jira()
        if not jira or not jira.enabled:
            return 0
        candidates = [
            t
            for t in self._task_board.all_tasks
            if t.jira_key
            and t.status not in (TaskStatus.DONE, TaskStatus.FAILED)
            and not _is_disowned(t)
        ]
        # ONE search for the whole set instead of one call per task — see
        # fetch_synced_fields. A task missing from the response is simply not refreshed
        # this cycle, which is a no-op rather than a truncation.
        prefetched = await jira.fetch_synced_fields([t.jira_key for t in candidates])

        updated = 0
        for task in candidates:
            fields = prefetched.get(task.jira_key)
            if fields is None:
                continue
            # PERSIST ON CHANGE, NOTIFY ON NEWS — two different questions, and
            # conflating them silently lost data. refresh_synced_content mutates the
            # board's own SwarmTask object, so a change was visible in the daemon and
            # written to the database only when there ALSO happened to be a notifiable
            # comment. A reporter/due-date update, or a ticket whose only new comment is
            # Swarm's own, mutated memory and never persisted — surviving until some
            # unrelated board write flushed it, and lost on restart.
            #
            # Observed on WWD-6743: the API served a description with the synced block
            # while the database had none.
            before = task.description
            try:
                latest = await jira.refresh_synced_content(task, prefetched=fields)
            except Exception:
                _log.warning("jira: refresh of %s raised", task.jira_key, exc_info=True)
                continue
            if task.description != before:
                self._task_board.update(task.id, description=task.description)
                updated += 1
            if latest:
                self._notify_of_jira_update(task, latest)
        if updated:
            self._broadcast_ws({"type": "task_update"})
        return updated

    def _notify_of_jira_update(self, task: Any, latest: str) -> None:
        """Tell the assigned worker their ticket changed. Best effort."""
        snippet = latest if len(latest) <= 400 else latest[:400] + "…"
        _log.warning(
            "jira: %s has new activity — #%s updated. Latest: %s",
            task.jira_key,
            task.number,
            snippet.replace("\n", " ")[:160],
        )
        worker = getattr(task, "assigned_worker", "") or ""
        if not worker or self._message_store is None:
            return
        try:
            # send() takes KEYWORDS, not a Message. Passing a Message object raises
            # TypeError, which a surrounding try/except then swallows — see the
            # daemon's queen-drift notification, which had never once fired.
            self._message_store.send(
                sender="jira",
                recipient=worker,
                msg_type="finding",
                content=(
                    f"{task.jira_key} (your task #{task.number}) has new activity in "
                    f"Jira. Latest comment — {snippet}\n\n"
                    f"The full thread is mirrored under '--- Jira sync ---' in the "
                    f"task description. If this changes what the task needs, say so "
                    f"before continuing rather than finishing the old scope."
                ),
            )
        except Exception:
            _log.debug("could not message %s about %s", worker, task.jira_key, exc_info=True)

    async def reconcile_ownership(self) -> int:
        """Release tasks whose Jira ticket was reassigned away from this dev.

        THE FAILURE THIS PREVENTS. Routing is by ``assignee = currentUser()`` — that is
        the whole reason Jira can be enabled for every dev without them colliding. But
        nothing re-checked it after import, so handing a ticket over in Jira left BOTH
        swarms holding the task: the new owner's imports it, the old owner's keeps
        working it, and they race to transition the same ticket. That is precisely the
        duplication assignee routing exists to prevent, arriving through the back door.

        Only OPEN tasks are checked. A finished task's ownership is history, and
        re-litigating it would churn the board for no one's benefit.

        The task is RELEASED and put on HOLD rather than deleted or completed: the work
        is not done and the link is still true, so it stays visible and traceable while
        no longer being anyone's active work. HOLD matters — a bare release returns it to
        UNASSIGNED, where the auto-assign drone would hand it to another worker in THIS
        swarm, which is the same wrong answer with a different name.
        """
        jira = self._get_jira()
        if not jira or not jira.enabled:
            return 0
        open_linked = [
            t
            for t in self._task_board.all_tasks
            if t.jira_key and t.status not in (TaskStatus.DONE, TaskStatus.FAILED)
        ]
        if not open_linked:
            return 0

        moved = await jira.find_reassigned(open_linked)
        released = 0
        for task, new_owner in moved:
            owner = task.assigned_worker or "nobody"
            # Captured BEFORE the release. Reading it afterwards made every message say
            # "It was unassigned" — the release had already made that true, so the line
            # reported its own effect instead of what the operator needed to know.
            was_status = task.status.value
            if not self._task_board.release(task.id):
                continue
            # HOLD via update(tags=...) — the board has no add-tag verb, and replacing
            # the list wholesale would drop the task's existing tags.
            current = list(getattr(self._task_board.get(task.id), "tags", []) or [])
            if HOLD_TAG not in current:
                self._task_board.update(task.id, tags=[*current, HOLD_TAG])
            released += 1
            _log.warning(
                "jira: %s is no longer assigned to you in Jira (now: %s) — #%s released "
                "from %s and put on hold. It was %s. Nothing was written to Jira.",
                task.jira_key,
                new_owner or "unassigned",
                task.number,
                owner,
                was_status,
            )
            self._audit_reassignment(task, owner, new_owner)
        if released:
            self._broadcast_ws({"type": "task_update"})
        return released

    def _audit_reassignment(self, task: Any, owner: str, new_owner: str) -> None:
        """Record a release in the drone log and task history — best effort.

        Separate from the mutation so an audit failure cannot undo a correct release,
        and so ``reconcile_ownership`` stays under the complexity gate.
        """
        from swarm.drones.log import LogCategory, SystemAction

        detail = (
            f"{task.jira_key} reassigned in Jira to {new_owner or 'nobody'} — released from {owner}"
        )
        try:
            if self._drone_log is not None:
                self._drone_log.add(
                    SystemAction.OPERATOR, "system", detail, category=LogCategory.TASK
                )
        except Exception:
            _log.debug("drone log write failed for %s", task.jira_key, exc_info=True)

    async def _record_existing_agreement(self, jira: Any, tasks: list[Any]) -> int:
        """Record tasks whose Jira ticket is ALREADY in the desired state. Returns count.

        A pure read followed by a local write. Split out to keep ``reconcile_exports``
        under the complexity gate.

        WHY IT RUNS ON UNCONFIRMED PROJECTS. The confirmation gate stops an unattended
        sweep from BULK WRITING to a shared tracker. A comparison writes nothing, so
        gating it achieves no safety and costs real noise: MTR-11806 was done in Swarm
        and already `Done` in Jira, and warned every five minutes forever about a
        divergence that did not exist.
        """
        agreed = 0
        for task in tasks:
            try:
                if await jira.agrees_already(task, task.status):
                    self._task_board.record_jira_export(task.id, task.status.value)
                    agreed += 1
            except Exception:
                _log.warning(
                    "jira reconcile: agreement check for %s raised",
                    task.jira_key,
                    exc_info=True,
                )
        return agreed

    async def reconcile_exports(self) -> int:
        """Re-export every task whose status Jira has not acknowledged. Returns count.

        WHY THIS EXISTS AND THE RETRY ALONE DOES NOT. Exports are fire-and-forget: the
        caller gets no signal, and before this nothing compared the two systems
        afterwards. A single dropped export left Jira showing a ticket open while the
        swarm had it done — permanently, because nothing ever looked again. That is the
        same architecture as the task panel that only reacted to a pushed frame, and it
        failed the same way.

        Comparing ``status`` against ``jira_exported_status`` makes the divergence a
        FACT rather than an event that can be missed, so a lost export costs one sync
        interval instead of lasting until someone notices Jira is wrong.

        Only touches tasks that have a jira_key; a task the swarm owns alone is not
        out of sync with anything.
        """
        jira = self._get_jira()
        if not jira or not jira.enabled:
            return 0
        # PAIRS ALREADY REFUSED ARE NOT RETRIED. Found the hard way within minutes of
        # shipping this: 11 tickets were retried every sync interval forever, two
        # WARNING lines each, because Jira cannot transition them at all —
        #   "no transition to 'Done' found for IS-10278 (available: ['Waiting for
        #    support'])"
        # They are already closed in Jira; the empty default on jira_exported_status
        # just made every historical task LOOK unacknowledged. That is a stable
        # property of the ticket's workflow, not a transient error, so retrying it on a
        # loop only hammers the API and buries real divergence in noise — the exact
        # failure this file's own test asserts against for local tasks.
        #
        # Keyed on (task, target status) so a genuine status CHANGE retries: the pair
        # differs, and the new target may well be reachable. Held in memory
        # deliberately — one retry per daemon start is a cheap way to recover from a
        # workflow or permission change without another column.
        refused = getattr(self, "_export_refused", None)
        if refused is None:
            refused = self._export_refused = set()
        stale = [
            t
            for t in self._task_board.all_tasks
            if t.jira_key
            and t.jira_exported_status != t.status.value
            and (t.id, t.status.value) not in refused
            and not _is_disowned(t)
        ]

        # UNCONFIRMED PROJECTS ARE PLANNED, NOT WRITTEN (v2 phase 3). The sweep is a
        # BULK convergence: it can transition many tickets at once, on its own
        # schedule, with nobody watching — which is exactly what happened when a
        # migration made 25 tasks look unacknowledged and 14 real tickets moved.
        #
        # Individual exports caused by a real task transition are NOT gated: those are
        # a direct consequence of an action the operator or a worker just took, and
        # blocking them would break a working integration on upgrade. The dangerous
        # thing is the unattended batch, so that is what needs a go-ahead.
        unconfirmed = [
            t
            for t in stale
            if not self._project_confirmed(t.jira_key.split("-")[0] if "-" in t.jira_key else "")
        ]
        if unconfirmed:
            by_project: dict[str, int] = {}
            for t in unconfirmed:
                key = t.jira_key.split("-")[0] if "-" in t.jira_key else "?"
                by_project[key] = by_project.get(key, 0) + 1
            _log.warning(
                "jira reconcile: %d task(s) NOT written because their project's "
                "workflow is unconfirmed (%s) — confirm the discovered mapping to let "
                "the sweep converge them",
                len(unconfirmed),
                ", ".join(f"{k}: {v}" for k, v in sorted(by_project.items())),
            )
            skipped_ids = {t.id for t in unconfirmed}
            stale = [t for t in stale if t.id not in skipped_ids]
            # ...but a task Jira ALREADY AGREES WITH is reconciled by comparison, which
            # is not a write and so is not what the confirmation gate is protecting.
            # Without this, MTR-11806 — done in Swarm and already `Done` in Jira —
            # warned every five minutes forever about a divergence that did not exist.
            # The gate stops the sweep from CHANGING an unconfirmed project's tickets;
            # it should not stop it from noticing they need no change.
            agreed = await self._record_existing_agreement(jira, unconfirmed)
            if agreed:
                _log.warning(
                    "jira reconcile: %d of those already match Jira and were recorded "
                    "without any write",
                    agreed,
                )
        skipped = sum(
            1
            for t in self._task_board.all_tasks
            if t.jira_key
            and t.jira_exported_status != t.status.value
            and (t.id, t.status.value) in refused
        )
        if skipped:
            _log.info(
                "jira reconcile: skipping %d task(s) Jira has already refused this "
                "session; they retry after a restart",
                skipped,
            )
        if not stale:
            return 0
        _log.warning(
            "jira reconcile: %d task(s) whose status Jira has not acknowledged — %s",
            len(stale),
            ", ".join(
                f"#{t.number} ({t.jira_exported_status or 'never'} -> {t.status.value})"
                for t in stale[:10]
            ),
        )
        repaired = 0
        for task in stale:
            try:
                if await self.export_status(task.id, task.status):
                    repaired += 1
                else:
                    # Refused, not errored. Record the pair so the next cycle does not
                    # repeat it — see the note above.
                    refused.add((task.id, task.status.value))
            except Exception:
                _log.warning("jira reconcile: export of #%s raised", task.number, exc_info=True)
                refused.add((task.id, task.status.value))
        return repaired

    async def sync_loop(self) -> None:
        """Periodically import Jira issues, and reconcile outstanding exports."""
        try:
            while True:
                interval = self._get_sync_interval()
                await asyncio.sleep(interval)
                await self.run_import()
                # OWNERSHIP FIRST, and the order is load-bearing. Run it the other way
                # round and the export sweep writes to a ticket, then the ownership sweep
                # discovers two seconds later that it was never ours — observed live on
                # WWD-6715 at 23:43:21 vs 23:43:23. Establish what is ours, then act.
                await self.reconcile_ownership()
                # Import alone leaves the OUTBOUND direction unchecked, which is where
                # the two systems actually drifted.
                await self.reconcile_exports()
                # Import creates a task once and never looks again; this is what keeps a
                # live ticket's comments reaching the worker working it.
                await self.refresh_linked_tasks()
                # ...and a blocked task says nothing to anyone outside Swarm unless the
                # ticket itself is told.
                await self.reconcile_blockers()
                # ...and time refused while a project was unconfirmed is otherwise lost
                # for good, since nothing retried after the operator confirmed it.
                await self.backfill_worklogs()
        except asyncio.CancelledError:
            return
