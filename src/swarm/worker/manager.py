"""Worker lifecycle management: spawn, kill, revive workers via WorkerProcessProvider."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from swarm.config import WorkerConfig
from swarm.logging import get_logger
from swarm.providers import get_provider
from swarm.pty.process import ProcessError
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.pty.provider import WorkerProcessProvider

    # #1195: called with ``(worker_config, spawn_path)`` immediately before the
    # process starts, to write that worker's ``.mcp.json`` identity file into
    # the directory the session will actually run in.
    #
    # It is a REQUIRED keyword-only argument on both spawn helpers below, never
    # an optional one, because the invariant it protects already failed once by
    # being optional in practice: in #1187 the writer existed, worked, and the
    # create path simply never called it — producing live workers that
    # transmitted a PARENT directory's identity. Ownership guards are exact
    # comparisons against the canonicalised name, so such a worker silently IS
    # whichever worker owns the inherited file. Giving this a default would
    # restore that exact failure mode, so omitting it is a TypeError at the
    # call site.
    #
    # ``spawn_path`` is passed rather than derived from the config because
    # under ``isolation: worktree`` the two differ, and this layer is the only
    # one that knows which directory the session is started in.
    IdentityWriter = Callable[[WorkerConfig, str], None]

_log = get_logger("worker.manager")


def _resolve_provider_name(wc: WorkerConfig, default: str) -> str:
    """Return the provider name for a worker config, falling back to default."""
    return wc.provider or default


async def _resolve_worktree(
    wc: WorkerConfig,
) -> tuple[str, str, str]:
    """Resolve worktree isolation for a worker config.

    Returns ``(spawn_path, repo_path, worktree_branch)`` where
    *repo_path* and *worktree_branch* are empty strings when isolation
    is not enabled.
    """
    if wc.isolation != "worktree":
        return str(wc.resolved_path), "", ""

    from swarm.git.worktree import (
        create_worktree,
        is_git_repo,
        worktree_branch,
    )

    if not await is_git_repo(wc.resolved_path):
        _log.warning(
            "isolation=worktree but %s is not a git repo",
            wc.resolved_path,
        )
        return str(wc.resolved_path), "", ""

    wt_path = await create_worktree(wc.resolved_path, wc.name)
    return str(wt_path), str(wc.resolved_path), worktree_branch(wc.name)


async def _cleanup_worktree(repo_path: str, worker_name: str) -> None:
    """Remove a worktree after a failed spawn. Best-effort — logs on failure."""
    try:
        from pathlib import Path

        from swarm.git.worktree import remove_worktree

        await remove_worktree(Path(repo_path), worker_name)
    except Exception:
        _log.debug("worktree cleanup failed for %s", worker_name, exc_info=True)


async def launch_workers(
    pool: WorkerProcessProvider,
    worker_configs: list[WorkerConfig],
    stagger_seconds: float = 2.0,
    default_provider: str = "claude",
    *,
    write_identity: IdentityWriter,
) -> list[Worker]:
    """Spawn all workers via the pool and return Worker objects.

    Each worker gets its own provider-specific command based on its
    ``provider`` config (or the *default_provider* fallback).

    ``write_identity`` is required (#1195) — see :data:`IdentityWriter`. Each
    worker's identity file is written before ITS process starts, inside the
    loop, so a failure partway through still leaves every already-started
    worker correctly identified.
    """
    launched: list[Worker] = []
    for i, wc in enumerate(worker_configs):
        prov_name = _resolve_provider_name(wc, default_provider)
        prov = get_provider(prov_name)
        spawn_path, repo_path, wt_branch = await _resolve_worktree(wc)
        # BEFORE pool.spawn: a session reads .mcp.json at STARTUP, so a write
        # that lands after the process is up does not reach it (#1187/#1195).
        write_identity(wc, spawn_path)
        try:
            cmd = prov.worker_command()
            proc = await pool.spawn(wc.name, spawn_path, command=cmd, shell_wrap=True)
        except (ProcessError, OSError) as exc:
            _log.error("spawn failed for worker '%s': %s", wc.name, exc)
            if repo_path:
                await _cleanup_worktree(repo_path, wc.name)
            # Kill already-launched workers to avoid orphans
            for w in launched:
                try:
                    await pool.kill(w.name)
                except (ProcessError, OSError):
                    _log.debug("cleanup kill failed for %s", w.name)
            raise
        worker = Worker(
            name=wc.name,
            path=spawn_path,
            provider_name=prov_name,
            process=proc,
            repo_path=repo_path,
            worktree_branch=wt_branch,
        )
        launched.append(worker)
        if i < len(worker_configs) - 1 and stagger_seconds > 0:
            await asyncio.sleep(stagger_seconds)

    return launched


async def revive_worker(
    worker: Worker,
    pool: WorkerProcessProvider,
) -> None:
    """Revive a stung (exited) worker by respawning via the pool."""
    if worker.state not in (WorkerState.STUNG,):
        _log.warning(
            "refusing to revive %s — state is %s, not STUNG",
            worker.name,
            worker.state.value,
        )
        return
    prov = get_provider(worker.provider_name)
    cmd = prov.worker_command()
    try:
        new_proc = await pool.revive(worker.name, cwd=worker.path, command=cmd, shell_wrap=True)
        if new_proc:
            worker.process = new_proc
            worker.record_revive()
            worker.update_state(WorkerState.BUZZING)
            _log.info("revived %s (pid=%d)", worker.name, new_proc.pid)
        else:
            _log.warning("cannot revive %s — not found in pool", worker.name)
    except ProcessError as e:
        _log.warning("revive failed for %s: %s", worker.name, e)


async def add_worker_live(
    pool: WorkerProcessProvider,
    worker_config: WorkerConfig,
    workers: list[Worker],
    auto_start: bool = False,
    default_provider: str = "claude",
    kind: str = "worker",
    resume: bool = False,
    *,
    write_identity: IdentityWriter,
) -> Worker:
    """Add a new worker to a running swarm.

    Spawns a new process via the pool. When *auto_start* is ``True``,
    launches the provider's interactive command immediately.

    When *kind* is ``"queen"`` the resulting :class:`Worker` is marked
    as the swarm's coordinator — task assignment and SLEEPING are
    skipped for her.  *resume* forwards to the provider's
    ``worker_command`` (the Queen resumes her prior session).

    ``write_identity`` is required (#1195) — see :data:`IdentityWriter`. This
    function and :func:`launch_workers` are the only two places that call
    ``pool.spawn`` for a worker, so requiring it here is what makes "a live
    worker has an identity file naming IT" true by construction rather than by
    convention.
    """
    prov_name = _resolve_provider_name(worker_config, default_provider)
    prov = get_provider(prov_name)
    spawn_path, repo_path, wt_branch = await _resolve_worktree(worker_config)
    # BEFORE pool.spawn — see the note in launch_workers. Also AFTER
    # _resolve_worktree, so an isolated worker's file lands in the worktree the
    # session actually starts in rather than the configured repo path.
    write_identity(worker_config, spawn_path)
    if auto_start:
        command = prov.worker_command(resume=resume)
    else:
        command = ["bash"]
    try:
        proc = await pool.spawn(
            worker_config.name,
            spawn_path,
            command=command,
            shell_wrap=auto_start,
        )
    except (ProcessError, OSError):
        if repo_path:
            await _cleanup_worktree(repo_path, worker_config.name)
        raise

    initial_state = WorkerState.BUZZING if auto_start else WorkerState.RESTING
    worker = Worker(
        name=worker_config.name,
        path=spawn_path,
        provider_name=prov_name,
        kind=kind,
        process=proc,
        state=initial_state,
        repo_path=repo_path,
        worktree_branch=wt_branch,
    )
    workers.append(worker)
    _log.info(
        "live-added %s %s at %s (pid=%d)",
        kind,
        worker_config.name,
        spawn_path,
        proc.pid,
    )
    return worker


async def kill_worker(worker: Worker, pool: WorkerProcessProvider | None) -> None:
    """Kill a specific worker.

    Tolerates a missing pool. A kill that raises leaves the caller half-done —
    and the operator-kill path has already removed the worker from the roster
    by this point, so an exception here would strand a live process with no
    entry in the UI to kill it from.
    """
    if pool is None:
        _log.warning("kill for %s: no process pool — nothing to signal", worker.name)
        return
    try:
        await pool.kill(worker.name)
    except (ProcessError, OSError):
        _log.info("kill failed for %s (process may have already exited)", worker.name)
