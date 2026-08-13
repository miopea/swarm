"""#1570 — the handoff generator must not resurrect stale or answered messages.

MEASURED FIRST, and the ticket's premise did not survive. Across 62 AUTO_HANDOFF_TASK
events in 27 days, EVERY ONE produced exactly one task — there is no one-message-to-N
fan-out, because #1116 already records the SEND as spawned. The burst that prompted the
report (4 tasks in 6 seconds) was four DIFFERENT messages: a backlog draining, not one
message amplifying.

What IS real: no age cut-off (max observed 19.1 HOURS, 7 of 62 over an hour) and no
answered-check. The concurrency risk is real too, but its cause is the sweep, which is
why the ceiling here is per-pass rather than per-message.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.drones.inter_worker_watcher import (
    _HANDOFF_MAX_AGE_SECONDS,
    _HANDOFF_MAX_PER_SWEEP,
    InterWorkerMessageWatcher,
)


def _msg(mid: int, *, age_seconds: float = 0.0, sender: str = "hub") -> MagicMock:
    m = MagicMock()
    m.id = mid
    m.msg_type = "warning"
    m.sender = sender
    m.recipient = "swarm"
    m.created_at = time.time() - age_seconds
    return m


def _watcher(*, replies: list | None = None) -> InterWorkerMessageWatcher:
    w = InterWorkerMessageWatcher.__new__(InterWorkerMessageWatcher)
    w._spawn_handoff_task = AsyncMock(return_value=True)
    w._spawned_msg_ids = set()
    w._spawned_sources = set()
    w._handoff_spawns_this_sweep = 0
    w._last_nudge = {}
    w._drone_log = MagicMock()
    store = MagicMock()
    store.get_recent.return_value = replies or []
    store.mark_read = MagicMock()
    w._message_store = store
    return w


async def _run(w, msgs) -> bool:
    return await w._maybe_spawn_handoff("swarm", msgs, now=time.time())


# ---------------------------------------------------------------------------
# Age cut-off (AC3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stale_message_is_not_resurrected():
    """The reported incident: an 11-hour-old resolved message waking five workers.
    The measured tail reached 19.1 hours, so this is well inside real traffic."""
    w = _watcher()

    assert await _run(w, [_msg(1, age_seconds=_HANDOFF_MAX_AGE_SECONDS + 3600)]) is False
    w._spawn_handoff_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_fresh_message_still_fans_out():
    """AC5 POSITIVE CONTROL. Without this the fix could pass by disabling the
    generator entirely — 55 of the 62 measured events were under an hour old and
    must keep working."""
    w = _watcher()

    assert await _run(w, [_msg(2, age_seconds=60)]) is True
    w._spawn_handoff_task.assert_awaited_once()


# ---------------------------------------------------------------------------
# Answered-check (AC2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_answered_message_produces_no_handoff():
    """A reply from the recipient back to the sender, after the message arrived."""
    reply = MagicMock()
    reply.sender = "swarm"
    reply.recipient = "hub"
    w = _watcher(replies=[reply])

    assert await _run(w, [_msg(3, age_seconds=60)]) is False
    w._spawn_handoff_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unrelated_reply_does_not_suppress():
    """CONTROL: a message between two OTHER workers must not read as an answer, or
    the guard would suppress real handoffs whenever the fleet was chatty."""
    other = MagicMock()
    other.sender = "platform"
    other.recipient = "admin"
    w = _watcher(replies=[other])

    assert await _run(w, [_msg(4, age_seconds=60)]) is True


@pytest.mark.asyncio
async def test_read_at_is_not_used_as_the_answered_signal():
    """THE MEASUREMENT THAT SHAPED THIS GUARD.

    All 62 handoff messages carried a `read_at` within 2s of the handoff, average gap
    0.0s — the generator sets it itself (#894). A guard keyed on `read_at` would fire
    on everything while appearing to check something. A message marked read but never
    replied to must still fan out.
    """
    w = _watcher(replies=[])
    m = _msg(5, age_seconds=60)
    m.read_at = time.time()

    assert await _run(w, [m]) is True


# ---------------------------------------------------------------------------
# Per-sweep ceiling (AC4) and the #1116 pin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_ceiling_bounds_dispatches_per_sweep():
    """AC4, retargeted from per-message to per-sweep because the measurement says
    per-message is already 1. The cost that redlined the machine was CONCURRENT
    builds from a backlog draining at once."""
    w = _watcher()
    w._handoff_spawns_this_sweep = _HANDOFF_MAX_PER_SWEEP

    assert await _run(w, [_msg(6, age_seconds=60)]) is False
    w._spawn_handoff_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_message_still_yields_exactly_one_task():
    """REGRESSION PIN FOR #1116, which already closed the hole this ticket believed
    was open. Measured: 62 of 62 events produced one task. If a change ever makes a
    single send spawn per-recipient, this fails rather than the fleet discovering it.
    """
    w = _watcher()
    m = _msg(7, age_seconds=60)

    assert await _run(w, [m]) is True
    assert w._spawn_handoff_task.await_count == 1


@pytest.mark.asyncio
async def test_an_unknowable_age_fails_open():
    """THE DIRECTION THIS FLEET KEEPS GETTING BACKWARDS, so it is pinned.

    A missing or zero `created_at` means "could not determine how old this is", NOT
    "infinitely old". Treating it as ancient silently suppresses real handoffs — a
    worse failure than the stale-resurrection the guard exists to stop, because a
    suppressed handoff is invisible while a resurrected one announces itself.

    Caught by the suite: three existing watcher tests use a `ts=0.0` fixture and went
    red on the first version of this guard. Same shape as an empty worker roster read
    as "nobody exists" and an absent tool schema read as "nothing allowed".
    """
    w = _watcher()
    m = _msg(8, age_seconds=0)
    m.created_at = 0.0

    assert await _run(w, [m]) is True

    # NOT tested: created_at=None. `_source_key` does float(m.created_at) and would
    # already have raised before this guard existed, and the store always stamps a
    # timestamp — so it is an impossible input, and hardening against it here would be
    # speculative. The zero case above is the one the fixtures actually produce.
