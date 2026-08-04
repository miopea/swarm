"""Operator shell sessions — a bash PTY rooted in a worker's directory.

THE LOAD-BEARING PROPERTY IS THAT A SHELL IS NOT A WORKER.

The cheap implementation is to spawn bash into the process pool and let the
existing terminal plumbing find it. That plumbing is worker-shaped, and the pool
is not the boundary anyone thinks it is: ``WorkerService.discover`` wraps *every*
process the holder reports in a ``Worker`` dataclass. A shell left visible to it
becomes a worker on the next reconcile — one that appears in the sidebar, is
eligible for task assignment, gets polled by drones, has its bash prompt
classified into BUZZING/RESTING, and gets nudged by the IdleWatcher. Assigning
real work to a bash prompt loses the task silently: nothing is there to run it.

So ``test_a_shell_is_never_adopted_as_a_worker`` is the test that matters here.
The rest describe lifecycle; that one describes the blast radius.

Lifecycle is ephemeral by operator decision: closing the window kills bash. That
is the simple contract, and it means a shell cannot outlive the UI that shows it
and become an orphan nobody can see or reap.
"""

from __future__ import annotations

import pytest

from swarm.server.shell_service import (
    SHELL_SESSION_PREFIX,
    ShellService,
    is_shell_session,
    shell_session_name,
)
from swarm.worker.worker import Worker
from tests.fakes.process import FakeWorkerProcess
from tests.test_worker_service import daemon  # noqa: F401  (fixture)


class _FakePool:
    """Minimal WorkerProcessProvider: records spawns and kills.

    ENFORCES THE HOLDER'S SPAWN PRECONDITIONS. The first version of this fake
    accepted any name and any cwd, so nine tests went green over a session
    name (``shell:swarm``) the real holder rejects on sight — the fake was
    encoding my assumption about the boundary rather than the boundary's
    actual rule. A double that is more permissive than the thing it doubles
    cannot fail the way production fails, which makes it worse than no test:
    it reports confidence it never earned.
    """

    def __init__(self) -> None:
        self.procs: dict[str, FakeWorkerProcess] = {}
        self.spawned: list[dict[str, object]] = []
        self.killed: list[str] = []

    async def spawn(self, name, cwd, command=None, cols=200, rows=50, shell_wrap=False):
        # Mirrors PtyCommandHandler._cmd_spawn, importing the same regex so the
        # two cannot drift apart.
        import os

        from swarm.pty.command_handler import WORKER_NAME_RE
        from swarm.pty.process import ProcessError

        if not name or not WORKER_NAME_RE.fullmatch(name):
            raise ProcessError(f"Spawn failed: invalid worker name: {name!r}")
        if not os.path.isabs(cwd):
            raise ProcessError(f"Spawn failed: cwd must be absolute: {cwd!r}")
        self.spawned.append(
            {"name": name, "cwd": cwd, "command": command, "shell_wrap": shell_wrap}
        )
        proc = FakeWorkerProcess(name=name, cwd=cwd)
        self.procs[name] = proc
        return proc

    def get(self, name):
        return self.procs.get(name)

    def get_all(self):
        return list(self.procs.values())

    async def kill(self, name):
        self.killed.append(name)
        proc = self.procs.pop(name, None)
        if proc:
            proc._alive = False

    async def discover(self):
        return list(self.procs.values())


@pytest.fixture
def svc():
    pool = _FakePool()
    worker = Worker(name="alice", path="/repos/alice", process=FakeWorkerProcess(name="alice"))
    service = ShellService(
        get_pool=lambda: pool,
        get_worker=lambda n: worker if n == "alice" else None,
    )
    service._pool = pool  # test handle
    service._worker = worker
    return service


# --- naming ------------------------------------------------------------


def test_session_names_are_namespaced_and_recognisable():
    """The prefix is the only thing separating a shell from a worker in the
    pool's flat namespace, so both directions have to be exact.

    Asserted against the constant rather than a literal — a test that hardcodes
    the prefix has to be edited in lockstep with it, and an edited assertion is
    not evidence.
    """
    assert shell_session_name("alice") == f"{SHELL_SESSION_PREFIX}alice"
    assert is_shell_session(shell_session_name("alice")) is True
    assert is_shell_session("alice") is False
    # A worker legitimately containing the substring must not be mistaken for
    # a shell — the check is a prefix, not a search.
    assert is_shell_session(f"my-{SHELL_SESSION_PREFIX}thing") is False


# --- the property that matters -----------------------------------------


@pytest.mark.asyncio
async def test_a_shell_is_never_adopted_as_a_worker(daemon):  # noqa: F811
    """A shell in the pool must survive a reconcile without becoming a Worker.

    If this regresses, the symptom is not an exception — it is a bash prompt
    sitting in the sidebar quietly accepting task assignments.
    """
    pool = _FakePool()
    await pool.spawn(shell_session_name("alice"), "/repos/alice", command=["bash", "--login"])
    await pool.spawn("bob", "/repos/bob", command=["claude"])
    daemon.pool = pool

    workers = await daemon.worker_svc.discover()

    names = {w.name for w in workers}
    assert "bob" in names, "a real worker was dropped by the shell filter"
    assert not any(is_shell_session(n) for n in names), f"shell adopted as a worker: {names}"


# --- lifecycle ---------------------------------------------------------


@pytest.mark.asyncio
async def test_open_spawns_a_login_shell_in_the_workers_directory(svc):
    session = await svc.open("alice")

    assert session.name == shell_session_name("alice")
    assert session.worker_name == "alice"
    spawn = svc._pool.spawned[0]
    assert spawn["cwd"] == "/repos/alice", "shell did not start in the worker's folder"
    assert spawn["command"][0] == "bash"
    # shell_wrap re-execs bash after the command exits, which would resurrect
    # the session the operator just closed with `exit`.
    assert spawn["shell_wrap"] is False


@pytest.mark.asyncio
async def test_reopening_returns_the_live_session_instead_of_orphaning_bash(svc):
    """Two opens must not leave a bash nobody has a handle to.

    The pool is keyed by name, so a second spawn under the same key would drop
    the first process from the registry while it kept running.
    """
    first = await svc.open("alice")
    second = await svc.open("alice")

    assert first is second
    assert len(svc._pool.spawned) == 1


@pytest.mark.asyncio
async def test_close_kills_bash_and_forgets_it(svc):
    await svc.open("alice")
    await svc.close("alice")

    assert svc._pool.killed == [shell_session_name("alice")]
    assert svc.get("alice") is None


@pytest.mark.asyncio
async def test_close_without_an_open_shell_is_a_no_op(svc):
    """The window's close handler fires on paths where open never succeeded."""
    await svc.close("alice")
    assert svc._pool.killed == []


@pytest.mark.asyncio
async def test_open_on_an_unknown_worker_raises(svc):
    from swarm.server.daemon import WorkerNotFoundError

    with pytest.raises(WorkerNotFoundError):
        await svc.open("nobody")


@pytest.mark.asyncio
async def test_reopening_after_close_gives_a_fresh_shell(svc):
    """Ephemeral, per the operator decision: no scrollback carried over."""
    await svc.open("alice")
    await svc.close("alice")
    await svc.open("alice")

    assert len(svc._pool.spawned) == 2


@pytest.mark.asyncio
async def test_killing_a_worker_closes_its_shell(daemon):  # noqa: F811
    """A shell is only reachable through its worker's context menu.

    Leave one behind after the worker is killed and it is invisible in the UI
    while still holding a bash process in the holder — reapable only by hand.
    """
    from swarm.server.shell_service import ShellService

    pool = _FakePool()
    daemon.pool = pool
    daemon.shell_svc = ShellService(
        get_pool=lambda: pool,
        get_worker=daemon.get_worker,
    )
    await daemon.shell_svc.open("alice")
    assert daemon.shell_svc.get("alice") is not None

    await daemon.kill_worker("alice")

    assert daemon.shell_svc.get("alice") is None
    assert shell_session_name("alice") in pool.killed


# --- the contract the fake pool did not enforce -------------------------


def test_session_name_is_one_the_HOLDER_will_actually_accept():
    """THE regression guard for the 2026-08-04 shipped bug.

    The first prefix was ``shell:`` — chosen *because* ``:`` cannot appear in
    a worker name, so a collision would have to be deliberate. The holder
    validates spawn names against ``[a-zA-Z0-9_-]+`` and rejects ``:``
    outright, so every real Open-shell click died with
    ``Spawn failed: invalid worker name: 'shell:swarm'``.

    Nine unit tests passed against a fake pool that validated nothing. The
    fake encoded my assumption about the holder rather than the holder's
    actual rule — the exact shape CLAUDE.md warns about: passing tests are
    not verification when the change touches an external system.

    So this asserts against ``WORKER_NAME_RE`` imported from the holder's own
    command handler. If either side moves, this breaks.
    """
    from swarm.pty.command_handler import WORKER_NAME_RE

    for worker in ("swarm", "rcg-dev-install", "my-rcg", "d365-solutions", "a_b-c"):
        name = shell_session_name(worker)
        assert WORKER_NAME_RE.fullmatch(name), f"holder would reject {name!r}"


@pytest.mark.asyncio
async def test_a_configured_worker_is_never_mistaken_for_a_shell(daemon):  # noqa: F811
    """The new prefix uses the same charset as worker names, so unlike ``:``
    it *could* collide. A false positive here is severe and silent: the
    discover filter would drop a real worker from the roster, and a worker
    that is simply absent produces no error anywhere to notice.

    So the filter defers to configuration — a configured worker always wins,
    which turns an unlikely-but-invisible failure into a no-op.
    """
    from swarm.config import WorkerConfig
    from swarm.server.shell_service import SHELL_SESSION_PREFIX

    hostile = f"{SHELL_SESSION_PREFIX}looks_like_a_shell"
    daemon.config.workers.append(WorkerConfig(name=hostile, path="/repos/x"))

    pool = _FakePool()
    await pool.spawn(hostile, "/repos/x", command=["claude"])
    daemon.pool = pool

    workers = await daemon.worker_svc.discover()

    assert hostile in {w.name for w in workers}, "a configured worker was dropped as a shell"
