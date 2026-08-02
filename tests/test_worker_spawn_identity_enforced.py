"""#1195 — the identity write is enforced by CONSTRUCTION, not by a docstring.

#1187 put ``ensure_worker_identity`` in ``daemon.spawn_worker`` and held the
rest of the invariant with a comment on ``WorkerService.spawn`` saying "NOT the
public entry point". That is a convention, and a convention is precisely what
the original bug was: ``_write_worker_mcp_configs`` existed, worked, and simply
was not called. The next contributor reads the code, not the docstring.

CHOKEPOINT — why ``manager.add_worker_live`` / ``manager.launch_workers`` and
NOT ``WorkerService.spawn``:

``pool.spawn`` is called for a worker in exactly two places, both in
``swarm/worker/manager.py`` (lines 84 and 165). Every production path that
brings a worker to life funnels through one of them:

    WorkerService.spawn            -> add_worker_live
    WorkerService.launch (resume)  -> add_worker_live
    WorkerService.launch (fresh)   -> launch_workers
    queen.runtime.ensure_queen_running -> add_worker_live

``WorkerService.spawn`` is only the FIRST of those four. Injecting into
WorkerService would have left the Queen and both launch branches routing around
the guarantee — a smaller door than the task assumed. ``add_worker_live`` is
also the only place that knows ``spawn_path``, the directory the session is
actually started in, which differs from the configured path under
``isolation: worktree``.

So both manager functions take a REQUIRED keyword-only ``write_identity``:
there is no way to spawn a worker without supplying one, and the failure is a
TypeError at the call site rather than a silently identity-less worker.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.config import WorkerConfig
from swarm.worker.manager import add_worker_live, launch_workers


def _make_fake_pool() -> MagicMock:
    pool = MagicMock()
    proc = MagicMock()
    proc.pid = 4242
    proc.name = "api"
    pool.spawn = AsyncMock(return_value=proc)
    return pool


def _identity_of(worker_dir: Path) -> str:
    payload = json.loads((worker_dir / ".mcp.json").read_text())
    return payload["mcpServers"]["swarm"]["url"].split("worker=", 1)[1].split("&", 1)[0]


def _real_writer(port: int = 9090):
    """A writer with the shape the daemon injects — name into its own file."""

    def _write(wc: WorkerConfig, spawn_path: str) -> None:
        Path(spawn_path).mkdir(parents=True, exist_ok=True)
        (Path(spawn_path) / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "swarm": {
                            "type": "http",
                            "url": f"http://localhost:{port}/mcp?worker={wc.name}",
                        }
                    }
                }
            )
        )

    return _write


# --- the structural guarantee ------------------------------------------


@pytest.mark.parametrize("fn", [add_worker_live, launch_workers])
def test_spawn_functions_require_an_identity_writer(fn) -> None:
    """AC-1. ``write_identity`` is keyword-only with NO default, so omitting it
    is a TypeError rather than a worker that quietly inherits an identity.

    Delete the parameter (or give it a default) and this test fails — which is
    the point: the enforcement has to be the thing under test, not a comment
    somebody can read past.
    """
    param = inspect.signature(fn).parameters.get("write_identity")
    assert param is not None, f"{fn.__name__} lost its identity-writer parameter"
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty, (
        "a default makes the guarantee optional again — that is the #1187 shape"
    )


@pytest.mark.asyncio
async def test_add_worker_live_without_a_writer_raises(tmp_path) -> None:
    """The way a future contributor plausibly writes a new spawn route: call
    the spawn helper the way the old signature allowed. It must not work."""
    with pytest.raises(TypeError, match="write_identity"):
        await add_worker_live(
            _make_fake_pool(),
            WorkerConfig(name="api", path=str(tmp_path)),
            [],
            auto_start=True,
        )


@pytest.mark.asyncio
async def test_launch_workers_without_a_writer_raises(tmp_path) -> None:
    with pytest.raises(TypeError, match="write_identity"):
        await launch_workers(
            _make_fake_pool(),
            [WorkerConfig(name="api", path=str(tmp_path))],
        )


# --- ordering, which is the load-bearing half --------------------------


@pytest.mark.asyncio
async def test_identity_exists_before_the_process_starts(tmp_path) -> None:
    """AC-3. A session reads .mcp.json at STARTUP, so the file must be on disk
    at the MOMENT pool.spawn is called — not merely present afterwards. Recorded
    from inside the spawn call, because a post-hoc existence check passes on the
    broken ordering that #1187 had to be respawned to work around."""
    worker_dir = tmp_path / "api"
    worker_dir.mkdir()
    pool = _make_fake_pool()
    seen: dict[str, object] = {}

    async def _spawn(name, cwd, **kwargs):
        seen["existed_at_spawn"] = (Path(cwd) / ".mcp.json").is_file()
        if seen["existed_at_spawn"]:
            seen["identity_at_spawn"] = _identity_of(Path(cwd))
        proc = MagicMock()
        proc.pid = 1
        return proc

    pool.spawn = AsyncMock(side_effect=_spawn)

    await add_worker_live(
        pool,
        WorkerConfig(name="api", path=str(worker_dir)),
        [],
        auto_start=True,
        write_identity=_real_writer(),
    )

    assert seen.get("existed_at_spawn") is True, (
        "identity file was not on disk when the process started — the session "
        "would inherit a parent directory's identity until respawned"
    )
    # AC-4: content, not existence. An inherited file exists too.
    assert seen.get("identity_at_spawn") == "api"


@pytest.mark.asyncio
async def test_launch_workers_writes_identity_for_every_worker(tmp_path) -> None:
    """The batch path is a spawn path too — ``POST /api/workers/launch`` reaches
    it without ever passing through ``daemon.spawn_worker``."""
    dirs = {}
    for name in ("alpha", "beta"):
        d = tmp_path / name
        d.mkdir()
        dirs[name] = d
    pool = _make_fake_pool()

    await launch_workers(
        pool,
        [WorkerConfig(name=n, path=str(p)) for n, p in dirs.items()],
        stagger_seconds=0.0,
        write_identity=_real_writer(),
    )

    for name, d in dirs.items():
        assert _identity_of(d) == name


@pytest.mark.asyncio
async def test_writer_receives_the_worktree_spawn_path_not_the_repo_path(
    tmp_path, monkeypatch
) -> None:
    """Under ``isolation: worktree`` the session starts in the worktree, not the
    configured repo path. ``add_worker_live`` is the only layer that knows the
    difference, which is a second reason the guarantee belongs here.

    Today the worktree nests INSIDE the repo, so an inherited file would still
    resolve to the right name — but that is a property of the layout, not a
    guarantee, and the writer should not depend on it.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree = repo / ".swarm" / "worktrees" / "api"
    worktree.mkdir(parents=True)
    seen: dict[str, str] = {}

    async def _fake_resolve(wc):
        return str(worktree), str(repo), "swarm/api"

    monkeypatch.setattr("swarm.worker.manager._resolve_worktree", _fake_resolve)

    def _record(wc: WorkerConfig, spawn_path: str) -> None:
        seen["path"] = spawn_path

    await add_worker_live(
        _make_fake_pool(),
        WorkerConfig(name="api", path=str(repo)),
        [],
        auto_start=True,
        write_identity=_record,
    )

    assert seen["path"] == str(worktree)


# --- the service that used to hold the invariant in prose --------------


def test_worker_service_requires_a_writer_to_be_constructed() -> None:
    """AC-1 at the service layer: the dependency is a required constructor
    argument, so a future WorkerService cannot be wired without one."""
    from swarm.server.worker_service import WorkerService

    param = inspect.signature(WorkerService.__init__).parameters.get("write_identity")
    assert param is not None, "WorkerService no longer takes an identity writer"
    assert param.default is inspect.Parameter.empty


def test_worker_service_spawn_docstring_states_a_guarantee() -> None:
    """AC-6. The docstring must describe what the code enforces, not ask the
    caller to please use a different entry point."""
    from swarm.server.worker_service import WorkerService

    doc = inspect.getdoc(WorkerService.spawn) or ""
    assert "NOT the public entry point" not in doc, "still phrased as a request"
    assert "write_identity" in doc or "guarantee" in doc.lower()
