"""The Queen's card must show what she is doing (operator report, 2026-08-10).

THE REPORT, with a screenshot: "Right now she stands out in the UI, but there is no way
to easily see she is working, waiting on input, etc."

Correct. Every worker row carries a state badge with a duration ("BUZZING — 1m") and,
when relevant, an "Awaiting your input" pill. The Queen's card rendered only her name and
the static subtitle "operator command center" — she was the one entry in the sidebar that
never said what it was doing.

She DID have a coloured border keyed to state (.queen-state-buzzing / -waiting / -stung),
which is readable once you know the code, invisible if you do not, and answers nothing
about HOW LONG she has been that way. The data was already on the card's dict —
``state``, ``state_duration`` and ``needs_operator_input`` all come from
``_queen_dict`` (web/app.py) — so this was a rendering gap, not a plumbing one.

THE DISTINCTION THAT MATTERS: "thinking" and "blocked on you" are not the same, and only
one of them is the operator's problem. The state word alone does not separate them, which
is why the needs-input pill is asserted separately from the badge.
"""

from __future__ import annotations

from pathlib import Path

_CARD = Path("src/swarm/web/templates/partials/queen_card.html").read_text(encoding="utf-8")
_BASE = Path("src/swarm/web/templates/base.html").read_text(encoding="utf-8")
_APP = Path("src/swarm/web/app.py").read_text(encoding="utf-8")


def test_the_card_shows_her_state():
    assert "queen.state" in _CARD, "the Queen's card still never says what she is doing"


def test_it_shows_how_long_she_has_been_in_it():
    """A state with no duration cannot tell 'deciding' from 'stuck since this morning'."""
    assert "queen.state_duration" in _CARD, "no duration — a stuck Queen looks like a busy one"


def test_waiting_on_the_operator_is_called_out_separately():
    """The state word does not distinguish thinking from blocked-on-you."""
    assert "needs_operator_input" in _CARD
    assert "needs-input-pill" in _CARD, (
        "no explicit 'awaiting your input' marker; the operator has to infer it from a "
        "state word that does not mean that"
    )


def test_the_data_was_already_available():
    """A POSITIVE CONTROL on the diagnosis: this was a rendering gap, not plumbing. If
    _queen_dict ever stops supplying these, the template silently renders blanks."""
    assert "state_duration" in _APP, "_queen_dict no longer supplies the duration"
    assert "to_api_dict()" in _APP, "the Queen dict no longer carries the worker fields"


def test_the_offline_case_still_renders():
    """The card must not go blank when she is not running — the sidebar slot has to stay
    stable, and the operator needs the affordance to spawn her."""
    assert "offline" in _CARD.lower(), "the offline placeholder was lost"


def test_the_state_word_is_coloured_consistently_with_workers():
    """Same palette as state_color() in worker_list.html. The macros are not in scope in
    this partial, so the mapping is inlined — and an inlined copy is exactly the kind of
    thing that drifts, hence this check."""
    for state, colour in (("BUZZING", "leaf"), ("WAITING", "honey"), ("STUNG", "poppy")):
        assert state in _CARD and colour in _CARD, f"{state} is not mapped to {colour}"
    assert ".queen-state" in _BASE, "the state span has no styling"
