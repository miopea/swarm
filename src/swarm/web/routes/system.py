"""System action routes: stop server, tunnel, updates, clear logs."""

from __future__ import annotations

from aiohttp import web

from swarm.paths import state_dir
from swarm.server.daemon import console_log
from swarm.server.helpers import get_daemon, handle_errors, json_error


@handle_errors
async def handle_action_stop_server(request: web.Request) -> web.Response:
    """Trigger graceful shutdown of the web server."""
    console_log("Web server stopping...")
    shutdown_event = request.app.get("shutdown_event")
    if shutdown_event:
        shutdown_event.set()
        return web.json_response({"status": "stopping"})
    return json_error("no shutdown event configured", 500)


@handle_errors
async def handle_action_tunnel_start(request: web.Request) -> web.Response:
    d = get_daemon(request)
    if d.tunnel.is_running:
        return web.json_response(d.tunnel.to_dict())
    try:
        await d.tunnel.start()
    except RuntimeError as e:
        return json_error(str(e), 500)
    result = d.tunnel.to_dict()
    if not d.config.api_password:
        result["warning"] = "Tunnel is public — set api_password for security"
    console_log(f"Tunnel started: {d.tunnel.url}")
    return web.json_response(result)


@handle_errors
async def handle_action_tunnel_stop(request: web.Request) -> web.Response:
    d = get_daemon(request)
    await d.tunnel.stop()
    console_log("Tunnel stopped")
    return web.json_response(d.tunnel.to_dict())


@handle_errors
async def handle_action_check_update(request: web.Request) -> web.Response:
    """Force a fresh update check and return the result."""
    from swarm.update import check_for_update, update_result_to_dict

    d = get_daemon(request)
    result = await check_for_update(force=True)
    d._update_result = result
    if result.available:
        d.broadcast_ws({"type": "update_available", **update_result_to_dict(result)})
    return web.json_response(update_result_to_dict(result))


@handle_errors
async def handle_action_install_update(request: web.Request) -> web.Response:
    """Install the update via uv tool reinstall."""
    from swarm.update import perform_update

    d = get_daemon(request)
    console_log("Installing update...")

    def _on_output(line: str) -> None:
        d.broadcast_ws({"type": "update_progress", "line": line})

    success, output = await perform_update(on_output=_on_output)
    if success:
        console_log("Update installed successfully")
        d.broadcast_ws({"type": "update_installed"})
    else:
        console_log(f"Update failed: {output[:200]}", level="error")
        d.broadcast_ws({"type": "update_failed"})
    return web.json_response({"success": success, "output": output})


@handle_errors
async def handle_action_update_and_restart(request: web.Request) -> web.Response:
    """Install the update and restart the server process via os.execv."""
    from swarm.update import perform_update

    d = get_daemon(request)
    console_log("Installing update and restarting...")

    def _on_output(line: str) -> None:
        d.broadcast_ws({"type": "update_progress", "line": line})

    success, output = await perform_update(on_output=_on_output)
    if not success:
        console_log(f"Update failed: {output[:200]}", level="error")
        d.broadcast_ws({"type": "update_failed"})
        return web.json_response({"success": False, "output": output})

    console_log("Update installed — restarting server")
    d.broadcast_ws({"type": "update_restarting"})
    restart_flag = request.app.get("restart_flag")
    if restart_flag is not None:
        restart_flag["requested"] = True
    shutdown_event = request.app.get("shutdown_event")
    if shutdown_event:
        shutdown_event.set()
    return web.json_response({"success": True, "restarting": True})


async def handle_action_clear_logs(request: web.Request) -> web.Response:
    """Truncate ~/.swarm/swarm.log."""

    log_path = state_dir() / "swarm.log"
    try:
        log_path.write_text("")
        console_log("Log file cleared")
    except OSError as e:
        return json_error(str(e), 500)
    return web.json_response({"status": "cleared"})


def register(app: web.Application) -> None:
    """Register system action routes."""
    app.router.add_post("/action/stop-server", handle_action_stop_server)
    app.router.add_post("/action/tunnel/start", handle_action_tunnel_start)
    app.router.add_post("/action/tunnel/stop", handle_action_tunnel_stop)
    app.router.add_post("/action/check-update", handle_action_check_update)
    app.router.add_post("/action/install-update", handle_action_install_update)
    app.router.add_post("/action/update-and-restart", handle_action_update_and_restart)
    app.router.add_post("/action/clear-logs", handle_action_clear_logs)
