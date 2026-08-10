"""The service worker is disabled — a kill switch, and a diagnostic (#1359 crash work).

WHY. With ONLY Swarm open, the operator's Edge BROWSER PROCESS climbed past 5GB at 4.3%
sustained CPU and "very high" power, while the Swarm RENDERER sat at 264MB and 0% CPU.
The page is not looping. Something between the page and the network/storage layer is —
and the service worker is the only Swarm component living there: it intercepts every
request and writes to Cache Storage, both of which are browser-process work.

An earlier fix to its fetch handler (caching /api/health?_=<timestamp>, a unique URL per
poll, into an unbounded cache) took the browser process from 12,316MB to 88.8MB in a
controlled test. It was a real bug. It was evidently not the whole story.

So the worker is removed rather than adjusted again. Six speculative fixes tonight was
enough; this one answers a question either way:
  - memory and CPU normalise  -> the worker is confirmed, restore it piece by piece
  - they do not               -> the worker is exonerated and the search moves on

COST: the PWA loses offline support and app-shell precaching. The app is fully
server-rendered and does not otherwise depend on it.
"""

from __future__ import annotations

from pathlib import Path

_SW = Path("src/swarm/web/static/sw.js").read_text(encoding="utf-8")
_BASE = Path("src/swarm/web/templates/base.html").read_text(encoding="utf-8")


def test_the_page_does_not_reregister_the_kill_switch_worker():
    """The production page must not recreate the worker it is trying to remove.

    2026.8.10.10 changed ``sw.js`` into a kill switch whose activate handler
    unregisters itself and navigates every client.  ``base.html`` still registered
    that file on every production page load, creating an unbounded browser-process
    loop::

        page load -> register -> activate -> unregister + navigate -> page load

    Edge reached 4 GB private memory in under a minute with only Swarm open.  The
    cleanup belongs in the page; registering an unregistering worker can never
    converge.
    """
    assert "serviceWorker.register(" not in _BASE, (
        "base.html recreates the service-worker kill switch on every load; its "
        "activate-time navigation then reloads the page and starts the cycle again"
    )


def test_the_page_removes_existing_workers_and_swarm_caches():
    """Removing registration must still clean up installs from older releases."""
    assert "serviceWorker.getRegistrations()" in _BASE
    assert "reg.unregister()" in _BASE
    assert "caches.keys()" in _BASE and "caches.delete(" in _BASE


def test_the_worker_unregisters_itself():
    """THE KILL SWITCH. Shipping a worker that merely does less would leave it in the
    request path, which is the thing under suspicion."""
    assert "self.registration.unregister()" in _SW, (
        "the worker does not remove itself; requests still route through it"
    )


def test_it_has_no_fetch_handler():
    """A fetch handler is what puts it in the path of every request. Its absence is the
    property that matters, not any particular caching policy."""
    assert "addEventListener('fetch'" not in _SW, (
        "a fetch handler is back — the worker is intercepting requests again"
    )


def test_it_deletes_every_cache_it_ever_made():
    """Unregistering alone leaves the accumulated Cache Storage on disk and in the
    browser process. The point is to reclaim it, not just to stop adding."""
    assert "caches.keys()" in _SW and "caches.delete" in _SW, (
        "old caches are not deleted; the banked entries stay"
    )


def test_it_takes_effect_without_a_second_reload():
    """skipWaiting + navigating existing clients. Without both, the OLD worker keeps
    control until every window is closed — and the operator would reasonably conclude
    the fix did nothing."""
    assert "skipWaiting()" in _SW, "the new worker waits for the old one to release"
    assert "clients.matchAll" in _SW, "open windows are never moved off the old worker"
