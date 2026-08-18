"""Which REPO did a command touch? Bounded enumeration, no paths, no command text.

#1698. The ticket asked whether ``workers.path`` should be a boundary. Two measurements
say it cannot be, as defined:

1. THE PROCESS CWD IS THE WRONG OBSERVABLE. project-root's live cwd never left
   /home/bschleifer/projects while it did implementation, workflow edits and three
   production deploys inside bfg-ops-console's repo. ``cd /other/repo && cmd`` runs in a
   subshell; the parent's cwd never moves. The Phase A instrumentation recorded
   out_of_path=False for every one of those calls — correctly, and uselessly.
2. THE FIELD CONTAINS THE FLEET. project-root's configured path is ~/projects, which
   CONTAINS 24 of the other 25 workers' paths. Under a raw containment test that worker
   can never be out of path.

So the signal lives in the COMMAND'S TARGETS, not in process state — and ownership has to
be LONGEST MATCH, or every path in the fleet resolves to project-root.

WHAT IS STORED, AND WHAT DELIBERATELY IS NOT (Queen ruling 2026-08-18):

* NOT COMMAND TEXT. Commands carry secrets — ``op read``, ``az`` invocations, connection
  strings, bearer tokens in curl headers. A cross-repo audit trail that becomes a
  credential archive is a worse problem than the one it solves, and this fleet has already
  had a Postgres credential exposed in a transcript.
* NOT RAW PATHS, for a weaker form of the same reason: paths leak (/tmp/token-abc123), and
  an unbounded string field invites the drift that makes it one.
* THE RESOLVED REPO IDENTITY ONLY — one of the configured worker names, or
  ``OUTSIDE_KNOWN_REPOS``. Bounded, enumerable, nothing worth stealing, and it answers the
  only question a confinement guard needs: which repo did this worker operate in?

COVERAGE IS PARTIAL AND THE LIMITS ARE NAMED IN ``UNDETECTABLE`` BELOW. A guard trusted
beyond its reach is worse than none, so the gaps are part of the module rather than a
footnote someone has to find.
"""

from __future__ import annotations

import os
import re
from typing import Any

# The value for a path that resolves under no configured worker root. This is the
# INTERESTING case, not the leftover one: work outside every known repo is either a
# genuine excursion or a path this module failed to attribute, and both want looking at.
OUTSIDE_KNOWN_REPOS = "outside-known-repos"

# A path this module could not resolve at all (unreadable, malformed). Kept DISTINCT from
# OUTSIDE_KNOWN_REPOS so a rate built from these is not padded with unknowns — the same
# reason ``out_of_path`` returns None rather than False.
UNRESOLVED = "unresolved"

# `cd <path>` and `git -C <path>` are the two shapes that move a command's target without
# moving the process. Both were present in the #1844 incident.
_RE_CD = re.compile(r"(?:^|[;&|]\s*)cd\s+(?P<path>[^\s;&|]+)")
_RE_GIT_C = re.compile(r"\bgit\s+-C\s+(?P<path>[^\s;&|]+)")
# Absolute or ~-rooted tokens anywhere in the command. Deliberately greedy about finding
# candidates and strict about resolving them: a token that resolves under no root
# contributes OUTSIDE_KNOWN_REPOS, which is a signal, not noise.
_RE_ABS_TOKEN = re.compile(r"(?<![\w=])(?P<path>(?:~|/)[^\s;&|'\"<>()]{2,})")

#: What this CANNOT see. Stated here because the Queen's instruction was that a guard
#: whose coverage nobody has stated is worse than none.
UNDETECTABLE: tuple[str, ...] = (
    "paths built from shell variables ($REPO/src, ${DIR}) — the value is not in the text",
    "paths produced by command substitution ($(git rev-parse --show-toplevel))",
    "relative paths that depend on a cwd this module was not given",
    "paths inside a quoted string handed to a subshell or to ssh",
    "symlinks pointing outside the root they appear to be under",
    "anything a tool does internally after being handed a directory",
)


def repo_roots_from_workers(workers: Any) -> dict[str, str]:
    """Map realpath → owning worker name, for every configured worker.

    Returns {} when the roster cannot be read, which callers must treat as "cannot tell"
    rather than "nothing owns anything".
    """
    roots: dict[str, str] = {}
    if not isinstance(workers, (list, tuple)):
        return roots
    for worker in workers:
        name = getattr(worker, "name", None)
        path = getattr(worker, "path", None)
        if not isinstance(name, str) or not isinstance(path, str) or not name or not path:
            continue
        try:
            roots[os.path.realpath(os.path.expanduser(path))] = name
        except (OSError, ValueError):
            continue
    return roots


def resolve_repo(path: str, roots: dict[str, str]) -> str:
    """Which configured repo owns *path*? LONGEST MATCH WINS.

    Longest-match is the whole reason this is not a one-liner. project-root's root is
    ~/projects, an ancestor of 24 other workers' roots, so a shortest- or first-match
    resolver attributes the entire fleet to it — which is exactly how #1646's attribution
    bug read 77.4% of calls as project-root.

    Requires a trailing separator on the prefix test: ``rcg-platform`` must NOT own
    ``rcg-platform-api``. Those are sibling worktrees of the fleet's most active workers,
    and a resolver firing on them would be switched off within a day.
    """
    if not path or not roots:
        return UNRESOLVED
    try:
        expanded = os.path.expanduser(path)
    except (OSError, ValueError):
        return UNRESOLVED
    # A RELATIVE PATH IS UNRESOLVABLE HERE, NOT RESOLVABLE AGAINST US. os.path.realpath
    # would silently resolve it against the DAEMON's cwd — attributing "$REPO/src" or
    # "../other" to whatever directory this process happens to be sitting in, which is a
    # confident wrong answer of exactly the kind this module exists to stop producing.
    # The worker's cwd is captured separately and contributes its own entry.
    if not os.path.isabs(expanded):
        return UNRESOLVED
    try:
        here = os.path.realpath(expanded)
    except (OSError, ValueError):
        return UNRESOLVED
    best_root = ""
    best_name = OUTSIDE_KNOWN_REPOS
    for root, name in roots.items():
        if (here == root or here.startswith(root + os.sep)) and len(root) > len(best_root):
            best_root, best_name = root, name
    return best_name


def candidate_paths(tool_name: str, tool_input: Any, cwd: str) -> list[str]:
    """Paths a tool call plausibly targets. Never returned to a caller that stores them.

    The command string is read here and DISCARDED — only the resolved repo identity
    leaves this module.
    """
    paths: list[str] = []
    if isinstance(tool_input, dict):
        for key in ("file_path", "notebook_path", "path"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                paths.append(value)
        command = tool_input.get("command")
        if isinstance(command, str) and command:
            for pattern in (_RE_CD, _RE_GIT_C, _RE_ABS_TOKEN):
                paths.extend(m.group("path") for m in pattern.finditer(command))
    if cwd:
        paths.append(cwd)
    return paths


def repos_touched(tool_name: str, tool_input: Any, cwd: str, roots: dict[str, str]) -> list[str]:
    """The bounded set of repo identities this call touched, sorted for a stable record.

    Returns [] when nothing could be resolved — an empty list means NOT MEASURED, and a
    caller must not read it as "touched nothing".
    """
    if not roots:
        return []
    seen = {resolve_repo(p, roots) for p in candidate_paths(tool_name, tool_input, cwd)}
    seen.discard(UNRESOLVED)
    return sorted(seen)
