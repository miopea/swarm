"""#1910 — two warnings fired every cycle and neither could be acted on.

Same shape at opposite ends: one reported something that was NOT TRUE, the other reported
something true that NOBODY COULD ACT ON. Both fired four times into the Queen's inbox in
one night, unchanged. Neither is harmful; both are corrosive, because a channel that
reports the same unactionable thing indefinitely trains its reader to skim — and the same
sweep carries findings that do matter.

1. THE DRIFT WARNING SAID "your on-disk file has local edits" WHEN IT HAD NONE. Measured
   on the live queen workdir: CLAUDE.md and CLAUDE.md.shipped-latest are both 11,708 bytes
   and byte-identical; the marker is 10,461. The decision matrix asked `shipped_latest vs
   marker` and `on_disk vs marker` and NEVER `on_disk vs shipped_latest` — so a file
   already updated to the newest ship read as locally edited, because the marker still
   held the older one. Both offered remedies (--accept-shipped, --keep-local) presuppose
   something to reconcile when there is nothing.

2. THE CONTAINMENT FINDING NEVER NAMED THE REPO. It reported
   "CONTAINED origin/fix-service-worker-reregister-loop" with the remedy
   "git push origin --delete ...", which assumes the reader is standing in the right repo.
   The Queen is not in any repo. Unactionable by construction, which is why it was
   reported four times and fixed zero. (The repo turned out to be `swarm` itself.)

THE SWEEP STILL DOES NOT DELETE. Its caveat is correct and preserved: deleting a branch a
dependent PR points at closes that PR irrecoverably. The fix is to make the report
routable, not to make the tool act.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from swarm.queen.runtime import ReconcileAction, reconcile_queen_claude_md

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify-branch-containment.py"


def _workdir(tmp_path: Path, on_disk: str, marker: str) -> Path:
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "CLAUDE.md").write_text(on_disk)
    (wd / ".claude_md_shipped").write_text(marker)
    return wd


# ---------------------------------------------------------------------------
# 1. The drift detector
# ---------------------------------------------------------------------------


def test_no_drift_when_on_disk_already_matches_the_newest_ship(tmp_path):
    """THE LIVE CASE. on_disk == shipped_latest, marker stale. There is nothing to
    reconcile and the warning must not fire."""
    wd = _workdir(tmp_path, on_disk="NEW", marker="OLD")

    result = reconcile_queen_claude_md(wd, shipped_latest="NEW")

    assert result.action is ReconcileAction.NO_OP
    assert not (wd / "CLAUDE.md.shipped-latest").exists(), "drift files were written anyway"


def test_the_stale_marker_is_resynced_so_it_stops_recurring(tmp_path):
    """Reporting no-op while leaving the marker stale would fire again next cycle —
    silencing the symptom and keeping the cause."""
    wd = _workdir(tmp_path, on_disk="NEW", marker="OLD")

    reconcile_queen_claude_md(wd, shipped_latest="NEW")

    assert (wd / ".claude_md_shipped").read_text() == "NEW"
    # And a second pass is a plain no-op, not a second resync.
    assert reconcile_queen_claude_md(wd, shipped_latest="NEW").action is ReconcileAction.NO_OP


def test_POSITIVE_CONTROL_genuine_local_edits_STILL_flag_drift(tmp_path):
    """THE ACCEPTANCE CRITERION THE TICKET NAMED. A detector silenced by a bug looks
    exactly like a detector silenced by a fix — catalogued as entry 9 two days ago. This
    is the test that tells them apart."""
    wd = _workdir(tmp_path, on_disk="MY OWN EDITS", marker="OLD")

    result = reconcile_queen_claude_md(wd, shipped_latest="NEW")

    assert result.action is ReconcileAction.DRIFT_FLAGGED
    assert (wd / "CLAUDE.md.shipped-latest").read_text() == "NEW"
    assert (wd / "CLAUDE.md.shipped-last").read_text() == "OLD"
    assert (wd / "CLAUDE.md").read_text() == "MY OWN EDITS", "local edits were clobbered"


def test_a_clean_upgrade_still_auto_updates(tmp_path):
    """The row above the new one must keep working: no local edits, so take the ship."""
    wd = _workdir(tmp_path, on_disk="OLD", marker="OLD")

    result = reconcile_queen_claude_md(wd, shipped_latest="NEW")

    assert result.action is ReconcileAction.AUTO_UPDATED
    assert (wd / "CLAUDE.md").read_text() == "NEW"


def test_an_unchanged_ship_is_still_a_no_op(tmp_path):
    wd = _workdir(tmp_path, on_disk="ANYTHING", marker="OLD")

    assert reconcile_queen_claude_md(wd, shipped_latest="OLD").action is ReconcileAction.NO_OP


# ---------------------------------------------------------------------------
# 2. The containment finding
# ---------------------------------------------------------------------------


def _repo(tmp_path: Path, name: str) -> tuple[Path, str]:
    """A real git repo, returned with its ACTUAL default branch.

    Not assumed: git's default is `master` on some installs and `main` on others, and
    hard-coding either makes the test pass or fail for a reason that has nothing to do
    with what it is checking. I made exactly that assumption an hour ago probing this
    same script and got "fatal: invalid reference: main".
    """
    repo = tmp_path / name
    repo.mkdir()

    def run(*a: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(repo), *a], capture_output=True, text=True, check=True
        )

    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (repo / "a.txt").write_text("base\n")
    run("add", "a.txt")
    run("commit", "-qm", "base")
    base = run("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    return repo, base


def _run(repo: Path, *args: str) -> str:
    """stdout AND stderr — a helper that hides the failure is how an empty result gets
    read as a clean one, which is the defect this very ticket is about."""
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--repo", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.stdout + proc.stderr


def test_the_finding_names_the_repository(tmp_path):
    """One field, and it is the difference between a report and a routable report."""
    repo, base = _repo(tmp_path, "bfg-ops-console")
    subprocess.run(["git", "-C", str(repo), "branch", "some-branch"], check=True)

    out = _run(repo, "--all", "--base", base)

    assert "[bfg-ops-console]" in out or "no branches to check" in out
    if "CONTAINED" in out or "NOT CONTAINED" in out:
        assert "[bfg-ops-console]" in out


def test_the_remedy_is_runnable_from_outside_the_repo(tmp_path):
    """`git push origin --delete X` assumes the reader is standing in the repo. The Queen
    is not in any repo — and the sweep is invoked with `--repo .` from a directory she
    does not share, so an unresolved path routes nowhere either."""
    repo, base = _repo(tmp_path, "some-repo")
    subprocess.run(["git", "-C", str(repo), "branch", "stale-branch"], check=True)

    out = _run(repo, "--all", "--base", base, "--fail-on", "stale")

    if "stale branch(es)" in out:
        assert f"git -C {repo}" in out, "the remedy does not name the repo it applies to"
        assert "    git push origin --delete" not in out, "bare remedy still emitted"


def test_the_do_not_delete_caveat_is_preserved():
    """EXPLICIT ACCEPTANCE CRITERION. The fix is to make the report routable, not to make
    the tool act — deleting a branch a dependent PR points at closes that PR
    irrecoverably."""
    source = _SCRIPT.read_text()

    assert "closes that PR irrecoverably" in source
    assert "this reports, it does not delete" in source


def test_the_sweep_does_not_run_a_delete_anywhere():
    """The behavioural half: no deletion verb is invoked by the script itself."""
    source = _SCRIPT.read_text()
    executed = [ln for ln in source.splitlines() if "git(" in ln and "delete" in ln.lower()]

    assert executed == [], f"the sweep executes a deletion: {executed}"


@pytest.mark.parametrize("mode", ["--all", "--remote"])
def test_both_modes_carry_the_repo_label(tmp_path, mode):
    repo, base = _repo(tmp_path, "labelled-repo")
    subprocess.run(["git", "-C", str(repo), "branch", "another"], check=True)

    out = _run(repo, mode, "--base", base)

    assert "[labelled-repo]" in out or "no branches to check" in out
