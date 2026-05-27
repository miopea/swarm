"""SwarmClient — connects to the daemon API for remote control."""

from __future__ import annotations

import inspect
import json
import urllib.parse
from collections.abc import Callable
from typing import Any

import aiohttp

from swarm.logging import get_logger

_log = get_logger("client")


_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=10)


class SwarmClient:
    """Client for the swarm daemon REST + WebSocket API."""

    def __init__(
        self, base_url: str = "http://localhost:9090", password: str | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._password = password
        ws_base = self.base_url.replace("http", "ws", 1) + "/ws"
        self.ws_url = f"{ws_base}?token={password}" if password else ws_base
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._on_message: list[Callable[[dict[str, Any]], None]] = []

    def _headers(self) -> dict[str, str]:
        """Build common headers for REST requests."""
        headers: dict[str, str] = {"X-Requested-With": "SwarmClient"}
        if self._password:
            headers["Authorization"] = f"Bearer {self._password}"
        return headers

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=_DEFAULT_TIMEOUT,
                headers=self._headers(),
            )
        return self._session

    async def close(self) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # --- REST API ---

    async def health(self) -> dict[str, Any]:
        session = await self._get_session()
        async with session.get(f"{self.base_url}/api/health") as resp:
            resp.raise_for_status()
            return await resp.json()

    async def workers(self) -> list[dict[str, Any]]:
        session = await self._get_session()
        async with session.get(f"{self.base_url}/api/workers") as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("workers", [])

    def _worker_url(self, name: str, endpoint: str = "") -> str:
        """Build a worker API URL with proper name encoding."""
        encoded = urllib.parse.quote(name, safe="")
        return f"{self.base_url}/api/workers/{encoded}{endpoint}"

    async def worker_detail(self, name: str) -> dict[str, Any]:
        session = await self._get_session()
        async with session.get(self._worker_url(name)) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def send_message(self, worker_name: str, message: str) -> dict[str, Any]:
        session = await self._get_session()
        async with session.post(
            self._worker_url(worker_name, "/send"),
            json={"message": message},
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def continue_worker(self, worker_name: str) -> dict[str, Any]:
        session = await self._get_session()
        async with session.post(self._worker_url(worker_name, "/continue")) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def kill_worker(self, worker_name: str) -> dict[str, Any]:
        session = await self._get_session()
        async with session.post(self._worker_url(worker_name, "/kill")) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def drone_log(self, limit: int = 50) -> list[dict[str, Any]]:
        session = await self._get_session()
        async with session.get(f"{self.base_url}/api/drones/log", params={"limit": limit}) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("entries", [])

    async def toggle_drones(self) -> dict[str, Any]:
        session = await self._get_session()
        async with session.post(f"{self.base_url}/api/drones/toggle") as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_tasks(self) -> list[dict[str, Any]]:
        session = await self._get_session()
        async with session.get(f"{self.base_url}/api/tasks") as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("tasks", [])

    async def create_task(
        self,
        title: str,
        description: str = "",
        priority: str = "normal",
    ) -> dict[str, Any]:
        session = await self._get_session()
        async with session.post(
            f"{self.base_url}/api/tasks",
            json={"title": title, "description": description, "priority": priority},
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def assign_task(self, task_id: str, worker: str) -> dict:
        session = await self._get_session()
        async with session.post(
            f"{self.base_url}/api/tasks/{task_id}/assign",
            json={"worker": worker},
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    # --- WebSocket ---

    def on_message(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._on_message.append(callback)

    async def connect_ws(self) -> None:
        """Connect to the WebSocket and listen for events."""
        session = await self._get_session()
        self._ws = await session.ws_connect(self.ws_url)
        # Mask token in log output to avoid leaking credentials
        safe_url = self.ws_url.split("?")[0] if "?" in self.ws_url else self.ws_url
        _log.info("WebSocket connected to %s", safe_url)

        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    for cb in self._on_message:
                        result = cb(data)
                        if inspect.isawaitable(result):
                            await result
                except json.JSONDecodeError:
                    _log.warning("invalid JSON from WebSocket: %s", msg.data[:100])
            elif msg.type == aiohttp.WSMsgType.ERROR:
                _log.warning("WebSocket error: %s", self._ws.exception())
                break

    async def is_daemon_running(self) -> bool:
        """Check if the daemon is reachable."""
        try:
            await self.health()
            return True
        except Exception:
            # Probe contract: False == not reachable. Logged at debug so a
            # confused operator can trace whether the failure was a real
            # connection error vs. health-endpoint protocol mismatch.
            _log.debug("is_daemon_running probe failed", exc_info=True)
            return False
