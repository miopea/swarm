"""Tests for service registry and runner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

import pytest

from swarm.services.registry import ServiceContext, ServiceRegistry, ServiceResult


@dataclass
class _EchoHandler:
    """Test service that echoes its config back."""

    async def execute(
        self,
        config: dict[str, Any],
        context: ServiceContext,
    ) -> ServiceResult:
        return ServiceResult(success=True, data={"echo": config})


@dataclass
class _FailHandler:
    """Test service that always fails."""

    async def execute(
        self,
        config: dict[str, Any],
        context: ServiceContext,
    ) -> ServiceResult:
        raise RuntimeError("intentional failure")


class TestServiceRegistry:
    def test_register_and_list(self) -> None:
        reg = ServiceRegistry()
        reg.register("echo", _EchoHandler())
        assert "echo" in reg.names
        assert reg.has("echo")

    def test_unregister(self) -> None:
        reg = ServiceRegistry()
        reg.register("echo", _EchoHandler())
        assert reg.unregister("echo")
        assert not reg.has("echo")
        assert not reg.unregister("echo")

    def test_describe_returns_metadata(self) -> None:
        """P1: describe() feeds the pipeline-editor service dropdown — must
        return name/description/example_config for every registered handler,
        defaulting empty strings/dicts when the handler doesn't expose them.
        """

        class _RichHandler:
            description = "A handler with metadata."
            example_config: ClassVar[dict[str, Any]] = {"foo": "bar"}

            async def execute(
                self, config: dict[str, Any], context: ServiceContext
            ) -> ServiceResult:
                return ServiceResult(success=True)

        reg = ServiceRegistry()
        reg.register("rich", _RichHandler())
        reg.register("bare", _EchoHandler())
        described = {entry["name"]: entry for entry in reg.describe()}
        assert described["rich"]["description"] == "A handler with metadata."
        assert described["rich"]["example_config"] == {"foo": "bar"}
        # Backward-compat: handler without the optional attrs gets empties.
        assert described["bare"]["description"] == ""
        assert described["bare"]["example_config"] == {}

    @pytest.mark.asyncio
    async def test_execute_success(self) -> None:
        reg = ServiceRegistry()
        reg.register("echo", _EchoHandler())
        result = await reg.execute("echo", {"key": "value"})
        assert result.success
        assert result.data == {"echo": {"key": "value"}}

    @pytest.mark.asyncio
    async def test_execute_not_registered(self) -> None:
        reg = ServiceRegistry()
        with pytest.raises(KeyError, match="not registered"):
            await reg.execute("missing", {})

    @pytest.mark.asyncio
    async def test_execute_handler_exception(self) -> None:
        reg = ServiceRegistry()
        reg.register("fail", _FailHandler())
        result = await reg.execute("fail", {})
        assert not result.success
        assert "intentional failure" in result.error

    @pytest.mark.asyncio
    async def test_execute_with_context(self) -> None:
        reg = ServiceRegistry()
        reg.register("echo", _EchoHandler())
        ctx = ServiceContext(
            pipeline_id="p1",
            step_id="s1",
            pipeline_name="test",
            step_name="echo step",
        )
        result = await reg.execute("echo", {"x": 1}, context=ctx)
        assert result.success
