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


def test_webgl_is_still_gated_on_that_guard():
    """A POSITIVE CONTROL on the two tests above. They check a regex; this checks the
    regex is what decides. If the branch stopped consulting _isMac, both would pass while
    every Mac loaded WebGL again."""
    assert re.search(r"if\s*\(\s*!_isMac\s*&&[^)]*WebglAddon", _JS), (
        "the WebGL renderer is no longer gated on _isMac"
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
