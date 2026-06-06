"""Tests for direct gh-CLI submission."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from swarm.feedback.gh_submit import (
    GhSubmitError,
    check_gh_status,
    submit_via_gh,
)


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.stdin = None

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        del input
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_check_gh_status_missing():
    with patch("swarm.feedback.gh_submit._find_gh", return_value=None):
        status = await check_gh_status()
    assert not status.installed
    assert not status.authenticated


@pytest.mark.asyncio
async def test_check_gh_status_authenticated():
    fake = _FakeProc(
        0,
        stdout=(
            b"github.com\n"
            b"  Logged in to github.com account bschleifer (/path)\n"
            b"  Active account: true\n"
        ),
    )
    with (
        patch("swarm.feedback.gh_submit._find_gh", return_value="/usr/bin/gh"),
        patch(
            "swarm.feedback.gh_submit.asyncio.create_subprocess_exec",
            AsyncMock(return_value=fake),
        ),
    ):
        status = await check_gh_status()
    assert status.installed
    assert status.authenticated
    assert status.account == "bschleifer"


@pytest.mark.asyncio
async def test_check_gh_status_not_authenticated():
    fake = _FakeProc(1, stdout=b"You are not logged into any GitHub hosts.")
    with (
        patch("swarm.feedback.gh_submit._find_gh", return_value="/usr/bin/gh"),
        patch(
            "swarm.feedback.gh_submit.asyncio.create_subprocess_exec",
            AsyncMock(return_value=fake),
        ),
    ):
        status = await check_gh_status()
    assert status.installed
    assert not status.authenticated
    assert "not logged" in status.error.lower()


@pytest.mark.asyncio
async def test_submit_via_gh_success():
    fake = _FakeProc(
        0,
        stdout=b"https://github.com/bschleifer/swarm/issues/42\n",
    )
    with (
        patch("swarm.feedback.gh_submit._find_gh", return_value="/usr/bin/gh"),
        patch(
            "swarm.feedback.gh_submit.asyncio.create_subprocess_exec",
            AsyncMock(return_value=fake),
        ),
    ):
        result = await submit_via_gh(
            title="Test bug",
            body="## Description\n\nSomething broke.",
            category="bug",
        )
    assert result.url == "https://github.com/bschleifer/swarm/issues/42"


@pytest.mark.asyncio
async def test_submit_via_gh_missing_binary():
    with patch("swarm.feedback.gh_submit._find_gh", return_value=None):
        with pytest.raises(GhSubmitError, match="not installed"):
            await submit_via_gh(title="x", body="y", category="bug")


@pytest.mark.asyncio
async def test_submit_via_gh_failure():
    fake = _FakeProc(
        1,
        stdout=b"",
        stderr=b"HTTP 403: resource not accessible",
    )
    with (
        patch("swarm.feedback.gh_submit._find_gh", return_value="/usr/bin/gh"),
        patch(
            "swarm.feedback.gh_submit.asyncio.create_subprocess_exec",
            AsyncMock(return_value=fake),
        ),
    ):
        with pytest.raises(GhSubmitError, match="403"):
            await submit_via_gh(title="x", body="y", category="bug")


@pytest.mark.asyncio
async def test_submit_via_gh_timeout():
    class _Hanging:
        returncode = None

        async def communicate(self, input=None):
            del input
            await asyncio.sleep(60)
            return b"", b""

    with (
        patch("swarm.feedback.gh_submit._find_gh", return_value="/usr/bin/gh"),
        patch(
            "swarm.feedback.gh_submit.asyncio.create_subprocess_exec",
            AsyncMock(return_value=_Hanging()),
        ),
        patch("swarm.feedback.gh_submit._GH_TIMEOUT_SECONDS", 0.05),
    ):
        with pytest.raises(GhSubmitError, match="timed out"):
            await submit_via_gh(title="x", body="y", category="bug")


@pytest.mark.asyncio
async def test_submit_via_gh_label_missing_retries_without_label():
    """#feedback-audit D: when the label doesn't exist, gh fails; we retry once
    without --label and the report still goes through."""
    fail = _FakeProc(1, stderr=b"error: could not add label 'bug' to issue")
    ok = _FakeProc(0, stdout=b"https://github.com/o/r/issues/7\n")
    calls = {"n": 0}

    async def fake_exec(*args, **kwargs):
        calls["n"] += 1
        return fail if calls["n"] == 1 else ok

    with (
        patch("swarm.feedback.gh_submit._find_gh", return_value="/usr/bin/gh"),
        patch("swarm.feedback.gh_submit.asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        result = await submit_via_gh(title="x", body="y", category="bug")
    assert result.url == "https://github.com/o/r/issues/7"
    assert calls["n"] == 2  # main call (label fail) + no-label retry


@pytest.mark.asyncio
async def test_submit_via_gh_retry_timeout_raises_clean_error():
    """#feedback-audit C: a timeout on the no-label RETRY surfaces as a clean
    GhSubmitError, not a raw TimeoutError."""
    fail = _FakeProc(1, stderr=b"error: could not add label 'bug' to issue")
    calls = {"n": 0}

    async def fake_exec(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return fail  # first call: label-missing → triggers retry
        raise TimeoutError  # retry's create_subprocess_exec/wait_for path

    with (
        patch("swarm.feedback.gh_submit._find_gh", return_value="/usr/bin/gh"),
        patch("swarm.feedback.gh_submit.asyncio.create_subprocess_exec", side_effect=fake_exec),
        pytest.raises(GhSubmitError),
    ):
        await submit_via_gh(title="x", body="y", category="bug")
