"""#1510 — the two ownership maps answer different questions, and one guard never fired.

MEASURED BEFORE PROPOSING ANYTHING, and it inverts the ticket. FileOwnershipMap keys
REPO-RELATIVE paths (`git diff --name-only HEAD`); `file_locks` keys REALPATH ABSOLUTE
paths (`swarm_claim_file`, `_check_file_lock`). The same file recorded in both is invisible
to the other, so they can neither agree nor disagree — the disagreement the ticket looks for
cannot be constructed without first normalising paths.

That split is not an accident, which is why these are NOT unified here. Workers run in
separate worktrees: the ownership map predicts MERGE conflicts across them (logical path),
while file_locks prevents CONCURRENT WRITES to one file on disk (physical path). Unify on
realpath and the merge predictor stops relating the two checkouts; unify on relative and the
write lock starts blocking workers who share no file. Either choice breaks one guarantee.

What IS broken: `task_coordinator.check_ownership` compares a worker's own files against
itself, so it can never raise and never warn.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from swarm.coordination.ownership import FileOwnershipMap, OwnershipMode

# ---------------------------------------------------------------------------
# AC1 / AC2 — the key-space split, and the positive control
# ---------------------------------------------------------------------------


def test_the_two_maps_cannot_see_each_others_entries():
    """AC1. Same file, two different owners on record, and neither lookup resolves.

    Pinned so a future "tidy-up" that normalises one side fails loudly here rather than
    silently changing what blocks a worker mid-edit.
    """
    rel = "src/swarm/server/daemon.py"
    absolute = os.path.realpath(rel)

    ownership = FileOwnershipMap(mode=OwnershipMode.HARD_BLOCK)
    ownership.claim("platform", {rel})
    file_locks = {absolute: ("swarm", 0.0)}

    assert rel != absolute, "precondition: the two key formats differ"
    assert ownership.get_owner(absolute) is None
    assert file_locks.get(rel) is None


def test_the_maps_CAN_disagree_once_the_key_format_matches():
    """AC2 POSITIVE CONTROL, and it is what makes the finding above a measurement rather
    than an absence. If the maps simply could not represent a conflict, the empty result
    above would measure nothing."""
    rel = "src/swarm/server/daemon.py"
    ownership = FileOwnershipMap(mode=OwnershipMode.HARD_BLOCK)
    ownership.claim("platform", {rel})

    assert ownership.check_overlap("swarm", {os.path.realpath(rel)}) == []

    overlaps = ownership.check_overlap("swarm", {rel})
    assert [(o.owner, o.intruder) for o in overlaps] == [("platform", "swarm")]


# ---------------------------------------------------------------------------
# The guard that could never fire
# ---------------------------------------------------------------------------


def _map_with_overlap() -> FileOwnershipMap:
    """`a.py` is owned by platform; swarm then touches it. A real, recorded overlap."""
    m = FileOwnershipMap(mode=OwnershipMode.HARD_BLOCK)
    m.claim("platform", {"a.py", "b.py"})
    m.claim("swarm", {"a.py", "c.py"})
    return m


def test_a_workers_own_files_can_never_overlap_with_itself():
    """THE DEAD LOGIC, stated as the invariant it rests on.

    `get_worker_files(w)` returns files where `_owners[f] == w`; `check_overlap` reports f
    only when `_owners[f] != w`. Those are mutually exclusive as long as
    `_worker_files[w] == {f: _owners[f] == w}` — which `claim`, `release`, `release_file`
    and `transfer` all maintain. So the old `check_ownership` produced zero overlaps for
    every state the system can actually reach.

    PRECISION MATTERS HERE: it was not impossible to trigger, it was impossible to trigger
    HONESTLY. `tests/server/test_task_coordinator.py` reached the firing state by writing
    `_worker_files` directly, which `claim` never does — so the guard passed its tests
    against a configuration that cannot occur. That fixture is now built from two real
    claims.
    """
    m = _map_with_overlap()

    for worker in ("platform", "swarm"):
        assert m.check_overlap(worker, m.get_worker_files(worker)) == []

    # …while the conflict is genuinely there and recorded.
    assert m.overlaps_for("swarm"), "the overlap exists; only the old query could not see it"


def test_overlaps_for_names_the_intruder_not_the_owner():
    """Direction matters: platform OWNS the contested file, so platform is not the one
    to block. Getting this backwards would block the victim instead of the intruder."""
    m = _map_with_overlap()

    assert [(o.file_path, o.owner) for o in m.overlaps_for("swarm")] == [("a.py", "platform")]
    assert m.overlaps_for("platform") == []


def _coordinator(m: FileOwnershipMap) -> MagicMock:
    from swarm.server.task_coordinator import TaskCoordinator

    d = MagicMock()
    d.file_ownership = m
    tc = TaskCoordinator.__new__(TaskCoordinator)
    tc._d = d
    return tc


def test_hard_block_now_raises_on_a_real_overlap():
    """Fails on today's code, which returns silently."""
    from swarm.server.daemon import SwarmOperationError

    tc = _coordinator(_map_with_overlap())

    with pytest.raises(SwarmOperationError, match=r"a\.py"):
        tc.check_ownership("swarm")


def test_warning_mode_records_the_conflict_without_raising():
    m = _map_with_overlap()
    m.mode = OwnershipMode.WARNING
    tc = _coordinator(m)

    tc.check_ownership("swarm")  # must not raise

    details = [c.args[2] for c in tc._d.drone_log.add.call_args_list if len(c.args) >= 3]
    assert any("a.py" in str(t) for t in details), f"no ownership warning logged: {details}"


def test_a_worker_with_no_overlap_is_not_blocked():
    """POSITIVE CONTROL. Without it, a guard that blocked EVERY assignment would pass both
    tests above while halting the fleet — and it would look like the fix working."""
    tc = _coordinator(_map_with_overlap())

    tc.check_ownership("platform")  # owns its files outright
    tc.check_ownership("nobody-here")


def test_off_disables_the_guard_entirely():
    m = _map_with_overlap()
    m.mode = OwnershipMode.OFF
    tc = _coordinator(m)

    tc.check_ownership("swarm")
    tc._d.drone_log.add.assert_not_called()


# ---------------------------------------------------------------------------
# AC4 / AC5 — #1498's fixes stay fixed
# ---------------------------------------------------------------------------


def _hook_daemon(mode: OwnershipMode) -> MagicMock:
    d = MagicMock()
    d.file_ownership = FileOwnershipMap(mode=mode)
    d.file_locks = {}
    d._file_lock_ttl = 3600.0
    return d


def _worker(name: str) -> MagicMock:
    w = MagicMock()
    w.name = name
    return w


def test_warning_mode_does_not_block_an_edit():
    """#1498's REPORTED SYMPTOM: a hard block fired while the operator's config read
    'warning'. A mode nobody selected, enforced fleet-wide, read as a broken tool."""
    from swarm.server.routes.hooks import _check_file_lock

    d = _hook_daemon(OwnershipMode.WARNING)
    target = os.path.realpath("some_file.py")
    d.file_locks[target] = ("platform", 1e12)

    assert _check_file_lock(d, _worker("swarm"), "Edit", {"file_path": target}) is None
    assert d.file_locks[target][0] == "platform", "advisory mode must not steal the lock"


def test_hard_block_mode_still_blocks():
    """The other half of the same control — honouring the mode must not mean never blocking."""
    from swarm.server.routes.hooks import _check_file_lock

    d = _hook_daemon(OwnershipMode.HARD_BLOCK)
    target = os.path.realpath("some_file.py")
    d.file_locks[target] = ("platform", 1e12)

    resp = _check_file_lock(d, _worker("swarm"), "Edit", {"file_path": target})
    assert resp is not None


def test_unknown_identity_fails_open_and_writes_no_lock():
    """AC4. `_identify_worker` is a CWD heuristic; the old code turned its None into the
    literal name "unknown", which compared unequal to every real owner — so an
    unidentified worker was refused every claimed file AND dispossessed the real holder
    on the way past. A guard that cannot tell who is asking must not be the thing that
    says no."""
    from swarm.server.routes.hooks import _check_file_lock

    d = _hook_daemon(OwnershipMode.HARD_BLOCK)
    target = os.path.realpath("some_file.py")
    d.file_locks[target] = ("platform", 1e12)

    assert _check_file_lock(d, None, "Edit", {"file_path": target}) is None
    assert d.file_locks[target] == ("platform", 1e12), "the real holder was dispossessed"
    assert not any("unknown" == o for o, _ in d.file_locks.values())
