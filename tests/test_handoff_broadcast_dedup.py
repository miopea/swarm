"""#1116 — one broadcast must spawn ONE handoff task, not N.

``MessageStore.send`` fans a broadcast out into one row per recipient,
each with its own primary key. The watcher's ``_spawned_msg_ids`` guard
keys on that primary key, so it could never dedup across the fan-out:
every idle worker saw a different id for the same send and each spawned
its own task. Measured on the live board: one rcg-dev-install broadcast
wrote 23 rows sharing a single ``created_at``, and reached this worker
as #1108 / #1112 / #1113 — two of them byte-identical.
"""

from __future__ import annotations

import pytest

from swarm.drones.inter_worker_watcher import _source_key
from swarm.messages.store import Message


def _row(mid: int, recipient: str, *, sender="rcg-dev-install", created=1785444027.41832):
    """One row of a fan-out: same send, different recipient AND id."""
    return Message(
        id=mid,
        sender=sender,
        recipient=recipient,
        msg_type="warning",
        content="DO NOT PASTE THE .npmrc REMEDY BLOCK",
        created_at=created,
    )


def test_fanout_rows_share_a_source_key_despite_distinct_ids() -> None:
    fanout = [_row(2978, "swarm"), _row(2979, "aria"), _row(2980, "shotcraft")]
    assert len({m.id for m in fanout}) == 3, "precondition: ids really do differ"
    assert len({_source_key(m) for m in fanout}) == 1, "one send, one key"


def test_key_ignores_recipient_and_content() -> None:
    a = _row(1, "swarm")
    b = _row(2, "hub")
    b.content = "totally different text"
    assert _source_key(a) == _source_key(b)


def test_a_genuinely_distinct_send_still_spawns() -> None:
    """AC-3. Two separate sends differ in timestamp even with identical
    text, so the second is NOT swallowed — dedup must not become silence."""
    first = _row(10, "swarm", created=1785443539.04768)
    second = _row(11, "swarm", created=1785444027.41832)
    assert _source_key(first) != _source_key(second)


def test_a_different_sender_is_a_different_source() -> None:
    assert _source_key(_row(1, "swarm")) != _source_key(_row(2, "swarm", sender="hub"))


@pytest.mark.parametrize("n_idle", [2, 5, 23])
def test_only_the_first_idle_worker_spawns(n_idle: int) -> None:
    """The real shape: N idle workers each holding their own row of one
    broadcast. Simulates the watcher's filter + record steps."""
    fanout = [_row(2978 + i, f"w{i}") for i in range(n_idle)]
    spawned_sources: set[tuple[str, float]] = set()
    spawns = 0
    for m in fanout:  # one sweep per recipient, as the watcher does
        if _source_key(m) in spawned_sources:
            continue
        spawns += 1
        spawned_sources.add(_source_key(m))
    assert spawns == 1, f"{n_idle} idle workers spawned {spawns} tasks"
