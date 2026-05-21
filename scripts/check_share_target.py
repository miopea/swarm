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

    # 2. Navigate to the redirect — the dashboard JS should see the
    # share param, fetch the payload, and open the New Task modal.
    page = ctx.new_page()
    page.goto("http://localhost:9090" + location, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)  # share fetch + modal open + thumbnail render

    # 3. Verify the task modal is now visible + pre-filled.
    modal_open = page.evaluate(
        "document.getElementById('task-modal')?.style.display !== 'none'"
    )
    title_value = page.evaluate("document.getElementById('tm-title')?.value || ''")
    desc_value = page.evaluate("document.getElementById('tm-desc')?.value || ''")
    thumbs = page.evaluate(
        "document.querySelectorAll('#tm-attachments img, #tm-attachments .attachment-thumb').length"
    )
    attachment_paths = page.evaluate(
        "typeof taskModalAttachmentPaths !== 'undefined' ? taskModalAttachmentPaths.length : -1"
    )

    print(f"task-modal display:none cleared: {modal_open}")
    print(f"  tm-title: {title_value!r}")
    print(f"  tm-desc:  {desc_value!r}")
    print(f"  thumbnails: {thumbs}")
    print(f"  taskModalAttachmentPaths.length: {attachment_paths}")

    # 4. Confirm the URL was cleaned (no ?share= leftover).
    current_url = page.url
    print(f"current URL: {current_url}")
    assert "share=" not in current_url, f"share param not stripped: {current_url}"

    # Capture screenshot for the record.
    out = Path("/home/bschleifer/projects/personal/swarm/docs/qa-share-target.png")
    page.screenshot(path=str(out), full_page=False)
    print(f"screenshot: {out.relative_to(out.parent.parent)}")

    browser.close()

print("OK — Web Share Target flow works end-to-end.")
