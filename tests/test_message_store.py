"""Regression tests for :class:`swarm.messages.store.MessageStore`.

Primary motivation (2026-04-19): the wildcard broadcast bug —
``send(sender, "*", ...)`` wrote a single row with ``recipient='*'``
and the read path used ``WHERE recipient='*' OR recipient=<worker>``,
which meant the first worker to call ``get_unread()`` marked the
one shared row read.  Every subsequent worker saw nothing.

These tests pin down the new semantics:
- A true broadcast fans out to one row per recipient so read-state
  is tracked independently.
- ``send()`` still supports literal ``recipient='*'`` but delegates
  internally to ``broadcast()`` when the store is given a roster.
- ``broadcast()`` returns ``(fanout_count, ids)`` so callers can
  report recipient count to the operator.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm.messages.store import MessageStore


@pytest.fixture
def store(tmp_path: Path) -> MessageStore:
    return MessageStore(db_path=tmp_path / "msgs.db")


class TestBroadcast:
    def test_broadcast_inserts_one_row_per_recipient(self, store: MessageStore) -> None:
        ids = store.broadcast("queen", ["hub", "platform", "admin"], "warning", "heads up")
        assert len(ids) == 3
        # Each worker sees the broadcast in their inbox
        assert len(store.get_unread("hub")) == 1
        assert len(store.get_unread("platform")) == 1
        assert len(store.get_unread("admin")) == 1

    def test_broadcast_independent_read_state(self, store: MessageStore) -> None:
        """THE BUG: reader A must not steal reader B's copy."""
        store.broadcast("queen", ["hub", "platform"], "finding", "shared")
        # Hub reads + marks read
        hub_msgs = store.get_unread("hub")
        assert len(hub_msgs) == 1
        store.mark_read("hub", [m.id for m in hub_msgs])
        # Platform must still see its copy
        platform_msgs = store.get_unread("platform")
        assert len(platform_msgs) == 1, (
            "wildcard broadcast was claimed by hub — platform saw nothing"
        )

    def test_broadcast_excludes_sender(self, store: MessageStore) -> None:
        """Broadcasting from a worker should not put the message in its own inbox."""
        ids = store.broadcast("hub", ["hub", "platform", "admin"], "finding", "my own note")
        # Only 2 rows — hub skipped itself
        assert len(ids) == 2
        assert store.get_unread("hub") == []
        assert len(store.get_unread("platform")) == 1
        assert len(store.get_unread("admin")) == 1

    def test_broadcast_empty_recipients_returns_no_ids(self, store: MessageStore) -> None:
        ids = store.broadcast("queen", [], "finding", "nobody home")
        assert ids == []

    def test_broadcast_deduplicates_within_window(self, store: MessageStore) -> None:
        """Send → same (sender, recipient, type) within 60s → merged, not double-written."""
        first = store.broadcast("queen", ["hub"], "warning", "first")
        second = store.broadcast("queen", ["hub"], "warning", "updated")
        # Dedup collapses to the same row id
        assert first == second
        msgs = store.get_unread("hub")
        assert len(msgs) == 1
        assert msgs[0].content == "updated"


class TestMcpWildcardHandler:
    """End-to-end: ``swarm_send_message`` with ``to="*"`` must fan out via
    MessageStore.broadcast() and report the recipient count back to the
    caller."""

    def test_wildcard_fans_out_and_reports_count(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock

        from swarm.mcp.tools import handle_tool_call
        from swarm.messages.store import MessageStore

        store = MessageStore(db_path=tmp_path / "m.db")
        d = MagicMock()
        d.message_store = store
        d.drone_log = MagicMock()

        # Three workers; sender is hub, so wildcard should hit platform + admin
        class _W:
            def __init__(self, name: str) -> None:
                self.name = name

        d.config.workers = [_W("hub"), _W("platform"), _W("admin")]
        d.workers = [_W("hub"), _W("platform"), _W("admin")]

        result = handle_tool_call(
            d,
            "hub",
            "swarm_send_message",
            {"to": "*", "type": "warning", "content": "stop"},
        )
        text = result[0]["text"]
        assert "Broadcast sent to 2 worker(s)" in text
        assert "platform" in text and "admin" in text
        # Verify state: each non-sender has it in their inbox
        assert len(store.get_unread("platform")) == 1
        assert len(store.get_unread("admin")) == 1
        assert store.get_unread("hub") == []

    def test_wildcard_with_no_other_workers_reports_gracefully(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock

        from swarm.mcp.tools import handle_tool_call
        from swarm.messages.store import MessageStore

        store = MessageStore(db_path=tmp_path / "m.db")
        d = MagicMock()
        d.message_store = store
        d.drone_log = MagicMock()

        class _W:
            def __init__(self, name: str) -> None:
                self.name = name

        d.workers = [_W("hub")]  # only sender
        d.config.workers = [_W("hub")]

        result = handle_tool_call(
            d,
            "hub",
            "swarm_send_message",
            {"to": "*", "type": "finding", "content": "alone"},
        )
        assert "No other workers" in result[0]["text"]

    def test_wildcard_fans_out_to_offline_workers(self, tmp_path: Path) -> None:
        """Regression (2026-04-19): a `*` broadcast must reach every
        *registered* worker, not just the ones with a currently-running
        Claude process. The previous handler iterated ``d.workers`` (live
        Worker objects), so any worker that wasn't running at send time was
        silently skipped — the operator saw ``Broadcast sent`` but the
        quiet workers never got the row. Messages persist in SQLite, so
        offline workers pick them up via ``get_unread`` when they restart.
        """
        from unittest.mock import MagicMock

        from swarm.mcp.tools import handle_tool_call
        from swarm.messages.store import MessageStore

        store = MessageStore(db_path=tmp_path / "m.db")
        d = MagicMock()
        d.message_store = store
        d.drone_log = MagicMock()

        class _W:
            def __init__(self, name: str) -> None:
                self.name = name

        # hub + admin are live; platform + realtruth are registered but
        # their PTYs aren't running right now.
        d.config.workers = [_W("hub"), _W("admin"), _W("platform"), _W("realtruth")]
        d.workers = [_W("hub"), _W("admin")]

        result = handle_tool_call(
            d,
            "hub",
            "swarm_send_message",
            {"to": "*", "type": "finding", "content": "heads up"},
        )
        text = result[0]["text"]
        # All three non-sender workers (incl. the two offline) must be in the fanout.
        assert "Broadcast sent to 3 worker(s)" in text
        for name in ("admin", "platform", "realtruth"):
            assert name in text, f"{name} missing from broadcast: {text}"
        # Each registered non-sender has its own inbox row.
        assert len(store.get_unread("admin")) == 1
        assert len(store.get_unread("platform")) == 1
        assert len(store.get_unread("realtruth")) == 1
        assert store.get_unread("hub") == []


class TestSendWildcardStillWorks:
    """The existing handler calls ``send(..., "*", ...)``. That contract must
    still function — either by routing to broadcast internally (when a roster
    is provided) or by keeping the single-row fallback (when it isn't).
    Handlers now call ``broadcast()`` directly with the worker list; this
    class documents that literal ``send("*", ...)`` is still accepted for
    callers that never knew about the broadcast path (e.g. historical
    integration tests, external scripts)."""

    def test_literal_star_send_preserved_for_legacy_callers(self, store: MessageStore) -> None:
        msg_id = store.send("queen", "*", "finding", "legacy broadcast")
        assert msg_id is not None
        # Every worker calling get_unread() gets a hit via the wildcard filter.
        # NOTE: read-state is NOT independent in this legacy path — that's the
        # precise reason handlers switched to broadcast().  The legacy path
        # still works; it just suffers the first-reader-wins behavior.
        assert len(store.get_unread("any_worker")) == 1


# ---------------------------------------------------------------------------
# Task #529 Bug B verification: get_unread is recipient-only (NOT
# sender-leaky). rcg-networks's escalation note (msg #1175) raised the
# secondary theory "watcher counts my outbound swarm_send_message as new
# inbox traffic, resetting the pause-debounce." DB investigation falsified
# the theory: the SQL `WHERE recipient = ? OR recipient = '*'` cannot
# match a row where the worker is the sender. This regression test pins
# the semantics so a future refactor that touches the WHERE clause can't
# silently flip it.
# ---------------------------------------------------------------------------


class TestGetUnreadInboundOnly:
    """``get_unread(worker)`` must only return rows where the worker is the
    RECIPIENT (or the row is a legacy broadcast '*'). Outbound messages
    (where the worker is the SENDER) MUST NOT appear."""

    def test_outbound_messages_excluded(self, store: MessageStore) -> None:
        # rcg-networks sends an outbound message to platform.
        out_id = store.send("rcg-networks", "platform", "status", "FYI on #523")
        assert out_id is not None
        # platform sends an inbound message to rcg-networks.
        in_id = store.send("platform", "rcg-networks", "dependency", "spec amend")
        assert in_id is not None

        # get_unread for rcg-networks returns ONLY the inbound row.
        unread = store.get_unread("rcg-networks")
        assert len(unread) == 1
        assert unread[0].id == in_id
        assert unread[0].sender == "platform"
        assert unread[0].recipient == "rcg-networks"

        # Conversely, platform's get_unread returns the outbound rcg-networks
        # message (because platform is the recipient there).
        unread_p = store.get_unread("platform")
        assert any(m.id == out_id and m.sender == "rcg-networks" for m in unread_p)


class TestPrune:
    """prune() enforces message retention — the only retention path for the
    messages table (wired into daemon.start() so it actually runs)."""

    def test_deletes_old_keeps_recent(self, store: MessageStore) -> None:
        import time

        fresh_id = store.send("hub", "api", "finding", "fresh")
        old_id = store.send("hub", "platform", "finding", "old")
        assert fresh_id is not None and old_id is not None
        # Backdate the second message beyond the retention window AND mark it
        # read — prune now spares *unread* messages regardless of age (B10),
        # so a prunable message must be both old and consumed.
        store.mark_read("platform", [old_id])
        with store._lock:
            store._conn.execute(
                "UPDATE messages SET created_at = ? WHERE id = ?",
                (time.time() - 30 * 86400, old_id),
            )
            store._conn.commit()

        deleted = store.prune(max_age_days=7)
        assert deleted == 1
        remaining = {m.id for m in store.get_recent()}
        assert fresh_id in remaining
        assert old_id not in remaining

    def test_no_old_messages_is_noop(self, store: MessageStore) -> None:
        store.send("hub", "api", "finding", "recent")
        assert store.prune(max_age_days=7) == 0


class TestDelete:
    def test_delete_by_ids(self, store: MessageStore) -> None:
        # Distinct msg_type per send — same-type sends within 60s dedup-merge.
        id1 = store.send("a", "b", "finding", "one")
        id2 = store.send("a", "b", "warning", "two")
        deleted = store.delete([id1])
        assert deleted == 1
        remaining = store.get_recent()
        assert [m.id for m in remaining] == [id2]

    def test_delete_missing_ids_is_zero(self, store: MessageStore) -> None:
        assert store.delete([99999]) == 0

    def test_delete_empty_list_is_zero(self, store: MessageStore) -> None:
        store.send("a", "b", "finding", "keep me")
        assert store.delete([]) == 0
        assert len(store.get_recent()) == 1


def _set_created(store, msg_id, ts):
    store._conn.execute("UPDATE messages SET created_at=? WHERE id=?", (ts, msg_id))
    store._conn.commit()


class TestGetRecentFilters:
    def test_search_content(self, store):
        store.send("a", "b", "finding", "the redis migration is done")
        store.send("a", "b", "warning", "unrelated note")
        rows = store.get_recent(search="redis")
        assert [m.content for m in rows] == ["the redis migration is done"]

    def test_unread_only(self, store):
        m1 = store.send("a", "b", "finding", "one")
        store.send("a", "b", "warning", "two")
        store.mark_read("b", [m1])
        rows = store.get_recent(unread_only=True)
        assert [m.content for m in rows] == ["two"]

    def test_since_until_on_created_at(self, store):
        old = store.send("a", "b", "finding", "old")
        _set_created(store, old, 1000.0)
        new = store.send("a", "b", "warning", "new")
        _set_created(store, new, 5000.0)
        assert [m.content for m in store.get_recent(since=2000.0)] == ["new"]
        assert [m.content for m in store.get_recent(until=2000.0)] == ["old"]

    def test_offset_paginates(self, store):
        # Distinct types so the 60s same-type dedup doesn't merge rows.
        for t in ("finding", "warning", "dependency", "status", "operator"):
            store.send("a", "b", t, "m-" + t)
        p1 = store.get_recent(limit=2, offset=0)
        p2 = store.get_recent(limit=2, offset=2)
        assert len(p1) == 2 and len(p2) == 2
        assert {m.id for m in p1}.isdisjoint({m.id for m in p2})

    def test_no_params_legacy(self, store):
        store.send("a", "b", "finding", "x")
        store.send("a", "b", "warning", "y")
        assert len(store.get_recent()) == 2

    def test_combined(self, store):
        m1 = store.send("a", "b", "finding", "redis one")
        store.send("a", "b", "warning", "redis two")
        store.mark_read("b", [m1])
        rows = store.get_recent(search="redis", unread_only=True)
        assert [m.content for m in rows] == ["redis two"]


class TestPruneReadOnly:
    def test_default_spares_unread_regardless_of_age(self, store):
        unread = store.send("a", "b", "finding", "never read")
        _set_created(store, unread, 1000.0)  # ancient
        read = store.send("a", "b", "warning", "was read")
        _set_created(store, read, 1000.0)
        store.mark_read("b", [read])
        removed = store.prune(max_age_days=30)
        assert removed == 1  # only the read one
        remaining = [m.content for m in store.get_recent()]
        assert remaining == ["never read"]

    def test_read_only_false_is_legacy_deletes_all_old(self, store):
        unread = store.send("a", "b", "finding", "never read")
        _set_created(store, unread, 1000.0)
        read = store.send("a", "b", "warning", "was read")
        _set_created(store, read, 1000.0)
        store.mark_read("b", [read])
        removed = store.prune(max_age_days=30, read_only=False)
        assert removed == 2
        assert store.get_recent() == []

    def test_respects_window(self, store):
        old = store.send("a", "b", "finding", "old")
        _set_created(store, old, 1000.0)
        store.mark_read("b", [old])
        recent = store.send("a", "b", "warning", "recent")
        store.mark_read("b", [recent])
        removed = store.prune(max_age_days=30)
        assert removed == 1
        assert [m.content for m in store.get_recent()] == ["recent"]
