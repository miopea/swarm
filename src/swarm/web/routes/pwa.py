"""PWA routes: bee icon, service worker, offline page, manifest, share-target."""

from __future__ import annotations

import time
import uuid

from aiohttp import web

from swarm.logging import get_logger
from swarm.server.helpers import get_daemon, json_error
from swarm.web.app import STATIC_DIR

_log = get_logger("web.pwa")

# In-process share-target cache. Each share gets a short-lived entry
# keyed by a UUID; the operator's browser claims it via GET /share/<id>
# after the OS redirect lands on /?share=<id>. Entries auto-expire
# at the 5-minute mark — long enough for a slow connection to redirect
# + load the dashboard, short enough that an interrupted share doesn't
# linger as orphaned bytes.
_SHARE_CACHE: dict[str, dict[str, object]] = {}
_SHARE_TTL_SECONDS = 300.0


def _prune_share_cache() -> None:
    """Drop share entries older than the TTL. Cheap; runs on every read."""
    cutoff = time.time() - _SHARE_TTL_SECONDS
    for key in [k for k, v in _SHARE_CACHE.items() if float(v.get("ts", 0)) < cutoff]:
        _SHARE_CACHE.pop(key, None)


async def handle_bee_icon(request: web.Request) -> web.Response:
    """Serve the bee icon SVG with caching."""
    static = request.app.get("static_dir", STATIC_DIR)
    return web.FileResponse(
        static / "bee-icon.svg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def handle_service_worker(request: web.Request) -> web.Response:
    """Serve sw.js from root path (service workers need root scope)."""
    static = request.app.get("static_dir", STATIC_DIR)
    return web.FileResponse(
        static / "sw.js",
        headers={"Content-Type": "application/javascript", "Cache-Control": "no-cache"},
    )


async def handle_offline_page(request: web.Request) -> web.Response:
    """Serve the PWA offline fallback page."""
    static = request.app.get("static_dir", STATIC_DIR)
    return web.FileResponse(static / "offline.html")


async def handle_manifest(request: web.Request) -> web.Response:
    """PWA manifest for add-to-homescreen + Web Share Target support."""
    manifest = {
        "name": "Swarm",
        "short_name": "Swarm",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#2A1B0E",
        "theme_color": "#D8A03D",
        "icons": [
            {
                "src": "/static/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable",
            },
            {
                "src": "/static/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
        ],
        # Web Share Target API: when this PWA is installed (homescreen),
        # "Swarm" appears in the OS share sheet. iOS Safari ≥ 16.4 and
        # Android Chrome both support it. Shared payload lands at
        # /share-receive; server redirects to /?share=<id>; dashboard
        # JS opens the New Task modal pre-filled with the file as an
        # attachment.
        "share_target": {
            "action": "/share-receive",
            "method": "POST",
            "enctype": "multipart/form-data",
            "params": {
                "title": "title",
                "text": "text",
                "url": "url",
                "files": [
                    {
                        "name": "file",
                        "accept": ["image/*", "text/*", "application/pdf"],
                    }
                ],
            },
        },
    }
    return web.json_response(manifest)


async def handle_share_receive(request: web.Request) -> web.Response:
    """Web Share Target landing — accept the OS share-sheet POST.

    The PWA manifest declares this URL as the share target with
    method=POST and enctype=multipart/form-data. iOS / Android send a
    multipart with optional `title` / `text` / `url` text fields and
    a `file` field for shared attachments (screenshots being the
    primary case). We save each file via the existing
    daemon.save_attachment path (lands in ~/.swarm/uploads/), stash
    the share metadata in the in-process cache, and 303-redirect to
    the dashboard with the share id. The dashboard JS picks up the
    `?share=<id>` query param and opens the New Task modal pre-filled.
    """
    d = get_daemon(request)
    title = ""
    text = ""
    url = ""
    file_paths: list[str] = []
    file_names: list[str] = []
    try:
        reader = await request.multipart()
        async for field in reader:
            name = field.name or ""
            if name == "title":
                title = (await field.text()).strip()
            elif name == "text":
                text = (await field.text()).strip()
            elif name == "url":
                url = (await field.text()).strip()
            elif name == "file":
                filename = field.filename or "shared.bin"
                # save_attachment streams to disk + returns the path.
                # Hand it the raw bytes since the existing API is
                # bytes-based (small attachments only — a phone
                # screenshot is fine; arbitrarily large files would
                # need a streaming variant we don't have today).
                data = await field.read(decode=True)
                path = d.save_attachment(filename, data)
                file_paths.append(path)
                file_names.append(filename)
    except Exception:
        _log.warning("share-target multipart parse failed", exc_info=True)
        return json_error("share payload could not be parsed", 400)

    share_id = uuid.uuid4().hex[:12]
    _SHARE_CACHE[share_id] = {
        "title": title,
        "text": text,
        "url": url,
        "files": file_paths,
        "filenames": file_names,
        "ts": time.time(),
    }
    _log.info(
        "share-target: captured %d file(s), title=%r, redirecting to dashboard",
        len(file_paths),
        title,
    )
    # 303 See Other so the browser switches to GET on the dashboard
    # (rather than POSTing again).
    raise web.HTTPSeeOther(location=f"/?share={share_id}")


async def handle_share_get(request: web.Request) -> web.Response:
    """Return the share-target payload for the dashboard JS to consume.

    Single-shot: deleted from the cache after the first successful
    read so a shared screenshot can't be re-claimed by a second tab.
    """
    _prune_share_cache()
    share_id = request.match_info["share_id"]
    entry = _SHARE_CACHE.pop(share_id, None)
    if entry is None:
        return json_error("share not found or expired", 404)
    return web.json_response(entry)


def register(app: web.Application) -> None:
    """Register PWA routes."""
    app.router.add_get("/manifest.json", handle_manifest)
    app.router.add_get("/bee-icon.svg", handle_bee_icon)
    app.router.add_get("/sw.js", handle_service_worker)
    app.router.add_get("/offline.html", handle_offline_page)
    # Web Share Target — OS share sheet POSTs here when the PWA is
    # selected as a share destination.
    app.router.add_post("/share-receive", handle_share_receive)
    app.router.add_get("/share/{share_id}", handle_share_get)
