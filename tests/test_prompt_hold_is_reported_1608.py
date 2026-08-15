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

    async def _write(  # type: ignore[override]
        self, data: bytes, *, actor: str = "unknown"
    ) -> None:
        # #1658 added `actor` at the choke point; doubles must accept it or every
        # write path through them raises TypeError instead of exercising the guard.
        _ = actor
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


# ---------------------------------------------------------------------------
# The fingerprint must be REACHABLE, or the answer tool cannot be called
# ---------------------------------------------------------------------------


def test_the_view_surfaces_a_fingerprint_the_answer_tool_can_use():
    """WITHOUT THIS THE FEATURE IS UNUSABLE. `queen_answer_prompt` requires a
    fingerprint and nothing else produces one — the Queen would have to hash normalised
    option labels by hand. A tool that ships, passes its tests and cannot be called is
    the exact shape #1608 exists to fix, so this closes the loop rather than leaving it
    as a note in a resolution."""
    from swarm.mcp.queen_handlers._views import _open_prompt_payload
    from swarm.pty.prompt_options import parse_open_prompt

    payload = _open_prompt_payload(REAL_PLAN_PICKER)

    assert payload is not None
    assert payload["fingerprint"] == parse_open_prompt(REAL_PLAN_PICKER).fingerprint
    assert [o["number"] for o in payload["options"]] == [1, 2, 3]
    assert payload["options"][0]["cursored"] is True


def test_the_view_returns_no_prompt_block_for_an_ordinary_worker():
    """POSITIVE CONTROL. A payload that always appeared would have the Queen answering
    prompts that are not there."""
    from swarm.mcp.queen_handlers._views import _open_prompt_payload

    assert _open_prompt_payload("just working\nno menu here\n") is None


def test_a_parse_failure_degrades_to_no_prompt_rather_than_raising():
    """This enriches a tool the Queen uses for everything else; a parse bug must not
    take out her view of the fleet."""
    from swarm.mcp.queen_handlers._views import _open_prompt_payload

    assert _open_prompt_payload("") is None


def test_the_round_trip_holds_view_fingerprint_answers_the_prompt():
    """END TO END ACROSS THE TWO TOOLS, which is the property that matters: the
    fingerprint the VIEW reports must be the one the ANSWER path accepts. Pinned
    because they are separate modules and could drift into disagreeing — at which
    point every answer would be refused and the refusal would look correct."""
    from unittest.mock import MagicMock

    from swarm.mcp.queen_handlers._views import _open_prompt_payload
    from swarm.server.worker_service import WorkerService

    fingerprint = _open_prompt_payload(REAL_PLAN_PICKER)["fingerprint"]

    svc = WorkerService.__new__(WorkerService)
    worker = MagicMock()
    worker.name = "platform-api"
    worker.process.get_content.return_value = REAL_PLAN_PICKER
    svc._get_workers = lambda: [worker]  # type: ignore[method-assign]
    svc._drone_log = MagicMock()
    svc._get_pilot = lambda: None  # type: ignore[method-assign]

    ok, message = svc.check_prompt_answer("platform-api", 1, fingerprint)

    assert ok is True
    assert "Yes, and use auto mode" in message


# ---------------------------------------------------------------------------
# The answer path must READ BACK, not report success from having written
# ---------------------------------------------------------------------------
#
# FIRST LIVE USE, 2026-08-14: queen_answer_prompt returned "answering option 1
# (Yes, and use auto mode)" and 16 seconds later the picker was IDENTICAL — same
# fingerprint, same cursor, worker still SLEEPING. The tool reported success on the
# strength of having written to the PTY. That is precisely the defect #1608 was filed
# about — queen_prompt_worker saying "sent" for a held message — reproduced inside the
# fix for it.


@pytest.mark.asyncio
async def test_the_answer_reports_UNCONFIRMED_when_the_prompt_survives():
    """The reported case. A caller cannot distinguish "wrote the key" from "the prompt
    is gone", so the tool must not conflate them."""
    from swarm.server.worker_service import WorkerService

    svc = WorkerService.__new__(WorkerService)
    worker = MagicMock()
    worker.name = "platform-api"
    worker.process.get_content.return_value = REAL_PLAN_PICKER  # never clears
    worker.process.send_keys = AsyncMock(return_value=True)
    worker.process.send_enter = AsyncMock()
    worker.process.send_arrow_down = AsyncMock()
    worker.process.send_arrow_up = AsyncMock()
    svc._get_workers = lambda: [worker]  # type: ignore[method-assign]
    svc._get_pilot = lambda: None  # type: ignore[method-assign]
    svc._drone_log = MagicMock()

    from swarm.mcp.queen_handlers._views import _open_prompt_payload
    from swarm.server import worker_service as ws

    ws._ANSWER_SETTLE_SECONDS = 0.0  # do not make the suite wait
    fp = _open_prompt_payload(REAL_PLAN_PICKER)["fingerprint"]

    outcome = await svc.answer_open_prompt("platform-api", 1, fp)

    assert "NOT CONFIRMED" in outcome
    assert worker.process.send_enter.await_count == 1, "it must still have tried"
    # THE KEY ASSERTION. Cursor is already on option 1, so no arrows and — crucially —
    # NO DIGIT. Typing "1" into a picker that does not consume number keys makes it FREE
    # TEXT, which is the exact harm #1451 exists to prevent.
    worker.process.send_keys.assert_not_awaited()
    assert worker.process.send_arrow_down.await_count == 0
    logged = [c.args[2] for c in svc._drone_log.add.call_args_list if len(c.args) >= 3]
    assert any("SENT BUT NOT CONFIRMED" in str(m) for m in logged)


@pytest.mark.asyncio
async def test_the_answer_reports_CONFIRMED_when_the_prompt_clears():
    """POSITIVE CONTROL, and it is what stops the fix from being 'always say unconfirmed'
    — which would pass the test above while making the tool useless."""
    from swarm.server import worker_service as ws
    from swarm.server.worker_service import WorkerService

    ws._ANSWER_SETTLE_SECONDS = 0.0
    from swarm.mcp.queen_handlers._views import _open_prompt_payload

    fp = _open_prompt_payload(REAL_PLAN_PICKER)["fingerprint"]

    svc = WorkerService.__new__(WorkerService)
    worker = MagicMock()
    worker.name = "platform-api"
    # Picker on the first read (validation), gone on the read-back.
    # re-validate, then the cursor read, then the read-back on a cleared screen.
    worker.process.get_content.side_effect = [REAL_PLAN_PICKER, REAL_PLAN_PICKER, "done\n"]
    worker.process.send_keys = AsyncMock(return_value=True)
    worker.process.send_enter = AsyncMock()
    worker.process.send_arrow_down = AsyncMock()
    worker.process.send_arrow_up = AsyncMock()
    svc._get_workers = lambda: [worker]  # type: ignore[method-assign]
    svc._get_pilot = lambda: None  # type: ignore[method-assign]
    svc._drone_log = MagicMock()

    outcome = await svc.answer_open_prompt("platform-api", 1, fp)

    assert "confirmed" in outcome
    assert "NOT CONFIRMED" not in outcome


def test_the_handler_never_claims_the_prompt_was_answered():
    """`_fire_async` returns before the keystroke is written, so the handler cannot know.
    Pinned as a WORDING contract because the wording is the defect: 'answering' read as
    success to the caller who then reported it publicly."""
    import inspect

    from swarm.mcp.queen_handlers import _workers

    src = inspect.getsource(_workers._handle_answer_prompt)

    assert "NOT YET CONFIRMED" in src
    assert "SENT option" in src


@pytest.mark.asyncio
async def test_answering_a_non_cursored_option_moves_with_arrows():
    """Cursor on 1, target 3 → two Down presses, then Enter, and STILL no digit.

    This is how a human answers the prompt, and it is the only mechanism whose worst
    case is a moved cursor rather than a stray character submitted as a message.
    """
    from swarm.mcp.queen_handlers._views import _open_prompt_payload
    from swarm.server import worker_service as ws
    from swarm.server.worker_service import WorkerService

    ws._ANSWER_SETTLE_SECONDS = 0.0
    ws._ARROW_STEP_SECONDS = 0.0
    fp = _open_prompt_payload(REAL_PLAN_PICKER)["fingerprint"]

    svc = WorkerService.__new__(WorkerService)
    worker = MagicMock()
    worker.name = "platform-api"
    worker.process.get_content.return_value = REAL_PLAN_PICKER
    worker.process.send_keys = AsyncMock(return_value=True)
    worker.process.send_enter = AsyncMock()
    worker.process.send_arrow_down = AsyncMock()
    worker.process.send_arrow_up = AsyncMock()
    svc._get_workers = lambda: [worker]
    svc._get_pilot = lambda: None
    svc._drone_log = MagicMock()

    await svc.answer_open_prompt("platform-api", 3, fp)

    assert worker.process.send_arrow_down.await_count == 2
    assert worker.process.send_arrow_up.await_count == 0
    assert worker.process.send_enter.await_count == 1
    worker.process.send_keys.assert_not_awaited()


# ---------------------------------------------------------------------------
# queen_interrupt_worker: say what is known, not what was dispatched
# ---------------------------------------------------------------------------
#
# "Interrupt sent to platform-api" was TRUE AND USELESS. The Queen read it as
# "the interrupt worked", reported that to the operator, and the picker it was aimed at
# stayed open for ten more minutes. Together with queen_prompt_worker's "Prompt sent"
# for a held message, those two responses cost the fleet ~15 worker-hours — not because
# they lied, but because they described a DISPATCH and were read as an OUTCOME.


def _interrupt(screen: str) -> str:
    from swarm.mcp.queen_handlers._workers import _handle_interrupt_worker

    d = _daemon_with(screen)
    d.worker_svc.interrupt_worker = MagicMock()
    result = _handle_interrupt_worker(d, "queen", {"worker": "platform-api", "reason": "probe"})
    return result[0]["text"]


def test_interrupt_does_not_claim_it_cancelled_anything():
    """It dispatches an OS signal; whether anything stopped is a different fact."""
    text = _interrupt("just working\n")

    assert "NOT CONFIRMED" in text
    assert "queen_view_worker_state" in text, "it must name how to check"


def test_interrupt_refuses_when_the_target_is_on_a_picker():
    """THE CASE THAT COST THE HOURS. SIGINT cannot close a picker — it is an input WAIT,
    not a running turn, so there is nothing to cancel.

    #1608 shipped a WARNING here. #1633 upgraded it to a REFUSAL: a note appended to a
    completed send reads as advisory, and dispatching a signal already measured to do
    nothing is not made honest by describing it afterwards. Full coverage of the refusal,
    including that it writes no buzz-log entry, is in test_verb_honesty_1633.py.
    """
    text = _interrupt(REAL_PLAN_PICKER)

    assert "NOT SENT" in text
    assert "queen_dismiss_prompt" in text
    assert "queen_answer_prompt" in text


def test_the_picker_refusal_is_absent_for_an_ordinary_worker():
    """POSITIVE CONTROL. A refusal that fired on every target would remove the only way
    to rescue a genuinely stuck BUZZING worker."""
    assert "NOT SENT" not in _interrupt("ordinary output\n")


# ---------------------------------------------------------------------------
# #1623 — dismiss was the last verb reporting a dispatch as an outcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dismiss_reports_CONFIRMED_when_the_picker_closes():
    from swarm.server import worker_service as ws
    from swarm.server.worker_service import WorkerService

    ws._ANSWER_SETTLE_SECONDS = 0.0
    svc = WorkerService.__new__(WorkerService)
    worker = MagicMock()
    worker.name = "nexus"
    worker.process.get_content.side_effect = [REAL_PLAN_PICKER, "the prompt closed\n"]
    worker.process.send_escape = AsyncMock()
    svc._get_workers = lambda: [worker]
    svc._get_pilot = lambda: None
    svc._drone_log = MagicMock()

    outcome = await svc.dismiss_open_prompt("nexus")

    assert "confirmed" in outcome
    worker.process.send_escape.assert_awaited_once()


@pytest.mark.asyncio
async def test_dismiss_reports_UNCONFIRMED_when_the_picker_survives():
    """THE OUTCOME THIS TICKET EXPECTS TO BE POSSIBLE. `queen_interrupt_worker` looked
    equally sound on the same style of reasoning and does not close a picker at all, so
    Escape must be able to report failure rather than assume success."""
    from swarm.server import worker_service as ws
    from swarm.server.worker_service import WorkerService

    ws._ANSWER_SETTLE_SECONDS = 0.0
    svc = WorkerService.__new__(WorkerService)
    worker = MagicMock()
    worker.name = "nexus"
    worker.process.get_content.return_value = REAL_PLAN_PICKER  # never closes
    worker.process.send_escape = AsyncMock()
    svc._get_workers = lambda: [worker]
    svc._get_pilot = lambda: None
    svc._drone_log = MagicMock()

    outcome = await svc.dismiss_open_prompt("nexus")

    assert "NOT CONFIRMED" in outcome
    assert "queen_answer_prompt" in outcome, "it must name the proven route"


@pytest.mark.asyncio
async def test_dismiss_refuses_when_no_prompt_is_open():
    """Escape into an ordinary session cancels whatever the worker was typing. Sending
    it blindly would make a no-op call destructive."""
    from swarm.server.worker_service import WorkerService

    svc = WorkerService.__new__(WorkerService)
    worker = MagicMock()
    worker.name = "nexus"
    worker.process.get_content.return_value = "just working\n"
    worker.process.send_escape = AsyncMock()
    svc._get_workers = lambda: [worker]
    svc._get_pilot = lambda: None
    svc._drone_log = MagicMock()

    outcome = await svc.dismiss_open_prompt("nexus")

    assert "no selection prompt is open" in outcome
    worker.process.send_escape.assert_not_awaited()


def test_the_dismiss_description_records_the_measurement_not_a_code_reading():
    """#1623 CLOSED BY MEASUREMENT 2026-08-15. The description went through three
    states: it once asserted 'Escape only closes the prompt' as fact (a confident code
    reading), then honestly said the behaviour was unobserved, and now records what was
    actually watched — the Queen dismissing a manufactured AskUserQuestion picker.

    The claim it must carry is the one nobody could predict from the code: Escape
    DECLINES rather than committing the highlighted option."""
    from swarm.mcp.queen_handlers._workers import TOOLS

    desc = next(t for t in TOOLS if t["name"] == "queen_dismiss_prompt")["description"]

    assert "OBSERVED TO WORK" in desc
    assert "2026-08-15" in desc
    assert "DECLINES" in desc
    assert "queen_answer_prompt" in desc
    # The stale claim must be gone, not merely contradicted further down.
    assert "NOT YET OBSERVED" not in desc


def test_the_dismiss_description_keeps_the_scope_caveat_it_earned():
    """A measurement on ONE prompt type is not a general result, and the failure mode
    this whole ticket exists to prevent is a plausible claim outrunning its evidence.
    AskUserQuestion was measured; a permission confirmation was not."""
    from swarm.mcp.queen_handlers._workers import TOOLS

    desc = next(t for t in TOOLS if t["name"] == "queen_dismiss_prompt")["description"]

    assert "AskUserQuestion" in desc
    assert "NOT been measured" in desc


def test_the_refusal_does_not_promise_a_later_delivery():
    """The wording described a HOLD while performing a REFUSAL — 'would be HELD and
    delivered only once the prompt closes' next to 'Nothing was queued'. A Queen reading
    the first clause waits for a message that is never coming."""
    from swarm.mcp.queen_handlers._workers import _refuse_if_prompt_would_hold

    text = _refuse_if_prompt_would_hold(_daemon_with(REAL_PLAN_PICKER).workers[0], "x")[0]["text"]

    assert "Nothing was queued" in text
    assert "nothing will arrive later" in text
    assert "delivered only once the prompt closes" not in text
