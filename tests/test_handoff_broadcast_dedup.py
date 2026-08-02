"""#1116 — one broadcast must spawn ONE handoff task, not N.

``MessageStore.send`` fans a broadcast out into one row per recipient,
each with its own primary key. The watcher's ``_spawned_msg_ids`` guard
keys on that primary key, so it could never dedup across the fan-out:
every idle worker saw a different id for the same send and each spawned
its own task. Measured on the live board: one rcg-dev-install broadcast
wrote 23 rows sharing a single ``created_at``, and reached this worker
as #1108 / #1112 / #1113 — two of them byte-identical.

#1182 extends this file past ``_source_key`` in isolation. #1116's tests
only ever exercised the KEY; they never exercised the code that RECORDS
it, which is where the defect actually lived — see the section below.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from swarm.drones.inter_worker_watcher import _source_key
from swarm.messages.store import Message
from swarm.server.task_coordinator import _dedup_title, _handoff_source_tag
from tests.conftest import make_daemon


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


# ---------------------------------------------------------------------------
# #1182 — the dedup was recorded ONLY on the success path, but the spawn is
# not atomic. Measured on the live board (buzz_log, not reasoned about):
#
#   1785643709.71608  TASK_ASSIGNED     platform  queued: Handoff from hub: ...
#   1785643709.90776  TASK_SEND_FAILED  platform  ... [kept ASSIGNED for retry]
#   1785644834.87765  TASK_ASSIGNED     platform  queued: Handoff from hub: ...
#   1785644835.26268  TASK_SEND_FAILED  platform  ... [kept ASSIGNED for retry]
#
# Both spawns cite the SAME message row (#3151, hub → platform, a direct
# message — not a fan-out), so #1116's ``(sender, created_at)`` key was never
# the problem. ``board.create`` + ``assign`` SUCCEEDED and persisted a task
# row; only the PTY write failed, so ``assign_and_start_task`` returned False,
# so ``spawn_handoff_task`` returned False, so the watcher took its
# ``if not ok: return False`` branch and recorded NEITHER the in-memory dedup
# NOR the #894 ``mark_read``. A durable task row existed that nothing knew
# about. 19 minutes later the same message spawned #1181.
#
# The dedup must therefore key on something written at ``board.create`` time,
# independent of whether delivery later succeeded.
# ---------------------------------------------------------------------------


def _handoff_msg(mid: int, *, sender="hub", created=1785642598.47355, content="deploy gap"):
    return SimpleNamespace(
        id=mid, sender=sender, msg_type="warning", content=content, created_at=created
    )


def _daemon_with_failing_dispatch(monkeypatch, *, dispatch_ok=False):
    """Daemon whose PTY dispatch fails the way platform's did.

    ``assign_and_start_task`` returning False is exactly the observed
    TASK_SEND_FAILED outcome: the board write already happened, the PTY
    write did not.
    """
    d = make_daemon()
    monkeypatch.setattr(d, "edit_task", MagicMock())
    d.message_store = MagicMock(mark_read=MagicMock(return_value=1))
    real_assign = d.task_board.assign

    async def _assign_then_fail(task_id, worker_name, actor="user", message=None):
        real_assign(task_id, worker_name)  # board state persists...
        return dispatch_ok  # ...but delivery failed

    monkeypatch.setattr(d.tasks_coord, "assign_and_start_task", _assign_then_fail)
    return d


def _handoff_tasks(d):
    return [t for t in d.task_board.all_tasks if "auto-handoff" in (t.tags or [])]


@pytest.mark.asyncio
async def test_failed_dispatch_still_dedups_the_source_send(monkeypatch) -> None:
    """THE #1182 REGRESSION. A spawn whose PTY dispatch fails still created a
    durable task row, so offering the same message again must NOT create a
    second one — even though the first call returned False."""
    d = _daemon_with_failing_dispatch(monkeypatch)
    msg = _handoff_msg(3151)

    first = await d.tasks_coord.spawn_handoff_task("platform", msg)
    second = await d.tasks_coord.spawn_handoff_task("platform", msg)

    assert first is False, "precondition: dispatch failed, so the call reports failure"
    assert second is False
    assert len(_handoff_tasks(d)) == 1, "one send must not become two tracked tasks"


@pytest.mark.asyncio
async def test_dedup_survives_a_daemon_restart(monkeypatch) -> None:
    """AC-4. The in-memory guards are wiped by a reload; the board is not.
    A fresh TaskCoordinator over the SAME board must still refuse the
    re-spawn."""
    d = _daemon_with_failing_dispatch(monkeypatch)
    msg = _handoff_msg(3151)
    await d.tasks_coord.spawn_handoff_task("platform", msg)
    assert len(_handoff_tasks(d)) == 1

    # Restart: brand-new daemon object (empty in-memory state), same board.
    d2 = _daemon_with_failing_dispatch(monkeypatch)
    d2.task_board = d.task_board
    await d2.tasks_coord.spawn_handoff_task("platform", msg)

    assert len(_handoff_tasks(d)) == 1, "a reload must not resurrect a handed-off send"


@pytest.mark.asyncio
async def test_completed_handoff_does_not_respawn(monkeypatch) -> None:
    """#1181's exact shape: #1180 was DONE (completed_at 1785644668) before
    #1181 spawned at 1785644834, so the #647 open-title dedup could not see
    it. A closed handoff is still a handoff that happened."""
    d = _daemon_with_failing_dispatch(monkeypatch, dispatch_ok=True)
    msg = _handoff_msg(3151)
    await d.tasks_coord.spawn_handoff_task("platform", msg)
    (task,) = _handoff_tasks(d)
    d.task_board.complete(task.id, resolution="handled")

    await d.tasks_coord.spawn_handoff_task("platform", msg)

    assert len(_handoff_tasks(d)) == 1, "a completed handoff must not re-spawn"


@pytest.mark.asyncio
async def test_a_distinct_later_send_still_spawns(monkeypatch) -> None:
    """AC-6 — dedup must not become silence. A genuinely separate send from
    the same worker, even with identical text, is a different source."""
    d = _daemon_with_failing_dispatch(monkeypatch)
    await d.tasks_coord.spawn_handoff_task("platform", _handoff_msg(3151, created=1785642598.4))
    await d.tasks_coord.spawn_handoff_task("platform", _handoff_msg(3199, created=1785649999.9))

    assert len(_handoff_tasks(d)) == 2, "a second, distinct warning must still be tracked"


@pytest.mark.asyncio
async def test_broadcast_fanout_still_collapses_to_one_task(monkeypatch) -> None:
    """Don't regress #1116. A fan-out is N rows / N ids / ONE created_at, and
    it must still produce exactly one tracked task across recipients."""
    d = _daemon_with_failing_dispatch(monkeypatch)
    for i in range(5):
        msg = _handoff_msg(2978 + i, created=1785444027.4)
        await d.tasks_coord.spawn_handoff_task(f"w{i}", msg)

    assert len(_handoff_tasks(d)) == 1


@pytest.mark.asyncio
async def test_title_carries_source_message_id_and_send_time(monkeypatch) -> None:
    """AC-5. Staleness must be visible BEFORE the work starts — #1180's title
    gave no hint that the message it carried was already 18 minutes old and
    partly overtaken by #1179."""
    d = _daemon_with_failing_dispatch(monkeypatch)
    await d.tasks_coord.spawn_handoff_task("platform", _handoff_msg(3151))
    (task,) = _handoff_tasks(d)

    assert "#3151" in task.title
    assert "2026-08-02T" in task.title, f"no send timestamp in {task.title!r}"


@pytest.mark.asyncio
async def test_legacy_untagged_open_handoff_still_blocks_a_respawn(monkeypatch) -> None:
    """Handoff tasks that predate #1182 carry no source tag. A still-open one
    must not be duplicated the first time its message is re-offered, or
    deploying the fix would itself spawn a round of duplicates."""
    d = _daemon_with_failing_dispatch(monkeypatch)
    legacy = d.task_board.create(title="Handoff from hub: deploy gap", tags=["auto-handoff"])
    d.task_board.assign(legacy.id, "platform")

    await d.tasks_coord.spawn_handoff_task("platform", _handoff_msg(3151))

    assert len(_handoff_tasks(d)) == 1


@pytest.mark.asyncio
async def test_timestampless_messages_keep_the_647_title_collapse(monkeypatch) -> None:
    """Without a ``created_at`` the source key cannot tell a fan-out sibling
    from a distinct re-send, so the title must stay in its legacy form and
    #647's same-title collapse must still carry the fan-out. Adding provenance
    unconditionally silently retired that guard."""
    d = _daemon_with_failing_dispatch(monkeypatch, dispatch_ok=True)
    a = SimpleNamespace(id=638, sender="realtruth", msg_type="warning", content="move to staging")
    b = SimpleNamespace(id=639, sender="realtruth", msg_type="warning", content="move to staging")

    assert await d.tasks_coord.spawn_handoff_task("web", a) is True
    assert await d.tasks_coord.spawn_handoff_task("hub", b) is False

    (task,) = _handoff_tasks(d)
    assert task.title == "Handoff from realtruth: move to staging"


def test_source_tag_matches_the_watcher_key() -> None:
    """The durable tag and the in-memory guard must key on the SAME thing,
    or they disagree about what a duplicate is."""
    row_a, row_b = _row(2978, "swarm"), _row(2979, "aria")
    assert _source_key(row_a) == _source_key(row_b)
    assert _handoff_source_tag(row_a.sender, row_a.created_at, row_a.id) == _handoff_source_tag(
        row_b.sender, row_b.created_at, row_b.id
    )


def test_source_tag_falls_back_to_message_id_without_a_timestamp() -> None:
    """A message with no usable ``created_at`` must NOT collapse into one
    shared bucket — over-dedup is silence, which is the worse failure."""
    a = _handoff_source_tag("hub", 0.0, 11)
    b = _handoff_source_tag("hub", 0.0, 12)
    assert a != b


def test_dedup_title_strips_provenance_preserving_the_647_check() -> None:
    """#647 collapses same-sender/same-content handoffs by exact title. Now
    that titles carry a per-message provenance segment, that check has to
    compare the stable part or it silently stops matching."""
    new = "Handoff from hub (msg #3151, sent 2026-08-02T03:49Z): deploy gap"
    old = "Handoff from hub: deploy gap"
    assert _dedup_title(new) == _dedup_title(old) == old
