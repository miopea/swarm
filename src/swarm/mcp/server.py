"""MCP server — Streamable HTTP + legacy SSE transport.

Implements the MCP transport protocol:
- POST /mcp  — Streamable HTTP (current standard, no OAuth trigger)
- GET  /mcp  — SSE stream for Streamable HTTP server-initiated messages
- GET  /mcp/sse — Legacy SSE transport (deprecated, triggers OAuth in Claude Code)
- POST /mcp/message — Legacy SSE message endpoint

Claude Code connects as an MCP client and calls tools defined in tools.py.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import TYPE_CHECKING, Any

from aiohttp import web

from swarm.logging import get_logger
from swarm.mcp.tools import TOOLS, handle_tool_call

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("mcp.server")

# MCP protocol version
_PROTOCOL_VERSION = "2024-11-05"
_SERVER_NAME = "swarm"
_SERVER_VERSION = "1.0.0"

# Per-worker last-MCP-activity timestamp, used by the IdleWatcher drone to
# detect the "client gave up reconnecting after a daemon reload" state (task
# #257).  Keyed on worker_name (the URL query/header value the client sent);
# "unknown" is treated as untracked.  Updated on every ``_dispatch`` call
# regardless of method so ``initialize`` / ``tools/list`` / ``tools/call`` all
# count as activity.  Survives ``broadcast_tools_list_changed`` broadcasts
# because those are client-initiated reactions to the notification we push.
_worker_last_mcp_activity: dict[str, float] = {}


def get_worker_last_mcp_activity(worker_name: str) -> float | None:
    """Return the last MCP dispatch timestamp for ``worker_name``, or None.

    ``None`` means either (a) we've never seen an MCP call from this worker
    since the daemon started, or (b) the worker has made no calls at all on
    this installation.  Callers distinguish those cases via the daemon's
    own start-time.
    """
    return _worker_last_mcp_activity.get(worker_name)


# How often the streamable SSE handler polls its transport for disconnect.
# Disconnect-detection latency is not user-visible (the stream is server→client
# push only; broadcast notifications fire on the broadcast call, not the tick),
# so this can be coarse. Tests that need a tighter cadence override the module
# constant via monkeypatch.
_SSE_KEEPALIVE_POLL = 5.0


def register(app: web.Application) -> None:
    """Register MCP endpoints on the aiohttp application."""
    # Streamable HTTP (current standard)
    app.router.add_post("/mcp", handle_streamable_http)
    app.router.add_get("/mcp", handle_streamable_sse)
    app.router.add_delete("/mcp", handle_streamable_delete)

    # Legacy SSE transport (kept for backward compat)
    app.router.add_get("/mcp/sse", handle_sse)
    app.router.add_post("/mcp/message", handle_message)


# ---------------------------------------------------------------------------
# Streamable HTTP transport (POST /mcp)
# ---------------------------------------------------------------------------


async def handle_streamable_http(request: web.Request) -> web.Response:
    """Handle a JSON-RPC request via Streamable HTTP.

    The client POSTs JSON-RPC to /mcp and receives the response directly
    in the HTTP response body. No persistent SSE connection needed.
    """
    # Identify worker: query param (from per-worker .mcp.json) > header > unknown
    worker_name = (
        request.rel_url.query.get("worker") or request.headers.get("X-Swarm-Worker") or "unknown"
    )
    session_id = request.headers.get("Mcp-Session-Id", "")

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    method = body.get("method", "")
    msg_id = body.get("id")
    params = body.get("params", {})

    # Auto-revive (task #227): a POST with an unknown non-empty session
    # ID is treated as a stale session from a previous daemon process.
    # Instead of rejecting with 404 (which Claude Code's HTTP transport
    # did not recover from), we mint a new session, bind the original
    # request to it, and return the new ID in the response header. The
    # ``broadcast_tools_list_changed()`` call after the response nudges
    # any open GET /mcp stream to re-enumerate so cached tool schemas
    # get refreshed. ``initialize`` always goes through its own fresh-
    # session path below and is never counted as a revive. An empty /
    # missing header is a session-less client — no revive needed.
    revived_session_id: str | None = None
    if session_id and session_id not in _active_session_ids and method != "initialize":
        revived_session_id = uuid.uuid4().hex[:16]
        _active_session_ids.add(revived_session_id)
        _log.info(
            "MCP session_revived: worker=%s old_session=%s new_session=%s method=%s",
            worker_name,
            session_id,
            revived_session_id,
            method,
        )

    # Handle notifications (no id) — just acknowledge. A revive during a
    # notification is unusual but possible; echo the new session ID so
    # the client can pick it up.
    if msg_id is None:
        if method == "notifications/initialized":
            _log.info("MCP initialized: worker=%s session=%s", worker_name, session_id)
        notif_headers: dict[str, str] = {}
        if revived_session_id:
            notif_headers["Mcp-Session-Id"] = revived_session_id
        return web.Response(status=204, headers=notif_headers)

    result = _dispatch(request, worker_name, method, params)

    # Build JSON-RPC response
    rpc_response: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
    if isinstance(result, dict) and "error" in result:
        rpc_response["error"] = result["error"]
    else:
        rpc_response["result"] = result

    headers: dict[str, str] = {}
    if method == "initialize":
        new_session = uuid.uuid4().hex[:16]
        _active_session_ids.add(new_session)
        headers["Mcp-Session-Id"] = new_session
        _log.info("MCP session created: worker=%s session=%s", worker_name, new_session)
    elif revived_session_id:
        headers["Mcp-Session-Id"] = revived_session_id

    # Task #239: on auto-revive, respond via ``text/event-stream`` and
    # prepend a ``tools/list_changed`` notification BEFORE the JSON-RPC
    # response. This is the MCP Streamable HTTP spec's supported way of
    # piggybacking server-initiated messages on POST responses, and
    # crucially covers clients that don't maintain a persistent GET
    # /mcp stream (i.e. Claude Code's HTTP MCP transport in practice,
    # as surfaced in #226's operator repros). Still also broadcast to
    # any active GET-stream subscribers for the complete coverage set.
    if revived_session_id:
        try:
            await broadcast_tools_list_changed()
        except Exception:
            _log.debug("post-revive broadcast_tools_list_changed failed", exc_info=True)
        return await _respond_sse_with_list_changed(
            request,
            rpc_response,
            headers=headers,
            session_id=revived_session_id,
        )

    return web.json_response(rpc_response, headers=headers)


async def _respond_sse_with_list_changed(
    request: web.Request,
    rpc_response: dict[str, Any],
    *,
    headers: dict[str, str],
    session_id: str,
) -> web.StreamResponse:
    """Stream a POST response as ``text/event-stream`` with notification + rpc.

    Per MCP Streamable HTTP spec, a POST response MAY be an SSE stream
    containing one or more JSON-RPC messages. We send:
      1. ``notifications/tools/list_changed`` (re-enumerate nudge)
      2. The actual response to the original request

    Clients that process the notification before the response will
    re-fetch ``tools/list`` and pick up any schema drift from the
    pre-revive session before consuming the response payload.
    """
    response_headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    }
    response_headers.update(headers)
    response = web.StreamResponse(status=200, reason="OK", headers=response_headers)
    await response.prepare(request)
    try:
        notification = json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        await _send_sse(response, "message", notification)
        _log.info(
            "[mcp] list_changed_sent session=%s transport=http-post-piggyback",
            session_id,
        )
        await _send_sse(response, "message", json.dumps(rpc_response))
    except Exception:
        _log.debug("SSE-piggyback write failed", exc_info=True)
    return response


async def handle_streamable_sse(request: web.Request) -> web.StreamResponse:
    """GET /mcp — SSE stream for server-initiated messages (Streamable HTTP).

    As soon as the stream opens we push one ``notifications/tools/list_changed``
    JSON-RPC notification. If the client connected after a daemon reload,
    any schema it cached from the previous process is now stale; receiving
    this notification makes the client re-call ``tools/list`` without
    needing its host session to restart.

    The response is also registered in ``_broadcast_subscribers`` so
    :func:`broadcast_tools_list_changed` can push additional
    notifications to it later — the hook future hot-reload code will
    use to notify already-connected clients without making them
    reconnect.
    """
    response = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await response.prepare(request)

    _broadcast_subscribers.add(response)
    try:
        try:
            await _push_tools_list_changed(response)
        except Exception:
            _log.debug("streamable SSE: list_changed push failed", exc_info=True)

        # Hold the handler open until the client disconnects so
        # ``broadcast_tools_list_changed`` can push additional
        # notifications on this same stream. ``request.content`` is
        # useless for a body-less GET (EOFs immediately in some
        # transport implementations), so poll the underlying transport
        # instead. Broken by aiohttp cancelling the handler on
        # disconnect — caught and translated to a clean exit.
        try:
            while True:
                transport = request.transport
                if transport is None or transport.is_closing():
                    break
                await asyncio.sleep(_SSE_KEEPALIVE_POLL)
        except asyncio.CancelledError:
            _log.debug("streamable SSE: handler cancelled (client disconnect)")
            raise
        except Exception:
            _log.debug("streamable SSE stream ended", exc_info=True)
    finally:
        _broadcast_subscribers.discard(response)

    return response


async def handle_streamable_delete(request: web.Request) -> web.Response:
    """DELETE /mcp — session teardown (Streamable HTTP).

    Removes the session ID from the active set so a subsequent request
    with the same ID will be rejected with 404. A client that sends
    DELETE is explicitly disposing of its session; honouring the
    termination lets the paired 404-on-stale-ID path stay consistent.
    """
    session_id = request.headers.get("Mcp-Session-Id", "")
    if session_id:
        _active_session_ids.discard(session_id)
    return web.Response(status=204)


# ---------------------------------------------------------------------------
# Legacy SSE transport
# ---------------------------------------------------------------------------

# Active legacy SSE connections: session_id → (worker_name, response).
# Used by the legacy SSE transport to route POSTed JSON-RPC messages back
# to the right client stream.
_sessions: dict[str, tuple[str, web.StreamResponse]] = {}

# Every currently-open SSE response that should receive server-initiated
# messages — both the Streamable HTTP GET /mcp and the legacy GET /mcp/sse
# register here. This is the set ``broadcast_tools_list_changed()``
# iterates. A separate, transport-agnostic collection so callers don't
# have to care which transport a session is using.
_broadcast_subscribers: set[web.StreamResponse] = set()

# Active Streamable HTTP session IDs issued by this daemon process.
# Kept in-process (not persisted) so an ``os.execv`` reload wipes every
# ID the previous process minted — that's what signals "this client's
# session is stale" to the handler below.
#
# On an unknown ID we DON'T return 404 (spec §8.4's default). That was
# the task #226 behaviour and it broke Claude Code in the wild: the
# Queen's MCP session went fully isolated after a daemon reload because
# Claude Code's HTTP MCP transport didn't re-initialize on 404 —
# instead it kept re-sending the dead session ID and every tool call
# failed. Task #227 replaces the reject with auto-revive: mint a new
# session ID, bind it to the incoming request, process the original
# call normally, and return the new ID in the ``Mcp-Session-Id``
# header. The paired ``broadcast_tools_list_changed()`` push tells any
# open GET /mcp stream to re-enumerate tools, so cached schemas from
# the pre-reload daemon get refreshed without a client restart.
_active_session_ids: set[str] = set()


async def handle_sse(request: web.Request) -> web.StreamResponse:
    """Legacy SSE endpoint — persistent connection for server→client msgs."""
    worker_name = (
        request.rel_url.query.get("worker") or request.headers.get("X-Swarm-Worker") or "unknown"
    )
    session_id = uuid.uuid4().hex[:16]

    response = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Swarm-Session": session_id,
        },
    )
    await response.prepare(request)

    # Register session BEFORE sending the endpoint event — the client may
    # POST immediately after receiving the URL, causing a race if we register after.
    _sessions[session_id] = (worker_name, response)
    _broadcast_subscribers.add(response)

    # Send the endpoint URL as the first SSE event (MCP convention).
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    endpoint = f"{scheme}://{host}/mcp/message?session_id={session_id}"
    await _send_sse(response, "endpoint", endpoint)

    # Nudge the client to re-fetch tools/list. Cheap for clients already
    # in sync (one redundant fetch); essential for clients whose cached
    # schema is from before a daemon reload.
    try:
        await _push_tools_list_changed(response)
    except Exception:
        _log.debug("legacy SSE: list_changed push failed", exc_info=True)

    _log.info("MCP SSE connected: worker=%s session=%s", worker_name, session_id)

    try:
        async for msg in request.content:
            pass
    except Exception:
        _log.debug("legacy SSE stream ended: worker=%s", worker_name, exc_info=True)
    finally:
        _sessions.pop(session_id, None)
        _broadcast_subscribers.discard(response)
        _log.info("MCP SSE disconnected: worker=%s", worker_name)

    return response


async def handle_message(request: web.Request) -> web.Response:
    """Handle a JSON-RPC message from the legacy MCP SSE client."""
    session_id = request.query.get("session_id", "")
    if not session_id or session_id not in _sessions:
        return web.json_response({"error": "invalid or missing session_id"}, status=400)

    worker_name, sse_response = _sessions[session_id]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    method = body.get("method", "")
    msg_id = body.get("id")
    params = body.get("params", {})

    result = _dispatch(request, worker_name, method, params)

    rpc_response: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
    if isinstance(result, dict) and "error" in result:
        rpc_response["error"] = result["error"]
    else:
        rpc_response["result"] = result

    await _send_sse(sse_response, "message", json.dumps(rpc_response))
    return web.Response(status=202, text="Accepted")


# ---------------------------------------------------------------------------
# JSON-RPC method dispatch
# ---------------------------------------------------------------------------


def _dispatch(
    request: web.Request,
    worker_name: str,
    method: str,
    params: dict[str, Any],
) -> Any:
    """Dispatch a JSON-RPC method call."""
    from swarm.server.helpers import get_daemon

    daemon = get_daemon(request)

    # Track last MCP activity per worker for the IdleWatcher's
    # tools-dropped detection (task #257).  Skip the sentinel
    # "unknown" (used when neither the query param nor the header
    # identifies the worker) since it'd just aggregate noise.
    if worker_name and worker_name != "unknown":
        _worker_last_mcp_activity[worker_name] = time.time()

    if method == "initialize":
        return _handle_initialize()
    if method == "tools/list":
        return _handle_tools_list()
    if method == "tools/call":
        return _handle_tools_call(daemon, worker_name, params)
    if method == "ping":
        return {}

    return {"error": {"code": -32601, "message": f"Method not found: {method}"}}


def _handle_initialize() -> dict[str, Any]:
    # Advertise tools.listChanged so clients know they can subscribe to
    # server-initiated refresh notifications. When the daemon reloads, any
    # previously-cached schema on the client side goes stale (observed in
    # the wild: Claude Code sessions holding a pre-reload ``tools/list``
    # result even after the MCP connection cycled). Pairing this with the
    # SSE-connect push of ``notifications/tools/list_changed`` below lets
    # conformant clients re-fetch without restarting their session.
    return {
        "protocolVersion": _PROTOCOL_VERSION,
        "serverInfo": {"name": _SERVER_NAME, "version": _SERVER_VERSION},
        "capabilities": {"tools": {"listChanged": True}},
    }


def _handle_tools_list() -> dict[str, Any]:
    return {"tools": TOOLS}


def _handle_tools_call(
    daemon: SwarmDaemon,
    worker_name: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    tool_name = params.get("name", "")
    arguments = params.get("arguments", {})
    raw = handle_tool_call(daemon, worker_name, tool_name, arguments)
    # Phase 3: handlers may return either a bare content list (legacy)
    # or a dict wrapper with ``content`` + optional ``structuredContent``
    # / ``_meta``. Surface ``structuredContent`` and ``_meta`` only when
    # the handler explicitly opted in — older clients that don't read
    # them then see exactly the prior payload shape.
    if isinstance(raw, dict):
        envelope: dict[str, Any] = {"content": raw.get("content") or []}
        if raw.get("structuredContent") is not None:
            envelope["structuredContent"] = raw["structuredContent"]
        if raw.get("_meta") is not None:
            envelope["_meta"] = raw["_meta"]
        return envelope
    return {"content": raw}


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


async def _send_sse(response: web.StreamResponse, event: str, data: str) -> None:
    """Send a single SSE event."""
    payload = f"event: {event}\ndata: {data}\n\n"
    await response.write(payload.encode("utf-8"))


async def _push_tools_list_changed(
    response: web.StreamResponse,
    *,
    transport: str = "sse-get",
    session_id: str = "",
) -> None:
    """Push an MCP ``notifications/tools/list_changed`` JSON-RPC message.

    Sent once per SSE connection open and again any time the registry
    mutates (via :func:`broadcast_tools_list_changed`). Clients that
    subscribe to the stream after a daemon reload get prompted to
    re-fetch ``tools/list`` instead of silently serving their stale
    cache (the exact failure mode that hid task #169's schema change
    until the operator reconnected).

    ``transport`` / ``session_id`` are logged at INFO so future
    propagation-gap debugging (task #239) has concrete data: every
    delivery attempt shows up as
    ``[mcp] list_changed_sent session=<id> transport=<kind>``.
    """
    notification = json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    await _send_sse(response, "message", notification)
    _log.info(
        "[mcp] list_changed_sent session=%s transport=%s",
        session_id or "<unknown>",
        transport,
    )


async def broadcast_tools_list_changed() -> int:
    """Push ``notifications/tools/list_changed`` to every connected MCP client.

    Complements the "push on connect" behaviour: that handles clients
    joining AFTER a registry change; this handles clients already
    subscribed when the registry changes at runtime. Today swarm's
    ``TOOLS`` is built at import time, so the main caller is
    :meth:`SwarmDaemon.start` right after daemon startup — a defensive
    broadcast for any session that raced connect with tool registration.
    Future hot-reload-of-tools paths can call this whenever they mutate
    the registry.

    Returns the number of subscribers that successfully received the
    notification. Dead / closed streams are pruned from
    ``_broadcast_subscribers`` in place so repeat calls don't keep
    retrying them.
    """
    if not _broadcast_subscribers:
        return 0
    sent = 0
    dead: list[web.StreamResponse] = []
    for response in list(_broadcast_subscribers):
        try:
            await _push_tools_list_changed(response)
        except Exception:
            _log.debug("broadcast list_changed: push failed, pruning", exc_info=True)
            dead.append(response)
            continue
        sent += 1
    for response in dead:
        _broadcast_subscribers.discard(response)
    if sent:
        _log.info("broadcast list_changed to %d MCP subscriber(s)", sent)
    return sent
