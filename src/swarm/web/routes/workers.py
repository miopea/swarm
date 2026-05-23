"""Worker action routes: send, kill, revive, escape, redraw, continue, etc."""

from __future__ import annotations

from aiohttp import web

from swarm.server.daemon import console_log
from swarm.server.helpers import get_daemon, handle_errors, json_error


@handle_errors
async def handle_action_send(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    data = await request.post()
    message = data.get("message", "")
    if message:
        await d.send_to_worker(name, message)
        console_log(f'Message sent to "{name}"')
    return web.Response(status=204)


@handle_errors
async def handle_action_continue(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    await d.continue_worker(name)
    console_log(f'Continued "{name}"')
    return web.Response(status=204)


@handle_errors
async def handle_action_toggle_drones(request: web.Request) -> web.Response:
    d = get_daemon(request)
    if d.pilot:
        new_state = d.toggle_drones()
        console_log(f"Drones toggled {'ON' if new_state else 'OFF'}")
        return web.json_response({"enabled": new_state})
    return web.json_response({"error": "pilot not running", "enabled": False})


@handle_errors
async def handle_action_continue_all(request: web.Request) -> web.Response:
    d = get_daemon(request)
    count = await d.continue_all()
    console_log(f"Continue all — {count} worker(s)")
    return web.json_response({"count": count})


@handle_errors
async def handle_action_kill(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    await d.kill_worker(name)
    console_log(f'Killed worker "{name}"', level="warn")
    return web.json_response({"status": "killed", "worker": name})


@handle_errors
async def handle_action_revive(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    await d.revive_worker(name)
    console_log(f'Revived worker "{name}"')
    return web.json_response({"status": "revived", "worker": name})


@handle_errors
async def handle_action_escape(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    await d.escape_worker(name)
    console_log(f'Escape sent to "{name}"')
    return web.json_response({"status": "escape_sent", "worker": name})


@handle_errors
async def handle_action_arrow_up(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    await d.arrow_up_worker(name)
    return web.json_response({"status": "arrow_up_sent", "worker": name})


@handle_errors
async def handle_action_arrow_down(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    await d.arrow_down_worker(name)
    return web.json_response({"status": "arrow_down_sent", "worker": name})


@handle_errors
async def handle_action_arrow_right(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    await d.arrow_right_worker(name)
    return web.json_response({"status": "arrow_right_sent", "worker": name})


@handle_errors
async def handle_action_arrow_left(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    await d.arrow_left_worker(name)
    return web.json_response({"status": "arrow_left_sent", "worker": name})


@handle_errors
async def handle_action_redraw(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    await d.redraw_worker(name)
    return web.json_response({"status": "redraw_sent", "worker": name})


@handle_errors
async def handle_action_send_all(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    message = data.get("message", "")
    if not message:
        return json_error("message required")

    count = await d.send_all(message)
    console_log(f"Broadcast sent to {count} worker(s)")
    return web.json_response({"count": count})


@handle_errors
async def handle_action_send_group(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    message = data.get("message", "")
    group_name = data.get("group", "")
    if not message:
        return json_error("message required")
    if not group_name:
        return json_error("group required")

    count = await d.send_group(group_name, message)
    console_log(f'Group "{group_name}" — sent to {count} worker(s)')
    return web.json_response({"count": count})


@handle_errors
async def handle_action_launch(request: web.Request) -> web.Response:
    d = get_daemon(request)

    data = await request.post()
    names_raw = data.get("workers", "")  # comma-separated worker names

    # Skip workers that are already running
    running_names = {w.name.lower() for w in d.workers}

    if names_raw:
        seen: set[str] = set()
        config_by_name = {w.name.lower(): w for w in d.config.workers}
        to_launch = []
        for raw in names_raw.split(","):
            key = raw.strip().lower()
            if key and key not in seen and key not in running_names and key in config_by_name:
                to_launch.append(config_by_name[key])
                seen.add(key)
    else:
        to_launch = [w for w in d.config.workers if w.name.lower() not in running_names]

    if not to_launch:
        return json_error("no workers to launch")

    console_log(f"Launching {len(to_launch)} worker(s)...")
    launched = await d.launch_workers(to_launch)
    names = ", ".join(w.name for w in launched)
    console_log(f"Launched: {names}")
    return web.json_response(
        {
            "status": "launched",
            "count": len(launched),
            "workers": [w.name for w in launched],
        }
    )


@handle_errors
async def handle_action_spawn(request: web.Request) -> web.Response:
    """Spawn a single worker into the running session."""
    d = get_daemon(request)
    data = await request.post()
    name = data.get("name", "").strip()
    path = data.get("path", "").strip()
    provider = data.get("provider", "").strip()

    if not name or not path:
        return json_error("name and path required")

    if provider:
        from swarm.providers import get_valid_providers

        if provider not in get_valid_providers():
            return json_error(f"Unknown provider '{provider}'")

    from swarm.config import WorkerConfig

    wc = WorkerConfig(name=name, path=path, provider=provider)
    worker = await d.spawn_worker(wc)
    console_log(f'Spawned worker "{worker.name}"')
    return web.json_response({"status": "spawned", "worker": worker.name})


@handle_errors
async def handle_action_kill_session(request: web.Request) -> web.Response:
    d = get_daemon(request)
    data = await request.post()
    all_sessions = data.get("all", "") == "1"
    scope = "all sessions" if all_sessions else "session"
    console_log(f"Killing {scope} — all workers terminated", level="warn")
    await d.kill_session(all_sessions=all_sessions)
    return web.json_response({"status": "killed"})


@handle_errors
async def handle_action_update(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    data = await request.post()
    new_name = data.get("name", "").strip() or None
    new_path = data.get("path", "").strip() or None

    d.worker_svc.update_worker(name, name=new_name, path=new_path)
    result_name = new_name or name
    console_log(f'Updated worker "{name}" → "{result_name}"')
    return web.json_response({"status": "updated", "worker": result_name})


@handle_errors
async def handle_action_interrupt(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    await d.interrupt_worker(name)
    console_log(f'Ctrl-C sent to "{name}"')
    return web.json_response({"status": "interrupt_sent", "worker": name})


def register(app: web.Application) -> None:
    """Register worker action routes."""
    app.router.add_post("/action/send/{name}", handle_action_send)
    app.router.add_post("/action/continue/{name}", handle_action_continue)
    app.router.add_post("/action/kill/{name}", handle_action_kill)
    app.router.add_post("/action/revive/{name}", handle_action_revive)
    app.router.add_post("/action/escape/{name}", handle_action_escape)
    app.router.add_post("/action/arrow-up/{name}", handle_action_arrow_up)
    app.router.add_post("/action/arrow-down/{name}", handle_action_arrow_down)
    app.router.add_post("/action/arrow-right/{name}", handle_action_arrow_right)
    app.router.add_post("/action/arrow-left/{name}", handle_action_arrow_left)
    app.router.add_post("/action/redraw/{name}", handle_action_redraw)
    app.router.add_post("/action/toggle-drones", handle_action_toggle_drones)
    app.router.add_post("/action/continue-all", handle_action_continue_all)
    app.router.add_post("/action/send-all", handle_action_send_all)
    app.router.add_post("/action/send-group", handle_action_send_group)
    app.router.add_post("/action/launch", handle_action_launch)
    app.router.add_post("/action/spawn", handle_action_spawn)
    app.router.add_post("/action/kill-session", handle_action_kill_session)
    app.router.add_post("/action/update/{name}", handle_action_update)
    app.router.add_post("/action/interrupt/{name}", handle_action_interrupt)
