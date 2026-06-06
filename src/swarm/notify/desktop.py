"""Desktop notification backend — WSL toast, Linux notify-send, macOS osascript."""

from __future__ import annotations

import platform
import shutil
import subprocess
import threading
from pathlib import Path

from swarm.logging import get_logger
from swarm.notify.bus import NotifyEvent, Severity

_log = get_logger("notify.desktop")

_TITLE_MAX_LEN = 80
_MESSAGE_MAX_LEN = 200

# Cached icon paths (resolved once, reused)
_icon_path: Path | None = None
_win_icon_path: str | None = None


def _get_icon_path() -> Path | None:
    """Resolve the icon PNG path from the installed package."""
    global _icon_path
    if _icon_path is not None:
        return _icon_path
    candidate = Path(__file__).resolve().parent.parent / "web" / "static" / "icon-192.png"
    if candidate.exists():
        _icon_path = candidate
    return _icon_path


def _get_win_icon_path() -> str | None:
    """Convert the icon path to a Windows path via wslpath (cached)."""
    global _win_icon_path
    if _win_icon_path is not None:
        return _win_icon_path
    icon = _get_icon_path()
    if not icon:
        return None
    try:
        result = subprocess.run(
            ["wslpath", "-w", str(icon)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            _win_icon_path = result.stdout.strip()
    except Exception:
        _log.debug("wslpath conversion failed", exc_info=True)
    return _win_icon_path


def _is_wsl() -> bool:
    from swarm.service import is_wsl

    return is_wsl()


def _ps_escape(s: str) -> str:
    """Escape a string for embedding in a PowerShell single-quoted literal."""
    return s.replace("'", "''")


def _send_wsl_toast(title: str, message: str) -> None:
    """Send a Windows toast notification from WSL via powershell.exe."""
    ps = shutil.which("powershell.exe")
    if not ps:
        return
    safe_title = _ps_escape(title)
    safe_message = _ps_escape(message)
    win_icon = _get_win_icon_path()
    # Build ToastGeneric XML — includes appLogoOverride image when icon available
    image_node = ""
    if win_icon:
        safe_icon = _ps_escape(win_icon)
        image_node = f"<image placement=\"appLogoOverride\" src='{safe_icon}' hint-crop='circle'/>"
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] > $null; "
        f"[xml]$xml = '<toast><visual><binding template=''ToastGeneric''>"
        f"{image_node}"
        f"<text>{safe_title}</text>"
        f"<text>{safe_message}</text>"
        f"</binding></visual></toast>'; "
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Swarm').Show($toast)"
    )
    try:
        proc = subprocess.Popen(
            [ps, "-NoProfile", "-NonInteractive", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Reap after timeout to prevent zombie processes

        threading.Thread(
            target=lambda: proc.wait(timeout=10), daemon=True, name="toast-reaper"
        ).start()
    except Exception:
        _log.debug("WSL toast failed", exc_info=True)


def _send_notify_send(title: str, message: str, urgency: str = "normal") -> None:
    """Send via notify-send (Linux desktop)."""
    ns = shutil.which("notify-send")
    if not ns:
        return
    cmd = [ns, f"--urgency={urgency}", "--app-name=Swarm"]
    icon = _get_icon_path()
    if icon:
        cmd.append(f"--icon={icon}")
    cmd.extend([title, message])
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        threading.Thread(
            target=lambda: proc.wait(timeout=10), daemon=True, name="notify-reaper"
        ).start()
    except Exception:
        _log.debug("notify-send failed", exc_info=True)


def _send_macos_notification(title: str, message: str) -> None:
    """Send a macOS notification via osascript."""
    osascript = shutil.which("osascript")
    if not osascript:
        return
    # Escape double quotes for AppleScript
    safe_title = title.replace('"', '\\"')
    safe_message = message.replace('"', '\\"')
    script = f'display notification "{safe_message}" with title "{safe_title}"'
    try:
        proc = subprocess.Popen(
            [osascript, "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        threading.Thread(
            target=lambda: proc.wait(timeout=10), daemon=True, name="osascript-reaper"
        ).start()
    except Exception:
        _log.debug("macOS notification failed", exc_info=True)


def desktop_backend(event: NotifyEvent) -> None:
    """Send a desktop notification appropriate for the current platform."""
    # Only send desktop notifications for warning/urgent events
    if event.severity == Severity.INFO:
        return

    title = event.title[:_TITLE_MAX_LEN]
    message = event.message[:_MESSAGE_MAX_LEN]
    urgency = "critical" if event.severity == Severity.URGENT else "normal"

    if _is_wsl():
        _send_wsl_toast(title, message)
    elif platform.system() == "Darwin":
        _send_macos_notification(title, message)
    elif platform.system() == "Linux":
        _send_notify_send(title, message, urgency)
    else:
        _log.debug("no notification backend available for platform %s", platform.system())
