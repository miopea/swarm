"""#1187 — a worker created after boot must get its OWN identity file.

``_write_worker_mcp_configs`` had exactly one call site: the startup sweep in
``daemon.start()``. Nothing on the create path invoked it, so a worker created
via ``POST /api/config/workers`` (which is what the dashboard's Add Worker
button posts to) came up live with NO ``.mcp.json`` in its workdir. Claude Code
then walks up the tree and loads the nearest parent's file — for anything under
``~/projects/*`` that is ``~/projects/.mcp.json``, carrying
``?worker=project-root``.

Every ownership guard is an exact comparison against the canonicalised name, so
the new worker *is* project-root as far as the board is concerned: it can read
and close project-root's tasks.

This is #1055's failure mode in its NON-RECOVERABLE form. Wrong-CASE is rescued
by the server's case-insensitive canonicalisation; wrong-IDENTITY canonicalises
cleanly to a legitimately different registered worker and nothing detects it.
Which is why these tests assert on the file's CONTENT — a mere-existence check
passes on an inherited file and would have missed the entire bug.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from swarm.worker.worker import Worker
from tests.fakes.process import FakeWorkerProcess

# Reuse test_api's already-wired daemon + authenticated client rather than
# re-deriving ~120 lines of service wiring. Importing a fixture and then naming
# it as a test parameter is the standard pytest pattern that ruff reads as a
# redefinition, so F811 is disabled for this file in pyproject.toml.
# ``pytest_plugins`` does not work here: test_api is itself a collected test
# module, so registering it as a plugin leaves the fixtures unresolvable once
# the full suite runs — it passed in isolation and errored in the suite.
from tests.test_api import (  # noqa: F401
    _AUTH_HEADERS,
    config_client,
    daemon,
    daemon_with_path,
)


def _identity_of(worker_dir) -> str:
    """The ``?worker=`` value the workdir's own .mcp.json transmits."""
    payload = json.loads((worker_dir / ".mcp.json").read_text())
    url = payload["mcpServers"]["swarm"]["url"]
    return url.split("worker=", 1)[1].split("&", 1)[0]


@pytest.mark.asyncio
async def test_created_worker_gets_an_identity_file_naming_itself(config_client, tmp_path):
    """AC-1/AC-2. The file must exist in the worker's OWN workdir and name the
    worker that was just created."""
    worker_dir = tmp_path / "renovate-config"
    worker_dir.mkdir()

    with patch("swarm.worker.manager.add_worker_live", new_callable=AsyncMock) as mock_add:
        mock_add.return_value = Worker(
            name="renovate-config",
            path=str(worker_dir),
            process=FakeWorkerProcess(name="renovate-config"),
        )
        resp = await config_client.post(
            "/api/config/workers",
            json={"name": "renovate-config", "path": str(worker_dir)},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 201

    assert (worker_dir / ".mcp.json").is_file(), "no identity file — the worker inherits a parent's"
    assert _identity_of(worker_dir) == "renovate-config"


@pytest.mark.asyncio
async def test_identity_file_is_written_before_the_session_starts(config_client, tmp_path):
    """A session reads .mcp.json at STARTUP. Writing it after the process is up
    does not reach that session — the reporter had to respawn by hand. So the
    write must be ordered BEFORE spawn, not merely happen eventually."""
    worker_dir = tmp_path / "ordering"
    worker_dir.mkdir()
    seen: dict[str, bool] = {}

    async def _record_then_spawn(*args, **kwargs):
        seen["identity_existed_at_spawn"] = (worker_dir / ".mcp.json").is_file()
        return Worker(
            name="ordering", path=str(worker_dir), process=FakeWorkerProcess(name="ordering")
        )

    with patch("swarm.worker.manager.add_worker_live", new=_record_then_spawn):
        resp = await config_client.post(
            "/api/config/workers",
            json={"name": "ordering", "path": str(worker_dir)},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 201

    assert seen.get("identity_existed_at_spawn") is True, (
        "identity file written after the PTY started — the session already "
        "inherited a parent's file and only a respawn would fix it"
    )


@pytest.mark.asyncio
async def test_no_startup_sweep_runs_in_this_test(config_client, tmp_path):
    """AC-3. Guards the test itself: ``daemon.start()`` is never called here, so
    a pass cannot be coming from the startup sweep that already worked."""
    worker_dir = tmp_path / "post-boot"
    worker_dir.mkdir()
    d = config_client.app["daemon"]

    with patch.object(d, "_write_worker_mcp_configs") as sweep:
        with patch("swarm.worker.manager.add_worker_live", new_callable=AsyncMock) as mock_add:
            mock_add.return_value = Worker(
                name="post-boot",
                path=str(worker_dir),
                process=FakeWorkerProcess(name="post-boot"),
            )
            resp = await config_client.post(
                "/api/config/workers",
                json={"name": "post-boot", "path": str(worker_dir)},
                headers=_AUTH_HEADERS,
            )
            assert resp.status == 201
        sweep.assert_not_called()

    assert _identity_of(worker_dir) == "post-boot"


@pytest.mark.asyncio
async def test_identity_token_comes_from_the_token_helper(config_client, tmp_path):
    """AC-7. The bearer must be minted by ``get_or_create_mcp_token`` — never
    copied out of a sibling worker's file. Asserting it MATCHES the helper is
    what rules the copy-a-sibling shortcut out."""
    from swarm.auth.mcp_token import get_or_create_mcp_token

    worker_dir = tmp_path / "tokened"
    worker_dir.mkdir()

    with patch("swarm.worker.manager.add_worker_live", new_callable=AsyncMock) as mock_add:
        mock_add.return_value = Worker(
            name="tokened", path=str(worker_dir), process=FakeWorkerProcess(name="tokened")
        )
        resp = await config_client.post(
            "/api/config/workers",
            json={"name": "tokened", "path": str(worker_dir)},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 201

    payload = json.loads((worker_dir / ".mcp.json").read_text())
    auth = payload["mcpServers"]["swarm"]["headers"]["Authorization"]
    assert auth == f"Bearer {get_or_create_mcp_token()}"


@pytest.mark.asyncio
async def test_spawn_route_also_writes_identity(config_client, tmp_path):
    """The invariant is 'a live worker has an identity file naming IT', so it
    must hold for every spawn route, not just the create handler."""
    worker_dir = tmp_path / "via-spawn"
    worker_dir.mkdir()

    with patch("swarm.worker.manager.add_worker_live", new_callable=AsyncMock) as mock_add:
        mock_add.return_value = Worker(
            name="via-spawn", path=str(worker_dir), process=FakeWorkerProcess(name="via-spawn")
        )
        resp = await config_client.post(
            "/api/workers/spawn",
            json={"name": "via-spawn", "path": str(worker_dir)},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 201

    assert _identity_of(worker_dir) == "via-spawn"


@pytest.mark.asyncio
async def test_spawn_resolves_path_from_config_when_given_only_a_name(config_client, tmp_path):
    """AC-6. The path is already in config; requiring the caller to repeat it
    made the documented recovery ('respawn the worker') fail with a 400."""
    worker_dir = tmp_path / "known-worker"
    worker_dir.mkdir()
    d = config_client.app["daemon"]
    from swarm.config import WorkerConfig

    d.config.workers.append(WorkerConfig(name="known-worker", path=str(worker_dir)))

    with patch("swarm.worker.manager.add_worker_live", new_callable=AsyncMock) as mock_add:
        mock_add.return_value = Worker(
            name="known-worker",
            path=str(worker_dir),
            process=FakeWorkerProcess(name="known-worker"),
        )
        resp = await config_client.post(
            "/api/workers/spawn",
            json={"name": "known-worker"},
            headers=_AUTH_HEADERS,
        )

    assert resp.status == 201, await resp.text()
    assert _identity_of(worker_dir) == "known-worker"


@pytest.mark.asyncio
async def test_revive_respawns_a_worker_that_kill_dropped_from_the_roster(config_client, tmp_path):
    """AC-5. ``kill`` marks the worker STUNG, but ``discover()`` rebuilds the
    roster from LIVE pool processes only — so the killed worker is erased and
    the documented recovery, ``POST /api/workers/{name}/revive``, answered 404
    "Worker not found". Revive now falls back to respawning a worker that is
    still in config, which also re-writes its identity file on the way."""
    worker_dir = tmp_path / "killed-worker"
    worker_dir.mkdir()
    d = config_client.app["daemon"]
    from swarm.config import WorkerConfig

    d.config.workers.append(WorkerConfig(name="killed-worker", path=str(worker_dir)))
    # The post-kill state: configured, but gone from the live roster.
    assert d.get_worker("killed-worker") is None

    with patch("swarm.worker.manager.add_worker_live", new_callable=AsyncMock) as mock_add:
        mock_add.return_value = Worker(
            name="killed-worker",
            path=str(worker_dir),
            process=FakeWorkerProcess(name="killed-worker"),
        )
        resp = await config_client.post("/api/workers/killed-worker/revive", headers=_AUTH_HEADERS)

    assert resp.status == 200, await resp.text()
    assert _identity_of(worker_dir) == "killed-worker"


@pytest.mark.asyncio
async def test_revive_of_an_unconfigured_worker_still_404s(config_client):
    """The fallback must not turn a genuine typo into a silent spawn."""
    resp = await config_client.post("/api/workers/no-such-worker/revive", headers=_AUTH_HEADERS)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_spawn_without_a_name_in_config_says_the_path_is_unresolvable(config_client):
    """The error must name WHY, so 'path is required' doesn't read as a bug in
    the caller when the worker simply isn't configured."""
    resp = await config_client.post(
        "/api/workers/spawn",
        json={"name": "never-heard-of-it"},
        headers=_AUTH_HEADERS,
    )
    assert resp.status == 400
    body = (await resp.text()).lower()
    assert "config" in body, body
