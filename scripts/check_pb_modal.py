#!/usr/bin/env python3
"""One-off probe: open the playbook detail modal, dump its rendered HTML
so I can see whether the Promote button is actually in the DOM."""

from __future__ import annotations

import os
import re
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
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    ctx.add_cookies([{
        "name": "swarm_session", "value": session,
        "domain": "localhost", "path": "/",
        "httpOnly": True, "sameSite": "Lax",
    }])
    page = ctx.new_page()
    page.goto("http://localhost:9090/", wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    page.evaluate("document.querySelector('[data-action=\"toggleBottomPanel\"]')?.click()")
    page.wait_for_timeout(400)
    page.evaluate("document.querySelector('[data-tab=\"playbooks\"]')?.click()")
    page.wait_for_timeout(1200)
    # Click first playbook title.
    page.evaluate("document.querySelector('.pb-playbook-row .task-title')?.click()")
    page.wait_for_timeout(1500)
    # Dump modal body innerHTML
    html = page.evaluate("document.getElementById('pb-events-body').innerHTML")
    print("=" * 60)
    print(html[:4000])
    print("=" * 60)
    # Look for the buttons specifically.
    promote = re.search(r'data-action-pb-modal="promote"', html)
    retire = re.search(r'data-action-pb-modal="retire"', html)
    print(f"Promote button present: {bool(promote)}")
    print(f"Retire button present:  {bool(retire)}")
    browser.close()
