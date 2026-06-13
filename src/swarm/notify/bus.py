"""Notification bus — event routing for swarm notifications."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from swarm.logging import get_logger

_log = get_logger("notify.bus")


class EventType(Enum):
    WORKER_IDLE = "worker_idle"
    WORKER_STUNG = "worker_stung"
    WORKER_ESCALATED = "worker_escalated"
    DRONE_ACTION = "drone_action"
    QUEEN_RESPONSE = "queen_response"
    TASK_ASSIGNED = "task_assigned"
    TASK_COMPLETED = "task_completed"
    RESOURCE_PRESSURE = "resource_pressure"
    DSTATE_DETECTED = "dstate_detected"
    CONTEXT_PRESSURE = "context_pressure"
    DAEMON_HEALTH = "daemon_health"
    TUNNEL_DOWN = "tunnel_down"
    TASK_FAILED = "task_failed"
    TASK_REOPENED = "task_reopened"
    PIPELINE_STARTED = "pipeline_started"
    PIPELINE_FINISHED = "pipeline_finished"
    DAILY_DIGEST = "daily_digest"


class Severity(Enum):
    INFO = "info"
    WARNING = "warning"
    URGENT = "urgent"


@dataclass
class NotifyEvent:
    event_type: EventType
    title: str
    message: str
    severity: Severity = Severity.INFO
    worker_name: str | None = None
    timestamp: float = field(default_factory=time.time)


# Backend protocol: any callable that takes a NotifyEvent
NotifyBackend = Callable[[NotifyEvent], None]


def filtered_backend(backend: NotifyBackend, events: list[str]) -> NotifyBackend:
    """Wrap a backend to only receive events matching the given type names.

    Unknown event names are dropped with a debug log rather than raising.
    Reason: the dashboard's notification config is treated as advisory —
    a typo or stale event name shouldn't block the entire config save
    (see ``tests/test_api.py::test_config_notification_validation``).
    """
    if not events:
        return backend
    allowed: set[EventType] = set()
    for e in events:
        try:
            allowed.add(EventType(e))
        except ValueError:
            _log.debug("filtered_backend: ignoring unknown event type %r", e)
    if not allowed:
        # All names were unknown — nothing this backend can match.
        # Return a no-op rather than the unwrapped backend (which would
        # receive every event, the opposite of "filter to none").
        return lambda _e: None

    def _wrapper(event: NotifyEvent) -> None:
        if event.event_type in allowed:
            backend(event)

    return _wrapper


class NotificationBus:
    """Central event bus for routing notifications to backends."""

    def __init__(self, debounce_seconds: float = 5.0) -> None:
        self._backends: list[NotifyBackend] = []
        self._debounce = debounce_seconds
        self._last_sent: dict[str, float] = {}
        self._templates: dict[str, str] = {}

    def set_templates(self, templates: dict[str, str]) -> None:
        """Set message templates. Keys are event type values, values are format strings."""
        self._templates = templates

    def add_backend(self, backend: NotifyBackend) -> None:
        self._backends.append(backend)

    def emit(self, event: NotifyEvent) -> None:
        """Emit an event to all registered backends, with debouncing."""
        key = f"{event.event_type.value}:{event.worker_name or ''}"
        now = time.time()
        last = self._last_sent.get(key, 0.0)

        if now - last < self._debounce:
            _log.debug("debounced notification: %s", key)
            return

        self._last_sent[key] = now
        _log.info("notify: %s — %s", event.event_type.value, event.title)

        for backend in self._backends:
            try:
                backend(event)
            except (OSError, TimeoutError, ConnectionError):
                _log.warning("notification backend %s failed", backend, exc_info=True)
            except Exception:
                _log.warning("unexpected error in notification backend %s", backend, exc_info=True)

    def _format_message(self, event_type: EventType, default: str, **kwargs: str) -> str:
        """Apply a custom template if configured, otherwise return the default."""
        template = self._templates.get(event_type.value)
        if template:
            try:
                return template.format(**kwargs)
            except (KeyError, ValueError):
                _log.debug("bad template for %s, using default", event_type.value)
        return default

    def emit_worker_idle(self, worker_name: str) -> None:
        msg = self._format_message(
            EventType.WORKER_IDLE,
            f"Worker {worker_name} is waiting for input",
            worker=worker_name,
        )
        self.emit(
            NotifyEvent(
                event_type=EventType.WORKER_IDLE,
                title=f"{worker_name} is idle",
                message=msg,
                severity=Severity.INFO,
                worker_name=worker_name,
            )
        )

    def emit_worker_stung(self, worker_name: str) -> None:
        msg = self._format_message(
            EventType.WORKER_STUNG,
            f"Worker {worker_name} has exited unexpectedly",
            worker=worker_name,
        )
        self.emit(
            NotifyEvent(
                event_type=EventType.WORKER_STUNG,
                title=f"{worker_name} exited",
                message=msg,
                severity=Severity.WARNING,
                worker_name=worker_name,
            )
        )

    def emit_escalation(self, worker_name: str, reason: str) -> None:
        msg = self._format_message(
            EventType.WORKER_ESCALATED,
            f"Drones escalated {worker_name}: {reason}",
            worker=worker_name,
            reason=reason,
        )
        self.emit(
            NotifyEvent(
                event_type=EventType.WORKER_ESCALATED,
                title=f"{worker_name} escalated",
                message=msg,
                severity=Severity.URGENT,
                worker_name=worker_name,
            )
        )

    def emit_task_assigned(self, worker_name: str, task_title: str) -> None:
        self.emit(
            NotifyEvent(
                event_type=EventType.TASK_ASSIGNED,
                title=f"Task → {worker_name}",
                message=f"Assigned '{task_title}' to {worker_name}",
                severity=Severity.INFO,
                worker_name=worker_name,
            )
        )

    def emit_task_completed(self, worker_name: str, task_title: str) -> None:
        self.emit(
            NotifyEvent(
                event_type=EventType.TASK_COMPLETED,
                title=f"Task done: {task_title}",
                message=f"{worker_name} completed '{task_title}'",
                severity=Severity.INFO,
                worker_name=worker_name,
            )
        )

    def emit_task_failed(self, worker_name: str, task_title: str) -> None:
        self.emit(
            NotifyEvent(
                event_type=EventType.TASK_FAILED,
                title=f"Task FAILED: {task_title}",
                message=f"'{task_title}' was marked failed (worker: {worker_name})",
                severity=Severity.WARNING,
                worker_name=worker_name,
            )
        )

    def emit_task_reopened(self, worker_name: str, task_title: str) -> None:
        self.emit(
            NotifyEvent(
                event_type=EventType.TASK_REOPENED,
                title=f"Task reopened: {task_title}",
                message=f"'{task_title}' was reopened (worker: {worker_name})",
                severity=Severity.INFO,
                worker_name=worker_name,
            )
        )

    def emit_pipeline_started(self, pipeline_name: str) -> None:
        self.emit(
            NotifyEvent(
                event_type=EventType.PIPELINE_STARTED,
                title=f"Pipeline started: {pipeline_name}",
                message=f"Pipeline '{pipeline_name}' is running",
                severity=Severity.INFO,
                worker_name=pipeline_name,
            )
        )

    def emit_pipeline_finished(self, pipeline_name: str, *, failed: bool) -> None:
        self.emit(
            NotifyEvent(
                event_type=EventType.PIPELINE_FINISHED,
                title=(
                    f"Pipeline FAILED: {pipeline_name}"
                    if failed
                    else f"Pipeline completed: {pipeline_name}"
                ),
                message=(
                    f"Pipeline '{pipeline_name}' "
                    + ("has a failed step." if failed else "finished successfully.")
                ),
                severity=Severity.URGENT if failed else Severity.INFO,
                worker_name=pipeline_name,
            )
        )

    def emit_daily_digest(self, title: str, message: str) -> None:
        self.emit(
            NotifyEvent(
                event_type=EventType.DAILY_DIGEST,
                title=title,
                message=message,
                severity=Severity.INFO,
            )
        )

    def emit_resource_pressure(self, level: str, mem_pct: float, swap_pct: float) -> None:
        self.emit(
            NotifyEvent(
                event_type=EventType.RESOURCE_PRESSURE,
                title=f"Memory pressure: {level}",
                message=f"Memory {mem_pct:.0f}% / Swap {swap_pct:.0f}% — pressure level: {level}",
                severity=Severity.WARNING if level != "critical" else Severity.URGENT,
            )
        )

    def emit_dstate_detected(self, pid: int, comm: str, worker_name: str) -> None:
        self.emit(
            NotifyEvent(
                event_type=EventType.DSTATE_DETECTED,
                title=f"D-state process: {comm}",
                message=f"PID {pid} ({comm}) in uninterruptible sleep under {worker_name}",
                severity=Severity.URGENT,
                worker_name=worker_name,
            )
        )

    def emit_context_pressure(self, worker_name: str, usage_pct: float, level: str) -> None:
        severity = Severity.URGENT if level == "critical" else Severity.WARNING
        self.emit(
            NotifyEvent(
                event_type=EventType.CONTEXT_PRESSURE,
                title=f"Context {level}: {worker_name}",
                message=(
                    f"Worker {worker_name} context window at {usage_pct:.0%}"
                    f" — pressure level: {level}"
                ),
                severity=severity,
                worker_name=worker_name,
            )
        )
