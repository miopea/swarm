"""#1679 — the holder-drift check reported clean while the running code was stale.

MEASURED 2026-08-15, and it is why a #1671 verification nearly passed on a false signal:

    holder.py in the source tree     sha256 77724ec8...  (contained the new code)
    /api/holder/drift → holder_hash  sha256 a6d47db2...
    /api/holder/drift → daemon_hash  sha256 a6d47db2...
    drift: False

Both reported hashes were identical TO EACH OTHER and neither matched the file on disk.
A worker spawned after the change was checked directly — `/proc/<pid>/environ` showed the
new variables were absent — so the direct observation was right and the indicator wrong.

ROOT CAUSE: `_check_holder_version` runs ONLY on connect (pool.py, from `_try_connect`),
and `handle_holder_drift` serves the stored dict. So the answer is a SNAPSHOT taken when
the holder last connected, and it goes stale the moment `holder.py` is edited — which is
precisely the moment the operator needs it. A cached comparison against a file that has
since changed cannot detect that the file changed.

The holder's own hash is legitimately fixed for its lifetime (it is an import-time value,
which is the whole point of the check). The DAEMON side is not: it is a file on disk that
can change under a running process, so it has to be re-read when asked.

This is the day's recurring shape — a control whose healthy state and whose
measuring-nothing state are indistinguishable from outside.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from swarm.pty.pool import ProcessPool


def _pool_with_cached_drift(holder_hash: str, cached_daemon_hash: str) -> ProcessPool:
    """A pool carrying a connect-time snapshot, as the real one does."""
    pool = ProcessPool.__new__(ProcessPool)
    pool.socket_path = Path("/tmp/holder.sock")
    pool.holder_drift = {
        "checked": True,
        "drift": holder_hash != cached_daemon_hash,
        "holder_hash": holder_hash,
        "daemon_hash": cached_daemon_hash,
        "holder_pid": 4242,
        "unknown": False,
    }
    return pool


def _disk_hash() -> str:
    from swarm.pty import holder as holder_mod

    return hashlib.sha256(Path(holder_mod.__file__).resolve().read_bytes()).hexdigest()


def test_the_live_check_catches_drift_the_cached_snapshot_missed():
    """THE REPORTED DEFECT, reproduced. The snapshot says holder and daemon agreed at
    connect time; the file has since changed, so the running holder is now stale and the
    cached answer cannot see it."""
    stale = "a6d47db2" + "0" * 56  # what both sides hashed to at connect
    pool = _pool_with_cached_drift(holder_hash=stale, cached_daemon_hash=stale)

    assert pool.holder_drift["drift"] is False, "precondition: the cached answer says clean"

    live = pool.live_holder_drift()

    assert live["drift"] is True, "the live check must see the holder is behind the file"
    assert live["daemon_hash"] == _disk_hash(), "daemon side must be re-read from disk"
    assert live["holder_hash"] == stale, "the holder's import-time hash is legitimately fixed"


def test_a_genuinely_current_holder_still_reports_clean():
    """POSITIVE CONTROL. A check that reported drift unconditionally would pass the test
    above while making the signal useless — and an always-red indicator gets ignored just
    as fast as an always-green one."""
    pool = _pool_with_cached_drift(holder_hash=_disk_hash(), cached_daemon_hash="whatever")

    live = pool.live_holder_drift()

    assert live["drift"] is False
    assert live["unknown"] is False


def test_an_unknown_holder_hash_does_not_assert_drift():
    """FAIL-SAFE DIRECTION, preserved from the original. An old holder that does not know
    the `version` command, or a failed probe, means WE COULD NOT TELL — which must not be
    reported as drift. 'Cannot tell' and 'is stale' are different answers and collapsing
    them is the same defect one level up."""
    pool = _pool_with_cached_drift(holder_hash="", cached_daemon_hash="")

    live = pool.live_holder_drift()

    assert live["drift"] is False
    assert live["unknown"] is True


def test_the_source_path_is_reported_so_the_reader_can_see_WHICH_file():
    """The incident was partly a two-installations problem — the daemon may run from an
    installed copy rather than the tree the operator edits. Naming the file that was
    hashed is what lets a reader notice they are looking at a different one."""
    pool = _pool_with_cached_drift(holder_hash="x" * 64, cached_daemon_hash="x" * 64)

    live = pool.live_holder_drift()

    assert live["source_path"].endswith("holder.py")
    assert Path(live["source_path"]).is_absolute()


def test_the_cached_snapshot_is_left_intact():
    """The live answer must not overwrite the connect-time record. That snapshot is
    evidence of what was true when the holder attached, and the loud warning logged at
    connect refers to it."""
    stale = "b" * 64
    pool = _pool_with_cached_drift(holder_hash=stale, cached_daemon_hash=stale)

    pool.live_holder_drift()

    assert pool.holder_drift["daemon_hash"] == stale
    assert pool.holder_drift["drift"] is False
