"""A task created with ``target_worker`` must own its worker before the reply lands.

FOUND IN PRODUCTION 2026-08-08. ``swarm_create_task`` returns as soon as the row
exists; the assignment rode a background coroutine that first awaited Outcomes-criteria
SYNTHESIS — an LLM call taking seconds. So a caller that created a task routed to itself
and then immediately acted on it was told the task was "not assigned to you", about a
task it had just routed to itself moments earlier.

It surfaced through ``swarm_request_jira_ticket``, but nothing about it is Jira-specific:
any verb that reads ownership straight after creating a task hits the same window, and
the reply gives no hint that ownership is still in flight.

THE ORDERING IS NOT ARBITRARY EITHER WAY. When the task IS dispatched, synthesis must
run first — the criteria have to be in the message the target worker receives, which is
why it was written that way. With ``start=False`` no message is ever sent, so nothing
needs the criteria first and ownership can land on the next loop tick instead.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.mcp.handlers._create import (
    _dispatch_then_synthesize,
    _schedule_synth_dispatch,
    _synthesize_then_dispatch,
)


class _Recorder:
    """Records the order of synthesis vs assignment, with synthesis made SLOW.

    The delay is the point: with both instantaneous, either order passes and the test
    proves nothing about which ran first.
    """

    def __init__(self) -> None:
        self.order: list[str] = []

    def daemon(self) -> Any:
        d = MagicMock()
        task = MagicMock()
        d.task_board.get.return_value = task

        async def _synth(_task: Any, actor: str = "") -> None:
            await asyncio.sleep(0.05)
            self.order.append("synthesis")

        d.tasks.apply_synthesized_criteria = AsyncMock(side_effect=_synth)
        return d

    async def dispatch(self) -> bool:
        self.order.append("assign")
        return True


@pytest.mark.asyncio
async def test_assignment_lands_before_synthesis_when_nothing_is_dispatched():
    """THE FIX. start=False has no message to enrich, so ownership must not wait."""
    rec = _Recorder()
    await _dispatch_then_synthesize(rec.daemon(), "t1", "api", rec.dispatch())

    assert rec.order == ["assign", "synthesis"], (
        f"ownership is still queued behind an LLM call: {rec.order}"
    )


@pytest.mark.asyncio
async def test_synthesis_still_runs_first_when_the_task_IS_dispatched():
    """The other half, and the reason this is not a blanket reorder: the criteria have
    to be in the message the worker receives."""
    rec = _Recorder()
    await _synthesize_then_dispatch(rec.daemon(), "t1", "api", rec.dispatch())

    assert rec.order == ["synthesis", "assign"], (
        f"the dispatched message would go out without its acceptance criteria: {rec.order}"
    )


@pytest.mark.asyncio
async def test_synthesis_failure_never_swallows_the_assignment():
    """Synthesis is best-effort. A task that exists but belongs to nobody because a
    model call raised is worse than a task with no criteria."""
    rec = _Recorder()
    d = rec.daemon()
    d.tasks.apply_synthesized_criteria = AsyncMock(side_effect=RuntimeError("model down"))

    await _dispatch_then_synthesize(d, "t1", "api", rec.dispatch())

    assert "assign" in rec.order, "a synthesis failure lost the assignment"


def test_the_scheduler_picks_the_order_from_whether_it_dispatches():
    """Pins the wiring, not just the two coroutines. Both orders exist and are correct;
    the defect was choosing the wrong one, so the choice is what needs a test."""
    picked: list[Any] = []

    class _Loop:
        def create_task(self, coro: Any) -> Any:
            picked.append(coro.cr_code.co_name)
            coro.close()
            t = MagicMock()
            return t

    import swarm.mcp.handlers._create as mod

    real_get_loop = asyncio.get_running_loop
    asyncio.get_running_loop = lambda: _Loop()  # type: ignore[assignment]
    try:
        rec = _Recorder()
        d = rec.daemon()
        _schedule_synth_dispatch(d, "t1", "api", "caller", rec.dispatch(), dispatching=False)
        _schedule_synth_dispatch(d, "t2", "api", "caller", rec.dispatch(), dispatching=True)
    finally:
        asyncio.get_running_loop = real_get_loop  # type: ignore[assignment]

    assert picked == ["_dispatch_then_synthesize", "_synthesize_then_dispatch"], (
        f"the scheduler chose the wrong ordering for one of the two cases: {picked}"
    )
    assert mod._dispatch_then_synthesize is not None
