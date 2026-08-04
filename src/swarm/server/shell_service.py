"""ShellService — operator bash sessions rooted in a worker's directory.

A shell lets the operator run local commands where a worker lives without
leaving the dashboard or borrowing the agent's own PTY (typing into that would
interleave with the agent's turn).

A SHELL IS DELIBERATELY NOT A ``Worker``. It is spawned into the same process
pool, because that is where PTYs live and the web terminal already knows how to
attach to one, but it is tracked here rather than in ``daemon.workers``. The
pool is a flat namespace shared with real workers and is *not* the boundary it
looks like: :meth:`WorkerService.discover` wraps every process the holder
reports in a ``Worker``. Anything visible there inherits the whole worker
machinery — sidebar presence, task assignment, drone polling, PTY-output state
classification, IdleWatcher nudges. A bash prompt handed a task drops it
silently; nothing is running that could execute it.

Two things keep that from happening, and they have to agree:

* every session name carries the :data:`SHELL_SESSION_PREFIX`, and
* ``WorkerService.discover`` skips names matching :func:`is_shell_session`.

Lifecycle is ephemeral (operator decision): closing the window kills bash. A
shell therefore cannot outlive the UI showing it and become an orphan nobody can
see or reap — the tradeoff being that a long-running command dies with the
window.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from swarm.logging import get_logger

if TYPE_CHECKING:
    from swarm.pty.process import WorkerProcess
    from swarm.pty.provider import WorkerProcessProvider
    from swarm.worker.worker import Worker

_log = get_logger("server.shell_service")

#: Namespace separating shells from workers in the pool's flat key space.
#:
#: MUST satisfy the holder's ``WORKER_NAME_RE`` (``[a-zA-Z0-9_-]+``) — the
#: holder validates every spawn name and rejects the rest outright. This was
#: originally ``shell:``, chosen because ``:`` cannot appear in a worker name
#: and so could never collide; that is exactly the character the holder
#: forbids, and every Open-shell click died with "invalid worker name".
#:
#: The tradeoff of a legal prefix is that it shares the worker-name charset
#: and *could* collide. :meth:`WorkerService.discover` therefore defers to
#: configuration rather than trusting the prefix alone — see the filter there.
SHELL_SESSION_PREFIX = "swarm_shell_"

#: A login shell, so the operator gets the same PATH and profile they would in
#: a normal terminal. NOT spawned with ``shell_wrap``: that re-execs bash after
#: the command exits, which would resurrect a session the operator just ended
#: with ``exit`` and strand it in the holder.
SHELL_COMMAND = ["bash", "--login"]


def shell_session_name(worker_name: str) -> str:
    """Pool key for *worker_name*'s shell."""
    return f"{SHELL_SESSION_PREFIX}{worker_name}"


def is_shell_session(name: str) -> bool:
    """True when *name* is a shell session key.

    A prefix test, not a substring search — a worker named ``my-shell:thing``
    is a worker.
    """
    return name.startswith(SHELL_SESSION_PREFIX)


@dataclass
class ShellSession:
    """A live bash PTY for one worker.

    Structurally duck-types the part of ``Worker`` the terminal bridge uses
    (``name`` + ``process``), which is what lets ``/ws/terminal`` attach to a
    shell without the bridge knowing shells exist.
    """

    name: str
    worker_name: str
    path: str
    process: WorkerProcess


class ShellService:
    """Opens, tracks and closes operator shell sessions."""

    def __init__(
        self,
        get_pool: Callable[[], WorkerProcessProvider | None],
        get_worker: Callable[[str], Worker | None],
    ) -> None:
        self._get_pool = get_pool
        self._get_worker = get_worker
        self._sessions: dict[str, ShellSession] = {}

    def get(self, worker_name: str) -> ShellSession | None:
        """The live shell for *worker_name*, if one is open."""
        return self._sessions.get(worker_name)

    def get_by_session_name(self, session_name: str) -> ShellSession | None:
        """Reverse lookup from the pool key — what the terminal bridge has."""
        if not is_shell_session(session_name):
            return None
        return self._sessions.get(session_name[len(SHELL_SESSION_PREFIX) :])

    async def open(self, worker_name: str) -> ShellSession:
        """Open (or return) a shell in *worker_name*'s directory.

        Idempotent: the pool is keyed by name, so a second spawn under the same
        key would evict the first process from the registry while it kept
        running — an orphan bash with no handle to kill it.
        """
        from swarm.server.daemon import SwarmOperationError, WorkerNotFoundError

        worker = self._get_worker(worker_name)
        if worker is None:
            raise WorkerNotFoundError(f"Worker '{worker_name}' not found")

        existing = self._sessions.get(worker_name)
        if existing is not None and existing.process.is_alive:
            return existing
        if existing is not None:
            # Died on its own (operator typed `exit`, or the holder restarted).
            # Drop it so the spawn below is a genuine replacement.
            self._sessions.pop(worker_name, None)

        pool = self._get_pool()
        if pool is None:
            raise SwarmOperationError("Process pool unavailable — cannot open a shell")

        name = shell_session_name(worker_name)
        proc = await pool.spawn(
            name,
            worker.path,
            command=list(SHELL_COMMAND),
            shell_wrap=False,
        )
        session = ShellSession(
            name=name,
            worker_name=worker_name,
            path=worker.path,
            process=proc,
        )
        self._sessions[worker_name] = session
        _log.info("opened shell %s at %s (pid=%s)", name, worker.path, proc.pid)
        return session

    async def close(self, worker_name: str) -> None:
        """Kill *worker_name*'s shell. A no-op when none is open.

        The close handler fires on paths where ``open`` never succeeded (failed
        spawn, window closed mid-open), so this must not raise.
        """
        session = self._sessions.pop(worker_name, None)
        if session is None:
            return
        pool = self._get_pool()
        if pool is None:
            return
        await pool.kill(session.name)
        _log.info("closed shell %s", session.name)
