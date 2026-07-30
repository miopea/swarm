"""#1045 — caller identity at the MCP boundary, and the task-mutation
defects it masked.

The identity a worker calls MCP tools with is a free-form string in the
MCP URL (``?worker=<name>``), written into each worker's ``.mcp.json``
by the daemon. Nothing downstream re-checked it and every ownership
guard is an exact string comparison, so a stale or wrong-cased file
silently turned a legitimate worker into an identity that owns nothing.

Observed: rcg-platform's ``.mcp.json`` said ``worker=Platform`` while
the board stores ``platform``. Every close of a PRE-ASSIGNED task was
rejected, while the UNASSIGNED self-close path — which compares nothing
— accepted the bogus name and stamped it onto the board (#1044 was the
only task among 1000+ owned by ``Platform``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarm.config.models import WorkerConfig
from swarm.mcp.server import _resolve_worker_identity
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import TaskStatus
from tests.conftest import make_daemon
from tests.fakes.process import FakeWorkerProcess


def _registry(*names: str, configured: tuple[str, ...] = ()) -> SimpleNamespace:
    """Minimal daemon stand-in exposing a worker registry."""
    return SimpleNamespace(
        workers=[SimpleNamespace(name=n) for n in names],
        config=SimpleNamespace(
            workers=[WorkerConfig(name=n, path=f"/tmp/{n}") for n in configured]
        ),
    )


# --- identity canonicalisation -----------------------------------------


def test_identity_exact_match_passes_through() -> None:
    assert _resolve_worker_identity(_registry("platform", "swarm"), "platform") == "platform"


def test_identity_canonicalises_case() -> None:
    """The #1045 root case: ``.mcp.json`` says ``Platform``, board says
    ``platform``. Resolve to the REGISTRY spelling so every downstream
    comparison stays an exact match."""
    assert _resolve_worker_identity(_registry("platform"), "Platform") == "platform"


def test_identity_strips_whitespace() -> None:
    assert _resolve_worker_identity(_registry("platform"), "  platform \n") == "platform"


def test_identity_unregistered_becomes_unknown() -> None:
    """An identity that matches no worker can never legitimately own a
    task. Report it as ``unknown`` so the handlers' fail-fast diagnostic
    names the real problem (the MCP URL) instead of blaming assignment."""
    assert _resolve_worker_identity(_registry("platform"), "ghost-worker") == "unknown"


def test_identity_empty_becomes_unknown() -> None:
    assert _resolve_worker_identity(_registry("platform"), "") == "unknown"
    assert _resolve_worker_identity(_registry("platform"), "   ") == "unknown"


def test_identity_queen_is_reserved() -> None:
    """The Queen reaches the same endpoint from her own workdir and is
    only in ``workers`` while her PTY happens to be running."""
    assert _resolve_worker_identity(_registry("platform"), "queen") == "queen"
    assert _resolve_worker_identity(_registry("platform"), "Queen") == "queen"


def test_identity_resolves_from_config_when_pty_not_running() -> None:
    """A configured worker whose PTY is down still has a valid identity."""
    reg = _registry("platform", configured=("sculpt-studio",))
    assert _resolve_worker_identity(reg, "sculpt-studio") == "sculpt-studio"


# --- .mcp.json writing --------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_config_written_for_tilde_path_worker(monkeypatch, tmp_path) -> None:
    """``Path("~/proj").is_dir()`` is False, so every tilde-path worker
    was silently skipped and never got — or had corrected — its identity
    file. 8 of 24 workers on the reporting box, ``platform`` among them.
    """
    from swarm.worker.worker import Worker

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("swarm.auth.mcp_token.get_or_create_mcp_token", lambda: "tok")

    wdir = tmp_path / "proj"
    wdir.mkdir()

    d = make_daemon(monkeypatch)
    d.workers = [Worker(name="platform", path="~/proj", process=FakeWorkerProcess(name="platform"))]
    d.config.port = 9090

    d._write_worker_mcp_configs()

    written = (wdir / ".mcp.json").read_text()
    assert "?worker=platform" in written


# --- the ownership guard stays strict ----------------------------------


def test_complete_task_still_rejects_another_workers_task(monkeypatch) -> None:
    """The guard is fixed, not removed: a worker may not close a task
    genuinely assigned to someone else."""
    from swarm.mcp.tools import handle_tool_call

    d = make_daemon(monkeypatch)
    d.task_board = TaskBoard()
    t = d.task_board.create(title="platform work")
    d.task_board.assign(t.id, "platform")
    d.task_board.activate(t.id)

    args = {"number": t.number, "resolution": "x"}
    out = handle_tool_call(d, "swarm", "swarm_complete_task", args)
    text = str(out)
    assert "not assigned to you" in text
    assert d.task_board.get(t.id).status != TaskStatus.DONE


# --- assignment must not silently revert -------------------------------


@pytest.mark.asyncio
async def test_send_failure_keeps_explicitly_targeted_assignment(monkeypatch) -> None:
    """A task dispatched to a NAMED worker is worker-specific by
    construction. Rolling the assignment back to unassigned on a PTY
    send failure discards that routing decision silently — the caller
    still sees success (#1039, #1044, #1045, #1048, #980 twice)."""
    from swarm.pty.process import ProcessError

    d = make_daemon(monkeypatch)
    d.task_board = TaskBoard()
    t = d.task_board.create(title="targeted work")
    t.target_worker = "api"
    d.task_board.assign(t.id, "api")

    async def _boom(*a, **kw):
        raise ProcessError("not connected")

    monkeypatch.setattr(d, "send_to_worker", _boom)

    ok = await d.tasks_coord.start_task(t.id, actor="queen")

    assert ok is False
    got = d.task_board.get(t.id)
    assert got.assigned_worker == "api", "assignment must survive a delivery failure"
    assert got.status == TaskStatus.ASSIGNED


# --- park must persist --------------------------------------------------


def test_parked_task_is_not_immediately_restarted(monkeypatch) -> None:
    """#1015: park moves ACTIVE → ASSIGNED and keeps the owner, so the
    momentum machinery picked the very same task straight back up and
    re-activated it — the worker's set-down never survived a cycle."""
    d = make_daemon(monkeypatch)
    d.task_board = TaskBoard()
    t = d.task_board.create(title="parked work")
    d.task_board.assign(t.id, "api")
    d.task_board.activate(t.id)

    assert d.task_board.park(t.id, "api", "operator preempt") is True

    started: list[str] = []

    async def _start(task_id, actor="user", message=None):
        started.append(task_id)
        return True

    monkeypatch.setattr(d, "start_task", _start)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_drain(d, "api"))
    finally:
        loop.close()

    assert started == [], "a parked task must not be auto-restarted"
    assert d.task_board.get(t.id).status == TaskStatus.ASSIGNED


async def _drain(d, worker_name: str) -> None:
    d.tasks_coord.auto_start_next_assigned(worker_name)
    await asyncio.sleep(0)


def test_parked_task_resumes_when_explicitly_activated(monkeypatch) -> None:
    """Suppression is not a one-way door — the normal re-dispatch
    chokepoint clears it so the worker can pick the task back up."""
    d = make_daemon(monkeypatch)
    d.task_board = TaskBoard()
    t = d.task_board.create(title="parked work")
    d.task_board.assign(t.id, "api")
    d.task_board.activate(t.id)
    d.task_board.park(t.id, "api", "set down")

    assert d.task_board.activate(t.id) is not None
    got = d.task_board.get(t.id)
    assert got.status == TaskStatus.ACTIVE
    assert not got.is_on_hold


# --- #1055: every CONFIGURED worker needs an identity file ---------------


@pytest.mark.asyncio
async def test_mcp_config_written_for_configured_but_not_running_worker(
    monkeypatch, tmp_path
) -> None:
    """#1055: ``daemon.workers`` holds LIVE PTY processes (built by
    ``worker_service.discover`` from ``pool.discover()``), not configured
    workers. ``_write_worker_mcp_configs`` iterated it, so a worker whose
    PTY happened to be down at daemon start got no identity file — and
    nothing writes one later, since the writer only runs at startup.

    That is how sculpt-studio and aria ended up with no ``.mcp.json`` at
    all despite valid, existing absolute paths. Their sessions then
    inherit the PARENT directory's config and transmit ``project-root``.

    #1045's canonicalisation cannot rescue this: ``project-root`` is a
    real registered worker, so the wrong identity resolves cleanly and
    the failure stays silent. Every configured worker must get its own
    file.
    """
    from swarm.config.models import WorkerConfig

    monkeypatch.setattr("swarm.auth.mcp_token.get_or_create_mcp_token", lambda: "tok")

    running_dir = tmp_path / "running"
    running_dir.mkdir()
    idle_dir = tmp_path / "sculpt-studio"
    idle_dir.mkdir()

    d = make_daemon(monkeypatch)
    d.config.port = 9090
    # Configured: both. Running (in d.workers): only one.
    d.config.workers = [
        WorkerConfig(name="running-one", path=str(running_dir)),
        WorkerConfig(name="sculpt-studio", path=str(idle_dir)),
    ]
    from swarm.worker.worker import Worker

    d.workers = [
        Worker(
            name="running-one",
            path=str(running_dir),
            process=FakeWorkerProcess(name="running-one"),
        )
    ]

    d._write_worker_mcp_configs()

    assert "?worker=running-one" in (running_dir / ".mcp.json").read_text()
    written = idle_dir / ".mcp.json"
    assert written.exists(), "a configured worker must get an identity file even when not running"
    assert "?worker=sculpt-studio" in written.read_text()
