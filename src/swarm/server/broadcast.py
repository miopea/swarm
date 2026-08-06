"""BroadcastHub — WebSocket broadcast, debounce, and client management."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from aiohttp import web

from swarm.logging import get_logger
from swarm.server.task_utils import log_task_exception as _log_task_exception

_log = get_logger("server.broadcast")

_WS_JANITOR_INTERVAL = 120  # seconds — safety-net cull


class BroadcastHub:
    """Manages WebSocket client sets, debounced broadcasting, and janitor loop."""

    # High-frequency broadcast types that benefit from debouncing.
    _DEBOUNCE_TYPES: frozenset[str] = frozenset(
        {
            "resources",
            "worker_changed",
            "tasks_changed",
            "queen_queue",
        }
    )
    _DEBOUNCE_DELAY: float = 0.1  # 100ms

    def __init__(
        self,
        *,
        track_task: Callable[[asyncio.Task[object]], None],
    ) -> None:
        self.ws_clients: set[web.WebSocketResponse] = set()
        self.terminal_ws_clients: set[web.WebSocketResponse] = set()
        # Hook for intercepting WS broadcasts (used by test runner)
        self._broadcast_hook: Callable[[dict[str, Any]], None] | None = None
        # Debounce: coalesce same-type broadcasts within 100ms
        # The LOOP is stored alongside the handle deliberately. A TimerHandle does not
        # expose the loop it was scheduled on through any public API, and without that
        # we cannot tell a live debounce from one stranded on a loop that has since
        # stopped — which is the difference between a working task panel and one that
        # only updates when the operator clicks something. See broadcast().
        self._broadcast_pending: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.TimerHandle]]
        self._broadcast_pending = {}
        self._broadcast_latest: dict[str, dict[str, Any]] = {}
        self._track_task = track_task

    def broadcast(self, data: dict[str, Any]) -> None:
        """Send a message to all connected WebSocket clients.

        High-frequency message types are debounced (100ms) so that rapid-fire
        updates coalesce into a single send with the latest data.
        """
        if self._broadcast_hook is not None:
            self._broadcast_hook(data)

        msg_type = data.get("type", "")
        if msg_type in self._DEBOUNCE_TYPES:
            # Store latest payload; schedule flush if not already pending
            self._broadcast_latest[msg_type] = data
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No loop: the frame is gone. Silent in CLI/test contexts is
                # correct (nobody is listening), but dropping a frame that
                # CONNECTED CLIENTS were owed is the stale-dashboard shape —
                # the mutation is real and durable and nothing can see it —
                # and it left no forensic anchor at all. Operators run at
                # default WARNING, so this is the level that reaches them.
                if self.ws_clients:
                    _log.warning(
                        "dropped %r broadcast owed to %d client(s): no running "
                        "event loop on this call path",
                        msg_type,
                        len(self.ws_clients),
                        stack_info=True,
                    )
                return

            # THE LATCH THIS GUARDS AGAINST, because it is not obvious and it is
            # permanent. The only thing that ever removed an entry from
            # _broadcast_pending was _flush_broadcast, which runs when the handle
            # FIRES. Schedule one on a loop that stops before the 100ms elapses and
            # the entry is never removed, so every later broadcast of that type found
            # the key present, scheduled nothing and returned. The frame type was then
            # dead for the whole life of the process — no exception, no warning, and
            # not even the RuntimeError branch above, because there IS a running loop.
            #
            # The operator saw exactly that: tasks_changed and worker_changed are
            # debounced while his Activity events are not, so those kept arriving over
            # the same live socket while the task panel never updated once. Clicking a
            # filter chip issues a plain HTTP fetch that never touches this hub, which
            # is why the board was always right the instant he interacted and never
            # before.
            pending = self._broadcast_pending.get(msg_type)
            if pending is not None and not self._pending_is_live(pending, loop):
                pending[1].cancel()
                del self._broadcast_pending[msg_type]
                pending = None
                # Loud, because a stranded debounce means frames of this type have
                # been silently swallowed since it was stranded.
                _log.warning(
                    "discarding a stranded %r debounce scheduled on a loop that is no "
                    "longer running; frames of this type were being dropped silently",
                    msg_type,
                    stack_info=True,
                )
            if pending is None:
                handle = loop.call_later(
                    self._DEBOUNCE_DELAY,
                    self._flush_broadcast,
                    msg_type,
                )
                self._broadcast_pending[msg_type] = (loop, handle)
            return

        self._send_ws_now(data)

    @staticmethod
    def _pending_is_live(
        pending: tuple[asyncio.AbstractEventLoop, asyncio.TimerHandle],
        loop: asyncio.AbstractEventLoop,
    ) -> bool:
        """Can this pending debounce still fire and clear itself?

        Only if it belongs to the loop we are on now, that loop is open, and the handle
        was not cancelled. Anything else is stranded: it will never run _flush_broadcast,
        so it will never remove its own key.
        """
        prev_loop, handle = pending
        return prev_loop is loop and not prev_loop.is_closed() and not handle.cancelled()

    def _flush_broadcast(self, msg_type: str) -> None:
        """Flush a debounced broadcast for *msg_type*."""
        self._broadcast_pending.pop(msg_type, None)
        data = self._broadcast_latest.pop(msg_type, None)
        if data is not None:
            self._send_ws_now(data)

    def _send_ws_now(self, data: dict[str, Any]) -> None:
        """Immediately send *data* to all connected WebSocket clients."""
        if not self.ws_clients:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # Reached only when ws_clients is non-empty (checked above), so a
            # real frame is being dropped — see the sibling warning in
            # broadcast(). Never silent here.
            _log.warning(
                "dropped %r broadcast owed to %d client(s): no running event "
                "loop on this call path",
                data.get("type", ""),
                len(self.ws_clients),
                stack_info=True,
            )
            return
        # Pre-filter closed clients synchronously
        dead: list[web.WebSocketResponse] = [ws for ws in self.ws_clients if ws.closed]
        for ws in dead:
            self.ws_clients.discard(ws)
        if not self.ws_clients:
            return
        # Single task gathers all sends and cleans up failures inline.
        # json.dumps happens inside the task so encoding doesn't block the
        # scheduling call path (noticeable for large state payloads).
        clients = list(self.ws_clients)

        async def _broadcast_all() -> None:
            payload = json.dumps(data)
            send_dead: list[web.WebSocketResponse] = []
            await asyncio.gather(
                *(self._safe_ws_send(ws, payload, send_dead) for ws in clients),
                return_exceptions=True,
            )
            for ws in send_dead:
                self.ws_clients.discard(ws)

        task = asyncio.create_task(_broadcast_all())
        task.add_done_callback(_log_task_exception)
        self._track_task(task)

    @staticmethod
    async def _safe_ws_send(
        ws: web.WebSocketResponse, payload: str, dead: list[web.WebSocketResponse]
    ) -> None:
        """Send a WS message, catching exceptions and discarding dead clients.

        Enforces a 5-second timeout to prevent a slow/hung client from
        stalling the broadcast loop.
        """
        try:
            await asyncio.wait_for(ws.send_str(payload), timeout=5.0)
        except Exception:  # broad catch: WS errors + TimeoutError are unpredictable
            _log.warning("WebSocket send failed, marking client as dead")
            dead.append(ws)

    async def ws_janitor_loop(self) -> None:
        """Periodically cull dead WebSocket clients."""
        try:
            while True:
                await asyncio.sleep(_WS_JANITOR_INTERVAL)
                dead = [ws for ws in self.ws_clients if ws.closed]
                if dead:
                    for ws in dead:
                        self.ws_clients.discard(ws)
                    _log.debug("ws janitor culled %d stale client(s)", len(dead))
        except asyncio.CancelledError:
            return

    @staticmethod
    async def close_ws_set(clients: set[web.WebSocketResponse]) -> None:
        """Close all WebSocket connections in a set, ignoring errors."""
        for ws in list(clients):
            try:
                await ws.close()
            except Exception:  # broad catch: cleanup must not raise
                pass
        clients.clear()
