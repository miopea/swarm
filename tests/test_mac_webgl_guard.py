"""The macOS WebGL guard must actually fire on macOS (#1359 crash investigation).

THE CRASH. The operator's Edge tab died repeatedly — never on demand, always minutes in,
sometimes taking the popped-out window with it in the same instant.

WHAT THE MEASUREMENT SAID. A heartbeat posted to the daemon every 30s (so it survives
the tab) showed the JS heap FLAT at 23MB of a 4192MB limit right up to the moment both
windows stopped. 0.5% of the limit, one cached terminal, no growth. That rules out every
memory hypothesis — the 1MB replay, the ten cached terminals, the doubled worker-list
swap — and two windows dying together rules out a single tab hitting a ceiling. It is a
GPU-process crash.

WHY IT WAS HAPPENING. dashboard.js already documents this exact failure: "macOS
Chromium/Edge crashes the *whole* renderer through xterm's WebGL path on a redraw (e.g.
the selection repaint a right-click triggers) — a hard GPU-process crash". The guard was
written and was correct in intent. It just never fired:

    navigator.userAgentData.platform  ->  "macOS"     (lowercase m, checked FIRST)
    /Mac|iPhone|iPad|iPod/            ->  no match     (case-sensitive)
    navigator.platform                ->  "MacIntel"   (would have matched, never reached)

So _isMac was false on every Mac with userAgentData, WebGL loaded, and the guard
protected nobody. A one-character class of bug behind four crashes.

These tests exercise the PLATFORM STRINGS rather than the DOM, because the defect was
never in the branching — it was in what the predicate returns for the values a real
browser supplies.
"""

from __future__ import annotations

import re
from pathlib import Path

_JS = Path("src/swarm/web/static/dashboard.js").read_text(encoding="utf-8")

# The closed set Chromium's User-Agent Client Hints spec allows, plus the legacy
# navigator.platform values that browsers still return.
_MAC_PLATFORMS = ["macOS", "MacIntel", "MacPPC", "Mac68K", "iOS", "iPhone", "iPad"]
_OTHER_PLATFORMS = ["Windows", "Win32", "Linux", "Linux x86_64", "Android", "Chrome OS"]


def _guard() -> re.Pattern[str]:
    """The live predicate, extracted from the source so the test cannot drift from it."""
    m = re.search(r"var _isMac = /([^/]+)/([a-z]*)\.test\(_uaPlat\);", _JS)
    assert m, "the _isMac guard is gone or was rewritten — re-read this file before editing"
    flags = re.I if "i" in m.group(2) else 0
    return re.compile(m.group(1), flags)


def test_the_guard_matches_what_a_real_mac_reports():
    """THE BUG. "macOS" is what userAgentData actually returns, and it is checked first."""
    pattern = _guard()
    missed = [p for p in _MAC_PLATFORMS if not pattern.search(p)]
    assert not missed, (
        f"these Mac platform strings do not trip the guard, so WebGL loads and the "
        f"renderer crashes: {missed}"
    )


def test_the_guard_does_not_fire_on_other_platforms():
    """The other direction: over-matching would drop Windows and Linux to the slow DOM
    renderer for no reason. 'Chrome OS' is the trap — a naive /os/i would match it."""
    pattern = _guard()
    wrong = [p for p in _OTHER_PLATFORMS if pattern.search(p)]
    assert not wrong, f"the guard fires on non-Mac platforms, costing them GPU rendering: {wrong}"


def test_webgl_is_disabled_outright():
    """THE ACTUAL FIX for this operator, who is on WINDOWS — not macOS.

    The platform correction matters: _isMac was correctly false for them, so fixing its
    case-sensitivity changed nothing on their machine. They were on the platform where
    this code deliberately LEFT WebGL enabled, and got the macOS crash signature anyway:
    JS heap flat at 23MB of a 4192MB limit, and both browser windows dying in the same
    instant. A WebGL crash takes the shared GPU process down and every tab with it.

    So the default flipped for everyone. The original comment already conceded the
    trade — "perf is a non-issue for viewing worker output" — and a measured crash
    outranks an optimisation nobody asked for.
    """
    assert re.search(r"var _webglDisabled = true;", _JS), (
        "WebGL is no longer disabled by default; the GPU-process crash comes back"
    )
    assert re.search(r"if\s*\(\s*!_webglDisabled\s*&&\s*!_isMac[^)]*WebglAddon", _JS), (
        "the WebGL branch no longer consults the disable flag"
    )


def test_disabling_webgl_falls_through_to_the_dom_renderer_not_canvas():
    """Canvas is also a GPU path, so falling back to it would keep the same exposure.

    The Canvas fallbacks live INSIDE the WebGL branch, so skipping the branch skips them
    too and xterm uses its DOM renderer. Asserted because someone hoisting a Canvas
    fallback "for perf" would silently reopen this.
    """
    i = _JS.index("var _webglDisabled = true;")
    j = _JS.index("// Custom link provider", i)
    block = _JS[i:j]
    canvas_uses = block.count("new CanvasAddon.CanvasAddon()")
    assert canvas_uses > 0, "the block moved; re-read before trusting this test"
    # Every Canvas construction must sit inside the WebGL branch that is now skipped.
    branch = block[block.index("if (!_webglDisabled") :]
    assert branch.count("new CanvasAddon.CanvasAddon()") == canvas_uses, (
        "a Canvas renderer is constructed outside the disabled WebGL branch, so the GPU "
        "path is still reachable"
    )


def test_userAgentData_is_preferred_and_therefore_must_be_handled():
    """Pins WHY case-sensitivity was fatal rather than merely untidy: the modern API is
    consulted first, so the legacy value that would have matched is never reached."""
    m = re.search(r"var _uaPlat = ([^;]+);", _JS, re.S)
    assert m, "the platform read was restructured"
    expr = m.group(1)
    assert expr.index("userAgentData") < expr.index("navigator.platform"), (
        "navigator.platform is consulted first now — if that ever changes back, the "
        "case-sensitivity trap returns"
    )


def test_the_renderer_choice_is_reported_in_the_heartbeat():
    """The guard was wrong for a long time because nothing surfaced what it decided.
    Reporting it means the next report is a measurement, not another inference."""
    assert "__swarmTermRenderer" in _JS, "the chosen renderer is not recorded"
    assert "webgl:" in _JS and "plat:" in _JS, (
        "the heartbeat does not report the platform and renderer, so the fix cannot be "
        "confirmed from the operator's own browser"
    )
