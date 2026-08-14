"""#1633 — the two verbs that reported success for actions that did not happen.

MEASURED COST, which is why this file exists: nexus 8.16h and platform-api 6.64h
stalled on selection prompts — 14.8 worker-hours — because the Queen read a success
string as an outcome. Reconstructed from buzz_log STATE_TRANSITION pairs; the figures
first reported were roughly half that.

Two remaining defects after #1608's pass:

1. ``queen_prompt_worker`` checks for an open prompt SYNCHRONOUSLY, then dispatches the
   send via ``_fire_async``. A prompt opening in that gap holds the message while the
   caller has already been told "Prompt sent to X".

2. ``queen_interrupt_worker`` dispatched SIGINT at a worker on a picker and appended a
   note saying it would not work. Performing a MEASURED-useless action and describing it
   afterwards is not honesty — refusing is.

The controls matter as much as the assertions. Interrupt is the right tool for a
genuinely stuck BUZZING worker; a refusal that fires too broadly would take away the
only way to rescue one.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.mcp.queen_handlers._workers import HANDLERS
from swarm.worker.worker import WorkerState
from tests.test_prompt_hold_is_reported_1608 import REAL_PLAN_PICKER


def _text(result: list[dict[str, Any]]) -> str:
    return "\n".join(part.get("text", "") for part in result)


def _worker(name: str, *, prompt_open: bool) -> MagicMock:
    w = MagicMock()
    w.name = name
    w.is_queen = False
    w.state = WorkerState.BUZZING
    w.state_duration = 900.0
    w.current_task = None
    # REAL captured picker text, not a synthetic marker: the guard runs the production
    # `has_open_selection_prompt` over whatever this returns, so a hand-written stand-in
    # would be testing my idea of a prompt rather than a prompt.
    w.process = MagicMock()
    w.process.get_content.return_value = REAL_PLAN_PICKER if prompt_open else "just working\n"
    return w


def _daemon(worker: MagicMock) -> MagicMock:
    d = MagicMock()
    d.workers = [worker]
    d.drone_log = MagicMock()
    d.worker_svc = MagicMock()
    d.worker_svc.interrupt_worker = AsyncMock(return_value=True)
    d.worker_svc.send_to_worker = AsyncMock(return_value=True)
    return d


@pytest.fixture(autouse=True)
def _no_background_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swallow the coroutines ``_fire_async`` would schedule.

    Without this the AsyncMock coroutines are created and never awaited, which is a
    warning — and warnings are failures here.
    """

    def _fake_fire(coro: Any, **_kw: Any) -> None:
        # RUN it rather than closing it. Closing would make the delivery control vacuous:
        # "no send happened" would pass whether the handler dispatched or not.
        asyncio.run(coro)

    monkeypatch.setattr("swarm.mcp.queen_handlers._workers._fire_async", _fake_fire)


# --------------------------------------------------------------------------------------
# AC2 — interrupt REFUSES on an open picker, and sends nothing
# --------------------------------------------------------------------------------------


def test_interrupt_refuses_when_target_is_on_a_picker() -> None:
    worker = _worker("nexus", prompt_open=True)
    d = _daemon(worker)

    result = HANDLERS["queen_interrupt_worker"](
        d, "queen", {"worker": "nexus", "reason": "stuck 8h"}
    )
    text = _text(result)

    assert "NOT SENT" in text
    d.worker_svc.interrupt_worker.assert_not_called()


def test_interrupt_refusal_names_the_tools_that_do_work() -> None:
    """A refusal that does not say what to do instead just moves the stall."""
    d = _daemon(_worker("nexus", prompt_open=True))

    text = _text(HANDLERS["queen_interrupt_worker"](d, "queen", {"worker": "nexus", "reason": "x"}))

    assert "queen_dismiss_prompt" in text
    assert "queen_answer_prompt" in text


def test_interrupt_refusal_does_not_write_a_buzz_log_entry() -> None:
    """The log must not record an interrupt that never happened.

    #1608 placed the picker check after the OPERATOR entry, so a forensic reader would
    have found evidence of a signal that was never dispatched.
    """
    d = _daemon(_worker("nexus", prompt_open=True))

    HANDLERS["queen_interrupt_worker"](d, "queen", {"worker": "nexus", "reason": "x"})

    d.drone_log.add.assert_not_called()


# --------------------------------------------------------------------------------------
# AC3 — POSITIVE CONTROL: interrupt still works on a worker with no prompt open
# --------------------------------------------------------------------------------------


def test_interrupt_still_dispatches_when_no_prompt_is_open() -> None:
    """The half that decides whether this change is safe to keep.

    Interrupt is the only way to rescue a genuinely stuck BUZZING worker. If the refusal
    fired on every target the guard would be a regression dressed as a fix.
    """
    d = _daemon(_worker("hub", prompt_open=False))

    text = _text(HANDLERS["queen_interrupt_worker"](d, "queen", {"worker": "hub", "reason": "x"}))

    assert "NOT SENT" not in text
    assert "SIGINT dispatched to hub" in text
    d.drone_log.add.assert_called_once()


def test_interrupt_success_path_still_says_it_cannot_confirm() -> None:
    """#1608's wording must survive #1633 — dispatch is not outcome."""
    d = _daemon(_worker("hub", prompt_open=False))

    text = _text(HANDLERS["queen_interrupt_worker"](d, "queen", {"worker": "hub", "reason": "x"}))

    assert "NOT CONFIRMED" in text
    assert "queen_view_worker_state" in text


# --------------------------------------------------------------------------------------
# AC1 — prompt_worker no longer claims a send it cannot see
# --------------------------------------------------------------------------------------


def test_prompt_worker_does_not_claim_the_prompt_was_sent() -> None:
    """THE ACCEPT-THEN-HOLD RACE.

    Nothing is open at check time, so the handler proceeds — correctly. What it must not
    do is report arrival, because the send happens later and the #1451 guard can hold it
    in between.
    """
    d = _daemon(_worker("platform-api", prompt_open=False))

    text = _text(
        HANDLERS["queen_prompt_worker"](
            d, "queen", {"worker": "platform-api", "prompt": "status?", "reason": "check"}
        )
    )

    assert "Prompt sent" not in text
    assert "DISPATCHED to platform-api" in text


def test_prompt_worker_says_how_to_confirm_and_what_a_hold_looks_like() -> None:
    d = _daemon(_worker("platform-api", prompt_open=False))

    text = _text(
        HANDLERS["queen_prompt_worker"](
            d, "queen", {"worker": "platform-api", "prompt": "status?", "reason": "check"}
        )
    )

    assert "queen_view_worker_state" in text
    assert "HELD" in text


def test_prompt_worker_still_delivers_when_nothing_is_holding() -> None:
    """POSITIVE CONTROL: honest wording must not mean a suppressed send."""
    d = _daemon(_worker("platform-api", prompt_open=False))
    sent: list[tuple[str, str]] = []

    async def _record(target: str, text: str, **_kw: Any) -> bool:
        sent.append((target, text))
        return True

    d.worker_svc.send_to_worker = _record

    HANDLERS["queen_prompt_worker"](
        d, "queen", {"worker": "platform-api", "prompt": "status?", "reason": "check"}
    )

    assert sent == [("platform-api", "status?")]


def test_prompt_worker_refusal_on_an_open_prompt_is_unchanged() -> None:
    """The Queen ruled this path CORRECT. Pin it so #1633 does not disturb it."""
    d = _daemon(_worker("platform-api", prompt_open=True))

    text = _text(
        HANDLERS["queen_prompt_worker"](
            d, "queen", {"worker": "platform-api", "prompt": "status?", "reason": "check"}
        )
    )

    assert "DISPATCHED" not in text
    assert "queen_answer_prompt" in text
