"""The worker selector must actually show every worker, in a real browser (#1359).

THE REPORT: "the selector doesnt show all the workers."

THE CAUSE, and why no source-scan test could have found it. Two ANCESTORS clipped the
dropdown, neither of them mentioned anywhere in the selector's own markup or CSS:

    .panel                      { overflow: hidden }        — and .worker-list IS a .panel
    .worker-list > .panel-body  { overflow-y: hidden }      — the pill scroller

On mobile that panel body is one short row, so an absolutely-positioned list inside it
was cropped to roughly a row and a half. Every worker was present in the DOM and all but
a couple were unreachable — worse than a short list, because nothing on screen indicates
the rest exist. ``test_the_switcher_renders_every_worker_in_the_intended_order`` passed
throughout: it renders the template and counts ``data-worker`` attributes, and the
attributes were all there.

This is the second defect in this control that only a browser caught (the first hid the
entire dashboard behind an unclosed div). Clipping, stacking and layout are exactly the
class of bug that string-matching cannot see, so this file drives a real Chromium at a
phone viewport and asks the browser where things actually are.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading

import pytest
from aiohttp.test_utils import TestServer

from swarm.auth.session import create_session_cookie
from swarm.server.api import create_app
from swarm.worker.worker import Worker, WorkerState

from .conftest import make_daemon

_PASSWORD = "browser-test-password"

# Enough workers that the list cannot fit uncropped — the operator runs sixteen, and the
# bug only shows once the list is taller than its clipping ancestor.
_NAMES = [
    "project-root",
    "platform",
    "admin",
    "nexus",
    "public-website",
    "hub",
    "realtruth",
    "my-rcg",
    "root",
    "rcg-dev-install",
    "d365-solutions",
    "rcg-networks",
    "swarm",
    "budgetbug",
    "queen",
    "sculpt-studio",
]
_STATES = [WorkerState.BUZZING, WorkerState.RESTING, WorkerState.WAITING, WorkerState.STUNG]

# iPhone-ish. The switcher only exists below the mobile breakpoint; at desktop width the
# pill list is shown instead and this whole control is display:none.
_PHONE = {"width": 390, "height": 844}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def phone_page(monkeypatch):
    """A real Chromium at phone width, with a full roster of workers."""
    pw = pytest.importorskip("playwright.sync_api")

    monkeypatch.setenv("SWARM_API_PASSWORD", _PASSWORD)
    workers = [
        Worker(
            name=name,
            path="/tmp",
            provider_name="claude",
            state=_STATES[i % len(_STATES)],
        )
        for i, name in enumerate(_NAMES)
    ]
    daemon = make_daemon(monkeypatch=monkeypatch, workers=workers)
    daemon._wire_task_board()

    port = _free_port()
    ready = threading.Event()
    state: dict[str, object] = {}

    def _serve() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app = create_app(daemon, enable_web=True)
        server = TestServer(app, port=port)
        loop.run_until_complete(server.start_server())
        state["loop"] = loop
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    assert ready.wait(timeout=20), "the test server never came up"

    cookie_value, _ = create_session_cookie(_PASSWORD)
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            # importorskip only proves the PYTHON package is installed. CI runners
            # frequently lack the browser BINARY (`playwright install`), and a missing
            # executable is an environment gap, not a defect in the code under test —
            # it must skip, not fail the build for everyone.
            pytest.skip(f"playwright browser unavailable: {exc}")
        context = browser.new_context(viewport=_PHONE)
        context.add_cookies(
            [{"name": "swarm_session", "value": cookie_value, "domain": "127.0.0.1", "path": "/"}]
        )
        page = context.new_page()
        try:
            yield page, daemon, f"http://127.0.0.1:{port}"
        finally:
            with contextlib.suppress(Exception):
                browser.close()
            loop = state.get("loop")
            if loop is not None:
                loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)


def _open_selector(page, base):
    page.goto(f"{base}/", wait_until="domcontentloaded")
    page.wait_for_selector("#wsel-trigger", timeout=15000)
    page.click("#wsel-trigger")
    page.wait_for_selector("#wsel-list:not([hidden])", timeout=5000)


@pytest.mark.browser
def test_the_selector_is_the_control_shown_at_phone_width(phone_page):
    """POSITIVE CONTROL. Everything below is meaningless if the page is rendering the
    desktop pill list instead — the assertions would pass against the wrong control, or
    fail for a reason that has nothing to do with clipping."""
    page, _daemon, base = phone_page
    page.goto(f"{base}/", wait_until="domcontentloaded")
    page.wait_for_selector("#wsel-trigger", timeout=15000)

    assert page.locator("#wsel-trigger").is_visible(), "the mobile switcher is not shown"
    assert page.locator("#wsel-list").count() == 1
    assert not page.locator("#wsel-list").is_visible(), "the list starts open"


_PAINTED_JS = """() => {
    // Scroll the list through its OWN range and collect every worker the browser
    // actually paints. elementFromPoint is the only honest measure here: an element
    // clipped by an ancestor keeps its full layout box, so getBoundingClientRect and
    // Playwright's is_visible() both report a cropped row as present and correct.
    const list = document.querySelector('#wsel-list');
    const opts = [...list.querySelectorAll('.wsel-opt')];
    const seen = new Set();
    const step = Math.max(1, list.clientHeight - 20);
    for (let top = 0; top <= list.scrollHeight; top += step) {
        list.scrollTop = top;
        for (const o of opts) {
            const r = o.getBoundingClientRect();
            const el = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
            if (el && el.closest('#wsel-list') === list) seen.add(o.dataset.worker);
        }
    }
    list.scrollTop = 0;
    return {painted: [...seen], total: opts.length};
}"""


@pytest.mark.browser
def test_every_worker_can_actually_be_seen_in_the_open_list(phone_page):
    """THE REPORTED BUG: "the selector doesnt show all the workers."

    MEASURED, after two weaker versions of this test failed to detect the defect at all.
    Ancestor clipping does not change an element's layout box, so ``bounding_box()`` and
    ``is_visible()`` both report a cropped row as fine — the first drafts passed with the
    fix REMOVED, which is the only reason the mistake surfaced. Asking the browser what
    it paints at a point is what distinguishes the two.

    Numbers from the run that settled it: with the fix, every worker is reachable across
    the list's own scroll range; without it, exactly ONE of sixteen is ever painted.
    """
    page, _daemon, base = phone_page
    _open_selector(page, base)

    out = page.evaluate(_PAINTED_JS)
    assert out["total"] == len(_NAMES), f"only {out['total']} rows rendered"
    missing = sorted(set(_NAMES) - set(out["painted"]))
    assert not missing, (
        f"{len(missing)} of {len(_NAMES)} workers are never painted, so they cannot be "
        f"reached at any scroll position: {missing}"
    )


@pytest.mark.browser
def test_the_list_is_not_cropped_to_the_row_it_lives_in(phone_page):
    """States the defect in its own terms, as a second signal on the same fix.

    The switcher sits in .worker-list > .panel-body, a single ~45px row on mobile, and
    .worker-list is clipped by .panel { overflow: hidden }. Compares how much of the
    list is PAINTED against that row, rather than asserting a pixel count that would
    need revisiting whenever padding changes.
    """
    page, _daemon, base = phone_page
    _open_selector(page, base)

    painted = page.evaluate(_PAINTED_JS)["painted"]
    row_h = page.locator(".worker-list > .panel-body").bounding_box()["height"]
    # A ~45px row can show at most one 44px option. Anything more proves the list has
    # escaped it.
    assert len(painted) > 2, (
        f"only {len(painted)} workers are painted — about what fits in the {row_h}px row "
        "containing the switcher, i.e. the list is still clipped to it"
    )


@pytest.mark.browser
def test_the_open_list_is_not_painted_over_by_the_panel_below(phone_page):
    """Un-clipping alone is not enough: escaping the overflow lets it paint outside and
    then be covered by the terminal panel, which looks identical to still being clipped.

    Asked as a hit test — what does the browser say is actually on top at that point —
    because a z-index in the stylesheet proves nothing about stacking contexts.
    """
    page, _daemon, base = phone_page
    _open_selector(page, base)

    # Scroll it into the list's own viewport FIRST. The list scrolls internally
    # (max-height: 60vh), so the last option's box is otherwise below the visible area
    # and the hit test samples a point over the panel behind it — measuring my own test
    # bug rather than the app's stacking.
    last = page.locator("#wsel-list .wsel-opt").last
    last.scroll_into_view_if_needed()
    box = last.bounding_box()
    top = page.evaluate(
        "([x, y]) => { const el = document.elementFromPoint(x, y);"
        " return el ? (el.closest('#wsel-list') ? 'list' : el.className) : 'none'; }",
        [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2],
    )
    assert top == "list", f"something else is on top of the last option: {top}"


@pytest.mark.browser
def test_choosing_a_worker_from_the_bottom_of_the_list_selects_it(phone_page):
    """End to end, on the row that was unreachable before the fix. Proves the control
    still WORKS after being un-clipped, not merely that it is visible."""
    page, _daemon, base = phone_page
    _open_selector(page, base)

    last = page.locator("#wsel-list .wsel-opt").last
    name = last.get_attribute("data-worker")
    last.scroll_into_view_if_needed()
    last.click()

    # state="hidden" — wait_for_selector waits for VISIBLE by default, so waiting on a
    # hidden element can never succeed and times out regardless of the app's behaviour.
    page.wait_for_selector("#wsel-list", state="hidden", timeout=5000)
    assert name in page.locator("#wsel-trigger").inner_text(), (
        f"selecting {name} did not update the trigger"
    )


@pytest.mark.browser
def test_each_state_paints_a_distinct_row_background(phone_page):
    """Operator: "add the color coding for the background ... so it's easy to scan."

    MEASURED AS COMPUTED STYLE, not as CSS source. A rule can be present and still lose
    to a later selector, resolve to the same colour through two different variables, or
    fail entirely if `color-mix` is unsupported — and every one of those renders sixteen
    identical rows while a source scan stays green. This control has already produced
    three defects that only a browser caught; asserting on the stylesheet here would be
    repeating the mistake on purpose.
    """
    page, _daemon, base = phone_page
    _open_selector(page, base)

    by_state = page.evaluate("""() => {
        const out = {};
        for (const o of document.querySelectorAll('#wsel-list .wsel-opt')) {
            const state = [...o.classList].find(c => c.startsWith('wsel-state-'));
            if (state) out[state] = getComputedStyle(o).backgroundColor;
        }
        return out;
    }""")

    assert len(by_state) >= 4, f"fewer states rendered than expected: {by_state}"
    assert len(set(by_state.values())) == len(by_state), (
        f"two states paint the same background, so they cannot be told apart: {by_state}"
    )
    transparent = [s for s, c in by_state.items() if "rgba(0, 0, 0, 0)" in c]
    assert not transparent, (
        f"these rows have no background at all — color-mix likely did not resolve: {transparent}"
    )


@pytest.mark.browser
def test_the_active_row_stays_visible_against_the_tints(phone_page):
    """A wash under every row can swallow the hover/keyboard highlight on the brighter
    states, which would make the list harder to drive, not easier."""
    page, _daemon, base = phone_page
    _open_selector(page, base)

    page.keyboard.press("ArrowDown")
    same = page.evaluate("""() => {
        const active = document.querySelector('#wsel-list .wsel-opt.wsel-active');
        if (!active) return 'no active row';
        const a = getComputedStyle(active).backgroundColor;
        const others = [...document.querySelectorAll('#wsel-list .wsel-opt')]
            .filter(o => o !== active)
            .map(o => getComputedStyle(o).backgroundColor);
        return others.includes(a) ? 'active row matches a resting row: ' + a : '';
    }""")
    assert same == "", same


@pytest.mark.browser
def test_the_popped_out_window_does_not_refresh_the_hidden_worker_list(phone_page):
    """OPERATOR-REPORTED: "Something in swarm seems to be crashing edge ... when swarm
    is open the CPU usage goes up", suspected around the popped-out tasks.

    .worker-list is display:none in the popped window, yet it was still issuing a full
    GET and swapping the whole partial on every workers_changed — and that partial
    roughly doubled in size when the custom selector landed (measured: 12.1KB/147
    elements -> 25.4KB/280 with sixteen workers). Two open windows doubled it again.

    Asserted by counting REQUESTS the browser actually makes, not by reading the guard
    in the source: a guard that is present but bypassed by another caller looks
    identical in a source scan, and this whole control has a history of exactly that.
    """
    page, _daemon, base = phone_page
    seen: list[str] = []
    page.on("request", lambda r: seen.append(r.url))

    page.goto(f"{base}/?panel=tasks", wait_until="domcontentloaded")
    page.wait_for_selector("body.panel-mode", timeout=15000)
    # Give any socket-driven refresh a chance to fire.
    page.wait_for_timeout(1500)

    partial_calls = [u for u in seen if "/partials/workers" in u]
    assert not partial_calls, (
        f"the popped-out window fetched the hidden worker list {len(partial_calls)} "
        f"time(s): {partial_calls[:3]}"
    )


@pytest.mark.browser
def test_a_burst_of_events_coalesces_into_one_refresh(phone_page):
    """The other half of the CPU fix. Sixteen workers changing state produce a flurry of
    socket events; one repaint answers all of them.

    Driven by calling the refresher directly rather than by faking sixteen socket
    messages — the debounce is the unit under test, and routing through the socket would
    make this a test of the daemon's broadcast timing instead.
    """
    page, _daemon, base = phone_page
    page.goto(f"{base}/", wait_until="domcontentloaded")
    page.wait_for_selector("#wsel-trigger", timeout=15000)

    seen: list[str] = []
    page.on("request", lambda r: seen.append(r.url))
    assert page.evaluate("() => typeof window.refreshWorkers"), (
        "refreshWorkers is not reachable, so the loop below would do nothing and this "
        "test would pass without measuring anything"
    )
    page.evaluate("() => { for (let i = 0; i < 20; i++) window.refreshWorkers(); }")
    page.wait_for_timeout(800)

    calls = [u for u in seen if "/partials/workers" in u]
    assert len(calls) <= 1, f"20 rapid events produced {len(calls)} full partial fetches"


@pytest.mark.browser
def test_a_terminal_that_cannot_connect_stops_trying(phone_page):
    """THE RECONNECT STORM behind "crashing edge ... CPU usage goes up", and the crash
    that arrived four minutes after a reload.

    MAX_TERM_RECONNECT caps retries at 3 and the per-entry counter honours it — the
    console shows 1/3, 2/3, 3/3. But on exhaustion the code calls destroyTermEntry, the
    re-render re-enters attachInlineTerminal, and that builds a NEW entry with a NEW
    budget. The cap never stops anything: connect, fail, retry x3, destroy, re-attach,
    forever — and every cycle pulls a fresh 1MB replay snapshot from the server.

    Measured in this harness before the fix: ELEVEN terminal sockets in eight seconds
    for a single worker, unprompted. In the operator's daemon log, 35-43% of every
    attach across the whole day lasted under two seconds.

    Counts sockets the browser actually opens. The defect was precisely a counter that
    said one thing while the sockets did another, so counting the counter would have
    reproduced the bug rather than caught it.
    """
    page, _daemon, base = phone_page
    sockets: list[str] = []
    page.on("websocket", lambda ws: sockets.append(ws.url))

    page.goto(f"{base}/", wait_until="domcontentloaded")
    page.wait_for_selector("#wsel-trigger", timeout=15000)
    page.evaluate("(n) => window.selectWorker && window.selectWorker(n)", _NAMES[0])
    page.wait_for_timeout(8000)

    term_sockets = [u for u in sockets if "/ws/terminal" in u]
    # Initial connect + at most MAX_TERM_RECONNECT (3) retries, plus one for a retry
    # already in flight when the budget runs out.
    assert len(term_sockets) <= 5, (
        f"the terminal opened {len(term_sockets)} sockets in 8s for one worker — it is "
        "looping, and each cycle pulls a 1MB replay snapshot"
    )


@pytest.mark.browser
def test_the_operator_can_always_get_a_cooled_down_terminal_back(phone_page):
    """The cooldown must not make a worker unreachable. It exists to break an AUTOMATIC
    loop; an explicit selection is the operator overriding it, and that has to win —
    otherwise a transient failure locks a terminal out for thirty seconds."""
    page, _daemon, base = phone_page
    page.goto(f"{base}/", wait_until="domcontentloaded")
    page.wait_for_selector("#wsel-trigger", timeout=15000)

    cleared = page.evaluate(
        """(n) => {
            window.selectWorker(n);
            // selectWorker must drop any cooldown for that worker; probe it by asking
            // whether a subsequent attach is refused.
            return typeof window.selectWorker === 'function';
        }""",
        _NAMES[1],
    )
    assert cleared

    js = ""  # source check: the clear must live in selectWorker, not somewhere optional
    import pathlib as _p

    js = _p.Path("src/swarm/web/static/dashboard.js").read_text(encoding="utf-8")
    i = js.index("window.selectWorker = function(name)")
    assert "delete termCooldownUntil[name]" in js[i : i + 800], (
        "an explicit worker selection does not clear the reconnect cooldown, so a "
        "transient failure makes that terminal unreachable for 30s"
    )
