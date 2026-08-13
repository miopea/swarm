"""#1580 — an overlap stops being reported when it is RESOLVED, not when a timer says so.

#1510 gave `overlaps_for` to `TaskCoordinator.check_ownership`, so that guard can now
actually block an assignment. It reads a capped history with no expiry, meaning a conflict
settled hours ago still blocks. The operator ruled the fix must be SEMANTIC rather than a
TTL: resolved at the moment it is resolved. A window nobody remembers setting is exactly
the constant that later gets misread as a bug (#1571).

MEASURED FIRST, AND IT MOVED THE PRIORITY. The ticket asked whether `claim()` should dedupe,
"worth measuring before assuming":
  · 5 poll cycles on ONE persistent conflict -> 5 identical records, reported 5 times.
  · With the 100-cap, 120 cycles of a noisy conflict EVICTED a genuine unrelated one
    entirely — so `overlaps_for` MISSED a real conflict. The guard silently under-fires.
Dedupe is therefore the correctness half, not a tidy-up.
"""

from __future__ import annotations

import logging

from swarm.coordination.ownership import FileOwnershipMap, OwnershipMode


def _map() -> FileOwnershipMap:
    """`platform` owns a.py; `swarm` touches it. One real, recorded overlap."""
    logging.disable(logging.CRITICAL)
    m = FileOwnershipMap(mode=OwnershipMode.HARD_BLOCK)
    m.claim("platform", {"a.py"})
    m.claim("swarm", {"a.py"})
    assert m.overlaps_for("swarm"), "precondition: the overlap is recorded"
    return m


def _with_unrelated(m: FileOwnershipMap) -> FileOwnershipMap:
    """A SECOND, untouched conflict: hub owns b.py, nexus touches it."""
    m.claim("hub", {"b.py"})
    m.claim("nexus", {"b.py"})
    assert m.overlaps_for("nexus"), "precondition: the unrelated overlap is recorded"
    return m


# ---------------------------------------------------------------------------
# AC1 — one per mutator
# ---------------------------------------------------------------------------


def test_release_file_resolves_the_overlap_on_that_path():
    m = _map()

    m.release_file("a.py")

    assert m.overlaps_for("swarm") == []


def test_release_resolves_overlaps_on_the_files_that_worker_owned():
    m = _map()

    m.release("platform")  # platform owned the contested a.py

    assert m.overlaps_for("swarm") == []


def test_transfer_resolves_the_overlap_on_that_path():
    """The record names an `owner` who no longer owns the file, so it no longer describes
    the current state — true whether or not the intruder is the new owner."""
    m = _map()

    m.transfer("a.py", "swarm")

    assert m.overlaps_for("swarm") == []


# ---------------------------------------------------------------------------
# AC2 — a positive control for each, and they are not optional
# ---------------------------------------------------------------------------


def test_release_file_leaves_an_unrelated_overlap_alone():
    """Without these three, a fix that simply emptied `_overlaps` passes every AC1 test
    while disabling the guard entirely — and looks identical to working."""
    m = _with_unrelated(_map())

    m.release_file("a.py")

    assert len(m.overlaps_for("nexus")) == 1


def test_release_leaves_an_unrelated_overlap_alone():
    m = _with_unrelated(_map())

    m.release("platform")

    assert len(m.overlaps_for("nexus")) == 1


def test_transfer_leaves_an_unrelated_overlap_alone():
    m = _with_unrelated(_map())

    m.transfer("a.py", "swarm")

    assert len(m.overlaps_for("nexus")) == 1


def test_release_does_not_resolve_overlaps_where_the_worker_was_the_INTRUDER():
    """DIRECTION, pinned so a later simplification cannot quietly widen it.

    `swarm` intruded on platform's a.py. Releasing swarm's OWN files says nothing about
    that: the contested file belongs to somebody else, and swarm giving up unrelated
    files does not settle it. Widening this would let any worker clear its own record by
    releasing something irrelevant.
    """
    m = _map()
    m.claim("swarm", {"c.py"})  # a file swarm genuinely owns

    m.release("swarm")

    assert len(m.overlaps_for("swarm")) == 1, "the intrusion on platform's file still stands"


# ---------------------------------------------------------------------------
# The measurements that made dedupe mandatory
# ---------------------------------------------------------------------------


def test_a_persistent_conflict_is_recorded_once_not_once_per_poll():
    """`update_from_conflicts` runs every ownership poll, and `claim()` appended each
    time. Fails today with 5."""
    logging.disable(logging.CRITICAL)
    m = FileOwnershipMap(mode=OwnershipMode.WARNING)
    m.claim("platform", {"a.py"})

    for _ in range(5):
        m.update_from_conflicts({"swarm": {"a.py"}})

    assert len(m.overlaps_for("swarm")) == 1


def test_a_noisy_conflict_cannot_evict_a_live_unrelated_one():
    """THE REGRESSION THAT MATTERS MOST — replayed from the measurement.

    120 poll cycles of one conflict pushed a genuine unrelated overlap out of the capped
    history entirely, so `overlaps_for` reported nothing for it. A guard that silently
    under-fires looks exactly like a guard with nothing to do.
    """
    logging.disable(logging.CRITICAL)
    m = FileOwnershipMap(mode=OwnershipMode.WARNING)
    m.claim("platform", {"a.py"})
    m.claim("hub", {"b.py"})
    m.update_from_conflicts({"nexus": {"b.py"}})

    for _ in range(120):
        m.update_from_conflicts({"swarm": {"a.py"}})

    assert m.overlaps_for("nexus"), "a live conflict was evicted by duplicates of another"


def test_a_resolved_overlap_re_arms_if_the_conflict_recurs():
    """Resolution is not permanent forgiveness. If the same worker touches the same
    owned file again, it is a live conflict once more — otherwise one release would
    immunise an intruder against that file forever."""
    m = _map()
    m.release_file("a.py")
    assert m.overlaps_for("swarm") == []

    m.claim("platform", {"a.py"})
    m.claim("swarm", {"a.py"})

    assert len(m.overlaps_for("swarm")) == 1


# ---------------------------------------------------------------------------
# AC3 — the decision, asserted rather than described
# ---------------------------------------------------------------------------


def test_to_dict_keeps_resolved_overlaps_and_flags_them():
    """DELIBERATE: display history and enforcement state are different things. The
    operator benefits from "these two collided and it is settled"; enforcement must see
    only what is still true. One structure with a flag, not two lists that can drift —
    which is exactly what #1510 found wrong with the two ownership maps.
    """
    m = _map()
    m.release_file("a.py")

    shown = m.to_dict()["recent_overlaps"]

    assert len(shown) == 1, "the operator-facing history lost a resolved overlap"
    assert shown[0]["resolved"] is True
    assert m.overlaps_for("swarm") == [], "…but enforcement must not see it"


# ---------------------------------------------------------------------------
# AC4 — no TTL, pinned against a future "small" one
# ---------------------------------------------------------------------------


def test_no_time_comparison_gates_overlap_reporting():
    """Asserted against the BYTECODE's referenced names, because a behavioural test
    cannot prove the absence of a TTL — a 24h window would pass every test above. The
    operator ruled this out explicitly: a duration picked by feel is #1571's failure, and
    a window nobody remembers setting later gets misread as a bug.

    NOT a source-text scan: my first version grepped `inspect.getsource`, which includes
    the docstring, so it tripped on the prose EXPLAINING that there is no TTL. A check
    that fails on its own justification is worse than no check — it would have been
    "fixed" by deleting the explanation. `co_names` sees only real references.
    """
    referenced = set(FileOwnershipMap.overlaps_for.__code__.co_names)

    for banned in ("time", "monotonic", "timestamp"):
        assert banned not in referenced, (
            f"overlaps_for now references {banned!r} — a time-based gate was added, which "
            f"#1580 ruled out in favour of semantic resolution."
        )


def test_the_cap_evicts_resolved_records_before_live_ones():
    """The eviction ORDER, tested directly rather than trusted.

    Dedupe bounds the history by distinct conflicts, but a fleet with many settled
    conflicts could still push a live one out at the cap — measurement #2 in a new
    costume. Resolved records must go first.
    """
    logging.disable(logging.CRITICAL)
    m = FileOwnershipMap(mode=OwnershipMode.WARNING)

    # 150 distinct conflicts, all settled.
    for i in range(150):
        m.claim(f"owner{i}", {f"f{i}.py"})
        m.claim("intruder", {f"f{i}.py"})
        m.release_file(f"f{i}.py")

    # One live conflict, recorded last.
    m.claim("hub", {"live.py"})
    m.claim("nexus", {"live.py"})

    assert m.overlaps_for("nexus"), "a live conflict was evicted in favour of resolved ones"
    assert len(m._overlaps) <= m._OVERLAP_CAP, "the cap stopped being enforced"
