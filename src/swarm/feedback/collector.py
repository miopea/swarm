"""Collect diagnostic attachments for a feedback report.

Pulls version info, recent log lines, drone events, and a redacted copy
of swarm.yaml. Each attachment is returned as an independent section so
the UI can toggle them on/off and let the user edit each one before
submission.
"""

from __future__ import annotations

import collections
import dataclasses
import json
import platform
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import swarm
from swarm.feedback.redact import redact_config_dict, redact_text

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

_DEFAULT_LOG_PATH = Path("~/.swarm/swarm.log").expanduser()
_DEFAULT_LOG_LINES = 200
_DEFAULT_DRONE_EVENTS = 50

# A config value that's a bare ``$VAR_NAME`` reference to an environment
# variable (e.g. ``client_secret: $JIRA_SECRET``). Their live values are
# scrubbed from logs/events via redact_text's env_refs.
_ENV_REF_RE = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)$")


def _config_env_refs(daemon: SwarmDaemon | None) -> list[str]:
    """Env-var NAMES the config references via ``$VAR``.

    Passed to :func:`redact_text` so the *live values* of those vars get
    scrubbed out of collected logs/drone-events (the config itself only stores
    the ``$VAR`` reference, but a resolved value could surface in a log line).
    Best-effort — returns ``[]`` on any error so it can't break the report.
    """
    if daemon is None or getattr(daemon, "config", None) is None:
        return []
    try:
        raw = dataclasses.asdict(daemon.config)
    except TypeError:
        return []
    refs: set[str] = set()

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for x in node:
                _walk(x)
        elif isinstance(node, str):
            m = _ENV_REF_RE.match(node.strip())
            if m:
                refs.add(m.group(1))

    _walk(raw)
    return sorted(refs)


@dataclass
class Attachment:
    """A single piece of diagnostic context attached to a feedback report."""

    key: str  # stable identifier (e.g. "environment", "logs", "drone_events")
    label: str  # human-readable section title
    content: str  # redacted body (markdown / plain text)
    redacted_count: int = 0  # items scrubbed during redaction


def _tail_file(path: Path, lines: int) -> str:
    """Return the last *lines* lines of *path*, or empty string on any error."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            buf: collections.deque[str] = collections.deque(f, maxlen=lines)
    except (OSError, ValueError):
        return ""
    return "".join(buf)


def _collect_environment() -> Attachment:
    body = "\n".join(
        [
            f"- **Swarm**: {swarm.__version__}",
            f"- **Python**: {sys.version.split()[0]}",
            f"- **Platform**: {platform.platform()}",
            f"- **Machine**: {platform.machine()}",
        ]
    )
    return Attachment(key="environment", label="Environment", content=body)


def _collect_install_id() -> Attachment:
    from swarm.feedback.install_id import get_install_id

    return Attachment(
        key="install_id",
        label="Install ID",
        content=get_install_id(),
    )


def _collect_logs(
    log_path: Path | None, lines: int, env_refs: list[str] | None = None
) -> Attachment:
    path = log_path or _DEFAULT_LOG_PATH
    raw = _tail_file(path, lines)
    if not raw:
        return Attachment(
            key="logs",
            label=f"Recent logs ({path})",
            content="(no log file found or empty)",
        )
    redacted, count = redact_text(raw, env_refs=env_refs)
    return Attachment(
        key="logs",
        label=f"Recent logs (last {lines} lines, from {path.name})",
        content=redacted,
        redacted_count=count,
    )


def _collect_drone_events(
    daemon: SwarmDaemon | None, limit: int, env_refs: list[str] | None = None
) -> Attachment:
    if daemon is None or not hasattr(daemon, "drone_log"):
        return Attachment(
            key="drone_events",
            label="Recent drone events",
            content="(no drone log available)",
        )
    try:
        entries = list(daemon.drone_log.entries)[-limit:]
    except Exception:  # defensive — never break the report flow
        return Attachment(
            key="drone_events",
            label="Recent drone events",
            content="(drone log unavailable)",
        )
    if not entries:
        return Attachment(
            key="drone_events",
            label="Recent drone events",
            content="(no recent events)",
        )
    lines = [entry.display for entry in entries]
    raw = "\n".join(lines)
    redacted, count = redact_text(raw, env_refs=env_refs)
    return Attachment(
        key="drone_events",
        label=f"Recent drone events (last {len(entries)})",
        content=redacted,
        redacted_count=count,
    )


def _collect_config(daemon: SwarmDaemon | None, env_refs: list[str] | None = None) -> Attachment:
    """Serialize the live in-memory HiveConfig, with secrets blanked.

    Config is loaded from ``swarm.db`` at startup and held on the daemon,
    so we serialize the dataclass directly rather than re-reading any
    file on disk. This works regardless of whether the user ever had a
    ``swarm.yaml``.
    """
    label = "Configuration (redacted)"
    if daemon is None:
        return Attachment(
            key="config",
            label=label,
            content="(no daemon available)",
        )

    try:
        raw_dict = dataclasses.asdict(daemon.config)
    except TypeError:
        return Attachment(
            key="config",
            label=label,
            content="(config is not serializable)",
        )

    # Drop the source_path field — it's an implementation detail and may
    # leak a filesystem path that's not useful for debugging.
    if isinstance(raw_dict, dict):
        raw_dict.pop("source_path", None)

    scrubbed, key_count = redact_config_dict(raw_dict)
    try:
        serialized = json.dumps(scrubbed, indent=2, default=str, sort_keys=True)
    except (TypeError, ValueError):
        serialized = str(scrubbed)

    # Second pass: regex scrub to catch any remaining secret-shaped values.
    final, regex_count = redact_text(serialized, env_refs=env_refs)
    return Attachment(
        key="config",
        label=label,
        content=final,
        redacted_count=key_count + regex_count,
    )


def collect_attachments(
    daemon: SwarmDaemon | None = None,
    *,
    log_path: Path | None = None,
    log_lines: int = _DEFAULT_LOG_LINES,
    drone_event_limit: int = _DEFAULT_DRONE_EVENTS,
) -> list[Attachment]:
    """Collect all default attachments, in the order they appear in the UI.

    The caller (API route) chooses which ones to include based on the
    user's selected category (Bug / Feature / Question).
    """
    env_refs = _config_env_refs(daemon)
    return [
        _collect_environment(),
        _collect_install_id(),
        _collect_logs(log_path, log_lines, env_refs),
        _collect_drone_events(daemon, drone_event_limit, env_refs),
        _collect_config(daemon, env_refs),
    ]
