"""POST /api/notifications — external tools raising an operator notification (#1265).

Before this, the only way for something outside the daemon to reach the operator
was to INSERT into ``buzz_log`` directly. ``credential-check-cron.sh`` did
exactly that, and documented why in the script:

    "There is no external API to raise an operator notification —
     /api/notifications is GET-only and /api/hooks/event only handles Claude
     Code lifecycle events... Coupling to an internal table is a real cost;
     the follow-up is a POST endpoint in swarm."

THE DESIGN POINT WORTH GUARDING is that the endpoint does exactly ONE thing:
appends a drone-log entry with ``is_notification=True``. ``StatePublisher``
already fans notification-worthy entries out to the WebSocket, so also calling
``push_notification`` would deliver every external notification TWICE. Using the
same single entry point is what makes an external notification
indistinguishable from an internal one, rather than merely similar-looking.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from swarm.server.api import create_app
from tests.test_api import _API_HEADERS, _inject_session_cookie, daemon  # noqa: F401


@pytest.fixture
async def client(daemon):  # noqa: F811
    app = create_app(daemon, enable_web=False)
    async with TestClient(TestServer(app)) as c:
        _inject_session_cookie(c)
        yield c


@pytest.mark.asyncio
async def test_external_tool_can_raise_a_notification(client, daemon):  # noqa: F811
    """AC-1. No buzz_log INSERT required."""
    resp = await client.post(
        "/api/notifications",
        json={"label": "CREDENTIAL_CHECK_BROKEN", "detail": "V6_API_KEY dead", "source": "cron"},
        headers=_API_HEADERS,
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "raised"
    assert body["label"] == "CREDENTIAL_CHECK_BROKEN"


@pytest.mark.asyncio
async def test_it_lands_as_a_notification_entry_like_internal_ones(client, daemon):  # noqa: F811
    """AC-2. Same entry point, so the dashboard cannot tell them apart."""
    await client.post(
        "/api/notifications",
        json={"label": "CREDENTIAL_CHECK_BROKEN", "detail": "V6_API_KEY dead"},
        headers=_API_HEADERS,
    )
    notifs = [e for e in daemon.drone_log.entries if e.is_notification]
    assert notifs, "nothing was flagged as a notification"
    last = notifs[-1]
    assert "V6_API_KEY dead" in last.detail
    assert last.metadata.get("label") == "CREDENTIAL_CHECK_BROKEN"


@pytest.mark.asyncio
async def test_it_does_not_double_notify(client, daemon):  # noqa: F811
    """THE guard. StatePublisher already fans is_notification entries out to the
    WebSocket; calling push_notification here too would deliver twice."""
    import ast
    import inspect
    import textwrap

    from swarm.server.routes import drones

    # AST, not a substring search. The handler's DOCSTRING names
    # push_notification to explain why it does not call it, so a text match
    # reports a call that isn't there — the same trap as grepping for a symbol
    # and finding the comment about it.
    tree = ast.parse(textwrap.dedent(inspect.getsource(drones.handle_notification_raise)))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "push_notification" not in called, (
        "handler calls push_notification directly — StatePublisher already does "
        "that for is_notification entries, so this would double-deliver"
    )


@pytest.mark.asyncio
async def test_empty_detail_is_refused_and_records_nothing(client, daemon):  # noqa: F811
    """AC-4. A rejected request must not leave a half-notification behind."""
    before = len(daemon.drone_log.entries)
    resp = await client.post("/api/notifications", json={"label": "X"}, headers=_API_HEADERS)
    assert resp.status >= 400
    assert len(daemon.drone_log.entries) == before


@pytest.mark.asyncio
async def test_the_action_enum_stays_closed(client, daemon):  # noqa: F811
    """A caller's arbitrary label must NOT become a SystemAction member.

    That value drives routing, filtering and priority mapping, so an open string
    would silently break every consumer that switches on it. The label travels
    in metadata instead.
    """
    from swarm.drones.log import SystemAction

    await client.post(
        "/api/notifications",
        json={"label": "TOTALLY_MADE_UP", "detail": "x"},
        headers=_API_HEADERS,
    )
    assert not hasattr(SystemAction, "TOTALLY_MADE_UP")
    last = [e for e in daemon.drone_log.entries if e.is_notification][-1]
    assert last.action == SystemAction.EXTERNAL_NOTIFICATION
    assert last.metadata.get("label") == "TOTALLY_MADE_UP"


@pytest.mark.asyncio
async def test_the_get_history_endpoint_still_works(client, daemon):  # noqa: F811
    """Adding POST to the same path must not shadow the existing GET."""
    resp = await client.get("/api/notifications")
    assert resp.status == 200
    assert "notifications" in await resp.json()
