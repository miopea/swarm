#!/usr/bin/env bash
# Report stale LOCAL branches in the shared working copies on this machine.
#
# WHY THIS IS NOT A CI JOB. GitHub Actions runs on a fresh clone and can only see
# remote branches. On 2026-08-16, 9 of the 13 branches flagged in
# rcg-architecture's shared checkout existed ONLY in that working copy — no
# workflow could ever have reached them. .github/workflows/branch-hygiene.yml
# covers the remote half; this covers the half CI is structurally blind to, and
# it has to run where the checkouts live.
#
# REPORTS ONLY. It never deletes. A shared checkout has one git identity and many
# workers, so a branch you cannot attribute may be someone's work in flight —
# #1669 began exactly there, and the work survived only because git refused. The
# containment result tells a human what is safe; the human decides.
#
# Usage:   sweep-shared-checkouts.sh [REPO_DIR...]      (defaults to ~/projects/rcg/*)
# Exit:    0 = nothing stale anywhere; 1 = at least one CONTAINED branch to review;
#          2 = a checkout was skipped because it was dirty (investigate, do not sweep).

set -uo pipefail

CHECK="$(dirname "$(readlink -f "$0")")/verify-branch-containment.py"
[ -x "$CHECK" ] || { echo "FATAL: containment checker not found at $CHECK" >&2; exit 2; }

repos=("$@")
[ ${#repos[@]} -eq 0 ] && repos=("$HOME"/projects/rcg/*/)

stale=0
skipped=0

for repo in "${repos[@]}"; do
  [ -d "$repo/.git" ] || continue
  name=$(basename "$repo")

  # THE THREE CLEANLINESS CHECKS, in the order that matters. The default
  # porcelain collapses an untracked DIRECTORY to a single entry, and a stash is
  # local state invisible to both. A dirty shared checkout is a stop condition,
  # not a thing to work around.
  dirty=$(git -C "$repo" status --porcelain --untracked-files=all 2>/dev/null | wc -l)
  stashed=$(git -C "$repo" stash list 2>/dev/null | wc -l)
  if [ "$dirty" -ne 0 ] || [ "$stashed" -ne 0 ]; then
    echo "SKIPPED  $name — dirty ($dirty uncommitted, $stashed stashed). Find the owner; do not sweep."
    skipped=1
    continue
  fi

  slug=$(git -C "$repo" remote get-url origin 2>/dev/null \
         | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')
  [ -n "$slug" ] || { echo "SKIPPED  $name — no origin remote"; skipped=1; continue; }

  echo "=== $name ($slug) ==="
  out=$(python3 "$CHECK" --repo "$repo" --base origin/main --all \
        --skip-pr-heads "$slug" --show 2 2>&1)
  echo "$out"

  n=$(printf '%s\n' "$out" | grep -c '^CONTAINED')
  if [ "$n" -gt 0 ]; then
    echo "--> $n local branch(es) fully contained in main and safe for a human to delete:"
    printf '%s\n' "$out" | awk '/^CONTAINED/{print "      git -C '"$repo"' branch -D " $2}'
    stale=1
  fi
  echo
done

[ "$skipped" -ne 0 ] && exit 2
exit "$stale"
