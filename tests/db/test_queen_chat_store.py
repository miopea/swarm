"""Tests for QueenChatStore learning management."""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm.db.core import SwarmDB
from swarm.db.queen_chat_store import QueenChatStore


@pytest.fixture
def store(tmp_path: Path) -> QueenChatStore:
    return QueenChatStore(SwarmDB(tmp_path / "q.db"))


class TestDeleteLearning:
    def test_delete_existing(self, store: QueenChatStore) -> None:
        learning = store.add_learning(context="ctx", correction="fix", applied_to="hub")
        assert store.delete_learning(learning.id) is True
        assert store.query_learnings(applied_to="hub") == []

    def test_delete_missing_returns_false(self, store: QueenChatStore) -> None:
        assert store.delete_learning(424242) is False

    def test_delete_leaves_others(self, store: QueenChatStore) -> None:
        keep = store.add_learning(context="keep", correction="k")
        drop = store.add_learning(context="drop", correction="d")
        store.delete_learning(drop.id)
        remaining = store.query_learnings()
        assert [learning.id for learning in remaining] == [keep.id]


class TestListThreadsFilters:
    def _thread_with_message(self, store, title, body, *, kind="operator", worker=None):
        t = store.create_thread(title=title, kind=kind, worker_name=worker)
        store.add_message(t.id, role="operator", content=body)
        return t

    def test_search_matches_title(self, store):
        self._thread_with_message(store, "Deploy auth fix", "unrelated body")
        self._thread_with_message(store, "Other", "nothing here")
        results = store.list_threads(search="auth")
        assert [t.title for t in results] == ["Deploy auth fix"]

    def test_search_matches_message_body(self, store):
        self._thread_with_message(store, "Generic title", "we discussed the redis migration")
        self._thread_with_message(store, "Another", "totally different")
        results = store.list_threads(search="redis")
        assert [t.title for t in results] == ["Generic title"]

    def test_search_returns_thread_once_even_with_multiple_matches(self, store):
        t = store.create_thread(title="auth thread", kind="operator")
        store.add_message(t.id, role="operator", content="auth one")
        store.add_message(t.id, role="queen", content="auth two")
        results = store.list_threads(search="auth")
        assert len(results) == 1

    def test_since_until_filter_on_updated_at(self, store):
        old = store.create_thread(title="old", kind="operator")
        store._db.update("queen_threads", {"updated_at": 1000.0}, "id = ?", (old.id,))
        new = store.create_thread(title="new", kind="operator")
        store._db.update("queen_threads", {"updated_at": 5000.0}, "id = ?", (new.id,))
        assert [t.title for t in store.list_threads(since=2000.0)] == ["new"]
        assert [t.title for t in store.list_threads(until=2000.0)] == ["old"]

    def test_offset_paginates(self, store):
        for i in range(5):
            store.create_thread(title=f"t{i}", kind="operator")
        page1 = store.list_threads(limit=2, offset=0)
        page2 = store.list_threads(limit=2, offset=2)
        assert len(page1) == 2 and len(page2) == 2
        assert {t.id for t in page1}.isdisjoint({t.id for t in page2})

    def test_filters_compose(self, store):
        self._thread_with_message(store, "hub auth", "x", kind="escalation", worker="hub")
        self._thread_with_message(store, "hub other", "x", kind="operator", worker="hub")
        results = store.list_threads(kind="escalation", worker_name="hub", search="auth")
        assert [t.title for t in results] == ["hub auth"]

    def test_no_params_reproduces_legacy(self, store):
        store.create_thread(title="a", kind="operator")
        store.create_thread(title="b", kind="operator")
        assert len(store.list_threads()) == 2


class TestMessageCounts:
    def test_counts_per_thread(self, store):
        t1 = store.create_thread(title="t1", kind="operator")
        t2 = store.create_thread(title="t2", kind="operator")
        store.add_message(t1.id, role="operator", content="a")
        store.add_message(t1.id, role="queen", content="b")
        store.add_message(t2.id, role="operator", content="c")
        counts = store.message_counts([t1.id, t2.id])
        assert counts == {t1.id: 2, t2.id: 1}

    def test_empty_list_returns_empty(self, store):
        assert store.message_counts([]) == {}

    def test_thread_with_no_messages_absent(self, store):
        t = store.create_thread(title="silent", kind="operator")
        assert store.message_counts([t.id]) == {}


class TestPurgeOldConfigurable:
    def test_purges_resolved_older_than_window(self, store):
        old = store.create_thread(title="old", kind="operator")
        store.resolve_thread(old.id, resolved_by="operator")
        store._db.update("queen_threads", {"resolved_at": 1000.0}, "id = ?", (old.id,))
        kept = store.create_thread(title="recent", kind="operator")
        store.resolve_thread(kept.id, resolved_by="operator")
        removed = store.purge_old(retention_days=1)
        assert removed == 1
        assert {t.title for t in store.list_threads()} == {"recent"}

    def test_active_threads_never_purged(self, store):
        active = store.create_thread(title="still open", kind="operator")
        store._db.update("queen_threads", {"updated_at": 1.0}, "id = ?", (active.id,))
        assert store.purge_old(retention_days=1) == 0
        assert len(store.list_threads()) == 1
