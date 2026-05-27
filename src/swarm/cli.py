"""CLI entry point for the swarm command."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from swarm.config import HiveConfig, load_config
from swarm.logging import get_logger, setup_logging, setup_logging_from_cli

_log = get_logger("cli")

_log_cli = __import__("logging").getLogger("swarm.cli")


def _load_cfg_from_swarm_db(config_path: str | None) -> HiveConfig:
    """Open swarm.db, run first-run migration if needed, and return
    a HiveConfig.  Returns a default HiveConfig on empty DB / load
    failure (the ERROR log is emitted in the failure path so callers
    can still surface the problem via the banner).
    """
    from swarm.db.config_store import load_config_from_db
    from swarm.db.core import _DEFAULT_DB_PATH, SwarmDB
    from swarm.db.migrate import auto_migrate

    db_pre_existed = _DEFAULT_DB_PATH.exists()
    try:
        db = SwarmDB()
    except Exception as exc:
        raise click.ClickException(
            f"Could not open swarm.db: {exc}\n"
            "Check permissions on ~/.swarm/ or pass a different HOME."
        ) from exc

    load_error: Exception | None = None
    try:
        if not db_pre_existed:
            _log_cli.info("no existing swarm.db — running initial migration from YAML")
            try:
                auto_migrate(db)
            except Exception:
                _log_cli.warning("auto_migrate raised; continuing", exc_info=True)
        try:
            cfg = load_config_from_db(db)
        except Exception as exc:
            load_error = exc
            cfg = None
    finally:
        db.close()

    if cfg is not None:
        if config_path:
            cfg.source_path = config_path
        cfg.config_source = "db"
        _log_cli.info(
            "config loaded from swarm.db (%d workers, %d groups, %d rules)",
            len(cfg.workers),
            len(cfg.groups),
            len(cfg.drones.approval_rules),
        )
        return cfg

    if load_error is not None:
        _log_cli.error(
            "DB config load FAILED — the daemon will start with an empty config. Error: %s: %s",
            type(load_error).__name__,
            load_error,
            exc_info=load_error,
        )
        cfg = HiveConfig()
        if config_path:
            cfg.source_path = config_path
        cfg.config_source = "yaml"
        return cfg

    _log_cli.info("fresh install: no DB or YAML content yet — using defaults")
    cfg = HiveConfig()
    if config_path:
        cfg.source_path = config_path
    cfg.config_source = "fresh"
    return cfg


def _load_config_db_first(config_path: str | None) -> HiveConfig:
    """Load config from swarm.db.  The DB is the source of truth.

    Architecture: swarm.db owns workers, groups, approval rules, tasks,
    history, secrets, etc.  YAML is a legacy seed format that feeds a
    **brand-new** DB on first run.  The daemon must never run against
    a YAML-sourced HiveConfig when the DB has data — doing so silently
    drops everything stored only in the DB (approval rules being the
    most painful example).

    Flow:
      1. If the caller passed an explicit ``--config path.yaml`` AND
         that file exists, treat it as an explicit override and load
         from YAML directly.  This is the "I know what I'm doing"
         escape hatch for ad-hoc YAML workflows and testing.
      2. Otherwise check whether ``~/.swarm/swarm.db`` exists on disk
         *before* we touch it.  If it does, it is the source of
         truth, full stop.  We open it and load from it.  Partially
         empty tables (e.g. no workers but rules) are NOT a trigger
         to re-migrate — the existing data stays exactly as it is.
      3. If the DB file does **not** exist, create it and run
         ``auto_migrate`` to seed from ``~/.config/swarm/config.yaml``
         and legacy files (tasks.json, etc.).
      4. Load the resulting config from the DB and return it.
      5. If after all of that the DB is still empty (truly fresh
         install, no YAML either), return a default HiveConfig so
         ``swarm init`` / first-run flows still work.
    """
    # DB is the source of truth.  The ``--config`` YAML override is
    # honoured ONLY when the DB doesn't yet have user data — i.e.
    # the test / fresh-install / explicit-YAML-bootstrap workflows.
    #
    # The hole this closes: a legacy systemd unit at
    # ``/etc/systemd/system/swarm.service`` (or
    # ``~/.config/systemd/user/swarm.service``) carries
    # ``ExecStart=swarm serve -c ~/.config/swarm/config.yaml`` from
    # the pre-DB era.  Every ``os.execv`` reload preserves that
    # argv, so the operator's restart silently flipped them onto the
    # YAML loader after months of dashboard-edited state piled up
    # in the DB.  Reported by Amanda 2026-05-05: workflows /
    # approval-rule / group edits "disappeared" across restart even
    # though the DB still had them — because the YAML didn't, and
    # the YAML was winning.
    if config_path and Path(config_path).exists():
        from swarm.db.core import _DEFAULT_DB_PATH, SwarmDB

        db_has_data = False
        if _DEFAULT_DB_PATH.exists():
            try:
                _probe_db = SwarmDB()
                row = _probe_db.fetchone(
                    "SELECT "
                    "  (SELECT COUNT(*) FROM workers) AS w,"
                    "  (SELECT COUNT(*) FROM groups) AS g,"
                    "  (SELECT COUNT(*) FROM config WHERE key != 'update_cache') AS c,"
                    "  (SELECT COUNT(*) FROM approval_rules) AS r"
                )
                _probe_db.close()
                if row and (row["w"] or row["g"] or row["c"] or row["r"]):
                    db_has_data = True
            except Exception:
                _log_cli.warning("DB probe before --config override failed", exc_info=True)

        if db_has_data:
            _log_cli.warning(
                "ignoring --config %s — swarm.db has user data and is the source of truth. "
                "If you really want to load from this YAML, wipe ~/.swarm/swarm.db first.",
                config_path,
            )
            # Fall through to DB-first load.  Pass None so
            # ``_load_cfg_from_swarm_db`` doesn't try to set source_path
            # back to the bypassed YAML.
            return _load_cfg_from_swarm_db(None)

        cfg = load_config(config_path)
        cfg.source_path = config_path
        cfg.config_source = "yaml"
        _log_cli.info(
            "config loaded from explicit --config %s (bypassing swarm.db)",
            config_path,
        )
        return cfg

    # Default path: DB is the source of truth.  The helper handles
    # first-run migration, load, error reporting, and fresh-default
    # fallback.  Extracted for complexity and testability.
    return _load_cfg_from_swarm_db(config_path)


if TYPE_CHECKING:
    from swarm.tasks.board import TaskBoard

# Default daemon API port
_DEFAULT_PORT = 9090


def _resolve_api_token(cfg: HiveConfig | None = None) -> str:
    """Return the Bearer token the CLI should send to the daemon.

    Resolution order mirrors the server's ``get_api_password`` helper:
    ``SWARM_API_PASSWORD`` env var first, then ``cfg.api_password`` from
    the loaded swarm.yaml.  Empty string means "no auth header" — correct
    for local installs that don't set a password.

    We send whatever is in the config as-is.  The daemon's
    ``verify_password`` accepts either the plaintext (if the stored value
    is hashed) or an exact token pass-through (if the CLI happens to have
    the same hash the daemon loaded), so both layouts work.
    """
    token = os.environ.get("SWARM_API_PASSWORD", "")
    if token:
        return token
    if cfg is not None and cfg.api_password:
        return cfg.api_password
    return ""


def _auth_headers(token: str) -> dict[str, str]:
    """Build the auth header dict for a CLI request.

    Returns an empty dict when *token* is empty so unprotected daemons
    continue to work without sending a bogus Bearer.
    """
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _api_get(port: int, path: str, *, token: str = "") -> dict[str, object]:
    """Make a GET request to the daemon API. Returns parsed JSON dict.

    If *token* is non-empty, sends ``Authorization: Bearer <token>``.
    Commands that need to reach a password-protected daemon should pass
    the result of ``_resolve_api_token(cfg)`` here.
    """
    import aiohttp

    url = f"http://localhost:{port}{path}"
    headers = _auth_headers(token)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise click.ClickException(f"API error ({resp.status}): {text}")
                return await resp.json()
    except aiohttp.ClientConnectorError:
        raise click.ClickException(f"Cannot connect to daemon on port {port}. Is swarm running?")


async def _api_post(
    port: int,
    path: str,
    json: dict[str, object] | None = None,
    *,
    token: str = "",
) -> dict[str, object]:
    """Make a POST request to the daemon API. Returns parsed JSON dict.

    If *token* is non-empty, sends ``Authorization: Bearer <token>``.
    """
    import aiohttp

    url = f"http://localhost:{port}{path}"
    headers = {"X-Requested-With": "swarm-cli", **_auth_headers(token)}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=json, headers=headers) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise click.ClickException(f"API error ({resp.status}): {text}")
                return await resp.json()
    except aiohttp.ClientConnectorError:
        raise click.ClickException(f"Cannot connect to daemon on port {port}. Is swarm running?")


class SwarmCLI(click.Group):
    """Route unknown subcommands as targets to ``start``."""

    def resolve_command(self, ctx: click.Context, args: list[str]) -> tuple:
        try:
            cmd_name, cmd, remaining = super().resolve_command(ctx, args)
            if cmd is not None:
                return cmd_name, cmd, remaining
        except click.UsageError:
            pass
        # Unknown command -- treat first arg as a target for 'start'
        start_cmd = self.get_command(ctx, "start")
        if start_cmd is not None:
            return "start", start_cmd, args
        raise click.UsageError(f"No such command '{args[0]}'.")


@click.group(cls=SwarmCLI, invoke_without_command=True)
@click.option(
    "--log-level",
    default="WARNING",
    envvar="SWARM_LOG_LEVEL",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    help="Logging verbosity",
)
@click.option(
    "--log-file", default=None, envvar="SWARM_LOG_FILE", type=click.Path(), help="Log to file"
)
@click.option(
    "--log-format",
    default="text",
    envvar="SWARM_LOG_FORMAT",
    type=click.Choice(["text", "json"], case_sensitive=False),
    help="Log output format",
)
@click.version_option(package_name="swarm-ai")
@click.pass_context
def main(ctx: click.Context, log_level: str, log_file: str | None, log_format: str) -> None:
    """Swarm -- a hive-mind for Claude Code agents.

    \b
    Run with a target name to launch directly:
        swarm rcg-v6           # launch 'rcg-v6' group + web dashboard
        swarm start default    # explicit 'start' subcommand
        swarm                  # start daemon + open web UI
    """
    # Stash CLI overrides on context so subcommands (start, serve, …)
    # can re-configure with config-file values once loaded.
    ctx.ensure_object(dict)
    ctx.obj["log_level"] = log_level
    ctx.obj["log_file"] = log_file
    ctx.obj["log_format"] = log_format

    # Configure logging unconditionally — pre-fix the bare ``swarm``
    # path (no subcommand → ``ctx.invoke(start_cmd)``) skipped this,
    # which meant ``_load_config_db_first`` ran before any handlers
    # were attached to the swarm logger.  Anything ``load_config_from_db``
    # logged on that path went to a handler-less root and was silently
    # dropped — including the diagnostic anchor we shipped in 2026.5.5.17
    # to triage Amanda's empty-workflows-on-restart symptom.  Subcommand
    # paths re-configure later via ``setup_logging_from_cli`` once cfg
    # is loaded; the early call here is harmless because ``setup_logging``
    # clears existing handlers before re-attaching.
    setup_logging(
        level=log_level,
        log_file=log_file,
        stderr=True,
        json_format=log_format == "json",
    )

    # No subcommand -> open the dashboard
    if ctx.invoked_subcommand is None:
        ctx.invoke(start_cmd)


def _read_db_state() -> dict[str, Any] | None:
    """Return a dict summarising the current ~/.swarm/swarm.db contents,
    or ``None`` if no DB file exists.  A dict with all-zero counts
    means the DB exists but is empty (e.g. just created by a prior
    tool run that never completed).
    """
    from swarm.db.core import _DEFAULT_DB_PATH, SwarmDB

    if not _DEFAULT_DB_PATH.exists():
        return None
    try:
        db = SwarmDB()
        row = db.fetchone(
            "SELECT "
            "  (SELECT COUNT(*) FROM workers) AS w,"
            "  (SELECT COUNT(*) FROM groups) AS g,"
            "  (SELECT COUNT(*) FROM approval_rules WHERE owner_type='global') AS gr,"
            "  (SELECT COUNT(*) FROM approval_rules WHERE owner_type='worker') AS wr,"
            "  (SELECT COUNT(*) FROM tasks) AS t"
        )
        db.close()
    except Exception as exc:
        return {"path": str(_DEFAULT_DB_PATH), "error": str(exc)}
    if not row:
        row = {"w": 0, "g": 0, "gr": 0, "wr": 0, "t": 0}
    return {
        "path": str(_DEFAULT_DB_PATH),
        "workers": row["w"] or 0,
        "groups": row["g"] or 0,
        "global_rules": row["gr"] or 0,
        "worker_rules": row["wr"] or 0,
        "tasks": row["t"] or 0,
    }


def _db_has_data(db_state: dict[str, Any] | None) -> bool:
    """True if the DB file exists and holds any user data."""
    if not db_state or "error" in db_state:
        return False
    return bool(
        db_state.get("workers")
        or db_state.get("groups")
        or db_state.get("global_rules")
        or db_state.get("worker_rules")
    )


def _print_db_status(db_state: dict[str, Any] | None, yaml_path: Path) -> None:
    """Print the current storage state to the operator at the top of init.

    Covers four real scenarios:
      1. DB has data — swarm is already configured, YAML is irrelevant
      2. DB exists but empty + YAML exists — migration will run on next start
      3. DB exists but empty + no YAML — blank slate, init will configure
      4. No DB yet, YAML exists / No DB yet, no YAML — blank slate too
    """
    click.echo("")
    click.echo("  Storage status")
    click.echo("  " + "─" * 60)

    if db_state is None:
        click.echo(f"    Database : {Path.home() / '.swarm' / 'swarm.db'}  (not yet created)")
    elif "error" in db_state:
        click.echo(f"    Database : {db_state['path']}  (error reading: {db_state['error']})")
    else:
        has_data = _db_has_data(db_state)
        label = "active" if has_data else "empty"
        click.echo(f"    Database : {db_state['path']}  ({label})")
        click.echo(f"               workers={db_state['workers']}  groups={db_state['groups']}")
        click.echo(
            f"               rules={db_state['global_rules']}  "
            f"worker_rules={db_state['worker_rules']}  tasks={db_state['tasks']}"
        )

    yaml_exists = yaml_path.exists()
    click.echo(f"    YAML     : {yaml_path}  ({'present' if yaml_exists else 'not yet created'})")
    click.echo("")


@main.command()
@click.option(
    "-d",
    "--dir",
    "projects_dir",
    type=click.Path(exists=True),
    help="Directory to scan for projects",
)
@click.option(
    "-o",
    "--output",
    "output_path",
    default=str(Path.home() / ".config" / "swarm" / "config.yaml"),
    help="Output config path (default: ~/.config/swarm/config.yaml)",
)
@click.option("--skip-hooks", is_flag=True, help="Skip Claude Code hooks installation")
@click.option("--skip-config", is_flag=True, help="Skip swarm.yaml generation")
def init(  # noqa: C901
    projects_dir: str | None,
    output_path: str,
    skip_hooks: bool,
    skip_config: bool,
) -> None:
    """Set up swarm: Claude Code hooks and swarm.yaml.

    On a fresh install, this ensures everything is ready to go.

    **Non-destructive to the database**: swarm stores its state
    (workers, groups, approval rules, tasks, queen sessions, etc.) in
    ``~/.swarm/swarm.db``.  ``swarm init`` only generates/edits the
    YAML template at ``~/.config/swarm/config.yaml`` and installs
    hooks/services — it never modifies the database.  On first run the
    daemon migrates the YAML into the DB once and then treats the DB
    as the source of truth.
    """
    checks: list[tuple[str, bool]] = []
    out_file = Path(output_path)

    # --- Storage state snapshot (drives every subsequent decision) ---
    db_state = _read_db_state()
    db_has_data = _db_has_data(db_state)
    _print_db_status(db_state, out_file)

    # --- Step 1: Install Claude Code hooks ---
    if not skip_hooks:
        from swarm.hooks.install import install

        install(global_install=True)
        click.echo("  Claude Code hooks installed globally")
        checks.append(("Claude Code hooks", True))
    else:
        click.echo("  Skipping hooks (--skip-hooks)")
        checks.append(("Claude Code hooks", None))

    # --- Step 2: Configure workers ---
    # Decision matrix (DB is the source of truth):
    #
    #   DB has data           → leave everything alone, YAML is
    #                           irrelevant.  Your config lives in the
    #                           DB already.  No prompt.
    #   DB empty, YAML present → the daemon will auto-migrate YAML→DB
    #                           on next ``swarm start``.  No prompt.
    #                           User can delete the YAML manually if
    #                           they want a true fresh start.
    #   DB empty, no YAML      → true first run.  Walk the project
    #                           scan + worker selection wizard.
    setup_proxy = False
    domain = ""

    if skip_config:
        click.echo("  Skipping swarm.yaml (--skip-config)")
        checks.append(("swarm.yaml generated", None))
    elif db_has_data:
        click.echo("  swarm.db already holds your configuration — nothing to write.")
        click.echo(
            "  (Edit workers / rules through the dashboard. "
            "Re-run with --skip-config to silence this section.)"
        )
        checks.append(("swarm.yaml generated", True))
    elif out_file.exists():
        # DB is empty but YAML exists — auto-migration will run on
        # next `swarm start`.  Nothing to ask here.
        click.echo(f"  YAML config present at {out_file}.")
        click.echo("  It will be migrated into swarm.db automatically on the next `swarm start`.")
        click.echo("  (To start from scratch instead: remove the file and re-run `swarm init`.)")
        checks.append(("swarm.yaml generated", True))
    else:
        # True first run — no DB, no YAML.  Run the scan + wizard to
        # seed a starter YAML that the daemon will then migrate into
        # the DB on first start.
        from swarm.config import discover_projects, write_config

        ported_settings: dict | None = None
        scan_dir = Path(projects_dir) if projects_dir else Path.home() / "projects"
        projects = discover_projects(scan_dir)

        if not projects:
            click.echo(f"\n  No git repos found in {scan_dir}")
            checks.append(("swarm.yaml generated", False))
        else:
            click.echo(f"\n  Found {len(projects)} projects in {scan_dir}:\n")
            for i, (name, path) in enumerate(projects):
                click.echo(f"    [{i + 1:2d}] {name:30s} {path}")

            click.echo("\n  Select workers (comma-separated numbers, 'a' for all):")
            selection = click.prompt("  ", default="a", show_default=False).strip()

            if selection.lower() == "a":
                selected = list(range(len(projects)))
            else:
                try:
                    selected = [int(x.strip()) - 1 for x in selection.split(",")]
                    selected = [i for i in selected if 0 <= i < len(projects)]
                except ValueError:
                    click.echo("  Invalid selection")
                    selected = []

            if selected:
                workers = [(projects[i][0], projects[i][1]) for i in selected]

                # Ask about groups
                groups: dict[str, list[str]] = {}
                if len(workers) > 1 and click.confirm("\n  Define custom groups?", default=False):
                    while True:
                        gname = click.prompt(
                            "    Group name (or Enter to finish)",
                            default="",
                            show_default=False,
                        ).strip()
                        if not gname:
                            break
                        click.echo("    Available:")
                        for i, (n, _) in enumerate(workers):
                            click.echo(f"      [{i + 1:2d}] {n}")
                        raw = click.prompt(
                            "    Members (numbers or names, comma-separated)"
                        ).strip()
                        member_names = []
                        for token in raw.split(","):
                            token = token.strip()
                            if not token:
                                continue
                            try:
                                idx = int(token) - 1
                                if 0 <= idx < len(workers):
                                    member_names.append(workers[idx][0])
                            except ValueError:
                                member_names.append(token)
                        groups[gname] = member_names
                        click.echo(f"    -> {gname}: {', '.join(member_names)}")

                groups["all"] = [n for n, _ in workers]

                # Ask for API password (no prior config to port from in this branch)
                click.echo(
                    "\n  API password enables login for the web dashboard."
                    "\n  Without one, the dashboard is open (fine for local-only use)."
                    "\n  Set a password if swarm will be internet-facing."
                )
                pw = click.prompt(
                    "  Set API password (Enter to skip)",
                    default="",
                    show_default=False,
                    hide_input=True,
                ).strip()
                api_password: str | None = pw if pw else None

                # Ask for domain (needed for passkey auth on remote access)
                domain = ""
                if api_password:
                    click.echo(
                        "\n  Domain is used for passkey (WebAuthn) authentication."
                        "\n  Set this to the hostname you'll use to access swarm remotely"
                        "\n  (e.g. swarm.example.com). Leave blank for localhost-only."
                    )
                    domain = click.prompt(
                        "  Domain (Enter to skip)",
                        default="",
                        show_default=False,
                    ).strip()

                # Ask about reverse proxy for public/remote servers
                trust_proxy = False
                if domain:
                    click.echo(
                        "\n  A reverse proxy provides HTTPS with automatic TLS certificates."
                        "\n  Recommended for internet-facing deployments."
                    )
                    setup_proxy = click.confirm("  Install Caddy as a reverse proxy?", default=True)
                    if setup_proxy:
                        trust_proxy = True

                extra_settings: dict[str, object] = {}
                if trust_proxy:
                    extra_settings["trust_proxy"] = True

                write_config(
                    output_path,
                    workers,
                    groups,
                    str(scan_dir),
                    api_password=api_password,
                    domain=domain,
                    ported_settings=ported_settings,
                    extra_settings=extra_settings,
                )
                click.echo(f"\n  Wrote {output_path} with {len(workers)} workers")
                checks.append(("swarm.yaml generated", True))
            else:
                click.echo("  No workers selected")
                checks.append(("swarm.yaml generated", False))

    # --- Step 3: Install systemd service ---
    from swarm.service import (
        _PLIST_PATH,
        _SERVICE_PATH,
        _check_systemd,
        enable_wsl_systemd,
        install_launchd,
        is_macos,
        is_wsl,
    )
    from swarm.service import (
        install_service as _install_svc,
    )

    systemd_err = _check_systemd()
    if systemd_err and is_wsl():
        click.echo("\n  systemd is not enabled in WSL.")
        if click.confirm("  Enable systemd in /etc/wsl.conf? (requires sudo)", default=True):
            try:
                enable_wsl_systemd()
                click.echo("  Enabled! Restart WSL to activate: wsl --shutdown")
                checks.append(("systemd service", "RESTART"))
            except Exception as e:
                click.echo(f"  Failed: {e}", err=True)
                checks.append(("systemd service", False))
        else:
            checks.append(("systemd service", None))
    elif systemd_err and is_macos():
        if _PLIST_PATH.exists():
            checks.append(("launchd service", True))
        else:
            try:
                install_launchd(output_path if not skip_config else None)
                checks.append(("launchd service", True))
            except Exception:
                checks.append(("launchd service", False))
    elif systemd_err:
        click.echo(f"  {systemd_err}")
        checks.append(("background service", None))
    elif _SERVICE_PATH.exists():
        checks.append(("systemd service", True))
    else:
        try:
            _install_svc(output_path if not skip_config else None)
            checks.append(("systemd service", True))
        except Exception:
            checks.append(("systemd service", False))

    # --- Step 4: Install reverse proxy (Caddy) ---
    if setup_proxy and domain:
        from swarm.reverse_proxy import setup_caddy

        click.echo("\n  Setting up Caddy reverse proxy...")
        try:
            if setup_caddy(domain):
                click.echo(f"  Caddy configured for https://{domain} -> localhost:9090")
                checks.append(("reverse proxy (Caddy)", True))
            else:
                click.echo("  Caddy setup failed. You can set it up manually later.", err=True)
                checks.append(("reverse proxy (Caddy)", False))
        except Exception as e:
            click.echo(f"  Caddy setup error: {e}", err=True)
            checks.append(("reverse proxy (Caddy)", False))
    elif domain:
        # Domain set but proxy declined — remind about manual setup
        checks.append(("reverse proxy (Caddy)", None))
    # (no check line when no domain — not applicable)

    # --- Step 5: WSL auto-start on Windows boot ---
    from swarm.service import install_wsl_startup, wsl_startup_installed

    if is_wsl():
        if wsl_startup_installed():
            checks.append(("WSL auto-start", True))
        else:
            try:
                vbs = install_wsl_startup()
                checks.append(("WSL auto-start", vbs is not None))
            except Exception:
                checks.append(("WSL auto-start", False))
    else:
        checks.append(("WSL auto-start", None))

    # --- Summary ---
    click.echo("\n  System readiness:")
    needs_restart = False
    for label, status in checks:
        if status is True:
            indicator = "OK"
        elif status is False:
            indicator = "FAIL"
        elif status == "RESTART":
            indicator = "PEND"
            needs_restart = True
        else:
            indicator = "SKIP"
        click.echo(f"    [{indicator:4s}] {label}")

    if needs_restart:
        click.echo("\n  Restart WSL (wsl --shutdown) then re-run: swarm init")
    all_ok = all(s is not False for _, s in checks)
    if all_ok and not needs_restart:
        if domain:
            click.echo(f"\n  Ready! Dashboard: https://{domain}")
            click.echo(
                "\n  Note: Ensure ports 80 and 443 are open in your firewall/security group."
            )
        click.echo("\n  Ready! Next: swarm start all")
    elif not all_ok:
        click.echo("\n  Some checks failed -- see above.", err=True)


def _show_available(cfg: HiveConfig) -> None:
    """Print available groups and workers for interactive selection."""
    num_groups = len(cfg.groups)
    click.echo("Groups:")
    for i, g in enumerate(cfg.groups):
        members = ", ".join(g.workers)
        click.echo(f"  [{i + 1:2d}] {g.name:20s} {members}")
    click.echo("\nIndividual workers:")
    for i, w in enumerate(cfg.workers):
        click.echo(f"  [{num_groups + i + 1:2d}] {w.name}")
    click.echo("\nUsage: swarm <name|number> or swarm start -a")


def _resolve_launch_workers(
    cfg: HiveConfig, group: str | None, launch_all: bool
) -> list[str] | None:
    """Resolve which workers to launch. Returns names or None (show help)."""
    if launch_all:
        return [w.name for w in cfg.workers]
    target = group or cfg.default_group
    if not target:
        return None
    _name, resolved = _resolve_target(cfg, target)
    if resolved is None:
        label = "default_group" if not group else "group or worker"
        click.echo(f"Unknown {label}: '{target}'\n")
        _show_available(cfg)
        return None
    return [w.name for w in resolved]


@main.command()
@click.argument("group", required=False)
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option("-a", "--all", "launch_all", is_flag=True, help="Launch all workers")
@click.option("--port", default=None, type=int, help="Daemon API port (default: config or 9090)")
def launch(group: str | None, config_path: str | None, launch_all: bool, port: int | None) -> None:
    """Launch workers via the running daemon."""
    cfg = _load_config_db_first(config_path)
    errors = cfg.validate()
    if errors:
        for e in errors:
            click.echo(f"Config error: {e}", err=True)
        raise SystemExit(1)

    worker_names = _resolve_launch_workers(cfg, group, launch_all)
    if worker_names is None:
        _show_available(cfg)
        return

    api_port = port or cfg.port

    async def _launch() -> None:
        try:
            data = await _api_post(
                api_port,
                "/api/workers/launch",
                {"workers": worker_names},
                token=_resolve_api_token(cfg),
            )
            launched = data.get("launched", [])
            if launched:
                click.echo(f"Launched {len(launched)} worker(s): {', '.join(launched)}")
            else:
                click.echo("No new workers to launch (already running).")
        except Exception as e:
            click.echo(f"Cannot reach daemon at localhost:{api_port}: {e}", err=True)
            click.echo("Is the daemon running? Start it with: swarm start", err=True)
            raise SystemExit(1)

    asyncio.run(_launch())


def _resolve_target(cfg: HiveConfig, target: str) -> tuple[str, list[object] | None]:
    """Resolve a target as group name, worker name, or number.

    Returns (session_name, workers) if resolved, or (target, None) if not found.
    """
    num_groups = len(cfg.groups)

    # Try as a number first
    try:
        idx = int(target) - 1
        if 0 <= idx < num_groups:
            group_name = cfg.groups[idx].name
            return group_name, cfg.get_group(group_name)
        elif num_groups <= idx < num_groups + len(cfg.workers):
            w = cfg.workers[idx - num_groups]
            return w.name, [w]
    except ValueError:
        pass

    # Try as a group name (case-insensitive)
    try:
        workers = cfg.get_group(target)
        return target, workers
    except ValueError:
        pass

    # Try as a worker name (case-insensitive)
    w = cfg.get_worker(target)
    if w:
        return w.name, [w]

    return target, None


@main.command()
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(),
    default=None,
    help="Path to swarm config (optional — loads from DB if available)",
)
@click.option("--host", default="localhost", help="Host to bind to")
@click.option("--port", default=None, type=int, help="Port to serve on (default: config or 9090)")
@click.pass_context
def serve(ctx: click.Context, config_path: str | None, host: str, port: int | None) -> None:
    """Serve the Bee Hive web dashboard."""
    from swarm.server.daemon import run_daemon

    cfg = _load_config_db_first(config_path)

    # Re-exec into dev venv when SWARM_DEV is set — but NOT under systemd
    # (the service unit already uses `uv run` for dev installs).
    if os.environ.get("SWARM_DEV") and not os.environ.get("INVOCATION_ID"):
        from swarm.update import get_local_source_path

        source = get_local_source_path()
        if source:
            click.echo(f"SWARM_DEV detected — switching to dev mode from {source}")
            os.chdir(source)
            os.execvp("uv", ["uv", "run", "swarm", *sys.argv[1:]])

    port = port or cfg.port

    # Re-configure logging with config values (stderr stays on for serve)
    setup_logging_from_cli(ctx.obj or {}, cfg)

    # Ensure hooks are up to date on every daemon start
    from swarm.hooks.install import install

    install(global_install=True)

    asyncio.run(run_daemon(cfg, host=host, port=port))


@main.command("start")
@click.argument("target", required=False)
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option("--host", default="0.0.0.0", help="Host to bind to (default: all interfaces)")
@click.option("--port", default=None, type=int, help="Port to serve on (default: config or 9090)")
@click.option("--no-browser", is_flag=True, help="Don't auto-open the browser")
@click.option("--test", "test_mode", is_flag=True, help="Run in test mode with synthetic project")
@click.pass_context
def start_cmd(  # noqa: C901
    ctx: click.Context,
    target: str | None,
    config_path: str | None,
    host: str,
    port: int | None,
    no_browser: bool,
    test_mode: bool,
) -> None:
    """Launch workers and open the web dashboard.

    TARGET can be a group name, worker name, or number.
    Workers are launched by the daemon automatically.

    \b
    Examples:
        swarm                  # start daemon, open web UI
        swarm rcg-v6           # launch 'rcg-v6' group, open web UI
        swarm start default    # explicit 'start' subcommand
    """
    import webbrowser

    from swarm.server.daemon import run_daemon

    # DB is the source of truth — read workers/groups/rules from
    # swarm.db, not the YAML.  The old code path (``load_config``)
    # silently served a stale YAML snapshot while the real state sat
    # in the DB, producing the "1 worker in dashboard / 3 workers in
    # DB" split brain reported by users.
    cfg = _load_config_db_first(config_path)

    # Re-exec into dev venv when SWARM_DEV is set — but NOT under systemd
    if os.environ.get("SWARM_DEV") and not os.environ.get("INVOCATION_ID"):
        from swarm.update import get_local_source_path

        source = get_local_source_path()
        if source:
            click.echo(f"SWARM_DEV detected — switching to dev mode from {source}")
            os.chdir(source)
            os.execvp("uv", ["uv", "run", "swarm", *sys.argv[1:]])

    port = port or cfg.port

    setup_logging_from_cli(ctx.obj or {}, cfg)

    if target:
        session_name, workers = _resolve_target(cfg, target)
        if workers is not None:
            errors = cfg.validate()
            if errors:
                for e in errors:
                    click.echo(f"Config error: {e}", err=True)
                raise SystemExit(1)
            cfg.session_name = session_name
        else:
            # Unresolved target -- use it as session name (daemon will handle)
            cfg.session_name = target
    elif cfg.default_group:
        session_name, workers = _resolve_target(cfg, cfg.default_group)
        if workers is not None:
            cfg.session_name = session_name
        else:
            cfg.session_name = cfg.default_group

    # --- Test mode setup ---
    test_project_mgr = None
    if test_mode:
        test_project_mgr, project_dir = _setup_test_config(cfg)
        click.echo(f"TEST MODE: synthetic project at {project_dir}")

    url = f"http://{host}:{port}"
    if not no_browser:

        def _open_browser() -> None:
            import subprocess
            import time

            time.sleep(0.8)  # let the server start
            try:
                subprocess.Popen(
                    ["xdg-open", url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                webbrowser.open(url)

        import threading

        threading.Thread(target=_open_browser, daemon=True).start()

    try:
        asyncio.run(run_daemon(cfg, host=host, port=port, test_mode=test_mode))
    finally:
        if test_project_mgr:
            test_project_mgr.cleanup()


def _setup_test_config(cfg: HiveConfig) -> tuple[object, Path]:
    """Set up test mode: create synthetic project, override config for single test worker.

    Returns (TestProjectManager, project_dir).
    """
    from swarm.config import GroupConfig, WorkerConfig
    from swarm.testing.project import TestProjectManager

    mgr = TestProjectManager()
    try:
        project_dir = mgr.setup()
    except FileNotFoundError as e:
        click.echo(f"Test mode error: {e}", err=True)
        raise SystemExit(1)

    cfg.workers = [WorkerConfig(name="test-worker", path=str(project_dir))]
    cfg.groups = [GroupConfig(name="all", workers=["test-worker"])]
    cfg.test.enabled = True
    cfg.drones.auto_stop_on_complete = True
    return mgr, project_dir


@main.command("test")
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option(
    "--port",
    default=None,
    type=int,
    help="Port for test dashboard (default: config test.port or 9091)",
)
@click.option(
    "--timeout", default=300, type=int, help="Max seconds before auto-shutdown (default: 300)"
)
@click.option("--no-cleanup", is_flag=True, help="Keep temp dir after test")
@click.option(
    "--pin-model",
    default=None,
    help="Pin the model id in the test run's infra snapshot (reproducibility aid)",
)
@click.pass_context
def test_cmd(
    ctx: click.Context,
    config_path: str | None,
    port: int | None,
    timeout: int,
    no_cleanup: bool,
    pin_model: str | None,
) -> None:
    """Run orchestration tests on a dedicated port with auto-shutdown.

    Launches a synthetic test project, runs the daemon on a side port (default 9091),
    monitors progress, generates a report, and exits.

    \b
    Examples:
        swarm test                    # run on :9091 with 5min timeout
        swarm test --port 9092        # custom port
        swarm test --timeout 120      # 2min timeout
        swarm test --no-cleanup       # keep temp dir after test
        swarm test --pin-model=claude-opus-4-7  # record model in report
    """
    import uuid

    from swarm.server.daemon import run_test_daemon

    cfg = load_config(config_path)
    port = port or cfg.test.port
    if pin_model:
        cfg.test.pin_model = pin_model

    setup_logging_from_cli(ctx.obj or {}, cfg)

    session_name = f"swarm-test-{uuid.uuid4().hex[:8]}"
    cfg.session_name = session_name

    test_project_mgr, project_dir = _setup_test_config(cfg)
    click.echo(f"TEST: synthetic project at {project_dir}")
    click.echo(f"TEST: session={session_name}, port={port}, timeout={timeout}s")

    report_path = None
    exit_code = 0
    try:
        report_path = asyncio.run(run_test_daemon(cfg, host="0.0.0.0", port=port, timeout=timeout))
    except KeyboardInterrupt:
        click.echo("\nTest interrupted by user")
        exit_code = 1
    except TimeoutError:
        click.echo(f"\nTest timed out after {timeout}s")
        exit_code = 2

    if report_path:
        _print_report_summary(report_path)
    else:
        click.echo("No test report generated.")

    if not no_cleanup:
        _cleanup_test(test_project_mgr)
    else:
        click.echo(f"Skipping cleanup (--no-cleanup). Session: {session_name}")

    raise SystemExit(exit_code)


def _cleanup_test(mgr: object) -> None:
    """Clean up temp directory and test tasks."""
    if hasattr(mgr, "cleanup"):
        mgr.cleanup()
    # Remove isolated test task board so it doesn't accumulate stale tasks
    test_tasks_path = Path.home() / ".swarm" / "test-tasks.json"
    test_tasks_path.unlink(missing_ok=True)


def _print_report_summary(path: Path) -> None:
    """Print the Summary section from a markdown test report."""
    click.echo(f"\nReport: {path}")
    try:
        text = path.read_text()
    except OSError:
        return
    # Extract ## Summary section
    in_summary = False
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## Summary"):
            in_summary = True
            continue
        if in_summary:
            if line.startswith("## "):
                break
            lines.append(line)
    if lines:
        click.echo("\n".join(lines).strip())


@main.command("analyze-tools")
@click.option(
    "--since",
    "since",
    default="7d",
    help="Window to analyze — '1h', '3d', '2w' (default: 7d).",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(),
    default=None,
    help="Path to swarm.db (default: ~/.swarm/swarm.db).",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Emit a machine-readable JSON report instead of a table.",
)
def analyze_tools_cmd(since: str, db_path: str | None, json_output: bool) -> None:
    """Summarize MCP tool usage from the buzz log.

    Reads ``mcp:*`` entries the daemon has written, groups them by
    tool, and prints call counts, error counts, active workers, and
    up to five distinct error snippets per tool. Use this to spot
    tools with high error rates — candidates for better descriptions
    or tighter schemas (see Phase 4.1b for the rewrite loop).
    """
    import json as _json
    import time
    from pathlib import Path as _Path

    from swarm.analysis.tool_usage import aggregate
    from swarm.db.core import SwarmDB

    window_seconds = _parse_window(since)
    if window_seconds is None:
        click.echo(f"Could not parse --since={since!r}. Try '1h', '3d', '2w'.", err=True)
        raise SystemExit(2)
    cutoff = time.time() - window_seconds

    path = _Path(db_path).expanduser() if db_path else None
    db = SwarmDB(path) if path else SwarmDB()
    try:
        rows = db.fetchall(
            """
            SELECT timestamp, action, worker_name, detail, category
            FROM buzz_log
            WHERE detail LIKE 'mcp:%' AND timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (cutoff,),
        )
        entries = [
            {
                "timestamp": r["timestamp"],
                "worker_name": r["worker_name"],
                "detail": r["detail"],
            }
            for r in rows
        ]
        stats = aggregate(entries)
    finally:
        db.close()

    if json_output:
        click.echo(
            _json.dumps(
                {
                    "window_seconds": window_seconds,
                    "total_entries": len(entries),
                    "tools": [s.to_report_row() for s in stats],
                },
                indent=2,
            )
        )
        return

    _print_tool_usage_table(stats, since, len(entries))


def _parse_window(spec: str) -> float | None:
    """Parse a shorthand like '3d' or '12h' into seconds."""
    spec = spec.strip().lower()
    if len(spec) < 2 or not spec[:-1].replace(".", "", 1).isdigit():
        return None
    unit = spec[-1]
    try:
        n = float(spec[:-1])
    except ValueError:
        return None
    multipliers = {"m": 60, "h": 3600, "d": 86400, "w": 86400 * 7}
    if unit not in multipliers:
        return None
    return n * multipliers[unit]


def _print_tool_usage_table(stats: list, since: str, total: int) -> None:
    click.echo(f"MCP tool usage (last {since}): {total} calls across {len(stats)} tools")
    if not stats:
        click.echo("  (no mcp:* entries in window)")
        return
    click.echo("")
    click.echo(f"  {'tool':<28} {'calls':>6} {'errors':>6} {'err%':>5}  workers")
    click.echo(f"  {'-' * 28} {'-' * 6} {'-' * 6} {'-' * 5}  {'-' * 24}")
    for s in stats:
        workers = ",".join(sorted(s.workers)[:4]) or "-"
        err_pct = f"{int(s.error_rate * 100):>3}%"
        click.echo(f"  {s.tool:<28} {s.calls:>6} {s.errors:>6} {err_pct:>5}  {workers}")
    # Error samples grouped below so the table stays scannable.
    any_errors = False
    for s in stats:
        if not s.error_samples:
            continue
        if not any_errors:
            click.echo("\nError samples:")
            any_errors = True
        click.echo(f"  {s.tool}:")
        for sample in s.error_samples:
            click.echo(f"    • {sample[:140]}")


@main.group()
def queen() -> None:
    """Queen-specific administration (CLAUDE.md sync, etc.)."""


@queen.command("sync-claude-md")
@click.option(
    "--accept-shipped",
    "mode_accept",
    is_flag=True,
    help="Overwrite on-disk CLAUDE.md with the current shipped QUEEN_SYSTEM_PROMPT.",
)
@click.option(
    "--keep-local",
    "mode_keep",
    is_flag=True,
    help="Update the shipped-at-last-sync marker only; preserve local on-disk edits.",
)
def queen_sync_claude_md(mode_accept: bool, mode_keep: bool) -> None:
    """Reconcile the Queen's ~/.swarm/queen/workdir/CLAUDE.md with the shipped prompt.

    Without flags, show the current state (shipped vs on-disk) without
    writing anything.  See task #254 for the full reconcile design.
    """
    from swarm.queen.runtime import (
        CLAUDE_MD_FILENAME,
        DRIFT_SHIPPED_LAST_SUFFIX,
        DRIFT_SHIPPED_LATEST_SUFFIX,
        QUEEN_SYSTEM_PROMPT,
        QUEEN_WORK_DIR,
        SHIPPED_MARKER_FILENAME,
        sync_queen_claude_md,
    )

    if mode_accept and mode_keep:
        raise click.UsageError("--accept-shipped and --keep-local are mutually exclusive")

    if not mode_accept and not mode_keep:
        # Status-only mode.  Report the three-way state; don't touch anything.
        target = QUEEN_WORK_DIR / CLAUDE_MD_FILENAME
        marker = QUEEN_WORK_DIR / SHIPPED_MARKER_FILENAME
        latest_path = QUEEN_WORK_DIR / f"{CLAUDE_MD_FILENAME}{DRIFT_SHIPPED_LATEST_SUFFIX}"
        last_path = QUEEN_WORK_DIR / f"{CLAUDE_MD_FILENAME}{DRIFT_SHIPPED_LAST_SUFFIX}"
        if not target.exists():
            click.echo(f"{target}: does not exist — will be seeded on next daemon start")
            return
        on_disk = target.read_text()
        shipped_last = marker.read_text() if marker.exists() else None
        click.echo(f"workdir:           {QUEEN_WORK_DIR}")
        click.echo(f"live file:         {target.name}  ({len(on_disk)} chars)")
        click.echo(
            f"shipped marker:    {marker.name}  "
            f"({'present' if shipped_last is not None else 'missing'})"
        )
        click.echo(f"shipped constant:  QUEEN_SYSTEM_PROMPT  ({len(QUEEN_SYSTEM_PROMPT)} chars)")
        click.echo("")
        shipped_unchanged = shipped_last == QUEEN_SYSTEM_PROMPT
        local_edits = (shipped_last is not None) and (on_disk != shipped_last)
        if shipped_last is None:
            click.echo("status:  marker missing — will baseline from on-disk on next boot")
        elif shipped_unchanged and not local_edits:
            click.echo("status:  clean — shipped unchanged, no local edits")
        elif shipped_unchanged and local_edits:
            click.echo("status:  local edits only (no shipped change to merge)")
        elif not shipped_unchanged and not local_edits:
            click.echo("status:  shipped updated — will auto-apply on next boot")
        else:
            click.echo("status:  DRIFT — shipped updated AND local edits present")
            if latest_path.exists() and last_path.exists():
                click.echo(f"         diff refs: {latest_path.name}, {last_path.name}")
            click.echo("         run with --accept-shipped or --keep-local to reconcile")
        return

    mode = "accept-shipped" if mode_accept else "keep-local"
    result = sync_queen_claude_md(mode)
    click.echo(f"{result.action}: {result.details}")


@queen.command("contribute-claude-md")
@click.option(
    "--emit-patch",
    "emit_patch_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write a git apply-able unified diff of local → shipped to this path.",
)
@click.option(
    "--open-pr",
    "open_pr_flag",
    is_flag=True,
    help=(
        "Apply the rewrite against a detected swarm repo checkout, commit, "
        "push, and open a PR via `gh pr create`."
    ),
)
@click.option(
    "--mark-synced",
    "mark_synced_flag",
    is_flag=True,
    help="After an upstream merge, update `.claude_md_shipped` so the reconcile"
    " no longer flags the promoted hunks as drift.",
)
@click.option(
    "--repo-root",
    "repo_root_opt",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Path to the swarm repo checkout (default: auto-detect from ~/projects).",
)
def queen_contribute_claude_md(
    emit_patch_path: Path | None,
    open_pr_flag: bool,
    mark_synced_flag: bool,
    repo_root_opt: Path | None,
) -> None:
    """Promote local CLAUDE.md edits back to the shipped QUEEN_SYSTEM_PROMPT.

    Companion to ``swarm queen sync-claude-md`` (#254) — that pulls
    shipped → local on daemon start, this pushes local → shipped on
    operator demand.  Every diff is a candidate for upstream: the Queen
    is a global role, not an operator-specific one, so any local
    improvement applies to every install.

    Modes are mutually exclusive.  No flags = status mode (show diff
    summary, no writes).  See task #258 for the full design.
    """
    from swarm.queen.contribute import (
        compute_status,
        detect_repo_root,
        mark_synced,
        open_pr,
    )
    from swarm.queen.contribute import (
        emit_patch as do_emit_patch,
    )

    flags_set = sum(1 for f in (emit_patch_path, open_pr_flag, mark_synced_flag) if f)
    if flags_set > 1:
        raise click.UsageError("--emit-patch, --open-pr, and --mark-synced are mutually exclusive")

    if mark_synced_flag:
        marker = mark_synced()
        click.echo(f"marked synced: {marker}")
        return

    status = compute_status()

    if not status.in_sync:
        click.echo(f"local vs shipped: {status.hunk_count} hunk(s) differ")
    else:
        click.echo("local and shipped are in sync — nothing to contribute")
        return

    if not emit_patch_path and not open_pr_flag:
        # Status mode: show the diff, don't write anything.
        click.echo("")
        click.echo(status.diff)
        click.echo("")
        click.echo("Run with --emit-patch <path> to write a git apply-able patch,")
        click.echo("or --open-pr to apply + commit + push + `gh pr create` in one step.")
        return

    repo_root = repo_root_opt or detect_repo_root()
    if repo_root is None:
        raise click.UsageError("could not auto-detect swarm repo; pass --repo-root <path>")

    if emit_patch_path:
        result = do_emit_patch(emit_patch_path, repo_root=repo_root)
        click.echo(
            f"wrote {result.bytes_written} bytes to {result.path} "
            f"({result.hunk_count} hunk(s), target {result.target_rel_path})"
        )
        click.echo("")
        click.echo("To apply:")
        click.echo(f"  cd {repo_root} && git apply {result.path}")
        click.echo("After upstream merges, run:")
        click.echo("  swarm queen contribute-claude-md --mark-synced")
        return

    # open_pr_flag
    pr = open_pr(repo_root=repo_root)
    if pr.pr_url:
        click.echo(pr.message)
        click.echo(
            "After merge, run `swarm queen contribute-claude-md --mark-synced` "
            "to clear the drift marker."
        )
    else:
        click.echo(f"PR flow did not complete: {pr.message}", err=True)
        raise SystemExit(1)


@main.group()
def web() -> None:
    """Manage the web dashboard (background process)."""


@web.command()
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option("--host", default="0.0.0.0", help="Host to bind to (default: all interfaces)")
@click.option("--port", default=None, type=int, help="Port to serve on (default: config or 9090)")
def start(config_path: str | None, host: str, port: int | None) -> None:
    """Start the web dashboard in the background."""
    from swarm.server.webctl import web_start

    cfg = _load_config_db_first(config_path)
    port = port or cfg.port
    ok, msg = web_start(host=host, port=port, config_path=config_path)
    click.echo(msg)
    if ok:
        from swarm.server.webctl import _WEB_LOG_FILE

        click.echo(f"  Logs: {_WEB_LOG_FILE}")
        click.echo("  Stop with: swarm web stop")


@web.command("stop")
def web_stop_cmd() -> None:
    """Stop the background web dashboard."""
    from swarm.server.webctl import web_stop

    _ok, msg = web_stop()
    click.echo(msg)


@web.command("status")
def web_status() -> None:
    """Check if the web dashboard is running."""
    from swarm.server.webctl import web_is_running

    pid = web_is_running()
    if pid:
        click.echo(f"Web dashboard is running (PID {pid})")
    else:
        click.echo("Web dashboard is not running")


@main.command()
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option("--port", default=None, type=int, help="Daemon API port (default: config or 9090)")
def status(config_path: str | None, port: int | None) -> None:
    """One-shot status check of all workers via daemon API."""
    cfg = _load_config_db_first(config_path)
    api_port = port or cfg.port

    async def _status() -> None:
        try:
            data = await _api_get(api_port, "/api/workers", token=_resolve_api_token(cfg))
        except Exception as e:
            click.echo(f"Cannot reach daemon at localhost:{api_port}: {e}", err=True)
            click.echo("Is the daemon running? Start it with: swarm start", err=True)
            raise SystemExit(1)

        workers = data.get("workers", [])
        if not workers:
            click.echo("No workers registered with the daemon.")
            return

        # State indicators matching WorkerState
        indicators = {
            "buzzing": ".",
            "waiting": "?",
            "resting": "~",
            "sleeping": "z",
            "stung": "!",
        }

        for w in workers:
            name = w.get("name", "?")
            state = w.get("state", "unknown").lower()
            indicator = indicators.get(state, " ")
            click.echo(f"  {indicator} {name:20s} [{state}]")

    asyncio.run(_status())


@main.command("check-states")
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option("--port", default=None, type=int, help="Daemon API port (default: config or 9090)")
def check_states(config_path: str | None, port: int | None) -> None:
    """Show current worker states from the daemon.

    With PTY-based process management, state is always fresh (read from the
    ring buffer), so this command simply displays the current states.
    """
    cfg = _load_config_db_first(config_path)
    api_port = port or cfg.port

    async def _check_states() -> None:
        try:
            data = await _api_get(api_port, "/api/workers", token=_resolve_api_token(cfg))
        except Exception as e:
            click.echo(f"Cannot reach daemon at localhost:{api_port}: {e}", err=True)
            click.echo("Is the daemon running? Start it with: swarm start", err=True)
            raise SystemExit(1)

        workers = data.get("workers", [])
        if not workers:
            click.echo("No workers registered with the daemon.")
            return

        # Print table header
        click.echo(f"{'Worker':<20s} {'State':<12s} {'Duration'}")
        for w in workers:
            name = w.get("name", "?")
            state = w.get("state", "unknown")
            duration = w.get("state_duration", 0)
            click.echo(f"{name:<20s} {state:<12s} {duration:.1f}s")

    asyncio.run(_check_states())


@main.command()
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
def validate(config_path: str | None) -> None:
    """Validate the swarm.yaml configuration."""
    cfg = load_config(config_path)
    errors = cfg.validate()
    if errors:
        click.echo(f"Found {len(errors)} error(s):", err=True)
        for e in errors:
            click.echo(f"  x {e}", err=True)
        raise SystemExit(1)
    click.echo(f"Config OK: {len(cfg.workers)} workers, {len(cfg.groups)} groups")


@main.command()
@click.argument("target")
@click.argument("message")
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option("--port", default=None, type=int, help="Daemon API port (default: config or 9090)")
def send(target: str, message: str, config_path: str | None, port: int | None) -> None:
    """Send a message to a worker, group, or all.

    TARGET is a worker name, group name, or 'all'.
    MESSAGE is the text to send.
    """
    cfg = _load_config_db_first(config_path)
    api_port = port or cfg.port

    token = _resolve_api_token(cfg)

    async def _send() -> None:
        try:
            data = await _api_get(api_port, "/api/workers", token=token)
        except Exception as e:
            click.echo(f"Cannot reach daemon at localhost:{api_port}: {e}", err=True)
            click.echo("Is the daemon running? Start it with: swarm start", err=True)
            raise SystemExit(1)

        workers = data.get("workers", [])
        if not workers:
            click.echo("No workers registered with the daemon.")
            return

        worker_names = [w.get("name", "") for w in workers]

        # Resolve targets
        if target.lower() == "all":
            targets = worker_names
        else:
            # Try as group name first
            try:
                group_workers = cfg.get_group(target)
                group_names = {w.name.lower() for w in group_workers}
                targets = [n for n in worker_names if n.lower() in group_names]
            except ValueError:
                # Try as worker name
                targets = [n for n in worker_names if n.lower() == target.lower()]

        if not targets:
            click.echo(f"No matching workers for '{target}'")
            return

        for name in targets:
            try:
                await _api_post(
                    api_port,
                    f"/api/workers/{name}/send",
                    {"message": message},
                    token=token,
                )
                click.echo(f"  Sent to {name}")
            except Exception as e:
                click.echo(f"  Failed to send to {name}: {e}", err=True)

        click.echo(f"Message sent to {len(targets)} worker(s)")

    asyncio.run(_send())


@main.command()
@click.option(
    "--timeout",
    default=5.0,
    type=float,
    help="Seconds to wait for graceful shutdown before SIGKILL",
)
@click.option(
    "--force",
    is_flag=True,
    help="Skip SIGTERM and send SIGKILL immediately",
)
def stop(timeout: float, force: bool) -> None:
    """Stop the running swarm daemon.

    Reads the daemon lock file and sends SIGTERM to the holder process,
    waits up to ``--timeout`` seconds for a graceful shutdown, then
    escalates to SIGKILL if the process is still alive.  Use this to
    recover from a wedged or orphaned daemon.
    """
    import signal
    import time as _time

    from swarm.server.runner import _DAEMON_LOCK_PATH, _pid_alive, _read_lock_pid

    pid = _read_lock_pid()
    if pid is None:
        if _DAEMON_LOCK_PATH.exists():
            click.echo("Lock file exists but contains no readable PID; removing.")
            _DAEMON_LOCK_PATH.unlink(missing_ok=True)
        else:
            click.echo("No swarm daemon is running (no lock file).")
        return
    if not _pid_alive(pid):
        click.echo(f"Stale lock for dead PID {pid} — cleaning up.")
        _DAEMON_LOCK_PATH.unlink(missing_ok=True)
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(pid, sig)
    except OSError as e:
        click.echo(f"Failed to signal PID {pid}: {e}", err=True)
        raise SystemExit(1) from e
    if force:
        click.echo(f"Sent SIGKILL to swarm daemon (PID {pid}).")
        return
    # Wait for graceful shutdown, then escalate.
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if not _pid_alive(pid):
            click.echo(f"Stopped swarm daemon (PID {pid}).")
            return
        _time.sleep(0.2)
    click.echo(f"PID {pid} did not exit within {timeout:.0f}s — sending SIGKILL.")
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    _time.sleep(0.3)
    if _pid_alive(pid):
        click.echo(f"Failed to stop PID {pid}.", err=True)
        raise SystemExit(1)
    click.echo(f"Stopped swarm daemon (PID {pid}).")


@main.command("holder-restart")
@click.option(
    "--socket",
    "socket_path",
    default=None,
    help="Override holder socket path (default: ~/.swarm/holder.sock)",
)
@click.option(
    "--timeout",
    default=10.0,
    type=float,
    help="Seconds to wait for the new holder to rebind",
)
def holder_restart_cmd(socket_path: str | None, timeout: float) -> None:
    """Gracefully restart the PTY holder, preserving worker child processes.

    The holder writes its worker registry + ring buffers to a handoff
    file, marks each PTY master FD as inheritable, and ``execv``s into a
    fresh ``swarm.pty.holder`` invocation that resumes serving from the
    handoff. Worker child processes (Claude Code sessions) keep running
    throughout — they own the slave end of the PTY, the kernel keeps it
    open as long as someone holds the master.

    Use this to deploy holder code changes without taking down workers.
    Older holders that don't know the ``restart_in_place`` command will
    return ``unknown command`` and the workers stay where they are.
    """
    import asyncio

    from swarm.pty.holder import DEFAULT_SOCKET_PATH
    from swarm.pty.pool import ProcessPool

    sock = socket_path or str(DEFAULT_SOCKET_PATH)

    async def _run() -> bool:
        pool = ProcessPool(socket_path=sock)
        try:
            await pool.connect()
        except Exception as exc:
            click.echo(f"Could not connect to holder at {sock}: {exc}", err=True)
            return False
        try:
            ok = await pool.restart_holder_in_place(reconnect_timeout=timeout)
            return ok
        finally:
            try:
                await pool.disconnect()
            except Exception:
                # Cleanup phase — never raise. Logged so an operator
                # diagnosing a flaky restart can still see the cause.
                _log.debug("pool disconnect failed during restart", exc_info=True)

    ok = asyncio.run(_run())
    if ok:
        click.echo("Holder restarted in place — workers preserved.")
    else:
        click.echo(
            "Holder restart did not confirm — check 'swarm status' and "
            "the holder PID. If the existing holder predates the "
            "restart_in_place command (released 2026-05), it returns "
            "'unknown command' and worker processes are unaffected; you "
            "would need a one-time disruptive 'kill <holder_pid>' to "
            "deploy the new code.",
            err=True,
        )
        raise SystemExit(1)


@main.command()
@click.argument("worker_name")
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option("--port", default=None, type=int, help="Daemon API port (default: config or 9090)")
def kill(worker_name: str, config_path: str | None, port: int | None) -> None:
    """Kill a worker process."""
    cfg = _load_config_db_first(config_path)
    api_port = port or cfg.port

    async def _kill() -> None:
        try:
            await _api_post(
                api_port,
                f"/api/workers/{worker_name}/kill",
                token=_resolve_api_token(cfg),
            )
            click.echo(f"Killed worker: {worker_name}")
        except Exception as e:
            click.echo(f"Failed to kill worker '{worker_name}': {e}", err=True)
            raise SystemExit(1)

    asyncio.run(_kill())


@main.command()
@click.argument(
    "action",
    type=click.Choice(["list", "create", "assign", "complete"]),
    default="list",
)
@click.option("--title", help="Task title (for create)")
@click.option("--desc", default="", help="Task description (for create)")
@click.option(
    "--priority",
    type=click.Choice(["low", "normal", "high", "urgent"]),
    default="normal",
    help="Task priority (for create)",
)
@click.option("--task-id", help="Task ID (for assign/complete)")
@click.option("--worker", help="Worker name (for assign)")
def tasks(
    action: str,
    title: str | None,
    desc: str,
    priority: str,
    task_id: str | None,
    worker: str | None,
) -> None:
    """Manage the task board.

    Actions: list, create, assign, complete.
    """
    from swarm.db import SqliteTaskStore, SwarmDB
    from swarm.tasks.board import TaskBoard
    from swarm.tasks.task import PRIORITY_MAP

    _db = SwarmDB()
    board = TaskBoard(store=SqliteTaskStore(_db))

    handlers = {
        "list": lambda: _tasks_list(board),
        "create": lambda: _tasks_create(board, title, desc, PRIORITY_MAP[priority], priority),
        "assign": lambda: _tasks_assign(board, task_id, worker),
        "complete": lambda: _tasks_complete(board, task_id),
    }
    handlers[action]()


def _tasks_list(board: TaskBoard) -> None:
    all_tasks = board.all_tasks
    if not all_tasks:
        click.echo(
            "No tasks on the board. (Tasks are session-scoped"
            " -- create from the web dashboard or use 'swarm tasks create')"
        )
        return
    for t in all_tasks:
        assigned = f" -> {t.assigned_worker}" if t.assigned_worker else ""
        click.echo(f"  {t.status.value:12s} [{t.id}] {t.title}{assigned}")
    click.echo(f"\n{board.summary()}")


def _tasks_create(board: TaskBoard, title: str | None, desc: str, prio: int, label: str) -> None:
    if not title:
        click.echo("--title is required for create", err=True)
        raise SystemExit(1)
    task = board.create(title, description=desc, priority=prio)
    click.echo(f"Created task [{task.id}]: {task.title} (priority={label})")


def _tasks_assign(board: TaskBoard, task_id: str | None, worker: str | None) -> None:
    if not task_id or not worker:
        click.echo("--task-id and --worker are required for assign", err=True)
        raise SystemExit(1)
    if board.assign(task_id, worker):
        click.echo(f"Assigned task [{task_id}] -> {worker}")
    else:
        click.echo(f"Task [{task_id}] not found", err=True)
        raise SystemExit(1)


def _tasks_complete(board: TaskBoard, task_id: str | None) -> None:
    if not task_id:
        click.echo("--task-id is required for complete", err=True)
        raise SystemExit(1)
    if board.complete(task_id):
        click.echo(f"Task [{task_id}] marked complete")
    else:
        click.echo(f"Task [{task_id}] not found", err=True)
        raise SystemExit(1)


@main.command()
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(),
    default=None,
    help="Path to swarm config (optional — loads from DB if available)",
)
@click.option("--host", default="localhost", help="Host to bind to")
@click.option(
    "--port", default=None, type=int, help="Port for the API server (default: config or 9090)"
)
def daemon(config_path: str | None, host: str, port: int | None) -> None:
    """Run the swarm as a background daemon with REST + WebSocket API."""
    from swarm.server.daemon import run_daemon

    cfg = _load_config_db_first(config_path)
    port = port or cfg.port

    asyncio.run(run_daemon(cfg, host=host, port=port))


@main.command()
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option(
    "--port", default=None, type=int, help="Local port to tunnel (default: config or 9090)"
)
def tunnel(config_path: str | None, port: int | None) -> None:
    """Start a Cloudflare Tunnel for remote access.

    Prints the public HTTPS URL for accessing the dashboard from a phone.
    Press Ctrl-C to stop.
    """
    import shutil

    if not shutil.which("cloudflared"):
        click.echo(
            "cloudflared is not installed.\n"
            "Install: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/",
            err=True,
        )
        raise SystemExit(1)

    cfg = _load_config_db_first(config_path)
    port = port or cfg.port

    async def _run_tunnel() -> None:
        from swarm.tunnel import TunnelManager

        mgr = TunnelManager(port=port)
        try:
            url = await mgr.start()
        except RuntimeError as e:
            click.echo(f"Error: {e}", err=True)
            raise SystemExit(1)

        click.echo(f"\n  Tunnel active: {url}")
        click.echo(f"  Proxying to:   http://localhost:{port}")
        click.echo("  Press Ctrl-C to stop\n")

        # Wait until interrupted
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await mgr.stop()
            click.echo("Tunnel stopped")

    try:
        asyncio.run(_run_tunnel())
    except KeyboardInterrupt:
        pass


@main.command("install-hooks")
@click.option("--global", "global_install", is_flag=True, help="Install hooks globally")
@click.option("--uninstall", is_flag=True, help="Remove swarm hooks instead of installing")
def install_hooks(global_install: bool, uninstall: bool) -> None:
    """Install or remove auto-approval hooks for Claude Code."""
    if uninstall:
        from swarm.hooks.install import uninstall as do_uninstall

        do_uninstall(global_install=global_install)
        scope = "globally" if global_install else "for this project"
        click.echo(f"Hooks removed {scope}")
    else:
        from swarm.hooks.install import install

        install(global_install=global_install)
        scope = "globally" if global_install else "for this project"
        click.echo(f"Hooks installed {scope}")


@main.command("install-service")
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True),
    help="Path to swarm.yaml",
)
@click.option("--uninstall", is_flag=True, help="Remove the background service")
def install_service_cmd(config_path: str | None, uninstall: bool) -> None:
    """Install (or remove) a background service so swarm starts on login.

    Uses systemd on Linux/WSL and launchd on macOS.
    """
    from swarm.service import is_macos

    if is_macos():
        from swarm.service import install_launchd, launchd_status, uninstall_launchd

        if uninstall:
            removed = uninstall_launchd()
            if removed:
                click.echo("Swarm Launch Agent removed.")
            else:
                click.echo("No plist file found -- nothing to remove.")
            return

        try:
            path = install_launchd(config_path)
        except (RuntimeError, FileNotFoundError) as e:
            click.echo(str(e), err=True)
            raise SystemExit(1)

        click.echo(f"Service installed: {path}")
        click.echo(launchd_status())
        click.echo("\nThe swarm dashboard will now start automatically on login.")
        click.echo("  Status:    launchctl list com.swarm.dashboard")
        click.echo("  Logs:      tail -f ~/.swarm/launchd-stderr.log")
        click.echo("  Uninstall: swarm install-service --uninstall")
    else:
        from swarm.service import install_service, service_status, uninstall_service

        if uninstall:
            removed = uninstall_service()
            if removed:
                click.echo("Swarm service removed.")
            else:
                click.echo("No service file found -- nothing to remove.")
            return

        try:
            path = install_service(config_path)
        except (RuntimeError, FileNotFoundError) as e:
            click.echo(str(e), err=True)
            raise SystemExit(1)

        click.echo(f"Service installed: {path}")
        click.echo(service_status())
        click.echo("\nThe swarm dashboard will now start automatically on login.")
        click.echo("  Status:    systemctl --user status swarm")
        click.echo("  Logs:      journalctl --user -u swarm -f")
        click.echo("  Uninstall: swarm install-service --uninstall")


async def _probe_daemon_sha(port: int, token: str) -> tuple[bool, str]:
    """Return ``(reachable, build_sha)`` for the local daemon on *port*.

    ``reachable`` is ``False`` iff the daemon isn't running or can't be
    contacted within a short timeout.  Never raises.
    """
    import aiohttp

    url = f"http://localhost:{port}/api/health"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_auth_headers(token), timeout=3.0) as resp:
                if resp.status != 200:
                    return False, ""
                data = await resp.json()
                return True, str(data.get("build_sha", ""))
    except (aiohttp.ClientConnectorError, TimeoutError):
        return False, ""
    except Exception:
        return False, ""


async def _wait_for_daemon_sha_change(port: int, token: str, pre_sha: str, timeout: float) -> str:
    """Poll /api/health until build_sha differs from *pre_sha* or *timeout* elapses."""
    import time as _time

    import aiohttp

    url = f"http://localhost:{port}/api/health"
    headers = _auth_headers(token)
    deadline = _time.monotonic() + timeout
    await asyncio.sleep(1.0)  # let the old process exec into the new one
    while _time.monotonic() < deadline:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=2.0) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        new_sha = str(data.get("build_sha", ""))
                        if new_sha and (not pre_sha or new_sha != pre_sha):
                            return f"Daemon restarted with new build ({new_sha})."
                        if not pre_sha:
                            return "Daemon is back up."
        except (aiohttp.ClientError, TimeoutError):
            # Expected during the exec window: the old process drops
            # the listener and the new one isn't accepting yet.  Anything
            # else (bug in the parse path, hostname surprise) should
            # not be silently swallowed — let it propagate so callers
            # see it rather than spin forever.
            pass
        await asyncio.sleep(0.5)
    return (
        f"Restart triggered but daemon did not return new build within {timeout:.0f}s — "
        "check `swarm status` or the dashboard."
    )


async def _restart_running_daemon(port: int, token: str, timeout: float = 30.0) -> str:
    """Detect a running daemon, trigger a restart, and wait for it to come back.

    Mirrors the dashboard "Update & Restart" flow: captures the current
    build_sha, POSTs /api/server/restart, polls /api/health until the sha
    changes (confirming the new process image is live).

    Returns a human-readable status string. Never raises — callers rely on
    the string for user output.
    """
    import aiohttp

    reachable, pre_sha = await _probe_daemon_sha(port, token)
    if not reachable:
        return "No running daemon detected — changes will apply next time swarm starts."

    url = f"http://localhost:{port}/api/server/restart"
    headers = {"X-Requested-With": "swarm-cli", **_auth_headers(token)}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, timeout=5.0) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return f"Restart request failed ({resp.status}): {text[:200]}"
    except TimeoutError:
        # The daemon may have begun shutting down before replying — that's fine.
        pass
    except Exception as exc:
        return f"Restart request failed: {exc}"

    return await _wait_for_daemon_sha_change(port, token, pre_sha, timeout)


@main.command()
@click.option("--check", "check_only", is_flag=True, help="Check only, don't install")
@click.option(
    "--no-restart",
    is_flag=True,
    help="Skip auto-restart of a running daemon after install",
)
def update(check_only: bool, no_restart: bool) -> None:
    """Check for and install updates from GitHub.

    If a swarm daemon is running on the local machine, it is automatically
    restarted after a successful install so the new code takes effect
    immediately — matching the dashboard's "Update & Restart" flow.  Pass
    ``--no-restart`` to skip the restart step.
    """
    from swarm.update import check_for_update, perform_update

    result = asyncio.run(check_for_update(force=True))
    if result.error:
        click.echo(f"Update check failed: {result.error}", err=True)
        raise SystemExit(1)

    click.echo(f"  Installed: {result.current_version}")
    click.echo(f"  Latest:    {result.remote_version}")
    if result.commit_sha:
        click.echo(f"  Commit:    {result.commit_sha} -- {result.commit_message}")

    if not result.available:
        click.echo("\n  Already up to date.")
        return

    click.echo(f"\n  Update available: {result.current_version} -> {result.remote_version}")

    if check_only:
        return

    if not click.confirm("  Install update?", default=True):
        return

    click.echo("  Updating...")
    success, output = asyncio.run(perform_update())
    if not success:
        click.echo(f"  Update failed:\n{output}", err=True)
        raise SystemExit(1)

    click.echo("  Update installed successfully.")

    if no_restart:
        click.echo("  Restart any running swarm processes to use the new version.")
        return

    # Best-effort auto-restart of a running daemon. We load the config only
    # to discover the port and API token — if neither is available the helper
    # will just report "no running daemon detected" and exit cleanly.
    try:
        cfg = _load_config_db_first(None)
    except Exception:
        cfg = None
    port = cfg.port if cfg else 9090
    token = _resolve_api_token(cfg)

    click.echo("  Restarting running daemon...")
    status = asyncio.run(_restart_running_daemon(port, token))
    click.echo(f"  {status}")


# ---------------------------------------------------------------------------
# swarm db — database management commands
# ---------------------------------------------------------------------------


@main.group()
def db() -> None:
    """Database management commands."""


@db.command()
def stats() -> None:
    """Show table row counts and database size."""
    from swarm.db.core import SwarmDB

    sdb = SwarmDB()
    counts = sdb.stats()
    size = sdb.db_size()
    sdb.close()

    click.echo("Tables:")
    for table, count in sorted(counts.items()):
        click.echo(f"  {table:20s} {count:>8,} rows")
    click.echo(f"\nDB size: {size / 1024:.1f} KB")


@db.command()
@click.argument("table", required=False)
@click.option("--limit", default=20, help="Max rows to export")
def export(table: str | None, limit: int) -> None:
    """Export table data as JSON for inspection."""
    import json as _json

    from swarm.db.core import SwarmDB

    sdb = SwarmDB()
    if table:
        tables = [table]
    else:
        tables = list(sdb.stats().keys())

    for tbl in tables:
        try:
            rows = sdb.fetchall(
                f"SELECT * FROM {tbl} ORDER BY rowid DESC LIMIT ?",
                (limit,),
            )
            data = [dict(r) for r in rows]
            click.echo(f"\n--- {tbl} ({len(data)} rows) ---")
            click.echo(_json.dumps(data, indent=2, default=str))
        except Exception as e:
            click.echo(f"  {tbl}: error — {e}", err=True)
    sdb.close()


@db.command()
@click.option("--days", default=30, help="Delete entries older than N days")
def prune(days: int) -> None:
    """Clean old buzz log entries, expired proposals, and read messages."""
    from swarm.db.core import SwarmDB

    sdb = SwarmDB()
    import time

    cutoff = time.time() - (days * 86400)

    buzz = sdb.delete("buzz_log", "timestamp < ?", (cutoff,))
    proposals = sdb.delete("proposals", "status != 'pending' AND resolved_at < ?", (cutoff,))
    messages = sdb.delete("messages", "read_at IS NOT NULL AND created_at < ?", (cutoff,))
    history = sdb.delete("task_history", "created_at < ?", (cutoff,))
    sdb.close()

    click.echo(f"Pruned (older than {days} days):")
    click.echo(f"  buzz_log:     {buzz:>6,} entries")
    click.echo(f"  proposals:    {proposals:>6,} entries")
    click.echo(f"  messages:     {messages:>6,} entries")
    click.echo(f"  task_history: {history:>6,} entries")


@db.command()
def backup() -> None:
    """Create a manual backup of swarm.db."""
    from swarm.db.core import SwarmDB

    sdb = SwarmDB()
    path = sdb.backup()
    sdb.close()
    click.echo(f"Backup created: {path}")


@db.command()
def check() -> None:
    """Run integrity check on swarm.db."""
    from swarm.db.core import SwarmDB

    sdb = SwarmDB()
    ok = sdb.integrity_check()
    sdb.close()
    if ok:
        click.echo("Integrity check: OK")
    else:
        click.echo("Integrity check: FAILED", err=True)
        raise SystemExit(1)
