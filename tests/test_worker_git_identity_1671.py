"""#1671 — a commit must say WHICH WORKER made it.

`admin` could not claim commit 4f2d417 as its own. Measured while diagnosing: `user.email`
is set GLOBALLY and nowhere else — no local override in rcg-architecture, swarm or
rcg-platform-data — so every worker in the fleet commits as one person. That is a loss of
ATTRIBUTION, separate from branch collision and not fixed by worktrees.

THE CORRECTION THAT SHAPED THIS FIX came from project-root, with real commits rather than
argument:
  A. Two worktrees under one identity produce BYTE-IDENTICAL metadata. A commit object
     holds tree/parent/author/committer/message — no path, no worktree. So worktrees alone
     cannot fix attribution BY CONSTRUCTION.
  B. Per-worktree identity does fix it.
  C. But identity follows the DIRECTORY: committing inside bravo's worktree while "being"
     alpha records bravo. B buys which-DIRECTORY, not which-WORKER.

THIS FIX DEFEATS C, which is the reason to prefer it: the environment follows the PROCESS.
A worker that reaches into another repo still records itself.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from swarm.pty.holder import build_worker_env


def test_the_spawn_env_carries_a_per_worker_committer_identity():
    env = build_worker_env("d365-solutions")

    assert env["GIT_COMMITTER_NAME"] == "worker:d365-solutions"
    assert env["GIT_COMMITTER_EMAIL"] == "d365-solutions@workers.swarm"


def test_two_workers_get_DIFFERENT_identities():
    """THE WHOLE POINT, and the thing worktrees could not deliver: two workers must be
    distinguishable from commit metadata alone."""
    a = build_worker_env("admin")
    b = build_worker_env("d365-solutions")

    assert a["GIT_COMMITTER_EMAIL"] != b["GIT_COMMITTER_EMAIL"]
    assert a["GIT_COMMITTER_NAME"] != b["GIT_COMMITTER_NAME"]


def test_the_author_is_left_alone():
    """OPERATOR DECISION 2026-08-15. Author stays the human's identity so GitHub linkage
    and verified-commit status are untouched and no per-worker address has to be added to
    the account. Setting GIT_AUTHOR_* here would silently unlink every commit the fleet
    makes, which is a far larger blast radius than the bug being fixed."""
    env = build_worker_env("admin")

    assert "GIT_AUTHOR_NAME" not in env
    assert "GIT_AUTHOR_EMAIL" not in env


def test_the_existing_swarm_env_is_unchanged():
    """POSITIVE CONTROL. The identity was added to an env block that already carries
    worker wiring; breaking that would break worker identity everywhere (#1646) while
    these tests still passed."""
    env = build_worker_env("swarm", ["claude", "--continue"])

    assert env["SWARM_MANAGED"] == "1"
    assert env["SWARM_WORKER_NAME"] == "swarm"
    assert env["TERM"] == "xterm-256color"
    assert env["CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN"] == "1"


def test_a_non_claude_command_does_not_get_the_claude_screen_flag(monkeypatch):
    """POSITIVE CONTROL for the branch above — it must stay conditional.

    `monkeypatch.delenv` is load-bearing: `build_worker_env` starts from
    `os.environ.copy()`, and THIS TEST RUNS INSIDE A SWARM-SPAWNED WORKER, which already
    carries CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN. Without clearing it first the assertion
    fails against inherited state rather than against the branch — which is what it did on
    the first run."""
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN", raising=False)

    env = build_worker_env("swarm", ["bash"])

    assert "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN" not in env


# ---------------------------------------------------------------------------
# The claim, tested against REAL git rather than asserted from the docs
# ---------------------------------------------------------------------------


# The variables `build_worker_env` injects. The negative control must REMOVE these, not
# merely decline to set them: since the holder began injecting them at spawn (#1671), a
# worker's own session HAS them, so a test that inherits `os.environ` measures the
# operator's shell rather than the absence it claims to prove. That is how this control
# started failing — correctly — the moment the fix went live in the session running it.
_IDENTITY_VARS = ("GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")


def _git(repo, *args, env=None, strip=()):
    merged = {**os.environ, **(env or {})}
    for key in strip:
        merged.pop(key, None)
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env=merged,
    )


@pytest.fixture
def repo(tmp_path):
    """A real repo with a global-style author identity and no local override.

    Mirrors the live fleet: `user.email` set at a level above the repo, nothing local.
    """
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.name", "Brad")
    _git(r, "config", "user.email", "bschleifer@rcg.org")
    return r


def _commit(repo, message: str, worker: str) -> tuple[str, str]:
    """Commit as *worker* and return (author, committer) as `Name <email>`."""
    env = build_worker_env(worker)
    (repo / f"{worker}-{message}.txt").write_text("x")
    _git(repo, "add", "-A", env=env)
    _git(repo, "commit", "-qm", message, env=env)
    out = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>%n%cn <%ce>"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return out[0], out[1]


def test_a_real_commit_identifies_the_worker_from_metadata_alone(repo):
    """AC3's shape, in a test. `git log` alone answers 'which worker made this'."""
    author, committer = _commit(repo, "one", "architecture")

    assert committer == "worker:architecture <architecture@workers.swarm>"
    assert author == "Brad <bschleifer@rcg.org>", "author must be untouched"


def test_two_workers_committing_to_THE_SAME_directory_stay_distinguishable(repo):
    """PROJECT-ROOT'S C, DEFEATED — and this is the test that justifies choosing the
    environment over per-worktree config.

    Under per-worktree identity these two commits would be IDENTICAL, because both were
    made in the same directory and identity follows the directory. Under a per-process
    environment they differ, which is exactly the incident: d365-solutions running git
    inside rcg-architecture must record d365-solutions."""
    _, first = _commit(repo, "one", "architecture")
    _, second = _commit(repo, "two", "d365-solutions")

    assert first == "worker:architecture <architecture@workers.swarm>"
    assert second == "worker:d365-solutions <d365-solutions@workers.swarm>"
    assert first != second


def test_without_the_env_both_commits_are_indistinguishable(repo):
    """THE NEGATIVE CONTROL — the bug itself, reproduced. Without the injected identity
    the two commits above collapse to the same committer, which is why `admin` could not
    claim 4f2d417. Without this test the one above proves only that git works.

    STRIPS the identity vars explicitly. Inheriting the ambient environment made this
    control pass for the wrong reason before #1671 shipped and fail for the right one
    after — a worker session now carries `GIT_COMMITTER_*`, so "did not set it" and "it
    is not set" stopped being the same statement."""
    (repo / "a.txt").write_text("x")
    _git(repo, "add", "-A", strip=_IDENTITY_VARS)
    _git(repo, "commit", "-qm", "plain one", strip=_IDENTITY_VARS)
    (repo / "b.txt").write_text("x")
    _git(repo, "add", "-A", strip=_IDENTITY_VARS)
    _git(repo, "commit", "-qm", "plain two", strip=_IDENTITY_VARS)

    out = subprocess.run(
        ["git", "log", "-2", "--format=%cn <%ce>"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\n")
    assert out[0] == out[1] == "Brad <bschleifer@rcg.org>"
