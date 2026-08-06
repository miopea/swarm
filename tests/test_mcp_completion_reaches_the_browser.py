"""An MCP completion must reach a real WebSocket client (#1294).

OPERATOR-REPORTED 2026-08-06: he completed nothing himself — a worker closed #1292 over
MCP — and the row stayed in his task panel until he clicked a filter chip. His
screenshot pins his working filter as Backlog + Unassigned + Assigned + In Progress +
Blocked with **Done and Failed OFF**, so a closed task leaves his filtered set and a
real refresh WOULD have removed the row. It did not. That makes this a live-update
failure, not a presentation quirk.

WHY THIS FILE EXISTS WHEN tests/test_task_board_broadcasts.py ALREADY PASSES. That file
drives ``StatePublisher`` directly and asserts the change event fires for all 11
dashboard-reachable verbs. It proves the *middle* of the chain. It cannot fail for
either end: it never goes through the MCP tool handler that actually mutated #1292, and
it never puts a frame on a real socket to a real client. The call graph has now been
traced by hand three times and found correct three times, while the symptom kept
recurring — so this test asserts the two ends instead:

    real MCP handler → real daemon → real BroadcastHub → real aiohttp /ws → CLIENT

FIXTURE HAZARD, and it would have inverted the result. ``make_daemon`` builds the daemon
via ``__new__`` and skips ``__init__``, so it does NOT wire
``task_board.on_change(self._on_task_board_changed)`` — production does that in
``SwarmDaemon._wire_task_board`` (daemon.py:571). A test that forgot this would observe
"no frame arrived" and blame production for an artifact of its own setup. So the wiring
is established by calling production's own ``_wire_task_board()``, never by
re-implementing the subscription here, and ``test_the_fixture_is_wired_like_production``
asserts it took effect before any conclusion is drawn from a missing frame.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from swarm.server.api import create_app
from swarm.tasks.task import SwarmTask, TaskStatus
from tests.conftest import make_daemon

# His chips, verbatim, from the 2026-08-06 screenshot. Done and Failed are OFF.
_OPERATOR_FILTER = "backlog,unassigned,assigned,active,blocked"

_PASSWORD = "test-password-not-a-real-secret"


@pytest.fixture
def daemon(monkeypatch):
    from swarm.server.daemon import SwarmDaemon

    d = make_daemon(monkeypatch=monkeypatch)
    monkeypatch.setattr("swarm.server.routes.websocket.get_api_password", lambda _d: _PASSWORD)

    # conftest sets `d.broadcast_ws = MagicMock()` (conftest.py:244) and the
    # StatePublisher CAPTURES it at construction. With the mock in place the whole
    # chain runs, raises nothing, reports success — and no frame is ever handed to
    # the hub. The first version of this file "reproduced" the operator's bug that
    # way and the reproduction was entirely my own fixture. Rebind production's real
    # function rather than writing a substitute, then repoint the publisher's
    # captured reference at it.
    d.broadcast_ws = SwarmDaemon.broadcast_ws.__get__(d, SwarmDaemon)
    d.publisher._broadcast_ws = d.broadcast_ws

    # Production's own wiring — see the module docstring. Never re-implement it here.
    d._wire_task_board()
    return d


@pytest.fixture
async def client(daemon):
    app = create_app(daemon, enable_web=False)
    async with TestClient(TestServer(app)) as c:
        yield c


async def _open_authed_ws(client):
    """Connect and pass the first-message auth gate, as static/ws-auth.js does."""
    ws = await client.ws_connect("/ws")
    await ws.send_str(json.dumps({"type": "auth", "token": _PASSWORD}))
    return ws


async def _drain(ws, seconds: float = 1.5) -> list[dict]:
    """Collect frames for *seconds*. The hub debounces tasks_changed by 100ms, so a
    read that gives up sooner would report a missing frame that was merely pending."""
    out: list[dict] = []
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while loop.time() < end:
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=max(0.05, end - loop.time()))
        except TimeoutError:
            break
        if msg.type.name == "TEXT":
            out.append(json.loads(msg.data))
        else:
            break
    return out


def _seed_active_task(daemon, worker: str = "api"):
    """One task ASSIGNED to *worker* and then ACTIVE, i.e. closable by that worker."""
    task = daemon.task_board.add(SwarmTask(title="stale-panel repro", description="body"))
    daemon.task_board.assign(task.id, worker)
    daemon.task_board.activate(task.id)
    assert task.status is TaskStatus.ACTIVE, f"seed failed: {task.status}"
    return task


@pytest.mark.asyncio
async def test_the_fixture_is_wired_like_production(daemon):
    """Load-bearing positive control. If the board's change event is not subscribed,
    every assertion about a missing frame below is an artifact of this fixture."""
    fired: list[bool] = []
    daemon.publisher.on_task_board_changed = lambda: fired.append(True)  # type: ignore[method-assign]
    _seed_active_task(daemon)
    assert fired, (
        "task_board.on_change is not wired to the publisher, so no board mutation can "
        "produce a frame in this fixture regardless of production's behaviour"
    )


def test_the_broadcast_path_is_not_mocked(daemon):
    """The control that was missing, and its absence invalidated an entire result.

    ``make_daemon`` replaces ``broadcast_ws`` with a ``MagicMock`` and the publisher
    captures it, so every link reports success while the frame goes nowhere. A test
    built on that observes "no frame arrived" and blames production. Mocks fail in
    BOTH directions and this is the second direction: they make correct code look
    broken. Assert the seam is real before trusting anything downstream of it."""
    from unittest.mock import MagicMock

    assert not isinstance(daemon.broadcast_ws, MagicMock), "daemon.broadcast_ws is mocked"
    assert not isinstance(daemon.publisher._broadcast_ws, MagicMock), (
        "the publisher captured a mocked broadcast_ws, so no frame can reach the hub"
    )
    seen: list[dict] = []
    daemon.hub.broadcast = lambda payload: seen.append(payload)  # type: ignore[method-assign]
    daemon.publisher.on_task_board_changed()
    assert [f.get("type") for f in seen] == ["tasks_changed"], (
        f"publisher.on_task_board_changed did not hand a tasks_changed frame to the hub; got {seen}"
    )


@pytest.mark.asyncio
async def test_a_real_client_receives_the_init_frame(daemon, client):
    """Positive control for the transport itself: auth passes and frames flow, so a
    later missing tasks_changed means that frame specifically, not a dead socket."""
    ws = await _open_authed_ws(client)
    try:
        frames = await _drain(ws)
        assert any(f.get("type") == "init" for f in frames), (
            f"no init frame — the WS never authenticated, so nothing below would be "
            f"evidence about broadcasts. Got: {[f.get('type') for f in frames]}"
        )
        assert daemon.hub.ws_clients, "the client is not registered in hub.ws_clients"
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_completing_a_task_over_mcp_pushes_tasks_changed_to_the_client(daemon, client):
    """THE TEST. The exact shape of the operator's report: a worker closes its task
    through the MCP tool surface, and a connected dashboard must be told."""
    from swarm.mcp.handlers._tasks import _handle_complete_task

    task = _seed_active_task(daemon)
    ws = await _open_authed_ws(client)
    try:
        await _drain(ws)  # clear init/state frames

        reply = _handle_complete_task(daemon, "api", {"number": task.number})
        text = " ".join(part.get("text", "") for part in reply)
        assert "completed" in text.lower(), f"the MCP verb refused, so nothing mutated: {text}"
        assert task.status is TaskStatus.DONE, f"task did not close: {task.status}"

        frames = await _drain(ws)
        types = [f.get("type") for f in frames]
        assert "tasks_changed" in types, (
            f"the task closed over MCP and NO tasks_changed frame reached the connected "
            f"client. Frames seen: {types}. This is the operator's #1294 symptom "
            f"reproduced: with his filter (Done OFF) the row can only leave his panel "
            f"on a refresh, and the refresh is driven by this frame."
        )
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_a_ui_status_change_also_pushes_tasks_changed(daemon, client):
    """AC-2, the comparison that matters. #1275 was closed on the operator verifying a
    UI-driven status change, and reopened on an MCP-driven completion. If the UI path
    broadcasts and the MCP path does not, that difference is the bug; asserting both on
    one daemon is what makes the comparison meaningful rather than anecdotal."""
    task = _seed_active_task(daemon)
    ws = await _open_authed_ws(client)
    try:
        await _drain(ws)
        daemon.task_board.demote_to_backlog(task.id)
        assert task.status is TaskStatus.BACKLOG, f"the UI-path mutation failed: {task.status}"
        types = [f.get("type") for f in await _drain(ws)]
        assert "tasks_changed" in types, (
            f"a UI-reachable status change did not reach the client either. Frames: {types}"
        )
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_the_closed_task_leaves_the_operators_filtered_view(daemon):
    """The other half of the round trip: the frame is only useful if the re-fetch it
    triggers actually drops the row. Runs his five chips against the real partial so a
    delivered frame that changes nothing would still fail."""
    from aiohttp.test_utils import make_mocked_request

    import swarm.web.app  # noqa: F401  # breaks the partials<->app circular import
    from swarm.web.routes import partials

    task = _seed_active_task(daemon)
    request = make_mocked_request("GET", f"/partials/tasks?status={_OPERATOR_FILTER}")
    request.app["daemon"] = daemon

    ctx_before = await partials.handle_partial_tasks.__wrapped__(request)
    assert task.number in {t["number"] for t in ctx_before["tasks"]}, (
        "positive control: the ACTIVE task must be IN his filtered view to start with"
    )

    daemon.task_board.complete(task.id, "done")
    ctx_after = await partials.handle_partial_tasks.__wrapped__(request)
    assert task.number not in {t["number"] for t in ctx_after["tasks"]}, (
        "the completed task is STILL returned under the operator's filter (Done OFF), "
        "so even a delivered tasks_changed frame could not remove the row"
    )
