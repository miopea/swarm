"""Server entry-point code — `run_daemon`, `run_test_daemon`, lock helpers, banners.

Extracted from :mod:`swarm.server.daemon` (audit finding #1) so the
daemon module is just the class.  See
``docs/specs/daemon-god-object-refactor.md``.

The CLI (`swarm serve` / `swarm test`) calls into this module to
construct, start, and supervise a :class:`SwarmDaemon`.  External
importers historically reached for these names through
``swarm.server.daemon``; the daemon module re-exports them for one
release cycle so existing call sites don't break.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from swarm.drones.rules import Decision
from swarm.logging import get_logger
from swarm.server.daemon import SwarmDaemon
from swarm.tasks.store import FileTaskStore

if TYPE_CHECKING:
    from swarm.config import HiveConfig

_log = get_logger("server.runner")

_DAEMON_LOCK_PATH = Path.home() / ".swarm" / "daemon.lock"


def _read_lock_pid() -> int | None:
    """Read the PID from the daemon lock file, or None if unreadable."""
    try:
        text = _DAEMON_LOCK_PATH.read_text().strip()
        return int(text) if text else None
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """Check if a process is alive (signal 0 probe)."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_daemon_lock() -> int:
    """Acquire an exclusive lock on the daemon lock file.

    Uses ``fcntl.flock()`` which is automatically released when the
    process exits (even on crash).  Returns the open file descriptor
    so it stays alive for the process lifetime.

    If the lock is held by a dead process (e.g. orphaned child from
    SWARM_DEV execvp via ``uv run``), the stale lock is broken and
    re-acquired automatically.

    Raises ``SystemExit`` if another daemon already holds the lock
    and that process is still alive.
    """
    import fcntl

    _DAEMON_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_DAEMON_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Lock held — check if the holder is still alive
        holder_pid = _read_lock_pid()
        if holder_pid is not None and not _pid_alive(holder_pid):
            # Stale lock from a dead process — break it
            _log.warning("breaking stale daemon lock held by dead PID %d", holder_pid)
            os.close(fd)
            _DAEMON_LOCK_PATH.unlink(missing_ok=True)
            fd = os.open(str(_DAEMON_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise SystemExit(
                    "Another swarm daemon is already running. Run 'swarm stop' to stop it."
                )
        else:
            os.close(fd)
            raise SystemExit(
                "Another swarm daemon is already running. Run 'swarm stop' to stop it."
            )
    # Write our PID for diagnostics
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def _maybe_patch_systemd_unit() -> None:
    """Auto-patch existing systemd unit to use KillMode=process."""
    try:
        from swarm.service import ensure_killmode_process

        if ensure_killmode_process():
            _log.info("Patched systemd unit: KillMode=process (preserves workers across restarts)")
    except Exception:
        pass  # not critical — skip on non-systemd systems


async def run_daemon(
    config: HiveConfig, host: str = "localhost", port: int = 9090, *, test_mode: bool = False
) -> None:
    """Start the daemon with HTTP server."""
    import signal

    from swarm.server.api import create_app

    # Diagnostic: log cfg.workflows immediately on entry — if this is
    # already empty here, the wipe happened in cli.py between
    # ``_load_config_db_first`` and the call to ``run_daemon``.  If
    # it's correct here but ``SwarmDaemon.__init__`` later sees empty,
    # the wipe is in ``__init__`` itself.  WARNING level survives any
    # log-level filter (Amanda 2026-05-05).
    _log.warning(
        "run_daemon entry: config.workflows=%r config_source=%s argv=%r",
        config.workflows,
        getattr(config, "config_source", "<unset>"),
        sys.argv,
    )

    # Singleton lock — prevents two daemons from running simultaneously
    # and causing revive wars via the shared pty-holder.
    # The fd must stay open for the process lifetime; stored on the daemon.
    _daemon_lock_fd = _acquire_daemon_lock()

    _maybe_patch_systemd_unit()

    # Capture startup command for os.execv restart
    startup_argv = list(sys.argv)

    test_store = None
    if test_mode:
        test_store = FileTaskStore(path=Path.home() / ".swarm" / "test-tasks.json")
    daemon = SwarmDaemon(config, task_store=test_store)
    daemon._lock_fd = _daemon_lock_fd  # prevent GC / keep lock alive

    # Initialize the PTY process pool (starts holder sidecar if needed)
    from swarm.pty.pool import ProcessPool

    pool = ProcessPool()
    await pool.ensure_holder()
    daemon.pool = pool

    await daemon.start()

    # Initialize test mode components if enabled
    if test_mode:
        daemon._init_test_mode()

    app = create_app(daemon)

    # Graceful shutdown via signal — avoids KeyboardInterrupt race with aiohttp
    shutdown = asyncio.Event()
    app["shutdown_event"] = shutdown
    # Mutable container so the handler can set it without triggering
    # aiohttp's "changing state of started app" deprecation warning.
    app["restart_flag"] = {"requested": False}

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    _print_banner(daemon, host, port)

    # Wire runtime event logging to console
    if daemon.pilot:
        daemon.pilot.on_state_changed(
            lambda w: console_log(f'Worker "{w.name}" state -> {w.state.value}')
        )
        daemon.pilot.on_task_assigned(
            lambda w, t, m="": console_log(f'Task "{t.title}" assigned -> {w.name}')
        )
        daemon.pilot.on_workers_changed(lambda: console_log("Workers changed (add/remove)"))
        daemon.pilot.on_hive_empty(lambda: console_log("All workers gone", level="warn"))
        daemon.pilot.on_hive_complete(lambda: console_log("Hive complete — all tasks done"))

    daemon.task_board.on_change(lambda: console_log("Task board updated"))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    # If a restart was requested and the tunnel is running, auto-start it
    # after the new process comes up by checking for the marker file.
    if daemon.tunnel.consume_restart_marker():
        try:
            url = await daemon.tunnel.start()
            console_log(f"Tunnel auto-restarted: {url}")
        except Exception as exc:
            console_log(f"Tunnel auto-restart failed: {exc}", level="warn")

    await shutdown.wait()
    print("\nShutting down...", flush=True)

    # Save tunnel restart marker before stopping (only if restart requested)
    if app.get("restart_flag", {}).get("requested"):
        daemon.tunnel.save_restart_marker()

    await daemon.stop()
    try:
        await asyncio.wait_for(runner.cleanup(), timeout=5.0)
    except TimeoutError:
        _log.warning("shutdown: timed out waiting for HTTP runner cleanup")

    # If restart was requested (e.g. after update), replace process with new binary
    if app.get("restart_flag", {}).get("requested"):
        _exec_restart(daemon, startup_argv)


def _exec_restart(daemon: SwarmDaemon, startup_argv: list[str]) -> None:
    """Clear caches, release the daemon lock, and exec into a fresh process."""
    _clear_pycache()
    # Close DB connection before exec so the new process gets a clean connection
    if hasattr(daemon, "swarm_db") and daemon.swarm_db:
        try:
            daemon.swarm_db.checkpoint()
            daemon.swarm_db.close()
        except Exception:
            pass
    # Release daemon lock before exec so the new process image can acquire it
    lock_fd = getattr(daemon, "_lock_fd", None)
    if lock_fd is not None:
        try:
            os.close(lock_fd)
        except OSError:
            pass
    # Strip ``-c`` / ``--config`` from argv before exec.  Pre-fix a
    # legacy ``swarm.service`` ExecStart of
    # ``swarm serve -c ~/.config/swarm/config.yaml`` carried that
    # bypass through every reload.  The DB-first override at
    # ``_load_config_db_first`` now ignores it when the DB has data,
    # but once we know we're DB-canonical we should also stop
    # propagating the flag — otherwise the operator sees a
    # "ignoring --config X" WARNING on every restart even though
    # the value is moot.
    cleaned = _strip_config_flag(startup_argv)
    print("Restarting swarm...", flush=True)
    os.execv(cleaned[0], cleaned)


def _strip_config_flag(argv: list[str]) -> list[str]:
    """Return ``argv`` with any ``-c <path>`` / ``--config <path>`` removed.

    Handles all four forms: ``-c X``, ``-cX``, ``--config X``, ``--config=X``.
    """
    out: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-c" or a == "--config":
            i += 2  # skip flag and its value
            continue
        if a.startswith("-c") and len(a) > 2 and not a.startswith("--"):
            i += 1  # bundled ``-c<path>``
            continue
        if a.startswith("--config="):
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def _clear_pycache() -> None:
    """Remove all __pycache__ dirs under the swarm source tree.

    Forces Python to recompile from .py source on the next import,
    guaranteeing that a restart picks up code changes.
    """
    import shutil

    import swarm

    src_root = Path(swarm.__file__).resolve().parent
    for cache_dir in src_root.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)


def _reachable_addresses(host: str) -> list[str]:
    """Return a list of client-usable host addresses for the banner.

    ``0.0.0.0`` / ``::`` is a bind address ("listen on all interfaces"),
    NOT a client address.  Displaying it in the banner as
    ``http://0.0.0.0:9090`` is misleading and — on headless servers —
    actively harmful: operators logging in remotely copy the URL and
    then can't reach it, while modern Chrome (>=128, Private Network
    Access) explicitly blocks web origins loaded at 0.0.0.0 from
    opening WebSockets to themselves, which looks exactly like the
    "Connection lost, reconnecting" loop.

    Behaviour:
      * ``0.0.0.0`` / ``::`` / ``*``  → enumerate every non-loopback
        IPv4 address attached to the host, plus the system hostname
        (if it resolves to anything), plus ``localhost``/``127.0.0.1``.
        Order: public-looking IPs first (most useful for remote
        operators), hostname, then loopback (fallback for local dev).
      * Any other bind host (specific IP, a hostname) → return it
        as-is since the operator chose it deliberately.
    """
    is_wildcard = host in ("0.0.0.0", "::", "*", "")
    if not is_wildcard:
        return [host]

    import socket

    addrs: list[str] = []
    seen: set[str] = set()

    def _add(a: str) -> None:
        if a and a not in seen:
            seen.add(a)
            addrs.append(a)

    # Enumerate non-loopback IPv4 addresses from all interfaces.
    # getaddrinfo(hostname) pulls addresses via the resolver, which
    # covers most practical cases (WSL adapter, eth0, etc.).  We
    # deliberately skip IPv6 in the banner to keep it readable.
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = ""
    try:
        for info in socket.getaddrinfo(hostname or None, None, family=socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ("0.0.0.0",):
                _add(ip)
    except Exception:
        pass

    # Best-effort: also scan all configured interfaces via a UDP
    # connect trick — this catches interfaces that don't show up in
    # getaddrinfo(hostname), e.g. tunnels and secondary NICs.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 53))
            _add(s.getsockname()[0])
    except Exception:
        pass

    # Add the hostname itself if it's distinct and not just
    # ``localhost``.  Users connecting from the same LAN may reach
    # the box by hostname (mDNS, /etc/hosts, corporate DNS).
    if hostname and hostname != "localhost":
        _add(hostname)

    # Loopback goes last — useful for local dev, useless for headless.
    _add("localhost")

    return addrs


def _db_ground_truth_counts(daemon: SwarmDaemon) -> dict[str, int] | None:
    """Query the DB directly for what it actually contains.

    Returns a dict with keys ``workers``, ``groups``, ``config``,
    ``global_rules``, ``worker_rules`` or ``None`` if the query fails.
    Used by the startup banner to detect silent config-load failures:
    if the in-memory state doesn't match what the DB holds, the user
    is running against a stale/fallback config and the dashboard will
    show empty panels regardless of what's on disk.
    """
    try:
        row = daemon.swarm_db.fetchone(
            "SELECT "
            "  (SELECT COUNT(*) FROM workers) AS w,"
            "  (SELECT COUNT(*) FROM groups) AS g,"
            "  (SELECT COUNT(*) FROM config WHERE key != 'update_cache') AS c,"
            "  (SELECT COUNT(*) FROM approval_rules WHERE owner_type='global') AS gr,"
            "  (SELECT COUNT(*) FROM approval_rules WHERE owner_type='worker') AS wr"
        )
    except Exception:
        return None
    if not row:
        return None
    return {
        "workers": row["w"] or 0,
        "groups": row["g"] or 0,
        "config": row["c"] or 0,
        "global_rules": row["gr"] or 0,
        "worker_rules": row["wr"] or 0,
    }


def _print_banner(daemon: SwarmDaemon, host: str, port: int) -> None:
    """Print NestJS-style structured startup banner."""
    import importlib.metadata

    try:
        version = importlib.metadata.version("swarm-ai")
    except importlib.metadata.PackageNotFoundError:
        version = "dev"

    Y = "\033[33m"  # yellow/honey
    C = "\033[36m"  # cyan
    D = "\033[2m"  # dim
    B = "\033[1m"  # bold
    M = "\033[31m"  # red — used for loud mismatch warnings
    R = "\033[0m"  # reset

    # Two distinct counts:
    #   n_configured = workers defined in the loaded config (DB/YAML)
    #   n_running    = live Worker objects whose PTY process is
    #                  currently attached via the holder
    # On a fresh ``swarm start`` with no prior launches, n_running is
    # 0 and n_configured is everything in swarm.db — that is NORMAL
    # and NOT a mismatch.  The old banner conflated these and cried
    # "MISMATCH" every single startup.
    n_running = len(daemon.workers)
    n_configured = len(daemon.config.workers)
    n_groups = len(daemon.config.groups)
    n_global_rules = len(daemon.config.drones.approval_rules)
    drones_enabled = daemon.pilot.enabled if daemon.pilot else False
    interval = daemon.config.drones.poll_interval
    queen_model = getattr(daemon.config.queen, "model", "sonnet")
    task_summary = daemon.task_board.summary()

    # Ground truth from the DB itself (independent of whatever the
    # loader actually installed on self.config).
    db_counts = _db_ground_truth_counts(daemon)
    config_source = getattr(daemon.config, "config_source", "unknown")

    from swarm.update import build_sha

    sha = build_sha()
    sha_suffix = f" @ {sha}" if sha else ""

    # Resolve a list of client-usable addresses.  Never display
    # 0.0.0.0 — it's a bind address, not a client address, and
    # Chrome's Private Network Access rules treat it specially which
    # causes the exact "Connection lost, reconnecting" loop users
    # have been hitting.  On headless servers we enumerate all
    # non-loopback interface IPs so remote operators see a URL they
    # can actually paste into a browser.
    addrs = _reachable_addresses(host)
    primary = addrs[0]
    extras = addrs[1:]

    print(f"\n{Y}{B}Swarm WUI v{version}{sha_suffix}{R}", flush=True)
    print(f"  {D}├─{R} Dashboard:  {C}http://{primary}:{port}{R}", flush=True)
    for extra in extras:
        print(f"  {D}│{R}              {C}http://{extra}:{port}{R}", flush=True)
    print(f"  {D}├─{R} API:        {C}http://{primary}:{port}/api/health{R}", flush=True)
    print(f"  {D}├─{R} WebSocket:  {C}ws://{primary}:{port}/ws{R}", flush=True)

    # Config line — compares *configured* count against DB, not the
    # running count.  A MISMATCH here is a real bug (loader dropped
    # data).  The Workers line below shows running vs configured.
    source_label = {
        "db": "swarm.db",
        "yaml": "YAML fallback",
        "fresh": "fresh install (defaults)",
        "unknown": "unknown",
    }.get(config_source, config_source)
    loaded_summary = f"{n_configured} workers, {n_groups} groups, {n_global_rules} rules"
    if db_counts is not None and config_source == "db":
        db_summary = (
            f"{db_counts['workers']} workers, {db_counts['groups']} groups,"
            f" {db_counts['global_rules']} rules"
        )
        mismatch = (
            db_counts["workers"] != n_configured
            or db_counts["groups"] != n_groups
            or db_counts["global_rules"] != n_global_rules
        )
        if mismatch:
            print(
                f"  {D}├─{R} Config:     {M}{B}MISMATCH{R} "
                f"{source_label}  loaded={loaded_summary}  |  "
                f"db={db_summary}",
                flush=True,
            )
            print(
                f"  {D}│{R}             {M}⚠ The daemon loader dropped data on the "
                f"way in. Re-run with --log-level DEBUG to see why.{R}",
                flush=True,
            )
        else:
            print(
                f"  {D}├─{R} Config:     {source_label} ({loaded_summary})",
                flush=True,
            )
    elif (
        config_source in {"yaml", "fresh"}
        and db_counts
        and any(db_counts[k] for k in ("workers", "groups", "global_rules", "worker_rules"))
    ):
        # Fell back to YAML/defaults but the DB actually has data — LOUD.
        print(
            f"  {D}├─{R} Config:     {M}{B}{source_label}{R}  loaded={loaded_summary}",
            flush=True,
        )
        print(
            f"  {D}│{R}             {M}⚠ ~/.swarm/swarm.db contains "
            f"{db_counts['workers']} workers / {db_counts['global_rules']} rules "
            f"that are NOT loaded. Check log for DB load error.{R}",
            flush=True,
        )
    else:
        print(
            f"  {D}├─{R} Config:     {source_label} ({loaded_summary})",
            flush=True,
        )

    # Workers line shows running vs configured so "0 running" doesn't
    # look broken when the user just hasn't launched anything yet.
    if n_configured == 0:
        print(f"  {D}├─{R} Workers:    {Y}0{R} configured", flush=True)
    elif n_running == n_configured:
        print(
            f"  {D}├─{R} Workers:    {Y}{n_running}{R} running ({Y}{n_configured}{R} configured)",
            flush=True,
        )
    else:
        # Partial or no workers launched yet — normal on a fresh start.
        print(
            f"  {D}├─{R} Workers:    {Y}{n_running}{R} running, "
            f"{Y}{n_configured}{R} configured  "
            f"{D}(run `swarm launch -a` to start them){R}",
            flush=True,
        )
    drones_str = f"enabled (interval {interval}s)" if drones_enabled else "disabled"
    print(f"  {D}├─{R} Drones:     {drones_str}", flush=True)
    print(f"  {D}├─{R} Queen:      ready (model: {queen_model})", flush=True)
    # Auth status
    explicit_pw = os.environ.get("SWARM_API_PASSWORD") or daemon.config.api_password
    if explicit_pw:
        print(f"  {D}├─{R} Auth:       explicit password set", flush=True)
    else:
        from swarm.server.api import _auto_token

        print(
            f"  {D}├─{R} Auth:       auto-token {Y}{_auto_token[:12]}…{R}"
            f" (set SWARM_API_PASSWORD for persistent auth)",
            flush=True,
        )
    # Check cache-only for update info (no network call during startup)
    from swarm.update import check_for_update_sync

    cached = check_for_update_sync()
    if cached and cached.available:
        print(
            f"  {D}├─{R} Tasks:      {task_summary}",
            flush=True,
        )
        print(
            f"  {D}└─{R} Update:     {Y}{cached.remote_version}{R} available"
            f" (current: {cached.current_version})",
            flush=True,
        )
    else:
        print(f"  {D}└─{R} Tasks:      {task_summary}", flush=True)
    print(flush=True)


async def run_test_daemon(
    config: HiveConfig, host: str = "0.0.0.0", port: int | None = None, timeout: int = 300
) -> Path | None:
    """Run the daemon in test mode with auto-shutdown on completion or timeout.

    Returns the report file path, or None if no report was generated.
    Raises TimeoutError if the timeout is reached.
    """
    import signal

    from swarm.server.api import create_app

    port = port or config.test.port

    # Isolate test tasks from the main task board so they don't leak.
    test_store = FileTaskStore(path=Path.home() / ".swarm" / "test-tasks.json")
    daemon = SwarmDaemon(config, task_store=test_store)

    from swarm.pty.pool import ProcessPool

    pool = ProcessPool()
    await pool.ensure_holder()
    daemon.pool = pool

    await daemon.start()
    daemon._init_test_mode()

    app = create_app(daemon)

    shutdown = asyncio.Event()
    app["shutdown_event"] = shutdown
    report_result: dict[str, Path | None] = {"path": None}

    # Hook into broadcast_ws to detect test_report_ready
    def _on_ws_broadcast(data: dict[str, Any]) -> None:
        if data.get("type") == "test_report_ready":
            report_result["path"] = Path(data["path"])
            shutdown.set()

    daemon.hub._broadcast_hook = _on_ws_broadcast

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    _print_test_banner(daemon, host, port, timeout)
    _wire_test_console(daemon)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    timed_out = False
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        console_log(f"Test timeout reached ({timeout}s)", level="warn")

    # If we timed out without a report, try to generate one as fallback
    if timed_out and report_result["path"] is None:
        await daemon._generate_test_report_if_pending()
        # Check if the fallback produced a report via the test_log
        if daemon.test_runner.test_log is not None:
            report_dir = Path(daemon.test_runner.test_log.report_dir)
            # Find the most recent report
            reports = sorted(report_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            if reports:
                report_result["path"] = reports[0]

    print("\nShutting down test daemon...", flush=True)
    await daemon.stop()
    await runner.cleanup()

    if timed_out and report_result["path"] is None:
        raise TimeoutError(f"Test timed out after {timeout}s with no report")

    return report_result["path"]


def _wire_test_console(daemon: SwarmDaemon) -> None:
    """Wire pilot + daemon events to console_log with structured prefixes."""
    if daemon.pilot:
        daemon.pilot.on_state_changed(lambda w: console_log(f"[STATE] {w.name} -> {w.state.value}"))
        daemon.pilot.on_task_assigned(
            lambda w, t, m="": console_log(f'[TASK] "{t.title}" -> {w.name}')
        )
        daemon.pilot.on_workers_changed(lambda: console_log("[HIVE] Workers changed"))
        daemon.pilot.on_hive_empty(lambda: console_log("[HIVE] All workers gone", level="warn"))
        daemon.pilot.on_hive_complete(lambda: console_log("[HIVE] Complete — all tasks done"))

        # Drone decisions (skip NONE to reduce noise)
        if hasattr(daemon.pilot, "_emit_decisions"):
            daemon.pilot.on(
                "drone_decision",
                lambda w, content, d: (
                    console_log(f"[DRONE] {w.name}: {d.decision.value} — {d.reason}")
                    if d.decision != Decision.NONE
                    else None
                ),
            )

        daemon.pilot.on_escalate(
            lambda w, reason: console_log(f"[ESCALATE] {w.name}: {reason}", level="warn")
        )

    # Queen analysis events
    daemon.on(
        "queen_analysis",
        lambda wn, action, reasoning, conf: console_log(
            f"[QUEEN] {wn}: {action} (confidence={conf:.2f})"
        ),
    )

    daemon.task_board.on_change(lambda: console_log("[TASK] Board updated"))


def _print_test_banner(daemon: SwarmDaemon, host: str, port: int, timeout: int) -> None:
    """Print structured startup banner for test mode."""
    import importlib.metadata

    try:
        version = importlib.metadata.version("swarm-ai")
    except importlib.metadata.PackageNotFoundError:
        version = "dev"

    Y = "\033[33m"
    C = "\033[36m"
    D = "\033[2m"
    B = "\033[1m"
    R = "\033[0m"

    n_workers = len(daemon.workers)
    n_tasks = len(daemon.task_board.all_tasks)
    session = daemon.config.session_name

    # Same 0.0.0.0 → reachable-address treatment as the main banner.
    _test_addrs = _reachable_addresses(host)
    _primary = _test_addrs[0]
    print(f"\n{Y}{B}Swarm Test Runner v{version}{R}", flush=True)
    print(f"  {D}├─{R} Dashboard:  {C}http://{_primary}:{port}{R}", flush=True)
    for _extra in _test_addrs[1:]:
        print(f"  {D}│{R}              {C}http://{_extra}:{port}{R}", flush=True)
    print(f"  {D}├─{R} Workers:    {Y}{n_workers}{R} test worker(s)", flush=True)
    print(f"  {D}├─{R} Tasks:      {Y}{n_tasks}{R} loaded", flush=True)
    print(f"  {D}├─{R} Timeout:    {timeout}s", flush=True)
    print(f"  {D}├─{R} Session:    {session}", flush=True)
    print(f"  {D}└─{R} Port:       {port}", flush=True)
    print(flush=True)


_console_pipe_broken = False


def console_log(msg: str, level: str = "info") -> None:
    """Print a timestamped runtime event to the console.

    Silently stops logging after the first BrokenPipeError — the parent
    terminal is gone and further attempts would just flood the error log.
    """
    global _console_pipe_broken

    from datetime import datetime

    ts = datetime.now().strftime("%H:%M:%S")
    if level == "warn":
        prefix = "\033[33m⚠\033[0m"
    elif level == "error":
        prefix = "\033[31m✗\033[0m"
    else:
        prefix = " "
    try:
        print(f"[{ts}] {prefix} {msg}", flush=True)
        _console_pipe_broken = False
    except BrokenPipeError:
        if not _console_pipe_broken:
            _console_pipe_broken = True
