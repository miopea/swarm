"""Move Swarm (legacy) off the ``swarm`` name so the name can be reused.

This is the destructive half of the legacy handoff.  It moves the state
directory, renames the systemd unit, and drops the ``swarm`` entrypoint —
after which the hive answers to ``swarm-legacy`` and keeps working
exactly as before.  Nothing about a hive's *contents* changes; this moves
where they live, not what they are.

Why it cannot be done live
--------------------------

The pty-holder is a sidecar that owns every worker's terminal.  It binds
``<state>/holder.sock`` and writes ``<state>/pty-writes.jsonl``; the
daemon holds ``<state>/daemon.lock`` and an open handle on
``<state>/swarm.db``.  A Unix socket's path is captured at ``bind()`` —
moving the directory underneath a live holder leaves it serving a socket
no client can find, while the daemon keeps writing to the *old* inode
through its open descriptor.  The result is a hive that looks up but
whose two halves are talking about different directories.

So the sidecar goes offline and the workers with it.  That is inherent,
not a shortcut: there is no way to move a bound socket.

Ordering
--------

The directory moves **first** and everything else follows.  A crash after
the move leaves state in the new location with the old unit still
pointing at the old name, which the next daemon start resolves on its own
because :func:`swarm.paths.state_dir` prefers the relocated directory.
The reverse order — rewriting the unit first — would leave a unit naming
a directory that does not exist yet, which is a harder hole to climb out
of by hand.

Every step is idempotent, so a half-finished relocation is fixed by
running the command again rather than by unpicking it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from swarm.logging import get_logger
from swarm.paths import original_state_dir, relocated_state_dir

_log = get_logger("relocate")

LEGACY_UNIT = "swarm-legacy.service"
OLD_UNIT = "swarm.service"
_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"


class RelocationError(RuntimeError):
    """The relocation cannot proceed safely, so nothing was changed."""


@dataclass
class LiveProcess:
    """Something still running that the relocation will take down."""

    kind: str
    pid: int
    detail: str = ""


@dataclass
class RelocationPlan:
    """What a relocation would do, resolved against the current machine."""

    source: Path
    target: Path
    move_needed: bool
    old_unit: Path | None
    new_unit: Path
    unit_active: bool
    old_entrypoints: list[Path] = field(default_factory=list)
    live: list[LiveProcess] = field(default_factory=list)
    stale_enable_link: bool = False

    @property
    def already_done(self) -> bool:
        """True only when nothing of the old name is left anywhere.

        The stale enable link counts: a dangling
        ``default.target.wants/swarm.service`` makes systemd complain on
        every reload, and reporting "already relocated" while leaving it
        behind means re-running never cleans it up.
        """
        return (
            not self.move_needed
            and self.old_unit is None
            and not self.old_entrypoints
            and not self.stale_enable_link
        )


@dataclass
class RelocationResult:
    moved: bool
    source: Path
    target: Path
    unit_written: Path | None
    old_unit_removed: bool
    entrypoints_removed: list[Path]
    dropins_carried: list[Path] = field(default_factory=list)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``systemctl --user``, tolerating a machine that has no systemd.

    macOS and systemd-less WSL have no ``systemctl`` at all, and the raw
    call raises ``FileNotFoundError``.  Unguarded, that aborted the
    relocation *after* the state directory had already moved — leaving a
    correct-but-alarming half-finished run and a raw traceback.  A failed
    CompletedProcess lets the rest of the sequence finish and report.
    """
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        _log.warning("systemctl --user %s unavailable on this machine", " ".join(args))
        return subprocess.CompletedProcess(["systemctl", *args], 1, stdout="", stderr="")


def _unit_is_active(unit: str) -> bool:
    try:
        return _systemctl("is-active", unit).stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def find_live_processes(state: Path) -> list[LiveProcess]:
    """Daemon and holder still running against *state*, if any.

    Reported so the operator sees what the confirmation is actually about
    rather than a generic warning.
    """
    live: list[LiveProcess] = []
    daemon_pid = _read_pid(state / "daemon.lock")
    if daemon_pid is not None and _pid_alive(daemon_pid):
        live.append(LiveProcess("daemon", daemon_pid, "holds swarm.db and the listen port"))
    holder_pid = _read_pid(state / "holder.pid")
    if holder_pid is not None and _pid_alive(holder_pid):
        live.append(LiveProcess("pty-holder", holder_pid, "owns every worker terminal"))
    return live


def _shim_directories() -> list[Path]:
    """Every directory a ``swarm`` shim could plausibly live in.

    ``uv`` honours ``$UV_TOOL_BIN_DIR`` and ``$XDG_BIN_HOME``, so checking
    only ``~/.local/bin`` would silently leave the old name occupied on
    any install that moved its bin directory — the one thing this command
    exists to prevent.
    """
    seen: list[Path] = []
    for raw in (
        os.environ.get("UV_TOOL_BIN_DIR"),
        os.environ.get("XDG_BIN_HOME"),
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / "bin"),
    ):
        if not raw:
            continue
        directory = Path(raw).expanduser()
        if directory not in seen:
            seen.append(directory)
    return seen


def _entrypoint_candidates() -> list[Path]:
    """Installed ``swarm`` shims that would keep the old name alive."""
    found: list[Path] = []
    for directory in _shim_directories():
        candidate = directory / "swarm"
        if (candidate.exists() or candidate.is_symlink()) and candidate not in found:
            found.append(candidate)
    return found


def plan(*, source: Path | None = None, target: Path | None = None) -> RelocationPlan:
    """Resolve what relocating would change, touching nothing."""
    src = source or original_state_dir()
    dst = target or relocated_state_dir()
    old_unit = _UNIT_DIR / OLD_UNIT
    wants = _UNIT_DIR / "default.target.wants" / OLD_UNIT
    return RelocationPlan(
        source=src,
        target=dst,
        move_needed=src.is_dir(),
        old_unit=old_unit if old_unit.exists() else None,
        new_unit=_UNIT_DIR / LEGACY_UNIT,
        unit_active=_unit_is_active(OLD_UNIT),
        old_entrypoints=_entrypoint_candidates(),
        live=find_live_processes(src if src.is_dir() else dst),
        stale_enable_link=wants.is_symlink() and not wants.exists(),
    )


def _stop_live(plan_: RelocationPlan) -> None:
    """Take the unit down so nothing holds the directory mid-move."""
    if plan_.unit_active:
        _systemctl("stop", OLD_UNIT)
    for proc in plan_.live:
        if not _pid_alive(proc.pid):
            continue
        try:
            os.kill(proc.pid, 15)
        except OSError:
            _log.warning("could not signal %s pid %s", proc.kind, proc.pid, exc_info=True)


def _move_state(src: Path, dst: Path) -> bool:
    if not src.is_dir():
        # Nothing to move — an install relocated before it ever ran.  The
        # target still has to exist, because its existence is what marks
        # this install as relocated.  Without it the unit and entrypoint
        # would be renamed while `state_dir()` still resolved to the old
        # path: an install that looks relocated but writes its state back
        # to the name it was supposed to free.
        dst.mkdir(parents=True, exist_ok=True)
        return False
    if dst.exists():
        raise RelocationError(
            f"Target already exists: {dst}. Move or remove it before relocating, "
            "so two hives are never merged by accident."
        )
    # Same filesystem in every normal install, so this is a rename and the
    # directory is never half-copied.  shutil.move falls back to a copy
    # across devices, which is slower but still leaves a complete tree.
    shutil.move(str(src), str(dst))
    return True


# The ``swarm`` token in an ExecStart, whichever shape the unit takes:
# production ``/home/u/.local/bin/swarm serve`` (slash before) or dev
# ``/path/to/uv run swarm serve`` (space before).  Matching only the
# production form would leave a dev unit invoking a command this very
# function is about to delete.
_EXEC_SWARM_RE = re.compile(r"^(ExecStart=.*)([/\s])swarm(\s+serve)", re.MULTILINE)


def _rename_exec_start(unit: str) -> str:
    """Point an ExecStart line at ``swarm-legacy`` instead of ``swarm``."""
    return _EXEC_SWARM_RE.sub(r"\1\2swarm-legacy\3", unit)


def _write_unit() -> Path:
    from swarm.service import generate_unit

    # Point the unit at the renamed entrypoint; the old one is removed below.
    unit = _rename_exec_start(generate_unit())
    _UNIT_DIR.mkdir(parents=True, exist_ok=True)
    path = _UNIT_DIR / LEGACY_UNIT
    path.write_text(unit)
    return path


def _migrate_dropins() -> list[Path]:
    """Carry ``swarm.service.d/`` overrides across to the renamed unit.

    A drop-in directory is how dev installs pin ExecStart at the project
    venv.  Dropping the old unit without moving these would silently
    discard the operator's override — the service would still start, just
    not the way they configured it, which is the worst kind of quiet
    breakage.  Each carried file gets the same ExecStart rename as the
    unit itself.
    """
    src = _UNIT_DIR / f"{OLD_UNIT}.d"
    if not src.is_dir():
        return []
    dst = _UNIT_DIR / f"{LEGACY_UNIT}.d"
    dst.mkdir(parents=True, exist_ok=True)
    carried: list[Path] = []
    for conf in sorted(src.glob("*.conf")):
        target = dst / conf.name
        try:
            target.write_text(_rename_exec_start(conf.read_text()))
            carried.append(target)
        except OSError:
            _log.warning("could not carry drop-in %s", conf, exc_info=True)
    if carried:
        shutil.rmtree(src, ignore_errors=True)
    return carried


def _remove_old_unit() -> bool:
    """Disable and delete ``swarm.service``, including a stale enable link.

    ``disable`` runs even when the unit file is already gone: systemd
    records the enablement as a symlink under ``default.target.wants``,
    and a unit file removed by hand leaves that symlink dangling, which
    systemd reports on every ``daemon-reload``.
    """
    old = _UNIT_DIR / OLD_UNIT
    _systemctl("disable", OLD_UNIT)
    wants = _UNIT_DIR / "default.target.wants" / OLD_UNIT
    if wants.is_symlink() and not wants.exists():
        try:
            wants.unlink()
        except OSError:
            _log.warning("could not clear dangling enable link %s", wants, exc_info=True)
    if not old.exists():
        return False
    old.unlink()
    return True


def _remove_old_entrypoints(paths: list[Path]) -> list[Path]:
    removed: list[Path] = []
    for path in paths:
        try:
            path.unlink()
            removed.append(path)
        except OSError:
            _log.warning("could not remove old entrypoint %s", path, exc_info=True)
    return removed


def relocate(plan_: RelocationPlan, *, start: bool = True) -> RelocationResult:
    """Execute *plan_*.  Idempotent — safe to re-run after a failure."""
    _stop_live(plan_)
    moved = _move_state(plan_.source, plan_.target)
    unit = _write_unit()
    dropins = _migrate_dropins()
    removed_unit = _remove_old_unit()
    removed_entrypoints = _remove_old_entrypoints(plan_.old_entrypoints)
    _systemctl("daemon-reload")
    if start:
        _systemctl("enable", LEGACY_UNIT)
        _systemctl("start", LEGACY_UNIT)
    return RelocationResult(
        moved=moved,
        source=plan_.source,
        target=plan_.target,
        unit_written=unit,
        old_unit_removed=removed_unit,
        entrypoints_removed=removed_entrypoints,
        dropins_carried=dropins,
    )
