"""Tests for the ShellCommand and WebhookNotify service handlers.

These external-action executors (run a shell command / POST a webhook) had no
direct coverage — only the registry and the youtube/file_uploader handlers did.
ShellCommand uses real (fast) subprocesses; WebhookNotify mocks aiohttp.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swarm.services.handlers.shell_command import ShellCommand
from swarm.services.handlers.webhook_notify import WebhookNotify
from swarm.services.registry import ServiceContext


@pytest.fixture
def ctx() -> ServiceContext:
    return ServiceContext(pipeline_id="p1", step_id="s1", pipeline_name="P", step_name="S")


class TestShellCommand:
    async def test_success_captures_stdout(self, ctx: ServiceContext) -> None:
        res = await ShellCommand().execute({"command": "echo hello"}, ctx)
        assert res.success is True
        assert res.data["stdout"] == "hello"
        assert res.data["returncode"] == 0

    async def test_nonzero_exit_is_failure(self, ctx: ServiceContext) -> None:
        res = await ShellCommand().execute({"command": "exit 3"}, ctx)
        assert res.success is False
        assert res.data["returncode"] == 3
        assert "3" in res.error

    async def test_missing_command_rejected(self, ctx: ServiceContext) -> None:
        res = await ShellCommand().execute({}, ctx)
        assert res.success is False
        assert "command is required" in res.error

    async def test_timeout(self, ctx: ServiceContext) -> None:
        res = await ShellCommand().execute({"command": "sleep 5", "timeout": 0.1}, ctx)
        assert res.success is False
        assert "timed out" in res.error.lower()


def _mock_session(*, status: int, body: str = "ok") -> MagicMock:
    resp = AsyncMock()
    resp.status = status
    resp.text = AsyncMock(return_value=body)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    session = AsyncMock()
    session.post = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


class TestWebhookNotify:
    async def test_missing_url_rejected(self, ctx: ServiceContext) -> None:
        res = await WebhookNotify().execute({}, ctx)
        assert res.success is False
        assert "url is required" in res.error

    async def test_successful_post(self, ctx: ServiceContext) -> None:
        session = _mock_session(status=200)
        with patch("aiohttp.ClientSession", return_value=session):
            res = await WebhookNotify().execute({"url": "https://example.com/hook"}, ctx)
        assert res.success is True
        session.post.assert_called_once()

    async def test_http_error_status_is_failure(self, ctx: ServiceContext) -> None:
        session = _mock_session(status=500, body="boom")
        with patch("aiohttp.ClientSession", return_value=session):
            res = await WebhookNotify().execute({"url": "https://example.com/hook"}, ctx)
        assert res.success is False

    async def test_does_not_mutate_caller_headers(self, ctx: ServiceContext) -> None:
        config = {"url": "https://example.com/hook", "headers": {}}
        session = _mock_session(status=200)
        with patch("aiohttp.ClientSession", return_value=session):
            await WebhookNotify().execute(config, ctx)
        # The handler must not leak Content-Type back into the caller's dict.
        assert config["headers"] == {}
