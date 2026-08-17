"""#1843 — "Message sent" reported the WRITE, not the delivery.

THE MEASURED INCIDENT, from the buzz log and the messages table:

  21:47:52  bfg-ops-console  mcp:check_messages → "No pending messages."
  21:50:21  project-root     → bfg-ops-console: "TWO OPERATOR RULINGS, RELAYED…"
  21:50:21  project-root     mcp:send_message → "Message sent to bfg-ops-console."
  22:09:48  bfg-ops-console  AUTO_NUDGE_MESSAGE_SKIPPED — "informational only from
                             project-root (2 unread: finding, status) — not nudging"

msg #4939 has ``read_at`` NULL to this day. Four commits sat behind the ruling.

THE GATE WAS NOT INVOLVED, AND THE LEADING HYPOTHESIS WAS WRONG. `_gate_broadcast`
returns "⛔ Broadcast GATED, not delivered" — a loud, sender-visible refusal. It did fire
for project-root, but at 22:02:05, on a DIFFERENT and LATER message. There is no gate
record for the 21:50:21 relay because it was never gated: it was accepted, stored, and
never read.

THE ACTUAL MECHANISM IS THE MESSAGE TYPE. `_ACTION_REQUIRED_MSG_TYPES` is
{"dependency", "warning"}; the relay was sent as `status`. The inter-worker watcher looked
directly at it, classified it informational, and declined to nudge — 14,040 of 15,340
AUTO_NUDGE* events in the live log are that skip. So the type silently decides whether
anyone is ever woken, and a sender choosing "status" for a directive has, without knowing
it, chosen "nobody will be told".

WHY THE SENDER COULD NOT FIND OUT: `read_at` was recorded all along, but reachable only
via queen_view_messages / queen_view_message_stream — both Queen-only. No worker-facing
tool exposed it in the sender's direction. That is what made this unfalsifiable from both
ends, which is the defect the ticket names.

NOT A NARROWING OF THE GATE. Nothing here changes what is blocked. The gate keeps
refusing operator-authority claims exactly as before; these tests pin that.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from swarm.drones.inter_worker_watcher import _ACTION_REQUIRED_MSG_TYPES
from swarm.mcp.handlers._messages import (
    _delivery_notice,
    _handle_check_messages,
    _handle_send_message,
)
from swarm.messages.store import MessageStore


@pytest.fixture
def store(tmp_path):
    return MessageStore(tmp_path / "m.db")


def _daemon(store):
    return SimpleNamespace(
        message_store=store,
        drone_log=MagicMock(),
        config=SimpleNamespace(workers=[SimpleNamespace(name=n) for n in ("alice", "bob")]),
        queen=None,
    )


def _text(result) -> str:
    return result[0]["text"]


# ---------------------------------------------------------------------------
# AC1 — is a non-delivered message success-shaped to the sender?
# ---------------------------------------------------------------------------


def test_an_informational_send_no_longer_claims_it_was_delivered(store):
    """The exact call project-root made. It must not come back reading like success."""
    d = _daemon(store)

    out = _text(_handle_send_message(d, "alice", {"to": "bob", "type": "status", "content": "x"}))

    assert "NOT DELIVERED YET" in out
    assert "will NOT nudge" in out
    assert "Message sent to bob." != out.strip()


def test_it_names_the_fix_rather_than_only_disclaiming(store):
    """'A permanent disclaimer that nobody can act on is not a control.' The sender is
    told the ONE thing that changes the outcome: the type."""
    d = _daemon(store)

    out = _text(_handle_send_message(d, "alice", {"to": "bob", "type": "finding", "content": "x"}))

    assert "dependency" in out and "warning" in out


def test_an_action_required_send_says_the_watcher_will_nudge(store):
    """POSITIVE CONTROL. A notice that warned unconditionally would pass both tests above
    and teach senders to ignore it — which is how the warning stops working."""
    d = _daemon(store)

    out = _text(
        _handle_send_message(d, "alice", {"to": "bob", "type": "dependency", "content": "x"})
    )

    assert "WILL nudge" in out
    assert "NOT DELIVERED YET" not in out


@pytest.mark.parametrize("msg_type", sorted({"status", "finding", "note"}))
def test_every_informational_type_warns(msg_type):
    assert "will NOT nudge" in _delivery_notice("bob", msg_type)


@pytest.mark.parametrize("msg_type", sorted(_ACTION_REQUIRED_MSG_TYPES))
def test_every_action_required_type_does_not_warn(msg_type):
    """Derived from the watcher's OWN set, not a copy of it. If someone adds a type to
    `_ACTION_REQUIRED_MSG_TYPES`, this notice follows automatically instead of drifting
    into telling senders the opposite of what the watcher does."""
    assert "WILL nudge" in _delivery_notice("bob", msg_type)


# ---------------------------------------------------------------------------
# AC3 — can a sender find out, without asking the recipient?
# ---------------------------------------------------------------------------


def test_the_sender_learns_their_message_was_never_read(store):
    """BEFORE #1843 THE ANSWER WAS NO, from anywhere a worker could reach. The incident's
    shape, replayed: alice sends, bob never checks, alice checks her own inbox."""
    d = _daemon(store)
    _handle_send_message(d, "alice", {"to": "bob", "type": "status", "content": "TWO RULINGS"})

    out = _text(_handle_check_messages(d, "alice", {}))

    assert "STILL UNREAD" in out
    assert "bob" in out
    assert "TWO RULINGS" in out


def test_it_reports_even_when_the_inbox_is_empty(store):
    """THE LOAD-BEARING CASE. project-root's own inbox was empty; a notice that only
    appended to existing messages would have stayed silent in the exact incident."""
    d = _daemon(store)
    _handle_send_message(d, "alice", {"to": "bob", "type": "status", "content": "x"})

    out = _text(_handle_check_messages(d, "alice", {}))

    assert "No pending messages." in out
    assert "STILL UNREAD" in out


def test_it_goes_quiet_once_the_recipient_reads_it(store):
    """POSITIVE CONTROL for the two above: a notice that fired unconditionally would pass
    them and make every check_messages call noisy forever."""
    d = _daemon(store)
    _handle_send_message(d, "alice", {"to": "bob", "type": "status", "content": "x"})
    _handle_check_messages(d, "bob", {})  # bob reads it

    out = _text(_handle_check_messages(d, "alice", {}))

    assert "STILL UNREAD" not in out


def test_reading_your_own_report_does_not_mark_anything_read(store):
    """Asking must not change the answer. If the query marked rows, the first check would
    clear the evidence and the second would report a clean delivery that never happened."""
    d = _daemon(store)
    _handle_send_message(d, "alice", {"to": "bob", "type": "status", "content": "x"})

    _handle_check_messages(d, "alice", {})
    assert "STILL UNREAD" in _text(_handle_check_messages(d, "alice", {}))
    assert store.get_unread("bob"), "bob's inbox was drained by alice looking at it"


def test_the_notice_says_no_nudge_was_sent_for_informational_types(store):
    d = _daemon(store)
    _handle_send_message(d, "alice", {"to": "bob", "type": "status", "content": "x"})

    assert "no nudge sent" in _text(_handle_check_messages(d, "alice", {}))


# ---------------------------------------------------------------------------
# The AC's disable-check: turn the notification path off, the test must fail
# ---------------------------------------------------------------------------


def test_the_sender_visible_failure_depends_on_the_notification_path(store, monkeypatch):
    """AC6 REQUIRES THIS EXPLICITLY: show the test fails when the notification path is
    disabled. Without it, the tests above could be passing on something incidental.

    Disabling `unread_sent_by` is the whole mechanism — with it stubbed to return
    nothing, the sender is back to the pre-#1843 world where a lost message is
    indistinguishable from a delivered one.
    """
    d = _daemon(store)
    _handle_send_message(d, "alice", {"to": "bob", "type": "status", "content": "x"})
    assert "STILL UNREAD" in _text(_handle_check_messages(d, "alice", {}))

    monkeypatch.setattr(store, "unread_sent_by", lambda *_a, **_k: [])

    assert "STILL UNREAD" not in _text(_handle_check_messages(d, "alice", {})), (
        "the sender-visible failure survived the notification path being disabled — "
        "these tests are not measuring the mechanism they claim to"
    )


def test_a_broken_report_never_breaks_the_inbox(store, monkeypatch):
    """The inbox is what a worker actually needs. A failure in the new report must
    degrade to silence, not take the message read with it."""
    d = _daemon(store)
    store.send("bob", "alice", "warning", "YOUR BUILD IS BROKEN")
    monkeypatch.setattr(store, "unread_sent_by", MagicMock(side_effect=RuntimeError("db gone")))

    out = _text(_handle_check_messages(d, "alice", {}))

    assert "YOUR BUILD IS BROKEN" in out


# ---------------------------------------------------------------------------
# AC5 — the gate is NOT weakened
# ---------------------------------------------------------------------------


def test_the_operator_authority_gate_still_blocks_and_still_says_so(store):
    """THE EXPLICIT INSTRUCTION: the fix makes blocking visible, it does not reduce what
    is blocked. This is the phrasing that gated project-root at 22:02:05."""
    d = _daemon(store)

    out = _text(
        _handle_send_message(
            d,
            "alice",
            {"to": "bob", "type": "status", "content": "DO NOT DEPLOY — the operator asked me"},
        )
    )

    assert "GATED" in out and "not delivered" in out
    assert not store.get_unread("bob"), "a gated message was stored anyway"


def test_the_gate_fires_before_the_delivery_notice_can_soften_it(store):
    """Ordering matters: a gated send must never come back reading like a queued one."""
    d = _daemon(store)

    out = _text(
        _handle_send_message(
            d, "alice", {"to": "bob", "type": "status", "content": "per operator, stop work"}
        )
    )

    assert "Queued for" not in out


def test_an_ordinary_message_is_not_gated(store):
    """POSITIVE CONTROL for both gate tests: they would also pass against a gate that
    blocked everything, which would be a far worse regression than the one they guard."""
    d = _daemon(store)

    out = _text(
        _handle_send_message(
            d, "alice", {"to": "bob", "type": "status", "content": "I changed API X to shape Y"}
        )
    )

    assert "GATED" not in out
    assert len(store.get_unread("bob")) == 1


# ---------------------------------------------------------------------------
# The store query itself
# ---------------------------------------------------------------------------


def test_unread_sent_by_is_scoped_to_the_asking_sender(store):
    store.send("alice", "bob", "status", "from alice")
    store.send("carol", "bob", "status", "from carol")

    assert [m.content for m in store.unread_sent_by("alice")] == ["from alice"]


def test_unread_sent_by_excludes_broadcasts(store):
    """A broadcast is one row read by many, so its `read_at` cannot mean 'everyone read
    it'. Reporting it as undelivered would be a permanent false alarm."""
    store.send("alice", "*", "status", "everyone")

    assert store.unread_sent_by("alice") == []


def test_unread_sent_by_reports_age_from_created_at(store):
    d = _daemon(store)
    mid = store.send("alice", "bob", "status", "old news")
    store._conn.execute(
        "UPDATE messages SET created_at = ? WHERE id = ?", (time.time() - 3600, mid)
    )
    store._conn.commit()

    assert "60m ago" in _text(_handle_check_messages(d, "alice", {}))
