"""TaskCoordinator — task lifecycle orchestration (assign / start / complete / handoff).

Extracted from :class:`~swarm.server.daemon.SwarmDaemon` (audit
finding #1, Phase 3 of ``docs/specs/daemon-god-object-refactor.md``).
Owns the methods that drive a task through its lifecycle:

* :meth:`assign_task` — queue an UNASSIGNED task onto a worker.
* :meth:`start_task` — send an ASSIGNED task into the worker's PTY
  with the rendered prompt + recalled playbooks + optional native
  ``/goal`` seeding.
* :meth:`assign_and_start_task` — convenience wrapper used by drones
  and Queen.
* :meth:`complete_task` — finish a task, fire all the post-completion
  fan-out (notifications, jira sync, cross-project notify,
  email-reply draft, attention-thread cleanup, post-ship self-loop,
  playbook synthesis).
* :meth:`_spawn_handoff_task` (#442) — promote a cross-worker
  message into a tracked task assigned to the recipient.
* :meth:`_auto_start_next_assigned` / :meth:`_auto_resolve_attention_for_task`
  — post-completion side effects called from ``complete_task``.
* :meth:`_check_ownership` — file-ownership gate consulted at assign
  time.
* :meth:`_send_completion_reply` / :meth:`retry_draft_reply` — email
  reply path for tasks originating from a Microsoft Graph message.
* :meth:`_maybe_seed_goal` — optional native ``/goal`` arming after
  a successful dispatch.

The coordinator uses a back-reference to :class:`SwarmDaemon`
(``self._d``) rather than a long dependency-bundle dataclass.  Same
pattern :class:`TestRunner` already uses — these methods touch
~15+ daemon attributes (task_board, task_history, drone_log,
notification_bus, jira_svc, graph_mgr, pilot, pipeline_engine,
playbook_ops, queen_chat, file_ownership, send_to_worker,
push_notification, _track_task, _require_worker, _require_task,
get_worker, broadcast_ws, email, …), and threading each through a
dedicated dataclass would obscure rather than reveal the wiring.

Daemon keeps the existing public method names as thin proxy shims
so callers (routes, MCP tools, tests) don't need to know the
methods moved.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from swarm.drones.log import DroneAction, LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.pty.process import ProcessError
from swarm.server.task_utils import log_task_exception as _log_task_exception
from swarm.tasks.history import TaskAction
from swarm.tasks.task import TaskStatus

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon
    from swarm.tasks.task import SwarmTask


_log = get_logger("server.task_coordinator")


class TaskCoordinator:
    """Lifecycle orchestrator for tasks moving through the swarm.

    Constructed once by :class:`SwarmDaemon` and bound to it via
    ``self._d``.  Every method here is a behaviour-preserving move
    of the same-named (or ``_``-prefixed) daemon method; refactor
    audit #1, Phase 3.
    """

    def __init__(self, daemon: SwarmDaemon) -> None:
        self._d = daemon

    # ----- assign -----

    def check_ownership(self, worker_name: str) -> None:
        """Check file ownership conflicts; raise in HARD_BLOCK, warn in WARNING mode."""
        from swarm.coordination.ownership import OwnershipMode

        ownership = getattr(self._d, "file_ownership", None)
        if ownership is None or ownership.mode == OwnershipMode.OFF:
            return
        worker_files = ownership.get_worker_files(worker_name)
        if not worker_files:
            return
        overlaps = ownership.check_overlap(worker_name, worker_files)
        if not overlaps:
            return
        overlap_str = ", ".join(f"{o.file_path} (owned by {o.owner})" for o in overlaps[:3])
        if ownership.mode == OwnershipMode.HARD_BLOCK:
            from swarm.server.daemon import SwarmOperationError

            raise SwarmOperationError(f"File ownership conflict: {overlap_str}")
        _log.warning("ownership overlap for %s: %s", worker_name, overlap_str)
        self._d.drone_log.add(
            SystemAction.OVERSIGHT_SIGNAL,
            worker_name,
            f"ownership warning: {overlap_str}",
            category=LogCategory.WORKER,
        )

    async def assign_task(
        self,
        task_id: str,
        worker_name: str,
        actor: str = "user",
    ) -> bool:
        """Assign (queue) a task to a worker without sending it.

        The task moves to ASSIGNED status. Call :meth:`start_task` to
        actually send the task message to the worker's PTY.
        """
        from swarm.server.daemon import TaskOperationError

        d = self._d
        d._require_worker(worker_name)
        self.check_ownership(worker_name)

        task = d.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        if not task.is_available:
            raise TaskOperationError(
                f"Task '{task_id}' is not available ({task.status.value})", status_code=409
            )

        result = d.task_board.assign(task_id, worker_name)
        if result:
            d.task_history.append(task_id, TaskAction.ASSIGNED, actor=actor, detail=worker_name)
            d.drone_log.add(
                SystemAction.TASK_ASSIGNED,
                worker_name,
                f"queued: {task.title}",
                category=LogCategory.TASK,
                metadata={"task_id": task.id},
            )
            if actor == "user":
                d.drone_log.add(
                    DroneAction.OPERATOR,
                    worker_name,
                    f"task queued: {task.title}",
                    category=LogCategory.OPERATOR,
                )
        return result

    # ----- start -----

    def _activate_with_history(self, task_id: str, worker_name: str, actor: str) -> bool:
        """Route a task to ACTIVE through the single board chokepoint
        (``board.activate``) and log the history/jira side-effects for the
        demotions + the activation. Returns False if the task isn't startable
        (operator-action / vanished). Split out of ``start_task`` to keep it
        under the complexity gate (#611 P3).
        """
        d = self._d
        demoted = d.task_board.activate(task_id)
        if demoted is None:
            return False
        for demoted_id in demoted:
            d.task_history.append(
                demoted_id,
                TaskAction.UNASSIGNED,
                actor="system",
                detail=f"demoted to ASSIGNED — {worker_name} started newer task",
            )
            d.jira_svc.fire_export(demoted_id, "assigned")
        d.task_history.append(task_id, TaskAction.STARTED, actor=actor, detail=worker_name)
        d.jira_svc.fire_export(task_id, "active")
        return True

    def _augment_with_recall(self, task: SwarmTask, worker_name: str, msg: str) -> str:
        """Append recalled procedural memory to a dispatch message: the playbook
        block (existing) plus, when ``learning_preload`` is on, the top relevant
        prior learnings (P3) so the worker starts with them instead of pulling
        via ``swarm_get_learnings``. Both are best-effort and exception-guarded
        inside the ops layer."""
        d = self._d
        pb_block = d.playbook_ops.recall_for_task(task, worker_name)
        if pb_block:
            msg = f"{msg}\n{pb_block}"
        if d.config.drones.learning_preload:
            learn_block = d.playbook_ops.recall_learnings_for_task(task)
            if learn_block:
                msg = f"{msg}\n{learn_block}"
        return msg

    async def start_task(
        self,
        task_id: str,
        actor: str = "user",
        message: str | None = None,
    ) -> bool:
        """Send an ASSIGNED task to the worker's PTY and start it.

        If *message* is provided (e.g. from a Queen proposal), it is
        appended as context to the auto-generated task message.
        """
        from swarm.providers import get_provider
        from swarm.server.daemon import TaskOperationError
        from swarm.server.messages import build_task_message

        d = self._d
        task = d.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        if task.status != TaskStatus.ASSIGNED:
            raise TaskOperationError(
                f"Task '{task_id}' must be ASSIGNED to start (is {task.status.value})",
                status_code=409,
            )
        worker_name = task.assigned_worker
        if not worker_name:
            raise TaskOperationError(f"Task '{task_id}' has no assigned worker")

        d._require_worker(worker_name)

        worker_prov = get_provider(d._require_worker(worker_name).provider_name)
        msg = build_task_message(
            task,
            supports_slash_commands=worker_prov.supports_slash_commands,
            plan_mode_for_user_requests=d.config.drones.user_request_plan_mode,
            enrich_dispatch=d.config.drones.dispatch_enrichment,
        )
        if message:
            msg = f"{msg}\n\nQueen context: {message}"

        msg = self._augment_with_recall(task, worker_name, msg)

        _log.info(
            "starting task %s on %s (%d chars)",
            task_id[:8],
            worker_name,
            len(msg),
        )

        try:
            await d.send_to_worker(worker_name, msg, _log_operator=False)
            if "\n" in msg or len(msg) > 200:
                worker = d._require_worker(worker_name)
                await asyncio.sleep(0.3)
                proc = worker.process
                if proc and not proc.is_user_active:
                    await proc.send_enter()
        except (TimeoutError, ProcessError, OSError):
            _log.warning("failed to send task message to %s", worker_name, exc_info=True)
            # Task #527: auto-handoff tasks (the inter-worker watcher's
            # #442 spawn output, tagged "auto-handoff") are worker-
            # specific by construction. The watcher resolved THIS
            # recipient from a direct message addressed to them, so
            # routing the task to anyone else is a bug — yet today's
            # unassign-on-send-failure drops the task into the pending
            # pool where the queen's auto-assigner can pick it up and
            # route it to a random idle worker. That's the #525 misroute
            # pattern (platform → rcg-networks message #1156 ended up
            # completed by public-website after rcg-networks's send
            # failed). KEEP the task ASSIGNED to the original recipient
            # so the IdleWatcher's nudge-on-RESTING-with-ASSIGNED path
            # retries delivery once the recipient's PTY recovers. The
            # auto-spawn's _spawned_msg_ids dedup prevents re-spawning
            # the same handoff in the interim.
            is_auto_handoff = "auto-handoff" in (task.tags or [])
            if is_auto_handoff:
                d.task_history.append(
                    task_id,
                    TaskAction.EDITED,
                    actor="system",
                    detail=(
                        f"send failed to {worker_name} — keeping ASSIGNED "
                        f"(auto-handoff tasks are not requeueable)"
                    ),
                )
            else:
                d.task_board.unassign(task_id)
                d.task_history.append(
                    task_id,
                    TaskAction.UNASSIGNED,
                    actor="system",
                    detail=f"send failed to {worker_name} — returned to pending",
                )
            d.broadcast_ws(
                {"type": "task_send_failed", "worker": worker_name, "task_title": task.title}
            )
            buzz_detail = task.title + (
                " [auto-handoff: kept ASSIGNED for retry]" if is_auto_handoff else ""
            )
            d.drone_log.add(
                SystemAction.TASK_SEND_FAILED,
                worker_name,
                buzz_detail,
                category=LogCategory.TASK,
                is_notification=True,
            )
            return False

        # #611 P3: activate via the single board chokepoint + log history/jira.
        if not self._activate_with_history(task_id, worker_name, actor):
            # Not startable (operator-action or vanished). Defensive — status
            # was checked above.
            return False
        if d.pilot:
            d.pilot.wake_worker(worker_name)
        d.drone_log.add(
            DroneAction.OPERATOR if actor == "user" else DroneAction.AUTO_ASSIGNED,
            worker_name,
            f"task started: {task.title}",
            category=LogCategory.TASK,
        )

        await self._maybe_seed_goal(task, worker_name, worker_prov)
        return True

    async def _maybe_seed_goal(
        self, task: SwarmTask, worker_name: str, worker_prov: object
    ) -> None:
        """Seed a native ``/goal`` from the task's acceptance criteria.

        Hands the criteria to the provider's own ``/goal`` evaluator
        (Claude Code / Codex) so it runs the keep-working loop — Swarm
        builds no evaluator. Called only from :meth:`start_task` (the
        dispatch boundary), so it is naturally set-once-per-dispatch:
        idle-watcher nudges go through ``send_to_worker`` directly and
        never re-arm the goal. No-op unless the feature flag is on, the
        task has acceptance criteria, and the worker's provider has a
        native ``/goal``. Best-effort — the task message already shipped
        and the task is started, so a ``/goal`` send failure must not
        unwind that.
        """
        d = self._d
        drones = d.config.drones
        if not (
            drones.native_goal_enabled
            and task.acceptance_criteria
            and getattr(worker_prov, "supports_native_goal", False)
        ):
            return
        # Task #524: cross-project tasks ship the to-worker's criteria.
        # If for any reason the dispatch lands on the from-worker (the
        # requester's repo doesn't host the implementation), seeding
        # ``/goal`` there pins the worker into a Stop-hook loop on
        # criteria it physically can't satisfy. Concrete bite: cross-
        # project task #523 (from=rcg-networks → to=platform) burned
        # ~$10 / 257K output tokens on rcg-networks before reassignment.
        # Skip and log so the operator can see what was suppressed.
        if (
            task.is_cross_project
            and task.source_worker
            and task.target_worker
            and task.source_worker != task.target_worker
            and worker_name == task.source_worker
        ):
            d.drone_log.add(
                SystemAction.GOAL_SKIPPED,
                worker_name,
                f"#{task.number}: cross-project from={task.source_worker} "
                f"to={task.target_worker} — criteria belong to to-worker, skipped",
                category=LogCategory.TASK,
                metadata={"task_id": task.id, "task_number": task.number},
            )
            return
        try:
            from swarm.server.messages import render_goal_condition

            condition = render_goal_condition(
                task.acceptance_criteria, max_turns=drones.native_goal_max_turns
            )
            if not condition:
                return
            await d.send_to_worker(worker_name, f"/goal {condition}", _log_operator=False)
            await asyncio.sleep(0.3)
            proc = d._require_worker(worker_name).process
            if proc and not proc.is_user_active:
                await proc.send_enter()
            d.drone_log.add(
                SystemAction.GOAL_SET,
                worker_name,
                f"#{task.number} goal armed: {condition[:120]}",
                category=LogCategory.TASK,
                metadata={"task_id": task.id, "task_number": task.number},
            )
        except Exception:
            _log.warning(
                "native /goal seeding failed for #%s on %s",
                task.number,
                worker_name,
                exc_info=True,
            )

    # ----- handoff -----

    def _buzz_suppressed_duplicate(
        self, recipient: str, sender: str, dup: object, msg_id: object
    ) -> None:
        """#913: buzz-log a handoff spawn suppressed as duplicate work."""
        dup_num = getattr(dup, "number", "?")
        try:
            self._d.drone_log.add(
                DroneAction.AUTO_HANDOFF_TASK,
                recipient,
                (
                    f"suppressed duplicate: handoff from {sender} (msg #{msg_id}) duplicates "
                    f"{recipient}'s active task #{dup_num} — not re-spawned, source marked read"
                ),
                category=LogCategory.DRONE,
            )
        except Exception:
            _log.debug("spawn_handoff: suppression buzz-log failed", exc_info=True)

    async def spawn_handoff_task(self, recipient: str, message: object) -> bool:
        """task #442: turn an actionable cross-worker handoff to an idle,
        task-less recipient into a *tracked* task assigned to that
        recipient — so the IdleWatcher carries it to completion instead
        of the handoff living only in a skip-prone one-shot nudge that a
        missed turn or a daemon restart silently loses (the #985 →
        realtruth incident; #441 was the manual backfill this removes
        the need for).

        Wired into ``InterWorkerMessageWatcher`` via
        ``set_idle_nudge_sender``. Returns True when a task was created
        and assigned. Idempotency is handled upstream: the watcher
        de-dupes per message id, and once this assignment lands the
        recipient has an active task so the watcher's ``has_task`` gate
        stops it re-firing.
        """
        d = self._d
        board = getattr(d, "task_board", None)
        if board is None:
            return False
        sender = getattr(message, "sender", "") or "?"
        msg_type = getattr(message, "msg_type", "dependency")
        msg_id = getattr(message, "id", None)
        content = (getattr(message, "content", "") or "").strip()
        first_line = content.splitlines()[0] if content else "(no content)"
        title = f"Handoff from {sender}: {first_line[:70]}"
        # task #647: a broadcast fanned to N idle recipients arrives as N
        # near-identical handoff messages (different ids, same sender+content),
        # and the per-recipient watcher sweep would spawn one task row each —
        # the #638-645 incident where ONE directive rendered as 8 "tasks on
        # many workers". A directive is not N tasks: collapse to a single
        # tracked task by skipping when an open handoff with the same title
        # already exists. The deduped recipient still gets a watcher nudge, so
        # the broadcast isn't lost — it just isn't re-tracked N times.
        existing = [
            t
            for t in board.all_tasks
            if t.title == title and t.status not in (TaskStatus.DONE, TaskStatus.FAILED)
        ]
        if existing:
            _log.info(
                "spawn_handoff_task: dedup — open handoff '%s' exists, skipping %s",
                title[:60],
                recipient,
            )
            return False
        # #913: duplicate-work suppression. Beyond the exact-title dedup above,
        # skip the spawn if the recipient is ALREADY engaged on equivalent work
        # — a task that is_duplicate_work-matches the incoming handoff (same
        # source-worker + high title similarity / same number / same jira_key).
        # This is the automated-path complement to the engagement awareness the
        # Queen gets on queen_prompt_worker (#913): two converging coordination
        # paths shouldn't stack a second tracked task for the same work. Mark
        # the source message read (the #894 consume pattern) so the watcher
        # doesn't re-relay it, and log the suppression — no silent drop. Gated
        # by ``suppress_duplicate_handoff``.
        drones_cfg = getattr(getattr(d, "config", None), "drones", None)
        if bool(getattr(drones_cfg, "suppress_duplicate_handoff", False)):
            from types import SimpleNamespace

            from swarm.server.engagement import is_duplicate_work

            incoming = SimpleNamespace(number=0, jira_key="", source_worker=sender, title=title)
            similarity = float(getattr(drones_cfg, "duplicate_title_similarity", 0.8))
            try:
                dup = is_duplicate_work(
                    incoming, board.active_tasks_for_worker(recipient), similarity=similarity
                )
            except Exception:
                dup = None
            if dup is not None:
                if msg_id is not None and getattr(d, "message_store", None) is not None:
                    try:
                        d.message_store.mark_read(recipient, [msg_id])
                    except Exception:
                        _log.debug("spawn_handoff: mark_read on suppress failed", exc_info=True)
                self._buzz_suppressed_duplicate(recipient, sender, dup, msg_id)
                return False
        description = (
            f"Auto-spawned by the inter-worker watcher (task #442): "
            f"{recipient} was idle and task-less when {sender} sent a "
            f"'{msg_type}' handoff (message #{msg_id}). Process the handoff "
            f"and complete this task.\n\n--- original message ---\n{content}"
        )
        try:
            task = board.create(
                title=title,
                description=description,
                tags=["auto-handoff"],
            )
        except Exception:
            _log.warning("spawn_handoff_task: create failed for %s", recipient, exc_info=True)
            return False
        # Tag the originating worker so the dispatch path treats this as a
        # worker-to-worker handoff (skips the user-request plan-mode gate
        # added 2026-05-22). Without this, every auto-handoff would gate
        # behind plan approval and stall the inter-worker watcher's whole
        # point — getting a stuck recipient unstuck without operator help.
        if sender and sender != "?":
            try:
                d.edit_task(task.id, source_worker=sender, actor="drone:inter-worker-handoff")
            except Exception:
                _log.warning(
                    "spawn_handoff_task: source_worker tag failed for %s", task.id, exc_info=True
                )
        try:
            return await self.assign_and_start_task(
                task.id, recipient, actor="drone:inter-worker-handoff"
            )
        except Exception:
            _log.warning(
                "spawn_handoff_task: assign_and_start failed for %s (task %s)",
                recipient,
                task.id,
                exc_info=True,
            )
            return False

    async def assign_and_start_task(
        self,
        task_id: str,
        worker_name: str,
        actor: str = "user",
        message: str | None = None,
    ) -> bool:
        """Assign and immediately start a task (used by drones/Queen)."""
        assigned = await self.assign_task(task_id, worker_name, actor=actor)
        if assigned:
            return await self.start_task(task_id, actor=actor, message=message)
        return False

    # ----- complete -----

    def _acquire_and_board_complete(
        self, task_id: str, resolution: str, *, force: bool
    ) -> tuple[SwarmTask, bool]:
        """Resolve the task and complete it on the board (force-aware).

        Normal path requires ASSIGNED/ACTIVE. Force path accepts any status,
        clears the task's blocker rows (a wedged BLOCKED task may carry rows
        from more than one worker), and completes past the status gate.
        Returns ``(task, board_result)``; the caller runs the shared
        downstream side-effects when ``board_result`` is True. Split out to
        keep ``complete_task`` under the complexity gate.
        """
        d = self._d
        if force:
            task = d._require_task(task_id, None)
            store = getattr(d, "blocker_store", None)
            if store is not None:
                store.clear_for_task(task.number)
            return task, d.task_board.force_complete(task_id, resolution=resolution)
        task = d._require_task(task_id, {TaskStatus.ASSIGNED, TaskStatus.ACTIVE})
        return task, d.task_board.complete(task_id, resolution=resolution)

    def complete_task(
        self,
        task_id: str,
        actor: str = "user",
        resolution: str = "",
        *,
        verify: bool = True,
        force: bool = False,
    ) -> bool:
        """Complete a task. Raises if not found or wrong state.

        When the task originated from an email and Graph is configured,
        automatically drafts a reply via the Graph API.

        ``verify=True`` (default) fires the verifier drone asynchronously
        after a successful completion so tier-1 deterministic checks +
        tier-2 LLM judgment can either confirm or reopen the task. Pass
        ``verify=False`` from explicit operator/Queen overrides
        (``queen_force_complete_task``) — those are deliberate
        completions that the verifier must respect.

        ``force=True`` is the operator/Queen override for a *wedged* task:
        it clears any blocker rows pinning the task and completes it from
        ANY non-terminal status, including BLOCKED — which the normal
        status-gated path refuses (the #574 deadlock). All the downstream
        completion side-effects below run identically for both paths.
        """
        d = self._d
        task, result = self._acquire_and_board_complete(task_id, resolution, force=force)

        # Email info is stable across completion (only status/completed_at/
        # resolution change), so reading it here is equivalent to before.
        source_email_id = task.source_email_id
        task_title = task.title
        task_type = task.task_type.value
        if result:
            # Knowledge consolidation: capture worker's last output as learnings
            d.playbook_ops.consolidate_learnings(task)
            # Signal pilot that a task was completed during this session
            # so hive_complete detection can distinguish fresh completions
            # from stale ones loaded from the persistent store.
            if d.pilot:
                d.pilot.mark_completion_seen()
            d.task_history.append(task_id, TaskAction.COMPLETED, actor=actor, detail=resolution)
            d.drone_log.add(
                SystemAction.TASK_COMPLETED,
                task.assigned_worker or actor,
                task_title,
                category=LogCategory.TASK,
            )
            d.push_notification(
                event="task_completed",
                worker=task.assigned_worker or actor,
                message=f"Task completed: {task_title}",
                priority="medium",
            )
            d.notification_bus.emit_task_completed(task.assigned_worker or actor, task_title)
            if hasattr(d, "pipeline_engine"):
                d.pipeline_engine.on_task_completed(task_id, resolution)
            d.jira_svc.fire_assign(task_id)
            d.jira_svc.fire_export(task_id, "done")
            d.jira_svc.fire_completion(task_id)
            # Notify source worker for cross-project tasks
            if task.is_cross_project and task.source_worker:
                source = d.get_worker(task.source_worker)
                if source:
                    notify_msg = (
                        f"Cross-project task completed: {task_title}\n"
                        f"Resolution: {resolution or '(no resolution)'}"
                    )
                    try:
                        t = asyncio.create_task(
                            d.send_to_worker(task.source_worker, notify_msg, _log_operator=False)
                        )
                        t.add_done_callback(_log_task_exception)
                        d._track_task(t)
                    except RuntimeError:
                        pass  # No running event loop
            # Auto-draft reply for email-originated tasks (like Jira comments).
            # Use a distinct local name so we don't clobber the SwarmTask bound
            # at the top of this method — ``task.assigned_worker`` is read
            # again below for the post-ship self-loop (task #270 regression).
            if source_email_id and d.graph_mgr and resolution:
                try:
                    asyncio.get_running_loop()
                    # Use ``d._send_completion_reply`` (daemon proxy) so
                    # tests that monkeypatch the daemon-side method still
                    # intercept; same reason :meth:`auto_start_next_assigned`
                    # routes through ``d.start_task``.
                    reply_bg = asyncio.create_task(
                        d._send_completion_reply(
                            source_email_id, task_title, task_type, resolution, task_id
                        )
                    )
                    reply_bg.add_done_callback(_log_task_exception)
                    d._track_task(reply_bg)
                except RuntimeError:
                    pass  # No running event loop (test/CLI context)
            # Command Center: auto-resolve any active Attention threads
            # linked to this task. Threads with kind in queen-escalation /
            # escalation / proposal that carry the same ``task_id`` get
            # cleared so the operator's Attention queue doesn't accumulate
            # stale items after work ships.
            d._auto_resolve_attention_for_task(task_id)
            # Task #225 Phase 3: post-ship self-loop.  If the worker that just
            # shipped has another ASSIGNED task queued up, kick it off now so
            # the PTY keeps moving instead of parking at the idle prompt
            # waiting for a drone/Queen nudge.  We skip IN_PROGRESS follow-ups
            # (already mid-flight in some session) and all other states.
            d._auto_start_next_assigned(task.assigned_worker)
            # Operator force-completes (verify=False) leave a SKIPPED
            # stamp on the task so the audit trail distinguishes them
            # from normal completions.  The fire-the-verifier branch
            # for verify=True existed in commit 4249a39 but the
            # ``_init_verifier_drone`` call site was missed, so the
            # verifier never ran in production; the dead code was
            # removed in 2026.5.25.4 along with the closure helpers.
            # The ``verify`` kwarg stays on the public API so
            # queen_force_complete_task keeps its audit semantics.
            if not verify:
                d.playbook_ops.log_verifier_skip(task, actor=actor)
            else:
                # Fire the tiered verifier (shadow by default — see
                # _fire_verifier). This is the re-wired call site the
                # 2026.5.25.4 cleanup removed; gated on verifier_enabled.
                self._fire_verifier(task)
            # Playbook synthesis (independent of verification): mine this
            # successful completion into reusable procedural memory.
            d.playbook_ops.fire_synthesis(task, resolution)
        return result

    def _fire_verifier(self, task: SwarmTask) -> None:
        """Construct and asynchronously fire the tiered verifier drone for a
        just-completed task.

        No-op when ``DroneConfig.verifier_enabled`` is False or there is no
        running event loop (CLI/test context). The drone runs
        fire-and-forget so it never blocks completion. By default it runs in
        SHADOW mode (``verifier_enforce=False``): it records verdicts for the
        Harness metrics but reopens/escalates nothing until the operator
        enables enforcement from the dashboard.
        """
        d = self._d
        if not d.config.drones.verifier_enabled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # No event loop (test/CLI) — skip the async verifier.

        from swarm.drones.verifier import (
            VerifierDrone,
            fire_and_forget,
            has_check_evidence,
            safe_git_diff,
        )
        from swarm.messages.store import Message
        from swarm.queen.verifier import VerifierClient

        worker = d.get_worker(task.assigned_worker) if task.assigned_worker else None
        repo = (worker.repo_path or worker.path) if worker else "."

        async def _diff(_t: SwarmTask) -> str:
            return await safe_git_diff(repo, "HEAD~1")

        def _check(worker_name: str) -> bool:
            return has_check_evidence(d.drone_log.entries, worker_name)

        async def _warn(*, to: str, msg_type: str, content: str, from_: str) -> None:
            try:
                d.message_store.send(
                    Message(sender=from_, recipient=to, msg_type=msg_type, content=content)
                )
            except Exception:
                _log.warning(
                    "verifier warning send failed for task #%d", task.number, exc_info=True
                )

        drone = VerifierDrone(
            drone_log=d.drone_log,
            task_board=d.task_board,
            verifier_client=VerifierClient(),
            diff_provider=_diff,
            check_evidence_provider=_check,
            send_warning=_warn,
            enforce=d.config.drones.verifier_enforce,
            max_reopens=d.config.drones.verify_reopen_cap,
        )
        t = loop.create_task(fire_and_forget(drone, task))
        t.add_done_callback(_log_task_exception)
        d._track_task(t)

    def auto_resolve_attention_for_task(self, task_id: str) -> None:
        """Resolve active Attention threads whose ``task_id`` matches.

        Best-effort: an exception here must never interrupt the
        completion path. Broadcasts a ``queen.thread`` resolved event so
        the dashboard clears the Attention card without polling.
        """
        d = self._d
        chat = getattr(d, "queen_chat", None)
        if chat is None or not task_id:
            return
        try:
            active = chat.list_threads(status="active", limit=200)
        except Exception:
            return
        for thread in active:
            if thread.task_id != task_id:
                continue
            try:
                ok = chat.resolve_thread(
                    thread.id, resolved_by="queen", reason="upstream task DONE"
                )
            except Exception:
                # State mutation (resolving an attention thread) — a silent
                # failure would leave a stuck card with no forensic trail.
                _log.warning(
                    "failed to auto-resolve thread %s for task %s",
                    thread.id,
                    task_id,
                    exc_info=True,
                )
                continue
            if ok:
                try:
                    from swarm.server.routes.queen import _broadcast_thread

                    _broadcast_thread(d, thread.id, "resolved")
                except Exception:
                    # Best-effort UI refresh only — the thread IS resolved;
                    # a missed broadcast just delays the dashboard update.
                    _log.debug("thread %s resolve broadcast failed", thread.id, exc_info=True)

    def auto_start_next_assigned(self, worker_name: str | None) -> None:
        """Fire-and-forget: start the next ASSIGNED task for *worker_name*.

        No-op when no such task exists, when there's no running event loop
        (sync/CLI callers), or when the worker name is empty. Intentionally
        picks the lowest task number so chained work ships in creation
        order rather than LIFO — matches operator expectations when a
        burst of related tasks gets filed.
        """
        d = self._d
        if not worker_name or not d.task_board:
            return
        next_assigned = next(
            (
                t
                for t in sorted(
                    d.task_board.active_tasks_for_worker(worker_name),
                    key=lambda t: t.number,
                )
                if t.status == TaskStatus.ASSIGNED
            ),
            None,
        )
        if next_assigned is None:
            # #765: empty queue → let the worker's standing loop (if the
            # operator enabled one) file one lowest-priority filler task.
            # Real work never reaches here, so the loop is always preempted.
            d._maybe_run_standing_loop(worker_name)
            return
        try:
            # Go through ``d.start_task`` (the daemon proxy) rather than
            # ``self.start_task`` so existing tests that patch
            # ``daemon.start_task`` still intercept the auto-chain dispatch.
            t = asyncio.create_task(d.start_task(next_assigned.id, actor="auto-chain"))
            t.add_done_callback(_log_task_exception)
            d._track_task(t)
        except RuntimeError:
            # No running event loop (sync/CLI context) — leave the task
            # ASSIGNED; the idle-watcher or the next dashboard action
            # will pick it up.
            return

    # ----- email reply -----

    async def _send_completion_reply(
        self,
        message_id: str,
        task_title: str,
        task_type: str,
        resolution: str,
        task_id: str = "",
    ) -> None:
        """Delegate to EmailService."""
        await self._d.email.send_completion_reply(
            message_id, task_title, task_type, resolution, task_id
        )

    async def retry_draft_reply(self, task_id: str) -> None:
        """Retry drafting an email reply for an already-completed task."""
        from swarm.server.daemon import TaskOperationError

        d = self._d
        task = d._require_task(task_id)
        if not task.source_email_id:
            raise TaskOperationError("Task has no source email", status_code=409)
        if not task.resolution:
            raise TaskOperationError("Task has no resolution text", status_code=409)
        if not d.graph_mgr:
            raise TaskOperationError("Microsoft Graph not configured", status_code=409)

        await d.email.send_completion_reply(
            task.source_email_id, task.title, task.task_type.value, task.resolution, task_id
        )
