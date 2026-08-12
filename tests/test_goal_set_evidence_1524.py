"""#1524 — GOAL_SET must be usable as evidence of WHICH condition was seeded.

THE FAILURE THIS PREVENTS. GOAL_SET logged `condition[:120]`. The phrase that tells the
two goal variants apart sits around index 299 of a ~400-char condition, so the natural
check — `detail LIKE '%SATISFIES this goal%'` — COULD NOT MATCH EITHER VARIANT. It
returned "old condition" for old and new alike, and the CASE's ELSE branch then labelled
everything OLD by construction. That false negative was reported to the operator TWICE
before anyone noticed the pattern was unreachable. The Queen independently hit the same
wall with a '%stop after%' pattern and read it as a query bug.

So the tests that matter here are the DISCRIMINATION ones. A test asserting only
"metadata contains the flag" would pass against an implementation that always writes
true — which is exactly the class of check that caused the incident.
"""

from __future__ import annotations

import json

from swarm.drones.log import truncate_for_log
from swarm.server.messages import (
    condition_has_blocker_exit,
    render_goal_condition,
)

# A real pre-2026.8.11.4 condition: criteria plus a bare turn cap, no blocker exit.
_OLD_STYLE = (
    "All of these hold, each demonstrated in your own output: (1) it works; "
    "(2) there is a test. Stop after 25 turns and report what's blocking."
)


# ---------------------------------------------------------------------------
# AC3 — the negative control. This is the point of the ticket.
# ---------------------------------------------------------------------------


def test_detector_discriminates_old_from_new():
    """MUST fail on the old condition and pass on the new one.

    An always-true implementation cannot satisfy both halves. Without the first
    assertion this whole file would be decorative.
    """
    new = render_goal_condition(["it works", "there is a test"], max_turns=25)

    assert condition_has_blocker_exit(_OLD_STYLE) is False
    assert condition_has_blocker_exit(new) is True


def test_the_old_style_condition_is_genuinely_old_style():
    """Guards the fixture itself.

    If _OLD_STYLE accidentally contained the marker, the negative control above
    would be asserting nothing — the same "no positive control" mistake that
    produced the original false measurement.
    """
    assert "SATISFIES" not in _OLD_STYLE
    assert "stop after" in _OLD_STYLE.lower(), "should still carry a turn cap"


def test_flag_is_false_when_the_renderer_drops_the_exits(monkeypatch):
    """The budget<1 path returns head+criteria with NO exits.

    This is what makes the flag load-bearing rather than a restatement of "the
    renderer always appends exits": there is a real code path where it does not,
    and the flag reports that truthfully.

    Reached by shrinking _GOAL_MAX_LEN rather than by a huge criterion. With the
    real cap the budget is ~3693, so a big input takes the `enumerated[:budget] +
    exits` path and STILL carries the exits — a version of this test that just fed
    a long string would assert nothing and pass, which is the failure mode this
    whole ticket is about.

    100 rather than something smaller: the head is 57 chars, so a tighter cap eats
    the criteria as well and the test stops being about the exits.
    """
    monkeypatch.setattr("swarm.server.messages._GOAL_MAX_LEN", 100)

    starved = render_goal_condition(["it works"], max_turns=25)

    assert condition_has_blocker_exit(starved) is False
    assert "it works" in starved, "the criteria must survive; only the exits are dropped"


# ---------------------------------------------------------------------------
# AC1 / AC2 / AC5 — what a query can actually read off the row
# ---------------------------------------------------------------------------


def _metadata_for(condition: str) -> dict:
    """The metadata GOAL_SET stores, round-tripped through JSON as buzz_log does.

    Mirrors the real query path (`json_extract(metadata, '$.key')`) rather than
    reading a live dict, so a value that cannot survive serialization fails here.
    """
    import hashlib

    payload = {
        "task_id": "abc123",
        "task_number": 1524,
        "goal_has_blocker_exit": condition_has_blocker_exit(condition),
        "condition_sha": hashlib.sha256(condition.encode()).hexdigest()[:8],
        "condition_len": len(condition),
    }
    return json.loads(json.dumps(payload))


def test_a_query_can_tell_the_variants_apart_from_metadata_alone():
    """AC1 + AC2 — answered WITHOUT reading the condition text."""
    new = render_goal_condition(["it works"], max_turns=25)

    assert _metadata_for(new)["goal_has_blocker_exit"] is True
    assert _metadata_for(_OLD_STYLE)["goal_has_blocker_exit"] is False


def test_sha_identifies_the_variant_independently_of_wording():
    """Survives a future edit to the exits text, which a boolean would not.

    Same condition -> same sha; different condition -> different sha.
    """
    a = render_goal_condition(["it works"], max_turns=25)
    b = render_goal_condition(["it works"], max_turns=50)  # different turn cap

    assert _metadata_for(a)["condition_sha"] == _metadata_for(a)["condition_sha"]
    assert _metadata_for(a)["condition_sha"] != _metadata_for(b)["condition_sha"]


def test_metadata_is_fixed_size_regardless_of_condition_length():
    """AC5 — a 4000-char condition must not bloat every dispatch row."""
    long_condition = render_goal_condition(["y" * 3000], max_turns=25)
    assert len(long_condition) > 1000, "fixture must actually be long"

    encoded = json.dumps(_metadata_for(long_condition))

    assert len(encoded) < 200, f"metadata grew with the condition: {len(encoded)}"
    assert _metadata_for(long_condition)["condition_len"] == len(long_condition)


# ---------------------------------------------------------------------------
# AC4 — a cut value can never again look complete
# ---------------------------------------------------------------------------


def test_truncation_declares_itself_and_the_true_total():
    long_condition = render_goal_condition(["z" * 2000], max_turns=25)

    out = truncate_for_log(long_condition, 120)

    assert "truncated" in out
    assert str(len(long_condition)) in out, "the reader must learn how much is missing"
    assert len(out) < 200, "the marker must not reintroduce unbounded growth"


def test_a_complete_value_is_not_labelled_truncated():
    """CONTROL. A helper that always appended the marker would pass the test above
    while lying about every short value — and a false "truncated" claim sends the
    next reader hunting for data that was never cut."""
    short = "goal armed: (1) it works"

    assert truncate_for_log(short, 120) == short
    assert "truncated" not in truncate_for_log(short, 120)


def test_boundary_exactly_at_the_limit_is_not_truncated():
    exact = "q" * 120

    assert truncate_for_log(exact, 120) == exact
    assert truncate_for_log("q" * 121, 120) != "q" * 121


# ---------------------------------------------------------------------------
# The incident, encoded. Stops anyone going back to a detail LIKE check.
# ---------------------------------------------------------------------------


def test_the_old_truncated_detail_could_not_discriminate_either_way():
    """PROOF OF THE ORIGINAL BUG, and a guard against reintroducing it.

    Reconstructs the pre-#1524 detail and runs the exact check that was reported to
    the operator twice: `detail LIKE '%SATISFIES this goal%'`. It comes back False
    for BOTH variants — so the query could not have returned "new" for a new
    condition, and the ELSE branch labelled everything "old" by construction.

    If someone ever replaces the metadata flag with a detail substring check, this
    test tells them why it cannot work.
    """
    new = render_goal_condition(["it works", "there is a test"], max_turns=25)

    def old_detail(condition: str) -> str:
        return f"#1524 goal armed: {condition[:120]}"

    # The marker exists in the new condition — well past where the cut lands.
    assert "SATISFIES this goal" in new
    assert new.index("SATISFIES this goal") > 120

    # ...and is therefore absent from BOTH stored details. Indistinguishable.
    assert "SATISFIES this goal" not in old_detail(new)
    assert "SATISFIES this goal" not in old_detail(_OLD_STYLE)

    # The metadata check separates them where the detail check cannot.
    assert _metadata_for(new)["goal_has_blocker_exit"] is True
    assert _metadata_for(_OLD_STYLE)["goal_has_blocker_exit"] is False
