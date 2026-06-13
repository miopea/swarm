"""Tests for notify/digest.py — daily digest rendering."""

from __future__ import annotations

from swarm.notify.digest import build_digest


def _summary(**overrides):
    base = {
        "window_days": 1,
        "created": 5,
        "completed": 3,
        "failed": 1,
        "avg_completion_seconds": 1800.0,
        "workers": [
            {"worker": "alice", "completed": 2, "failed": 0},
            {"worker": "bob", "completed": 1, "failed": 1},
        ],
        "backlog": {"assigned": 2, "active": 1, "done": 10},
    }
    base.update(overrides)
    return base


class TestBuildDigest:
    def test_title_carries_counts(self):
        title, _ = build_digest(_summary())
        assert "3 done" in title
        assert "1 failed" in title

    def test_message_mentions_workers_and_backlog(self):
        _, message = build_digest(_summary())
        assert "alice (2)" in message
        assert "Open tasks on the board: 3." in message
        assert "30m" in message

    def test_empty_board(self):
        title, message = build_digest(
            _summary(
                created=0,
                completed=0,
                failed=0,
                avg_completion_seconds=None,
                workers=[],
                backlog={},
            )
        )
        assert "0 done" in title
        assert "n/a" in message
