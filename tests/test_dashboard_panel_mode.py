"""Popping the tasks/decisions panel into its own window (#1353).

OPERATOR REQUEST: "pop-out the task/decision area of the UI so I could run these as a
separate window, especially now that task management is handled there."

IT IS THE SAME PAGE, not a second one. A standalone template would need its own copy of
the task renderer, the socket wiring and every element id — and two renderers for one
panel drift, with the server-rendered one winning on load. That already happened to the
Jira mappings panel. Reusing the page means one renderer, one reconciler, and every
action handler works unchanged.

THE COST THAT HAD TO BE DESIGNED AROUND: window.open COPIES the opener's sessionStorage,
and the dashboard restores the previously-selected worker from it on load — which mounts
an xterm and attaches a SECOND PTY subscription for a terminal nobody can see. Terminal
traffic is the heaviest thing this daemon moves.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_JS = Path("src/swarm/web/static/dashboard.js").read_text()
_HTML = Path("src/swarm/web/templates/dashboard.html").read_text()
_BASE = Path("src/swarm/web/templates/base.html").read_text()


def _js_without_comments() -> str:
    """Whole-line // and /* comments dropped.

    Scans in this repo have matched the PROSE EXPLAINING a change rather than the change
    five separate times. The comments added here name both `sessionStorage` and
    `panel-mode`.
    """
    out = []
    for line in _JS.split("\n"):
        st = line.lstrip()
        if st.startswith(("//", "/*", "*/", "* ")):
            continue
        out.append(line)
    return "\n".join(out)


# --- the control exists and is wired ------------------------------------------


def test_the_pop_out_control_is_rendered_and_registered():
    """A data-action with no handler is a button that does nothing — silently."""
    assert 'data-action="popOutTasks"' in _HTML
    assert re.search(r"^\s+popOutTasks: function", _js_without_comments(), re.M), (
        "the pop-out button has no handler in the action registry"
    )


def test_it_opens_the_panel_url_in_a_named_window():
    """NAMED, so pressing it twice focuses the existing window instead of opening a
    second copy that would hold its own socket."""
    code = _js_without_comments()
    fn = code[code.index("window.popOutTasks = function") :][:600]
    assert "'/?panel=tasks'" in fn
    assert "'swarm-tasks'" in fn, "an unnamed window opens a duplicate on every click"


def test_the_control_hides_itself_inside_the_popped_window():
    """Offering "pop out" inside the popped-out window is a loop with no meaning."""
    assert 'body.panel-mode [data-action="popOutTasks"]' in _BASE


# --- the expensive mistake ------------------------------------------------------


def test_panel_mode_does_NOT_restore_a_worker():
    """THE PROPERTY WORTH THE MOST. window.open copies sessionStorage, so without this
    the popped window restores the opener's selected worker, mounts an xterm, and
    attaches a second PTY subscription for a terminal that is not even visible."""
    code = _js_without_comments()
    # EVERY read of the stored worker must sit behind a panel-mode check. There are two:
    # one at the top of the IIFE that runs before init, and one in init itself. The first
    # version of this test scanned only around the FIRST occurrence in the file and so
    # checked the wrong one.
    reads = [m.start() for m in re.finditer(r"getItem\('swarm_selected_worker'\)", code)]
    assert reads, "the scan found no restore at all; it is checking nothing"
    for idx in reads:
        # A TIGHT window — 160 chars. The first version used 500 and passed with the
        # guard replaced by `if (true)`, because that window also swept up the
        # DECLARATION of _panelMode a few lines above. Proximity of a name is not a
        # guard; the branch has to be the thing immediately enclosing the read.
        window = code[max(0, idx - 160) : idx]
        assert "!_panelMode" in window or "} else {" in window, (
            f"a read of the stored worker is not gated on panel mode; the popped window "
            f"will attach a second PTY for an invisible terminal. Context: {window[-90:]!r}"
        )


def test_panel_mode_is_detected_from_the_query_string():
    code = _js_without_comments()
    fn = code[code.index("function isPanelMode") :][:400]
    assert "panel" in fn and "tasks" in fn


# --- what the popped window shows ----------------------------------------------


@pytest.mark.parametrize(
    "hidden",
    [".worker-list", ".detail-area > .panel:not(.bottom-tabbed)", "#bottom-panel-fab"],
)
def test_panel_mode_hides_everything_but_the_panel(hidden: str):
    assert f"body.panel-mode {hidden}" in _BASE, f"{hidden} is still shown in the popped window"


def test_it_does_not_hide_the_container_the_panel_lives_in():
    """FOUND BY OPENING THE PAGE — it rendered completely blank.

    .bottom-tabbed is a CHILD of .detail-area, so hiding the detail area hides the panel
    this mode exists to show. Every source scan and browser test passed anyway, because
    they all asserted what should be ABSENT and none asserted the panel was PRESENT.
    """
    rules = _BASE[_BASE.index("body.panel-mode") : _BASE.index("body.panel-mode") + 1400]
    assert "body.panel-mode .detail-area," not in rules, (
        "the detail area is hidden wholesale, which hides the tasks panel inside it"
    )


def test_the_panel_itself_is_given_the_whole_window():
    """Otherwise it keeps the height it had as a bottom strip and the popped window is
    mostly empty."""
    assert "body.panel-mode .bottom-tabbed" in _BASE
    rule = _BASE[_BASE.index("body.panel-mode .bottom-tabbed") :][:220]
    assert "flex: 1" in rule, "the panel does not expand to fill the window"


def test_the_tasks_and_decisions_panels_are_the_ones_kept():
    """Both, not just tasks — the operator named the decisions surface specifically,
    and it is where promotion approvals land."""
    assert 'id="tab-tasks"' in _HTML
    assert 'id="tab-decisions"' in _HTML


# --- it inherits the live-update machinery rather than reimplementing it --------


def test_it_reuses_the_existing_renderer_and_reconciler():
    """The whole reason for reusing the page. If a second renderer ever appears, the two
    drift and the stale one wins on load."""
    code = _js_without_comments()
    assert code.count("function reconcileTaskView") == 1, (
        "a second task reconciler exists; the popped window and the dashboard will drift"
    )
    assert "startTaskReconciler" in code, "the popped window has no reconciliation path"


def test_no_second_template_was_added():
    """A standalone panel template is the thing this design avoids."""
    templates = {p.name for p in Path("src/swarm/web/templates").glob("*.html")}
    for forbidden in ("panel.html", "tasks_panel.html", "popout.html"):
        assert forbidden not in templates, f"{forbidden} duplicates the dashboard renderer"


def test_the_js_still_parses_as_a_single_iife():
    """Cheap structural check: an unbalanced edit to a 13k-line file fails everything at
    once, and the browser tests are the only other thing that would notice."""
    assert _JS.count("(function()") >= 1
    assert _JS.rstrip().endswith("})();")


# --- The blank pop-out, and why the first fix could not have worked -----------------

# Anchored WITH the brace: `body.panel-mode .detail-area > .panel:not(...)` appears
# first and a loose anchor sliced that rule instead, which is how the first run of
# these tests went red against a correct stylesheet.
_PANEL_RULES = "body.panel-mode .detail-area {"


def _base_css() -> str:
    return Path("src/swarm/web/templates/base.html").read_text(encoding="utf-8")


def test_panel_mode_does_not_try_to_lay_out_a_grid_with_flex_direction():
    """THE DEFECT. `.detail-area` is `display: grid`, so the original

        body.panel-mode .detail-area { flex-direction: column; }

    was inert — it parsed, applied, and did nothing. The panel kept the terminal's
    grid tracks. Measured live at 1100x800: rows `415.86px 0px 233.94px`, i.e. ~230px
    of the window given to an empty track, and at shorter heights the track the panel
    lands in collapses and the window renders blank.

    Asserting the property (a grid is sized with grid rules) rather than the string,
    so a future `flex-flow`/`align-items` reintroduction is caught too.
    """
    css = _base_css()
    i = css.index(_PANEL_RULES)
    block = css[i : css.index("}", i)]
    for flex_only in ("flex-direction", "flex-flow"):
        assert flex_only not in block, (
            f"{flex_only} in a rule targeting .detail-area, which is display:grid — "
            "this is the inert declaration that left the popped-out window blank"
        )


def test_panel_mode_collapses_the_detail_area_to_a_single_cell():
    """THE FIX, stated as the property that matters: one row, one column, so there is
    no second track to strand the panel in and none of the window is wasted."""
    css = _base_css()
    i = css.index(_PANEL_RULES)
    block = css[i : css.index("}", i)]
    assert "grid-template-rows: 1fr" in block, block
    assert "grid-template-columns: 1fr" in block, block


def test_the_panel_is_placed_in_that_cell():
    """A single-cell grid still strands the panel if nothing puts it there — the
    hidden terminal is display:none, but the resize handle is not."""
    css = _base_css()
    i = css.index("body.panel-mode .bottom-tabbed {")
    block = css[i : css.index("}", i)]
    assert "grid-row: 1" in block and "grid-column: 1" in block, block


def test_the_pop_out_icon_sits_with_the_collapse_caret():
    """Operator: "the popout button should be next to minimize. The alignment is all
    off." `.btn-collapse` carries `margin-left: auto`, so a sibling placed before it
    lands at the end of the LEFT group with the whole header's slack between them.
    The icon has to take the auto margin for the two to end up adjacent."""
    css = _base_css()
    i = css.index(".btn.btn-icon {")
    block = css[i : css.index("}", i)]
    assert "margin-left: auto" in block, (
        "without the auto margin the icon stays with '+ New Task' and the caret is "
        "pushed to the far right — the gap the operator screenshotted"
    )

    markup = Path("src/swarm/web/templates/dashboard.html").read_text(encoding="utf-8")
    j = markup.index('data-action="popOutTasks"')
    k = markup.index('data-action="toggleBottomPanel"', j)
    # up to the caret's own opening tag — not to its data-action, which sits INSIDE
    # that tag and would count the caret itself as an intruder.
    between = markup[
        markup.index("</button>", j) + len("</button>") : markup.rindex("<button", j, k)
    ]
    assert "<button" not in between, (
        f"something was inserted between the pop-out icon and the caret: {between!r}"
    )


def test_popping_out_collapses_the_panel_in_the_main_window():
    """Operator: "if I Popout the task/decision panel the one in the main window should
    minimize." The whole point is to move the panel off this window, so leaving a second
    live copy behind means two renderers competing for the same screen space.

    NOT persisted (#1360): writing the preference made closing the pop-out leave the
    panel minimized forever, which silently redefined what the caret does.
    """
    js = Path("src/swarm/web/static/dashboard.js").read_text(encoding="utf-8")
    i = js.index("window.popOutTasks = function")
    body = js[i : js.index("\n    };", i)]
    # persist=FALSE (#1360). Passing true overwrote the operator's own stored collapse
    # preference: the panel stayed collapsed across reloads, closing the pop-out never
    # restored it, and a click labelled "pop out" silently became "minimize forever".
    assert "window.setBottomCollapsed(true, false)" in body, body


def test_a_blocked_popup_leaves_the_main_panel_alone():
    """If window.open returns null the panel was never moved anywhere — collapsing it
    then would hide the only copy the operator has."""
    js = Path("src/swarm/web/static/dashboard.js").read_text(encoding="utf-8")
    i = js.index("window.popOutTasks = function")
    body = js[i : js.index("\n    };", i)]
    guard = body.index("if (!w) return")
    assert guard < body.index("window.setBottomCollapsed"), (
        "the collapse runs even when the popup was blocked"
    )


def test_the_popped_out_window_ignores_the_stored_collapse_preference():
    """THE BLANK POP-OUT, root cause — reported twice, and the second time reproducibly.

    The collapse preference lives in localStorage, which the popped-out window SHARES
    with the main one. So a panel minimized in the main window opened the pop-out
    already collapsed: a bare header and nothing else until a tab was clicked. Making
    the pop-out collapse the main window (the operator's own request) turned that from
    intermittent into every time.

    In that window the panel IS the window, so the preference does not apply. Asserted
    on initBottomPanel because that is where a stored value would otherwise win.
    """
    js = Path("src/swarm/web/static/dashboard.js").read_text(encoding="utf-8")
    i = js.index("function initBottomPanel()")
    body = js[i : js.index("})();", i)]
    forced = body.index("inPanelWindow()")
    assert "setBottomCollapsed(false, false)" in body, body
    assert forced < body.index("readBottomCollapsed()"), (
        "the stored collapse preference is read before the panel-window override, so a "
        "minimized main window still opens the pop-out blank"
    )


def test_the_popped_out_window_never_writes_the_collapse_preference():
    """The other direction. Both windows share localStorage, so persisting from the
    pop-out would silently redefine what the caret means in the main window."""
    js = Path("src/swarm/web/static/dashboard.js").read_text(encoding="utf-8")
    i = js.index("function setBottomCollapsed(")
    body = js[i : js.index("\n    }\n", i)]
    assert "if (inPanelWindow()) persist = false;" in body, body
    assert body.index("inPanelWindow()") < body.index("if (persist)"), (
        "persistence is decided before the panel-window guard runs"
    )


def test_the_icon_button_matches_its_siblings_metrics():
    """#1359 email 4 — the one I wrongly recorded as "nothing actionable".

    That email arrived with a dashboard link and no body, so I filled the gap with an
    assumption instead of asking. The operator later said what it meant: "that was not
    vertically aligned in mobile like the other buttons".

    The cause is measurable rather than aesthetic. .btn-icon used font-size 0.8rem with
    line-height 1.2 while every sibling is .btn-sm at 0.75rem, so its box was taller and
    it sat off their baseline in the header row — invisible on desktop, where the row has
    slack, and obvious on mobile where it does not.
    """
    css = Path("src/swarm/web/templates/base.html").read_text(encoding="utf-8")

    sm = re.search(r"\.btn-sm \{([^}]*)\}", css)
    assert sm, ".btn-sm is gone; this comparison no longer means anything"
    sm_font = re.search(r"font-size:\s*([\d.]+rem)", sm.group(1)).group(1)

    i = css.index(".btn.btn-icon {")
    icon = css[i : css.index("}", i)]
    icon_font = re.search(r"font-size:\s*([\d.]+rem)", icon).group(1)

    assert icon_font == sm_font, (
        f"the icon button is {icon_font} against its siblings' {sm_font}; a different "
        "box height puts it off their baseline"
    )
    assert "align-items: center" in icon, (
        "the glyph sits on a text baseline rather than centred in its box, which reads "
        "as misaligned even when the heights match"
    )
