"""#1486 — queen_reassign_task must not claim a dispatch it did not perform.

THE INCIDENT. `start_task` refuses anything whose status is not ASSIGNED.
Assignment deliberately PRESERVES a BACKLOG/parked status (routing parked work
must not un-park it). So reassigning parked work with `start=true` produced:
`start_task` raised, `_fire_async` discarded the exception, and the handler
returned "…and dispatched" regardless. The worker was never messaged and the
task never reached ACTIVE. hub, public-website and root each sat on urgent work
for hours because the Queen believed that string.
"""

from __future__ import annotations

import asyncio
import types
from unittest.mock import MagicMock

import pytest

from swarm.mcp.queen_handlers._tasks import _dispatch_after_reassign, _fire_async
from swarm.tasks.task import TaskStatus


def _task(status: TaskStatus, number: int = 1358):
    return types.SimpleNamespace(id="t1", number=number, status=status)


def _daemon_with(status: TaskStatus) -> MagicMock:
    d = MagicMock()
    d.task_board.get.return_value = _task(status)
    return d


def _text(result) -> str:
    return result[0]["text"]


@pytest.mark.parametrize("status", [TaskStatus.BACKLOG, TaskStatus.BLOCKED, TaskStatus.UNASSIGNED])
def test_does_not_claim_dispatch_for_a_non_assigned_task(status: TaskStatus) -> None:
    """The exact shape that lied: a status dispatch cannot act on."""
    d = _daemon_with(status)
    out = _text(_dispatch_after_reassign(d, _task(status), "swarm", "platform"))

    assert "NOT dispatched" in out, out
    assert status.value in out, "the refusal must name the status that blocked it"
    # And it must not have fired the call whose failure would be invisible.
    d.assign_and_start_task.assert_not_called()


@pytest.mark.parametrize("status", [TaskStatus.BACKLOG, TaskStatus.BLOCKED])
def test_refusal_names_something_that_resolves_it(status: TaskStatus) -> None:
    """A refusal that does not say what to do next is how this went unnoticed."""
    d = _daemon_with(status)
    out = _text(_dispatch_after_reassign(d, _task(status), "swarm", "platform"))
    assert "queen_prompt_worker" in out or "Un-park" in out, out


def test_dispatches_when_the_task_really_is_assigned() -> None:
    d = _daemon_with(TaskStatus.ASSIGNED)
    out = _text(_dispatch_after_reassign(d, _task(TaskStatus.ASSIGNED), "swarm", "platform"))

    assert "dispatched" in out
    assert "NOT dispatched" not in out
    d.assign_and_start_task.assert_called_once()


def test_success_text_does_not_overclaim() -> None:
    """Dispatch is fire-and-forget, so the wording must not promise arrival.

    The whole failure was a confident past-tense claim about an asynchronous
    action nobody had observed complete.
    """
    d = _daemon_with(TaskStatus.ASSIGNED)
    out = _text(_dispatch_after_reassign(d, _task(TaskStatus.ASSIGNED), "swarm", "platform"))
    assert "asynchronous" in out.lower()
    assert "ACTIVE" in out, "must tell the reader what to confirm"


@pytest.mark.asyncio
async def test_fire_async_logs_failures_instead_of_discarding_them(caplog) -> None:
    """The mechanism that made the bug invisible.

    The old done-callback was `t.exception()` alone — it retrieved the exception
    purely to silence asyncio's warning, then dropped it. A dispatch that raised
    left no trace anywhere.
    """

    async def boom() -> None:
        raise RuntimeError("start_task refused: task is backlog")

    with caplog.at_level("ERROR"):
        _fire_async(boom(), label="dispatch #1358 -> platform")
        await asyncio.sleep(0.05)

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "dispatch #1358 -> platform" in joined, f"label missing from log: {joined!r}"
    assert "backlog" in joined, "the underlying reason must survive into the log"


@pytest.mark.asyncio
async def test_fire_async_stays_quiet_on_success() -> None:
    """A logger that cries on success gets muted, and then it protects nothing."""
    ran = asyncio.Event()

    async def fine() -> None:
        ran.set()

    _fire_async(fine(), label="ok")
    await asyncio.wait_for(ran.wait(), timeout=1)
