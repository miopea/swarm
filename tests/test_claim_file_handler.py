"""Tests for the ``swarm_claim_file`` MCP handler (advisory file lock).

Covers the concurrency-guard branches in ``_handle_claim_file``: fresh claim,
re-claim/renew by the same owner, a blocked claim while another worker holds a
live lock, and takeover after the lock's TTL expires.
"""

from __future__ import annotations

import os
import time

from swarm.mcp.handlers._files import _handle_claim_file
from tests.conftest import make_daemon

_PATH = "/home/user/projects/repo/src/shared/types.ts"
_RESOLVED = os.path.realpath(_PATH)


def _daemon(monkeypatch, *, ttl: float = 300.0):
    d = make_daemon(monkeypatch)
    d.file_locks = {}
    d._file_lock_ttl = ttl
    return d


def test_missing_path_is_rejected(monkeypatch):
    d = _daemon(monkeypatch)
    out = _handle_claim_file(d, "swarm", {})
    assert "Missing" in out[0]["text"]


def test_fresh_claim_succeeds_and_records_owner(monkeypatch):
    d = _daemon(monkeypatch)
    out = _handle_claim_file(d, "swarm", {"path": _PATH})
    assert "claimed:" in out[0]["text"].lower()
    owner, ts = d.file_locks[_RESOLVED]
    assert owner == "swarm"
    assert time.time() - ts < 1


def test_reclaim_by_same_owner_renews_timer(monkeypatch):
    d = _daemon(monkeypatch)
    d.file_locks[_RESOLVED] = ("swarm", time.time() - 100)
    out = _handle_claim_file(d, "swarm", {"path": _PATH})
    assert "claimed:" in out[0]["text"].lower()
    owner, ts = d.file_locks[_RESOLVED]
    assert owner == "swarm"
    assert time.time() - ts < 1  # timer renewed


def test_blocked_by_other_owner_within_ttl(monkeypatch):
    d = _daemon(monkeypatch, ttl=300.0)
    d.file_locks[_RESOLVED] = ("platform", time.time())
    out = _handle_claim_file(d, "swarm", {"path": _PATH})
    assert "claimed by platform" in out[0]["text"].lower()
    # The existing lock is untouched — no silent takeover.
    assert d.file_locks[_RESOLVED][0] == "platform"


def test_takeover_after_ttl_expiry(monkeypatch):
    d = _daemon(monkeypatch, ttl=60.0)
    d.file_locks[_RESOLVED] = ("platform", time.time() - 120)  # older than TTL
    out = _handle_claim_file(d, "swarm", {"path": _PATH})
    assert "claimed:" in out[0]["text"].lower()
    assert d.file_locks[_RESOLVED][0] == "swarm"  # taken over
