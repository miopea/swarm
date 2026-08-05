"""A background WebSocket event may draw attention; it may not take the viewport.

REPORTED 2026-08-05 with a screenshot: every Queen notification/approval opened
the bottom panel. The handler for ``proposal_created`` called
``switchTab('decisions')`` under a comment that read "Flash the Decisions badge
so users notice even if not on that tab" — the comment described the intent, the
code did something much larger.

``switchTab(tab)`` without ``restoring`` performs FOUR state changes:

    exitFocusMode();                                  // leaves focus mode
    expandBottomPanel();                              // opens a collapsed panel
    ...                                               // changes the active tab
    sessionStorage.setItem('swarm_bottom_tab', tab);  // and remembers it

So a proposal arriving while the operator was reading another tab moved them,
un-collapsed a panel they had deliberately collapsed, dropped them out of focus
mode, and persisted the new tab so a reload did not undo it. The Queen proposes
on her own schedule, so this could land at any moment, including mid-read.

Asserted against the SOURCE rather than behaviour because this project has no
JS test harness. That makes the check coarse but real: it pins the specific
call-shape that caused the defect, in the specific handler, and it fails if
someone reintroduces it there.

The distinction that matters is WHO initiated the event. ``switchTab`` is
correct and wanted from a click — opening a linked task, choosing a tab. It is
only wrong when the trigger came from the server.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_DASHBOARD_JS = Path(__file__).parent.parent / "src" / "swarm" / "web" / "static" / "dashboard.js"


@pytest.fixture(scope="module")
def js() -> str:
    return _DASHBOARD_JS.read_text()


def _ws_case_body(js: str, event: str) -> str:
    """The body of one ``case '<event>':`` arm in the WebSocket dispatch."""
    start = js.index(f"case '{event}':")
    end = js.index("break;", start)
    return js[start:end]


def test_proposal_created_does_not_switch_tabs(js):
    """THE regression guard for the reported defect."""
    body = _ws_case_body(js, "proposal_created")
    assert "flashDecisionsBadge()" in body, "the attention cue is gone entirely"
    # Match a CALL, not the word — the explanatory comment in this arm names
    # switchTab deliberately, and a bare substring check would forbid saying
    # why the call was removed.
    assert not re.search(r"^\s*switchTab\(", body, re.M), (
        "proposal_created calls switchTab() again — that expands the bottom "
        "panel, exits focus mode and persists the tab, which is the reported bug"
    )


@pytest.mark.parametrize(
    "event",
    ["proposals_changed", "queen_auto_acted", "queen_queue"],
)
def test_other_queen_events_do_not_switch_tabs_either(js, event):
    """The same rule for its siblings.

    Fixing only the reported arm would leave the next Queen event free to
    reintroduce the behaviour — these fire on the same background channel and
    the operator cannot tell which one moved them.
    """
    body = _ws_case_body(js, event)
    assert not re.search(r"^\s*switchTab\(", body, re.M), f"{event} hijacks the viewport"


def test_the_attention_cue_respects_reduced_motion(js):
    """WCAG 2.1 AA, and the count is the real signal anyway.

    The badge number updates regardless, so honouring the preference costs the
    operator no information — it drops only the flash.
    """
    start = js.index("function flashDecisionsBadge()")
    body = js[start : js.index("\n    }", start)]
    assert "prefers-reduced-motion" in body, "flash ignores prefers-reduced-motion"


def test_the_cue_cannot_break_the_websocket_handler(js):
    """It runs inside the WS dispatch; an exception there would kill the arm
    and lose the refresh/toast that carry the actual information."""
    start = js.index("function flashDecisionsBadge()")
    body = js[start : js.index("\n    }", start)]
    assert "try {" in body and "catch" in body


# --- worker state filter: multi-select ------------------------------------
#
# Reported alongside the notification defect: the state chips behaved as radio
# buttons, so "everything except Sleeping" — a normal thing to want on a board
# where most workers are asleep — could not be expressed at all. Only "one
# state" or "all of them" were reachable.


def test_state_filter_holds_a_set_not_a_single_value(js):
    """A scalar cannot represent two selected states, so the type IS the fix."""
    assert "activeWorkerStates = new Set()" in js
    assert "activeWorkerStateFilter" not in js, "the single-value filter survives somewhere"


def test_all_is_the_empty_set_rather_than_a_sentinel_member(js):
    """'all' must not be stored alongside real states.

    A sentinel mixed into the same set forces every read to special-case it,
    and the one place that forgets silently filters every worker out.
    """
    start = js.index("var activeWorkerStates")
    body = js[start : js.index("// Bulk worker actions", start)]
    assert "activeWorkerStates.clear()" in body, "'All' does not reset to the empty set"
    assert "activeWorkerStates.add('all')" not in body
    assert "!hasStateFilter || activeWorkerStates.has(state)" in body


def test_deselecting_the_last_chip_cannot_match_nothing(js):
    """Emptying the set must mean All, not "no worker matches"."""
    start = js.index("var chip = e.target.closest('[data-worker-state]')")
    body = js[start : js.index("});", start)]
    assert "activeWorkerStates.delete(state)" in body, "chips cannot be deselected"
    # The empty set is read as "no filter" by filterWorkers, so no explicit
    # fallback is needed — but the semantics must be the ones asserted above.
    assert "activeWorkerStates.add(state)" in body


def test_chips_expose_their_toggle_state_to_assistive_tech(js):
    """WCAG 2.1 AA. With multi-select, pressed state is the ONLY signal that
    more than one filter is live — a class name conveys nothing to AT."""
    assert "aria-pressed" in js
    markup = (
        Path(__file__).parent.parent / "src" / "swarm" / "web" / "templates" / "dashboard.html"
    ).read_text()
    filters = markup[markup.index('id="worker-state-filters"') :][:1200]
    assert 'role="group"' in markup[markup.index("worker-state-filters") - 200 :][:600]
    assert filters.count("aria-pressed") >= 6, "not every chip declares aria-pressed"
