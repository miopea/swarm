"""Task History — append-only audit log for task lifecycle events."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from swarm.logging import get_logger

_log = get_logger("tasks.history")

_DEFAULT_LOG_PATH = Path.home() / ".swarm" / "task_history.jsonl"
_DEFAULT_MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
_DEFAULT_MAX_ROTATIONS = 2


class TaskAction(Enum):
    CREATED = "CREATED"
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    ASSIGNED = "ASSIGNED"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    UNASSIGNED = "UNASSIGNED"
    BLOCKED = "BLOCKED"  # #876: parked on an external/upstream dependency
    REOPENED = "REOPENED"
    REMOVED = "REMOVED"
    EDITED = "EDITED"


@dataclass
class TaskEvent:
    timestamp: float
    task_id: str
    action: TaskAction
    actor: str = "user"
    detail: str = ""

    @property
    def formatted_time(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp))

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "task_id": self.task_id,
            "action": self.action.value,
            "actor": self.actor,
            "detail": self.detail,
        }


class TaskHistory:
    """Append-only task audit log with JSONL file persistence."""

    def __init__(
        self,
        log_file: Path | None = None,
        max_file_size: int = _DEFAULT_MAX_FILE_SIZE,
        max_rotations: int = _DEFAULT_MAX_ROTATIONS,
    ) -> None:
        self._log_file = log_file or _DEFAULT_LOG_PATH
        self._max_file_size = max_file_size
        self._max_rotations = max_rotations
        self._write_semaphore = asyncio.Semaphore(50)

    def append(
        self,
        task_id: str,
        action: TaskAction,
        actor: str = "user",
        detail: str = "",
    ) -> TaskEvent:
        event = TaskEvent(
            timestamp=time.time(),
            task_id=task_id,
            action=action,
            actor=actor,
            detail=detail,
        )
        self._append_to_file(event)
        return event

    def get_events(self, task_id: str, limit: int = 50) -> list[TaskEvent]:
        """Get events for a specific task from the JSONL log."""
        events: list[TaskEvent] = []
        if not self._log_file.exists():
            return events
        try:
            with open(self._log_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        if d.get("task_id") == task_id:
                            events.append(
                                TaskEvent(
                                    timestamp=d["timestamp"],
                                    task_id=d["task_id"],
                                    action=TaskAction(d["action"]),
                                    actor=d.get("actor", "user"),
                                    detail=d.get("detail", ""),
                                )
                            )
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
        except OSError:
            _log.warning("failed to read task history from %s", self._log_file, exc_info=True)
        return events[-limit:]

    def _append_to_file(self, event: TaskEvent) -> None:
        """Append a task event to the JSONL file.

        Offloads the blocking file I/O to a thread when an event loop is
        running, keeping the main async loop unblocked.  Uses a semaphore
        to bound the number of concurrent pending writes.
        """
        try:
            loop = asyncio.get_running_loop()

            async def _bounded_write() -> None:
                async with self._write_semaphore:
                    await asyncio.to_thread(self._write_event, event)

            loop.call_soon_threadsafe(lambda: asyncio.create_task(_bounded_write()))
        except RuntimeError:
            # No event loop — write synchronously (startup / tests)
            self._write_event(event)

    def _write_event(self, event: TaskEvent) -> None:
        """Synchronously write a task event to the JSONL file.

        Uses ``fcntl.flock`` to prevent concurrent write corruption
        when multiple threads or processes append simultaneously.
        """
        import fcntl

        try:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(event.to_dict())
            with open(self._log_file, "a") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.write(line + "\n")
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            self._rotate_if_needed()
        except OSError:
            _log.warning("failed to append to task history %s", self._log_file, exc_info=True)

    def _rotate_if_needed(self) -> None:
        if not self._log_file.exists():
            return
        try:
            if self._log_file.stat().st_size <= self._max_file_size:
                return
            for i in range(self._max_rotations, 0, -1):
                if i == self._max_rotations:
                    rotated = self._log_file.with_suffix(f".jsonl.{i}")
                    if rotated.exists():
                        rotated.unlink()
                    continue
                src = self._log_file.with_suffix(f".jsonl.{i}") if i > 0 else self._log_file
                dst = self._log_file.with_suffix(f".jsonl.{i + 1}")
                if src.exists():
                    src.rename(dst)
            if self._log_file.exists():
                self._log_file.rename(self._log_file.with_suffix(".jsonl.1"))
            _log.info("rotated task history %s", self._log_file)
        except OSError:
            _log.warning("failed to rotate task history", exc_info=True)
