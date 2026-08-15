"""Command-routing dispatch for the PTY holder sidecar.

Extracted from ``holder.py`` (task #516, audit-code 2026-05-27) so the
holder owns only PTY lifecycle concerns. Each handler accepts a JSON
command message from the daemon and returns a JSON response dict.
State changes route back to the held :class:`~swarm.pty.holder.PtyHolder`
via ``self.holder``.

The wire protocol is unchanged — the daemon still sends
``{"cmd": "<name>", ...}`` and receives ``{"ok": bool, ...}`` — so this
refactor is invisible to all callers.
"""

from __future__ import annotations

import base64
import json
import os
import re
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from swarm.logging import get_logger

if TYPE_CHECKING:
    from swarm.pty.holder import PtyHolder

_log = get_logger("pty.command_handler")

#: Names the holder will accept for a spawned session. This is the real
#: boundary — the daemon can generate any name it likes, but a name that
#: fails here is rejected at ``spawn`` with "invalid worker name" and no
#: amount of daemon-side validation changes that. Named (rather than
#: inlined in :meth:`_cmd_spawn`) so callers that synthesise session names
#: can be TESTED against the actual rule instead of a fake pool's
#: assumption about it.
WORKER_NAME_RE = re.compile(r"[a-zA-Z0-9_-]+")


class PtyCommandHandler:
    """Translates JSON command dicts → JSON response dicts.

    Created once per :class:`PtyHolder`; the holder owns one instance and
    routes ``_handle_client`` traffic through it via :meth:`dispatch`.
    """

    def __init__(self, holder: PtyHolder) -> None:
        self.holder = holder

    def dispatch(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a command from the daemon. Returns a response dict."""
        cmd = msg.get("cmd", "")
        handler = self._CMD_HANDLERS.get(cmd)
        if handler is None:
            return {"ok": False, "error": f"unknown command: {cmd}"}
        return handler(self, msg)

    # ----- individual command handlers -----

    def _cmd_ping(self, msg: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True}

    def _cmd_version(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Return the holder's import-time source hash + its PID.

        The daemon compares ``source_hash`` against a fresh hash of
        ``holder.py`` on disk to detect bytecode skew — a fix that
        shipped but never ran because the holder wasn't bounced. The
        PID is included so the operator warning can name the exact
        process that needs killing.
        """
        # Lazy-import the holder accessor to avoid a circular import at
        # module load: holder.py imports this module, this module
        # references holder.holder_source_hash_at_import.
        from swarm.pty.holder import holder_source_hash_at_import

        return {
            "ok": True,
            "source_hash": holder_source_hash_at_import(),
            "pid": os.getpid(),
        }

    def _cmd_spawn(self, msg: dict[str, Any]) -> dict[str, Any]:
        from swarm.pty.holder import _DEFAULT_COLS, _DEFAULT_ROWS, HolderError

        name = msg.get("name", "")
        cwd = msg.get("cwd", "/tmp")
        if not name or not WORKER_NAME_RE.fullmatch(name):
            return {"ok": False, "error": f"invalid worker name: {name!r}"}
        if not os.path.isabs(cwd):
            return {"ok": False, "error": f"cwd must be absolute: {cwd!r}"}
        command = msg.get("command")
        try:
            cols = max(1, min(500, int(msg.get("cols", _DEFAULT_COLS))))
            rows = max(1, min(500, int(msg.get("rows", _DEFAULT_ROWS))))
        except (ValueError, TypeError):
            return {"ok": False, "error": "invalid cols/rows"}
        shell_wrap = bool(msg.get("shell_wrap", False))
        try:
            worker = self.holder.spawn_worker(name, cwd, command, cols, rows, shell_wrap=shell_wrap)
            return {"ok": True, "name": worker.name, "pid": worker.pid}
        except (HolderError, OSError) as e:
            return {"ok": False, "error": str(e)}

    def _cmd_list(self, msg: dict[str, Any]) -> dict[str, Any]:
        return {"workers": self.holder.list_workers()}

    def _cmd_write(self, msg: dict[str, Any]) -> dict[str, Any]:
        name = msg.get("name", "")
        try:
            data = base64.b64decode(msg.get("data", ""))
        except Exception:
            return {"ok": False, "error": "invalid base64"}
        # #1658: carry the caller's identity through to the audit row. Defaults to
        # "unknown" rather than guessing, so an unlabelled write is visibly unlabelled
        # instead of silently attributed to whoever happens to be nearby.
        actor = str(msg.get("actor") or "unknown")
        ok = self.holder.write_to_worker(name, data, actor)
        return {"ok": ok}

    def _cmd_signal(self, msg: dict[str, Any]) -> dict[str, Any]:
        name = msg.get("name", "")
        sig_name = msg.get("sig", "SIGINT")
        allowed = {"SIGINT", "SIGTERM", "SIGKILL", "SIGCONT", "SIGWINCH", "SIGTSTP"}
        if sig_name not in allowed:
            return {"ok": False, "error": f"signal {sig_name!r} not allowed"}
        sig = getattr(signal, sig_name, signal.SIGINT)
        ok = self.holder.signal_worker(name, sig)
        return {"ok": ok}

    def _cmd_resize(self, msg: dict[str, Any]) -> dict[str, Any]:
        from swarm.pty.holder import _DEFAULT_COLS, _DEFAULT_ROWS

        name = msg.get("name", "")
        try:
            cols = max(1, min(500, int(msg.get("cols", _DEFAULT_COLS))))
            rows = max(1, min(500, int(msg.get("rows", _DEFAULT_ROWS))))
        except (ValueError, TypeError):
            return {"ok": False, "error": "invalid cols/rows"}
        ok = self.holder.resize_worker(name, cols, rows)
        return {"ok": ok}

    def _cmd_kill(self, msg: dict[str, Any]) -> dict[str, Any]:
        name = msg.get("name", "")
        ok = self.holder.kill_worker(name)
        return {"ok": ok}

    def _cmd_snapshot(self, msg: dict[str, Any]) -> dict[str, Any]:
        name = msg.get("name", "")
        worker = self.holder.workers.get(name)
        if not worker:
            return {"ok": False, "error": "worker not found"}
        data = worker.buffer.snapshot()
        return {"ok": True, "data": base64.b64encode(data).decode()}

    def _cmd_shutdown(self, msg: dict[str, Any]) -> dict[str, Any]:
        self.holder._shutdown_all()
        return {"ok": True}

    def _cmd_restart_in_place(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Re-exec the holder process while preserving worker PTY FDs.

        The flow:
          1. Snapshot every live worker's name/pid/cwd/command/cols/rows
             plus the FD and the current ring-buffer bytes into a JSON
             handoff file.
          2. Mark each PTY master FD as inheritable (clear FD_CLOEXEC) so
             the FDs survive ``os.execv``.
          3. Stop the asyncio server, remove the socket file (the new
             holder process will re-bind it when it starts up).
          4. ``os.execv`` into a fresh holder process invoked with
             ``--inherit <handoff_path>``.

        Worker child processes (Claude Code sessions) are unaffected —
        they own the slave end of the PTY, and the kernel keeps the slave
        open as long as anyone holds the master. The brief gap between
        execv and the new holder rebinding the socket is filled by the
        kernel's own PTY buffer (workers continue writing into the slave;
        new holder reads it on resume).

        On failure to write the state file or before execv, the function
        returns an error response and the existing holder keeps running.
        Once execv runs, control never returns.
        """
        from swarm.pty.holder import DEFAULT_HANDOFF_PATH, _make_fd_inheritable

        h = self.holder
        # Default to the canonical handoff path; allow the caller to
        # override (mostly useful in tests with an isolated socket dir).
        handoff_path = Path(msg.get("handoff_path") or str(DEFAULT_HANDOFF_PATH))

        try:
            workers_state: list[dict[str, Any]] = []
            for w in list(h.workers.values()):
                if not w.alive:
                    continue
                try:
                    _make_fd_inheritable(w.master_fd)
                except OSError as exc:
                    _log.warning(
                        "restart_in_place: failed to clear FD_CLOEXEC on %s (fd=%d): %s",
                        w.name,
                        w.master_fd,
                        exc,
                    )
                    continue
                workers_state.append(
                    {
                        "name": w.name,
                        "pid": w.pid,
                        "master_fd": w.master_fd,
                        "cwd": w.cwd,
                        "command": list(w.command),
                        "cols": w.cols,
                        "rows": w.rows,
                        "buffer": base64.b64encode(w.buffer.snapshot()).decode(),
                    }
                )

            handoff_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic-ish write: tmp + rename.
            tmp_path = handoff_path.with_suffix(handoff_path.suffix + ".tmp")
            tmp_path.write_text(json.dumps({"workers": workers_state}))
            tmp_path.replace(handoff_path)
        except OSError as exc:
            return {"ok": False, "error": f"failed to write handoff state: {exc}"}

        socket_path_str = str(h.socket_path)

        # Stop accepting connections + drop the socket file so the new
        # process can bind it cleanly.
        if h._server:
            try:
                h._server.close()
            except Exception:
                _log.debug("error closing server pre-exec", exc_info=True)
        try:
            if h.socket_path.exists():
                h.socket_path.unlink()
        except OSError:
            pass

        _log.warning(
            "restart_in_place: handing off %d worker(s) via %s",
            len(workers_state),
            handoff_path,
        )

        # Update the PID file to reflect the new pid (it's the same pid
        # post-execv since execv doesn't fork). Done lazily by the new
        # holder's startup path.
        argv = [
            sys.executable,
            "-m",
            "swarm.pty.holder",
            "--inherit",
            str(handoff_path),
            "--socket",
            socket_path_str,
        ]
        # NOTE: never returns on success.
        os.execv(sys.executable, argv)
        return {"ok": False, "error": "execv returned (should never happen)"}

    _CMD_HANDLERS: ClassVar[
        dict[str, Callable[[PtyCommandHandler, dict[str, Any]], dict[str, Any]]]
    ] = {
        "ping": _cmd_ping,
        "version": _cmd_version,
        "spawn": _cmd_spawn,
        "list": _cmd_list,
        "write": _cmd_write,
        "signal": _cmd_signal,
        "resize": _cmd_resize,
        "kill": _cmd_kill,
        "snapshot": _cmd_snapshot,
        "shutdown": _cmd_shutdown,
        "restart_in_place": _cmd_restart_in_place,
    }
