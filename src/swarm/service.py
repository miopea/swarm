"""Install/uninstall a systemd or launchd service for ``swarm serve``."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from swarm.logging import get_logger
from swarm.paths import state_dir

_log = get_logger("service")

_SERVICE_NAME = "swarm.service"
_SERVICE_DIR = Path.home() / ".config" / "systemd" / "user"
_SERVICE_PATH = _SERVICE_DIR / _SERVICE_NAME

_UNIT_TEMPLATE = """\
[Unit]
Description=Swarm (legacy) Web Dashboard
After=network.target

[Service]
Type=simple
KillMode=process
ExecStart={exec_start}
Restart=always
RestartSec=5
{env_lines}
WorkingDirectory={work_dir}

[Install]
WantedBy=default.target
"""


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``systemctl --user <args>``."""
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
    )


_CONFIG_FLAG_RE = re.compile(
    r"\s+(?:-c\s+\S+|-c\S+|--config(?:=|\s+)\S+)",
)

# The unit Description shipped before this project was renamed "Swarm
# (legacy)".  Matched exactly so a customised Description survives.
_LEGACY_DESCRIPTION = "Description=Swarm Web Dashboard"
_CURRENT_DESCRIPTION = "Description=Swarm (legacy) Web Dashboard"


def ensure_killmode_process() -> bool:
    """Auto-patch the installed systemd unit to match current architecture.

    Two transforms applied (idempotent — safe to run on every daemon
    start):

    1. **``KillMode=process``**.  ``process`` only kills the main PID,
       leaving worker PTY processes alive across daemon restarts — this
       is essential for the sidecar architecture.  An ExecStartPre step
       handles cleaning up stale daemon processes that might be holding
       the listen port.  Pre-existing ``KillMode=mixed`` units (which
       killed workers) are downgraded.

    2. **Strip ``-c`` / ``--config`` from ExecStart**.  Pre-DB-era units
       carried ``ExecStart=swarm serve -c ~/.config/swarm/config.yaml``
       so the daemon could read its workers/groups/etc from YAML.  The
       DB has been the source of truth for months, but the legacy flag
       silently flipped the daemon onto the YAML loader on every
       restart, overwriting dashboard-edited state with whatever stale
       value the YAML happened to have.  Reported by Amanda 2026-05-05.
       Strip it so future starts read from the DB unambiguously.

    3. **Rebrand the unit Description**.  This project became "Swarm
       (legacy)" when Swarm Next superseded it, but ``perform_update()``
       only reinstalls the package -- it never rewrites the unit, so an
       updated install would keep advertising itself as plain "Swarm" in
       ``systemctl`` output forever.  Only the exact pre-rename string is
       replaced, so an operator-customised Description is left alone.
       Regenerating the whole unit would be the obvious alternative and is
       deliberately NOT done: it would clobber a hand-tuned ExecStart or
       WorkingDirectory, which is the failure mode transform 2 exists to
       clean up after.

    Runs ``systemctl --user daemon-reload`` after patching so systemd
    picks up the change immediately.  Returns True if the unit was
    patched.
    """
    if not _SERVICE_PATH.exists():
        return False
    content = _SERVICE_PATH.read_text()
    changed = False
    # Downgrade from KillMode=mixed (kills workers!) back to process
    if "KillMode=mixed" in content:
        content = content.replace("KillMode=mixed", "KillMode=process", 1)
        changed = True
    elif "KillMode=process" not in content:
        content = content.replace("[Service]\n", "[Service]\nKillMode=process\n", 1)
        changed = True
    # Strip legacy ``-c <yaml>`` from any ``ExecStart=`` line.  The DB
    # is canonical now; the flag is moot at best and actively harmful
    # at worst (silent overwrite of dashboard-edited state).
    new_lines: list[str] = []
    for line in content.splitlines(keepends=True):
        if line.startswith("ExecStart=") and _CONFIG_FLAG_RE.search(line):
            new_lines.append(_CONFIG_FLAG_RE.sub("", line))
            changed = True
        else:
            new_lines.append(line)
    content = "".join(new_lines)
    # Rebrand to "Swarm (legacy)".  Exact-match only -- a Description the
    # operator has customised is not ours to rewrite.
    if _LEGACY_DESCRIPTION in content:
        content = content.replace(_LEGACY_DESCRIPTION, _CURRENT_DESCRIPTION, 1)
        changed = True
    if not changed:
        return False
    _SERVICE_PATH.write_text(content)
    _systemctl("daemon-reload")
    return True


def _check_systemd() -> str | None:
    """Return an error message if systemd user services are unavailable, else None."""
    if not shutil.which("systemctl"):
        return (
            "systemctl not found. systemd is required for this feature.\n"
            "On WSL2, add '[boot] systemd=true' to /etc/wsl.conf and restart."
        )
    result = _systemctl("--version")
    if result.returncode != 0:
        return (
            "systemd user services are not available.\n"
            "On WSL2, add '[boot] systemd=true' to /etc/wsl.conf and restart."
        )
    return None


def _resolve_config_path(config_path: str | None) -> Path | None:
    """Find the config file to use."""
    if config_path:
        return Path(config_path).resolve()
    # Check common locations
    cwd_config = Path.cwd() / "swarm.yaml"
    if cwd_config.exists():
        return cwd_config.resolve()
    home_config = Path.home() / ".config" / "swarm" / "config.yaml"
    if home_config.exists():
        return home_config.resolve()
    return None


def _detect_source_dir() -> str | None:
    """Return the swarm source tree path if running from a dev install, else None.

    Checks two scenarios:
    1. ``uv tool install .`` — package metadata has a ``file://`` URL
    2. ``uv run swarm init`` — swarm.__file__ lives inside a source tree
    """
    # 1. Local-path tool install (uv tool install /path/to/swarm)
    try:
        from swarm.update import get_local_source_path

        source = get_local_source_path()
        if source:
            return source
    except Exception:
        _log.debug("_detect_source_dir: get_local_source_path() failed", exc_info=True)

    # 2. Running from dev venv (uv run / editable install)
    try:
        import swarm as _swarm_pkg

        pkg_dir = Path(_swarm_pkg.__file__).resolve().parent
        candidate = pkg_dir
        while candidate != candidate.parent:
            if (candidate / "pyproject.toml").exists() and (candidate / "src" / "swarm").is_dir():
                return str(candidate)
            candidate = candidate.parent
    except Exception:
        _log.debug("_detect_source_dir: source tree detection failed", exc_info=True)

    return None


def _build_path_parts(*binaries: str | None) -> list[str]:
    """Build a deduplicated PATH list from binary locations + standard dirs."""
    parts: list[str] = []
    for b in binaries:
        if b:
            d = str(Path(b).parent)
            if d not in parts:
                parts.append(d)
    local_bin = str(Path.home() / ".local" / "bin")
    for p in [local_bin, "/usr/local/bin", "/usr/bin", "/bin"]:
        if p not in parts:
            parts.append(p)
    return parts


def generate_unit(config_path: str | None = None) -> str:
    """Generate the systemd unit file content.

    For dev installs (local source tree detected), the unit uses
    ``uv run swarm serve`` so source changes take effect on restart.
    For production installs, it uses the installed binary directly.
    """
    swarm_bin = shutil.which("swarm")
    resolved_config = _resolve_config_path(config_path)
    source_dir = _detect_source_dir()

    # ``-c <yaml>`` is intentionally NOT passed to ExecStart.  The DB
    # is the only source of truth for the running daemon (workers,
    # groups, approval rules, workflows, …).  YAML is read once by
    # ``auto_migrate`` to seed a brand-new DB and never touched again.
    # Pre-fix this added ``-c ~/.config/swarm/config.yaml`` to every
    # ExecStart, which silently overrode the DB on every restart and
    # made dashboard-edited state look like it "disappeared" across
    # reboots.  Reported by Amanda 2026-05-05.
    _ = resolved_config  # kept for legacy callers; no longer threaded into ExecStart
    if source_dir:
        # Dev: run via uv so the dev venv is used directly — no SWARM_DEV re-exec
        uv_bin = shutil.which("uv")
        if not uv_bin:
            msg = "uv not found in PATH (required for dev-mode service)"
            raise FileNotFoundError(msg)

        exec_start = uv_bin + " run swarm serve"
        work_dir = source_dir
        path_parts = _build_path_parts(uv_bin, swarm_bin)
        env_lines = f"Environment=PATH={':'.join(path_parts)}\nEnvironment=SWARM_DEV=1"
    else:
        # Production: use the installed binary
        if not swarm_bin:
            msg = "swarm binary not found in PATH. Install with: uv tool install swarm-ai"
            raise FileNotFoundError(msg)

        exec_start = swarm_bin + " serve"
        work_dir = str(Path.home())
        path_parts = _build_path_parts(swarm_bin)
        env_lines = f"Environment=PATH={':'.join(path_parts)}"

    return _UNIT_TEMPLATE.format(
        exec_start=exec_start,
        env_lines=env_lines,
        work_dir=work_dir,
    )


def install_service(config_path: str | None = None) -> Path:
    """Install and start the systemd user service.

    Returns the path to the installed service file.
    """
    error = _check_systemd()
    if error:
        raise RuntimeError(error)

    unit_content = generate_unit(config_path)

    _SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    _SERVICE_PATH.write_text(unit_content)

    _systemctl("daemon-reload")
    _systemctl("enable", _SERVICE_NAME)
    _systemctl("start", _SERVICE_NAME)

    return _SERVICE_PATH


def uninstall_service() -> bool:
    """Stop, disable, and remove the systemd user service.

    Returns True if the service file existed and was removed.
    """
    _systemctl("stop", _SERVICE_NAME)
    _systemctl("disable", _SERVICE_NAME)

    if _SERVICE_PATH.exists():
        _SERVICE_PATH.unlink()
        _systemctl("daemon-reload")
        return True
    return False


def service_status() -> str:
    """Return the systemd status output for the swarm service."""
    result = _systemctl("status", _SERVICE_NAME)
    return result.stdout or result.stderr or "unknown"


# --- macOS launchd (Launch Agent plist) ---

_PLIST_LABEL = "com.swarm.dashboard"
_PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
_PLIST_PATH = _PLIST_DIR / f"{_PLIST_LABEL}.plist"
_SWARM_LOG_DIR = state_dir()

_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{program_arguments}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{stdout_log}</string>
    <key>StandardErrorPath</key>
    <string>{stderr_log}</string>
</dict>
</plist>
"""


def is_macos() -> bool:
    """Return True if running on macOS."""
    import sys

    return sys.platform == "darwin"


def _check_launchd() -> str | None:
    """Return an error message if launchd is unavailable, else None."""
    if not is_macos():
        return "launchd is only available on macOS."
    return None


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``launchctl <args>``."""
    return subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
    )


def generate_plist(config_path: str | None = None) -> str:
    """Generate the launchd plist content."""
    resolved_config = _resolve_config_path(config_path)
    source_dir = _detect_source_dir()

    if source_dir:
        uv_bin = shutil.which("uv")
        if not uv_bin:
            msg = "uv not found in PATH (required for dev-mode service)"
            raise FileNotFoundError(msg)
        args = [uv_bin, "run", "swarm", "serve"]
    else:
        swarm_bin = shutil.which("swarm")
        if not swarm_bin:
            msg = "swarm binary not found in PATH. Install with: uv tool install swarm-ai"
            raise FileNotFoundError(msg)
        args = [swarm_bin, "serve"]

    if resolved_config:
        args.extend(["-c", str(resolved_config)])

    indent = " " * 8
    program_arguments = "\n".join(f"{indent}<string>{arg}</string>" for arg in args)

    _SWARM_LOG_DIR.mkdir(parents=True, exist_ok=True)

    return _PLIST_TEMPLATE.format(
        label=_PLIST_LABEL,
        program_arguments=program_arguments,
        stdout_log=str(_SWARM_LOG_DIR / "launchd-stdout.log"),
        stderr_log=str(_SWARM_LOG_DIR / "launchd-stderr.log"),
    )


def install_launchd(config_path: str | None = None) -> Path:
    """Install and load the launchd Launch Agent.

    Returns the path to the installed plist file.
    """
    error = _check_launchd()
    if error:
        raise RuntimeError(error)

    plist_content = generate_plist(config_path)

    _PLIST_DIR.mkdir(parents=True, exist_ok=True)
    _PLIST_PATH.write_text(plist_content)

    _launchctl("load", str(_PLIST_PATH))

    return _PLIST_PATH


def uninstall_launchd() -> bool:
    """Unload and remove the launchd Launch Agent.

    Returns True if the plist file existed and was removed.
    """
    if _PLIST_PATH.exists():
        _launchctl("unload", str(_PLIST_PATH))
        _PLIST_PATH.unlink()
        return True
    return False


def launchd_status() -> str:
    """Return the launchd status for the swarm service."""
    result = _launchctl("list", _PLIST_LABEL)
    return result.stdout or result.stderr or "unknown"


# --- WSL auto-start (Windows Startup folder) ---

_VBS_CONTENT = 'CreateObject("WScript.Shell").Run "wsl -d {distro} --exec /bin/true", 0, False\n'
_VBS_NAME = "start-wsl.vbs"


def _wsl_startup_dir() -> Path | None:
    """Return the Windows Startup folder path via /mnt/c, or None if unavailable."""
    import os
    import subprocess

    try:
        # Prefer wslvar for the real Windows username
        result = subprocess.run(
            ["wslvar", "USERPROFILE"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            # wslvar returns a Windows path — translate to WSL
            win_profile = result.stdout.strip()
            wsl_result = subprocess.run(
                ["wslpath", "-u", win_profile],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if wsl_result.returncode == 0 and wsl_result.stdout.strip():
                candidate = (
                    Path(wsl_result.stdout.strip())
                    / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"
                )
                if candidate.is_dir():
                    return candidate
    except (OSError, FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: use Linux USER (may differ from Windows username)
    try:
        win_user = os.environ.get("USER") or os.environ.get("LOGNAME", "")
        candidate = Path(
            f"/mnt/c/Users/{win_user}/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"
        )
        if candidate.is_dir():
            return candidate
    except OSError:
        pass
    return None


def _wsl_distro_name() -> str:
    """Return the current WSL distro name."""
    import os

    return os.environ.get("WSL_DISTRO_NAME", "Ubuntu")


def is_wsl() -> bool:
    """Return True if running inside WSL."""
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


_WSL_CONF = Path("/etc/wsl.conf")


def _wsl_systemd_enabled() -> bool:
    """Return True if systemd is already enabled in /etc/wsl.conf."""
    try:
        text = _WSL_CONF.read_text()
    except OSError:
        return False
    import configparser

    cp = configparser.ConfigParser()
    cp.read_string(text)
    return cp.getboolean("boot", "systemd", fallback=False)


def enable_wsl_systemd() -> bool:
    """Enable systemd in /etc/wsl.conf (requires sudo).

    Reads the existing config (or starts empty), ensures ``[boot] systemd=true``
    is present, and writes back via ``sudo tee``.

    Returns True on success.
    """
    if _wsl_systemd_enabled():
        return True

    try:
        text = _WSL_CONF.read_text()
    except OSError:
        text = ""

    import configparser

    cp = configparser.ConfigParser()
    cp.read_string(text)
    if not cp.has_section("boot"):
        cp.add_section("boot")
    cp.set("boot", "systemd", "true")

    import io

    buf = io.StringIO()
    cp.write(buf)
    new_content = buf.getvalue()

    result = subprocess.run(
        ["sudo", "tee", str(_WSL_CONF)],
        input=new_content,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        msg = f"Failed to write {_WSL_CONF}: {result.stderr.strip()}"
        raise RuntimeError(msg)
    return True


def install_wsl_startup() -> Path | None:
    """Install a VBS script in the Windows Startup folder to auto-launch WSL.

    Returns the path to the VBS file, or None if not applicable.
    """
    if not is_wsl():
        return None
    startup = _wsl_startup_dir()
    if not startup:
        return None
    vbs_path = startup / _VBS_NAME
    distro = _wsl_distro_name()
    vbs_path.write_text(_VBS_CONTENT.format(distro=distro))
    return vbs_path


def uninstall_wsl_startup() -> bool:
    """Remove the WSL auto-start VBS script. Returns True if it existed."""
    startup = _wsl_startup_dir()
    if not startup:
        return False
    vbs_path = startup / _VBS_NAME
    if vbs_path.exists():
        vbs_path.unlink()
        return True
    return False


def wsl_startup_installed() -> bool:
    """Check if the WSL auto-start VBS script is already installed."""
    startup = _wsl_startup_dir()
    if not startup:
        return False
    return (startup / _VBS_NAME).exists()
