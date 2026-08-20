"""#1698 — record WHICH REPO a command touched, as a bounded enumeration.

TWO MEASUREMENTS KILLED THE ORIGINAL DESIGN, and this module is what survives them.

1. THE PROCESS CWD IS THE WRONG OBSERVABLE. project-root's live cwd never left
   ~/projects while it did implementation, workflow edits and THREE PRODUCTION DEPLOYS
   inside bfg-ops-console's repo. `cd /other/repo && cmd` runs in a subshell; the parent
   never moves. Phase A recorded out_of_path=False for every one of those calls —
   correctly, and uselessly. The Queen had claimed twice that a cwd boundary "would have
   refused project-root's FIRST tool call"; it would not have seen it at all.
2. THE FIELD CONTAINS THE FLEET. project-root's configured path is ~/projects, which
   CONTAINS 24 of the other 25 workers' roots. Under a raw containment test that worker
   can NEVER be out of path.

WHAT IS STORED (Queen ruling): the resolved repo identity only — a configured worker name
or `outside-known-repos`. NOT command text (commands carry `op read`, `az`, connection
strings, bearer tokens; an audit trail that becomes a credential archive is worse than the
problem). NOT raw paths (they leak: /tmp/token-abc123).

COVERAGE IS PARTIAL ON PURPOSE AND THE LIMITS ARE IN `UNDETECTABLE`. A guard trusted
beyond its reach is worse than none.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from swarm.drones.repo_scope import (
    OUTSIDE_KNOWN_REPOS,
    UNDETECTABLE,
    UNRESOLVED,
    repo_roots_from_workers,
    repos_touched,
    resolve_repo,
)

# Mirrors the real roster's shape: project-root's root is an ANCESTOR of every other.
WORKERS = [
    SimpleNamespace(name="project-root", path="/home/u/projects"),
    SimpleNamespace(name="architecture", path="/home/u/projects/rcg/rcg-architecture"),
    SimpleNamespace(name="bfg-ops-console", path="/home/u/projects/personal/bfg-ops-console"),
    SimpleNamespace(name="platform", path="/home/u/projects/rcg/rcg-platform"),
    SimpleNamespace(name="platform-api", path="/home/u/projects/rcg/rcg-platform-api"),
]
ROOTS = repo_roots_from_workers(WORKERS)


# ---------------------------------------------------------------------------
# Longest match — without it the whole fleet resolves to project-root
# ---------------------------------------------------------------------------


def test_longest_match_wins_over_the_containing_root():
    """THE #1646 BUG, PREVENTED. A shortest- or first-match resolver attributes every
    path in the fleet to project-root, which is how 77.4% of hook calls were once read as
    its work."""
    assert resolve_repo("/home/u/projects/rcg/rcg-architecture/docs/x.md", ROOTS) == "architecture"


def test_the_containing_worker_still_owns_what_nobody_else_claims():
    assert resolve_repo("/home/u/projects/scratch/notes.txt", ROOTS) == "project-root"


def test_sibling_worktrees_are_not_confused_by_a_prefix():
    """THE ONE-CHARACTER TRAP. `rcg-platform` is a string prefix of `rcg-platform-api`,
    and those are sibling worktrees of the fleet's most active workers. A resolver
    matching without a separator would fire on them constantly and be switched off."""
    assert resolve_repo("/home/u/projects/rcg/rcg-platform-api/src/a.ts", ROOTS) == "platform-api"
    assert resolve_repo("/home/u/projects/rcg/rcg-platform/src/b.ts", ROOTS) == "platform"


def test_a_path_under_no_root_is_named_rather_than_dropped():
    """`outside-known-repos` is the INTERESTING value, not the leftover one."""
    assert resolve_repo("/etc/cron.d/backdoor", ROOTS) == OUTSIDE_KNOWN_REPOS


def test_an_unreadable_path_is_UNRESOLVED_not_outside():
    """Distinct values, for the same reason out_of_path returns None rather than False:
    a rate padded with unknowns is not a rate."""
    assert resolve_repo("", ROOTS) == UNRESOLVED
    assert resolve_repo("/x", {}) == UNRESOLVED


# ---------------------------------------------------------------------------
# THE INCIDENT SHAPE — what the cwd observable could not see
# ---------------------------------------------------------------------------


def test_THE_1844_SHAPE_a_cd_into_another_repo_is_seen():
    """project-root, cwd at its own root the whole time, working in bfg-ops-console.
    This is the exact call the previous design was blind to."""
    repos = repos_touched(
        "Bash",
        {"command": "cd /home/u/projects/personal/bfg-ops-console && npm run deploy"},
        "/home/u/projects",
        ROOTS,
    )

    assert "bfg-ops-console" in repos


def test_git_dash_C_is_seen_too():
    repos = repos_touched(
        "Bash",
        {"command": "git -C /home/u/projects/rcg/rcg-architecture commit -m x"},
        "/home/u/projects",
        ROOTS,
    )

    assert "architecture" in repos


def test_an_edit_target_is_seen_without_any_command():
    repos = repos_touched(
        "Edit",
        {"file_path": "/home/u/projects/rcg/rcg-architecture/docs/standards/ci.md"},
        "/home/u/projects",
        ROOTS,
    )

    assert "architecture" in repos


def test_the_workers_own_repo_still_shows_up_as_the_cwd():
    """POSITIVE CONTROL: ordinary in-repo work must resolve to that repo, or 'touched
    another repo' would be indistinguishable from 'touched nothing'."""
    repos = repos_touched(
        "Bash", {"command": "pytest -q"}, "/home/u/projects/rcg/rcg-architecture", ROOTS
    )

    assert repos == ["architecture"]


def test_both_repos_appear_when_a_command_spans_two():
    repos = repos_touched(
        "Bash",
        {
            "command": (
                "cp /home/u/projects/rcg/rcg-platform/a.ts "
                "/home/u/projects/rcg/rcg-platform-api/b.ts"
            )
        },
        "/home/u/projects",
        ROOTS,
    )

    assert repos == ["platform", "platform-api", "project-root"]


# ---------------------------------------------------------------------------
# What it NEVER stores
# ---------------------------------------------------------------------------


def test_no_command_text_and_no_raw_path_survives_the_call():
    """THE RULING, ENFORCED. Every value returned must be a configured worker name or
    the outside marker — nothing free-form, so a secret in a command cannot reach the
    record."""
    secret = "curl -H 'Authorization: Bearer sk-live-abc123' https://x/ && cd /tmp/token-xyz789"
    repos = repos_touched("Bash", {"command": secret}, "/home/u/projects", ROOTS)

    allowed = set(ROOTS.values()) | {OUTSIDE_KNOWN_REPOS}
    assert set(repos) <= allowed, f"a value outside the enumeration escaped: {repos}"
    joined = " ".join(repos)
    assert "sk-live" not in joined and "token-xyz789" not in joined and "curl" not in joined


def test_the_enumeration_is_bounded_by_the_roster():
    repos = repos_touched("Bash", {"command": "ls /a /b /c /d /e /f"}, "/home/u/projects", ROOTS)

    assert set(repos) <= set(ROOTS.values()) | {OUTSIDE_KNOWN_REPOS}


# ---------------------------------------------------------------------------
# Not-measured must never read as touched-nothing
# ---------------------------------------------------------------------------


def test_an_unreadable_roster_yields_EMPTY_meaning_not_measured():
    assert repos_touched("Bash", {"command": "cd /anywhere"}, "/home/u", {}) == []
    assert repo_roots_from_workers(None) == {}
    assert repo_roots_from_workers("not a list") == {}


def test_a_worker_with_a_missing_name_or_path_is_skipped_not_crashed():
    roots = repo_roots_from_workers(
        [
            SimpleNamespace(name="ok", path="/tmp/x"),
            SimpleNamespace(name=None, path="/tmp/y"),
            SimpleNamespace(name="z", path=None),
        ]
    )

    assert roots == {"/tmp/x": "ok"}


@pytest.mark.parametrize("bad", [None, 42, "a string", []])
def test_a_malformed_tool_input_never_raises(bad):
    assert repos_touched("Bash", bad, "/home/u/projects", ROOTS) == ["project-root"]


# ---------------------------------------------------------------------------
# Coverage is stated, not implied
# ---------------------------------------------------------------------------


def test_the_undetectable_cases_are_named_in_the_module():
    """A guard whose coverage nobody has stated is worse than none. These limits live in
    the code so they cannot be lost from a resolution nobody re-reads."""
    joined = " ".join(UNDETECTABLE).lower()

    assert "variable" in joined
    assert "substitution" in joined
    assert "relative" in joined
    assert len(UNDETECTABLE) >= 5


def test_a_variable_built_path_is_genuinely_missed():
    """DEMONSTRATES a documented limit rather than asserting it. $REPO is not in the text,
    so this resolves to the cwd only — and that is the honest answer, not a silent one."""
    repos = repos_touched(
        "Bash", {"command": "cd $REPO/src && rm -rf ."}, "/home/u/projects", ROOTS
    )

    assert repos == ["project-root"], (
        "if this ever resolves $REPO, the UNDETECTABLE list is stale and coverage is "
        "wider than the resolution claims"
    )
