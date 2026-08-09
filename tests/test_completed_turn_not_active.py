"""A completed-turn summary above an idle prompt is not work in progress (#1357).

THE OPERATOR REPORT. After every daemon reload the whole fleet showed BUZZING — all
sixteen workers, identically. Persisting worker state across the restart (2026.8.9.21)
did not fix it, and the reason turned out to be that the restore was never the problem:
a diagnostic showed workers coming back correctly as ``was=RESTING`` and then being
reclassified ``decided=BUZZING`` by the pilot's first poll, seconds later.

WHAT WAS ACTUALLY WRONG. ``_RE_SUBAGENT_ACTIVE`` treats ``<glyph> <verb> for <digits>``
as evidence of an active turn. That shape has two opposite meanings:

    mid-turn   "✻ Sautéed for 16m 13s"   still working
    turn over  "✻ Brewed for 1m 58s"     Claude Code's COMPLETION summary

The second is what sits above the returned input box on an idle worker — so on any
buffer where a turn had recently finished, the elapsed-time line short-circuited
``classify_output`` to BUZZING before the RESTING branch was reachable.

Both meanings are real, so the pattern was split rather than narrowed. Where a prompt
is already visible and there is no "esc to interrupt", the turn has ended and elapsed
time is history: those sites use ``_RE_SUBAGENT_IN_PROGRESS`` (live spinner ellipsis or
subagent token counter). The stuck-BUZZING safety net keeps the broad pattern, because
there a false positive merely keeps a busy worker BUZZING — the safe direction, and the
whole point of the ``for 16m 13s`` capture that put it there.

WHY THE FIXTURES ARE REAL. These are verbatim first-poll PTY buffers from four live
workers, captured by a temporary diagnostic during an actual reload. Hand-written
samples are what let this survive: the ambiguity only shows up in a buffer that has a
finished turn AND a returned prompt AND no interrupt hint at once, which is exactly the
combination nobody writes by hand. Ground truth is the state map the daemon had
persisted moments earlier, independently of any classification made here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm.providers import get_provider
from swarm.providers.claude import _RE_SUBAGENT_ACTIVE, _RE_SUBAGENT_IN_PROGRESS
from swarm.worker.worker import WorkerState

_FIXTURES = Path(__file__).parent / "fixtures" / "first_poll"

# name -> the state the daemon had persisted for that worker just before the restart.
_GROUND_TRUTH = {
    "admin": WorkerState.RESTING,
    "queen": WorkerState.RESTING,
    "rcg-networks": WorkerState.RESTING,
    # Genuinely working, and its buffer carries a live indicator rather than a
    # completed-turn summary. Present so this is not a one-sided fixture set: a change
    # that simply stopped returning BUZZING would pass the other three and fail here.
    "project-root": WorkerState.BUZZING,
}


def _buffer(name: str) -> str:
    return (_FIXTURES / f"{name}.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("name,expected", sorted(_GROUND_TRUTH.items()))
def test_first_poll_matches_the_state_the_daemon_had_persisted(
    name: str, expected: WorkerState
) -> None:
    """THE REPRODUCTION. Before the fix every one of these returned BUZZING."""
    actual = get_provider("claude").classify_output("claude", _buffer(name))
    assert actual == expected, (
        f"{name}: classified {actual.value}, but the daemon had persisted "
        f"{expected.value} moments earlier — the fleet reads all-BUZZING after a reload"
    )


def test_the_fixtures_actually_contain_the_ambiguous_shape() -> None:
    """A POSITIVE CONTROL on the fixtures themselves.

    If Claude Code stops printing the completed-turn summary, or the fixtures are ever
    regenerated from idle-from-boot workers, the parametrised test above would keep
    passing while testing nothing at all. Assert the trap is still in the data: the
    broad pattern must match these buffers, and the narrow one must not.
    """
    for name in ("admin", "queen", "rcg-networks"):
        tail = get_provider("claude")._get_tail(_buffer(name), 30)
        assert _RE_SUBAGENT_ACTIVE.search(tail), (
            f"{name} no longer contains a completed-turn elapsed summary — this fixture "
            "no longer reproduces #1357 and needs regenerating"
        )
        assert not _RE_SUBAGENT_IN_PROGRESS.search(tail), (
            f"{name} contains a live-progress indicator, so RESTING is not obviously "
            "the right answer for it"
        )


def test_the_two_meanings_are_distinguished_not_merged() -> None:
    """The elapsed-time shape must stay evidence for the safety net.

    This is the #236 capture: the platform worker was 16 minutes into a background task
    with no interrupt hint on screen, and dropping the elapsed branch outright would
    flip it to RESTING. Both patterns exist precisely so this stays true.
    """
    mid_turn = "some line\n  ✻ Sautéed for 16m 13s\n❯\n"
    assert _RE_SUBAGENT_ACTIVE.search(mid_turn), (
        "the safety net lost its only signal for a long turn with no spinner on screen"
    )
    assert not _RE_SUBAGENT_IN_PROGRESS.search(mid_turn)


def test_a_live_spinner_still_reads_as_working() -> None:
    """The narrow pattern must not have narrowed to nothing."""
    for live in ("✻ Verifying… (5s)\n❯\n", "· Cooking...\n❯\n", "↓ 1.2k tokens\n❯\n"):
        assert _RE_SUBAGENT_IN_PROGRESS.search(live), live
