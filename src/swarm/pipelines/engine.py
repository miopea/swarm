"""Pipeline engine — step sequencing, task creation, and lifecycle management."""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from swarm.events import EventEmitter
from swarm.logging import get_logger
from swarm.pipelines.models import (
    Pipeline,
    PipelineStatus,
    PipelineStep,
    StepStatus,
    StepType,
)
from swarm.pipelines.store import PipelineStore
from swarm.pipelines.template import load_template
from swarm.tasks.task import TYPE_MAP, TaskType

if TYPE_CHECKING:
    from swarm.services.registry import ServiceRegistry
    from swarm.tasks.board import TaskBoard

_log = get_logger("pipelines.engine")

# Legacy HH:MM shorthand — "14:30", "*:30", "14:*", "*:*"
_LEGACY_HHMM = re.compile(r"^(\*|\d{1,2}):(\*|\d{1,2})$")


class PipelineEngine(EventEmitter):
    """Manages pipeline lifecycle and step progression.

    Watches the TaskBoard for task completions and advances pipeline steps
    accordingly.  Automated steps are dispatched to the ServiceRegistry.
    """

    def __init__(
        self,
        store: PipelineStore | None = None,
        task_board: TaskBoard | None = None,
        service_registry: ServiceRegistry | None = None,
    ) -> None:
        self.__init_emitter__()
        self._store = store or PipelineStore()
        self._pipelines: dict[str, Pipeline] = self._store.load()
        self._task_board = task_board
        self._service_registry = service_registry
        # Map task_id → (pipeline_id, step_id) for completion tracking
        self._task_step_map: dict[str, tuple[str, str]] = {}
        self._rebuild_task_step_map()

    def _rebuild_task_step_map(self) -> None:
        """Rebuild the task→step lookup from all pipelines."""
        self._task_step_map.clear()
        for pipeline in self._pipelines.values():
            for step in pipeline.steps:
                if step.task_id:
                    self._task_step_map[step.task_id] = (pipeline.id, step.id)

    def _persist(self) -> None:
        self._store.save(self._pipelines)

    # -- CRUD ------------------------------------------------------------------

    def create(
        self,
        name: str,
        description: str = "",
        steps: list[PipelineStep] | None = None,
        tags: list[str] | None = None,
        timezone: str = "",
    ) -> Pipeline:
        """Create a new pipeline in DRAFT status."""
        pipeline = Pipeline(
            name=name,
            description=description,
            steps=steps or [],
            tags=tags or [],
            timezone=timezone,
        )
        self._pipelines[pipeline.id] = pipeline
        self._persist()
        _log.info("pipeline %s created: %s", pipeline.id, pipeline.name)
        self.emit("change")
        return pipeline

    def create_from_template(
        self,
        template_name: str,
        template_dir: str | None = None,
    ) -> Pipeline:
        """Create a pipeline from a YAML template file."""
        pipeline = load_template(template_name, template_dir)
        self._pipelines[pipeline.id] = pipeline
        self._persist()
        _log.info(
            "pipeline %s created from template %s: %s",
            pipeline.id,
            template_name,
            pipeline.name,
        )
        self.emit("change")
        return pipeline

    def get(self, pipeline_id: str) -> Pipeline | None:
        return self._pipelines.get(pipeline_id)

    def list_all(self) -> list[Pipeline]:
        return sorted(self._pipelines.values(), key=lambda p: p.created_at, reverse=True)

    def update(
        self,
        pipeline_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        steps: list[PipelineStep] | None = None,
        timezone: str | None = None,
    ) -> Pipeline | None:
        """Update mutable fields on an existing pipeline. Returns None if not found.

        Step replacement is only permitted while the pipeline is in DRAFT or
        PAUSED state — once a pipeline is RUNNING/COMPLETED/FAILED, the step
        graph is locked. Callers should treat an attempted step edit on a
        non-editable pipeline as a 409 conflict. The ``timezone`` field is
        always editable so an operator can correct a misconfigured zone
        without having to pause a running pipeline.
        """
        pipeline = self._pipelines.get(pipeline_id)
        if not pipeline:
            return None
        if name is not None:
            pipeline.name = name
        if description is not None:
            pipeline.description = description
        if tags is not None:
            pipeline.tags = tags
        if timezone is not None:
            pipeline.timezone = timezone
        if steps is not None:
            if pipeline.status not in (PipelineStatus.DRAFT, PipelineStatus.PAUSED):
                raise ValueError(
                    f"Pipeline {pipeline_id} is {pipeline.status.value} — "
                    "steps can only be edited while draft or paused"
                )
            pipeline.steps = steps
            # New step list may reference task IDs from prior steps — wipe the
            # map and rebuild from whatever survived the replacement.
            self._rebuild_task_step_map()
        pipeline.updated_at = time.time()
        self._persist()
        self.emit("change")
        return pipeline

    def remove(self, pipeline_id: str) -> bool:
        if pipeline_id in self._pipelines:
            del self._pipelines[pipeline_id]
            self._rebuild_task_step_map()
            self._persist()
            self.emit("change")
            return True
        return False

    # -- Lifecycle -------------------------------------------------------------

    def start_pipeline(self, pipeline_id: str) -> list[PipelineStep]:
        """Start a DRAFT pipeline, advancing first steps."""
        pipeline = self._pipelines.get(pipeline_id)
        if not pipeline:
            raise ValueError(f"Pipeline {pipeline_id} not found")
        if pipeline.status != PipelineStatus.DRAFT:
            raise ValueError(f"Pipeline {pipeline_id} is {pipeline.status.value}, not draft")

        newly_ready = pipeline.start()
        tasks_created = self._create_tasks_for_steps(pipeline, newly_ready)
        self._persist()
        self.emit("change")
        _log.info(
            "pipeline %s started, %d steps ready, %d tasks created",
            pipeline_id,
            len(newly_ready),
            tasks_created,
        )
        return newly_ready

    def pause_pipeline(self, pipeline_id: str) -> None:
        pipeline = self._pipelines.get(pipeline_id)
        if not pipeline:
            raise ValueError(f"Pipeline {pipeline_id} not found")
        pipeline.pause()
        self._persist()
        self.emit("change")

    def resume_pipeline(self, pipeline_id: str) -> list[PipelineStep]:
        pipeline = self._pipelines.get(pipeline_id)
        if not pipeline:
            raise ValueError(f"Pipeline {pipeline_id} not found")
        newly_ready = pipeline.resume()
        self._create_tasks_for_steps(pipeline, newly_ready)
        self._persist()
        self.emit("change")
        return newly_ready

    # -- Step completion -------------------------------------------------------

    def complete_step(
        self,
        pipeline_id: str,
        step_id: str,
        result: dict[str, Any] | None = None,
    ) -> list[PipelineStep]:
        """Mark a step as completed and advance the pipeline.

        Returns newly ready steps.
        """
        pipeline = self._pipelines.get(pipeline_id)
        if not pipeline:
            raise ValueError(f"Pipeline {pipeline_id} not found")
        step = pipeline.get_step(step_id)
        if not step:
            raise ValueError(f"Step {step_id} not found in pipeline {pipeline_id}")

        step.complete(result)
        newly_ready = pipeline.advance()
        self._create_tasks_for_steps(pipeline, newly_ready)
        self._persist()
        self.emit("change")
        _log.info(
            "pipeline %s step %s completed, %d new steps ready",
            pipeline_id,
            step_id,
            len(newly_ready),
        )
        return newly_ready

    def fail_step(self, pipeline_id: str, step_id: str, error: str = "") -> None:
        """Mark a step as failed."""
        pipeline = self._pipelines.get(pipeline_id)
        if not pipeline:
            raise ValueError(f"Pipeline {pipeline_id} not found")
        step = pipeline.get_step(step_id)
        if not step:
            raise ValueError(f"Step {step_id} not found")

        step.fail(error)
        pipeline.advance()  # updates pipeline status
        self._persist()
        self.emit("change")

    def skip_step(self, pipeline_id: str, step_id: str) -> list[PipelineStep]:
        """Skip a step and advance the pipeline."""
        pipeline = self._pipelines.get(pipeline_id)
        if not pipeline:
            raise ValueError(f"Pipeline {pipeline_id} not found")
        step = pipeline.get_step(step_id)
        if not step:
            raise ValueError(f"Step {step_id} not found")

        step.skip()
        newly_ready = pipeline.advance()
        self._create_tasks_for_steps(pipeline, newly_ready)
        self._persist()
        self.emit("change")
        return newly_ready

    def retry_step(self, pipeline_id: str, step_id: str) -> list[str]:
        """Reset a FAILED step plus its FAILED downstream descendants.

        P3 of the editor-UX series. Returns the list of step IDs that were
        reset (the operator's explicit target first, then any FAILED steps
        transitively downstream of it). SKIPPED and COMPLETED downstream
        are left alone — SKIPPED is sticky operator intent and re-running
        a COMPLETED side-effecting step would double-fire it.

        Raises ``ValueError`` for not-found and for non-FAILED targets;
        the route handler maps those to 404 / 409 respectively.
        """
        pipeline = self._pipelines.get(pipeline_id)
        if not pipeline:
            raise ValueError(f"Pipeline {pipeline_id} not found")
        step = pipeline.get_step(step_id)
        if not step:
            raise ValueError(f"Step {step_id} not found")
        if step.status != StepStatus.FAILED:
            raise ValueError(
                f"Step {step_id} is {step.status.value} — retry only resets FAILED steps"
            )

        # Collect the target + every FAILED descendant. BFS forward through
        # the DAG: a step is "downstream" if its depends_on includes one of
        # our reset-set IDs (transitively).
        reset_ids: list[str] = [step_id]
        seen = {step_id}
        frontier = [step_id]
        while frontier:
            current = frontier.pop(0)
            for candidate in pipeline.steps:
                if candidate.id in seen:
                    continue
                if current not in (candidate.depends_on or []):
                    continue
                if candidate.status == StepStatus.FAILED:
                    reset_ids.append(candidate.id)
                    seen.add(candidate.id)
                    frontier.append(candidate.id)
                else:
                    # Non-FAILED downstream blocks the cascade — we don't
                    # walk past it. A SKIPPED step's downstream stays in
                    # whatever state the operator left it.
                    continue

        # Apply the resets. Wipe transient fields so the engine treats each
        # step like a fresh PENDING entry; advance() then re-evaluates
        # readiness from the dep graph.
        for sid in reset_ids:
            s = pipeline.get_step(sid)
            if s is None:
                continue
            s.status = StepStatus.PENDING
            s.started_at = None
            s.completed_at = None
            s.error = ""
            s.result = {}
            # Drop the task link so a downstream agent step gets a fresh
            # SwarmTask on the next advance(); the old task may already
            # have been completed/failed and we don't want to inherit it.
            if s.task_id:
                self._task_step_map.pop(s.task_id, None)
            s.task_id = None

        newly_ready = pipeline.advance()
        self._create_tasks_for_steps(pipeline, newly_ready)
        self._persist()
        self.emit("change")
        _log.info(
            "pipeline %s step %s retried, %d steps reset (%s), %d newly ready",
            pipeline_id,
            step_id,
            len(reset_ids),
            ",".join(reset_ids),
            len(newly_ready),
        )
        return reset_ids

    # -- Task integration ------------------------------------------------------

    def on_task_completed(self, task_id: str, resolution: str = "") -> None:
        """Called when a SwarmTask completes — advances the linked pipeline step."""
        mapping = self._task_step_map.get(task_id)
        if not mapping:
            return
        pipeline_id, step_id = mapping
        self.complete_step(pipeline_id, step_id, result={"resolution": resolution})

    def on_task_failed(self, task_id: str) -> None:
        """Called when a SwarmTask fails — fails the linked pipeline step."""
        mapping = self._task_step_map.get(task_id)
        if not mapping:
            return
        pipeline_id, step_id = mapping
        self.fail_step(pipeline_id, step_id, error="linked task failed")

    # -- Internal --------------------------------------------------------------

    def _create_tasks_for_steps(
        self,
        pipeline: Pipeline,
        steps: list[PipelineStep],
    ) -> int:
        """Create SwarmTask entries on the TaskBoard for agent/human steps."""
        if not self._task_board:
            return 0
        created = 0
        for step in steps:
            if step.step_type in (StepType.AGENT, StepType.HUMAN):
                task_type = TYPE_MAP.get(step.task_type, TaskType.CHORE)
                task = self._task_board.create(
                    title=f"[{pipeline.name}] {step.name}",
                    description=step.description or f"Pipeline step: {step.name}",
                    task_type=task_type,
                    tags=[f"pipeline:{pipeline.id}", f"step:{step.id}"],
                )
                step.task_id = task.id
                self._task_step_map[task.id] = (pipeline.id, step.id)
                step.start()
                if step.assigned_worker:
                    self._task_board.assign(task.id, step.assigned_worker)
                created += 1
        return created

    def check_scheduled_steps(self) -> list[PipelineStep]:
        """Check for READY steps with a schedule matching the current minute.

        Each pipeline's ``timezone`` field (added P2) decides which tz the
        cron expression is evaluated in. Empty timezone falls back to
        server-local — the legacy behaviour, preserved so existing
        pipelines keep firing on the same minute as before the migration.
        """
        started: list[PipelineStep] = []
        for pipeline in self._pipelines.values():
            if pipeline.status != PipelineStatus.RUNNING:
                continue
            for step in pipeline.steps:
                if step.status not in (StepStatus.PENDING, StepStatus.READY) or not step.schedule:
                    continue
                if self._schedule_matches(step.schedule, tz=pipeline.timezone):
                    step.start()
                    self._create_tasks_for_steps(pipeline, [step])
                    started.append(step)
                    _log.info("scheduled step %s started in pipeline %s", step.id, pipeline.id)
        if started:
            self._persist()
            self.emit("change")
        return started

    @staticmethod
    def _schedule_matches(
        schedule: str,
        now: time.struct_time | None = None,
        *,
        tz: str = "",
    ) -> bool:
        """Check if a cron schedule matches the current minute.

        Supported formats:
          - 5-field cron expression: ``"30 14 * * 1-5"`` (14:30 Mon–Fri)
          - Legacy ``HH:MM`` shorthand: ``"14:30"``, ``"*:30"``, ``"14:*"``,
            ``"*:*"`` — translated to cron internally for backward compat.

        ``tz`` is an IANA zone name (e.g. ``"America/New_York"``). Empty
        means server-local — preserves pre-P2 behaviour. ``now`` is
        accepted only so existing tests that pass a fixed struct_time
        keep working; in production the value is always the live wall
        clock in the chosen zone.
        """
        schedule = schedule.strip()
        if not schedule:
            return False

        cron_expr = _LEGACY_HHMM.match(schedule)
        if cron_expr:
            hour, minute = cron_expr.group(1), cron_expr.group(2)
            schedule = f"{minute} {hour} * * *"

        try:
            from croniter import croniter  # imported lazily — optional in tests
        except ImportError:
            return False

        # Build the datetime to test against. Three sources, in priority:
        #   1. Caller-provided struct_time (legacy test surface).
        #   2. Caller-provided tz → live now in that zone.
        #   3. Neither → server local now.
        if now is not None:
            dt = datetime(now.tm_year, now.tm_mon, now.tm_mday, now.tm_hour, now.tm_min, 0)
        elif tz:
            try:
                from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

                dt = datetime.now(ZoneInfo(tz))
            except (ZoneInfoNotFoundError, ImportError):
                # Unknown zone → fall back to local rather than dropping the
                # firing entirely; the operator still gets *some* schedule.
                dt = datetime.now()
        else:
            dt = datetime.now()

        try:
            return croniter.match(schedule, dt)
        except (ValueError, KeyError, TypeError):
            return False

    @property
    def pipelines(self) -> dict[str, Pipeline]:
        return self._pipelines
