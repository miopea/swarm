#!/usr/bin/env python3
"""Report whether a base branch already CONTAINS every line a branch added.

WHY THIS EXISTS, AND WHY `git branch --merged` IS NOT ENOUGH.

`git branch --merged` is an ANCESTRY test: it asks whether the branch tip is
reachable from the base. When a PR is SQUASH-merged, the squash commit has
different content-identity from the branch tip, so the tip is never an ancestor
— not now, not ever. A repo that squash-merges therefore accumulates branches
that `--merged` calls unmerged forever, and `--no-merged` fills up with false
positives. rcg-architecture measured 23 such branches on 2026-08-16, of which
18 were squash-merged twins already fully present on main.

The question that actually matters is not "is the tip an ancestor" but "would I
lose anything by deleting this". That is CONTAINMENT: does the base tree already
hold every line this branch added? Containment answers STRANDED vs SUPERSEDED,
which ancestry cannot. On 2026-08-16 exactly one branch had no squash twin and
no open PR — the shape that looks like lost work — and containment showed it was
superseded, with a single unaccounted line: a heading the base carried in fuller
wording. Ancestry could not have distinguished that from genuinely lost work.

DELETION RULE: `-D` only what this reports CONTAINED. Anything else is reported
with its unaccounted lines and left alone.

Usage:
    verify-branch-containment.py [--repo PATH] [--base REF] [--all | BRANCH...]
    verify-branch-containment.py --repo ~/projects/rcg/rcg-architecture --all

Exit status: 0 if every branch examined is CONTAINED, 1 otherwise.
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def git(repo: str, *args: str) -> str:
    """Run a git command in `repo` and return stdout, or "" if it failed."""
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


class Unmeasurable(Exception):
    """The check could not run — never report this as CONTAINED.

    A tool that cannot resolve its inputs must say so rather than return the
    permissive answer. The first version of this script reported an unresolvable
    branch as "CONTAINED (0 added lines)" with exit 0 — i.e. it authorised a
    deletion on the strength of a measurement it had never taken. That is the
    same defect this script exists to catch, so it fails loudly instead.
    """


def added_lines(repo: str, base: str, branch: str) -> tuple[list[str], list[str]]:
    """Lines the branch ADDS relative to its merge-base with `base`.

    Returns (added_lines, touched_paths). Blank and whitespace-only lines are
    dropped: they carry no information and would otherwise match trivially.
    """
    for ref in (base, branch):
        if not git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip():
            raise Unmeasurable(f"cannot resolve ref {ref!r} in {repo}")
    merge_base = git(repo, "merge-base", base, branch).strip()
    if not merge_base:
        raise Unmeasurable(f"no merge-base between {base!r} and {branch!r} — unrelated histories?")
    diff = git(repo, "diff", "--unified=0", f"{merge_base}..{branch}")
    lines = [
        line[1:].strip()
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    paths = git(repo, "diff", "--name-only", f"{merge_base}..{branch}").split()
    return [line for line in lines if line], paths


def base_corpus(repo: str, base: str, paths: list[str]) -> set[str]:
    """Every line the base tree holds, for the paths the branch touched.

    Scoped to those paths rather than the whole tree: a line that reappears in an
    unrelated file is a coincidence, not evidence the work landed. A path absent
    from the base contributes nothing, which correctly makes a brand-new file
    read as NOT CONTAINED.
    """
    corpus: set[str] = set()
    for path in paths:
        blob = git(repo, "show", f"{base}:{path}")
        corpus.update(line.strip() for line in blob.splitlines() if line.strip())
    return corpus


def check(repo: str, base: str, branch: str) -> tuple[bool, list[str], int]:
    """Return (contained, unaccounted_lines, total_added)."""
    lines, paths = added_lines(repo, base, branch)
    if not lines:
        # Nothing added at all: nothing can be lost by deleting it.
        return True, [], 0
    corpus = base_corpus(repo, base, paths)
    unaccounted = [line for line in lines if line not in corpus]
    return not unaccounted, unaccounted, len(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="repository path")
    parser.add_argument("--base", default="origin/main", help="base ref")
    parser.add_argument("--all", action="store_true", help="every local branch")
    parser.add_argument("--show", type=int, default=5, help="unaccounted lines to print")
    parser.add_argument("branches", nargs="*")
    args = parser.parse_args()

    branches = args.branches
    if args.all:
        head = git(args.repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        branches = [
            b.strip()
            for b in git(args.repo, "branch", "--format=%(refname:short)").splitlines()
            if b.strip() and b.strip() != head
        ]
    if not branches:
        print("no branches to check", file=sys.stderr)
        return 1

    failures = 0
    for branch in branches:
        try:
            contained, unaccounted, total = check(args.repo, args.base, branch)
        except Unmeasurable as exc:
            failures += 1
            print(f"UNMEASURABLE   {branch}  ({exc}) — DO NOT DELETE")
            continue
        if contained:
            print(f"CONTAINED      {branch}  ({total} added lines all present in {args.base})")
        else:
            failures += 1
            print(f"NOT CONTAINED  {branch}  ({len(unaccounted)} of {total} added lines unaccounted)")
            for line in unaccounted[: args.show]:
                print(f"                 | {line[:100]}")
            if len(unaccounted) > args.show:
                print(f"                 | … {len(unaccounted) - args.show} more")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
