"""Worker routes — CRUD, I/O, lifecycle."""

from __future__ import annotations

import json
import re

from aiohttp import web

from swarm.logging import get_logger
from swarm.pty.process import ProcessError
from swarm.server.helpers import (
    get_daemon,
    handle_errors,
    json_error,
    require_message,
    validate_worker_name,
    worker_action,
)
from swarm.worker.worker import TokenUsage

_log = get_logger("server.routes.workers")


def register(app: web.Application) -> None:
    app.router.add_get("/api/workers", handle_workers)
    app.router.add_get("/api/workers/tails", handle_worker_tails)

    # Literal worker routes BEFORE {name} to avoid ambiguity
    app.router.add_post("/api/workers/launch", handle_workers_launch)
    app.router.add_post("/api/workers/spawn", handle_workers_spawn)
    app.router.add_post("/api/workers/continue-all", handle_workers_continue_all)
    app.router.add_post("/api/workers/send-all", handle_workers_send_all)
    app.router.add_post("/api/workers/discover", handle_workers_discover)
    app.router.add_post("/api/workers/reorder", handle_workers_reorder)

    app.router.add_get("/api/workers/{name}", handle_worker_detail)
    app.router.add_patch("/api/workers/{name}", handle_worker_update)
    app.router.add_get("/api/workers/{name}/identity", handle_worker_identity)
    app.router.add_get("/api/workers/{name}/memory", handle_worker_memory)
    app.router.add_put("/api/workers/{name}/memory", handle_worker_memory_save)
    app.router.add_post("/api/workers/{name}/send", handle_worker_send)
    app.router.add_post("/api/workers/{name}/continue", handle_worker_continue)
    app.router.add_post("/api/workers/{name}/kill", handle_worker_kill)
    app.router.add_post("/api/workers/{name}/escape", handle_worker_escape)
    app.router.add_post("/api/workers/{name}/force-rest", handle_worker_force_rest)
    app.router.add_post("/api/workers/{name}/arrow-up", handle_worker_arrow_up)
    app.router.add_post("/api/workers/{name}/arrow-down", handle_worker_arrow_down)
    app.router.add_post("/api/workers/{name}/interrupt", handle_worker_interrupt)
    app.router.add_post("/api/workers/{name}/revive", handle_worker_revive)
    app.router.add_post("/api/workers/{name}/sleep", handle_worker_sleep)
    app.router.add_post("/api/workers/{name}/analyze", handle_worker_analyze)
    app.router.add_post("/api/workers/{name}/merge", handle_worker_merge)

    # Conflicts
    app.router.add_get("/api/conflicts", handle_conflicts)

    # Groups
    app.router.add_post("/api/groups/{name}/send", handle_group_send)

    # Usage
    app.router.add_get("/api/usage", handle_usage)


@handle_errors
async def handle_workers(request: web.Request) -> web.Response:
    d = get_daemon(request)
    workers = []
    for w in d.workers:
        wd = w.to_api_dict()
        wd["in_config"] = d.config.get_worker(w.name) is not None
        workers.append(wd)
    return web.json_response({"workers": workers})


# Strip ANSI CSI / OSC / SGR escape sequences (color, cursor moves)
# before chrome filtering — raw PTY bytes from the ring buffer still
# carry terminal escapes which broke literal-text regex matches.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\]\d*;[^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[PX^_].*?(?:\x1b\\|\x07)"
)
# Provider chrome (Claude Code / Gemini / Codex prompt UI) that's NOT
# meaningful content. Filtered out before returning the PTY tail so
# the operator sees the actual reasoning / tool calls / prompts.
_CHROME_PATTERNS = [
    re.compile(r"auto mode on", re.IGNORECASE),
    re.compile(r"plan mode on", re.IGNORECASE),
    re.compile(r"accept edits on", re.IGNORECASE),
    re.compile(r"shift\+tab to cycle", re.IGNORECASE),
    re.compile(r"^[\s─━═\-═_]{5,}$"),  # long horizontal rule
    re.compile(r"^\s*Enter to confirm\b", re.IGNORECASE),
    re.compile(r"^\s*Esc to cancel\b", re.IGNORECASE),
    re.compile(r"^\s*\?\s+for shortcuts\b", re.IGNORECASE),
    re.compile(r"^\s*ctrl\+[a-z]\b", re.IGNORECASE),
    re.compile(r"^\s*Try \"", re.IGNORECASE),
    re.compile(r"\(esc to interrupt\)", re.IGNORECASE),
    re.compile(r"^[>▸\s]+$"),  # bare prompt indicator with no content
]


def _strip_ansi(line: str) -> str:
    return _ANSI_RE.sub("", line)


def _is_chrome_line(line: str) -> bool:
    s = _strip_ansi(line).strip()
    if not s:
        return True
    return any(p.search(s) for p in _CHROME_PATTERNS)


@handle_errors
async def handle_worker_tails(request: web.Request) -> web.Response:
    """Bulk PTY tail for non-sleeping workers — the Now panel's primary signal.

    Returns ``{"tails": {worker_name: "meaningful tail lines"}}`` for each
    non-sleeping worker (queen skipped). Chrome lines (auto mode banner,
    separator rules, shortcut hints, etc.) are filtered out so the
    operator sees the actual reasoning / tool output / prompt context.
    The PTY content is the only ground-truth signal for "what is this
    worker doing" — state classification and recent_tools are derived
    signals that can lag or be wrong; the PTY shows the actual screen.
    """
    from swarm.worker.worker import QUEEN_WORKER_NAME, WorkerState

    d = get_daemon(request)
    try:
        lines = max(1, min(int(request.query.get("lines", "4")), 12))
    except ValueError:
        lines = 4
    # Capture a wider window so chrome filtering still leaves N lines.
    capture_lines = max(40, lines * 8)

    skip_states = {WorkerState.SLEEPING}
    targets = [w for w in d.workers if w.name != QUEEN_WORKER_NAME and w.state not in skip_states]

    async def _one(w: object) -> tuple[str, str]:
        try:
            content = await d.safe_capture_output(w.name, lines=capture_lines)
        except Exception:
            content = ""
        if not content:
            return w.name, ""
        # Strip ANSI escapes both for chrome filtering AND for the final
        # output so the frontend renders clean text.
        meaningful = []
        for raw in content.splitlines():
            if _is_chrome_line(raw):
                continue
            clean = _strip_ansi(raw).rstrip()
            if clean.strip():
                meaningful.append(clean)
        kept = meaningful[-lines:]
        return w.name, "\n".join(kept)

    import asyncio

    results = await asyncio.gather(*[_one(w) for w in targets])
    tails = {name: text for name, text in results}
    return web.json_response({"tails": tails})


@handle_errors
async def handle_worker_detail(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    worker = d.get_worker(name)
    if not worker:
        return json_error(f"Worker '{name}' not found", 404)

    try:
        content = await d.capture_worker_output(name)
    except (ProcessError, OSError):
        content = "(output unavailable)"

    result = worker.to_api_dict()
    result["worker_output"] = content
    return web.json_response(result)


@handle_errors
async def handle_worker_update(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    body = await request.json()
    new_name = body.get("name", "").strip() or None
    new_path = body.get("path", "").strip() or None

    if new_name:
        if err := validate_worker_name(new_name):
            return json_error(err)

    d.worker_svc.update_worker(name, name=new_name, path=new_path)
    result_name = new_name or name
    return web.json_response({"status": "updated", "worker": result_name})


@handle_errors
async def handle_worker_send(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]

    body = await request.json()
    result = require_message(body)
    if isinstance(result, web.Response):
        return result
    message = result
    # Optional `enter: false` types the message without pressing Enter —
    # used by the Web Share Target flow so the operator can add context
    # before submitting. Default True preserves prior semantics for
    # every existing caller.
    enter = body.get("enter", True)
    if not isinstance(enter, bool):
        enter = True

    await d.send_to_worker(name, message, enter=enter)
    return web.json_response({"status": "sent", "worker": name, "enter": enter})


async def handle_worker_continue(request: web.Request) -> web.Response:
    return await worker_action(request, lambda d, n: d.continue_worker(n), "continued")


async def handle_worker_kill(request: web.Request) -> web.Response:
    return await worker_action(request, lambda d, n: d.kill_worker(n), "killed")


async def handle_worker_escape(request: web.Request) -> web.Response:
    return await worker_action(request, lambda d, n: d.escape_worker(n), "escape_sent")


async def handle_worker_force_rest(request: web.Request) -> web.Response:
    """Operator override: force a worker into RESTING state."""
    return await worker_action(request, lambda d, n: d.force_rest_worker(n), "force_rested")


async def handle_worker_arrow_up(request: web.Request) -> web.Response:
    return await worker_action(request, lambda d, n: d.arrow_up_worker(n), "arrow_up_sent")


async def handle_worker_arrow_down(request: web.Request) -> web.Response:
    return await worker_action(request, lambda d, n: d.arrow_down_worker(n), "arrow_down_sent")


async def handle_worker_interrupt(request: web.Request) -> web.Response:
    return await worker_action(request, lambda d, n: d.interrupt_worker(n), "interrupted")


async def handle_worker_revive(request: web.Request) -> web.Response:
    return await worker_action(request, lambda d, n: d.revive_worker(n), "revived")


async def handle_worker_sleep(request: web.Request) -> web.Response:
    return await worker_action(request, lambda d, n: d.sleep_worker(n), "sleeping")


@handle_errors
async def handle_worker_identity(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    wc = d.config.get_worker(name)
    if not wc:
        return json_error(f"Worker '{name}' not found in config", 404)
    content = wc.load_identity()
    return web.json_response({"worker": name, "identity": content, "path": wc.identity})


@handle_errors
async def handle_worker_memory(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    from swarm.worker.memory import list_memory_files, load_memory

    content = load_memory(name)
    files = list_memory_files(name)
    return web.json_response({"worker": name, "memory": content, "files": files})


@handle_errors
async def handle_worker_memory_save(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    body = await request.json()
    content = body.get("content", "")
    if not isinstance(content, str):
        return json_error("'content' must be a string")
    from swarm.worker.memory import save_memory

    save_memory(name, content)
    return web.json_response({"status": "saved", "worker": name})


@handle_errors
async def handle_worker_analyze(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    result = await d.analyze_worker(name, force=True)
    return web.json_response(result)


@handle_errors
async def handle_worker_merge(request: web.Request) -> web.Response:
    d = get_daemon(request)
    name = request.match_info["name"]
    result = await d.worker_svc.merge_worker(name)
    status = 200 if result.get("success") else 409
    return web.json_response(result, status=status)


@handle_errors
async def handle_conflicts(request: web.Request) -> web.Response:
    d = get_daemon(request)
    conflicts = getattr(d, "_conflicts", [])
    return web.json_response({"conflicts": conflicts})


@handle_errors
async def handle_workers_reorder(request: web.Request) -> web.Response:
    d = get_daemon(request)
    body = await request.json()
    order = body.get("order")
    if not isinstance(order, list) or not all(isinstance(n, str) for n in order):
        return json_error("'order' must be a list of worker name strings")
    d.worker_svc.reorder_workers(order)
    # Keep config.workers in sync so save_config_to_db writes correct sort_order
    by_name = {wc.name: wc for wc in d.config.workers}
    reordered_cfg: list = []
    for name in order:
        if name in by_name:
            reordered_cfg.append(by_name.pop(name))
    reordered_cfg.extend(by_name.values())
    d.config.workers = reordered_cfg
    # Keep default group member order in sync with dashboard order
    dg_name = d.config.default_group or "default"
    grp = next(
        (g for g in d.config.groups if g.name.lower() == dg_name.lower()),
        None,
    )
    if grp:
        member_set = {w.lower() for w in grp.workers}
        grp.workers = [n for n in order if n.lower() in member_set]
    # Persist sort_order to DB so it survives reload.  Wrap in
    # try/except + WARNING log so a failure (DB locked, schema drift,
    # etc.) surfaces in default-level operator logs — same forensic
    # contract as the rest of the config save chain after #328 / Phase 9.
    if getattr(d, "swarm_db", None):
        try:
            for i, name in enumerate(order):
                d.swarm_db.execute(
                    "UPDATE workers SET sort_order = ? WHERE name = ?",
                    (i, name),
                )
        except Exception:
            _log.warning(
                "workers.reorder: failed to persist sort_order — "
                "in-memory order updated but DB write failed",
                exc_info=True,
            )
            return json_error("failed to persist worker order", status=500)
    return web.json_response({"status": "ok"})


@handle_errors
async def handle_workers_launch(request: web.Request) -> web.Response:
    d = get_daemon(request)
    body = await request.json() if request.can_read_body else {}
    requested = body.get("workers", [])

    # Determine which configs to launch
    running_names = {w.name.lower() for w in d.workers}
    if requested:
        config_by_name = {wc.name.lower(): wc for wc in d.config.workers}
        seen: set[str] = set()
        configs = []
        for name in requested:
            key = name.lower()
            if key not in seen and key not in running_names and key in config_by_name:
                configs.append(config_by_name[key])
                seen.add(key)
    else:
        configs = [wc for wc in d.config.workers if wc.name.lower() not in running_names]

    if not configs:
        return web.json_response({"status": "no_new_workers", "launched": []})

    launched = await d.launch_workers(configs)

    return web.json_response(
        {"status": "launched", "launched": [w.name for w in launched]},
        status=201,
    )


@handle_errors
async def handle_workers_spawn(request: web.Request) -> web.Response:
    d = get_daemon(request)
    body = await request.json()
    name = body.get("name", "").strip()
    path = body.get("path", "").strip()
    provider = body.get("provider", "").strip()

    if not name:
        return json_error("name is required")
    if err := validate_worker_name(name):
        return json_error(err)
    if not path:
        return json_error("path is required")
    from swarm.providers import get_valid_providers

    if provider and provider not in get_valid_providers():
        return json_error(f"Unknown provider '{provider}'")

    from swarm.config import WorkerConfig

    worker = await d.spawn_worker(WorkerConfig(name=name, path=path, provider=provider))

    return web.json_response(
        {"status": "spawned", "worker": worker.name},
        status=201,
    )


@handle_errors
async def handle_workers_continue_all(request: web.Request) -> web.Response:
    d = get_daemon(request)
    count = await d.continue_all()
    return web.json_response({"status": "ok", "count": count})


@handle_errors
async def handle_workers_send_all(request: web.Request) -> web.Response:
    d = get_daemon(request)
    body = await request.json()
    result = require_message(body)
    if isinstance(result, web.Response):
        return result
    count = await d.send_all(result)
    return web.json_response({"status": "sent", "count": count})


@handle_errors
async def handle_workers_discover(request: web.Request) -> web.Response:
    d = get_daemon(request)
    workers = await d.discover()
    return web.json_response({"status": "ok", "workers": [{"name": w.name} for w in workers]})


@handle_errors
async def handle_group_send(request: web.Request) -> web.Response:
    d = get_daemon(request)
    group_name = request.match_info["name"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return json_error("Invalid JSON in request body")
    result = require_message(body)
    if isinstance(result, web.Response):
        return result
    try:
        count = await d.send_group(group_name, result)
    except (ValueError, KeyError) as e:
        return json_error(str(e), 404)
    return web.json_response({"status": "sent", "group": group_name, "count": count})


def _parse_usage_window(
    request: web.Request,
) -> tuple[float | None, float | None, web.Response | None]:
    """Parse optional ?since / ?until query params as unix timestamps.

    Returns ``(since, until, error_response)``.  When the error response
    is non-None the caller should return it unchanged (400).
    """

    def _one(key: str) -> tuple[float | None, web.Response | None]:
        raw = request.query.get(key)
        if not raw:
            return None, None
        try:
            return float(raw), None
        except ValueError:
            return None, json_error(f"invalid {key!r}: {raw!r}", 400)

    since, err = _one("since")
    if err is not None:
        return None, None, err
    until, err = _one("until")
    if err is not None:
        return None, None, err
    return since, until, None


def _compute_worker_usage(
    worker: object,
    start_time: float,
    window_since: float | None,
    window_until: float | None,
) -> TokenUsage:
    """Resolve a worker's usage for the current API request.

    Fast path (no window) returns the cached ``worker.usage`` updated by
    ``_usage_refresh_loop``.  Windowed path does a synchronous read of
    all matching session files and re-computes cost against the
    provider's pricing tier.
    """
    from swarm.providers import get_provider
    from swarm.worker.usage import estimate_cost_for_provider, get_worker_usage

    if window_since is None and window_until is None:
        return worker.usage  # type: ignore[attr-defined,no-any-return]
    try:
        prov = get_provider(worker.provider_name)  # type: ignore[attr-defined]
    except Exception:
        prov = None
    tu = get_worker_usage(
        worker.path,  # type: ignore[attr-defined]
        start_time,
        provider=prov,
        window_since=window_since,
        window_until=window_until,
    )
    tu.cost_usd = estimate_cost_for_provider(
        tu,
        worker.provider_name,  # type: ignore[attr-defined]
    )
    return tu


@handle_errors
async def handle_usage(request: web.Request) -> web.Response:
    """Return per-worker, queen, and total token usage.

    Query params:
      - ``since`` / ``until`` — unix timestamps.  When set, usage is
        aggregated across ALL session files and filtered per-turn to
        the window.  Enables the Usage tab's time filters (24h / 7d /
        30d / last month / this month).  Without params, returns the
        daemon's cached ``worker.usage`` for the current session
        (fast path, refreshed every 10s).

    The Queen used to be a headless ``claude -p`` conductor whose usage
    lived on ``d.queen.usage`` alone.  She's now a PTY-managed Worker
    (kind="queen"), so her tokens accumulate on her Worker row — we
    skip her from the regular worker loop to avoid double-counting.
    """
    d = get_daemon(request)
    from swarm.worker.worker import TokenUsage

    window_since, window_until, err = _parse_usage_window(request)
    if err is not None:
        return err

    workers_usage: dict[str, dict[str, object]] = {}
    total = TokenUsage()
    queen_worker = None
    for w in d.workers:
        if w.is_queen:
            queen_worker = w
            continue
        wu = _compute_worker_usage(w, d.start_time, window_since, window_until)
        workers_usage[w.name] = wu.to_dict()
        total.add(wu)

    if queen_worker is not None:
        queen_tu = _compute_worker_usage(queen_worker, d.start_time, window_since, window_until)
    else:
        queen_tu = d.queen.usage
    total.add(queen_tu)

    return web.json_response(
        {
            "workers": workers_usage,
            "queen": queen_tu.to_dict(),
            "total": total.to_dict(),
            "window_since": window_since,
            "window_until": window_until,
        }
    )
