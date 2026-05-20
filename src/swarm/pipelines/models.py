"""Pipeline and step models for multi-step workflow orchestration."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StepType(Enum):
    """How a pipeline step is executed."""

    AGENT = "agent"  # Assigned to a PTY worker (interactive Claude)
    AUTOMATED = "automated"  # Executed by a registered service (no PTY)
    HUMAN = "human"  # Manual step — waits for human to mark done


class StepStatus(Enum):
    """Lifecycle state of a pipeline step."""

    PENDING = "pending"  # Not yet actionable (dependencies unmet)
    READY = "ready"  # Dependencies met, waiting to start
    IN_PROGRESS = "in_progress"  # Currently executing
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PipelineStatus(Enum):
    """Overall pipeline lifecycle state."""

    DRAFT = "draft"  # Created but not started
    RUNNING = "running"  # At least one step active
    PAUSED = "paused"  # Manually paused
    COMPLETED = "completed"  # All steps done
    FAILED = "failed"  # A required step failed


@dataclass
class PipelineStep:
    """A single step in a pipeline."""

    id: str
    name: str
    step_type: StepType = StepType.AGENT
    status: StepStatus = StepStatus.PENDING
    description: str = ""
    depends_on: list[str] = field(default_factory=list)
    # Agent steps: which worker to assign, what task_type to create
    task_type: str = "chore"
    assigned_worker: str | None = None
    task_id: str | None = None  # linked SwarmTask id once created
    # Automated steps: service name + config
    service: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    # Scheduling — 5-field cron (e.g. "30 14 * * 1-5") or legacy HH:MM
    # shorthand (e.g. "14:30", "*:30"). Empty string = on-demand only.
    schedule: str = ""
    # Timestamps
    started_at: float | None = None
    completed_at: float | None = None
    # Result data from automated steps
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def start(self) -> None:
        self.status = StepStatus.IN_PROGRESS
        self.started_at = time.time()

    def complete(self, result: dict[str, Any] | None = None) -> None:
        self.status = StepStatus.COMPLETED
        self.completed_at = time.time()
        if result:
            self.result = result

    def fail(self, error: str = "") -> None:
        self.status = StepStatus.FAILED
        self.completed_at = time.time()
        self.error = error

    def skip(self) -> None:
        self.status = StepStatus.SKIPPED
        self.completed_at = time.time()

    @property
    def is_terminal(self) -> bool:
        return self.status in (StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED)


@dataclass
class Pipeline:
    """A multi-step workflow that creates and sequences tasks."""

    name: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    description: str = ""
    status: PipelineStatus = PipelineStatus.DRAFT
    steps: list[PipelineStep] = field(default_factory=list)
    template_name: str = ""  # source template if created from one
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    tags: list[str] = field(default_factory=list)
    # Per-pipeline IANA timezone (e.g. "America/New_York"). Empty string =
    # evaluate schedules in server-local time (legacy behaviour). Added in
    # P2 of the editor-UX series so an operator who moves machines (or
    # whose worker fleet straddles time zones) gets the cron firing they
    # expect regardless of where the daemon happens to run.
    timezone: str = ""

    def get_step(self, step_id: str) -> PipelineStep | None:
        for step in self.steps:
            if step.id == step_id:
                return step
        return None

    def ready_steps(self) -> list[PipelineStep]:
        """Return steps whose dependencies are all satisfied (completed or skipped)."""
        satisfied_ids = {
            s.id for s in self.steps if s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED)
        }
        return [
            s
            for s in self.steps
            if s.status in (StepStatus.PENDING, StepStatus.READY)
            and all(d in satisfied_ids for d in s.depends_on)
        ]

    def advance(self) -> list[PipelineStep]:
        """Mark ready steps as READY and return them.

        Also updates overall pipeline status based on step states.
        """
        newly_ready: list[PipelineStep] = []
        for step in self.ready_steps():
            if step.status == StepStatus.PENDING:
                step.status = StepStatus.READY
                newly_ready.append(step)

        self._update_status()
        self.updated_at = time.time()
        return newly_ready

    def _update_status(self) -> None:
        """Derive pipeline status from step states."""
        if self.status == PipelineStatus.PAUSED:
            return
        if any(s.status == StepStatus.FAILED for s in self.steps):
            self.status = PipelineStatus.FAILED
            return
        if all(s.is_terminal for s in self.steps):
            self.status = PipelineStatus.COMPLETED
            self.completed_at = time.time()
            return
        if any(s.status == StepStatus.IN_PROGRESS for s in self.steps):
            self.status = PipelineStatus.RUNNING
            return
        if any(s.status == StepStatus.READY for s in self.steps):
            self.status = PipelineStatus.RUNNING

    def start(self) -> list[PipelineStep]:
        """Start the pipeline: set RUNNING and advance first steps."""
        self.status = PipelineStatus.RUNNING
        self.updated_at = time.time()
        return self.advance()

    def pause(self) -> None:
        self.status = PipelineStatus.PAUSED
        self.updated_at = time.time()

    def resume(self) -> list[PipelineStep]:
        """Resume a paused pipeline."""
        self.status = PipelineStatus.RUNNING
        self.updated_at = time.time()
        return self.advance()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "status": self.status.value,
            "template_name": self.template_name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "tags": self.tags,
            "timezone": self.timezone,
            "steps": [_step_to_dict(s) for s in self.steps],
        }

    @property
    def progress(self) -> float:
        """Fraction of steps completed (0.0 - 1.0)."""
        if not self.steps:
            return 0.0
        done = sum(1 for s in self.steps if s.is_terminal)
        return done / len(self.steps)


def _step_to_dict(step: PipelineStep) -> dict[str, Any]:
    return {
        "id": step.id,
        "name": step.name,
        "step_type": step.step_type.value,
        "status": step.status.value,
        "description": step.description,
        "depends_on": step.depends_on,
        "task_type": step.task_type,
        "assigned_worker": step.assigned_worker,
        "task_id": step.task_id,
        "service": step.service,
        "config": step.config,
        "schedule": step.schedule,
        "started_at": step.started_at,
        "completed_at": step.completed_at,
        "result": step.result,
        "error": step.error,
    }


def step_from_dict(d: dict[str, Any]) -> PipelineStep:
    return PipelineStep(
        id=d["id"],
        name=d["name"],
        step_type=StepType(d.get("step_type", "agent")),
        status=StepStatus(d.get("status", "pending")),
        description=d.get("description", ""),
        depends_on=d.get("depends_on", []),
        task_type=d.get("task_type", "chore"),
        assigned_worker=d.get("assigned_worker"),
        task_id=d.get("task_id"),
        service=d.get("service", ""),
        config=d.get("config", {}),
        schedule=d.get("schedule", ""),
        started_at=d.get("started_at"),
        completed_at=d.get("completed_at"),
        result=d.get("result", {}),
        error=d.get("error", ""),
    )


def pipeline_from_dict(d: dict[str, Any]) -> Pipeline:
    return Pipeline(
        id=d["id"],
        name=d["name"],
        description=d.get("description", ""),
        status=PipelineStatus(d.get("status", "draft")),
        steps=[step_from_dict(s) for s in d.get("steps", [])],
        template_name=d.get("template_name", ""),
        created_at=d.get("created_at", 0.0),
        updated_at=d.get("updated_at", 0.0),
        completed_at=d.get("completed_at"),
        tags=d.get("tags", []),
        timezone=d.get("timezone", ""),
    )
