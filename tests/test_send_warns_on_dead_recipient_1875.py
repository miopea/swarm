"""A send to a configured-but-not-running worker looks exactly like a send to a live one.

MEASURED (swarm.db, 2026-08-18): 429 of 551 unread messages fleet-wide — 78% — belong to
nine workers that have never read anything. Oldest 53 days. admin filed 20 of their own
believing they were mis-targeted sends; they were not, and I have 9 in the same pile.

WHY EVERY SEND SUCCEEDS. `_guard_direct_send` validates the recipient against the
CONFIGURED roster, deliberately, so an offline-but-configured worker still validates
(#873's fix for sends to nonexistent names). Seven of the nine ARE configured. The guard
passes, the row is written, the sender is told it was queued, and nobody opens the inbox.

THE HOLE FOR *UNCONFIGURED* NAMES IS ALREADY CLOSED, verified against the live roster:
`admin-agent1` and a made-up name are both REFUSED today. Its 108 rows predate #873. So
the remaining defect is precisely the configured-but-dormant case, which is the one the
guard is designed to allow.

NOT A REFUSAL. A worker can be legitimately restarting, and a guard that blocked sends to
a momentarily-down worker would be switched off within a day — this codebase's most
reliably observed failure mode. This reports and lets the sender decide.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from swarm.mcp.handlers._messages import _handle_send_message, _liveness_notice


def _daemon(*, alive: bool | None, known: bool = True):
    """A daemon whose recipient is alive / dead / unknown to it."""
    if known:
        process = None if alive is None else SimpleNamespace(is_alive=alive)
        worker = SimpleNamespace(name="aria", process=process)
    else:
        worker = None
    d = SimpleNamespace(
        get_worker=lambda _n: worker,
        config=SimpleNamespace(workers=[SimpleNamespace(name=n) for n in ("swarm", "aria")]),
        drone_log=MagicMock(),
        message_store=MagicMock(),
        queen=None,
    )
    d.message_store.send = MagicMock(return_value=1)
    return d


# ---------------------------------------------------------------------------
# AC3 — the signal
# ---------------------------------------------------------------------------


def test_a_dead_recipient_is_reported_at_send_time():
    notice = _liveness_notice(_daemon(alive=False), "aria")

    assert "NOBODY IS RUNNING AS aria" in notice


def test_a_live_recipient_produces_no_noise():
    """POSITIVE CONTROL. A notice that fired unconditionally would pass the test above and
    put a scary paragraph on every send in the fleet."""
    assert _liveness_notice(_daemon(alive=True), "aria") == ""


def test_a_worker_with_no_process_at_all_counts_as_not_running():
    assert "NOBODY IS RUNNING" in _liveness_notice(_daemon(alive=None), "aria")


def test_it_says_resending_as_another_type_will_not_help():
    """THE DISTINCTION FROM #1843, AND WHY BOTH NOTICES EXIST. The type warning tells you
    an informational message will not WAKE the recipient — fixed by resending as
    'dependency'. This one says there is nobody to wake, which resending cannot fix. A
    sender who conflated them would retype the same message three times."""
    notice = _liveness_notice(_daemon(alive=False), "aria")

    assert "will NOT help" in notice
    assert "no session to wake" in notice


def test_both_notices_appear_together_when_both_are_true():
    d = _daemon(alive=False)

    out = _handle_send_message(d, "swarm", {"to": "aria", "type": "status", "content": "x"})[0][
        "text"
    ]

    assert "NOT DELIVERED YET" in out  # #1843: informational type
    assert "NOBODY IS RUNNING AS aria" in out  # #1875: nobody home
    assert d.message_store.send.called, "the row must still be written — this is not a refusal"


def test_it_is_NOT_a_refusal_even_for_a_dead_worker():
    """AC-adjacent and load-bearing: a worker can be restarting. Blocking here is how the
    warning gets switched off, and then nothing reports it at all."""
    d = _daemon(alive=False)

    out = _handle_send_message(d, "swarm", {"to": "aria", "type": "dependency", "content": "x"})[0][
        "text"
    ]

    assert d.message_store.send.called
    assert "not a registered worker" not in out


# ---------------------------------------------------------------------------
# It must never claim health it did not check
# ---------------------------------------------------------------------------


def test_an_unknown_worker_produces_silence_not_reassurance():
    """`get_worker` returning None means this daemon cannot say. Silence is correct;
    inventing "recipient is fine" would be the defect this whole week catalogued."""
    assert _liveness_notice(_daemon(alive=True, known=False), "aria") == ""


def test_a_raising_lookup_produces_silence_and_never_breaks_the_send():
    d = _daemon(alive=False)
    d.get_worker = MagicMock(side_effect=RuntimeError("registry down"))

    assert _liveness_notice(d, "aria") == ""

    out = _handle_send_message(d, "swarm", {"to": "aria", "type": "status", "content": "x"})[0][
        "text"
    ]
    assert "Queued for aria" in out


@pytest.mark.parametrize("msg_type", ["status", "finding", "dependency", "warning"])
def test_the_liveness_notice_is_independent_of_message_type(msg_type):
    """Nobody home is nobody home. Unlike the type warning, this does not vary — and a
    sender must not be able to read its absence on a 'dependency' as reassurance."""
    d = _daemon(alive=False)

    out = _handle_send_message(d, "swarm", {"to": "aria", "type": msg_type, "content": "x"})[0][
        "text"
    ]

    assert "NOBODY IS RUNNING AS aria" in out
