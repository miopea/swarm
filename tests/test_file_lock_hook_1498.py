"""The Edit/Write file-lock hook: honour the configured mode, fail open, audit.

Reported by platform-api, 2026-08-11, deterministically reproducible: claim a
file with ``swarm_claim_file``, then Edit that same file as that same worker,
and the edit is refused with "File locked by worker platform-api" — the holder
apparently refused its own claim.

The comparison was never inverted. ``lock_owner != worker_name`` is correct.
The defect is that ``worker`` is None whenever ``_identify_worker``'s CWD
heuristic cannot match a worker path, and the old code substituted the literal
string ``"unknown"``, which compares unequal to EVERY real owner. So an
unidentified worker was refused every claimed file, and the message named the
legitimate holder — which reads exactly like the holder being refused itself.

Two consequences made it worse than a false negative:
  * The worker who politely claims first is the only one whose file is locked,
    so claiming was strictly harmful — the inverse of the intended incentive.
  * On the way past, ``"unknown"`` was written into the lock table, taking the
    file from whoever actually held it.

And it hard-blocked regardless of ``coordination.file_ownership``, which the
operator has set to "warning" (advisory). A mode nobody selected was enforced
fleet-wide, mid-session, against briefs that correctly said claims were advisory.
"""

from __future__ import annotations

import time
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from swarm.coordination.ownership import OwnershipMode
from swarm.server.routes.hooks import _check_file_lock


def _daemon(mode: OwnershipMode = OwnershipMode.WARNING, locks: dict | None = None) -> MagicMock:
    d = MagicMock()
    d.file_locks = locks if locks is not None else {}
    d.file_ownership = types.SimpleNamespace(mode=mode)
    d._file_lock_ttl = 60.0
    d.drone_log = MagicMock()
    return d


def _worker(name: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(name=name)


def _edit(path: str) -> dict[str, Any]:
    return {"file_path": path}


# ------------------------------------------------- the reported reproduction


def test_the_claim_holder_may_edit_its_own_claimed_file(tmp_path) -> None:
    """platform-api's exact repro: claim, then edit, same worker, same session."""
    f = tmp_path / "fundraising-api.dto.ts"
    f.write_text("x")
    import os

    d = _daemon(locks={os.path.realpath(str(f)): ("platform-api", time.time())})

    assert _check_file_lock(d, _worker("platform-api"), "Edit", _edit(str(f))) is None


def test_unknown_identity_fails_open_and_does_not_steal_the_lock(tmp_path) -> None:
    """The actual defect. None became "unknown", which != every real owner.

    Both assertions matter. Allowing is the fix for the refusal; not writing the
    lock is the fix for the dispossession that made claiming harmful.
    """
    f = tmp_path / "a.ts"
    f.write_text("x")
    import os

    resolved = os.path.realpath(str(f))
    d = _daemon(mode=OwnershipMode.HARD_BLOCK, locks={resolved: ("platform-api", time.time())})

    assert _check_file_lock(d, None, "Edit", _edit(str(f))) is None, "must not refuse"
    assert d.file_locks[resolved][0] == "platform-api", "must not take the claim"


# --------------------------------------------------------- the configured mode


def test_warning_mode_does_not_block_another_workers_file(tmp_path) -> None:
    """The operator's live setting. Advisory means advisory."""
    f = tmp_path / "b.ts"
    f.write_text("x")
    import os

    d = _daemon(
        mode=OwnershipMode.WARNING,
        locks={os.path.realpath(str(f)): ("admin", time.time())},
    )
    assert _check_file_lock(d, _worker("platform-api"), "Edit", _edit(str(f))) is None


def test_warning_mode_leaves_the_holders_claim_intact(tmp_path) -> None:
    """Passing through must not silently re-own the file, or the next edit by the
    real holder would be the one refused."""
    f = tmp_path / "c.ts"
    f.write_text("x")
    import os

    resolved = os.path.realpath(str(f))
    d = _daemon(mode=OwnershipMode.WARNING, locks={resolved: ("admin", time.time())})
    _check_file_lock(d, _worker("platform-api"), "Edit", _edit(str(f)))
    assert d.file_locks[resolved][0] == "admin"


def test_hard_block_mode_still_blocks_and_names_both_parties(tmp_path) -> None:
    """The direction that must keep working — project-root demonstrated it against
    the live ownership map before enforcement landed."""
    f = tmp_path / "d.ts"
    f.write_text("x")
    import os

    d = _daemon(
        mode=OwnershipMode.HARD_BLOCK,
        locks={os.path.realpath(str(f)): ("admin", time.time())},
    )
    resp = _check_file_lock(d, _worker("platform-api"), "Edit", _edit(str(f)))
    assert resp is not None
    body = resp.text or ""
    assert "admin" in body, "the block must name the owner"
    assert "platform-api" in body, "and who it thinks is asking, or it reads as self-refusal"


def test_off_mode_never_blocks_and_records_nothing(tmp_path) -> None:
    f = tmp_path / "e.ts"
    f.write_text("x")
    import os

    resolved = os.path.realpath(str(f))
    d = _daemon(mode=OwnershipMode.OFF, locks={})
    assert _check_file_lock(d, _worker("platform-api"), "Edit", _edit(str(f))) is None
    assert resolved not in d.file_locks, "OFF must not maintain a lock table"


# ---------------------------------------------------------------- audit trail


@pytest.mark.parametrize("mode", [OwnershipMode.HARD_BLOCK, OwnershipMode.WARNING])
def test_every_conflict_reaches_the_drone_log(tmp_path, mode: OwnershipMode) -> None:
    """A denial with no record cannot be diagnosed from outside the worker.

    The old code logged at INFO while the daemon runs at log_level=WARNING, so
    the refusal reached no destination at all, and it was the only decision on
    this route that skipped _log_hook_decision.
    """
    f = tmp_path / "f.ts"
    f.write_text("x")
    import os

    d = _daemon(mode=mode, locks={os.path.realpath(str(f)): ("admin", time.time())})
    _check_file_lock(d, _worker("platform-api"), "Edit", _edit(str(f)))
    assert d.drone_log.add.called, "a conflict must leave a durable record"


def test_an_expired_lock_is_not_a_conflict(tmp_path) -> None:
    f = tmp_path / "g.ts"
    f.write_text("x")
    import os

    resolved = os.path.realpath(str(f))
    d = _daemon(mode=OwnershipMode.HARD_BLOCK, locks={resolved: ("admin", time.time() - 3600)})
    assert _check_file_lock(d, _worker("platform-api"), "Edit", _edit(str(f))) is None
    assert d.file_locks[resolved][0] == "platform-api", "an expired lock is reclaimable"


def test_non_write_tools_are_untouched() -> None:
    d = _daemon(mode=OwnershipMode.HARD_BLOCK)
    assert _check_file_lock(d, _worker("w"), "Read", _edit("/tmp/x")) is None
    assert _check_file_lock(d, _worker("w"), "Bash", {"command": "ls"}) is None
