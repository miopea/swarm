"""Task persistence — save/load task board state to disk."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Protocol

from swarm.logging import get_logger
from swarm.tasks.task import (
    SwarmTask,
    TaskPriority,
    TaskStatus,
    TaskType,
    VerificationStatus,
)

_log = get_logger("tasks.store")

_DEFAULT_PATH = Path.home() / ".swarm" / "tasks.json"


class TaskStore(Protocol):
    """Protocol for task persistence backends."""

    def save(self, tasks: dict[str, SwarmTask]) -> None: ...
    def load(self) -> dict[str, SwarmTask]: ...


class FileTaskStore:
    """Persist tasks as JSON to a file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _DEFAULT_PATH

    def save(self, tasks: dict[str, SwarmTask]) -> None:
        """Write all tasks to disk atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = [_task_to_dict(t) for t in tasks.values()]
        try:
            tmp = self.path.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, self.path)
        except OSError:
            _log.warning("failed to save tasks to %s", self.path, exc_info=True)

    def load(self) -> dict[str, SwarmTask]:
        """Read tasks from disk. Returns empty dict if file missing or corrupt."""
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, list):
                _log.warning("tasks file %s does not contain a list", self.path)
                return {}
            tasks: dict[str, SwarmTask] = {}
            for item in data:
                task = _dict_to_task(item)
                tasks[task.id] = task
            _log.info("loaded %d tasks from %s", len(tasks), self.path)
            return tasks
        except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
            _log.warning("failed to load tasks from %s", self.path, exc_info=True)
            return {}

    def backup(self, max_backups: int = 5) -> Path | None:
        """Create a timestamped backup of the task file. Returns backup path or None."""
        if not self.path.exists():
            return None
        ts = f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}"
        backup_path = self.path.parent / f"{self.path.name}.bak.{ts}"
        try:
            shutil.copy2(self.path, backup_path)
            _log.info("task backup created: %s", backup_path)
        except OSError:
            _log.warning("failed to create task backup", exc_info=True)
            return None
        # Rotate — keep only the newest max_backups
        backups = sorted(self.path.parent.glob(f"{self.path.name}.bak.*"), reverse=True)
        for old in backups[max_backups:]:
            try:
                old.unlink()
            except OSError:
                pass
        return backup_path


def _task_to_dict(task: SwarmTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status.value,
        "priority": task.priority.value,
        "task_type": task.task_type.value,
        "assigned_worker": task.assigned_worker,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "completed_at": task.completed_at,
        "depends_on": task.depends_on,
        "tags": task.tags,
        "attachments": task.attachments,
        "resolution": task.resolution,
        "source_email_id": task.source_email_id,
        "jira_key": task.jira_key,
        "number": task.number,
        "is_cross_project": task.is_cross_project,
        "source_worker": task.source_worker,
        "target_worker": task.target_worker,
        "dependency_type": task.dependency_type,
        "acceptance_criteria": task.acceptance_criteria,
        "context_refs": task.context_refs,
        "cost_budget": task.cost_budget,
        "cost_spent": task.cost_spent,
        "learnings": task.learnings,
        "block_reason": task.block_reason,
        "external_blocker_ref": task.external_blocker_ref,
        "verification_status": task.verification_status.value,
        "verification_reason": task.verification_reason,
        "verification_reopen_count": task.verification_reopen_count,
        "effort_tier": task.effort_tier,
    }


def _dict_to_task(d: dict[str, Any]) -> SwarmTask:
    return SwarmTask(
        id=d["id"],
        title=d["title"],
        description=d.get("description", ""),
        status=TaskStatus(d["status"]),
        priority=TaskPriority(d.get("priority", "normal")),
        task_type=TaskType(d.get("task_type", "chore")),
        assigned_worker=d.get("assigned_worker"),
        created_at=d.get("created_at", 0.0),
        updated_at=d.get("updated_at", 0.0),
        completed_at=d.get("completed_at"),
        depends_on=d.get("depends_on", []),
        tags=d.get("tags", []),
        attachments=d.get("attachments", []),
        resolution=d.get("resolution", ""),
        source_email_id=d.get("source_email_id", ""),
        jira_key=d.get("jira_key", ""),
        number=d.get("number", 0),
        is_cross_project=d.get("is_cross_project", False),
        source_worker=d.get("source_worker", ""),
        target_worker=d.get("target_worker", ""),
        dependency_type=d.get("dependency_type", "blocks"),
        acceptance_criteria=d.get("acceptance_criteria", []),
        context_refs=d.get("context_refs", []),
        cost_budget=d.get("cost_budget", 0.0),
        cost_spent=d.get("cost_spent", 0.0),
        learnings=d.get("learnings", ""),
        block_reason=d.get("block_reason", ""),
        external_blocker_ref=d.get("external_blocker_ref", ""),
        verification_status=VerificationStatus(d.get("verification_status", "not_run")),
        verification_reason=d.get("verification_reason", ""),
        verification_reopen_count=d.get("verification_reopen_count", 0),
        effort_tier=d.get("effort_tier", ""),
    )
