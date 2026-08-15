"""#1648 — a refused prompt must leave the message RECOVERABLE, on both sides.

MEASURED 2026-08-15 during #1623, and it cost a real message. The Queen sent a long
dispatch describing three tickets while a test picker was open on `swarm`. The #1451
guard refused it at the caller — correctly: nothing was queued, nothing arrived late,
nothing flushed into the open question. But the CONTENT simply evaporated. It reached
the recipient on neither channel, and was rescued only because a one-line recap happened
to appear in a later message.

TWO GAPS, one per side:
  · THE SENDER was told "Nothing was queued" and given nothing to resend — recovery
    depended entirely on them still holding the body, which an agent under context
    pressure will not.
  · THE RECIPIENT never learned a message had been attempted at all. Any analysis from
    their side is blind to it.

WHAT MUST NOT CHANGE: the refusal itself. Delivery semantics stay exactly as #1608/#1451
left them — no queueing into `_deferred_keys`, no late arrival, no flush into an open
prompt. This ticket is about recovery, not about going back to deferring.
"""

from __future__ import annotations

from unittest.mock import MagicMock

REAL_PLAN_PICKER = """\
  Would you like to proceed?
❯ 1. Yes, and use auto mode
   2. Yes, manually approve edits
   3. Tell Claude what to change
"""

BODY = "TICKET LIST: _queen_can_approve and _identify_worker are separate tickets."


def _worker_on_a_picker(name: str = "swarm") -> MagicMock:
    w = MagicMock()
    w.name = name
    w.process.get_content.return_value = REAL_PLAN_PICKER
    return w


def test_the_refusal_hands_the_sender_the_body_back():
    """AC1. Without this, recovery depends on the caller still holding the text."""
    from swarm.mcp.queen_handlers._workers import _refuse_if_prompt_would_hold

    text = _refuse_if_prompt_would_hold(_worker_on_a_picker(), "swarm", message=BODY)[0]["text"]

    assert BODY in text
    assert "RESEND" in text.upper()


def test_the_refusal_still_reports_the_refusal_itself():
    """NO REGRESSION on #1608. Echoing the body must not soften the verdict — the caller
    still has to know it was NOT sent and nothing is coming later."""
    from swarm.mcp.queen_handlers._workers import _refuse_if_prompt_would_hold

    text = _refuse_if_prompt_would_hold(_worker_on_a_picker(), "swarm", message=BODY)[0]["text"]

    assert "NOT SENT" in text
    assert "Nothing was queued" in text
    assert "nothing will arrive later" in text


def test_the_refusal_still_works_with_no_message_supplied():
    """`_refuse_if_prompt_would_hold` guards interrupt and dismiss too, and those have no
    message body. The parameter is optional and its absence must not break them."""
    from swarm.mcp.queen_handlers._workers import _refuse_if_prompt_would_hold

    text = _refuse_if_prompt_would_hold(_worker_on_a_picker(), "swarm")[0]["text"]

    assert "NOT SENT" in text
    assert "RESEND" not in text.upper()


def test_the_recipient_gets_an_inbox_note_naming_the_attempt():
    """AC2. The inbox is not subject to the PTY hold, so it is the one channel that can
    reach a worker sitting on a picker. The note carries the body, because a recipient
    who knows only THAT a message was lost is barely better off than one who knows
    nothing."""
    from swarm.mcp.queen_handlers._workers import _note_refused_prompt

    d = MagicMock()
    _note_refused_prompt(d, "swarm", BODY)

    d.message_store.send.assert_called_once()
    kwargs = d.message_store.send.call_args.kwargs
    args = d.message_store.send.call_args.args
    sent = " ".join(str(a) for a in args) + " " + " ".join(f"{k}={v}" for k, v in kwargs.items())
    assert "swarm" in sent
    assert BODY in sent


def test_the_inbox_note_never_raises_into_the_handler():
    """The note is a courtesy on a failure path. If the store is down, the REFUSAL is
    still the thing that must be reported — swallowing here is deliberate, because an
    exception would replace a truthful refusal with a stack trace."""
    from swarm.mcp.queen_handlers._workers import _note_refused_prompt

    d = MagicMock()
    d.message_store.send.side_effect = RuntimeError("store down")

    _note_refused_prompt(d, "swarm", BODY)  # must not raise


def test_a_missing_message_store_is_tolerated():
    """Older daemons and test doubles may not carry one."""
    from swarm.mcp.queen_handlers._workers import _note_refused_prompt

    d = MagicMock()
    d.message_store = None

    _note_refused_prompt(d, "swarm", BODY)  # must not raise
