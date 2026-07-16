"""TaskManager — task lifecycle operations extracted from SwarmDaemon."""

from __future__ import annotations

from typing import TYPE_CHECKING

from swarm.config.models import HiveConfig
from swarm.drones.log import LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.tasks.history import TaskAction
from swarm.tasks.task import (
    SwarmTask,
    TaskPriority,
    TaskStatus,
    TaskType,
    auto_classify_type,
    smart_title,
)

if TYPE_CHECKING:
    from swarm.drones.log import DroneLog
    from swarm.drones.pilot import DronePilot
    from swarm.notify.bus import NotificationBus
    from swarm.tasks.board import TaskBoard
    from swarm.tasks.history import TaskHistory

_log = get_logger("server.tasks")


class TaskManager:
    """Handles pure task lifecycle operations: create, edit, status transitions.

    Does NOT handle assign (requires worker/PTY coordination) or complete
    (requires email/graph side effects). Those stay on SwarmDaemon.
    """

    def __init__(
        self,
        task_board: TaskBoard,
        task_history: TaskHistory,
        drone_log: DroneLog,
        pilot: DronePilot | None = None,
        notification_bus: NotificationBus | None = None,
        config: HiveConfig | None = None,
    ) -> None:
        self.task_board = task_board
        self.task_history = task_history
        self.drone_log = drone_log
        self._pilot = pilot
        self._notification_bus = notification_bus
        self.config = config or HiveConfig()

    def require_task(
        self, task_id: str, allowed_statuses: set[TaskStatus] | None = None
    ) -> SwarmTask:
        """Get a task by ID or raise TaskOperationError.

        If *allowed_statuses* is given, also validates the task's current status.
        """
        from swarm.server.daemon import TaskOperationError

        task = self.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        if allowed_statuses and task.status not in allowed_statuses:
            raise TaskOperationError(f"Task '{task_id}' cannot be modified ({task.status.value})")
        return task

    def create_task(
        self,
        title: str,
        description: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        task_type: TaskType = TaskType.CHORE,
        tags: list[str] | None = None,
        depends_on: list[str] | None = None,
        attachments: list[str] | None = None,
        source_email_id: str = "",
        actor: str = "user",
    ) -> SwarmTask:
        """Create a task. Broadcast happens via task_board.on_change."""
        task = self.task_board.create(
            title=title,
            description=description,
            priority=priority,
            task_type=task_type,
            tags=tags,
            depends_on=depends_on,
            attachments=attachments,
            source_email_id=source_email_id,
        )
        self.task_history.append(task.id, TaskAction.CREATED, actor=actor, detail=title)
        self.drone_log.add(
            SystemAction.TASK_CREATED,
            actor,
            title,
            category=LogCategory.TASK,
        )
        return task

    async def create_task_smart(
        self,
        title: str = "",
        description: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        task_type: TaskType | None = None,
        tags: list[str] | None = None,
        depends_on: list[str] | None = None,
        attachments: list[str] | None = None,
        source_email_id: str = "",
        actor: str = "user",
    ) -> SwarmTask:
        """Create a task with auto-title generation and type classification.

        If *title* is empty, uses Claude to generate one from the description.
        If *task_type* is None, auto-classifies from title + description.

        Raises ``ValueError`` if both *title* and *description* are empty —
        it's an input-validation failure (HTTP 400), not a state conflict.
        Phase C of the duplication-cluster sweep unified the HTTP error
        decorators on 409 for SwarmOperationError, which made what used
        to be 400 for this case turn into 409.  Switching to ValueError
        keeps the correct 400 semantics.
        """
        if not title and description:
            title = await smart_title(description) or ""
        if not title:
            raise ValueError("title or description required")
        if task_type is None:
            task_type = auto_classify_type(title, description)
        task = self.create_task(
            title=title,
            description=description,
            priority=priority,
            task_type=task_type,
            tags=tags,
            depends_on=depends_on,
            attachments=attachments,
            source_email_id=source_email_id,
            actor=actor,
        )
        # Populate the Outcomes rubric at creation so it's visible when the
        # operator later dispatches this task. Best-effort; never blocks.
        await self.apply_synthesized_criteria(task, actor=actor)
        return task

    async def resolve_title(self, title_raw: str, desc_hint: str, task_id: str) -> str:
        """Resolve a title: return as-is if non-empty, generate from description if empty."""
        title = title_raw.strip() if isinstance(title_raw, str) else ""
        if title:
            return title
        desc = desc_hint or ""
        if not desc:
            task = self.task_board.get(task_id)
            desc = task.description if task else ""
        if desc:
            return await smart_title(desc)
        return ""

    def unassign_task(self, task_id: str, actor: str = "user") -> bool:
        """Unassign a task, returning it to PENDING. Raises if not found or wrong state."""
        self.require_task(task_id, {TaskStatus.ASSIGNED, TaskStatus.ACTIVE})
        result = self.task_board.unassign(task_id)
        if result and self._pilot:
            self._pilot.clear_proposed_completion(task_id)
            self.task_history.append(task_id, TaskAction.EDITED, actor=actor, detail="unassigned")
        return result

    def reopen_task(self, task_id: str, actor: str = "user") -> bool:
        """Reopen a completed or failed task, returning it to PENDING."""
        task = self.require_task(task_id, {TaskStatus.DONE, TaskStatus.FAILED})
        # board.reopen() clears the assignment — read the worker first.
        worker = task.assigned_worker or actor
        result = self.task_board.reopen(task_id)
        if result and self._pilot:
            self._pilot.clear_proposed_completion(task_id)
            self.task_history.append(task_id, TaskAction.REOPENED, actor=actor)
        if result and self._notification_bus:
            self._notification_bus.emit_task_reopened(worker, task.title)
        return result

    def fail_task(self, task_id: str, actor: str = "user") -> bool:
        """Fail a task. Raises if not found."""
        task = self.require_task(task_id)
        result = self.task_board.fail(task_id)
        if result:
            self.task_history.append(task_id, TaskAction.FAILED, actor=actor)
            self.drone_log.add(
                SystemAction.TASK_FAILED,
                actor,
                task.title,
                category=LogCategory.TASK,
                is_notification=True,
            )
            if self._notification_bus:
                self._notification_bus.emit_task_failed(task.assigned_worker or actor, task.title)
        return result

    def remove_task(self, task_id: str, actor: str = "user") -> bool:
        """Remove a task. Raises if not found."""
        task = self.require_task(task_id)
        self.task_board.remove(task_id)
        self.task_history.append(task_id, TaskAction.REMOVED, actor=actor)
        self.drone_log.add(
            SystemAction.TASK_REMOVED,
            actor,
            task.title,
            category=LogCategory.TASK,
        )
        return True

    def edit_task(
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
        effort_tier: str | None = None,
        actor: str = "user",
    ) -> bool:
        """Edit a task. Raises if not found."""
        self.require_task(task_id)
        result = self.task_board.update(
            task_id,
            title=title,
            description=description,
            priority=priority,
            task_type=task_type,
            tags=tags,
            attachments=attachments,
            depends_on=depends_on,
            source_worker=source_worker,
            target_worker=target_worker,
            dependency_type=dependency_type,
            acceptance_criteria=acceptance_criteria,
            context_refs=context_refs,
            effort_tier=effort_tier,
        )
        if result:
            self.task_history.append(task_id, TaskAction.EDITED, actor=actor)
        return result

    async def apply_synthesized_criteria(self, task: SwarmTask, actor: str = "system") -> None:
        """Best-effort populate a task's acceptance criteria + effort tier at
        creation via the headless Queen (the Outcomes rubric).

        Gated on ``DroneConfig.verifier_criteria_synthesis``. Skips standing-loop
        filler (cost gate) and any task that already carries criteria (a worker
        supplied them, or it's a re-run). Never raises — synthesis is advisory
        and must not block or fail task creation. Callers should invoke this
        BEFORE dispatch so the criteria are visible to the worker in the task
        message and available to the verifier.
        """
        from swarm.drones.standing_loop import STANDING_LOOP_TAG
        from swarm.tasks.task import synthesize_acceptance_criteria

        if not self.config.drones.verifier_criteria_synthesis:
            return
        if STANDING_LOOP_TAG in task.tags:
            return
        if task.acceptance_criteria:
            return
        criteria, tier = await synthesize_acceptance_criteria(task.title, task.description)
        if not criteria and not tier:
            return
        self.edit_task(
            task.id,
            acceptance_criteria=criteria or None,
            effort_tier=tier or None,
            actor=actor,
        )

    def create_cross_task(
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
        actor: str = "system",
    ) -> SwarmTask:
        """Create a cross-project task in PROPOSED status."""
        task = self.task_board.create_cross_project(
            title=title,
            description=description,
            source_worker=source_worker,
            target_worker=target_worker,
            dependency_type=dependency_type,
            priority=priority,
            task_type=task_type,
            acceptance_criteria=acceptance_criteria,
            context_refs=context_refs,
        )
        self.task_history.append(
            task.id, TaskAction.PROPOSED, actor=actor, detail=f"{source_worker} → {target_worker}"
        )
        self.drone_log.add(
            SystemAction.TASK_PROPOSED,
            actor,
            f"{title} ({source_worker} → {target_worker})",
            category=LogCategory.TASK,
            is_notification=True,
        )
        return task

    def approve_cross_task(self, task_id: str, actor: str = "user") -> bool:
        """Approve a PROPOSED cross-project task, transitioning to PENDING.

        If the task has a ``target_worker``, auto-assigns to that worker.
        """
        task = self.require_task(task_id, {TaskStatus.BACKLOG})
        result = self.task_board.approve_task(task_id)
        if result:
            self.task_history.append(task_id, TaskAction.APPROVED, actor=actor)
            self.drone_log.add(
                SystemAction.TASK_APPROVED,
                actor,
                f"approved cross-project task {task_id[:8]}",
                category=LogCategory.TASK,
            )
            # Auto-assign to target worker if specified
            if task.target_worker:
                self.task_board.assign(task_id, task.target_worker)
                self.task_history.append(
                    task_id, TaskAction.ASSIGNED, actor="system", detail=task.target_worker
                )
        return result

    def reject_cross_task(self, task_id: str, actor: str = "user") -> bool:
        """Reject a PROPOSED cross-project task, transitioning to FAILED."""
        task = self.require_task(task_id, {TaskStatus.BACKLOG})
        result = self.task_board.reject_task(task_id)
        if result:
            self.task_history.append(task_id, TaskAction.FAILED, actor=actor, detail="rejected")
            self.drone_log.add(
                SystemAction.TASK_FAILED,
                actor,
                f"rejected: {task.title}",
                category=LogCategory.TASK,
            )
        return result
