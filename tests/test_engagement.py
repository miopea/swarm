"""Task #913: engagement_snapshot + is_duplicate_work + handoff-spawn
duplicate suppression."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.server.engagement import (
    EngagementInfo,
    engagement_snapshot,
    is_duplicate_work,
)
from tests.conftest import make_daemon


def _task(number=0, title="", source_worker="", jira_key="", started_at=None):
    return SimpleNamespace(
        number=number,
        title=title,
        source_worker=source_worker,
        jira_key=jira_key,
        started_at=started_at,
    )


def _msg(sender="api", msg_type="dependency", created_at=0.0):
    return SimpleNamespace(sender=sender, msg_type=msg_type, created_at=created_at)


def _board(*, active=None, assigned=None):
    b = MagicMock()
    b.current_task_for_worker.return_value = active
    b.assigned_or_active_tasks_for_worker.return_value = assigned if assigned is not None else []
    return b


def _store(unread):
    s = MagicMock()
    s.get_unread.return_value = unread
    return s


# ---------------------------------------------------------------------------
# engagement_snapshot
# ---------------------------------------------------------------------------


def test_snapshot_empty_on_no_board_or_worker():
    assert engagement_snapshot(None, None, "", now=100.0) == EngagementInfo(worker="")
    snap = engagement_snapshot(None, None, "hub", now=100.0)
    assert snap.active_task is None and snap.assigned_count == 0
    assert snap.recent_handoff is None


def test_snapshot_active_task_age_and_count():
    active = _task(number=5, title="Fix the thing", started_at=940.0)
    board = _board(active=active, assigned=[active, _task(number=6)])
    snap = engagement_snapshot(board, None, "hub", now=1000.0)
    assert snap.active_task is active
    assert snap.active_started_ago == 60.0
    assert snap.assigned_count == 2


def test_snapshot_assigned_only_no_active():
    board = _board(active=None, assigned=[_task(number=6)])
    snap = engagement_snapshot(board, None, "hub", now=1000.0)
    assert snap.active_task is None
    assert snap.active_started_ago is None
    assert snap.assigned_count == 1


def test_snapshot_recent_inbound_handoff():
    store = _store([_msg("platform", "dependency", created_at=970.0), _msg("x", "status", 999.0)])
    snap = engagement_snapshot(None, store, "hub", now=1000.0)
    assert snap.recent_handoff is not None
    assert snap.recent_handoff.sender == "platform"
    assert snap.recent_handoff_ago == 30.0  # status msg ignored (not action-required)


def test_snapshot_store_exception_is_safe():
    store = MagicMock()
    store.get_unread.side_effect = RuntimeError("db down")
    snap = engagement_snapshot(_board(), store, "hub", now=1000.0)
    assert snap.recent_handoff is None  # no crash


def test_collides_within_window():
    fresh = EngagementInfo(active_started_ago=120.0)
    assert fresh.collides_within(300.0) is True
    stale = EngagementInfo(active_started_ago=600.0)
    assert stale.collides_within(300.0) is False
    # handoff path
    assert EngagementInfo(recent_handoff_ago=10.0).collides_within(300.0) is True
    # window 0 disables
    assert fresh.collides_within(0.0) is False
    # idle worker never collides
    assert EngagementInfo().collides_within(300.0) is False


# ---------------------------------------------------------------------------
# is_duplicate_work
# ---------------------------------------------------------------------------


def test_dup_by_number():
    incoming = _task(number=42)
    assert is_duplicate_work(incoming, [_task(number=42, title="diff")]) is not None


def test_dup_by_jira_key():
    incoming = _task(jira_key="PROJ-7")
    assert is_duplicate_work(incoming, [_task(jira_key="PROJ-7")]) is not None
    # empty jira keys never match
    assert is_duplicate_work(_task(jira_key=""), [_task(jira_key="")]) is None


def test_dup_by_source_and_high_title_similarity():
    incoming = _task(source_worker="api", title="Handoff from api: fix the foo bug")
    existing = _task(source_worker="api", title="Handoff from api: fix the foo bug now")
    assert is_duplicate_work(incoming, [existing], similarity=0.8) is not None


def test_no_dup_different_source():
    incoming = _task(source_worker="api", title="Handoff from api: fix the foo bug")
    existing = _task(source_worker="web", title="Handoff from api: fix the foo bug")
    assert is_duplicate_work(incoming, [existing], similarity=0.8) is None


def test_no_dup_low_title_similarity():
    incoming = _task(source_worker="api", title="bump eslint to 10")
    existing = _task(source_worker="api", title="migrate database to postgres 16")
    assert is_duplicate_work(incoming, [existing], similarity=0.8) is None


def test_no_dup_empty_list():
    assert is_duplicate_work(_task(number=1), []) is None
    assert is_duplicate_work(_task(number=1), None) is None


def test_dup_never_matches_on_freeform_content():
    # Two tasks with NO structured overlap (no number/jira/source) but identical
    # freeform-ish title tokens still don't match without a shared source_worker.
    incoming = _task(source_worker="", title="do the thing")
    existing = _task(source_worker="", title="do the thing")
    assert is_duplicate_work(incoming, [existing]) is None


# ---------------------------------------------------------------------------
# spawn_handoff_task duplicate suppression (#913 PATH 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_suppressed_when_recipient_already_engaged(monkeypatch):
    d = make_daemon()
    monkeypatch.setattr(d.tasks_coord, "assign_and_start_task", AsyncMock(return_value=True))
    monkeypatch.setattr(d, "edit_task", MagicMock())
    mark_read = MagicMock(return_value=1)
    d.message_store = MagicMock(mark_read=mark_read)

    # 'web' is already ACTIVE on a near-duplicate task from the SAME source 'api'.
    existing = d.task_board.create(title="Handoff from api: please fix the foo bug now")
    existing.source_worker = "api"
    d.task_board.assign(existing.id, "web")
    d.task_board.activate(existing.id)

    # Incoming handoff: same sender, near-identical content (different title string,
    # so the exact-title #647 dedup does NOT fire — this exercises is_duplicate_work).
    msg = SimpleNamespace(
        sender="api", msg_type="dependency", id=99, content="please fix the foo bug"
    )
    before = len(list(d.task_board.all_tasks))
    result = await d.tasks_coord.spawn_handoff_task("web", msg)

    assert result is False
    assert len(list(d.task_board.all_tasks)) == before  # no duplicate task spawned
    mark_read.assert_called_once_with("web", [99])  # source consumed (#894 pattern)
    assert any(
        getattr(e.action, "value", "") == "AUTO_HANDOFF_TASK" and "suppressed duplicate" in e.detail
        for e in d.drone_log.entries
    )


@pytest.mark.asyncio
async def test_suppression_survives_1182_provenance_on_both_titles(monkeypatch):
    """#1182 put ``(msg #N, sent …)`` in handoff titles. Those tokens are per-
    message, so with provenance on the existing task AND the incoming one they
    count against the Jaccard score twice — #913's suppression would silently
    stop matching work it matched the day before."""
    d = make_daemon()
    monkeypatch.setattr(d.tasks_coord, "assign_and_start_task", AsyncMock(return_value=True))
    monkeypatch.setattr(d, "edit_task", MagicMock())
    d.message_store = MagicMock(mark_read=MagicMock(return_value=1))

    existing = d.task_board.create(
        title="Handoff from api (msg #40, sent 2026-08-01T09:15Z): please fix the foo bug now"
    )
    existing.source_worker = "api"
    d.task_board.assign(existing.id, "web")
    d.task_board.activate(existing.id)

    msg = SimpleNamespace(
        sender="api",
        msg_type="dependency",
        id=99,
        content="please fix the foo bug",
        created_at=1785642598.47355,
    )
    before = len(list(d.task_board.all_tasks))
    result = await d.tasks_coord.spawn_handoff_task("web", msg)

    assert result is False
    assert len(list(d.task_board.all_tasks)) == before


@pytest.mark.asyncio
async def test_non_duplicate_handoff_still_spawns(monkeypatch):
    """Don't regress #442/#894 — an unrelated handoff still spawns."""
    d = make_daemon()
    monkeypatch.setattr(d.tasks_coord, "assign_and_start_task", AsyncMock(return_value=True))
    monkeypatch.setattr(d, "edit_task", MagicMock())
    existing = d.task_board.create(title="Handoff from api: migrate db to postgres")
    existing.source_worker = "api"
    d.task_board.assign(existing.id, "web")
    d.task_board.activate(existing.id)

    msg = SimpleNamespace(sender="api", msg_type="dependency", id=77, content="bump eslint to 10")
    result = await d.tasks_coord.spawn_handoff_task("web", msg)
    assert result is True  # different work → spawns


@pytest.mark.asyncio
async def test_suppression_disabled_restores_prefix_behavior(monkeypatch):
    d = make_daemon()
    d.config.drones.suppress_duplicate_handoff = False
    monkeypatch.setattr(d.tasks_coord, "assign_and_start_task", AsyncMock(return_value=True))
    monkeypatch.setattr(d, "edit_task", MagicMock())
    existing = d.task_board.create(title="Handoff from api: please fix the foo bug now")
    existing.source_worker = "api"
    d.task_board.assign(existing.id, "web")
    d.task_board.activate(existing.id)

    msg = SimpleNamespace(
        sender="api", msg_type="dependency", id=99, content="please fix the foo bug"
    )
    result = await d.tasks_coord.spawn_handoff_task("web", msg)
    assert result is True  # suppression off → spawns despite duplicate
