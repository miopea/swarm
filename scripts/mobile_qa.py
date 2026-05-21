#!/usr/bin/env python3
"""Mobile QA harness — drive the live dashboard via Playwright.

Visits each P5/P6 touch point at iPhone-14-portrait viewport (390×844)
and captures screenshots into ``docs/qa-mobile-<timestamp>/`` so an
operator can see what the mobile dashboard actually looks like instead
of guessing from CSS reading.

Usage:
    uv run python scripts/mobile_qa.py
    uv run python scripts/mobile_qa.py --viewport 360x780
    uv run python scripts/mobile_qa.py --base-url http://localhost:9091

Requires ``SWARM_API_PASSWORD`` in ``.env`` (gitignored) so we can log
into the dashboard. The script POSTs to /login, captures the
``swarm_session`` cookie, then injects it into a Playwright browser
context so subsequent navigation is authenticated.

Out of scope: this is a one-shot QA tool, not a continuous test
suite. No assertions, just screenshots + a punch list. Re-run after
fixes to compare.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


def _load_dotenv(path: Path) -> None:
    """Lightweight .env loader — avoids pulling python-dotenv as a dep."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        os.environ.setdefault(key, value)


def _get_session_cookie(base_url: str, password: str) -> str:
    """POST /login, parse the swarm_session cookie out of the redirect.

    Uses stdlib urllib so the script has no runtime deps beyond
    Playwright itself (which is the actual point of the script).
    """
    import urllib.parse
    import urllib.request

    body = urllib.parse.urlencode({"password": password}).encode()
    req = urllib.request.Request(
        f"{base_url}/login",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    # Don't follow redirects — we want to read the Set-Cookie header
    # from the 302, not from whatever / returns.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        resp = opener.open(req, timeout=10)
        set_cookie = resp.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as exc:
        # 302 raises HTTPError when the redirect handler returns None.
        if exc.code != 302:
            raise
        set_cookie = exc.headers.get("Set-Cookie", "")
    match = re.search(r"swarm_session=([^;]+)", set_cookie)
    if not match:
        raise RuntimeError(
            f"login succeeded but no swarm_session cookie in response — "
            f"Set-Cookie was: {set_cookie!r}"
        )
    return match.group(1)


def _parse_viewport(spec: str) -> tuple[int, int]:
    match = re.match(r"^\s*(\d+)\s*[xX×]\s*(\d+)\s*$", spec)
    if not match:
        raise argparse.ArgumentTypeError(f"invalid viewport {spec!r} — expected e.g. 390x844")
    return int(match.group(1)), int(match.group(2))


# Touch points to capture. Each tuple is (label, path, optional setup
# function that runs after navigation but before the screenshot — e.g.
# clicking a tab button to reveal the target panel).
TOUCH_POINTS: list[tuple[str, str, str]] = [
    # (label, path, post-load JS to dispatch — empty string for no-op)
    ("01-command-center", "/", ""),
    (
        "02-cc-focus-attention",
        "/",
        # P5: ensure Attention pane is selected on mobile.
        "document.querySelector('[data-cc-focus=\"attention\"]')?.click();",
    ),
    (
        "03-cc-focus-queen",
        "/",
        "document.querySelector('[data-cc-focus=\"queen\"]')?.click();",
    ),
    (
        "04-bottom-panel-tasks",
        "/",
        # Open the bottom panel + switch to the Tasks tab so the mobile
        # task UI is on screen.
        "document.querySelector('[data-action=\"toggleBottomPanel\"]')?.click();"
        "setTimeout(() => document.querySelector('[data-tab=\"tasks\"]')?.click(), 200);",
    ),
    (
        "05-bottom-panel-pipelines",
        "/",
        "document.querySelector('[data-action=\"toggleBottomPanel\"]')?.click();"
        "setTimeout(() => document.querySelector('[data-tab=\"pipelines\"]')?.click(), 200);",
    ),
    (
        "06-bottom-panel-playbooks",
        "/",
        "document.querySelector('[data-action=\"toggleBottomPanel\"]')?.click();"
        "setTimeout(() => document.querySelector('[data-tab=\"playbooks\"]')?.click(), 200);",
    ),
    (
        "07-bottom-panel-activity",
        "/",
        "document.querySelector('[data-action=\"toggleBottomPanel\"]')?.click();"
        "setTimeout(() => document.querySelector('[data-tab=\"buzz\"]')?.click(), 200);",
    ),
    ("08-config-general", "/config", ""),
    (
        "09-config-playbooks",
        "/config",
        # P4b: switch to the automation tab (which co-displays playbooks).
        "document.querySelector('[data-tab=\"automation\"]')?.click();",
    ),
    (
        "10-playbook-detail-modal",
        "/",
        # Open the playbooks tab, then click the first playbook title to
        # open the detail modal. 2026-05-21 follow-up — verifies the
        # enriched modal (body + trigger + provenance + events).
        "document.querySelector('[data-action=\"toggleBottomPanel\"]')?.click();"
        "setTimeout(() => document.querySelector('[data-tab=\"playbooks\"]')?.click(), 200);"
        "setTimeout(() => document.querySelector('.pb-playbook-row .task-title')?.click(), 1200);",
    ),
    (
        "11-playbook-bulk-select",
        "/",
        # Open playbooks tab + flip bulk-select mode on + check the first
        # two rows so the bulk action bar shows '2 selected'.
        "document.querySelector('[data-action=\"toggleBottomPanel\"]')?.click();"
        "setTimeout(() => document.querySelector('[data-tab=\"playbooks\"]')?.click(), 200);"
        "setTimeout(() => document.getElementById('pb-bulk-toggle')?.click(), 1200);"
        "setTimeout(() => {"
        "  var cbs = document.querySelectorAll('.pb-row-cb');"
        "  if (cbs[0]) { cbs[0].click(); }"
        "  if (cbs[1]) { cbs[1].click(); }"
        "}, 1500);",
    ),
]


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    _load_dotenv(repo_root / ".env")

    parser = argparse.ArgumentParser(description="Drive the dashboard at mobile viewports.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SWARM_BASE_URL", "http://localhost:9090"),
        help="Dashboard base URL (default: $SWARM_BASE_URL or http://localhost:9090)",
    )
    parser.add_argument(
        "--viewport",
        type=_parse_viewport,
        default=(390, 844),
        help="Viewport WxH (default: 390x844, iPhone 14 portrait)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Where to save screenshots (default: docs/qa-mobile-<timestamp>/)",
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Don't close the browser at the end — useful for manual poking",
    )
    args = parser.parse_args()

    password = os.environ.get("SWARM_API_PASSWORD")
    if not password:
        print("ERROR: SWARM_API_PASSWORD not set (check .env)", file=sys.stderr)
        return 2

    parsed = urlparse(args.base_url)
    host = parsed.hostname or "localhost"

    print(f"[mobile-qa] Logging in to {args.base_url} …")
    session = _get_session_cookie(args.base_url, password)
    print("[mobile-qa] Got session cookie.")

    out_dir = (
        Path(args.output_dir)
        if args.output_dir
        else repo_root / "docs" / f"qa-mobile-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[mobile-qa] Saving to {out_dir.relative_to(repo_root)}")

    from playwright.sync_api import sync_playwright

    width, height = args.viewport
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.keep_open)
        context = browser.new_context(
            viewport={"width": width, "height": height},
            device_scale_factor=2.0,
            is_mobile=True,
            has_touch=True,
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/16.0 Mobile/15E148 Safari/604.1"
            ),
        )
        context.add_cookies(
            [
                {
                    "name": "swarm_session",
                    "value": session,
                    "domain": host,
                    "path": "/",
                    "httpOnly": True,
                    "sameSite": "Lax",
                }
            ]
        )

        # JS console + page errors get streamed to stdout so the operator
        # sees them alongside the screenshots — easier to triage than
        # silent screenshots that "look fine."
        console_errors: list[tuple[str, str, str]] = []  # (label, type, text)
        current_label = {"value": "boot"}

        def _on_console(msg) -> None:  # type: ignore[no-untyped-def]
            if msg.type in ("error", "warning"):
                console_errors.append((current_label["value"], msg.type, msg.text))

        def _on_pageerror(exc) -> None:  # type: ignore[no-untyped-def]
            console_errors.append((current_label["value"], "pageerror", str(exc)))

        page = context.new_page()
        page.on("console", _on_console)
        page.on("pageerror", _on_pageerror)

        for label, path, post_js in TOUCH_POINTS:
            current_label["value"] = label
            url = args.base_url.rstrip("/") + path
            print(f"[mobile-qa] {label}: {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15_000)
            except Exception as exc:
                print(f"  ! goto failed: {exc}", file=sys.stderr)
                continue
            # Wipe localStorage so cross-capture state (e.g. P5's
            # `swarm.cc.mobileFocus`) doesn't leak from a prior touch
            # point into the next. Without this, the focus toggle clicks
            # were sticky across screenshots and the bottom-panel
            # captures all showed the Command Center.
            try:
                page.evaluate("localStorage.clear(); sessionStorage.clear();")
            except Exception:
                pass
            # Reload after the wipe so the dashboard re-initialises from
            # a clean storage state. domcontentloaded is enough — we
            # already waited above.
            page.reload(wait_until="domcontentloaded", timeout=15_000)
            # Give the dashboard a tick to finish its WS init + initial
            # paint. The dashboard does a lot of async work on load.
            page.wait_for_timeout(1200)
            if post_js:
                try:
                    page.evaluate(post_js)
                except Exception as exc:
                    print(f"  ! post-js failed: {exc}", file=sys.stderr)
                # Bottom-panel slide-up animations + tab content fetches
                # need real time. 1500ms covers the worst case I observed.
                page.wait_for_timeout(1500)
            shot = out_dir / f"{label}.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"  ✓ {shot.relative_to(repo_root)}")

        # Write a punch-list scaffolding markdown so the operator (or
        # the next agent) has a place to record findings.
        notes_path = out_dir / "FINDINGS.md"
        notes_path.write_text(
            "# Mobile QA Findings\n\n"
            f"Date: {datetime.now().isoformat()}\n"
            f"Viewport: {width}×{height}\n"
            f"User agent: iPhone OS 16 Safari (synthetic)\n\n"
            "## Screenshots\n\n"
            + "\n".join(
                f"- `{label}.png` — {label.split('-', 1)[1].replace('-', ' ')}"
                for label, _, _ in TOUCH_POINTS
            )
            + "\n\n## Console errors / warnings captured during the run\n\n"
            + (
                "\n".join(f"- **{lbl}** [{typ}] {txt}" for lbl, typ, txt in console_errors)
                if console_errors
                else "_(none)_"
            )
            + "\n\n## Issues observed (fill in)\n\n"
            "Format: one bullet per issue, prefix with the touch-point label.\n"
        )
        print(f"[mobile-qa] FINDINGS.md scaffolded at {notes_path.relative_to(repo_root)}")

        if args.keep_open:
            input("[mobile-qa] Browser open — press Enter to close…")
        browser.close()

    print(f"[mobile-qa] Done. {len(TOUCH_POINTS)} screenshots in {out_dir.relative_to(repo_root)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
