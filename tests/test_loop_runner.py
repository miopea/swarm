"""Tests for :class:`swarm.server.loop_runner.BackgroundLoopRunner`."""

from __future__ import annotations

import asyncio

import pytest

from swarm.server.loop_runner import BackgroundLoopRunner


async def _forever() -> None:
    """A loop that never terminates on its own — proxies a real periodic loop."""
    while True:
        await asyncio.sleep(0.01)


async def _quick() -> None:
    """A loop that runs once and exits — proxies an oneshot."""
    return None


class TestRegistration:
    def test_register_then_names(self) -> None:
        runner = BackgroundLoopRunner()
        runner.register("a", _forever)
        runner.register("b", _quick)
        assert runner.names() == ["a", "b"]

    def test_duplicate_register_raises(self) -> None:
        runner = BackgroundLoopRunner()
        runner.register("a", _forever)
        with pytest.raises(ValueError, match="already registered"):
            runner.register("a", _forever)

    def test_get_unstarted_returns_none(self) -> None:
        runner = BackgroundLoopRunner()
        runner.register("a", _forever)
        assert runner.get("a") is None


class TestStartAll:
    async def test_starts_every_enabled_loop(self) -> None:
        runner = BackgroundLoopRunner()
        runner.register("a", _forever)
        runner.register("b", _forever)
        runner.start_all()
        try:
            for n in ("a", "b"):
                t = runner.get(n)
                assert t is not None
                assert not t.done()
        finally:
            await runner.cancel_all()

    async def test_skips_disabled_loop(self) -> None:
        runner = BackgroundLoopRunner()
        runner.register("on", _forever, enabled=True)
        runner.register("off", _forever, enabled=False)
        runner.start_all()
        try:
            assert runner.get("on") is not None
            assert runner.get("off") is None
        finally:
            await runner.cancel_all()

    async def test_idempotent_when_already_running(self) -> None:
        runner = BackgroundLoopRunner()
        runner.register("a", _forever)
        runner.start_all()
        first = runner.get("a")
        runner.start_all()  # should not replace the live task
        try:
            second = runner.get("a")
            assert first is second
        finally:
            await runner.cancel_all()

    async def test_replaces_done_task_on_restart(self) -> None:
        runner = BackgroundLoopRunner()
        runner.register("oneshot", _quick)
        runner.start_all()
        first = runner.get("oneshot")
        assert first is not None
        await first  # wait for it to finish
        assert first.done()
        runner.start_all()  # should mint a new task
        try:
            second = runner.get("oneshot")
            assert second is not None
            assert second is not first
        finally:
            await runner.cancel_all()


class TestSingleStart:
    async def test_start_disabled_returns_false(self) -> None:
        runner = BackgroundLoopRunner()
        runner.register("off", _forever, enabled=False)
        assert runner.start("off") is False
        assert runner.get("off") is None

    async def test_start_unknown_returns_false(self) -> None:
        runner = BackgroundLoopRunner()
        assert runner.start("ghost") is False

    async def test_start_already_running_returns_false(self) -> None:
        runner = BackgroundLoopRunner()
        runner.register("a", _forever)
        assert runner.start("a") is True
        try:
            assert runner.start("a") is False  # already live
        finally:
            await runner.cancel_all()


class TestCancelAll:
    async def test_cancels_and_awaits_live_tasks(self) -> None:
        runner = BackgroundLoopRunner()
        runner.register("a", _forever)
        runner.register("b", _forever)
        runner.start_all()
        a, b = runner.get("a"), runner.get("b")
        await runner.cancel_all()
        assert a is not None and b is not None
        assert a.done() and b.done()

    async def test_cancel_all_swallows_exceptions(self) -> None:
        async def _raises() -> None:
            raise RuntimeError("kaboom")

        runner = BackgroundLoopRunner()
        runner.register("bad", _raises)
        runner.start_all()
        # Let the task actually run + fail.
        await asyncio.sleep(0)
        # Must NOT propagate the RuntimeError.
        await runner.cancel_all()

    async def test_cancel_all_with_no_running_tasks_is_noop(self) -> None:
        runner = BackgroundLoopRunner()
        runner.register("a", _forever)  # registered but never started
        await runner.cancel_all()  # should not raise

    async def test_cancel_all_clears_registry(self) -> None:
        runner = BackgroundLoopRunner()
        runner.register("a", _forever)
        runner.start_all()
        await runner.cancel_all()
        # After cancellation the runner has no live tasks; start_all() can
        # mint fresh ones.
        runner.start_all()
        try:
            assert runner.get("a") is not None
        finally:
            await runner.cancel_all()
