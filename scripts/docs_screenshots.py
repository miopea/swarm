#!/usr/bin/env python3
"""Generate README/docs screenshots from a throwaway, SEEDED demo dashboard.

The public docs screenshots must NEVER come from the live daemon (it holds
real, private project data). This harness instead spins up an **isolated**
in-process dashboard:

* ``HOME`` is redirected to a temp dir BEFORE any swarm import, so the demo
  ``SwarmDB`` opens ``<tmp>/.swarm/swarm.db`` — the real ``~/.swarm`` and the
  live ``:9090`` daemon are never touched.
* No ``api_password`` is set, so the session-auth middleware is skipped (no
  login dance) — see ``server/api.py`` ``_session_auth_middleware``.
* The daemon is CONSTRUCTED but never ``start()``-ed, so no worker PTYs spawn.
  Fake ``Worker`` rows + generic FAKE store data are seeded directly.

Captures the bottom-panel tabs that are hard to screenshot otherwise. Extend
``TABS`` to add more. Output overwrites ``docs/screenshots/<name>.png``.

    uv run python scripts/docs_screenshots.py

Run after adding a dashboard tab so the launch images stay current.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

# --- Isolation: redirect HOME before importing anything from swarm. --------- #
# Pin Playwright's browser cache to the REAL home first — otherwise the HOME
# redirect below sends it looking under the empty temp dir.
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(Path(os.path.expanduser("~")) / ".cache" / "ms-playwright"),
)
_TMP_HOME = tempfile.mkdtemp(prefix="swarm-demo-shots-")
os.environ["HOME"] = _TMP_HOME
os.environ.pop("SWARM_API_PASSWORD", None)  # ensure no-auth mode
(Path(_TMP_HOME) / ".swarm").mkdir(parents=True, exist_ok=True)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "docs" / "screenshots"

# (filename, tab data-tab value, settle-ms) — the bottom-panel tabs to capture.
TABS = [
    ("loops-tab", "loops", 1400),
    ("harness-tab", "harness", 1400),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _seed(daemon) -> None:
    """Populate the isolated daemon with generic FAKE demo data."""
    from swarm.drones.log import DroneAction, LogCategory
    from swarm.playbooks.models import Playbook, PlaybookStatus
    from swarm.worker.worker import Worker, WorkerState

    # A few resting workers so the chrome looks alive (no real processes).
    for name, path in [
        ("api-service", "/demo/api-service"),
        ("web-frontend", "/demo/web-frontend"),
        ("data-pipeline", "/demo/data-pipeline"),
    ]:
        daemon.workers.append(Worker(name=name, path=path, state=WorkerState.RESTING))

    # --- Standing loops (Loops tab) -------------------------------------- #
    daemon.standing_loop.start("api-service")
    daemon.standing_loop.record_burn("api-service", 84_000)  # mid-window burn
    daemon.standing_loop.start("web-frontend")
    daemon.standing_loop.pause("web-frontend")

    # --- Harness digest signals ----------------------------------------- #
    # Error-prone MCP tool (display-only suggestion): 12 calls, 4 errors.
    for i in range(12):
        snippet = "error: missing required field 'target_worker'" if i < 4 else f"ok #{i}"
        daemon.drone_log.add(
            DroneAction.CONTINUED,
            "api-service",
            f"mcp:swarm_create_task → {snippet}",
            category=LogCategory.MESSAGE,
        )
    # A healthy tool for contrast.
    for i in range(15):
        daemon.drone_log.add(
            DroneAction.CONTINUED,
            "web-frontend",
            f"mcp:swarm_check_messages → 2 unread #{i}",
            category=LogCategory.MESSAGE,
        )

    # Playbooks: one low-win-rate (retire, actionable) + one strong candidate
    # (promote, actionable).
    daemon.playbook_store.create(
        Playbook(
            name="retry-flaky-tests",
            title="Retry flaky tests up to 3x",
            status=PlaybookStatus.ACTIVE,
            uses=12,
            wins=2,
            losses=9,
        )
    )
    daemon.playbook_store.create(
        Playbook(
            name="grep-before-edit",
            title="Grep all call sites before changing a signature",
            status=PlaybookStatus.CANDIDATE,
            uses=6,
            wins=5,
            losses=1,
        )
    )

    # Dreamer-mined pattern (display-only).
    daemon.queen_chat.add_learning(
        context="Tasks touching the migration runner failed verification twice",
        correction="Auto-discovered by the dreamer: run `alembic check` before completing.",
        applied_to="discovered_by_dreamer:VERIFIER_TIER1_REOPENED:9f2a",
    )


def _run_server(app, port: int, ready: threading.Event) -> None:
    import asyncio

    from aiohttp import web

    async def _serve() -> None:
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        ready.set()
        while True:  # serve until the process exits
            await asyncio.sleep(3600)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_serve())


def _capture(base_url: str) -> int:
    from playwright.sync_api import sync_playwright

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1440, "height": 900}, device_scale_factor=2.0
        )
        page = context.new_page()
        page.goto(base_url, wait_until="domcontentloaded", timeout=20_000)
        page.wait_for_timeout(1500)  # WS init + first paint
        # Open the bottom panel once.
        page.evaluate("document.querySelector('[data-action=\"toggleBottomPanel\"]')?.click();")
        page.wait_for_timeout(400)
        for name, tab, settle in TABS:
            page.evaluate(f"document.querySelector('[data-tab=\"{tab}\"]')?.click();")
            page.wait_for_timeout(settle)  # tab switch fires fetch + render
            shot = OUT_DIR / f"{name}.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"  ✓ {shot.relative_to(REPO_ROOT)}")
        browser.close()
    return 0


def main() -> int:
    from swarm.config.models import HiveConfig
    from swarm.server.api import create_app
    from swarm.server.daemon import SwarmDaemon

    port = _free_port()
    config = HiveConfig(api_password="", port=port)
    daemon = SwarmDaemon(config)
    _seed(daemon)

    app = create_app(daemon)
    ready = threading.Event()
    threading.Thread(target=_run_server, args=(app, port, ready), daemon=True).start()
    if not ready.wait(timeout=20):
        print("ERROR: demo server did not start", file=sys.stderr)
        return 1
    time.sleep(0.5)

    base_url = f"http://127.0.0.1:{port}"
    print(f"[docs-shots] isolated demo dashboard at {base_url} (HOME={_TMP_HOME})")
    rc = _capture(base_url)
    print(f"[docs-shots] done — {len(TABS)} screenshots in {OUT_DIR.relative_to(REPO_ROOT)}/")
    return rc


if __name__ == "__main__":
    sys.exit(main())
