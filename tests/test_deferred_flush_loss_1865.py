"""#1865 — a mid-flush raise discarded every message still queued behind it.

WORSE THAN #1858, AND INVISIBLE WHERE THAT ONE IS VISIBLE. #1858 fixed a two-call write
that can strand text in the input line; `unsent_input()` now surfaces that. This is the
other half of the same function:

    queued, self._deferred_keys = self._deferred_keys, []   # <- before any await
    for qtext, qenter in queued:
        await self._write(...)                              # <- raises here

The item being written strands its text — detectable. EVERY REMAINING ITEM IS GONE:
nothing in the input line to detect, nothing in the queue to retry, and the sender
already received a success-shaped result from the enqueue. That is message LOSS, and it
is the same family as #1843 — a confirmation that reports the send rather than the
delivery — except the payload no longer exists anywhere.

WHY THIS QUEUE. The #1451 guard DEFERS automated writes while a selection prompt is open,
so `_deferred_keys` is exactly where Queen prompts and drone nudges pile up for a worker
who is mid-decision. The queue that could be silently emptied is the one holding messages
to busy workers.

MEASURED BEFORE CHANGING ANYTHING (pty-writes.jsonl, 24,123 writes, 08-15 → 08-18):
  * deferred-flush text writes: 15, ALL paired with an Enter. Zero mid-flush raises ever.
  * Positive control: the identical join against actor='automated' returns #1858's
    confirmed public-website failure (1 unpaired of 750), so the query is not blind.
  * Queue depth is NOT one: the queen flushed 4 messages in a single second on 08-17
    12:54:33, and my-rcg took a burst of 1,302 bare Enters. A one-item queue would lose
    nothing on failure; these would lose 3 and 1,301.

NO AUTO-SUBMIT. #1858's constraint stands unchanged — nothing here submits anything that
was not already queued by a caller that asked for it.
"""

from __future__ import annotations

import re

import pytest

from swarm.pty.process import WorkerProcess

pytestmark = pytest.mark.asyncio


def _proc(monkeypatch) -> WorkerProcess:
    """A WorkerProcess with the prompt check stubbed out and writes captured."""
    p = WorkerProcess.__new__(WorkerProcess)
    p.name = "budgetbug"
    p._deferred_keys = []
    p._flushing = False
    p.buffer = None
    p.writes = []

    async def _write(data, *, actor="unknown"):
        p.writes.append((data, actor))

    p._write = _write
    # No open prompt: the flush proceeds. Reading the buffer is not what is under test.
    monkeypatch.setattr(
        "swarm.pty.process.has_open_selection_prompt", lambda _c: False, raising=False
    )
    p.buffer = _FakeBuffer()
    return p


class _FakeBuffer:
    def get_lines(self, _n):
        return ""


# ---------------------------------------------------------------------------
# AC4 — THE LOSING CASE, DEMONSTRATED
# ---------------------------------------------------------------------------


async def test_THE_LOSING_CASE_messages_two_and_three_survive_a_mid_flush_raise(monkeypatch):
    """THE ACCEPTANCE CRITERION, LITERALLY. Three messages queued, a raise forced on the
    FIRST one's Enter, and messages two and three must still be there afterwards.

    Before this fix they were gone — swapped out of the object before the first await and
    referenced only by a local that the exception unwound."""
    p = _proc(monkeypatch)
    p._deferred_keys = [("msg-one", True), ("msg-two", True), ("msg-three", True)]

    calls = {"n": 0}
    real_write = p._write

    async def _boom(data, *, actor="unknown"):
        calls["n"] += 1
        if data == b"\r" and calls["n"] == 2:  # the Enter of message ONE
            raise RuntimeError("holder disconnected mid-flush")
        await real_write(data, actor=actor)

    p._write = _boom

    with pytest.raises(RuntimeError):
        await p._flush_deferred_keys()

    assert p._deferred_keys == [("msg-two", True), ("msg-three", True)], (
        f"messages two and three were DISCARDED by a failure that had nothing to do "
        f"with them — queue is {p._deferred_keys!r}"
    )


async def test_the_stranded_message_does_not_come_back_to_be_sent_twice(monkeypatch):
    """The other side of the same decision. Message one's TEXT reached the worker before
    its Enter failed, so replaying it would type it in twice. It leaves the queue; the
    stranding is visible through #1858's unsent_input instead."""
    p = _proc(monkeypatch)
    p._deferred_keys = [("msg-one", True), ("msg-two", True)]
    real_write = p._write
    n = {"i": 0}

    async def _boom(data, *, actor="unknown"):
        n["i"] += 1
        if data == b"\r" and n["i"] == 2:
            raise RuntimeError("boom")
        await real_write(data, actor=actor)

    p._write = _boom
    with pytest.raises(RuntimeError):
        await p._flush_deferred_keys()

    assert ("msg-one", True) not in p._deferred_keys


async def test_a_failure_on_the_TEXT_write_keeps_the_whole_item(monkeypatch):
    """Nothing reached the worker, so the item is intact and stays AT THE FRONT — the
    one case where the message can safely be retried in full."""
    p = _proc(monkeypatch)
    p._deferred_keys = [("msg-one", True), ("msg-two", True), ("msg-three", True)]

    async def _boom(data, *, actor="unknown"):
        raise RuntimeError("holder gone before anything landed")

    p._write = _boom
    with pytest.raises(RuntimeError):
        await p._flush_deferred_keys()

    assert p._deferred_keys == [
        ("msg-one", True),
        ("msg-two", True),
        ("msg-three", True),
    ], "an item that never reached the worker was dropped anyway"


async def test_the_queue_is_never_swapped_out_before_the_await():
    """AC3 as a source fact, not only a behaviour. The behavioural tests above cover the
    failure shapes I thought of; this catches the edit that reintroduces the pattern."""
    import inspect

    body = inspect.getsource(WorkerProcess._flush_deferred_keys)
    code = body.split('"""')[2] if body.count('"""') >= 2 else body

    # THIS ASSERTION WAS WRONG FIRST TIME AND PASSED AGAINST THE BUG. It looked for the
    # literal "self._deferred_keys = []", which the original never contained — it emptied
    # the queue via the TUPLE SWAP `queued, self._deferred_keys = self._deferred_keys, []`.
    # A sweep that cannot match the exact code it exists to ban is decoration; matching
    # the swap idiom is the point.
    # STRIP COMMENTS FIRST. The fix's own comment QUOTES the banned swap in order to
    # explain why it is banned, and the sweep matched that on its first run — the exact
    # trap #1685 named: a guard nobody can write a comment near.
    code = "\n".join(ln for ln in code.splitlines() if not ln.strip().startswith("#"))
    flat = re.sub(r"\s+", " ", code)

    assert not re.search(r"self\._deferred_keys\s*=\s*self\._deferred_keys\s*,\s*\[\]", flat), (
        "the queue is emptied by a tuple swap again — a mid-flush raise discards every "
        "message still queued behind the failure"
    )
    assert not re.search(r"self\._deferred_keys\s*=\s*\[\]", flat), (
        "the queue is cleared wholesale inside the flush"
    )
    assert "self._deferred_keys.pop(" in flat, (
        "items no longer leave the queue one at a time, so the remainder is not protected"
    )


# ---------------------------------------------------------------------------
# The happy path still works, and still only once
# ---------------------------------------------------------------------------


async def test_a_clean_flush_delivers_everything_in_order_and_empties_the_queue(monkeypatch):
    """POSITIVE CONTROL. Every test above asserts on what SURVIVES a failure; all of them
    would pass against a flush that delivered nothing at all."""
    p = _proc(monkeypatch)
    p._deferred_keys = [("one", True), ("two", False), ("three", True)]

    await p._flush_deferred_keys()

    payloads = [d.decode() for d, _ in p.writes]
    assert payloads == ["one", "\r", "two", "three", "\r"]
    assert p._deferred_keys == []
    assert {a for _, a in p.writes} == {"deferred-flush"}


async def test_a_concurrent_flush_cannot_double_send(monkeypatch):
    """The protection the swap was actually there for, preserved without it. Re-entering
    mid-flush must not send the same item twice."""
    p = _proc(monkeypatch)
    p._deferred_keys = [("one", True)]
    real_write = p._write
    reentered = {"n": 0}

    async def _reenter(data, *, actor="unknown"):
        await real_write(data, actor=actor)
        if reentered["n"] == 0:
            reentered["n"] = 1
            await p._flush_deferred_keys()  # re-entrant call, must no-op

    p._write = _reenter
    await p._flush_deferred_keys()

    assert [d.decode() for d, _ in p.writes] == ["one", "\r"], (
        f"the re-entrant flush double-sent: {[d for d, _ in p.writes]!r}"
    )


async def test_the_flag_is_cleared_even_when_the_flush_raises(monkeypatch):
    """A guard that stays set after a failure would silently disable every future flush —
    turning a one-off write error into permanent, total message loss."""
    p = _proc(monkeypatch)
    p._deferred_keys = [("one", True)]

    async def _boom(data, *, actor="unknown"):
        raise RuntimeError("boom")

    p._write = _boom
    with pytest.raises(RuntimeError):
        await p._flush_deferred_keys()

    assert p._flushing is False


async def test_no_auto_submit_is_introduced():
    """AC5. Enter is sent only for items whose caller asked for it — never synthesised."""
    import inspect

    code = inspect.getsource(WorkerProcess._flush_deferred_keys)

    assert 'await self._write(b"\\r"' in code
    assert "if qenter:" in code, (
        "the Enter is no longer conditional on what the caller queued — that is "
        "auto-submit, and two of #1858's strandings were production deploy approvals"
    )
