"""#1646 — `_identify_worker` returned `project-root` for the ENTIRE fleet.

MEASURED 2026-08-15 while testing #1623. The scan matched the hook's cwd against worker
paths and returned the FIRST prefix match, iterating the board in order. `project-root`
is configured as `/home/bschleifer/projects` — an ancestor of all 27 workers — and sits
at index 0, so every worker under it identified as project-root.

Evidence at the time: 191 hook entries in 30 minutes, ZERO naming `swarm`, while that
worker ran Bash continuously; a hand-fired hook from `swarm`'s own session logged as
`project-root`; and `project-root` showed SLEEPING for 11 hours while apparently
emitting Bash traffic every few seconds.

TWO CONSEQUENCES, and the second is why this is not merely cosmetic:
  · buzz-log attribution is fiction, so any investigation grouping hook decisions by
    worker reads one name for the whole fleet;
  · `_check_file_lock` takes `worker.name` as the lock owner. With every worker wearing
    one name, `lock_owner != worker_name` is never true between hook-driven writes, so
    the collision guard cannot fire at all — and the unconditional refresh at the end
    rewrites a real `swarm_claim_file` claim to `project-root`, dispossessing the holder.
    That is the same harm the `"unknown"` fix (#1498) removed, reachable by another route.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

from swarm.server.routes.hooks import _identify_worker

_HOME = os.path.expanduser("~")


def _worker(name: str, path: str) -> MagicMock:
    w = MagicMock()
    w.name = name
    w.path = path
    return w


def _daemon(*workers: MagicMock) -> MagicMock:
    d = MagicMock()
    d.workers = list(workers)
    return d


def test_a_nested_worker_identifies_as_itself_not_the_ancestor():
    """THE REPORTED DEFECT, in board order: the ancestor is listed FIRST, exactly as
    `project-root` is. First-match-wins returns it and the real worker never wins."""
    ancestor = _worker("project-root", f"{_HOME}/projects")
    nested = _worker("swarm", f"{_HOME}/projects/personal/swarm")
    d = _daemon(ancestor, nested)

    got = _identify_worker(d, {"cwd": f"{_HOME}/projects/personal/swarm"})

    assert got is not None
    assert got.name == "swarm"


def test_the_ancestor_still_wins_for_a_path_only_it_covers():
    """POSITIVE CONTROL. A fix that simply stopped matching ancestors would break
    project-root itself, which legitimately owns everything not claimed by a nested
    worker — and would pass the test above while being wrong."""
    ancestor = _worker("project-root", f"{_HOME}/projects")
    nested = _worker("swarm", f"{_HOME}/projects/personal/swarm")
    d = _daemon(ancestor, nested)

    got = _identify_worker(d, {"cwd": f"{_HOME}/projects/rcg/some-untracked-repo"})

    assert got is not None
    assert got.name == "project-root"


def test_a_deeper_subdirectory_of_the_nested_worker_still_resolves_to_it():
    """Hooks report the process cwd, which is often a subdirectory of the checkout."""
    d = _daemon(
        _worker("project-root", f"{_HOME}/projects"),
        _worker("swarm", f"{_HOME}/projects/personal/swarm"),
    )

    got = _identify_worker(d, {"cwd": f"{_HOME}/projects/personal/swarm/src/swarm/server"})

    assert got is not None
    assert got.name == "swarm"


def test_a_tilde_configured_worker_path_is_expanded():
    """SECOND, LATENT DEFECT under the first. 8 workers are configured with `~/...`
    paths. `os.path.realpath("~/projects/x")` does NOT expand `~`, so those entries
    could never match a real cwd even once the ordering bug was fixed — the fix would
    have looked complete while a third of the fleet still misidentified."""
    d = _daemon(
        _worker("project-root", f"{_HOME}/projects"),
        _worker("hub", "~/projects/rcg/rcg-hub"),
    )

    got = _identify_worker(d, {"cwd": f"{_HOME}/projects/rcg/rcg-hub"})

    assert got is not None
    assert got.name == "hub"


def test_a_partial_directory_name_is_not_a_match():
    """`/projects/rcg/rcg-admin` must not claim `/projects/rcg/rcg-admin-agent`. String
    prefixes and PATH prefixes are different things, and the fleet has exactly this
    pair configured (`admin` alongside `admin-agent1`)."""
    d = _daemon(
        _worker("admin", f"{_HOME}/projects/rcg/rcg-admin"),
        _worker("admin-agent1", f"{_HOME}/projects/rcg/rcg-admin-a"),
    )

    got = _identify_worker(d, {"cwd": f"{_HOME}/projects/rcg/rcg-admin-a"})

    assert got is not None
    assert got.name == "admin-agent1"


def test_no_match_still_returns_None_rather_than_guessing():
    """FAIL-CLOSED ON IDENTITY. `_check_file_lock` relies on None to fail OPEN; a
    confident wrong answer is worse than no answer, which is this whole ticket."""
    d = _daemon(
        _worker("project-root", f"{_HOME}/projects"),
        _worker("swarm", f"{_HOME}/projects/personal/swarm"),
    )

    assert _identify_worker(d, {"cwd": "/var/tmp/somewhere-else"}) is None


def test_a_single_worker_fallback_is_not_applied_when_the_cwd_is_foreign():
    """The one-worker fallback exists for single-worker installs, but it must not
    manufacture an identity for a cwd nowhere near that worker."""
    d = _daemon(_worker("solo", f"{_HOME}/projects/personal/solo"))

    got = _identify_worker(d, {"cwd": f"{_HOME}/projects/personal/solo/src"})
    assert got is not None and got.name == "solo"
