"""The mobile worker selector carries state visually, and survives a re-render (#1359).

THE REPORT, email 3 of four: "Hard to scan because it all looks the same", and on being
asked which surface: "Worker list — which need me. Because we're using the drop down
default for HTML, there's no formatting. I think maybe a custom selector will give a lot
more freedom on design."

That is a correct diagnosis of a structural limit, not a styling oversight. A native
``<option>`` can hold TEXT AND NOTHING ELSE — no icon, no colour, no second line — so
sixteen workers rendered as sixteen identical grey strings, "BUZZING · name (claude)".
No CSS fixes that; the control has to change.

WHAT THIS FILE GUARDS. Not the appearance — a stylesheet test that asserts a hex value
tells you nothing about whether anything is scannable. It guards the two properties that
would silently break and that a screenshot would not reveal:

1. STATE IS CARRIED NON-TEXTUALLY. Colour and icon per row, which is the entire point of
   replacing the select. A refactor that flattens rows back to text passes every layout
   test and reintroduces the exact complaint.
2. THE CONTROL SURVIVES ITS OWN PARTIAL BEING REPLACED. worker_list.html is re-rendered
   wholesale on every workers_changed swap — which is every few seconds across sixteen
   workers. Anything bound to the control, or any state stored on it, is destroyed. The
   old <select> only needed a delegated change listener; a listbox also has OPEN state,
   and losing that means the list slams shut while the operator is reading it.

Plus accessibility, because this replaces a native control that was accessible for free
and the app targets WCAG 2.1 AA. A custom listbox that cannot be operated by keyboard is
a regression the native <select> did not have.
"""

from __future__ import annotations

import re
from pathlib import Path

_WEB = Path("src/swarm/web")
_PARTIAL = (_WEB / "templates" / "partials" / "worker_list.html").read_text(encoding="utf-8")
_BASE = (_WEB / "templates" / "base.html").read_text(encoding="utf-8")
_JS = (_WEB / "static" / "dashboard.js").read_text(encoding="utf-8")


def _js_function(name: str) -> str:
    """A whole JS function body, bounded by its closing brace.

    NOT a fixed character window. Two assertions in this file went red the moment
    wselChoose grew, because each sliced a guessed number of characters and the code it
    was looking for fell off the end — a false failure that reads exactly like a real
    regression. Same mistake, twice, in the same session.
    """
    i = _JS.index("function " + name)
    return _JS[i : _JS.index("\n    }\n", i)]


def _option_block() -> str:
    """The listbox markup only — comments stripped.

    Stripped because the comments in that file discuss colour, icons and filtering at
    length; matching my own prose rather than the template is a mistake this codebase
    has made repeatedly.
    """
    code = re.sub(r"\{#.*?#\}", "", _PARTIAL, flags=re.S)
    return code[code.index('role="listbox"') : code.index("</ul>")]


# --- 1. state is visual, which is the whole reason the control changed --------------


def test_each_row_carries_state_as_colour_and_icon_not_only_words():
    """THE FIX. If a row is text again, we are back to "it all looks the same"."""
    block = _option_block()
    assert "worker_bee(w.state)" in block, "no per-row state icon"
    assert "state_color(w.state)" in block, "the worker name is not coloured by state"
    assert "state-bg-" in block, "no colour stripe — state is not scannable down the edge"


def test_the_state_colours_are_actually_defined():
    """A POSITIVE CONTROL on the test above. `state-bg-BUZZING` in the template proves
    nothing if the class does not exist; every row would render the same neutral bar and
    the previous test would still pass."""
    for state in ("BUZZING", "WAITING", "STUNG", "RESTING"):
        assert re.search(rf"\.state-bg-{state}\s*\{{[^}}]*background:", _BASE), (
            f"state-bg-{state} is used in the row but never defined, so that state "
            "renders with no colour at all"
        )


def test_the_colours_are_distinct_from_each_other():
    """Four states sharing one variable would satisfy every check above and still be
    unscannable — which is the complaint, restated."""
    found = {}
    for state in ("BUZZING", "WAITING", "STUNG", "RESTING"):
        m = re.search(rf"\.state-bg-{state}\s*\{{\s*background:\s*([^;]+);", _BASE)
        assert m
        found[state] = m.group(1).strip()
    assert len(set(found.values())) == len(found), f"states share a colour: {found}"


def test_rows_show_what_the_worker_is_doing():
    """Operator asked for "task count / current task" — choosing a worker by name alone
    is what made the list unscannable in the first place."""
    block = _option_block()
    # #1496 renamed the source: the rows now read `worker_task_cards`, which
    # carries number + title + ASSIGNED/ACTIVE instead of a bare title string.
    # The guarantee this test exists for — rows show what the worker is on —
    # is unchanged and now stronger.
    assert "worker_task_cards" in block, "rows do not show the worker's current task"
    assert "c.number" in block, "rows show a title but not WHICH task"
    assert "needs_operator_input" in block, (
        "nothing marks the workers that need the operator — 'which need me' was the "
        "stated reason for scanning the list at all"
    )


# --- 2. surviving the swap that replaces the whole partial --------------------------


def test_open_state_is_stored_where_the_partial_cannot_destroy_it():
    """worker_list.html is replaced wholesale every few seconds. State on the control
    dies with it, closing the list under the operator's finger."""
    assert "document.body.classList.toggle('wsel-open'" in _JS
    assert "swarm:workers-rendered" in _JS, (
        "nothing re-applies the open state to the freshly rendered list"
    )


def test_the_swap_hook_actually_fires_the_event():
    """A POSITIVE CONTROL on the listener: a handler for an event nobody dispatches is
    indistinguishable from a working one until you reload with the list open."""
    assert _JS.count("swarm:workers-rendered") >= 2, (
        "the render event is listened for but never dispatched"
    )
    hook = _JS[_JS.index("htmx:afterSwap") :]
    assert "swarm:workers-rendered" in hook[:2000], (
        "the event is not dispatched from the worker-list swap hook"
    )


def test_no_handler_is_bound_directly_to_the_control():
    """The failure this prevents reads as intermittent, not broken: the control works
    until the first worker changes state, then goes dead."""
    for bad in ("wselTrigger().addEventListener", "wselList().addEventListener"):
        assert bad not in _JS, f"{bad} dies on the first htmx swap"


# --- 3. accessibility, which the native <select> provided for free ------------------


def test_the_listbox_is_announced_correctly():
    assert 'role="combobox"' in _PARTIAL, "the trigger is not a combobox"
    assert 'aria-haspopup="listbox"' in _PARTIAL
    assert 'aria-expanded="false"' in _PARTIAL, "no initial expanded state"
    assert 'role="option"' in _option_block()
    assert "aria-selected=" in _option_block()


def test_the_collapsed_list_is_out_of_the_accessibility_tree():
    """`hidden`, not merely visually hidden: otherwise a screen reader reads sixteen
    workers that are not on screen."""
    assert re.search(r'<ul[^>]*class="wsel-list"[^>]*\shidden', _PARTIAL, re.S), (
        "the option list is not `hidden` when collapsed"
    )


def test_it_can_be_driven_from_the_keyboard():
    """Replacing a native <select> with something mouse-only is a regression, not a
    redesign. Escape included: without it an open list is a trap."""
    for key in ("ArrowDown", "ArrowUp", "Home", "End", "Escape", "Enter"):
        assert f"'{key}'" in _JS, f"the selector does not handle {key}"


def test_focus_stays_on_the_listbox_and_moves_via_activedescendant():
    """The listbox pattern. Focusing each option instead breaks Escape and makes the
    screen-reader announcement wrong."""
    assert "aria-activedescendant" in _JS
    assert re.search(r"id=\"wsel-opt-\{\{\s*loop\.index\s*\}\}\"", _PARTIAL), (
        "options have no stable ids, so aria-activedescendant cannot reference them"
    )


def test_the_tap_targets_are_big_enough():
    """WCAG 2.1 AA target size, and this is a sixteen-row list on a phone — precisely
    where a cramped target costs a mis-tap onto the wrong worker."""
    m = re.search(r"\.wsel-opt\s*\{[^}]*min-height:\s*(\d+)px", _BASE)
    assert m, "no minimum row height on the selector's options"
    assert int(m.group(1)) >= 44, f"rows are {m.group(1)}px, below the 44px AA target"


def test_selection_goes_through_the_same_entry_point_as_the_desktop_pills():
    """Two selection paths drift. The pills and the selector must agree on what
    choosing a worker means."""
    assert "window.selectWorker" in _js_function("wselChoose"), (
        "the selector does not route through window.selectWorker"
    )


# --- 4. the defect that got past every scan above ------------------------------------


def test_the_switcher_markup_is_balanced():
    """CAUGHT IN A REAL BROWSER, not here. My first version of this control closed
    ``.wsel`` but not ``.worker-switcher``, so the browser nested THE ENTIRE REST OF THE
    PAGE inside it — and ``.worker-switcher`` is ``display:none`` above mobile width, so
    the whole dashboard vanished. The popped-out panel test went red; every source-scan
    test in this file passed, because each one asks whether a string is present and none
    of them asks whether the document still parses.

    A tag-balance check is crude, but it is the cheapest thing that would have caught it,
    and template edits by string replacement is exactly how it happened.
    """
    code = re.sub(r"\{#.*?#\}", "", _PARTIAL, flags=re.S)
    i = code.index('<div class="worker-switcher">')
    j = code.index("{% for w in workers %}\n{{ worker_row")
    block = code[i:j]
    assert block.count("<div") == block.count("</div>"), (
        f"unbalanced <div> in the switcher: {block.count('<div')} open, "
        f"{block.count('</div>')} closed — the rest of the page becomes its child"
    )
    assert block.count("<ul") == block.count("</ul>")
    assert block.count("<li") == block.count("</li>")


def test_the_whole_partial_is_balanced():
    """The block check above only covers the switcher. The same string-replacement
    hazard applies to any edit in this file, and the failure is always the same shape:
    silently reparented content, no error anywhere."""
    code = re.sub(r"\{#.*?#\}", "", _PARTIAL, flags=re.S)
    for tag in ("div", "ul", "li", "span", "button"):
        opens = len(re.findall(rf"<{tag}[\s>]", code))
        closes = len(re.findall(rf"</{tag}>", code))
        assert opens == closes, f"<{tag}>: {opens} opened, {closes} closed in the partial"


def test_the_trigger_updates_immediately_on_selection():
    """A REGRESSION THE NATIVE CONTROL DID NOT HAVE, caught in a real browser.

    A <select> repaints its selected option the instant you pick one — the browser does
    it. A custom control gets nothing for free, and this trigger's label is
    server-rendered from ``selected_worker``, so after a tap it kept reading "Select a
    worker" until the next partial refresh. The control looked like it had ignored the
    input, which is the one thing a selector must never do.

    Pinned inside wselChoose specifically: doing it only on the server render is the
    behaviour that was wrong, and it looks perfectly correct in the template.
    """
    body = _js_function("wselChoose")
    assert "wsel-trigger-name" in body, (
        "wselChoose does not update the trigger label, so the control appears to ignore "
        "the tap until the next server render"
    )
    assert "aria-label" in body, "the accessible name still names the previous worker"
