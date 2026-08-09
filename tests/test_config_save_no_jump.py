"""Saving on the config page must not move the page under the operator (#1359).

THE REPORT, email 2 of four: "Dashboard alignment issue. Also when you save the page
jumps because the save toast appears inline." (Dictated from Outlook on Android, which
is why it reached us as "the Dave test"; the operator confirmed the reading.)

THE MECHANISM. ``#unsaved-banner`` is the FIRST child of ``<body>``, before ``<header>``,
and the save cycle toggles it between ``display:none`` and ``display:block`` three times
in a row — "Unsaved changes *" on the first keystroke, "Saving…", then "✓ Saved". In
normal flow that inserts and removes a ~30px band above everything, so the whole page
jumps twice per save while you are still typing in it.

Config autosaves 1.5s after the last change, so this fires unprompted rather than on a
button press: the page moves while your eye is on a field.

THE FIX is positioning, not markup — the element stays where it is and stops taking part
in layout, matching ``.toast-container`` on the dashboard so both pages behave alike.
Tested as that property rather than as an exact rule, because any of ``fixed`` /
``absolute`` / ``sticky`` would satisfy the report and only in-flow fails it.
"""

from __future__ import annotations

import re
from pathlib import Path

_BASE = Path("src/swarm/web/templates/base.html")
_CONFIG = Path("src/swarm/web/templates/config.html")


def _rule(selector: str) -> str:
    """The declaration block for a selector, from the first non-media occurrence."""
    css = _BASE.read_text(encoding="utf-8")
    i = css.index(selector + " {")
    return css[i : css.index("}", i)]


def test_the_save_status_is_taken_out_of_the_document_flow():
    """THE FIX. In flow, showing it pushes every element below it down the page."""
    block = _rule(".unsaved-banner")
    match = re.search(r"position:\s*(fixed|absolute|sticky)", block)
    assert match, (
        "#unsaved-banner is still laid out in normal flow, so toggling it on save "
        f"moves the whole page: {block}"
    )


def test_it_is_still_the_first_child_of_body():
    """A POSITIVE CONTROL on the premise. If the element were moved or renamed this
    test file would keep passing while testing nothing, so pin what makes the jump
    severe: it sits above the header, so its height displaces the entire document."""
    html = _CONFIG.read_text(encoding="utf-8")
    body = html.index("{% block body %}")
    banner = html.index('id="unsaved-banner"')
    header = html.index("<header>", body)
    assert body < banner < header, (
        "the save status is no longer the first thing in the body — re-check whether "
        "this test still describes the reported jump"
    )


def test_the_save_cycle_still_toggles_it_repeatedly():
    """The other half of the premise: one toggle would be a blink, three is a jump.

    Guards against a future refactor that keeps the element always-visible and makes
    the positioning fix look unnecessary when it is what is holding the page still.
    """
    html = _CONFIG.read_text(encoding="utf-8")
    shows = html.count("_showBanner(")
    assert shows >= 3, f"expected the dirty/saving/saved cycle, found {shows} calls"
    assert "_banner.style.display = 'none'" in html, "nothing hides it again"


def test_it_does_not_cover_the_header_controls():
    """Floating it over the Dashboard/Config buttons would trade a jump for a
    misclick. Offset below the header, the same 60px the dashboard toasts use."""
    block = _rule(".unsaved-banner")
    assert "60px" in block or "bottom:" in block, (
        f"no vertical offset — this will sit on top of the header: {block}"
    )
