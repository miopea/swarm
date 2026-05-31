"""Install Claude Code permissions and hooks for Swarm workers."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from swarm.logging import get_logger
from swarm.providers import get_provider

_log = get_logger("hooks.install")


def _swarm_read_permissions() -> list[str]:
    """Read permissions for swarm-managed shared paths.

    Workers run inside their project worktree by default — Claude Code's
    permission gate refuses absolute Read calls outside that root, so task
    messages that reference ``~/.swarm/uploads/<file>`` (Jira attachments,
    pasted images, email imports) silently fail unless we whitelist the
    directory up front. The Claude Code grammar is ``Read(//abs/path/**)``:
    a literal ``//`` prefix attached to an absolute path **with the leading
    slash stripped** (compare the existing user setting
    ``Read(//home/bschleifer/projects/rcg/**)``). Three slashes ends up matching
    nothing.
    """
    home = str(Path.home()).lstrip("/")
    return [
        f"Read(//{home}/.swarm/uploads/**)",
        f"Read(//{home}/.swarm/cross-tasks/**)",
    ]


PERMISSIONS_CONFIG = {
    "permissions": {
        "allow": ["Edit", "Write", "WebFetch", "WebSearch", *_swarm_read_permissions()],
    }
}

# Claude Code prints ``1.2.3 (Claude Code)`` on --version. Keep tolerant
# so pre-release suffixes like "2.0.0-beta.3" still parse.
_CC_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def install(global_install: bool = False, sandbox: object | None = None) -> None:
    """Install permissions and hooks into Claude Code settings.

    Only installs for the Claude provider — other providers do not
    support the same settings mechanism.

    When ``sandbox.enabled`` is True and the installed Claude Code
    version is at least ``sandbox.min_claude_version``, also merges
    ``sandbox.settings_overrides`` into ``settings["sandbox"]``. This
    opts the worker into CC's native sandbox (see Anthropic's "Beyond
    permission prompts" post) and lets drones stop fielding most of
    the routine approval traffic the sandbox now handles itself.
    """
    provider = get_provider()
    if not provider.supports_hooks:
        return

    if global_install:
        settings_path = Path.home() / ".claude" / "settings.json"
    else:
        settings_path = Path.cwd() / ".claude" / "settings.json"

    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing settings
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            # Back up corrupt file and start fresh
            bak = settings_path.with_suffix(".json.bak")
            settings_path.rename(bak)
            settings = {}
    else:
        settings = {}

    # Merge permissions — add ours without duplicating
    existing_allow = settings.get("permissions", {}).get("allow", [])
    for perm in PERMISSIONS_CONFIG["permissions"]["allow"]:
        if perm not in existing_allow:
            existing_allow.append(perm)

    settings.setdefault("permissions", {})["allow"] = existing_allow

    # Remove broken legacy PreToolUse auto-allow hook if present
    _remove_legacy_hook(settings)

    # Remove legacy PostToolUse hooks (replaced by MCP tools)
    _remove_legacy_post_tool_hooks(settings)

    # Install PreToolUse approval hook (replaces PTY-injection approvals)
    _install_approval_hook(settings)

    # Install SessionEnd hook (immediate STUNG detection)
    _install_session_end_hook(settings)

    # Install SessionStart hook (worker bootstrap: assigned task + unread messages)
    _install_session_start_hook(settings)

    # Install lifecycle event hooks (SubagentStart/Stop, PreCompact/PostCompact, etc.)
    _install_event_hooks(settings)

    # Register Swarm MCP server for worker↔daemon communication
    _install_mcp_server(settings)

    # Apply CC native sandbox settings when opted in and the installed
    # CC version is new enough to support them.
    _apply_sandbox(settings, sandbox)

    settings_path.write_text(json.dumps(settings, indent=2) + "\n")


def _apply_sandbox(settings: dict[str, Any], sandbox_cfg: object | None) -> None:
    """Merge sandbox overrides into ``settings["sandbox"]`` when enabled.

    Gated on three conditions, any of which skip the write (logged):
    - ``sandbox_cfg`` is provided and ``enabled`` is True.
    - ``sandbox_cfg.settings_overrides`` is non-empty (otherwise there's
      nothing to write).
    - The installed Claude Code version meets ``min_claude_version``.
      When ``min_claude_version`` is empty, the check is skipped.
    """
    if sandbox_cfg is None:
        return
    enabled = bool(getattr(sandbox_cfg, "enabled", False))
    if not enabled:
        return
    overrides = getattr(sandbox_cfg, "settings_overrides", None) or {}
    if not overrides:
        _log.info("sandbox enabled but no settings_overrides provided; skipping")
        return
    min_version = str(getattr(sandbox_cfg, "min_claude_version", "") or "")
    if min_version and not _claude_version_at_least(min_version):
        _log.warning(
            "sandbox requested but Claude Code version is below %s; staying on legacy flow",
            min_version,
        )
        return
    existing = settings.get("sandbox") or {}
    if isinstance(existing, dict):
        existing.update(overrides)
        settings["sandbox"] = existing
    else:
        settings["sandbox"] = dict(overrides)


def _claude_version_at_least(required: str) -> bool:
    """Return True when ``claude --version`` reports a version >= ``required``.

    Returns False on any subprocess error — callers interpret that as
    "can't confirm support, stay on the legacy path". Version comparison
    is purely numeric: pre-release suffixes (``2.0.0-rc.1``) are
    compared as 2.0.0.
    """
    required_tuple = _parse_version(required)
    if required_tuple is None:
        return False
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        _log.debug("claude --version not available; skipping sandbox gate")
        return False
    if result.returncode != 0:
        _log.debug("claude --version exited %d; skipping sandbox gate", result.returncode)
        return False
    installed = _parse_version(result.stdout)
    if installed is None:
        return False
    return installed >= required_tuple


def _parse_version(text: str) -> tuple[int, int, int] | None:
    """Parse the first dotted version triple found in *text*.

    ``1.2`` → ``(1, 2, 0)``; ``2.0.3-rc.1`` → ``(2, 0, 3)``; no match → None.
    """
    m = _CC_VERSION_RE.search(text)
    if not m:
        return None
    major = int(m.group(1))
    minor = int(m.group(2))
    patch = int(m.group(3) or 0)
    return (major, minor, patch)


def _remove_legacy_hook(settings: dict[str, Any]) -> None:
    """Remove the old broken PreToolUse auto-allow hook that used the wrong JSON schema."""
    hooks = settings.get("hooks", {})
    pre_tool = hooks.get("PreToolUse", [])
    if not pre_tool:
        return

    legacy_matcher = "Read|Edit|Write|Glob|Grep|WebSearch|WebFetch"
    pre_tool[:] = [m for m in pre_tool if m.get("matcher") != legacy_matcher]

    if not pre_tool:
        del hooks["PreToolUse"]
    if not hooks:
        settings.pop("hooks", None)


_APPROVAL_HOOK_SRC = Path(__file__).parent / "approval_hook.sh"
_APPROVAL_HOOK_DST = Path.home() / ".swarm" / "hooks" / "approval-hook.sh"

_SESSION_END_HOOK_SRC = Path(__file__).parent / "session_end_hook.sh"
_SESSION_END_HOOK_DST = Path.home() / ".swarm" / "hooks" / "session-end-hook.sh"

_SESSION_START_HOOK_SRC = Path(__file__).parent / "session_start_hook.sh"
_SESSION_START_HOOK_DST = Path.home() / ".swarm" / "hooks" / "session-start-hook.sh"

_EVENT_HOOK_SRC = Path(__file__).parent / "event_hook.sh"
_EVENT_HOOK_DST = Path.home() / ".swarm" / "hooks" / "event-hook.sh"

_COMMANDS_SRC_DIR = Path(__file__).parent / "commands"
_SKILLS_SRC_DIR = Path(__file__).parent / "skills"

# Slash command files we install per-worker.  Listed explicitly so a stray
# file in the source dir cannot quietly become a worker-visible command.
WORKER_COMMAND_FILES = (
    "swarm-status.md",
    "swarm-handoff.md",
    "swarm-finding.md",
    "swarm-warning.md",
    "swarm-blocker.md",
    "swarm-progress.md",
)

# Skills we install per-worker.  Each entry names a directory under
# ``_SKILLS_SRC_DIR`` containing a ``SKILL.md`` (and any helper files).
WORKER_SKILL_NAMES = (
    "swarm-checkpoint",
    "swarm-coordinate",
)

# Legacy hook destinations (for cleanup only)
_CROSS_TASK_HOOK_DST = Path.home() / ".swarm" / "hooks" / "cross-task-hook.sh"
_COMPLETE_TASK_HOOK_DST = Path.home() / ".swarm" / "hooks" / "complete-task-hook.sh"


def _remove_legacy_post_tool_hooks(settings: dict[str, Any]) -> None:
    """Remove cross-task and complete-task PostToolUse hooks.

    These are replaced by MCP tools (swarm_create_task, swarm_complete_task).
    """
    hooks = settings.get("hooks", {})
    post_tool = hooks.get("PostToolUse", [])
    if not post_tool:
        return

    post_tool[:] = [
        m
        for m in post_tool
        if not any(
            h.get("command", "").endswith(("cross-task-hook.sh", "complete-task-hook.sh"))
            for h in m.get("hooks", [])
        )
    ]

    if not post_tool:
        hooks.pop("PostToolUse", None)
    if not hooks:
        settings.pop("hooks", None)


def _install_hook_script(
    src: Path,
    dst: Path,
    settings: dict[str, Any],
    event_name: str,
    matcher: str | None,
    script_suffix: str,
    timeout: int = 5000,
) -> None:
    """Generic helper to copy a hook script and register it in settings."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.copy2(src, dst)
        dst.chmod(0o755)

    hooks = settings.setdefault("hooks", {})
    event_hooks = hooks.setdefault(event_name, [])

    hook_command = str(dst)
    already_installed = any(
        m.get("matcher") == matcher
        and any(h.get("command", "").endswith(script_suffix) for h in m.get("hooks", []))
        for m in event_hooks
    )
    if not already_installed:
        entry: dict = {
            "hooks": [{"type": "command", "command": hook_command, "timeout": timeout}],
        }
        if matcher is not None:
            entry["matcher"] = matcher
        event_hooks.append(entry)


def _install_approval_hook(settings: dict[str, Any]) -> None:
    """Register PreToolUse hook for drone-based tool approval."""
    _install_hook_script(
        _APPROVAL_HOOK_SRC,
        _APPROVAL_HOOK_DST,
        settings,
        event_name="PreToolUse",
        matcher=None,
        script_suffix="approval-hook.sh",
    )


def _install_session_end_hook(settings: dict[str, Any]) -> None:
    """Register SessionEnd hook for immediate STUNG detection."""
    _install_hook_script(
        _SESSION_END_HOOK_SRC,
        _SESSION_END_HOOK_DST,
        settings,
        event_name="SessionEnd",
        matcher=None,
        script_suffix="session-end-hook.sh",
        timeout=3000,
    )


def _install_session_start_hook(settings: dict[str, Any]) -> None:
    """Register SessionStart hook for worker bootstrap (task + unread messages)."""
    _install_hook_script(
        _SESSION_START_HOOK_SRC,
        _SESSION_START_HOOK_DST,
        settings,
        event_name="SessionStart",
        matcher=None,
        script_suffix="session-start-hook.sh",
        timeout=3000,
    )


def _install_event_hooks(settings: dict[str, Any]) -> None:
    """Register lifecycle event hooks (SubagentStart/Stop, PreCompact/PostCompact)."""
    events = ["SubagentStart", "SubagentStop", "PreCompact", "PostCompact"]
    for event_name in events:
        _install_hook_script(
            _EVENT_HOOK_SRC,
            _EVENT_HOOK_DST,
            settings,
            event_name=event_name,
            matcher=None,
            script_suffix="event-hook.sh",
            timeout=3000,
        )


def install_worker_commands(worker_path: Path) -> int:
    """Install Swarm slash commands into a worker workdir's ``.claude/commands/``.

    Copies the bundled command markdown files (``WORKER_COMMAND_FILES``) into
    ``<worker_path>/.claude/commands/`` so the worker's Claude Code session
    surfaces ``/swarm-*`` commands in ``/help``.  Idempotent: re-running
    overwrites existing files with the bundled version, so command updates
    propagate on every daemon start.

    Returns the number of files written.  Logs (does not raise) on per-file
    write failures so a single bad workdir doesn't block other workers.
    """
    commands_dir = worker_path / ".claude" / "commands"
    try:
        commands_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        _log.warning("failed to create %s — skipping commands install", commands_dir)
        return 0

    written = 0
    for fname in WORKER_COMMAND_FILES:
        src_path = _COMMANDS_SRC_DIR / fname
        if not src_path.exists():
            _log.warning("command source missing: %s", src_path)
            continue
        dst_path = commands_dir / fname
        try:
            shutil.copy2(src_path, dst_path)
            written += 1
        except OSError as e:
            _log.warning("failed to install %s: %s", dst_path, e)
    return written


def install_worker_skills(worker_path: Path) -> int:
    """Install Swarm skills into a worker workdir's ``.claude/skills/``.

    Each skill in ``WORKER_SKILL_NAMES`` is a directory under
    ``_SKILLS_SRC_DIR`` containing at minimum a ``SKILL.md``.  The whole
    directory is copied (so future skills with helper scripts also ship
    intact).  Idempotent: each run replaces the destination with the
    bundled source so SKILL.md updates propagate on every daemon start.

    Returns the number of skills installed.  Logs (does not raise) on
    per-skill failures so a single bad workdir does not block other
    workers.
    """
    skills_dir = worker_path / ".claude" / "skills"
    try:
        skills_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        _log.warning("failed to create %s — skipping skills install", skills_dir)
        return 0

    written = 0
    for name in WORKER_SKILL_NAMES:
        src_dir = _SKILLS_SRC_DIR / name
        if not src_dir.is_dir():
            _log.warning("skill source missing: %s", src_dir)
            continue
        dst_dir = skills_dir / name
        try:
            # Replace the destination directory atomically-ish: remove
            # the old version (if any) so deletions in the bundled
            # source propagate, then copy fresh.
            if dst_dir.exists():
                shutil.rmtree(dst_dir)
            shutil.copytree(src_dir, dst_dir)
            written += 1
        except OSError as e:
            _log.warning("failed to install skill %s: %s", name, e)
    return written


def _install_mcp_server(settings: dict[str, Any]) -> None:
    """Register Swarm as an MCP server via project-level .mcp.json.

    Project-level .mcp.json files are visible in Claude Code's /mcp dialog,
    whereas mcpServers in global settings.json are not.  We also clean up
    any legacy mcpServers entry from the global settings dict.

    MCP connections are always local (Claude Code CLI runs on the same
    machine as the daemon), so the URL is always ``http://localhost:<port>``.
    The HTTPS domain is only for the browser dashboard.

    If the existing .mcp.json already has a ``?worker=`` query param (written
    by the daemon's ``_write_worker_mcp_configs``), preserve it so that MCP
    calls carry the correct worker identity.
    """
    # Remove legacy global entry (was invisible in /mcp dialog)
    settings.pop("mcpServers", None)

    url = _resolve_mcp_url()

    # Preserve per-worker ?worker= param if the daemon already wrote one
    mcp_path = Path.cwd() / ".mcp.json"
    if mcp_path.exists():
        try:
            existing = json.loads(mcp_path.read_text())
            existing_url = existing.get("mcpServers", {}).get("swarm", {}).get("url", "")
            if "?worker=" in existing_url:
                # Keep the worker identity — only update the base URL (port may change)
                worker_param = existing_url.split("?", 1)[1]
                url = f"{url}?{worker_param}"
        except (json.JSONDecodeError, Exception):
            pass

    mcp_config = {
        "mcpServers": {
            "swarm": {
                "type": "http",
                "url": url,
            }
        }
    }
    mcp_path.write_text(json.dumps(mcp_config, indent=2) + "\n")


def _resolve_mcp_url() -> str:
    """Determine the MCP SSE URL from the swarm config file.

    Always uses localhost — MCP is a local connection between Claude Code
    CLI and the daemon on the same machine.  Only the port is configurable.
    """
    config_path = Path.home() / ".config" / "swarm" / "config.yaml"
    if config_path.exists():
        try:
            import yaml

            data = yaml.safe_load(config_path.read_text()) or {}
            port = data.get("port", 9090)
            return f"http://localhost:{port}/mcp"
        except Exception:
            pass
    return "http://localhost:9090/mcp"


def uninstall(global_install: bool = False) -> None:
    """Remove swarm-installed permissions from Claude Code settings."""
    if global_install:
        settings_path = Path.home() / ".claude" / "settings.json"
    else:
        settings_path = Path.cwd() / ".claude" / "settings.json"

    if not settings_path.exists():
        return

    try:
        settings = json.loads(settings_path.read_text())
    except json.JSONDecodeError:
        return

    existing_allow = settings.get("permissions", {}).get("allow", [])
    swarm_perms = set(PERMISSIONS_CONFIG["permissions"]["allow"])

    before = len(existing_allow)
    existing_allow[:] = [p for p in existing_allow if p not in swarm_perms]

    if len(existing_allow) < before:
        if existing_allow:
            settings["permissions"]["allow"] = existing_allow
        else:
            settings.get("permissions", {}).pop("allow", None)
            if not settings.get("permissions"):
                settings.pop("permissions", None)
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
