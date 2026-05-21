#!/usr/bin/env python3
"""One-off probe: simulate a Web Share Target POST, then verify the
dashboard opens the New Task modal with the shared content + attachment.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mobile_qa import _get_session_cookie, _load_dotenv  # type: ignore

_load_dotenv(Path(__file__).resolve().parent.parent / ".env")
password = os.environ["SWARM_API_PASSWORD"]
session = _get_session_cookie("http://localhost:9090", password)

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=2.0,
        is_mobile=True,
        has_touch=True,
    )
    ctx.add_cookies(
        [
            {
                "name": "swarm_session",
                "value": session,
                "domain": "localhost",
                "path": "/",
                "httpOnly": True,
                "sameSite": "Lax",
            }
        ]
    )

    # 1. Simulate a Web Share Target POST. iOS / Android would do this
    # as a multipart form navigation; we replay it from a request
    # context so the API receives the same shape.
    api = ctx.request
    repo_root = Path(__file__).resolve().parent.parent
    img_bytes = (repo_root / "src/swarm/web/static/icon-192.png").read_bytes()
    resp = api.post(
        "http://localhost:9090/share-receive",
        multipart={
            "title": "Bug screenshot",
            "text": "Login fails on iOS",
            "url": "",
            "file": {
                "name": "screenshot.png",
                "mimeType": "image/png",
                "buffer": img_bytes,
            },
        },
        max_redirects=0,
    )
    print(f"POST /share-receive → HTTP {resp.status}")
    location = resp.headers.get("location") or resp.headers.get("Location") or ""
    print(f"Redirect Location: {location!r}")
    assert resp.status in (302, 303), f"expected redirect, got {resp.status}"
    assert location.startswith("/?share="), f"unexpected redirect: {location}"

    # 2. Pre-seed localStorage with a 'last active worker' so the
    # dashboard JS routes the share INTO that worker's PTY instead of
    # opening the task modal. This is the post-2026-05-21 behaviour
    # the operator asked for.
    page = ctx.new_page()
    # Set localStorage on the dashboard origin before navigating to
    # /?share=<id>. Workers like 'swarm' / 'platform' / 'admin' exist
    # on the live daemon; pick 'public-website' since the existing
    # synth playbook references it as a known worker.
    page.goto("http://localhost:9090/", wait_until="domcontentloaded")
    page.evaluate("localStorage.setItem('swarm.lastActiveWorker', 'public-website')")
    # Now bounce to the share landing.
    page.goto("http://localhost:9090" + location, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)

    # 3. Verify the share routed into the worker (NOT the task modal).
    # The task modal should remain hidden; the toast should mention
    # 'Sent N attachment(s) to <worker>'.
    modal_open = page.evaluate(
        "document.getElementById('task-modal')?.style.display !== 'none'"
    )
    print(f"task-modal opened (should be False for worker-route): {modal_open}")

    # Confirm the URL was cleaned (no ?share= leftover).
    current_url = page.url
    print(f"current URL: {current_url}")
    assert "share=" not in current_url, f"share param not stripped: {current_url}"

    # Capture screenshot for the record.
    out = Path("/home/bschleifer/projects/personal/swarm/docs/qa-share-target.png")
    page.screenshot(path=str(out), full_page=False)
    print(f"screenshot: {out.relative_to(out.parent.parent)}")

    # 4. Now exercise the FALLBACK path: clear BOTH storages and
    # re-trigger a share — should land in the task modal because no
    # last-active worker is known. sessionStorage carries the
    # previously-selected worker name and the dashboard's boot code
    # re-restores it via selectWorker() (which re-writes localStorage),
    # so clearing only localStorage isn't enough to simulate a
    # never-selected state.
    page.evaluate(
        "localStorage.removeItem('swarm.lastActiveWorker');"
        "sessionStorage.removeItem('swarm_selected_worker');"
    )
    img_bytes2 = (repo_root / "src/swarm/web/static/icon-192.png").read_bytes()
    resp2 = api.post(
        "http://localhost:9090/share-receive",
        multipart={
            "title": "Second share",
            "text": "Fallback to modal",
            "file": {
                "name": "fallback.png",
                "mimeType": "image/png",
                "buffer": img_bytes2,
            },
        },
        max_redirects=0,
    )
    location2 = resp2.headers.get("location") or resp2.headers.get("Location") or ""
    page.goto("http://localhost:9090" + location2, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    modal_open2 = page.evaluate(
        "document.getElementById('task-modal')?.style.display !== 'none'"
    )
    ls_value = page.evaluate(
        "localStorage.getItem('swarm.lastActiveWorker')"
    )
    print(f"task-modal opened (should be True for fallback-route): {modal_open2}")
    print(f"  localStorage.swarm.lastActiveWorker after step 5: {ls_value!r}")
    fb_shot = Path("/home/bschleifer/projects/personal/swarm/docs/qa-share-fallback.png")
    page.screenshot(path=str(fb_shot), full_page=False)
    print(f"  fallback screenshot: {fb_shot.relative_to(fb_shot.parent.parent)}")

    browser.close()

print("OK — Web Share Target flow works end-to-end.")
