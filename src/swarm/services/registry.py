"""Service registry — register and execute named service handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from swarm.logging import get_logger

_log = get_logger("services.registry")


@dataclass
class ServiceContext:
    """Context passed to service handlers during execution."""

    pipeline_id: str = ""
    step_id: str = ""
    pipeline_name: str = ""
    step_name: str = ""


@dataclass
class ServiceResult:
    """Result of a service execution."""

    success: bool = True
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class ServiceHandler(Protocol):
    """Protocol for async service handlers.

    Handlers MAY expose ``description`` and ``example_config`` class
    attributes — the registry surfaces these via ``describe()`` so the
    pipeline editor UI can offer a "Use example config" affordance and a
    short one-liner about each service. Missing attributes default to
    empty/empty-dict and are silently tolerated for backward compat.
    """

    async def execute(
        self,
        config: dict[str, Any],
        context: ServiceContext,
    ) -> ServiceResult: ...


class ServiceRegistry:
    """Registry of named service handlers for automated pipeline steps."""

    def __init__(self) -> None:
        self._handlers: dict[str, ServiceHandler] = {}

    def register(self, name: str, handler: ServiceHandler) -> None:
        """Register a service handler under *name*."""
        if name in self._handlers:
            _log.warning("overwriting service handler: %s", name)
        self._handlers[name] = handler
        _log.info("registered service: %s", name)

    def unregister(self, name: str) -> bool:
        """Remove a service handler. Returns True if it existed."""
        return self._handlers.pop(name, None) is not None

    def get(self, name: str) -> ServiceHandler | None:
        return self._handlers.get(name)

    @property
    def names(self) -> list[str]:
        return sorted(self._handlers.keys())

    def describe(self) -> list[dict[str, Any]]:
        """Return service metadata for UI consumption.

        Each entry: ``{"name": str, "description": str, "example_config": dict}``
        Sorted by name. Handlers without ``description`` / ``example_config``
        attributes get empty defaults — never raises.
        """
        out: list[dict[str, Any]] = []
        for name in sorted(self._handlers.keys()):
            handler = self._handlers[name]
            out.append(
                {
                    "name": name,
                    "description": getattr(handler, "description", "") or "",
                    "example_config": getattr(handler, "example_config", {}) or {},
                }
            )
        return out

    def has(self, name: str) -> bool:
        return name in self._handlers

    async def execute(
        self,
        name: str,
        config: dict[str, Any],
        context: ServiceContext | None = None,
    ) -> ServiceResult:
        """Execute a named service. Raises KeyError if not registered."""
        handler = self._handlers.get(name)
        if not handler:
            raise KeyError(f"Service not registered: {name}")
        ctx = context or ServiceContext()
        _log.info(
            "executing service: %s (pipeline=%s, step=%s)",
            name,
            ctx.pipeline_id,
            ctx.step_id,
        )
        try:
            return await handler.execute(config, ctx)
        except Exception as e:
            _log.error("service %s failed: %s", name, e, exc_info=True)
            return ServiceResult(success=False, error=str(e))
