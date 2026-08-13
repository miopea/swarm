"""Broadcast is a Queen capability, not a worker one (operator ruling 2026-08-12).

WHY THE EXISTING #647 GATE WAS NOT ENOUGH, since it already blocked *some* broadcasts:
it inspects the CONTENT for directive/authority language. A worker with something
benign-sounding still reached every inbox, so fan-out was governed by PHRASING rather
than by authority — and a broadcast's cost to the fleet does not depend on how politely
it is worded. The operator's words: "this is a constant pain point".

The capability check added here does not read the message at all.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from swarm.mcp.handlers._messages import _handle_send_message
from swarm.worker.worker import QUEEN_WORKER_NAME


def _daemon() -> MagicMock:
    d = MagicMock()
    d.message_store.send.return_value = "msg-1"
    d.message_store.broadcast.return_value = ["m1", "m2"]
    d.workers = [MagicMock(name="w") for _ in range(3)]
    return d


def _text(result: Any) -> str:
    blocks = result.get("content") if isinstance(result, dict) else result
    return blocks[0]["text"] if blocks else ""


def _send(d: MagicMock, sender: str, to: str) -> Any:
    return _handle_send_message(d, sender, {"to": to, "type": "finding", "content": "hello"})


# ---------------------------------------------------------------------------
# The capability check
# ---------------------------------------------------------------------------


def test_a_worker_cannot_broadcast():
    d = _daemon()

    out = _text(_send(d, "platform", "*"))

    assert "Queen-only" in out
    d.message_store.broadcast.assert_not_called()
    d.message_store.send.assert_not_called()


def test_the_refusal_is_non_mutating():
    """Nothing is written and no worker receives it — asserted separately from the
    message text, because a refusal that still delivered would read identically to
    the caller."""
    d = _daemon()

    _send(d, "platform", "*")

    d.message_store.broadcast.assert_not_called()
    d.message_store.send.assert_not_called()


def test_the_refusal_names_what_to_do_instead():
    """A worker that cannot see the alternative just rephrases and retries — which
    is how the content-based #647 gate ended up being routed around."""
    out = _text(_send(_daemon(), "platform", "*"))

    assert "swarm_note_to_queen" in out
    assert "directly" in out


def test_the_queen_can_still_broadcast():
    """POSITIVE CONTROL. Without this, deleting the broadcast path outright would
    pass every test above while removing the capability entirely."""
    d = _daemon()

    _send(d, QUEEN_WORKER_NAME, "*")

    d.message_store.broadcast.assert_called_once()


def test_direct_worker_messages_are_untouched():
    """The change must not narrow ordinary 1:1 messaging, which is the path workers
    are being pointed AT."""
    d = _daemon()
    d.workers = [MagicMock()]
    d.workers[0].name = "hub"

    _send(d, "platform", "hub")

    assert d.message_store.send.called


@pytest.mark.parametrize("content", ["benign status update", "ALL WORKERS MUST STOP NOW"])
def test_refusal_does_not_depend_on_wording(content: str):
    """THE DISTINCTION FROM #647, pinned.

    #647 blocks on what the message SAYS. This blocks on who is asking. Both a
    harmless note and a naked directive are refused identically, so a worker cannot
    reword its way to a fan-out.
    """
    d = _daemon()

    out = _text(
        _handle_send_message(d, "platform", {"to": "*", "type": "finding", "content": content})
    )

    assert "Queen-only" in out
    d.message_store.broadcast.assert_not_called()


# ---------------------------------------------------------------------------
# The tool surface must not advertise what it refuses
# ---------------------------------------------------------------------------


def test_the_tool_no_longer_invites_broadcast():
    """The description used to say "Prefer direct messages over '*' broadcast", and the
    schema carried a `"to": "*"` EXAMPLE — which reads as an instruction to try it.
    A tool that advertises a capability it refuses trains workers to hit the refusal.
    """
    from swarm.mcp.handlers._messages import TOOLS

    tool = next(t for t in TOOLS if t["name"] == "swarm_send_message")

    # Case-insensitive: the description shouts it, the field description doesn't.
    assert "queen-only" in tool["description"].lower()
    examples = tool["inputSchema"].get("examples", [])
    assert all(e.get("to") != "*" for e in examples), "an example still demonstrates broadcast"
    assert "queen-only" in tool["inputSchema"]["properties"]["to"]["description"].lower()


def test_an_unknown_recipient_is_not_pointed_at_broadcast():
    """THE POINTER LEFT BEHIND BY THIS TICKET'S OWN CHANGE.

    Making broadcast Queen-only did not update the unknown-recipient error, which still
    ended "Use '*' to broadcast to all workers." So the single most likely way a worker
    reached this message — a typo'd name — answered it by recommending the one call the
    very next line of code refuses. A tool that suggests its own refusal trains the
    behaviour it is trying to stop.
    """
    d = _daemon()
    # A REAL roster is required, not the bare fixture: an unknowable roster deliberately
    # fails OPEN (#1543), so with MagicMock workers the send succeeds and this test would
    # pass vacuously while asserting nothing about the message.
    roster = []
    for name in ("hub", "platform", "swarm"):
        w = MagicMock()
        w.name = name
        roster.append(w)
    d.config.workers = roster

    out = _text(
        _handle_send_message(
            d, "platform", {"to": "no-such-worker", "type": "finding", "content": "x"}
        )
    )

    assert "not a registered worker" in out
    assert "'*'" not in out, "the unknown-name path still advertises broadcast"
    assert "note_to_queen" in out, "it should name the supported route instead"


def test_the_description_carries_the_test_the_queen_actually_applies():
    """Not decoration — it is the rule that decides the call, so it belongs where the
    caller reads it rather than in a closed ticket nobody opens at decision time.

    The Queen refused a fleet-wide retraction on 2026-08-13 with it: the claim had
    misled three parties, all reachable by name, so a broadcast would have woken
    fourteen uninvolved workers. Importance is not the test, and neither is scope — a
    finding can be true of the whole fleet and still require nothing of it.
    """
    from swarm.mcp.handlers._messages import TOOLS

    desc = next(t for t in TOOLS if t["name"] == "swarm_send_message")["description"].lower()

    assert "no stake" in desc
    assert "name them" in desc
