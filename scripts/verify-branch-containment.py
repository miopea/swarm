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

HOW TO READ "N LINES ABSENT FROM MAIN" — THE TOOL CANNOT TELL YOU, SO READ THEM.

An unaccounted line is either a correction main deliberately made, or a finding
that was written and never landed. Containment proves the line is ABSENT; it
cannot prove whether it SHOULD be. That distinction is a human read, every time.

Measured once, and the denominator matters: on rcg-architecture (a documentation
repo), 9 of 9 branches with unaccounted lines turned out to be deliberate
corrections — retractions, operator rulings, withdrawn advice — and none was lost
work. So on a doc repo, where corrections land as new commits, "absent from main"
correlates with "main decided against it" rather than "lost".

DO NOT GENERALISE THAT TO CODE, where an absent line is far more often unfinished
work. And 9 of 9 is suggestive, not a law: a uniform result on a sample of nine
is a reason to expect the pattern, not a reason to skip the read.

WHY A STALE BRANCH IS MORE THAN UNTIDY. One of those nine held a finding claiming
"a D365 role write ARMS a demotion", escalated to the operator as an urgent live
security issue and REFUTED AND RETRACTED hours later. Main records the retraction;
the branch does not. Anyone checking that branch out would have read a falsified
claim in a form that reads as current, with no signal it had been withdrawn. A
stale branch can preserve a retracted claim past its retraction.

Related, from the same sweep: a retraction in one repo does not automatically
reach a rule that lives in ANOTHER. Verify the cross-repo copy rather than
assuming the correction travelled.

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


def pr_heads(slug: str) -> list[str]:
    """Head branch names of OPEN PRs, read live from GitHub.

    An open PR's head is work in flight, not a stale branch, and deleting one is
    how a dependent PR gets irrecoverably closed. Read live rather than passed in
    as a list: a PR opened since the caller last looked is exactly the case a
    stale list gets wrong.
    """
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            slug,
            "--state",
            "open",
            "--json",
            "headRefName",
            "--jq",
            ".[].headRefName",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise Unmeasurable(f"cannot read open PRs for {slug}: {result.stderr.strip()[:120]}")
    return result.stdout.split()


def local_branches(repo: str) -> list[str]:
    """Every local branch except the checked-out one (git refuses to delete that)."""
    head = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return [
        b.strip()
        for b in git(repo, "branch", "--format=%(refname:short)").splitlines()
        if b.strip() and b.strip() != head
    ]


def remote_branches(repo: str, base: str) -> list[str]:
    """Every remote branch except the base.

    Prunes first. Remote-TRACKING refs go stale: a branch deleted on the server
    keeps its refs/remotes entry until someone prunes, so an unpruned run reports
    on branches that no longer exist. CI clones are always fresh; humans are not.
    """
    git(repo, "fetch", "origin", "--prune", "--quiet")
    base_short = base.split("/", 1)[-1]
    return [
        ref
        for ref in git(
            repo, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"
        ).split()
        # "origin" itself and "origin/HEAD" are pointers, not branches.
        if ref not in (base, "origin")
        and not ref.endswith("/HEAD")
        and ref.split("/", 1)[-1] != base_short
    ]


def resolve_branches(args: argparse.Namespace) -> list[str]:
    """Branches to examine, from the explicit list plus whichever modes are on."""
    branches = list(args.branches)
    if args.all:
        branches = local_branches(args.repo)
    if args.remote:
        branches += remote_branches(args.repo, args.base)
    return branches


def check(repo: str, base: str, branch: str) -> tuple[bool, list[str], int]:
    """Return (contained, unaccounted_lines, total_added)."""
    lines, paths = added_lines(repo, base, branch)
    if not lines:
        # Nothing added at all: nothing can be lost by deleting it.
        return True, [], 0
    corpus = base_corpus(repo, base, paths)
    unaccounted = [line for line in lines if line not in corpus]
    return not unaccounted, unaccounted, len(lines)


def report_branch(args: argparse.Namespace, branch: str) -> tuple[bool, int]:
    """Print one branch's verdict. Returns (is_contained, failure_count)."""
    try:
        contained, unaccounted, total = check(args.repo, args.base, branch)
    except Unmeasurable as exc:
        # Unmeasurable always fails, under EITHER polarity: a result we could not
        # take must never read as "safe to delete" nor as "all clean".
        print(f"UNMEASURABLE   {branch}  ({exc}) — DO NOT DELETE")
        return False, 1

    if contained:
        print(f"CONTAINED      {branch}  ({total} added lines all present in {args.base})")
        return True, 1 if args.fail_on == "stale" else 0

    print(f"NOT CONTAINED  {branch}  ({len(unaccounted)} of {total} added lines unaccounted)")
    for line in unaccounted[: args.show]:
        print(f"                 | {line[:100]}")
    if len(unaccounted) > args.show:
        print(f"                 | … {len(unaccounted) - args.show} more")
    return False, 1 if args.fail_on == "unaccounted" else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="repository path")
    parser.add_argument("--base", default="origin/main", help="base ref")
    parser.add_argument("--all", action="store_true", help="every local branch")
    parser.add_argument(
        "--remote",
        action="store_true",
        help=(
            "every remote branch. The only mode CI can use: a CI clone has no "
            "local branches, so --all there silently finds nothing and calls it clean."
        ),
    )
    parser.add_argument(
        "--skip-pr-heads",
        metavar="OWNER/REPO",
        help="exclude open-PR head branches, read live via gh; they are work in flight, not stale",
    )
    parser.add_argument(
        "--fail-on",
        choices=("unaccounted", "stale"),
        default="unaccounted",
        help=(
            "which condition exits non-zero. THE TWO USES HAVE OPPOSITE POLARITY. "
            "'unaccounted' (default) answers 'is it safe to delete what I named?' — "
            "NOT CONTAINED fails. 'stale' answers 'is anything dead lying around?' — "
            "CONTAINED fails, because a branch whose every line is already on main IS "
            "the stale one. Using the wrong polarity gives a green run that means the "
            "opposite of what the caller wanted."
        ),
    )
    parser.add_argument("--show", type=int, default=5, help="unaccounted lines to print")
    parser.add_argument("branches", nargs="*")
    args = parser.parse_args()

    branches = resolve_branches(args)
    if args.skip_pr_heads:
        try:
            heads = set(pr_heads(args.skip_pr_heads))
        except Unmeasurable as exc:
            # Never fall through to a clean report: without the PR set we cannot
            # tell a stale branch from work in flight, and the permissive answer
            # is the dangerous one.
            print(f"UNMEASURABLE   open-PR set ({exc}) — refusing to classify anything")
            return 1
        if heads:
            kept = [b for b in branches if b.split("/", 1)[-1] not in heads and b not in heads]
            for dropped in sorted(set(branches) - set(kept)):
                print(f"SKIPPED        {dropped}  (open-PR head — work in flight)")
            branches = kept
    if not branches:
        print("no branches to check", file=sys.stderr)
        return 1

    failures = 0
    stale = []
    for branch in branches:
        contained, failed = report_branch(args, branch)
        if contained:
            stale.append(branch)
        failures += failed

    if args.fail_on == "stale" and stale:
        print()
        print(f"{len(stale)} stale branch(es) — every added line is already on {args.base}.")
        print("Delete them by hand; this reports, it does not delete, because deleting a")
        print("branch a dependent PR points at closes that PR irrecoverably.")
        for branch in stale:
            print(
                f"    git push origin --delete {branch.split('/', 1)[-1]}"
                if branch.startswith("origin/")
                else f"    git branch -D {branch}"
            )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
