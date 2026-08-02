"""#1203 — a reload that loaded uncommitted code must be diagnosable afterwards.

Dev-mode Reload re-execs the daemon, which imports ``src/swarm/`` from the
WORKING TREE. That is the point of the button — edit, Reload, test — and it
stays. The hazard is that in this fleet the editor is usually the *swarm
worker*, and the operator has no visible connection between "I clicked Reload"
and "a worker is 20 minutes into a cross-module refactor".

Measured 2026-08-02: a Reload at ~15:06Z picked up a half-finished #1195 and
worker creation began failing fleet-wide with ``add_worker_live() missing 1
required keyword-only argument: 'write_identity'``. origin/main was
self-consistent the whole time; the daemon was running code that existed in no
commit. Diagnosing it cost a round trip, because the natural first read is "the
thing that just shipped is broken" and the source on disk had already moved on.

WHY THE ORDINARY STALENESS SIGNALS DO NOT WORK, and why nothing here may use
them: ``os.execv`` preserves both PID and process start time, so ``ps -o
lstart=`` reported a Jul 31 start for a daemon that had just reloaded.

``build_sha()`` already fingerprints the tree as ``<git sha>+<source hash>``,
but a fingerprint is not a diagnosis: ``abc12345+def67890`` cannot tell you
whether ``def67890`` is what ``abc12345`` checks out to, or the result of
uncommitted edits. "Running the last release" and "running code from no commit"
look identical. That is the same shape as a check that reports the same thing
whether or not the thing it measures happened.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swarm.update import SourceTreeState, get_source_tree_state

# --- the probe ---------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_tree_reports_not_dirty() -> None:
    with (
        patch("swarm.update._find_source_repo_root", return_value=Path("/repo")),
        patch("swarm.update._git_porcelain", new=AsyncMock(return_value="")),
    ):
        state = await get_source_tree_state()
    assert state.is_dirty is False
    assert state.dirty_files == []


@pytest.mark.asyncio
async def test_dirty_tree_names_the_files() -> None:
    """AC-3. 'dirty tree' alone does not tell an investigator which subsystem
    is suspect — the whole cost of the incident was not knowing that."""
    porcelain = (
        " M src/swarm/worker/manager.py\n M src/swarm/server/worker_service.py\n?? scratch.py\n"
    )
    with (
        patch("swarm.update._find_source_repo_root", return_value=Path("/repo")),
        patch("swarm.update._git_porcelain", new=AsyncMock(return_value=porcelain)),
    ):
        state = await get_source_tree_state()
    assert state.is_dirty is True
    assert "src/swarm/worker/manager.py" in state.dirty_files
    assert "src/swarm/server/worker_service.py" in state.dirty_files
    assert "scratch.py" in state.dirty_files


@pytest.mark.asyncio
async def test_probe_is_safe_when_not_a_git_checkout() -> None:
    """A PyPI/wheel install has no repo. Report unknown rather than claiming
    clean — 'we could not tell' and 'it is fine' are different answers."""
    with patch("swarm.update._find_source_repo_root", return_value=None):
        state = await get_source_tree_state()
    assert state.is_dirty is False
    assert state.repo_root == ""
    assert state.checked is False


@pytest.mark.asyncio
async def test_probe_never_raises_into_startup() -> None:
    with (
        patch("swarm.update._find_source_repo_root", return_value=Path("/repo")),
        patch("swarm.update._git_porcelain", new=AsyncMock(side_effect=OSError("no git"))),
    ):
        state = await get_source_tree_state()
    assert state.checked is False


def test_summary_is_useful_without_reading_the_file_list() -> None:
    state = SourceTreeState(
        repo_root="/repo",
        head="abc12345",
        dirty_files=["src/swarm/worker/manager.py", "src/swarm/server/daemon.py"],
        checked=True,
    )
    summary = state.summary()
    assert "abc12345" in summary
    assert "2" in summary
    assert "manager.py" in summary


# --- the durable record ------------------------------------------------


def _daemon_with_drone_log():
    from tests.conftest import make_daemon

    d = make_daemon()
    # ``make_daemon`` builds via ``__new__``, so attributes set in ``__init__``
    # are absent. Mirror the two this test needs: a fresh daemon has no source
    # state yet, and ``pilot`` must be JSON-serialisable for the health route.
    d.source_tree_state = None
    d.pilot = None
    return d


@pytest.mark.asyncio
async def test_dirty_reload_writes_a_durable_buzz_entry_naming_files() -> None:
    """AC-1 + AC-3. The record has to survive the session — the investigator
    arrives after the fact, and by then the tree has moved on again."""
    d = _daemon_with_drone_log()
    state = SourceTreeState(
        repo_root="/repo",
        head="abc12345",
        dirty_files=["src/swarm/worker/manager.py"],
        checked=True,
    )
    with patch("swarm.update.get_source_tree_state", new=AsyncMock(return_value=state)):
        await d.record_source_tree_state()

    entries = [e for e in d.drone_log.entries if "uncommitted" in e.detail.lower()]
    assert entries, "no durable record that the daemon loaded an uncommitted tree"
    assert "src/swarm/worker/manager.py" in entries[0].detail
    assert entries[0].is_notification is True, "operator must actually see it"


@pytest.mark.asyncio
async def test_clean_reload_is_silent() -> None:
    """AC-6. Reload is a routine dev action; a line on every clean reload would
    train the operator to ignore the one that matters."""
    d = _daemon_with_drone_log()
    state = SourceTreeState(repo_root="/repo", head="abc12345", dirty_files=[], checked=True)
    with patch("swarm.update.get_source_tree_state", new=AsyncMock(return_value=state)):
        await d.record_source_tree_state()

    assert not [e for e in d.drone_log.entries if "uncommitted" in e.detail.lower()]


@pytest.mark.asyncio
async def test_state_is_readable_live_not_only_in_the_log() -> None:
    """Diagnosable means answerable now, too — an investigator should not have
    to grep a log to ask 'what is this daemon running?'."""
    d = _daemon_with_drone_log()
    state = SourceTreeState(repo_root="/repo", head="abc12345", dirty_files=["a.py"], checked=True)
    with patch("swarm.update.get_source_tree_state", new=AsyncMock(return_value=state)):
        await d.record_source_tree_state()

    assert d.source_tree_state is not None
    assert d.source_tree_state.is_dirty is True


@pytest.mark.asyncio
async def test_recording_never_breaks_startup() -> None:
    d = _daemon_with_drone_log()
    with patch("swarm.update.get_source_tree_state", new=AsyncMock(side_effect=RuntimeError)):
        await d.record_source_tree_state()  # must not raise
    assert d.source_tree_state is None


def test_signal_does_not_depend_on_pid_or_process_start_time() -> None:
    """AC-5, structurally. ``os.execv`` preserves PID and start time, so any
    implementation reaching for them is measuring something that cannot change
    across the event it is supposed to detect."""
    import inspect

    import swarm.update as update_mod

    src = inspect.getsource(update_mod.get_source_tree_state) + inspect.getsource(
        update_mod._find_source_repo_root
    )
    for banned in ("getpid", "create_time", "lstart", "boot_time"):
        assert banned not in src, f"staleness signal must not rest on {banned}"


@pytest.mark.asyncio
async def test_health_endpoint_exposes_the_source_state() -> None:
    """The field is present on every response — a status field, not a log line,
    so it costs nothing on a clean reload."""
    d = _daemon_with_drone_log()
    d.source_tree_state = SourceTreeState(
        repo_root="/repo", head="abc12345", dirty_files=["x.py"], checked=True
    )
    from swarm.server.routes.system import handle_health

    request = MagicMock()
    request.app = {"daemon": d}
    resp = await handle_health(request)

    import json as _json

    body = _json.loads(resp.body)
    assert body["source_dirty"] is True
    assert body["source_dirty_files"] == ["x.py"]
