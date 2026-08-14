"""#1608 — a send that cannot be delivered must say so.

MEASURED ON A REAL PICKER, 2026-08-14. platform-api sat on a plan prompt for #1614.
The Queen called `queen_prompt_worker` with the approval; it reported
"Prompt sent… Target engagement: RESTING 10s". Ten minutes later the picker was still
open and the message was nowhere in the PTY — it was sitting in `_deferred_keys`.

FROM THE CALLER'S SIDE A HELD MESSAGE AND A DELIVERED ONE WERE IDENTICAL. That is the
whole defect, and it is why the Queen spent a night believing she had no way to act on a
stalled worker: the one tool that could reach it kept telling her it had.

Two causes, both fixed here:
  · `send_keys` returned None whether it delivered or deferred, so no caller could tell.
  · `_handle_prompt_worker` fires the send through `_fire_async`, which returns before
    the guard runs — so even a truthful return value would have arrived too late.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.pty.process import WorkerProcess

# The real plan picker platform-api was sitting on, captured before it cleared.
REAL_PLAN_PICKER = """\
  Ready to code?

  Here is the plan for #1614:
  /home/bschleifer/.claude/plans/some-plan.md

  Would you like to proceed?
❯ 1. Yes, and use auto mode
   2. Yes, manually approve edits
   3. Tell Claude what to change
"""


class _Proc(WorkerProcess):
    def __init__(self) -> None:
        super().__init__(name="platform-api", cwd="/tmp")
        self.writes: list[bytes] = []

    async def _write(self, data: bytes) -> None:  # type: ignore[override]
        self.writes.append(data)


# ---------------------------------------------------------------------------
# send_keys now reports the hold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_keys_returns_False_when_the_write_is_held():
    """The signal that did not exist. Without it every layer above was guessing."""
    proc = _Proc()
    proc.buffer.write(REAL_PLAN_PICKER.encode())

    delivered = await proc.send_keys("approve the plan", automated=True)

    assert delivered is False
    assert proc.writes == []


@pytest.mark.asyncio
async def test_send_keys_returns_True_when_it_actually_writes():
    """POSITIVE CONTROL. A function that returned False unconditionally would pass the
    test above while reporting every send as held — and would look identical."""
    proc = _Proc()
    proc.buffer.write(b"ordinary output, no prompt\n")

    delivered = await proc.send_keys("do the thing", automated=True)

    assert delivered is True
    assert proc.writes


@pytest.mark.asyncio
async def test_an_operator_write_reports_delivered_even_with_a_prompt_open():
    """The operator is the human the prompt waits for, so their write is never held —
    and must not be reported as held either."""
    proc = _Proc()
    proc.buffer.write(REAL_PLAN_PICKER.encode())

    assert await proc.send_keys("1", automated=False) is True


# ---------------------------------------------------------------------------
# The handler refuses instead of claiming delivery
# ---------------------------------------------------------------------------


def _daemon_with(screen: str) -> MagicMock:
    d = MagicMock()
    worker = MagicMock()
    worker.name = "platform-api"
    worker.process.get_content.return_value = screen
    d.workers = [worker]
    return d


def test_the_handler_refuses_when_a_prompt_would_hold_the_message():
    """THE REPORTED DEFECT. It said "Prompt sent"; the message was deferred."""
    from swarm.mcp.queen_handlers._workers import _refuse_if_prompt_would_hold

    d = _daemon_with(REAL_PLAN_PICKER)

    refusal = _refuse_if_prompt_would_hold(d.workers[0], "platform-api")

    assert refusal is not None
    text = refusal[0]["text"]
    assert "NOT SENT" in text
    assert "Nothing was queued" in text


def test_the_refusal_names_the_tools_that_DO_work():
    """A refusal that only says no leaves the Queen exactly where she was — stalled and
    out of options. It has to name the way through."""
    from swarm.mcp.queen_handlers._workers import _refuse_if_prompt_would_hold

    text = _refuse_if_prompt_would_hold(_daemon_with(REAL_PLAN_PICKER).workers[0], "x")[0]["text"]

    assert "queen_view_worker_state" in text
    assert "queen_answer_prompt" in text
    assert "queen_dismiss_prompt" in text


def test_the_refusal_records_that_interrupt_does_NOT_close_a_picker():
    """MEASURED, and it disconfirmed my own code reading. `send_interrupt` calls
    `_signal(SIGINT)` — a signal to the process group, never a PTY write, so it is not
    deferred: it FIRES and the picker survives it. A picker is an input WAIT, not a
    running turn, so the signal has nothing to cancel.

    This sentence is in the refusal text because the Queen tried interrupt first, was
    told "Interrupt sent", and believed it had worked. The next caller should not have
    to rediscover that.
    """
    from swarm.mcp.queen_handlers._workers import _refuse_if_prompt_would_hold

    text = _refuse_if_prompt_would_hold(_daemon_with(REAL_PLAN_PICKER).workers[0], "x")[0]["text"]

    assert "does NOT close a picker" in text
    assert "SIGINT" in text


def test_an_ordinary_worker_is_not_refused():
    """POSITIVE CONTROL. A check that refused everything would make queen_prompt_worker
    useless while passing every test above."""
    from swarm.mcp.queen_handlers._workers import _refuse_if_prompt_would_hold

    assert _refuse_if_prompt_would_hold(_daemon_with("just working\n").workers[0], "x") is None


def test_an_unreadable_pty_reports_the_send_rather_than_inventing_a_hold():
    """Fail-open direction. A read failure is not evidence a prompt is open, and
    blocking the Queen's only channel on one would be the worse error."""
    from swarm.mcp.queen_handlers._workers import _refuse_if_prompt_would_hold

    d = _daemon_with("")
    d.workers[0].process.get_content.side_effect = RuntimeError("pty gone")

    assert _refuse_if_prompt_would_hold(d.workers[0], "x") is None


@pytest.mark.asyncio
async def test_send_to_worker_propagates_the_held_flag():
    """The middle of the chain. If this swallowed the bool, the handler's refusal would
    be the only guard and any other caller would still be guessing."""
    from swarm.server.worker_service import WorkerService

    svc = WorkerService.__new__(WorkerService)
    worker = MagicMock()
    worker.name = "platform-api"
    worker.process.send_keys = AsyncMock(return_value=False)
    svc._get_workers = lambda: [worker]  # type: ignore[method-assign]
    svc._get_pilot = lambda: None  # type: ignore[method-assign]
    svc._drone_log = MagicMock()
    svc._pty_locks = {}
    svc._record_override = MagicMock()  # type: ignore[method-assign]

    delivered = await svc.send_to_worker("platform-api", "hello")

    assert delivered is False
    logged = [c.args[2] for c in svc._drone_log.add.call_args_list if len(c.args) >= 3]
    assert any("HELD" in str(m) for m in logged), f"the hold was not logged: {logged}"
