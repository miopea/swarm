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
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from swarm.logging import get_logger
from swarm.paths import original_state_dir, relocated_state_dir

_log = get_logger("relocate")

# A Unix socket path is capped by ``sockaddr_un.sun_path`` — 108 bytes on
# Linux, 104 on macOS, NUL included.  The relocated directory name is seven
# bytes longer than the original, so a deep enough home directory can push
# ``<state>/holder.sock`` over the limit.  The holder then cannot bind and
# no worker can start — after a one-way move.  Checked before anything is
# touched, using the smaller of the two limits.
_SUN_PATH_MAX = 104

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
    # The NEW side, which `already_done` deliberately does not consider.
    new_unit_exists: bool = False
    new_unit_active: bool = False
    target_exists: bool = False

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

    @property
    def blocked_reason(self) -> str | None:
        """Why relocating would fail, or None when it can proceed.

        ``_move_state`` already refuses to merge two hives — but it
        refused from *inside* :func:`relocate`, after ``_stop_live`` had
        stopped the service and killed every worker. The operator paid
        the full destructive price for an operation that could never
        have succeeded, and the traceback went to a detached process
        nobody was reading.

        Both directories existing means this hive already relocated and
        something recreated the old name. The remedy is to find out what
        did that — not to move a live hive onto itself.
        """
        if self.move_needed and self.target_exists:
            return (
                f"{self.source} and {self.target} both exist. This hive is already "
                f"relocated and something recreated the old directory; relocating "
                f"would merge two hives. Inspect {self.source} and remove it once "
                f"you know what recreated it."
            )
        return None

    @property
    def needs_repair(self) -> bool:
        """Relocated, but the hive it relocated INTO is not running.

        Kept out of :attr:`already_done` deliberately.  Folding it in
        would make a relocated install whose unit is merely stopped
        report as un-relocated, and #1677's fix exists precisely to stop
        showing the destructive plan to operators who already moved.
        The old name being gone and the new hive being healthy are two
        different questions; conflating them is what let a relocation
        that stranded the daemon answer "nothing to do".
        """
        return self.already_done and not (self.new_unit_exists and self.new_unit_active)


@dataclass
class RelocationResult:
    moved: bool
    source: Path
    target: Path
    unit_written: Path | None
    old_unit_removed: bool
    entrypoints_removed: list[Path]
    dropins_carried: list[Path] = field(default_factory=list)
    # Shims still occupying the old name after the attempt.  Reported so
    # the command cannot claim success it did not achieve.
    still_occupied: list[Path] = field(default_factory=list)
    # The old state directory reappearing means something is still running
    # against it and will keep re-occupying the freed name.
    source_recreated: bool = False


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


def _running_bin_dir() -> str | None:
    """The directory this very command was launched from.

    The most reliable signal there is: if the operator can type
    ``swarm relocate`` or ``swarm-legacy relocate``, the shim they typed
    is sitting right there, whatever ``uv`` was configured to do at
    install time.  ``$UV_TOOL_BIN_DIR`` is set when a tool is *installed*
    and usually absent from the shell that later relocates, so relying on
    it alone let a custom bin directory keep the old name occupied while
    the command reported success.
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if not argv0:
        return None
    try:
        resolved = Path(argv0).resolve()
    except OSError:
        return None
    parent = resolved.parent
    return str(parent) if parent.is_dir() else None


def _receipt_bin_dirs() -> list[str]:
    """Entrypoint locations recorded by ``uv`` in this tool's receipt.

    ``uv-receipt.toml`` names the exact ``install-path`` of every console
    script it wrote, which is authoritative even when the environment that
    chose it is long gone.  Located relative to this package so it is
    found regardless of ``$UV_TOOL_DIR``.
    """
    found: list[str] = []
    try:
        here = Path(__file__).resolve()
    except OSError:
        return found
    for parent in here.parents:
        receipt = parent / "uv-receipt.toml"
        if not receipt.is_file():
            continue
        try:
            text = receipt.read_text(encoding="utf-8")
        except OSError:
            break
        for match in re.finditer(r'install-path\s*=\s*"([^"]+)"', text):
            directory = str(Path(match.group(1)).parent)
            if directory not in found:
                found.append(directory)
        break
    return found


def _uv_bin_dir() -> str | None:
    """Where ``uv`` actually puts console scripts, asked at runtime.

    ``$UV_TOOL_BIN_DIR`` is an *install-time* variable and is usually not
    set in the shell that later runs ``swarm relocate``.  An install that
    used a custom bin directory would then keep ``swarm`` occupied while
    the command cheerfully reported success — the exact failure this
    command exists to prevent.  ``uv tool dir --bin`` reports the
    configured directory whether or not the variable is set.
    """
    try:
        proc = subprocess.run(
            ["uv", "tool", "dir", "--bin"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return out or None


def _shim_directories() -> list[Path]:
    """Every directory a ``swarm`` shim could plausibly live in.

    ``uv`` honours ``$UV_TOOL_BIN_DIR`` and ``$XDG_BIN_HOME``, so checking
    only ``~/.local/bin`` would silently leave the old name occupied on
    any install that moved its bin directory — the one thing this command
    exists to prevent.
    """
    seen: list[Path] = []
    for raw in (
        _running_bin_dir(),
        *_receipt_bin_dirs(),
        _uv_bin_dir(),
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


def dir_size_bytes(path: Path) -> int | None:
    """Total size of *path* in bytes, or ``None`` when it cannot be walked.

    ``None`` rather than ``0`` on failure: a relocation banner that says
    "0 B" about a 99 MB hive reads as "nothing to move" and invites the
    operator to skip the very thing they need to do.  Not knowing and
    knowing it is empty are different facts, so they get different values.
    """
    # rglob on a missing directory yields nothing and raises nothing, so
    # the sum would be a perfectly confident 0.  Check first.
    if not path.is_dir():
        return None
    try:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        return None


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
        new_unit_exists=(_UNIT_DIR / LEGACY_UNIT).exists(),
        new_unit_active=_unit_is_active(LEGACY_UNIT),
        target_exists=dst.exists(),
    )


def _check_socket_path_fits(target: Path) -> None:
    """Refuse a move that would leave the holder unable to bind."""
    sock = target / "holder.sock"
    length = len(str(sock).encode()) + 1  # sockaddr_un is NUL-terminated
    if length > _SUN_PATH_MAX:
        raise RelocationError(
            f"The relocated holder socket path would be {length} bytes, over the "
            f"{_SUN_PATH_MAX}-byte limit for a Unix socket:\n  {sock}\n"
            "The pty-holder could not bind it and no worker would start. "
            "Nothing was changed. Use $SWARM_STATE_DIR to place the state "
            "directory somewhere shorter instead."
        )


def _stop_live(plan_: RelocationPlan, *, timeout: float = 20.0) -> None:
    """Take the unit down and **wait** for it to actually be gone.

    Signalling without waiting is not enough.  A daemon that is still
    shutting down keeps the log path it resolved at import — the old one —
    and recreates the very directory the move just emptied, re-occupying
    the name and making a re-run believe there is still state to move.
    Observed exactly that: a relocation that reported success, with
    ``~/.swarm`` back moments later containing a lone ``swarm.log``.

    So: SIGTERM, wait, then SIGKILL what refuses to leave.  The operator
    has already been told every worker dies; a process that ignores the
    polite request does not get to undo the relocation.
    """
    if plan_.unit_active:
        _systemctl("stop", OLD_UNIT)
    pids = [p.pid for p in plan_.live if _pid_alive(p.pid)]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            _log.warning("could not signal pid %s", pid, exc_info=True)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pids = [pid for pid in pids if _pid_alive(pid)]
        if not pids:
            return
        time.sleep(0.2)

    for pid in pids:
        _log.warning("pid %s ignored SIGTERM; killing it before the move", pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            _log.warning("could not kill pid %s", pid, exc_info=True)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and any(_pid_alive(pid) for pid in pids):
        time.sleep(0.2)


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
    """Write ``swarm-legacy.service``, pointed at the renamed entrypoint.

    ``generate_unit()`` locates the binary with ``shutil.which``, which
    fails outright when the command was invoked by absolute path or from a
    shell whose PATH lacks the bin directory — both perfectly ordinary.
    It raised *after* the state directory had already moved, leaving a
    half-finished relocation reported as a traceback.  The directories we
    already know about are put on PATH for the duration of the call.
    """
    from swarm.service import generate_unit

    original = os.environ.get("PATH", "")
    extra = [str(d) for d in _shim_directories() if d.is_dir()]
    if extra:
        os.environ["PATH"] = (
            os.pathsep.join([*extra, original]) if original else os.pathsep.join(extra)
        )
    try:
        unit = _rename_exec_start(generate_unit())
    finally:
        os.environ["PATH"] = original
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


def repair(plan_: RelocationPlan, *, start: bool = True) -> list[str]:
    """Finish a relocation whose old side is gone but whose new side is not up.

    NON-DESTRUCTIVE, unlike :func:`relocate`: nothing is stopped, moved or
    deleted.  It rewrites the unit, reloads, and enables/starts it — every
    step idempotent, so running it on a healthy install changes nothing.

    Exists because "the old name is gone" and "the new hive is running"
    were treated as one question.  A relocation that completed every
    removal but left a daemon that would not start reported "Already
    relocated — nothing to do", which is a true statement about the old
    name and a useless one to an operator whose dashboard is down.

    Returns the human-readable steps taken, for the caller to print.
    """
    steps: list[str] = []
    if not plan_.new_unit_exists:
        written = _write_unit()
        steps.append(f"Wrote missing unit: {written}")
    _systemctl("daemon-reload")
    steps.append("Reloaded systemd")
    if start:
        enabled = _systemctl("enable", LEGACY_UNIT)
        if enabled.returncode != 0:
            steps.append(f"enable failed: {enabled.stderr.strip() or enabled.stdout.strip()}")
        started = _systemctl("start", LEGACY_UNIT)
        if started.returncode != 0:
            # The unit refusing to start is the whole reason we are here,
            # so surface systemd's reason rather than reporting a step
            # that silently did nothing.
            steps.append(f"start failed: {started.stderr.strip() or started.stdout.strip()}")
        else:
            steps.append(f"Started {LEGACY_UNIT}")
    return steps


def preflight(plan_: RelocationPlan) -> None:
    """Every check that must pass BEFORE anything destructive happens.

    Ordering is the whole point.  ``_move_state`` refused to merge two
    hives, but it refused after ``_stop_live`` had already stopped the
    service and SIGKILLed the daemon and holder — so an operator whose
    old directory had been recreated lost every worker to an operation
    that could not have succeeded under any circumstances.  A guard that
    fires after the damage is not a guard.

    Raises :class:`RelocationError` with an actionable message.
    """
    blocked = plan_.blocked_reason
    if blocked:
        raise RelocationError(blocked)
    _check_socket_path_fits(plan_.target)


def relocate(plan_: RelocationPlan, *, start: bool = True) -> RelocationResult:
    """Execute *plan_*.  Idempotent — safe to re-run after a failure."""
    preflight(plan_)
    _stop_live(plan_)
    moved = _move_state(plan_.source, plan_.target)
    # Past this point the state directory has already moved.  Anything that
    # goes wrong now must say so in terms the operator can act on — every
    # step is idempotent, so re-running finishes the job — rather than
    # surfacing a traceback that reads like data loss.
    try:
        unit = _write_unit()
        dropins = _migrate_dropins()
        removed_unit = _remove_old_unit()
        removed_entrypoints = _remove_old_entrypoints(plan_.old_entrypoints)
        _systemctl("daemon-reload")
    except Exception as exc:
        raise RelocationError(
            f"State was moved to {plan_.target} but the rest did not finish: {exc}\n"
            "Nothing was lost. Re-run the command to complete it."
        ) from exc
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
        still_occupied=_entrypoint_candidates(),
        source_recreated=plan_.source.exists(),
    )
