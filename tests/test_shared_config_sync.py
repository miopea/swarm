"""Shared agent-config sync (claude-team-config + codex-team-config). Task #1241.

FOUR THINGS WERE WRONG OR MISSING, AND ONE MUST NOT BREAK.

1. The installer was invoked DIRECTLY. Both repos commit ``install.sh`` as mode
   100644 — not executable, in git, in every clone — so direct invocation exits
   126. Measured on this box: 9 × "team config install.sh exited 126" over ~31h.

2. The dev-mode gate read only ``SWARM_DEV``, which is set nowhere here, so the
   sync fired against developers' working checkouts.

3. The full install re-ran on every daemon start regardless of change.

4. There was no codex path at all.

5. LOAD-BEARING: a prompt-ablation experiment lives on branch ``ablation`` in
   claude-team-config. Nothing here may change what is checked out.

A NOTE ON THE EVIDENCE, because it shaped the design. The original report said
"9 failures, 0 successes, so it has never succeeded". The 9 failures are real
(WARNING). The 0 is not evidence: the success line was ``_log.debug`` and the
daemon runs at ``log_level = WARNING``, so a success could never have appeared
in that log — a grep for it returns 0 whether or not it ever worked. That
asymmetry is the same defect class as the exit-126 outage itself, so outcomes
are now logged symmetrically and ``test_success_and_failure_are_equally_visible``
guards it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from swarm.update import (
    _SHARED_CONFIG_REPOS,
    SharedConfigRepo,
    _read_shared_config_state,
    _sync_one_shared_config,
    dev_mode_active,
    sync_team_config,
)


@pytest.fixture
def repo(tmp_path):
    """A repo dir with a NON-EXECUTABLE install.sh, mirroring the real mode."""
    d = tmp_path / "claude-team-config"
    d.mkdir()
    sh = d / "install.sh"
    sh.write_text("#!/usr/bin/env bash\necho installed\n")
    sh.chmod(0o644)  # 100644 — exactly what both repos commit
    return SharedConfigRepo(name="claude-team-config", candidates=(d,))


# --- 1. exit 126 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_installer_is_invoked_through_bash_not_directly(repo, tmp_path):
    """AC-1. The command must name an interpreter.

    Asserted on the command string rather than the exit code so the test states
    *why* it works. A test that only checked "returncode == 0" would pass again
    the moment someone made their local copy executable, while every clean clone
    kept failing — which is how this shipped in the first place.
    """
    seen: dict[str, str] = {}

    async def fake_exec(*args, **kwargs):
        seen["cmd"] = args[2]  # bash -c <cmd>
        proc = AsyncMock()
        proc.stdout.read = AsyncMock(return_value=b"ok")
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = 0
        return proc

    with (
        patch("asyncio.create_subprocess_exec", new=fake_exec),
        patch("swarm.update._repo_fingerprint", new=AsyncMock(return_value="main:aaa:aaa")),
    ):
        await _sync_one_shared_config(repo, {})

    assert "bash " in seen["cmd"], f"installer invoked without an interpreter: {seen['cmd']!r}"
    assert seen["cmd"].startswith("yes | bash "), seen["cmd"]


@pytest.mark.asyncio
async def test_a_real_non_executable_installer_actually_runs(repo):
    """The end-to-end version of AC-1: a mode-644 script, really executed.

    This is the check that would have caught the original bug. The mock above
    asserts the shape of the command; this one proves the shape works against a
    file with the mode the real repos carry.
    """
    state: dict[str, str] = {}
    with patch("swarm.update._repo_fingerprint", new=AsyncMock(return_value="main:aaa:aaa")):
        changed = await _sync_one_shared_config(repo, state)

    assert changed is True, "a non-executable install.sh still did not run (exit 126?)"
    assert state["claude-team-config"] == "main:aaa:aaa"


# --- 2. dev-mode gate ------------------------------------------------------


def test_dev_mode_is_not_decided_by_the_env_var_alone():
    """AC-2. ``SWARM_DEV`` unset must NOT mean "not a dev box"."""
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("swarm.update._is_dev_install", return_value=True),
    ):
        assert dev_mode_active() is True

    with (
        patch.dict("os.environ", {}, clear=True),
        patch("swarm.update._is_dev_install", return_value=False),
    ):
        assert dev_mode_active() is False

    # The env var still works on its own, for a non-editable checkout.
    with (
        patch.dict("os.environ", {"SWARM_DEV": "1"}, clear=True),
        patch("swarm.update._is_dev_install", return_value=False),
    ):
        assert dev_mode_active() is True


@pytest.mark.asyncio
async def test_dev_mode_skips_every_repo_including_codex():
    """AC-2 + AC-4. The gate is at the chokepoint, so it covers both repos."""
    with (
        patch("swarm.update.dev_mode_active", return_value=True),
        patch("swarm.update._sync_one_shared_config", new=AsyncMock()) as one,
    ):
        await sync_team_config()
    one.assert_not_called()


# --- 3. skip when already current -----------------------------------------


@pytest.mark.asyncio
async def test_already_current_repo_skips_the_install(repo):
    """AC-3. Matching fingerprint → the installer is never spawned."""
    state = {"claude-team-config": "main:aaa:aaa"}
    spawned = False

    async def fake_exec(*args, **kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("installer ran for an already-current repo")

    with (
        patch("swarm.update._repo_fingerprint", new=AsyncMock(return_value="main:aaa:aaa")),
        patch("asyncio.create_subprocess_exec", new=fake_exec),
    ):
        changed = await _sync_one_shared_config(repo, state)

    assert changed is False
    assert spawned is False


@pytest.mark.asyncio
async def test_a_moved_head_reinstalls(repo):
    """AC-3, the other direction. A skip-always check would also pass the test
    above; this is what proves the short-circuit still lets updates through."""
    state = {"claude-team-config": "main:OLD:OLD"}
    with patch("swarm.update._repo_fingerprint", new=AsyncMock(return_value="main:NEW:NEW")):
        changed = await _sync_one_shared_config(repo, state)
    assert changed is True
    assert state["claude-team-config"] == "main:NEW:NEW"


@pytest.mark.asyncio
async def test_an_unreadable_repo_installs_rather_than_skipping(repo):
    """ "Unknown" must never compare equal to a stored fingerprint.

    If a broken git returned "" and that were cached, the repo would look
    "already current" forever and silently stop updating — a quiet no-op that
    reads exactly like success.
    """
    state = {"claude-team-config": ""}
    with patch("swarm.update._repo_fingerprint", new=AsyncMock(return_value="")):
        changed = await _sync_one_shared_config(repo, state)
    assert changed is True, "empty fingerprint was treated as already-current"


# --- 4. codex parity -------------------------------------------------------


def test_codex_repo_is_registered_with_the_same_machinery():
    """AC-4. Same dataclass, same loop — so it inherits both gates by
    construction rather than by a parallel implementation that can drift."""
    names = [r.name for r in _SHARED_CONFIG_REPOS]
    assert "claude-team-config" in names
    assert "codex-team-config" in names
    for r in _SHARED_CONFIG_REPOS:
        assert r.installer == "install.sh"


# --- 5. the experiment must survive ---------------------------------------


@pytest.mark.asyncio
async def test_the_sync_never_runs_a_branch_changing_git_command(repo):
    """AC-6, structurally. checkout/reset/pull here would move the ablation
    branch — a corruption that would ALSO look like a successful sync."""
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        proc = AsyncMock()
        proc.stdout.read = AsyncMock(return_value=b"ok")
        proc.communicate = AsyncMock(return_value=(b"main:aaa:aaa", b""))
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = 0
        return proc

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        await _sync_one_shared_config(repo, {})

    git_args = [a[1:] for a in calls if a and a[0] == "git"]
    assert git_args, "no git commands ran at all — fingerprinting is not working"
    for args in git_args:
        assert args[0] in ("rev-parse", "fetch"), f"non-read-only git command: {args}"


def test_no_branch_mutating_git_verbs_appear_in_the_module_source():
    """Belt and braces on AC-6: the runtime test above only sees the paths it
    exercises. This one sees the whole module, so a checkout added to a branch
    the tests do not reach still trips it."""
    import inspect

    import swarm.update as mod

    src = inspect.getsource(mod)
    section = src[src.index("# --- Shared team-config sync") :]
    for verb in ('"checkout"', '"reset"', '"pull"', '"merge"', '"switch"'):
        assert verb not in section, f"branch-mutating git verb {verb} in the sync path"


# --- observability ---------------------------------------------------------


@pytest.mark.asyncio
async def test_success_and_failure_are_equally_visible(repo, caplog):
    """The success line must clear the daemon's configured WARNING threshold's
    sibling problem: it used to be DEBUG while failure was WARNING, so at the
    operator's level a working sync and a sync that never ran produced the same
    empty grep. Both outcomes now log at INFO or above.
    """
    with (
        caplog.at_level(logging.INFO, logger="swarm.update"),
        patch("swarm.update._repo_fingerprint", new=AsyncMock(return_value="main:aaa:aaa")),
    ):
        await _sync_one_shared_config(repo, {})

    msgs = [r.getMessage() for r in caplog.records]
    assert any("team config sync complete" in m for m in msgs), msgs
    assert all(r.levelno >= logging.INFO for r in caplog.records if "complete" in r.getMessage())


@pytest.mark.asyncio
async def test_installer_failure_is_loud_and_not_recorded_as_current(repo):
    """AC-7. A failed install must not be cached as installed, or one bad run
    would suppress every retry — a swallowed failure with a long tail."""
    state: dict[str, str] = {}

    async def failing_exec(*args, **kwargs):
        proc = AsyncMock()
        proc.stdout.read = AsyncMock(return_value=b"boom")
        proc.communicate = AsyncMock(return_value=(b"main:aaa:aaa", b""))
        proc.wait = AsyncMock(return_value=1)
        proc.returncode = 0 if args[0] == "git" else 1
        return proc

    with patch("asyncio.create_subprocess_exec", new=failing_exec):
        changed = await _sync_one_shared_config(repo, state)

    assert changed is False
    assert "claude-team-config" not in state, "a failed install was cached as current"


@pytest.mark.asyncio
async def test_a_missing_repo_is_a_no_op(tmp_path):
    """Most boxes have neither repo; that is normal, not an error."""
    missing = SharedConfigRepo(name="codex-team-config", candidates=(tmp_path / "nope",))
    assert await _sync_one_shared_config(missing, {}) is False


def test_state_file_survives_corruption():
    """A half-written cache must degrade to "reinstall", never crash startup."""
    with patch("builtins.open", side_effect=OSError):
        assert _read_shared_config_state() == {}
    with patch("builtins.open", new=lambda *a, **k: __import__("io").StringIO("not json")):
        assert _read_shared_config_state() == {}


def test_state_file_round_trips(tmp_path):
    from swarm.update import _write_shared_config_state

    target = tmp_path / "state.json"
    with patch("swarm.update._SHARED_CONFIG_STATE_FILE", target):
        _write_shared_config_state({"claude-team-config": "main:a:a"})
        assert json.loads(Path(target).read_text())["claude-team-config"] == "main:a:a"
