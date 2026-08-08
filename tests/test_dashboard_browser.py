"""A REAL browser drives the dashboard (#3 of the four-item audit).

WHY THIS FILE HAD TO EXIST. Every other client-side test in this repo scans
``dashboard.js`` as text. Those catch a deleted line; they cannot catch BEHAVIOUR, and
behaviour is where every dashboard bug of 2026-08-06/07 lived:

* the task panel not updating (a stranded debounce timer, then a lost frame with no way
  to notice),
* the editor showing stale values and writing them back,
* assigning a Backlog task silently un-parking it.

Every one was found by the operator, in his browser, after a reload. Source scans were
green throughout — they were green *while* production was broken. He named the
consequence: "feels like we just patched it, not fixed it properly if we keep having
flaky issues like this."

WHAT THIS ASSERTS THAT NO SCAN CAN. It runs the real aiohttp app, loads the real page
in Chromium, executes the real JavaScript, and checks what the DOM actually shows after
a server-side mutation the browser was never pushed. That is the reconciliation
property from 2026.8.7.5 — the one I shipped and could not verify, twice telling the
operator a fix was in without proof.

Marked ``browser`` so it can be deselected where no browser is installed; Playwright is
already a declared dependency and Chromium is present in this environment.
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
from swarm.tasks.task import SwarmTask
from tests.conftest import make_daemon

pytestmark = pytest.mark.browser

_PASSWORD = "browser-test-password-not-a-real-secret"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def playwright_page(monkeypatch):
    """Real app on a real port, a real Chromium page, already authenticated.

    The session cookie is minted directly rather than by driving the login form: this
    file exists to test the TASK VIEW, and routing every case through a login flow
    would make login failures look like view failures.
    """
    pw = pytest.importorskip("playwright.sync_api")

    monkeypatch.setenv("SWARM_API_PASSWORD", _PASSWORD)
    daemon = make_daemon(monkeypatch=monkeypatch)
    daemon._wire_task_board()

    from swarm.server.daemon import SwarmDaemon

    daemon.broadcast_ws = SwarmDaemon.broadcast_ws.__get__(daemon, SwarmDaemon)
    daemon.publisher._broadcast_ws = daemon.broadcast_ws

    # THE SERVER RUNS IN ITS OWN THREAD. Playwright's sync API blocks the calling
    # thread, and the app is in this same process — so with a single thread page.goto()
    # would wait for a response from a loop that is not running. Deadlock, not failure,
    # which is the worst way for a test to be wrong.
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
        state["server"] = server
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    assert ready.wait(timeout=20), "the test server never came up"

    cookie_value, _ = create_session_cookie(_PASSWORD)

    with pw.sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        context.add_cookies(
            [
                {
                    "name": "swarm_session",
                    "value": cookie_value,
                    "domain": "127.0.0.1",
                    "path": "/",
                }
            ]
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


def test_the_dashboard_loads_and_renders_the_task_list(playwright_page):
    """Positive control for everything below. If the page does not load or the panel
    does not render, every later assertion would pass or fail for the wrong reason."""
    page, daemon, base = playwright_page
    daemon.task_board.add(SwarmTask(title="visible-task-alpha", description=""))

    page.goto(f"{base}/", wait_until="domcontentloaded")
    page.wait_for_selector("#task-list", timeout=15000)

    assert page.locator("#task-list").count() == 1, "the task panel did not render"
    assert "visible-task-alpha" in page.content(), (
        "a task on the board is not in the rendered page; the panel is not showing "
        "server state at all"
    )


def test_the_rendered_page_stamps_the_board_version(playwright_page):
    """The reconciler can only detect drift if the page says which version it shows.
    Asserted in the DOM, not the template source — a stamp that fails to render is
    indistinguishable from one that was never added, and only this can tell."""
    page, daemon, base = playwright_page
    daemon.task_board.add(SwarmTask(title="stamped", description=""))

    page.goto(f"{base}/", wait_until="domcontentloaded")
    # state="attached": the stamp is a hidden <span>, and the default wait is for
    # VISIBLE, which it never becomes.
    page.wait_for_selector("#task-board-version", state="attached", timeout=15000)

    rendered = page.get_attribute("#task-board-version", "data-version")
    assert rendered is not None and rendered.isdigit(), (
        f"the board version is not stamped as a number in the DOM: {rendered!r}"
    )
    assert int(rendered) == daemon.task_board.version, (
        f"the page claims board version {rendered} but the board is at {daemon.task_board.version}"
    )


def test_the_editor_reads_the_task_from_the_server_not_the_row(playwright_page):
    """ROOT CAUSE 1, proved in a browser. The row is deliberately made STALE after
    render — exactly the state the operator hit ("the modal still showed the wrong
    information") — and the editor must still show current server state.

    A source scan can only prove the fetch call exists. This proves the value that
    reaches the operator's screen came from the server.
    """
    page, daemon, base = playwright_page
    task = daemon.task_board.add(SwarmTask(title="original-title", description=""))

    page.goto(f"{base}/", wait_until="domcontentloaded")
    page.wait_for_selector("#task-list", timeout=15000)

    # Change the task on the SERVER, then corrupt the row's cached attributes so the
    # DOM and the server disagree. Whichever the editor trusts, it will show.
    daemon.task_board.update(task.id, title="server-side-truth")
    page.evaluate(
        """() => {
            document.querySelectorAll('[data-task-title]').forEach(el => {
                el.dataset.taskTitle = 'STALE-ROW-VALUE';
            });
        }"""
    )

    page.evaluate("(id) => window.showTaskEditorById(id)", task.id)
    page.wait_for_timeout(400)

    shown = page.input_value("#tm-title")
    assert shown != "STALE-ROW-VALUE", (
        "the editor populated from the row's stale data-* attributes — this is the bug "
        "that displayed wrong values and wrote them back on save"
    )
    assert shown == "server-side-truth", (
        f"the editor is not showing current server state; it shows {shown!r}"
    )


def test_the_view_reconciles_after_a_change_it_was_never_pushed(playwright_page):
    """ROOT CAUSE 2, and the property I shipped twice without being able to verify.

    The page is disconnected from the WebSocket so it CANNOT be told anything, then the
    board is mutated server-side. Reacting to a push is now provably not what repairs
    the view — only the version comparison can, which is the whole point of the
    reconciliation design.
    """
    page, daemon, base = playwright_page
    daemon.task_board.add(SwarmTask(title="before-the-silence", description=""))

    page.goto(f"{base}/", wait_until="domcontentloaded")
    # state="attached": the stamp is a hidden <span>, and the default wait is for
    # VISIBLE, which it never becomes.
    page.wait_for_selector("#task-board-version", state="attached", timeout=15000)
    for _ in range(50):
        if daemon.hub.ws_clients:
            break
        page.wait_for_timeout(100)

    # SEVER THE PUSH SERVER-SIDE, and assert it is severed. An earlier version closed
    # the socket from inside the page — which silently did nothing, because the main
    # socket is a closure variable, not window.ws. The test then passed while proving
    # only that the PUSH worked; a control that removed the reconciler's re-render left
    # it green. Dropping the hub's clients here means the server has nobody to send to,
    # so nothing but the version comparison can repair the view.
    assert daemon.hub.ws_clients, "positive control: the page must be connected first"
    daemon.hub.ws_clients.clear()

    daemon.task_board.add(SwarmTask(title="arrived-during-the-silence", description=""))
    expected = daemon.task_board.version

    # The production interval is 15s; call the reconciler directly rather than idling,
    # so this asserts the LOGIC rather than the timer value (which the source test
    # already pins).
    page.evaluate("() => window.reconcileTaskView()")
    for _ in range(30):
        page.wait_for_timeout(100)
        if page.get_attribute("#task-board-version", "data-version") == str(expected):
            break

    rendered = page.get_attribute("#task-board-version", "data-version")
    assert rendered == str(expected), (
        f"the view is still showing board version {rendered} while the server is at "
        f"{expected}, with no push available — reconciliation did not repair it, which "
        f"is the exact 'flaky, only a filter toggle fixes it' failure"
    )
    assert "arrived-during-the-silence" in page.content(), (
        "the version was updated but the task the operator could not see is still "
        "missing from the rendered panel"
    )


def test_the_tile_controls_sit_together_at_the_right_of_the_detail_header(playwright_page):
    """OPERATOR-REPORTED 2026-08-07: "Tile thing is cool, but alignment of the top is
    funny."

    ``.panel-header`` is ``justify-content: space-between``, so three loose children
    spread evenly and the Tile button was parked in the MIDDLE of the header with gaps
    either side. It had never been seen before: the button's reveal was dead code until
    2026.8.6.24 (#1292), so this layout had genuinely never rendered.

    A GEOMETRY assertion, which is the whole reason a browser test earns its keep here.
    A source scan can confirm a wrapper div exists; only a rendered page can say where
    the button actually IS. Both facts are checked — the controls are adjacent to each
    other, and they sit in the right-hand portion of the header — because either alone
    is satisfiable by a layout that still looks wrong.
    """
    page, daemon, base = playwright_page
    page.goto(f"{base}/", wait_until="domcontentloaded")
    page.wait_for_selector("#tile-mode-btn", state="attached", timeout=15000)

    # The button ships hidden and is revealed when a worker is selected (#1292).
    page.evaluate("() => window.selectWorker('api')")
    page.wait_for_timeout(200)

    # TILE MODE ON, which is the operator's actual state and the only one that
    # reproduces the bug. With tile mode off the size select is hidden, leaving just
    # the title and the button — and space-between right-aligns TWO children anyway, so
    # the test passed against the broken layout. Both controls visible is what pushes
    # the button into the middle. (An earlier version of this test missed that, and two
    # controls that broke the layout left it green.)
    page.click("#tile-mode-btn")  # the real control, via the real click delegation
    page.wait_for_timeout(400)
    assert page.locator("#tile-size-select").is_visible(), (
        "tile mode did not turn on, so the three-child layout that causes the bug is "
        "not being exercised"
    )

    btn = page.locator("#tile-mode-btn").bounding_box()
    header = page.locator("#detail-title").bounding_box()
    assert btn and header, "positive control: the button and header must be laid out"
    assert btn["width"] > 0, "the Tile button is not visible, so its position means nothing"

    gap_to_right_edge = (header["x"] + header["width"]) - (btn["x"] + btn["width"])
    assert gap_to_right_edge < header["width"] * 0.35, (
        f"the Tile button sits {gap_to_right_edge:.0f}px from the header's right edge "
        f"(header is {header['width']:.0f}px wide) — it is floating in the middle "
        f"rather than grouped with the size select"
    )

    select = page.locator("#tile-size-select")
    if select.is_visible():
        sbox = select.bounding_box()
        between = sbox["x"] - (btn["x"] + btn["width"])
        assert 0 <= between < 40, (
            f"the Tile button and the size select are {between:.0f}px apart; they are "
            f"meant to read as one control group"
        )


def test_the_jira_setup_block_renders_on_the_config_page(playwright_page):
    """The setup UI must actually appear and be wired, not merely exist in the template.

    Every other check on this feature reads source. Source scans were green throughout
    every dashboard bug of 2026-08-06/07, and the Jira setup block was built the same
    day a renamed input silently broke the whole config save — so the page getting as
    far as rendering it is worth asserting in a real browser.

    Deliberately NOT asserting discovery results: that needs a live Atlassian instance,
    and a test that pretends otherwise would prove less than it appears to.
    """
    page, daemon, base = playwright_page
    page.goto(f"{base}/config", wait_until="domcontentloaded")
    page.wait_for_selector("#jira-setup-block", state="attached", timeout=15000)
    # Reachable, not merely present: the block sits in the integrations tab, and a
    # setup UI nobody can navigate to is the same as one that does not exist.
    page.click("[data-action='switchConfigTab'][data-tab='integrations']")
    page.wait_for_selector("#jira-setup-block", state="visible", timeout=10000)

    assert page.locator("#jira-discover-project").count() == 1, "no project input rendered"
    assert page.locator("#cfg-jira-projects").count() == 1, "the projects field is missing"
    assert page.locator("[data-action='jiraDiscover']").count() == 1, "no Discover control"
    assert page.locator("[data-action='jiraPlan']").count() == 1, "no Preview control"

    # The legacy routing fields must READ as inert rather than merely be ignored — a
    # live-looking input that no longer does anything is a setting lying to the operator.
    assert page.locator("#cfg-jira-import_filter").is_disabled(), (
        "the legacy JQL filter still looks editable, so it reads as though it still routes"
    )
    assert page.locator("#cfg-jira-import_label").is_disabled(), (
        "the legacy label field still looks editable"
    )


def test_clicking_discover_without_a_project_does_not_call_the_api(playwright_page):
    """The cheapest possible guard on a button that talks to someone's Jira: it must
    refuse locally rather than firing an empty request and surfacing a server error."""
    page, daemon, base = playwright_page
    page.goto(f"{base}/config", wait_until="domcontentloaded")
    page.wait_for_selector("#jira-setup-block", state="attached", timeout=15000)

    calls: list[str] = []
    page.on("request", lambda r: calls.append(r.url) if "/api/jira/" in r.url else None)

    # The Jira settings live in the "integrations" TAB, which ships display:none. Without
    # switching to it the button never becomes actionable and page.click waits forever —
    # the test hangs rather than failing, which is the worse outcome of the two.
    page.click("[data-action='switchConfigTab'][data-tab='integrations']")
    page.wait_for_selector("#jira-setup-block", state="visible", timeout=10000)
    page.click("[data-action='jiraDiscover']")
    page.wait_for_timeout(400)

    assert not calls, f"an empty project key still hit the Jira API: {calls}"
    result = page.locator("#jira-discover-result")
    assert result.is_visible(), "no feedback shown for the empty case"
    assert "project key" in result.inner_text().lower(), (
        f"the message does not say what to do: {result.inner_text()!r}"
    )


def test_the_mapping_rows_are_dropdowns_of_real_statuses(playwright_page):
    """OPERATOR-REPORTED 2026-08-08: "This needs to be a drop down of jira transitions".

    Free-text let you type a status the project does not have, which produces an export
    Jira refuses — the exact failure this phase exists to prevent — and gave no way to
    know which names were legal. Rendered here from a stubbed discovery response so the
    DOM is asserted, not the template source; every UI bug in this project so far was
    found in a browser, not a scan.
    """
    page, daemon, base = playwright_page
    page.goto(f"{base}/config", wait_until="domcontentloaded")
    page.wait_for_selector("#jira-setup-block", state="attached", timeout=15000)
    page.click("[data-action='switchConfigTab'][data-tab='integrations']")
    page.wait_for_selector("#jira-setup-block", state="visible", timeout=10000)

    # Feed the renderer the operator's real IS vocabulary.
    page.evaluate(
        """() => {
            const d = {
                project: 'IS',
                statuses: [
                    {name: 'ToDo', category: 'new'},
                    {name: 'Reopened', category: 'new'},
                    {name: 'In Progress', category: 'indeterminate'},
                    {name: 'Waiting for support', category: 'indeterminate'},
                    {name: 'Done', category: 'done'},
                    {name: 'Canceled', category: 'done'},
                ],
                proposed_status_map: {backlog: 'ToDo', active: 'In Progress', done: 'Done'},
                unmapped: [],
            };
            const out = document.getElementById('jira-discover-result');
            out.style.display = '';
            out.innerHTML = window._jiraRenderDiscoveryForTest
                ? window._jiraRenderDiscoveryForTest(d)
                : '';
        }"""
    )
    page.wait_for_timeout(200)

    selects = page.locator("#jira-discover-result select.jira-map-input")
    assert selects.count() >= 7, (
        f"expected a dropdown per Swarm status, found {selects.count()} — free-text "
        f"inputs let the operator type a status the project does not have"
    )

    first = selects.first
    options = first.locator("option").all_inner_texts()
    assert "ToDo" in options and "Done" in options, (
        f"the dropdown is not populated from the project's real statuses: {options}"
    )
    assert any("not mapped" in o for o in options), (
        "there is no way to choose 'no mapping', so an unmappable status cannot be "
        "left deliberately blank"
    )
    # Grouped, so choosing a To Do status to mean 'completed' is visibly wrong rather
    # than merely a typo.
    assert first.locator("optgroup").count() >= 2, "options are not grouped by category"
