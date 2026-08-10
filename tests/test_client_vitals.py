"""A browser heartbeat that outlives the tab (#1359 follow-up).

WHY THIS EXISTS. The operator's Edge tab has died four times, never on demand and
always some minutes in. Console output dies WITH the tab, so every one of those crashes
produced no evidence at all — and three fixes were shipped reasoned from inference. Two
of them were real defects, but none of them stopped the crash, and at that point more
inference is not a plan.

A heartbeat posted to the daemon survives the crash. The last line before a gap is the
trajectory: heap climbing toward jsHeapSizeLimit means memory, heap flat means the cause
is elsewhere and the memory levers (1MB replay, 10 cached terminals) are the wrong ones
to spend the operator's scrollback on.

The endpoint is deliberately dumb — parse, log, return. A diagnostic that can fail the
request it rides on, or slow the dashboard down, becomes its own incident.
"""

from __future__ import annotations

import pytest
from aiohttp import web

from swarm.server.routes.system import handle_client_vitals


class _Req:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.mark.asyncio
async def test_a_heartbeat_is_recorded(caplog):
    """The whole point: the numbers reach the log, where they survive the tab."""
    with caplog.at_level("WARNING"):
        resp = await handle_client_vitals(
            _Req({"heapMB": 812, "heapLimitMB": 4096, "terms": 7, "canvases": 3, "uptimeS": 240})
        )
    assert isinstance(resp, web.Response)
    text = caplog.text
    assert "client-vitals" in text
    assert "812" in text and "4096" in text, f"the heap figures did not reach the log: {text}"
    assert "terms=7" in text, "the live terminal count is missing — the memory lever"
    assert "canvases=3" in text, (
        "the canvas count is missing — it is the direct read of whether a GPU renderer "
        "is live, which is what the crash turns on"
    )


@pytest.mark.asyncio
async def test_the_platform_and_renderer_reach_the_log(caplog):
    """THE FIELDS THE CRASH INVESTIGATION TURNS ON. The client sent these and the server
    dropped them on the floor — the first reload after the WebGL fix produced a
    heartbeat that could not confirm the fix, which is the whole reason the heartbeat
    exists. A payload field nobody logs is a field nobody has."""
    with caplog.at_level("WARNING"):
        await handle_client_vitals(_Req({"plat": "macOS", "webgl": False, "heapMB": 20}))
    assert "plat=macOS" in caplog.text, "the platform is not in the log"
    assert "webgl=False" in caplog.text, "the renderer choice is not in the log"


@pytest.mark.asyncio
async def test_it_logs_at_warning_so_the_operator_actually_sees_it(caplog):
    """Operators run at the default level. A DEBUG heartbeat is a heartbeat nobody has,
    which is the same as not having written it — this codebase has made that mistake
    before and it cost a forensic trail."""
    with caplog.at_level("WARNING"):
        await handle_client_vitals(_Req({"heapMB": 1}))
    assert any(r.levelname == "WARNING" for r in caplog.records), (
        "the heartbeat is below WARNING, so it will not be in the log after a crash"
    )


@pytest.mark.asyncio
async def test_a_malformed_body_never_fails_the_request():
    """The browser posts this every 30s. If a bad body could 500, the diagnostic becomes
    a source of noise in the very log it exists to keep readable."""
    resp = await handle_client_vitals(_Req(ValueError("not json")))
    assert resp.status == 200


@pytest.mark.asyncio
async def test_junk_values_do_not_raise(caplog):
    """Values come from a browser and are not to be trusted — a string where a number
    belongs must not take the endpoint down."""
    with caplog.at_level("WARNING"):
        resp = await handle_client_vitals(
            _Req({"heapMB": "lots", "terms": None, "canvases": [], "uptimeS": {}})
        )
    assert resp.status == 200


def test_the_dashboard_actually_sends_it():
    """A POSITIVE CONTROL on the whole mechanism. An endpoint nobody calls logs nothing,
    and would look identical to a browser that never crashed again."""
    from pathlib import Path

    js = Path("src/swarm/web/static/dashboard.js").read_text(encoding="utf-8")
    assert "/api/client-vitals" in js, "nothing posts the heartbeat"
    # Matches the call, not its exact argument — the interval now also samples
    # process memory, and pinning the literal made this red for a formatting reason.
    assert "beat()" in js and "setInterval(" in js, "the heartbeat fires once and never again"
    assert "performance.memory" in js, "no heap reading — the number that settles it"
