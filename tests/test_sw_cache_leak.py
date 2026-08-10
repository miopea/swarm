"""The service worker must not cache unique-per-request URLs (browser-process leak).

THE REPORT: Edge climbing to 12-14GB with ALL extensions disabled and only Swarm open,
reclaimed only by fully quitting the browser — never by reloading the page.

THE CAUSE. sw.js cached EVERY response in its catch-all branch:

    caches.open(CACHE_NAME).then(c => c.put(req, clone));

``cache.put`` is keyed by URL, so that is bounded only while the URLs are. They are not —
the dashboard polls ``/api/health?_=<Date.now()>``, a UNIQUE URL every call, so each poll
wrote a permanent Cache Storage entry that nothing evicted.

WHY IT TOOK ALL EVENING TO FIND. Cache Storage lives in the BROWSER process. Every
instrument built during this investigation measured the renderer: JS heap (flat at 17MB
of a 4192MB limit), DOM nodes (~1,450), canvases, WebSocket bytes. All flat, all correct,
all irrelevant. The operator's own Task Manager screenshot is what localised it — Swarm's
renderer at 116MB beside a 12,316MB browser process — and that is the reading no
page-side counter could ever have produced.
"""

from __future__ import annotations

import re
from pathlib import Path

_SW = Path("src/swarm/web/static/sw.js").read_text(encoding="utf-8")
_JS = Path("src/swarm/web/static/dashboard.js").read_text(encoding="utf-8")


def test_the_cache_is_restricted_to_static_assets():
    """THE FIX. API responses are live state; caching them durably is both a leak and a
    correctness bug waiting to happen."""
    assert "cacheUrl.pathname.startsWith('/static/')" in _SW, (
        "the service worker caches beyond /static/ again — API responses will accumulate"
    )


def test_cache_busted_urls_are_never_cached():
    """The specific shape that caused it: a `_=<timestamp>` parameter makes every request
    a distinct cache key, so caching them grows without bound by construction."""
    assert "cacheUrl.searchParams.has('_')" in _SW, (
        "cache-busted URLs are cacheable again; each poll becomes a permanent entry"
    )


def test_non_cacheable_requests_still_work_offline():
    """The cache was also the offline fallback. Skipping the WRITE must not skip the
    READ, or turning this leak off would break offline use as a side effect."""
    i = _SW.index("if (!cacheable)")
    branch = _SW[i : i + 200]
    assert "caches.match(req)" in branch, (
        "non-cacheable requests no longer fall back to cache when the network fails"
    )


def test_the_polling_url_that_caused_it_still_exists():
    """A POSITIVE CONTROL on the premise. If the health poll stopped being cache-busted,
    every test above would pass while describing a problem that no longer exists — and
    the next person would not know why the restriction is there."""
    assert re.search(r"/api/health\?_=' \+ Date\.now\(\)", _JS), (
        "the cache-busted health poll is gone; re-read this file before trusting it"
    )


def test_only_the_app_shell_is_precached():
    """Bounding the runtime cache is pointless if install-time precaching is unbounded."""
    m = re.search(r"APP_SHELL\s*=\s*\[(.*?)\]", _SW, re.S)
    assert m, "APP_SHELL is gone"
    entries = [e for e in m.group(1).split(",") if e.strip()]
    assert len(entries) < 20, f"the precache list has grown to {len(entries)} entries"
