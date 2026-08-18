"""#1866 — a drone approval is delivered ONLY to the picker it was decided against.

THE RULING, AS AMENDED. The first version said "discard when it cannot be delivered while
its picker is open" AND "deliver when its picker is open". Those are ONE predicate under
the #1451 guard — `has_open_selection_prompt` fires on a cursored option line with a
sibling, which is exactly the content a drone approval exists to answer — so nothing could
satisfy both, and shipping the discard would have silently ended drone auto-approval of
choice prompts. tests/test_pilot.py::test_continue_on_choice_prompt is the proof: it
presents a real picker, expects CONTINUED, and got zero.

FINGERPRINT-MATCHED DELIVERY resolves it. An approval is an ANSWER TO A SPECIFIC QUESTION,
so the test is whether the question on screen is still the one it was judged against.
Match ⇒ deliver. Anything else ⇒ discard and log. NEVER queue: a queued approval is stale
by definition, and releasing it may answer a DIFFERENT picker, selecting whatever option
is cursored then.

THE IDENTITY IS NOT NEW. `pty/prompt_options.py` has computed a 12-hex fingerprint over a
picker's option labels since #1608, and `WorkerService.check_prompt_answer` already refuses
a stale one ("prompt changed since you read it"). Inventing a second scheme would give two
notions of "which prompt is this", and the case where they disagree is an approval
delivered to the wrong question.

THE CASE THIS MUST COVER: my-rcg took 1,302 bare Enters in 0.4 SECONDS on 08-16 23:27:08 —
deferred approvals all released after their pickers had cleared. Every one must now be
discarded. If any would still deliver, the fingerprint is not being captured at decision
time, and that is the bug rather than the fix.
"""

from __future__ import annotations

import pytest

from swarm.drones.decision_executor import _prompt_fingerprint
from swarm.pty.process import WorkerProcess

pytestmark = pytest.mark.asyncio

PICKER_A = "Allow `npm test` to run?\n\n> 1. Yes\n  2. No\n  3. Always allow"
PICKER_B = "Delete the production database?\n\n> 1. Yes\n  2. No"
APPROVAL = "\r"  # Claude's approval_response(True) — a bare carriage return


class _Buffer:
    def __init__(self, content: str = ""):
        self.content = content

    def get_lines(self, _n):
        return self.content


def _proc(screen: str = "") -> WorkerProcess:
    p = WorkerProcess.__new__(WorkerProcess)
    p.name = "my-rcg"
    p._deferred_keys = []
    p._flushing = False
    p.buffer = _Buffer(screen)
    p.writes = []

    async def _write(data, *, actor="unknown"):
        p.writes.append((data, actor))

    p._write = _write
    return p


def _sent(p) -> list[bytes]:
    return [d for d, _ in p.writes]


# ---------------------------------------------------------------------------
# The identities are real and distinct — everything below rests on this
# ---------------------------------------------------------------------------


async def test_the_two_pickers_have_different_fingerprints():
    """POSITIVE CONTROL FOR THE WHOLE FILE. If both parsed to None, or to the same value,
    every match/mismatch test below would pass while measuring nothing."""
    a, b = _prompt_fingerprint(PICKER_A), _prompt_fingerprint(PICKER_B)

    assert a is not None and b is not None, "the parser did not recognise these as pickers"
    assert a != b
    assert _prompt_fingerprint("just some transcript output") is None


# ---------------------------------------------------------------------------
# AC4a — MATCHED: delivered
# ---------------------------------------------------------------------------


async def test_an_approval_is_DELIVERED_to_the_picker_it_was_decided_against():
    """The direction the original ruling could not satisfy. The screen still shows the
    question the drone judged, so the approval lands — bypassing the #1451 defer, which a
    fingerprint match is precisely what rules out the danger of."""
    p = _proc(PICKER_A)

    ok = await p.send_keys(
        APPROVAL,
        enter=False,
        automated=True,
        expect_prompt_fingerprint=_prompt_fingerprint(PICKER_A),
    )

    assert ok is True
    assert _sent(p) == [b"\r"]
    assert p._deferred_keys == []


# ---------------------------------------------------------------------------
# AC1 + AC4b — MISMATCHED: discarded, logged, never appears later
# ---------------------------------------------------------------------------


async def test_an_approval_for_a_DIFFERENT_picker_is_discarded():
    """THE DANGER, DIRECTLY. The drone judged picker A; the screen now shows picker B.
    Delivering would answer B — selecting whatever is cursored, here 'Yes' to deleting a
    production database."""
    p = _proc(PICKER_B)

    ok = await p.send_keys(
        APPROVAL,
        enter=False,
        automated=True,
        expect_prompt_fingerprint=_prompt_fingerprint(PICKER_A),
    )

    assert ok is False
    assert _sent(p) == [], "the approval answered a picker it was never meant for"
    assert p._deferred_keys == []


async def test_MY_RCG_the_picker_is_gone_so_the_approval_is_discarded():
    """THE 1,302. Each was decided against a picker that had cleared by the time it was
    released. No open prompt is a MISMATCH, not a free pass — delivering a bare "\\r" into
    an empty input line submits it, which is what produced the burst."""
    p = _proc("")  # picker long gone

    ok = await p.send_keys(
        APPROVAL,
        enter=False,
        automated=True,
        expect_prompt_fingerprint=_prompt_fingerprint(PICKER_A),
    )

    assert ok is False
    assert _sent(p) == []
    assert p._deferred_keys == [], "a stale approval was queued to fire at a later picker"


async def test_the_discarded_approval_DOES_NOT_APPEAR_LATER():
    """Not queued means not queued: clearing the screen and driving another write must
    release nothing."""
    p = _proc(PICKER_B)
    await p.send_keys(
        APPROVAL,
        enter=False,
        automated=True,
        expect_prompt_fingerprint=_prompt_fingerprint(PICKER_A),
    )

    p.buffer.content = ""
    await p.send_keys("later unrelated message", enter=True, automated=True)

    assert _sent(p) == [b"later unrelated message", b"\r"]
    assert not any(a == "deferred-flush" for _, a in p.writes)


async def test_the_discard_is_logged_with_both_fingerprints(caplog):
    """AC2. A dropped approval means a picker went unanswered and something upstream is
    waiting — silent discard trades one invisible failure for another. Both identities are
    named so the reader can tell WHICH question went unanswered."""
    import logging

    p = _proc(PICKER_B)
    with caplog.at_level(logging.WARNING):
        await p.send_keys(
            APPROVAL,
            enter=False,
            automated=True,
            expect_prompt_fingerprint=_prompt_fingerprint(PICKER_A),
        )

    assert "DISCARDED" in caplog.text
    assert "my-rcg" in caplog.text
    assert _prompt_fingerprint(PICKER_A) in caplog.text
    assert _prompt_fingerprint(PICKER_B) in caplog.text


# ---------------------------------------------------------------------------
# The fingerprint must come from DECISION time, or it checks nothing
# ---------------------------------------------------------------------------


async def test_a_write_time_fingerprint_would_match_unconditionally():
    """THE RULING'S OWN WARNING, PINNED: "if your implementation would still deliver any of
    them, the fingerprint is not being captured at decision time and that is the bug."

    Comparing the screen against a fingerprint read from that same screen always matches —
    including for all 1,302. This test fails the moment someone "simplifies" the callers by
    reading the fingerprint at the point of the write."""
    p = _proc(PICKER_B)

    write_time = p.current_prompt_fingerprint()  # what a naive implementation would use
    assert (
        await p.send_keys(
            APPROVAL, enter=False, automated=True, expect_prompt_fingerprint=write_time
        )
        is True
    ), "sanity: a self-comparison always matches"

    decision_time = _prompt_fingerprint(PICKER_A)  # what the drone actually judged
    p2 = _proc(PICKER_B)
    assert (
        await p2.send_keys(
            APPROVAL, enter=False, automated=True, expect_prompt_fingerprint=decision_time
        )
        is False
    )


async def test_the_callers_read_the_fingerprint_before_the_send_not_inside_it():
    """Source-level, because the behavioural test above cannot see which VALUE a caller
    passes — only what happens once it is passed."""
    import inspect

    from swarm.drones import decision_executor, directives

    for mod in (decision_executor, directives):
        src = inspect.getsource(mod)
        assert "expect_prompt_fingerprint=decided_against" in src, (
            f"{mod.__name__} no longer passes a decision-time fingerprint"
        )
        assert "expect_prompt_fingerprint=self.current_prompt_fingerprint()" not in src
        assert "expect_prompt_fingerprint=target_proc.current_prompt_fingerprint()" not in src


# ---------------------------------------------------------------------------
# AC5 — the ORIGINAL behaviour, shown failing
# ---------------------------------------------------------------------------


async def test_the_original_behaviour_FAILS_this_same_test():
    """No fingerprint is the original path: the #1451 guard QUEUES the approval and the
    next write releases it, after the picker has cleared. That is the my-rcg incident in
    four lines, and it is what the fingerprint now prevents."""
    p = _proc(PICKER_A)

    await p.send_keys(APPROVAL, enter=False, automated=True)  # ORIGINAL — no fingerprint

    assert p._deferred_keys == [(APPROVAL, False)], "the original did not queue it"

    p.buffer.content = ""  # picker clears
    await p.send_keys("later unrelated message", enter=True, automated=True)

    assert _sent(p)[0] == b"\r", (
        "expected the ORIGINAL behaviour to release the stale approval first — if this "
        "fails, this test no longer demonstrates the bug it exists to demonstrate"
    )
    assert p.writes[0][1] == "deferred-flush"


# ---------------------------------------------------------------------------
# AC3 — the #1451 guard is UNCHANGED for every other automated write
# ---------------------------------------------------------------------------


async def test_an_ordinary_automated_write_still_defers():
    """THE NON-WEAKENING, PINNED. Queen prompts and drone nudges are still HELD while a
    picker is open — that is what #1451 exists for and none of it changed. Only writes
    that carry a fingerprint take the new path."""
    p = _proc(PICKER_A)

    ok = await p.send_keys("the operator ruled: ship it", enter=True, automated=True)

    assert ok is False
    assert p._deferred_keys == [("the operator ruled: ship it", True)]
    assert _sent(p) == []


async def test_a_held_ordinary_write_is_still_released_when_the_prompt_clears():
    p = _proc(PICKER_A)
    await p.send_keys("held message", enter=True, automated=True)

    p.buffer.content = ""
    await p.send_keys("next", enter=True, automated=True)

    assert _sent(p) == [b"held message", b"\r", b"next", b"\r"]


async def test_an_operator_write_is_never_touched_by_either_path():
    """automated=False is the operator's own keystrokes — the human the picker is waiting
    for. Neither the defer nor the fingerprint check may apply."""
    p = _proc(PICKER_A)

    ok = await p.send_keys("y", enter=False, expect_prompt_fingerprint="deadbeef")

    assert ok is True
    assert _sent(p) == [b"y"]
    assert p._deferred_keys == []


async def test_an_unreadable_buffer_discards_rather_than_guesses():
    """An approval must never be delivered on an unreadable screen — that is the one case
    where "which picker is open" is unknowable, and delivering would be a guess."""

    class _Boom:
        def get_lines(self, _n):
            raise RuntimeError("buffer gone")

    p = _proc()
    p.buffer = _Boom()

    assert (
        await p.send_keys(
            APPROVAL, enter=False, automated=True, expect_prompt_fingerprint="abc123abc123"
        )
        is False
    )
    assert _sent(p) == []


# ---------------------------------------------------------------------------
# AC2 + the THIRD instance of #1843's defect
# ---------------------------------------------------------------------------


async def test_safe_worker_action_reads_the_result_instead_of_discarding_it():
    """THE THIRD INSTANCE of "the confirmation reports the send, not the delivery"
    (#1843, #1832, now this). `_safe_worker_action` awaited send_keys and THREW THE RESULT
    AWAY — so the False that #1608 added specifically so a caller could tell a held write
    from a delivered one was invisible, and a write that never reached the worker was
    logged as CONTINUED."""
    from unittest.mock import MagicMock

    from swarm.drones.decision_executor import DecisionExecutor
    from swarm.drones.log import DroneAction

    ex = DecisionExecutor.__new__(DecisionExecutor)
    ex.log = MagicMock()
    ex._had_substantive_action = False
    worker = MagicMock()
    worker.name = "my-rcg"

    async def _refused():
        return False

    ok = await ex._safe_worker_action(
        worker,
        _refused(),
        DroneAction.CONTINUED,
        undelivered_action=DroneAction.APPROVAL_DISCARDED,
        undelivered_reason="picker no longer on screen (#1866)",
    )

    assert ok is False
    action, name, detail = ex.log.add.call_args.args
    assert action is DroneAction.APPROVAL_DISCARDED
    assert name == "my-rcg"
    assert "picker no longer on screen" in detail
    assert ex._had_substantive_action is False, "a discarded approval reset adaptive backoff"


async def test_a_delivered_approval_still_logs_the_success_action():
    """POSITIVE CONTROL: if every result logged APPROVAL_DISCARDED, the test above would
    pass and the drone log would never record a real continue again."""
    from unittest.mock import MagicMock

    from swarm.drones.decision_executor import DecisionExecutor
    from swarm.drones.log import DroneAction

    ex = DecisionExecutor.__new__(DecisionExecutor)
    ex.log = MagicMock()
    ex._had_substantive_action = False
    worker = MagicMock()
    worker.name = "my-rcg"

    async def _ok():
        return True

    assert (
        await ex._safe_worker_action(
            worker, _ok(), DroneAction.CONTINUED, undelivered_action=DroneAction.APPROVAL_DISCARDED
        )
        is True
    )
    assert ex.log.add.call_args.args[0] is DroneAction.CONTINUED
    assert ex._had_substantive_action is True


async def test_no_auto_submit_is_introduced():
    """Enter is still sent only when the caller asked for it — never synthesised."""
    import inspect

    code = inspect.getsource(WorkerProcess.send_keys)

    assert "if enter:" in code
    assert code.count("if enter:") >= 2, (
        "the matched-delivery branch must honour the caller's `enter`, not force one"
    )
