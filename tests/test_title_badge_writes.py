"""Per-event browser-process writes are removed — an EXPERIMENT, not a proven fix.

WHAT IS MEASURED (the operator's own trace, not my inference):
    browser process:  sqlite  6,677.6 MB
    GPU process:              286.7 MB   normal
    renderer v8:              131.7 MB   normal
and, observed directly: the growth stops and slowly drops when Swarm is closed.

WHAT IS RULED OUT BY MEASUREMENT: Cache Storage (empty), all storage APIs (0B of a
306GB quota), network looping (~45 requests at load then quiet), the service worker
(unregistered — still leaked), notifications (blocked — still leaked), renderer heap,
DOM nodes, GPU.

WHAT IS NOT PROVEN: that these two calls are what writes that database. Nothing has
measured a History or badge write. The trace reports `sqlite` in aggregate and refuses
to break it down further. These are suspects because they are per-event BROWSER-PROCESS
writes on a path that a server-side change earlier today made continuous — the classifier
fix that stopped workers being stuck at BUZZING. That is circumstantial.

So this is an experiment with a real outcome either way: if the browser process stops
growing, the theory holds; if it does not, these calls are exonerated and the search
moves on. That is worth as much as a fix, and more than another confident guess.
"""

from __future__ import annotations

import re
from pathlib import Path

_JS = Path("src/swarm/web/static/dashboard.js").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    i = _JS.index("function " + name)
    return _JS[i : _JS.index("\n    }\n", i)]


def test_the_title_is_not_rewritten_on_a_timer():
    """THE CHANGE. This rewrote document.title EVERY SECOND, indefinitely, for as long
    as an event went unacknowledged. Each assignment is an IPC to the browser process,
    which records the page title in its History database."""
    body = _fn("startTitleFlash")
    assert "setInterval" not in body, (
        "the title is on a timer again — one browser-process write per second, forever"
    )


def test_the_count_is_still_shown_once():
    """Removing the flash must not remove the signal. Setting it once conveys the same
    thing and writes once."""
    body = _fn("startTitleFlash")
    assert "document.title" in body and "pendingTitleCount" in body, (
        "the pending-event count no longer reaches the tab title at all"
    )


def test_stopping_still_restores_the_original_title():
    """The acknowledge path has to keep working, or the tab is left showing a stale
    count that nothing clears."""
    body = _fn("stopTitleFlash")
    assert "ORIGINAL_TITLE" in body


def test_the_app_badge_is_off_behind_an_explicit_flag():
    """For an INSTALLED PWA the badge is persisted by the browser, so every call is a
    browser-process write — and this fired on every event. Left as a guarded no-op
    rather than deleted, so restoring it is deliberate and one line."""
    body = _fn("updateAppBadge")
    assert "__swarmAppBadgeEnabled" in body, "the badge writes on every event again"
    assert body.index("__swarmAppBadgeEnabled") < body.index("setAppBadge"), (
        "the guard runs after the call it is supposed to prevent"
    )


def test_no_other_timer_writes_the_title():
    """A POSITIVE CONTROL on the premise: removing one timer achieves nothing if another
    rewrites the title just as often."""
    for m in re.finditer(r"setInterval\(", _JS):
        window = _JS[m.start() : m.start() + 400]
        assert "document.title" not in window, (
            f"another interval writes document.title: {window[:120]!r}"
        )
