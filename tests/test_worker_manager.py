"""Tests for worker/manager.py — pool-based worker lifecycle."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.config import WorkerConfig
from swarm.pty.process import ProcessError
from swarm.worker.manager import add_worker_live, kill_worker, launch_workers, revive_worker
from swarm.worker.worker import Worker, WorkerState
from tests.fakes.process import FakeWorkerProcess


# #1195: the spawn helpers REQUIRE an identity writer. These tests exercise
# process wiring, not identity, so they pass an explicit no-op — the whole
# point of the required argument is that skipping it has to be a deliberate,
# visible act rather than an omission nobody notices.
def _NO_IDENTITY(wc, spawn_path):
    return None


def _make_fake_pool(workers_dict: dict | None = None):
    """Create a mock ProcessPool that returns FakeWorkerProcess instances."""
    pool = AsyncMock()
    spawned = workers_dict if workers_dict is not None else {}

    async def fake_spawn(name, cwd, command=None, cols=200, rows=50, shell_wrap=False):
        proc = FakeWorkerProcess(name=name, cwd=cwd)
        proc.pid = 1000 + len(spawned)
        spawned[name] = proc
        return proc

    async def fake_spawn_batch(worker_list, command=None, stagger_seconds=2.0):
        result = []
        for name, cwd in worker_list:
            proc = await fake_spawn(name, cwd, command=command)
            result.append(proc)
        return result

    async def fake_kill(name):
        if name in spawned:
            spawned[name]._alive = False
            del spawned[name]

    async def fake_revive(name, cwd=None, command=None, shell_wrap=False):
        if name in spawned:
            old = spawned[name]
            old._alive = False
            cwd = cwd or old.cwd
            proc = FakeWorkerProcess(name=name, cwd=cwd)
            proc.pid = 2000 + len(spawned)
            spawned[name] = proc
            return proc
        if cwd:
            proc = FakeWorkerProcess(name=name, cwd=cwd)
            proc.pid = 2000 + len(spawned)
            spawned[name] = proc
            return proc
        return None

    pool.spawn = AsyncMock(side_effect=fake_spawn)
    pool.spawn_batch = AsyncMock(side_effect=fake_spawn_batch)
    pool.kill = AsyncMock(side_effect=fake_kill)
    pool.revive = AsyncMock(side_effect=fake_revive)
    pool.get = MagicMock(side_effect=lambda name: spawned.get(name))
    return pool


@pytest.mark.asyncio
async def test_launch_workers_spawns_all():
    pool = _make_fake_pool()
    configs = [
        WorkerConfig(name="api", path="/tmp/api"),
        WorkerConfig(name="web", path="/tmp/web"),
    ]
    result = await launch_workers(pool, configs, stagger_seconds=0, write_identity=_NO_IDENTITY)

    assert pool.spawn.call_count == 2
    assert len(result) == 2
    assert result[0].name == "api"
    assert result[1].name == "web"
    assert result[0].process is not None
    assert result[1].process is not None


@pytest.mark.asyncio
async def test_launch_workers_sets_initial_state():
    pool = _make_fake_pool()
    configs = [WorkerConfig(name="api", path="/tmp/api")]
    result = await launch_workers(pool, configs, stagger_seconds=0, write_identity=_NO_IDENTITY)

    assert result[0].state == WorkerState.BUZZING
    assert result[0].provider_name == "claude"


@pytest.mark.asyncio
async def test_launch_workers_passes_paths():
    pool = _make_fake_pool()
    configs = [WorkerConfig(name="api", path="/tmp/api")]
    result = await launch_workers(pool, configs, stagger_seconds=0, write_identity=_NO_IDENTITY)

    assert result[0].path == "/tmp/api"
    assert result[0].process.cwd == "/tmp/api"


@pytest.mark.asyncio
async def test_launch_workers_per_worker_provider():
    """Workers with different providers get different commands and provider_names."""
    pool = _make_fake_pool()
    configs = [
        WorkerConfig(name="claude-worker", path="/tmp/c", provider="claude"),
        WorkerConfig(name="gemini-worker", path="/tmp/g", provider="gemini"),
    ]
    result = await launch_workers(pool, configs, stagger_seconds=0, write_identity=_NO_IDENTITY)

    assert result[0].provider_name == "claude"
    assert result[1].provider_name == "gemini"
    # Verify spawn was called with different commands
    calls = pool.spawn.call_args_list
    assert calls[0].args[0] == "claude-worker"
    assert calls[1].args[0] == "gemini-worker"


@pytest.mark.asyncio
async def test_launch_workers_inherits_default():
    """Workers with no explicit provider inherit the passed default."""
    pool = _make_fake_pool()
    configs = [WorkerConfig(name="api", path="/tmp/api")]
    result = await launch_workers(
        pool, configs, stagger_seconds=0, default_provider="gemini", write_identity=_NO_IDENTITY
    )

    assert result[0].provider_name == "gemini"


@pytest.mark.asyncio
async def test_revive_worker_success():
    spawned = {}
    pool = _make_fake_pool(spawned)
    fake_proc = FakeWorkerProcess(name="api", cwd="/tmp/api")
    fake_proc.pid = 1000
    spawned["api"] = fake_proc

    worker = Worker(name="api", path="/tmp/api", process=fake_proc, state=WorkerState.STUNG)
    # Force past hysteresis
    worker._stung_confirmations = 2

    await revive_worker(worker, pool)

    pool.revive.assert_called_once_with(
        "api",
        cwd="/tmp/api",
        command=["claude", "--continue"],
        shell_wrap=True,
    )
    assert worker.process is not None
    assert worker.process.pid == 2001


@pytest.mark.asyncio
async def test_revive_worker_not_in_pool():
    """Revive succeeds even when the worker was removed from the pool (kill before revive)."""
    pool = _make_fake_pool()
    fake_proc = FakeWorkerProcess(name="ghost", cwd="/tmp")
    worker = Worker(name="ghost", path="/tmp", process=fake_proc, state=WorkerState.STUNG)

    await revive_worker(worker, pool)

    # Should spawn a new process using worker.path as cwd
    assert worker.process is not fake_proc
    assert worker.process.name == "ghost"
    assert worker.state == WorkerState.BUZZING


@pytest.mark.asyncio
async def test_revive_worker_handles_error():
    pool = AsyncMock()
    pool.revive = AsyncMock(side_effect=ProcessError("holder dead"))

    fake_proc = FakeWorkerProcess(name="api", cwd="/tmp")
    worker = Worker(name="api", path="/tmp", process=fake_proc, state=WorkerState.STUNG)

    # Should not raise
    await revive_worker(worker, pool)


@pytest.mark.asyncio
async def test_add_worker_live_with_auto_start():
    pool = _make_fake_pool()
    workers: list[Worker] = []
    config = WorkerConfig(name="api", path="/tmp/api")

    worker = await add_worker_live(
        pool, config, workers, auto_start=True, write_identity=_NO_IDENTITY
    )

    assert worker.name == "api"
    assert worker.state == WorkerState.BUZZING
    assert worker.provider_name == "claude"
    assert worker.process is not None
    assert len(workers) == 1
    # New workers should NOT use --continue/--resume (no session to resume)
    pool.spawn.assert_called_once_with(
        "api",
        "/tmp/api",
        command=["claude"],
        shell_wrap=True,
    )


@pytest.mark.asyncio
async def test_add_worker_live_without_auto_start():
    pool = _make_fake_pool()
    workers: list[Worker] = []
    config = WorkerConfig(name="api", path="/tmp/api")

    worker = await add_worker_live(
        pool, config, workers, auto_start=False, write_identity=_NO_IDENTITY
    )

    assert worker.state == WorkerState.RESTING
    assert worker.provider_name == "claude"
    pool.spawn.assert_called_once_with("api", "/tmp/api", command=["bash"], shell_wrap=False)


@pytest.mark.asyncio
async def test_add_worker_live_inherits_default_provider():
    """add_worker_live should use default_provider when worker has no explicit provider."""
    pool = _make_fake_pool()
    workers: list[Worker] = []
    config = WorkerConfig(name="api", path="/tmp/api")

    worker = await add_worker_live(
        pool,
        config,
        workers,
        auto_start=True,
        default_provider="gemini",
        write_identity=_NO_IDENTITY,
    )

    assert worker.provider_name == "gemini"


@pytest.mark.asyncio
async def test_kill_worker_calls_pool():
    pool = _make_fake_pool()
    fake_proc = FakeWorkerProcess(name="api", cwd="/tmp")
    worker = Worker(name="api", path="/tmp", process=fake_proc)

    await kill_worker(worker, pool)

    pool.kill.assert_called_once_with("api")


@pytest.mark.asyncio
async def test_kill_worker_ignores_error():
    pool = AsyncMock()
    pool.kill = AsyncMock(side_effect=ProcessError("already dead"))

    fake_proc = FakeWorkerProcess(name="api", cwd="/tmp")
    worker = Worker(name="api", path="/tmp", process=fake_proc)

    # Should not raise
    await kill_worker(worker, pool)


@pytest.mark.asyncio
async def test_kill_worker_logs_at_info_when_process_gone(caplog):
    """Kill failure should log at INFO level so operators see it in normal logs."""
    import logging

    pool = AsyncMock()
    pool.kill = AsyncMock(side_effect=ProcessError("already dead"))

    fake_proc = FakeWorkerProcess(name="api", cwd="/tmp")
    worker = Worker(name="api", path="/tmp", process=fake_proc)

    with caplog.at_level(logging.DEBUG, logger="swarm.worker.manager"):
        await kill_worker(worker, pool)

    kill_logs = [r for r in caplog.records if "kill" in r.message.lower() and "api" in r.message]
    assert kill_logs, "Expected a log message about kill failure"
    assert kill_logs[0].levelno == logging.INFO, (
        f"Expected INFO level, got {kill_logs[0].levelname}"
    )


@pytest.mark.asyncio
async def test_launch_workers_cleans_up_worktree_on_spawn_failure(monkeypatch):
    """When spawn fails and a worktree was created, it should be cleaned up."""
    pool = AsyncMock()
    pool.spawn = AsyncMock(side_effect=ProcessError("holder dead"))

    cleanup_calls: list[str] = []

    async def fake_cleanup(repo_path: str, worker_name: str) -> None:
        cleanup_calls.append(worker_name)

    monkeypatch.setattr("swarm.worker.manager._cleanup_worktree", fake_cleanup)

    # Simulate worktree isolation
    async def fake_resolve(wc):
        return str(wc.resolved_path), "/original/repo", "swarm/test"

    monkeypatch.setattr("swarm.worker.manager._resolve_worktree", fake_resolve)

    configs = [WorkerConfig(name="api", path="/tmp/api")]
    with pytest.raises(ProcessError):
        await launch_workers(pool, configs, stagger_seconds=0, write_identity=_NO_IDENTITY)

    assert "api" in cleanup_calls
