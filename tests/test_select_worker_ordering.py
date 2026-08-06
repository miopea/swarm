"""``window.selectWorker`` must have one base definition, decorators after it (#1292).

THE BUG, and it is a shape worth naming. dashboard.js had THREE assignments to
``window.selectWorker``. One of them, ~130 lines ABOVE the base definition, was a
decorator:

    // Show tile button when a worker is selected
    var _origSelectWorker = window.selectWorker;      // <- undefined at this point
    window.selectWorker = function(name) {
        var btn = document.getElementById('tile-mode-btn');
        if (btn) btn.style.display = '';
        _origSelectWorker(name);                       // <- would throw if reached
    };

It was DOUBLY dead. It captured ``window.selectWorker`` before anything had assigned
it, so its captured original was ``undefined``; and the base definition below then
overwrote the wrapper outright, so it was never reached at all. The visible consequence:
``#tile-mode-btn`` ships with ``style="display:none"`` and nothing ever cleared it, so
Tile view was unreachable while looking fully implemented in both markup and JS.

WHY A TEST AND NOT JUST A FIX: nothing about this fails loudly. Load order in one large
IIFE-per-module file is invisible at review time, the decorator reads as correct in
isolation, and the symptom is a button that never appears — which looks like a design
choice. The same file also has a legitimate decorator (the Command Center's, which
guards with ``if (_origSelectWorker)``), so "there is a wrapper" is not itself the
smell. The smell is a wrapper installed BEFORE the thing it wraps.
"""

from __future__ import annotations

import re
from pathlib import Path

_RAW = (
    Path(__file__).parent.parent / "src" / "swarm" / "web" / "static" / "dashboard.js"
).read_text()


def _blank_comment_only_lines(src: str) -> list[str]:
    r"""Return the lines with comment-ONLY lines replaced by empty strings.

    Two deliberate choices, both learned the hard way in this repo.

    FIRST, comments must be excluded at all. Three scans here have failed by matching
    the PROSE THAT EXPLAINS A BUG instead of the bug: #1286's refusal sweep matched a
    comment quoting the old bad text; #1291's sort check matched a Jinja comment saying
    why the sort was not used; and the first draft of THIS file classified the base
    definition as a decorator, because the comment inside it names ``_origSelectWorker``
    while explaining what was removed. A scan that reads comments reports a fix as the
    defect it fixed.

    SECOND, LINE-BASED and not a regex over the whole file. The draft used
    ``re.sub(r"/\*.*?\*/", "", src, flags=re.S)``, which ate real code: any ``/*`` inside
    a string literal or regex literal pairs with the next ``*/`` anywhere downstream and
    the span between them vanishes. That silently deleted one of the two assignments this
    file exists to count — the scan under-reported and would have gone green on a
    reintroduced bug. Dropping only lines that are ENTIRELY a comment cannot reach inside
    a string, and blanking rather than deleting keeps line numbers usable in failures.
    """
    out = []
    for line in src.split("\n"):
        s = line.lstrip()
        out.append("" if s.startswith(("//", "/*", "*/", "* ")) else line)
    return out


_CODE = "\n".join(_blank_comment_only_lines(_RAW))

_ASSIGN = re.compile(r"window\.selectWorker\s*=\s*function")
_CAPTURE = re.compile(r"\w+\s*=\s*window\.selectWorker\s*;")


def _line_of(offset: int) -> int:
    """1-based line number in dashboard.js — blanking preserved the line count."""
    return _CODE.count("\n", 0, offset) + 1


def _assignment_bodies() -> list[tuple[int, str]]:
    """(line number, body) per assignment, each bounded by the NEXT assignment.

    Bounding on the next assignment rather than a fixed character count is the fix for
    the draft's classifier: a fixed 1200-char window ran past the end of the base into
    unrelated code, so whether a body looked like a decorator depended on what happened
    to sit below it.
    """
    starts = [m for m in _ASSIGN.finditer(_CODE)]
    out = []
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(_CODE)
        out.append((_line_of(m.start()), _CODE[m.end() : end]))
    return out


def test_the_scan_finds_the_real_call_sites():
    """Positive control. If these patterns matched nothing, every assertion below would
    pass over an empty set — the same silence as the bug. This also pins the blanking
    pass: an over-eager comment filter that eats code shows up here as a short file."""
    assert _ASSIGN.search(_CODE), "no window.selectWorker assignment found; scan is broken"
    assert "tile-mode-btn" in _CODE, "tile-mode-btn is gone; this test needs revisiting"
    assert len(_CODE.split("\n")) == len(_RAW.split("\n")), "blanking changed the line count"
    assert len(_assignment_bodies()) >= 2, (
        "expected the base plus at least one decorator; fewer means the comment filter "
        "deleted code (the exact way the first draft of this test broke)"
    )


def test_there_is_exactly_one_base_definition():
    """AC-2. A base is an assignment that does NOT delegate to a captured original.
    Two bases means the later silently wins and the earlier is dead code."""
    bodies = _assignment_bodies()
    # A CALL to the captured original, not a mention of its NAME — the distinction is
    # load-bearing, see _blank_comment_only_lines.
    bases = [(ln, b) for ln, b in bodies if "_origSelectWorker(" not in b]
    assert len(bases) == 1, (
        f"expected exactly 1 base definition of window.selectWorker, found "
        f"{len(bases)} of {len(bodies)} assignments (lines "
        f"{[ln for ln, _ in bases]}). A second base overwrites the first and whatever "
        f"decorated it."
    )


def test_no_decorator_captures_selectWorker_before_the_base_is_defined():
    """AC-3, the load-bearing one. A decorator installed before the base captures
    ``undefined`` AND is then overwritten by the base — dead twice over, silently."""
    first = _ASSIGN.search(_CODE)
    assert first, "no assignment found"
    early = [_line_of(m.start()) for m in _CAPTURE.finditer(_CODE) if m.start() < first.start()]
    assert not early, (
        f"a decorator captures window.selectWorker at line(s) {early}, BEFORE the base "
        f"definition at line {_line_of(first.start())}. It captures undefined and is "
        f"then overwritten — exactly #1292's tile-mode-btn bug."
    )


def test_the_tile_button_is_revealed_by_the_base_definition():
    """AC-1. The reveal lives in the base rather than a wrapper, so there is no ordering
    hazard to get wrong again. The button ships hidden, so if nothing clears it the Tile
    feature is unreachable."""
    bases = [(ln, b) for ln, b in _assignment_bodies() if "_origSelectWorker(" not in b]
    assert len(bases) == 1, "expected one base; test_there_is_exactly_one_base covers this"
    _, base_body = bases[0]
    assert "tile-mode-btn" in base_body, (
        "the base definition of window.selectWorker no longer reveals #tile-mode-btn, "
        "so the Tile button stays display:none forever"
    )


def test_every_call_to_the_captured_original_is_guarded():
    """The Command Center's decorator is legitimate, but each of its calls must keep its
    ``if (_origSelectWorker)`` guard: unguarded, it throws whenever the capture is
    undefined — precisely how the deleted wrapper would have failed had it ever run.

    Checked PER CALL SITE, not by looking for the guard somewhere nearby. The draft
    searched an 800-char window after the capture, and there are two guarded calls in
    that window; deleting one guard left the other for the substring to find, so the
    control passed with the defect installed. Same first-match false negative as #1291's
    D-pad opacity test. A window test asks "does a guard exist?" — the property is "is
    every call guarded?"
    """
    calls = [
        (n, line)
        for n, line in enumerate(_blank_comment_only_lines(_RAW), start=1)
        if "_origSelectWorker(" in line
    ]
    assert len(calls) >= 2, (
        f"expected the CC decorator's call sites; found {len(calls)}. If the decorator "
        f"was deliberately removed, update this test — do not let it pass vacuously."
    )
    unguarded = [n for n, line in calls if "if (_origSelectWorker)" not in line]
    assert not unguarded, (
        f"line(s) {unguarded} call _origSelectWorker without guarding it; the call throws "
        f"whenever the capture is undefined"
    )
