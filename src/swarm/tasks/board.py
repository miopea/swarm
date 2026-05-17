"""TaskBoard — in-memory task store for the swarm."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from swarm.events import EventEmitter
from swarm.logging import get_logger
from swarm.tasks.task import SwarmTask, TaskPriority, TaskStatus, TaskType

if TYPE_CHECKING:
    from swarm.tasks.store import TaskStore

_log = get_logger("tasks.board")

_PRIORITY_ORDER = {
    TaskPriority.URGENT: 0,
    TaskPriority.HIGH: 1,
    TaskPriority.NORMAL: 2,
    TaskPriority.LOW: 3,
}


class TaskBoard(EventEmitter):
    """In-memory task board for tracking and assigning work."""

    def __init__(self, store: TaskStore | None = None) -> None:
        self.__init_emitter__()
        self._tasks: dict[str, SwarmTask] = {}
        # RLock is required: _notify() emits events whose callbacks may
        # re-enter the board (e.g. expire_stale_proposals reads available_tasks).
        # All locked sections are fast in-memory operations (no I/O, no awaits).
        self._lock = threading.RLock()
        self._store = store
        # Cached sort of available_tasks. Invalidated when _notify() fires.
        self._available_cache: list[SwarmTask] | None = None
        if store:
            self._tasks = store.load()
        # Derive next number from existing tasks; backfill any with number=0
        existing = [t.number for t in self._tasks.values() if t.number > 0]
        self._next_number: int = max(existing, default=0) + 1
        backfilled = False
        for task in sorted(self._tasks.values(), key=lambda t: t.created_at):
            if task.number == 0:
                task.number = self._next_number
                self._next_number += 1
                backfilled = True
        if backfilled:
            self._persist()

    def on_change(self, callback: Callable[[], None]) -> None:
        """Register callback for task board changes."""
        self.on("change", callback)

    def _notify(self) -> None:
        self._available_cache = None
        self.emit("change")

    def _persist(self) -> None:
        """Save tasks to store if configured."""
        if self._store:
            self._store.save(self._tasks)

    def persist(self, task: SwarmTask | None = None) -> None:
        """Public persist + notify hook.

        Used by callers that mutate a task field outside the board's
        own helpers (verifier drone updating ``verification_*`` fields
        is the current sole user). ``task`` is accepted for symmetry but
        ignored — the board persists the whole ``_tasks`` map. Notifies
        change subscribers so dashboard / WebSocket views update.
        """
        del task  # accepted for caller ergonomics; whole-map persist below
        with self._lock:
            self._persist()
            self._notify()

    def add(self, task: SwarmTask) -> SwarmTask:
        """Add a task to the board."""
        with self._lock:
            if task.number == 0:
                task.number = self._next_number
                self._next_number += 1
            self._tasks[task.id] = task
            _log.info("task #%d added: %s — %s", task.number, task.id, task.title)
            self._persist()
            self._notify()
        return task

    def create(
        self,
        title: str,
        description: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        task_type: TaskType = TaskType.CHORE,
        depends_on: list[str] | None = None,
        tags: list[str] | None = None,
        attachments: list[str] | None = None,
        source_email_id: str = "",
    ) -> SwarmTask:
        """Create and add a new task.

        Raises ValueError if the depends_on list would create a cycle.
        """
        task = SwarmTask(
            title=title,
            description=description,
            priority=priority,
            task_type=task_type,
            depends_on=depends_on or [],
            tags=tags or [],
            attachments=attachments or [],
            source_email_id=source_email_id,
        )
        if task.depends_on and self._has_cycle(task.id, task.depends_on):
            raise ValueError("circular task dependency detected")
        return self.add(task)

    def _has_cycle(self, task_id: str, depends_on: list[str]) -> bool:
        """Detect circular dependencies using DFS."""
        visited: set[str] = set()

        def _dfs(tid: str) -> bool:
            if tid in visited:
                return False
            visited.add(tid)
            deps = depends_on if tid == task_id else getattr(self._tasks.get(tid), "depends_on", [])
            for dep_id in deps:
                if dep_id == task_id:
                    return True
                if _dfs(dep_id):
                    return True
            return False

        return _dfs(task_id)

    def get(self, task_id: str) -> SwarmTask | None:
        return self._tasks.get(task_id)

    def remove(self, task_id: str) -> bool:
        with self._lock:
            if task_id in self._tasks:
                del self._tasks[task_id]
                self._scrub_dependency(task_id)
                self._persist()
                self._notify()
            else:
                return False
        return True

    def remove_tasks(self, task_ids: set[str]) -> int:
        """Remove multiple tasks by ID. Returns count removed."""
        removed = 0
        with self._lock:
            for tid in task_ids:
                if tid in self._tasks:
                    del self._tasks[tid]
                    removed += 1
            if removed:
                for tid in task_ids:
                    self._scrub_dependency(tid)
                self._persist()
                self._notify()
        return removed

    def _scrub_dependency(self, task_id: str) -> None:
        """Remove *task_id* from all other tasks' ``depends_on`` lists."""
        for task in self._tasks.values():
            if task_id in task.depends_on:
                task.depends_on = [d for d in task.depends_on if d != task_id]

    def update(
        self,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        priority: TaskPriority | None = None,
        task_type: TaskType | None = None,
        tags: list[str] | None = None,
        attachments: list[str] | None = None,
        depends_on: list[str] | None = None,
        source_worker: str | None = None,
        target_worker: str | None = None,
        dependency_type: str | None = None,
        acceptance_criteria: list[str] | None = None,
        context_refs: list[str] | None = None,
    ) -> bool:
        """Update fields on an existing task. Only non-None fields are changed."""
        import time

        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            self._apply_core_fields(
                task,
                title,
                description,
                priority,
                task_type,
                tags,
                attachments,
            )
            if depends_on is not None:
                if self._has_cycle(task_id, depends_on):
                    raise ValueError("circular task dependency detected")
                task.depends_on = depends_on
            self._apply_cross_fields(
                task,
                source_worker,
                target_worker,
                dependency_type,
                acceptance_criteria,
                context_refs,
            )
            task.updated_at = time.time()
            self._persist()
            self._notify()
        return True

    @staticmethod
    def _apply_core_fields(
        task: SwarmTask,
        title: str | None,
        description: str | None,
        priority: TaskPriority | None,
        task_type: TaskType | None,
        tags: list[str] | None,
        attachments: list[str] | None,
    ) -> None:
        if title is not None:
            task.title = title
        if description is not None:
            task.description = description
        if priority is not None:
            task.priority = priority
        if task_type is not None:
            task.task_type = task_type
        if tags is not None:
            task.tags = tags
        if attachments is not None:
            task.attachments = attachments

    @staticmethod
    def _apply_cross_fields(
        task: SwarmTask,
        source_worker: str | None,
        target_worker: str | None,
        dependency_type: str | None,
        acceptance_criteria: list[str] | None,
        context_refs: list[str] | None,
    ) -> None:
        if source_worker is not None:
            task.source_worker = source_worker
        if target_worker is not None:
            task.target_worker = target_worker
        if dependency_type is not None:
            task.dependency_type = dependency_type
        if acceptance_criteria is not None:
            task.acceptance_criteria = acceptance_criteria
        if context_refs is not None:
            task.context_refs = context_refs
        if source_worker or target_worker:
            task.is_cross_project = True

    def assign(self, task_id: str, worker_name: str) -> bool:
        """Assign a task to a worker."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if not task.is_available:
                return False
            task.assign(worker_name)
            _log.info("task %s assigned to %s", task_id, worker_name)
            self._persist()
            self._notify()
        return True

    def complete(self, task_id: str, resolution: str = "") -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status not in (TaskStatus.ASSIGNED, TaskStatus.ACTIVE):
                _log.warning("cannot complete task %s — status is %s", task_id, task.status.value)
                return False
            task.complete(resolution=resolution)
            _log.info("task %s completed", task_id)
            self._persist()
            self._notify()
        return True

    def fail(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task.fail()
            _log.info("task %s failed", task_id)
            self._persist()
            self._notify()
        return True

    def reopen(self, task_id: str) -> bool:
        """Reopen a completed or failed task, returning it to PENDING."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status not in (TaskStatus.DONE, TaskStatus.FAILED):
                return False
            task.reopen()
            _log.info("task %s reopened", task_id)
            self._persist()
            self._notify()
        return True

    def unassign(self, task_id: str) -> bool:
        """Unassign a single task, returning it to PENDING."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status not in (TaskStatus.ASSIGNED, TaskStatus.ACTIVE):
                return False
            task.unassign()
            _log.info("task %s unassigned", task_id)
            self._persist()
            self._notify()
        return True

    def set_jira_key(self, task_id: str, jira_key: str) -> bool:
        """Set the jira_key on an existing task. Thread-safe."""
        import time

        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task.jira_key = jira_key
            task.updated_at = time.time()
            self._persist()
            self._notify()
        return True

    def reassign_worker(self, old_name: str, new_name: str) -> None:
        """Reassign all tasks from one worker name to another (rename)."""
        with self._lock:
            for task in self._tasks.values():
                if task.assigned_worker == old_name:
                    task.assigned_worker = new_name
            self._persist()

    def unassign_worker(self, worker_name: str) -> None:
        """Unassign all tasks from a worker (e.g., when worker dies)."""
        with self._lock:
            for task in self._tasks.values():
                if task.assigned_worker == worker_name and task.status in (
                    TaskStatus.ASSIGNED,
                    TaskStatus.ACTIVE,
                ):
                    task.status = TaskStatus.UNASSIGNED
                    task.assigned_worker = None
                    _log.info("unassigned task %s from dead worker %s", task.id, worker_name)
            self._persist()
            self._notify()

    def demote_other_active(self, worker_name: str, keep_task_id: str) -> list[str]:
        """Demote any ACTIVE tasks for *worker_name* (other than *keep_task_id*) to ASSIGNED.

        A worker can only meaningfully process one task at a time, so the
        dashboard must show at most one ACTIVE task per worker. Returns the
        list of task IDs that were demoted.
        """
        import time

        demoted: list[str] = []
        with self._lock:
            for task in self._tasks.values():
                if (
                    task.assigned_worker == worker_name
                    and task.status == TaskStatus.ACTIVE
                    and task.id != keep_task_id
                ):
                    task.status = TaskStatus.ASSIGNED
                    task.updated_at = time.time()
                    demoted.append(task.id)
                    _log.info(
                        "demoted task %s from ACTIVE to ASSIGNED (worker=%s, kept=%s)",
                        task.id,
                        worker_name,
                        keep_task_id,
                    )
            if demoted:
                self._persist()
                self._notify()
        return demoted

    def activate(self, task_id: str) -> bool:
        """#405 INV-1: set a task ACTIVE, enforcing ≤1 ACTIVE per worker.

        Operator-action tasks never go ACTIVE (returns False). Any other
        ACTIVE task for the same worker is demoted to ASSIGNED. Returns
        True iff the task is now ACTIVE.
        """
        import time

        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.is_operator_action:
                return False
            worker = task.assigned_worker
            if worker:
                for other in self._tasks.values():
                    if (
                        other.assigned_worker == worker
                        and other.status == TaskStatus.ACTIVE
                        and other.id != task_id
                    ):
                        other.status = TaskStatus.ASSIGNED
                        other.updated_at = time.time()
            task.start()
            self._persist()
            self._notify()
        return True

    def current_task_for_worker(self, worker_name: str) -> SwarmTask | None:
        """#405 INV-3: a worker's current task IS its single ACTIVE task
        (or None). INV-1 guarantees there is never more than one."""
        with self._lock:
            for t in self._tasks.values():
                if t.assigned_worker == worker_name and t.status == TaskStatus.ACTIVE:
                    return t
        return None

    def park(self, task_id: str, worker_name: str, reason: str) -> bool:
        """#406: a worker hands its OWN ACTIVE task back to ASSIGNED.

        Intentional set-down (operator preempt, scope change) — NOT a
        blocker (no ``swarm_report_blocker`` binding is created). Keeps
        ``assigned_worker`` so the same worker resumes later. Rejects
        (returns False) unless the task exists, is ACTIVE, and is owned
        by *worker_name* — no cross-worker parking. Composes with the
        #405 invariants immediately: the worker has no ACTIVE task right
        after, no reconciler/reload needed. The caller records *reason*
        to history/buzz; this method is the pure state transition.
        """
        import time

        with self._lock:
            task = self._tasks.get(task_id)
            if (
                task is None
                or task.assigned_worker != worker_name
                or task.status != TaskStatus.ACTIVE
            ):
                return False
            task.status = TaskStatus.ASSIGNED
            task.updated_at = time.time()
            self._persist()
            self._notify()
            _log.info("task %s parked by %s (%s)", task_id, worker_name, reason[:80])
        return True

    def reconcile_invariants(
        self,
        *,
        working_workers: set[str] | None = None,
        blocked_task_ids: set[str] | None = None,
    ) -> list[dict[str, str]]:
        """#405 reconciliation — repair INV-1/2/3 + operator-action drift.

        * Operator-action task ACTIVE → demote to ASSIGNED (never ACTIVE).
        * INV-1: >1 ACTIVE per worker → keep the most recently updated,
          demote the rest to ASSIGNED.
        * INV-2: ACTIVE task whose worker is not in *working_workers* →
          BLOCKED if its id is in *blocked_task_ids*, else ASSIGNED.

        Deterministic + idempotent (a second call with the same inputs
        returns ``[]``). Returns one repair dict per change for the buzz
        audit. The caller supplies live worker/blocker state.
        """
        import time

        repairs: list[dict[str, str]] = []
        now = time.time()
        with self._lock:
            self._recon_operator_action(now, repairs)
            self._recon_inv1(now, repairs)
            self._recon_inv2(working_workers or set(), blocked_task_ids or set(), now, repairs)
            if repairs:
                self._persist()
                self._notify()
        return repairs

    @staticmethod
    def _repair(task: SwarmTask, to: str, reason: str) -> dict[str, str]:
        return {
            "task_id": task.id,
            "worker": task.assigned_worker or "",
            "from": "active",
            "to": to,
            "reason": reason,
        }

    def _recon_operator_action(self, now: float, repairs: list[dict[str, str]]) -> None:
        """Operator-action tasks may never be ACTIVE."""
        for task in self._tasks.values():
            if task.is_operator_action and task.status == TaskStatus.ACTIVE:
                task.status = TaskStatus.ASSIGNED
                task.updated_at = now
                repairs.append(
                    self._repair(task, "assigned", "operator-action task may not be ACTIVE")
                )

    def _recon_inv1(self, now: float, repairs: list[dict[str, str]]) -> None:
        """>1 ACTIVE per worker → keep newest, demote the rest."""
        by_worker: dict[str, list[SwarmTask]] = {}
        for task in self._tasks.values():
            if task.status == TaskStatus.ACTIVE and task.assigned_worker:
                by_worker.setdefault(task.assigned_worker, []).append(task)
        for wname, tasks in by_worker.items():
            if len(tasks) <= 1:
                continue
            tasks.sort(key=lambda t: t.updated_at, reverse=True)
            for task in tasks[1:]:
                task.status = TaskStatus.ASSIGNED
                task.updated_at = now
                repairs.append(self._repair(task, "assigned", f"INV-1: {wname} had >1 ACTIVE"))

    def _recon_inv2(
        self,
        working: set[str],
        blocked: set[str],
        now: float,
        repairs: list[dict[str, str]],
    ) -> None:
        """ACTIVE while worker not working → BLOCKED (if blocker binding)
        else ASSIGNED."""
        for task in self._tasks.values():
            if task.status != TaskStatus.ACTIVE or not task.assigned_worker:
                continue
            if task.assigned_worker in working:
                continue
            if task.id in blocked:
                task.block("auto: worker idle with a reported blocker binding")
                repairs.append(
                    self._repair(task, "blocked", f"INV-2: {task.assigned_worker} idle + blocker")
                )
            else:
                task.status = TaskStatus.ASSIGNED
                task.updated_at = now
                repairs.append(
                    self._repair(task, "assigned", f"INV-2: {task.assigned_worker} not working")
                )

    def reconcile_active_per_worker(self) -> dict[str, list[str]]:
        """Ensure each worker has at most one ACTIVE task.

        Sweeps all workers; for each worker with >1 ACTIVE task, keeps the
        most recently updated one and demotes the rest to ASSIGNED. Used at
        daemon startup to clean up state left behind by older code paths
        that allowed multiple concurrent ACTIVE tasks per worker. Returns a
        mapping of worker_name → demoted task IDs.
        """
        import time

        result: dict[str, list[str]] = {}
        with self._lock:
            by_worker: dict[str, list[SwarmTask]] = {}
            for task in self._tasks.values():
                if task.status != TaskStatus.ACTIVE or not task.assigned_worker:
                    continue
                by_worker.setdefault(task.assigned_worker, []).append(task)
            now = time.time()
            for worker_name, tasks in by_worker.items():
                if len(tasks) <= 1:
                    continue
                tasks.sort(key=lambda t: t.updated_at, reverse=True)
                demoted_ids: list[str] = []
                for task in tasks[1:]:
                    task.status = TaskStatus.ASSIGNED
                    task.updated_at = now
                    demoted_ids.append(task.id)
                if demoted_ids:
                    result[worker_name] = demoted_ids
                    _log.info(
                        "reconcile: worker %s had %d ACTIVE; kept %s, demoted %d to ASSIGNED",
                        worker_name,
                        len(tasks),
                        tasks[0].id,
                        len(demoted_ids),
                    )
            if result:
                self._persist()
                self._notify()
        return result

    def create_cross_project(
        self,
        title: str,
        description: str = "",
        source_worker: str = "",
        target_worker: str = "",
        dependency_type: str = "blocks",
        priority: TaskPriority = TaskPriority.NORMAL,
        task_type: TaskType = TaskType.CHORE,
        acceptance_criteria: list[str] | None = None,
        context_refs: list[str] | None = None,
    ) -> SwarmTask:
        """Create a cross-project task in PROPOSED status."""
        task = SwarmTask(
            title=title,
            description=description,
            status=TaskStatus.BACKLOG,
            priority=priority,
            task_type=task_type,
            is_cross_project=True,
            source_worker=source_worker,
            target_worker=target_worker,
            dependency_type=dependency_type,
            acceptance_criteria=acceptance_criteria or [],
            context_refs=context_refs or [],
        )
        return self.add(task)

    def approve_task(self, task_id: str) -> bool:
        """Approve a PROPOSED task, transitioning to PENDING."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status != TaskStatus.BACKLOG:
                return False
            task.approve()
            _log.info("task %s approved", task_id)
            self._persist()
            self._notify()
        return True

    def reject_task(self, task_id: str, resolution: str = "") -> bool:
        """Reject a PROPOSED task, transitioning to FAILED."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status != TaskStatus.BACKLOG:
                return False
            task.reject(resolution)
            _log.info("task %s rejected", task_id)
            self._persist()
            self._notify()
        return True

    @property
    def proposed_tasks(self) -> list[SwarmTask]:
        """Tasks awaiting review (PROPOSED status)."""
        with self._lock:
            snapshot = list(self._tasks.values())
        return sorted(
            [t for t in snapshot if t.status == TaskStatus.BACKLOG],
            key=lambda t: (_PRIORITY_ORDER.get(t.priority, 2), t.created_at),
        )

    @property
    def all_tasks(self) -> list[SwarmTask]:
        """All tasks sorted by priority (urgent first) then creation time."""
        with self._lock:
            snapshot = list(self._tasks.values())
        return sorted(
            snapshot,
            key=lambda t: (_PRIORITY_ORDER.get(t.priority, 2), t.created_at),
        )

    @property
    def available_tasks(self) -> list[SwarmTask]:
        """Tasks that are pending and have all dependencies met.

        Result is cached and invalidated on every board mutation (_notify).
        The auto-assign and task-lifecycle loops read this property every
        poll cycle; recomputing the O(n log n) sort on each access shows up
        in profiles under high task counts.
        """
        with self._lock:
            cached = self._available_cache
            if cached is not None:
                return list(cached)
            snapshot = list(self._tasks.values())
            completed_ids = {t.id for t in snapshot if t.status == TaskStatus.DONE}
            sorted_tasks = sorted(
                snapshot,
                key=lambda t: (_PRIORITY_ORDER.get(t.priority, 2), t.created_at),
            )
            result = [
                t
                for t in sorted_tasks
                if t.is_available and all(d in completed_ids for d in t.depends_on)
            ]
            self._available_cache = result
            return list(result)

    @property
    def active_tasks(self) -> list[SwarmTask]:
        """Tasks currently assigned or in progress."""
        with self._lock:
            snapshot = list(self._tasks.values())
        return [t for t in snapshot if t.status in (TaskStatus.ASSIGNED, TaskStatus.ACTIVE)]

    def tasks_for_worker(self, worker_name: str) -> list[SwarmTask]:
        """Get all tasks assigned to a specific worker."""
        with self._lock:
            snapshot = list(self._tasks.values())
        return [t for t in snapshot if t.assigned_worker == worker_name]

    def active_tasks_for_worker(self, worker_name: str) -> list[SwarmTask]:
        """Get only ASSIGNED/IN_PROGRESS tasks for a worker (excludes completed)."""
        with self._lock:
            snapshot = list(self._tasks.values())
        return [
            t
            for t in snapshot
            if t.assigned_worker == worker_name
            and t.status in (TaskStatus.ASSIGNED, TaskStatus.ACTIVE)
        ]

    def parkable_tasks_for_worker(self, worker_name: str) -> list[SwarmTask]:
        """#407: the tasks this worker may park — its OWN tasks that are
        currently ACTIVE. ``park()`` only transitions ACTIVE→ASSIGNED, so
        this *is* the parkable set. With #405 INV-1 enforced it is ≤1, but
        an un-reconciled / pre-reload board can hold several; the park
        handler refuses to guess among them rather than silently picking
        one (the 2026-05-17 public-website wrong-task incident)."""
        with self._lock:
            snapshot = list(self._tasks.values())
        return [
            t
            for t in snapshot
            if t.assigned_worker == worker_name and t.status == TaskStatus.ACTIVE
        ]

    def query(
        self,
        *,
        status: str | None = None,
        priority: str | None = None,
        task_type: str | None = None,
        worker: str | None = None,
        search: str | None = None,
        sort: str = "priority",
        desc: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[SwarmTask], int]:
        """Filter, sort, and paginate tasks. Returns (page, total_matching)."""
        import time as _time

        _t0 = _time.monotonic()
        with self._lock:
            snapshot = list(self._tasks.values())

        if status:
            snapshot = [t for t in snapshot if t.status.value == status]
        if priority:
            snapshot = [t for t in snapshot if t.priority.value == priority]
        if task_type:
            snapshot = [t for t in snapshot if t.task_type.value == task_type]
        if worker:
            snapshot = [t for t in snapshot if t.assigned_worker == worker]
        if search:
            q = search.lower()
            snapshot = [t for t in snapshot if q in t.title.lower() or q in t.description.lower()]

        sort_keys: dict[str, object] = {
            "priority": lambda t: (_PRIORITY_ORDER.get(t.priority, 2), t.created_at),
            "created_at": lambda t: t.created_at,
            "title": lambda t: t.title.lower(),
            "status": lambda t: t.status.value,
        }
        key_fn = sort_keys.get(sort, sort_keys["priority"])
        snapshot.sort(key=key_fn, reverse=desc)

        total = len(snapshot)
        page = snapshot[offset : offset + limit]
        elapsed_ms = (_time.monotonic() - _t0) * 1000
        if elapsed_ms > 100:
            _log.warning("slow task query: %.0fms (%d tasks)", elapsed_ms, total)
        return page, total

    def summary(self) -> str:
        """One-line summary of the board state."""
        with self._lock:
            snapshot = list(self._tasks.values())
        total = len(snapshot)
        backlog = sum(1 for t in snapshot if t.status == TaskStatus.BACKLOG)
        unassigned = sum(1 for t in snapshot if t.status == TaskStatus.UNASSIGNED)
        active = sum(1 for t in snapshot if t.status in (TaskStatus.ASSIGNED, TaskStatus.ACTIVE))
        done = sum(1 for t in snapshot if t.status == TaskStatus.DONE)
        failed = sum(1 for t in snapshot if t.status == TaskStatus.FAILED)
        parts = [f"{total} tasks:"]
        if backlog:
            parts.append(f"{backlog} backlog,")
        parts.append(f"{unassigned} unassigned, {active} in progress, {done} done")
        if failed:
            parts.append(f", {failed} failed")
        return " ".join(parts)
