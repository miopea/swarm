"""Tests for the attention-queue route gather helpers.

These lock in the batched (non-N+1) behaviour: blockers fetched in one query,
buzz-log lookups batched once instead of per STUNG worker, and thread detail
using ``latest_message`` instead of pulling the whole thread.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from swarm.server.routes import attention
from swarm.worker.worker import QUEEN_WORKER_NAME


def _state(value: str) -> SimpleNamespace:
    return SimpleNamespace(value=value)


def _worker(name: str, state: str = "RESTING") -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        state=_state(state),
        state_duration=1.0,
        needs_operator_input=False,
        _revive_at=0.0,
        revive_grace=15.0,
    )


class _FakeBlockerStore:
    def __init__(self, blocked: set[str]) -> None:
        self._blocked = blocked
        self.calls = 0

    def active_worker_names(self) -> set[str]:
        self.calls += 1
        return set(self._blocked)


class _FakeBuzz:
    """Records query() calls so the test can assert batching (no per-worker filter)."""

    def __init__(self, rows_by_action: dict[str, list[dict[str, Any]]]) -> None:
        self._rows = rows_by_action
        self.queries: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.queries.append(kwargs)
        return list(self._rows.get(kwargs.get("action"), []))


class TestGatherBlocked:
    def test_single_query_intersected_with_present_workers(self) -> None:
        store = _FakeBlockerStore({"w1", "w3", "ghost"})
        d = SimpleNamespace(blocker_store=store, workers=[_worker("w1"), _worker("w2")])
        result = attention._gather_blocked(d)
        # Only workers that are both blocked AND currently present.
        assert result == {"w1"}
        # Exactly one query for the whole board (not one per worker).
        assert store.calls == 1

    def test_no_store(self) -> None:
        assert attention._gather_blocked(SimpleNamespace(blocker_store=None, workers=[])) == set()


class TestGatherWorkers:
    def test_stung_worker_gets_batched_revive_and_stung_detail(self) -> None:
        buzz = _FakeBuzz(
            {
                "REVIVED": [{"worker_name": "w1"}, {"worker_name": "w1"}],
                "WORKER_STUNG": [
                    {"worker_name": "w1", "detail": "newest crash"},
                    {"worker_name": "w1", "detail": "older crash"},
                ],
            }
        )
        d = SimpleNamespace(workers=[_worker("w1", "STUNG")], pilot=None)
        snaps = attention._gather_workers(d, buzz, now=1000.0)
        assert len(snaps) == 1
        assert snaps[0].revive_count == 2
        # query() is newest-first, so the FIRST WORKER_STUNG row wins.
        assert snaps[0].last_stung_detail == "newest crash"
        # Batched: two action-scoped queries, never a per-worker worker_name filter.
        assert len(buzz.queries) == 2
        assert all(q.get("worker_name") is None for q in buzz.queries)

    def test_non_stung_worker_has_no_revive_data(self) -> None:
        buzz = _FakeBuzz({"REVIVED": [{"worker_name": "w1"}]})
        d = SimpleNamespace(workers=[_worker("w1", "RESTING")], pilot=None)
        snaps = attention._gather_workers(d, buzz, now=1000.0)
        assert snaps[0].revive_count == 0
        assert snaps[0].last_stung_detail is None

    def test_queen_worker_excluded(self) -> None:
        d = SimpleNamespace(workers=[_worker(QUEEN_WORKER_NAME, "BUZZING")], pilot=None)
        assert attention._gather_workers(d, buzz=None, now=1000.0) == []


class TestGatherThreads:
    def test_detail_kind_uses_latest_message(self) -> None:
        thread = SimpleNamespace(
            id="t1",
            kind="oversight",
            title="needs review",
            worker_name="w1",
            task_id=None,
            created_at=1.0,
            updated_at=2.0,
        )

        class _Chat:
            def __init__(self) -> None:
                self.latest_calls = 0

            def list_threads(self, *, status: str, limit: int) -> list[Any]:
                return [thread]

            def latest_message(self, thread_id: str) -> Any:
                self.latest_calls += 1
                return SimpleNamespace(content="the latest line")

        chat = _Chat()
        snaps = attention._gather_threads(chat, limit=100)
        assert len(snaps) == 1
        assert snaps[0].latest_message == "the latest line"
        assert chat.latest_calls == 1

    def test_operator_kind_skipped(self) -> None:
        thread = SimpleNamespace(
            id="t1",
            kind="operator",
            title="",
            worker_name="w1",
            task_id=None,
            created_at=1.0,
            updated_at=2.0,
        )

        class _Chat:
            def list_threads(self, *, status: str, limit: int) -> list[Any]:
                return [thread]

        assert attention._gather_threads(_Chat(), limit=100) == []

    def test_no_chat(self) -> None:
        assert attention._gather_threads(None, limit=100) == []
