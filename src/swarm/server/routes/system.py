"""System routes — health, session, tunnel, server, upload, resources."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from swarm.auth.password import verify_password
from swarm.server.helpers import get_daemon, handle_errors, json_error, read_file_field

if TYPE_CHECKING:
    from swarm.relocate import RelocationPlan

# Lives in $HOME, not the state directory: the relocation MOVES the state
# directory, so a log written inside it is either moved mid-write or lands
# in whichever copy the operator is not looking at.
_RELOCATE_LOG = ".swarm-relocate.log"


async def _best_effort_reinstall(context: str, timeout: float = 30.0) -> None:
    """Reinstall from local source without ever blocking a restart.

    Both user-initiated restart paths (Reload, holder bounce) previously
    used a bare ``await reinstall_from_local_source()``. That call runs
    up to three ``uv`` subprocesses, each bounded by a 120s step timeout
    — so a misbehaving step can stall the restart for up to ~6 minutes.
    For the holder bounce that's catastrophic: the holder is already
    killed, so the daemon never comes back and the operator sees a
    silent no-op (the 2026.5.15 report). Time-bound it and swallow
    failures — the restart MUST proceed regardless.
    """
    import logging

    from swarm.update import reinstall_from_local_source

    log = logging.getLogger("swarm.api")
    try:
        ok, output = await asyncio.wait_for(reinstall_from_local_source(), timeout=timeout)
        if not ok:
            log.warning("Local reinstall failed during %s (proceeding): %s", context, output)
    except TimeoutError:
        log.warning("Local reinstall timed out during %s (proceeding without it)", context)
    except Exception:
        log.warning("Local reinstall raised during %s (proceeding)", context, exc_info=True)


def register(app: web.Application) -> None:
    app.router.add_get("/health", handle_health_check)
    app.router.add_get("/ready", handle_readiness)
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/mcp/schema-drift", handle_mcp_schema_drift)
    app.router.add_get("/api/holder/drift", handle_holder_drift)
    app.router.add_post("/api/holder/bounce", handle_holder_bounce)
    app.router.add_get("/api/relocate", handle_relocation_status)
    app.router.add_post("/api/relocate", handle_relocate)
    app.router.add_get("/api/resources", handle_resources)
    app.router.add_post("/api/client-vitals", handle_client_vitals)
    app.router.add_get("/api/resources/history", handle_resource_history)

    app.router.add_post("/api/session/kill", handle_session_kill)

    app.router.add_post("/api/tunnel/start", handle_tunnel_start)
    app.router.add_post("/api/tunnel/stop", handle_tunnel_stop)
    app.router.add_get("/api/tunnel/status", handle_tunnel_status)

    app.router.add_post("/api/server/stop", handle_server_stop)
    app.router.add_post("/api/server/restart", handle_server_restart)

    app.router.add_post("/api/uploads", handle_upload)

    app.router.add_get("/api/docs", handle_openapi_spec)
    app.router.add_get("/api/docs/ui", handle_swagger_ui)

    app.router.add_get("/api/search", handle_global_search)


async def handle_readiness(request: web.Request) -> web.Response:
    """Readiness probe — unauthenticated, returns 200 when fully initialized."""
    d = get_daemon(request)
    checks: dict[str, bool] = {
        "config_loaded": d.config is not None,
        "workers_initialized": hasattr(d, "workers"),
    }
    if d.config and d.config.drones.enabled:
        checks["pilot_running"] = d.pilot is not None and d.pilot.enabled
    ready = all(checks.values())
    return web.json_response({"ready": ready, "checks": checks}, status=200 if ready else 503)


@handle_errors
async def handle_resources(request: web.Request) -> web.Response:
    """GET /api/resources — return current resource snapshot."""
    daemon = get_daemon(request)
    snapshot = daemon.get_resource_snapshot()
    if snapshot is None:
        return web.json_response({"error": "resource monitoring not active"}, status=503)
    return web.json_response(snapshot)


@handle_errors
async def handle_resource_history(request: web.Request) -> web.Response:
    """GET /api/resources/history — return historical resource snapshots."""
    daemon = get_daemon(request)
    history = daemon.resource_mon.history
    return web.json_response({"snapshots": history, "count": len(history)})


async def handle_openapi_spec(request: web.Request) -> web.Response:
    """GET /api/docs — serve the OpenAPI spec as JSON."""
    from pathlib import Path

    import yaml

    spec_path = Path(__file__).resolve().parent.parent.parent.parent / "docs" / "openapi.yaml"
    if not spec_path.exists():
        return json_error("OpenAPI spec not found", 404)
    data = yaml.safe_load(spec_path.read_text())
    return web.json_response(data)


async def handle_swagger_ui(request: web.Request) -> web.Response:
    """GET /api/docs/ui — serve a Swagger UI page."""
    html = """<!DOCTYPE html>
<html><head><title>Swarm (legacy) API Docs</title>
<link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
</head><body>
<div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({url:'/api/docs',dom_id:'#swagger-ui'})</script>
</body></html>"""
    return web.Response(text=html, content_type="text/html")


@handle_errors
async def handle_health_check(request: web.Request) -> web.Response:
    """Root-level health check — unauthenticated for tunnel probes."""
    from swarm.server.api import get_api_password
    from swarm.update import _get_installed_version, build_sha

    d = get_daemon(request)
    uptime = time.time() - d.start_time
    version = _get_installed_version()

    payload: dict[str, object] = {
        "status": "ok",
        "uptime": uptime,
        "version": version,
    }

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        password = get_api_password(d)
        if verify_password(auth[7:], password):
            payload["workers"] = [
                {
                    "name": w.name,
                    "state": w.state.value,
                    "duration": w.state_duration,
                }
                for w in d.workers
            ]
            payload["queen"] = dict(d.queen_queue.status())
            payload["drones"] = {"enabled": d.pilot.enabled if d.pilot else False}
            payload["pilot"] = d.pilot.get_diagnostics() if d.pilot else {}
            payload["build_sha"] = build_sha()

    return web.json_response(payload)


@handle_errors
async def handle_health(request: web.Request) -> web.Response:
    from swarm.mcp.tools import tools_source_drift
    from swarm.paths import is_relocated, state_dir
    from swarm.update import _get_installed_version, build_sha

    d = get_daemon(request)
    pilot_info: dict[str, object] = {}
    if d.pilot:
        pilot_info = d.pilot.get_diagnostics()
    pool = getattr(d, "pool", None)
    # #1679: live rather than the connect-time snapshot — see live_holder_drift().
    holder_drift = pool.live_holder_drift() if pool is not None else None
    # #1203: ``build_sha`` fingerprints the tree but cannot say WHY it differs —
    # "last release" and "code from no commit" produce equally opaque hashes.
    # These fields answer that directly, and being status fields rather than log
    # lines they cost nothing on a clean reload. ``source_checked`` keeps "we
    # could not tell" distinct from "it is clean".
    src = getattr(d, "source_tree_state", None)
    return web.json_response(
        {
            "status": "ok",
            "workers": len(d.workers),
            "drones_enabled": d.pilot.enabled if d.pilot else False,
            "uptime": time.time() - d.start_time,
            "pilot": pilot_info,
            "version": _get_installed_version(),
            "build_sha": build_sha(),
            "mcp_schema_drift": tools_source_drift()["drift"],
            "holder_drift": holder_drift,
            # Cheap enough for the poll loop: a name comparison and one
            # is_dir().  The full plan() shells out to systemctl, so the
            # banner fetches /api/relocate once rather than every tick.
            "relocated": is_relocated(),
            "state_dir": str(state_dir()),
            "source_checked": bool(src.checked) if src else False,
            "source_dirty": bool(src.is_dirty) if src else False,
            "source_dirty_files": list(src.dirty_files) if src else [],
        }
    )


@handle_errors
async def handle_mcp_schema_drift(request: web.Request) -> web.Response:
    """Return drift details for the MCP tool schema.

    Workers keep seeing the ``tools/list`` payload the daemon held in
    memory at startup — when ``src/swarm/mcp/tools.py`` is edited, those
    schemas go stale until the daemon reloads. Dashboard polls this to
    prompt the operator.
    """
    from swarm.mcp.tools import tools_source_drift

    return web.json_response(tools_source_drift())


@handle_errors
async def handle_holder_drift(request: web.Request) -> web.Response:
    """Return drift details for the PTY holder sidecar.

    The holder is a double-forked persistent process — daemon reloads
    (os.execv) never refresh its bytecode, so a holder that was spawned
    before a fix landed in ``holder.py`` will keep running the old
    behavior until explicitly killed. The pool hashes ``holder.py`` on
    each reconnect and compares against the holder's import-time hash
    (``cmd: version``). Dashboard polls this to nudge the operator when
    they need to bounce the holder, not just reload the daemon.
    """
    d = get_daemon(request)
    pool = getattr(d, "pool", None)
    if pool is None:
        return web.json_response(
            {"checked": False, "drift": False, "unknown": True, "error": "no pool"}
        )
    # #1679: LIVE, not the connect-time snapshot. The cached dict compares the holder
    # against a file as it was when the holder attached, so it reports clean the moment
    # after holder.py changes — exactly when the operator is asking.
    return web.json_response(pool.live_holder_drift())


@handle_errors
async def handle_holder_bounce(request: web.Request) -> web.Response:
    """Kill the PTY holder sidecar and restart the daemon.

    SIGTERMs the holder PID, removes ``holder.sock`` / ``holder.pid``,
    runs the same reinstall + restart path as the Reload button. On
    daemon startup the pool spawns a fresh holder with the current
    on-disk ``holder.py``.

    Destructive: all worker child processes are orphaned and respawned
    fresh by the post-restart reconcile. Operators should hard-refresh
    the browser/PWA once the daemon is back to bust the static-asset
    cache.
    """
    import signal

    from swarm.pty.holder import DEFAULT_PID_PATH, DEFAULT_SOCKET_PATH

    d = get_daemon(request)
    pool = getattr(d, "pool", None)
    if pool is None:
        return json_error("no PTY pool available", 503)

    drift = pool.live_holder_drift() if pool is not None else {}
    holder_pid = int(drift.get("holder_pid") or 0)
    if holder_pid <= 0:
        try:
            holder_pid = int(DEFAULT_PID_PATH.read_text().strip())
        except (OSError, ValueError):
            return json_error("could not determine holder PID", 500)

    try:
        os.kill(holder_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        return json_error(f"cannot kill holder PID {holder_pid}: {exc}", 500)

    DEFAULT_SOCKET_PATH.unlink(missing_ok=True)
    DEFAULT_PID_PATH.unlink(missing_ok=True)

    # Arm the restart BEFORE the reinstall. The holder is already dead;
    # the one thing that MUST happen now is the daemon restart. Reinstall
    # is a best-effort convenience (picks up `swarm update`d code) and
    # must never gate the restart.
    restart_flag = request.app.get("restart_flag")
    if restart_flag is not None:
        restart_flag["requested"] = True
    shutdown: asyncio.Event | None = request.app.get("shutdown_event")
    if shutdown is None:
        return json_error("shutdown not available", 500)

    await _best_effort_reinstall("holder bounce")

    shutdown.set()
    return web.json_response({"status": "bouncing", "killed_pid": holder_pid})


def _relocation_payload(plan_: RelocationPlan) -> dict[str, object]:
    """Serialize a :class:`RelocationPlan` for the dashboard banner."""
    from swarm.relocate import dir_size_bytes

    return {
        "relocated": plan_.already_done,
        "blocked_reason": plan_.blocked_reason,
        "needs_repair": plan_.needs_repair,
        "source": str(plan_.source),
        "target": str(plan_.target),
        "move_needed": plan_.move_needed,
        "size_bytes": dir_size_bytes(plan_.source) if plan_.move_needed else 0,
        "old_unit": plan_.old_unit.name if plan_.old_unit else None,
        "new_unit": plan_.new_unit.name,
        "old_entrypoints": [str(e) for e in plan_.old_entrypoints],
        "live": [
            {"kind": proc.kind, "pid": proc.pid, "detail": proc.detail} for proc in plan_.live
        ],
    }


def _relocate_helper() -> Path | None:
    """The command that runs the relocation, or None if it cannot be found.

    Deliberately ``swarm-legacy`` and never ``swarm``:
    ``_remove_old_entrypoints()`` deletes the ``swarm`` shim partway
    through, so a helper launched under that name would have its own
    executable removed while running.
    """
    import shutil
    import sys

    candidate = Path(sys.executable).parent / "swarm-legacy"
    if candidate.exists():
        return candidate
    found = shutil.which("swarm-legacy")
    return Path(found) if found else None


def _kill_mode_guard() -> str | None:
    """Refuse the relocation when systemd would kill the helper mid-move.

    ``relocate()`` runs ``systemctl stop swarm.service`` as its first
    real step.  ``start_new_session=True`` puts the helper in its own
    session but NOT its own cgroup, so without ``KillMode=process`` the
    stop takes the whole cgroup down — helper included — leaving state
    moved and the unit never rewritten.  That is exactly the
    half-relocated hive that needs a terminal to repair, so it is worth
    a pre-flight check rather than a post-mortem.
    """
    from swarm.service import current_unit_path

    unit = current_unit_path()
    if not unit.exists():
        # Not systemd-managed — nothing stops the unit, nothing kills the helper.
        return None
    try:
        if "KillMode=process" in unit.read_text():
            return None
    except OSError:
        return None
    return (
        f"{unit.name} does not set KillMode=process, so stopping the service would "
        "kill the relocation partway through. Reload swarm once (which patches the "
        "unit), then try again."
    )


@handle_errors
async def handle_relocation_status(request: web.Request) -> web.Response:
    """Report whether this hive still occupies the ``swarm`` name.

    ``plan()`` runs ``systemctl is-active`` and walks the state
    directory, so it is off the poll path — the dashboard reads the
    cheap ``relocated`` flag from ``/api/health`` and calls this once,
    when it needs the detail to render.
    """
    from swarm.relocate import plan

    plan_ = await asyncio.to_thread(plan)
    return web.json_response(_relocation_payload(plan_))


@handle_errors
async def handle_relocate(request: web.Request) -> web.Response:
    """Move this hive off the ``swarm`` name, without a terminal.

    DESTRUCTIVE: every worker is terminated, ``~/.swarm`` becomes
    ``~/.swarm-legacy``, and ``swarm.service`` becomes
    ``swarm-legacy.service``.

    The work CANNOT run in this process.  ``relocate()`` stops the unit
    and SIGTERMs the daemon PID it reads from ``daemon.lock`` — which is
    us — so an in-process call would kill itself between moving the
    state and rewriting the unit.  Instead we hand off to a detached
    ``swarm-legacy relocate --yes`` and return immediately; it stops
    this daemon, does the move, and starts ``swarm-legacy.service``,
    which rebinds the same port for the dashboard to reconnect to.
    """
    from swarm.relocate import plan

    plan_ = await asyncio.to_thread(plan)
    if plan_.already_done:
        return web.json_response({"status": "already", **_relocation_payload(plan_)})

    blocked = plan_.blocked_reason
    if blocked:
        # Refused BEFORE the helper is launched. relocate() would stop the
        # service and kill every worker before hitting the same condition,
        # so discovering it here is the difference between a 409 and a
        # destroyed hive.
        return json_error(blocked, 409)

    helper = _relocate_helper()
    if helper is None:
        return json_error(
            "could not find the 'swarm-legacy' command needed to run the relocation", 500
        )

    refusal = _kill_mode_guard()
    if refusal is not None:
        return json_error(refusal, 409)

    # The daemon dies partway through, so its output cannot be read back
    # in-process — and DEVNULL meant a RelocationError vanished entirely,
    # leaving the dashboard on "Relocating..." with no way to learn why.
    # A file outside the state directory survives the move either way.
    log_path = Path.home() / _RELOCATE_LOG
    try:
        log = log_path.open("w")
    except OSError:
        log = None
    try:
        await asyncio.create_subprocess_exec(
            str(helper),
            "relocate",
            "--yes",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=log or asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.STDOUT if log else asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        # The child holds its own dup of the fd; ours is done.
        if log is not None:
            log.close()

    return web.json_response(
        {
            "status": "relocating",
            "source": str(plan_.source),
            "target": str(plan_.target),
            "unit": plan_.new_unit.name,
            "log": str(log_path),
        }
    )


@handle_errors
async def handle_session_kill(request: web.Request) -> web.Response:
    d = get_daemon(request)
    await d.kill_session()
    return web.json_response({"status": "killed"})


@handle_errors
async def handle_tunnel_start(request: web.Request) -> web.Response:
    d = get_daemon(request)
    if d.tunnel.is_running:
        return web.json_response(d.tunnel.to_dict())
    explicit_pw = os.environ.get("SWARM_API_PASSWORD") or d.config.api_password
    if not explicit_pw:
        return json_error(
            "Set SWARM_API_PASSWORD or api_password in swarm.yaml before starting a public tunnel",
            400,
        )
    try:
        await d.tunnel.start()
    except RuntimeError as e:
        return json_error(str(e), 500)
    return web.json_response(d.tunnel.to_dict())


@handle_errors
async def handle_tunnel_stop(request: web.Request) -> web.Response:
    d = get_daemon(request)
    await d.tunnel.stop()
    return web.json_response(d.tunnel.to_dict())


@handle_errors
async def handle_tunnel_status(request: web.Request) -> web.Response:
    d = get_daemon(request)
    return web.json_response(d.tunnel.to_dict())


@handle_errors
async def handle_server_stop(request: web.Request) -> web.Response:
    shutdown: asyncio.Event | None = request.app.get("shutdown_event")
    if shutdown:
        shutdown.set()
        return web.json_response({"status": "stopping"})
    return json_error("shutdown not available")


@handle_errors
async def handle_server_restart(request: web.Request) -> web.Response:
    await _best_effort_reinstall("server restart")

    restart_flag = request.app.get("restart_flag")
    if restart_flag is not None:
        restart_flag["requested"] = True
    shutdown: asyncio.Event | None = request.app.get("shutdown_event")
    if shutdown:
        shutdown.set()
        return web.json_response({"status": "restarting"})
    return json_error("shutdown not available")


@handle_errors
async def handle_upload(request: web.Request) -> web.Response:
    d = get_daemon(request)
    filename, data = await read_file_field(request)
    path = d.save_attachment(filename, data)
    return web.json_response({"status": "uploaded", "path": path}, status=201)


@handle_errors
async def handle_global_search(request: web.Request) -> web.Response:
    """Search across workers, tasks, decisions, and buzz log."""
    d = get_daemon(request)
    q = request.query.get("q", "").strip().lower()
    if not q:
        return web.json_response({"workers": [], "tasks": [], "buzz": []})

    limit = min(int(request.query.get("limit", "10")), 50)

    # Workers: match by name
    workers = [
        {"name": w.name, "state": w.display_state.value, "provider": w.provider}
        for w in d.workers
        if q in w.name.lower()
    ][:limit]

    # Tasks: match by title or description
    tasks_found, _ = d.task_board.query(search=q, limit=limit, offset=0)
    tasks = [
        {
            "id": t.id,
            "number": t.number,
            "title": t.title,
            "status": t.status.value,
        }
        for t in tasks_found
    ]

    # Buzz log: match by detail or worker name
    buzz_entries = d.drone_log.search(query=q, limit=limit)
    buzz = [
        {
            "action": e["action"],
            "worker": e["worker_name"],
            "detail": (e.get("detail") or "")[:120],
            "timestamp": e["timestamp"],
        }
        for e in buzz_entries
    ]

    return web.json_response({"workers": workers, "tasks": tasks, "buzz": buzz})


async def handle_client_vitals(request: web.Request) -> web.Response:
    """Record a browser heartbeat so a tab crash leaves evidence behind.

    WHY THIS EXISTS. The operator's Edge tab has died repeatedly — never on demand,
    always some minutes in. Console logs die WITH the tab, so every crash so far has
    produced no evidence at all and three fixes have been reasoned from inference. A
    heartbeat posted to the daemon survives, so the last line before a gap shows the
    trajectory: heap climbing to a ceiling is memory, heap flat is not.

    Deliberately tiny and best-effort. It logs and returns; it must never become a
    reason the dashboard is slow, which would be its own kind of self-defeating.
    """
    import logging

    log = logging.getLogger("swarm.api")
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": True})

    def _num(key: str) -> float:
        try:
            return float(body.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    # plat and webgl are the two fields the WebGL-guard investigation turns on: the
    # guard was wrong for months because nothing ever surfaced what it decided. Logged
    # explicitly rather than dumping the whole body, so the line stays greppable.
    log.warning(
        "[client-vitals] heap=%.1fMB/%.1fMB wsMB=%d evMB=%d evMsgs=%d procMB=%d "
        "terms=%d canvases=%d nodes=%d uptime=%.0fs panel=%s "
        "plat=%s webgl=%s",
        _num("heapMB"),
        _num("heapLimitMB"),
        int(_num("wsMB")),
        int(_num("evMB")),
        int(_num("evMsgs")),
        int(_num("procMB")),
        int(_num("terms")),
        int(_num("canvases")),
        int(_num("nodes")),
        _num("uptimeS"),
        bool(body.get("panel")),
        str(body.get("plat") or "?")[:32],
        bool(body.get("webgl")),
    )
    return web.json_response({"ok": True})
