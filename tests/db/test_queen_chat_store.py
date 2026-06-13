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
