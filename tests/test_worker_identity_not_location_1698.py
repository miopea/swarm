"""#1698 Phase A — identify a worker by WHO IT IS, not by WHERE IT IS STANDING.

THE MEASUREMENT THIS UNBLOCKS, and why it was impossible before. `_identify_worker`
matched cwd against configured worker paths, so a worker operating inside ANOTHER
worker's repo was recorded as the worker who OWNS that repo. The #1671 incident — a
worker running git in a repo it does not own — was therefore invisible BY CONSTRUCTION,
not merely unlogged. No query of the existing 41,251 hook rows can recover it.

MEASURED 2026-08-16 before writing this:
  · 41,251 hook decisions logged; only the 2,600 after #1646's longest-match fix are
    attributable at all (before it, 77.4% said `project-root`, an ancestor of every
    worker; after, 10.7%).
  · 675 of those 2,600 (26.0%) matched NO configured path — but 611 are swarm MCP tools,
    i.e. the Queen, not out-of-path workers.
  · 19 of 20 live worker processes carry SWARM_WORKER_NAME. That is ground truth,
    independent of location, and the hook was throwing it away.

NOTHING HERE DENIES ANYTHING. Phase A instruments and counts. rules.py's standing rule is
that a guard is run against a corpus and its false positives counted BEFORE it moves from
abstain to deny, and there is no rate yet to design against.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from swarm.server.routes.hooks import _identify_worker, out_of_path


def _w(name: str, path: str):
    w = MagicMock()
    w.name = name
    w.path = path
    return w


def _daemon(*workers):
    d = MagicMock()
    d.workers = list(workers)
    return d


# ---------------------------------------------------------------------------
# Identity beats location
# ---------------------------------------------------------------------------


def test_a_worker_in_another_repo_is_attributed_to_ITSELF():
    """THE DEFECT, AS A TEST. `swarm` running inside rcg-architecture used to be logged
    as `architecture` — the location's owner — with full confidence and no ambiguity
    marker. That is worse than 'unknown': it is a confident wrong name in a forensic
    record, the exact thing #1675 refused to ship for PTY writes."""
    d = _daemon(_w("swarm", "/projects/swarm"), _w("architecture", "/projects/arch"))

    got = _identify_worker(d, {"swarm_worker_name": "swarm", "cwd": "/projects/arch"})

    assert got.name == "swarm"


def test_location_still_identifies_when_no_identity_is_forwarded():
    """1 of 20 live processes predates the env injection, and old sessions outlive a
    daemon restart. An absent identity must degrade to yesterday's behaviour, never to
    nobody — a measurement that loses a fifth of the fleet to be pure is worse."""
    d = _daemon(_w("swarm", "/projects/swarm"), _w("architecture", "/projects/arch"))

    got = _identify_worker(d, {"cwd": "/projects/arch"})

    assert got.name == "architecture"


def test_an_unknown_declared_name_falls_back_rather_than_inventing_a_worker():
    """A stale or renamed worker name must not resolve to something arbitrary."""
    d = _daemon(_w("swarm", "/projects/swarm"))

    got = _identify_worker(d, {"swarm_worker_name": "ghost", "cwd": "/projects/swarm"})

    assert got.name == "swarm"


# ---------------------------------------------------------------------------
# The measurement itself
# ---------------------------------------------------------------------------


def test_a_worker_at_home_is_not_out_of_path():
    assert out_of_path(_w("swarm", "/projects/swarm"), "/projects/swarm") is False


def test_a_subdirectory_of_home_is_not_out_of_path():
    assert out_of_path(_w("swarm", "/projects/swarm"), "/projects/swarm/src/x") is False


def test_a_sibling_repo_is_out_of_path():
    """The #1671 shape."""
    assert out_of_path(_w("swarm", "/projects/swarm"), "/projects/arch") is True


def test_a_sibling_with_a_shared_prefix_is_not_a_false_negative():
    """`/projects/swarm-extra` starts with `/projects/swarm` as a STRING but is a
    different directory. A prefix test without the separator would call it home."""
    assert out_of_path(_w("swarm", "/projects/swarm"), "/projects/swarm-extra") is True


@pytest.mark.parametrize(
    "worker,cwd",
    [(None, "/projects/x"), (_w("swarm", "/projects/swarm"), ""), (_w("swarm", ""), "/projects/x")],
)
def test_unknowable_cases_return_None_and_never_False(worker, cwd):
    """FAIL-HONEST, and this is the load-bearing property of a measurement. `None` means
    'could not tell'. Collapsing it to False would pad the denominator with unknowns and
    manufacture a clean rate out of ignorance — the failure this codebase keeps paying
    for. A zero built from Nones is not a zero."""
    assert out_of_path(worker, cwd) is None


def test_the_home_path_is_expanded_before_comparison():
    """Eight workers are configured with `~/...` paths. `realpath('~/x')` does NOT expand
    `~`, so without expanduser every one of them would read as permanently out-of-path —
    a 100% false-positive rate on a third of the fleet. #1646 hit exactly this."""
    home = str(Path.home() / "projects" / "swarm")

    assert out_of_path(_w("swarm", "~/projects/swarm"), home) is False


def test_nothing_in_the_hook_denies_on_this_signal_yet():
    """PHASE A IS MEASUREMENT ONLY. If `out_of_path` ever reaches a decision branch, it
    must arrive with a corpus and a false-positive count, per the standing rule at the top
    of rules.py — four false positives and one near-miss on the sibling guard layer in one
    week is why that rule exists."""
    src = (Path(__file__).resolve().parent.parent / "src/swarm/server/routes/hooks.py").read_text()
    decision_lines = [
        ln for ln in src.splitlines() if "out_of_path" in ln and ("if " in ln or "block" in ln)
    ]

    assert decision_lines == [], f"out_of_path is being acted on: {decision_lines}"
