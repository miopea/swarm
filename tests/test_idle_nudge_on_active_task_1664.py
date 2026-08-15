"""#1664 — the IdleWatcher nudged a worker whose task was already ACTIVE.

REPRODUCED FROM THE REAL EVENT, not invented. sculpt-studio, task #1656 (`status=active`):
    17:37:45  AUTO_NUDGE_SKIPPED  finished a turn 228s ago (within 600s window)
    17:40:45  AUTO_NUDGE_SKIPPED  finished a turn 408s ago (within 600s window)
    17:43:46  AUTO_NUDGE_SKIPPED  finished a turn 589s ago (within 600s window)
    17:46:48  AUTO_NUDGE          idle with active task(s): #1656      <- fired

THE SIGNAL, AND WHY IT DISAGREED WITH status=active (AC1). The watcher keys off TWO things,
neither of which is task status:
  · `worker.display_state in {RESTING, SLEEPING}` — a PTY-derived state, and
  · the worker owning any task in `task_board.assigned_or_active_tasks`.
That bucket DELIBERATELY includes ACTIVE tasks, so an ACTIVE task cannot suppress a nudge.
The only activity signal in the pipeline is `state_duration < idle_nudge_activity_window`
(600s), which measures time since the PTY last changed state — so once a worker rests past
600s while genuinely working (a long build, a big edit, thinking), the guard stops
suppressing and the nudge fires on work that is in progress.

The nudge then told the worker to call `swarm_start_task`, which for an ACTIVE task answers
"already in progress" — advice that cannot be followed, on a fact the board already knew.
"""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock

import pytest

from swarm.drones.idle_watcher import _nudge_message


class _Task:
    def __init__(self, number: int, status: str, worker: str = "sculpt-studio") -> None:
        self.number = number
        self.status = MagicMock()
        self.status.value = status
        self.assigned_worker = worker
        self.is_on_hold = False
        self.id = f"t{number}"
        self.title = f"task {number}"


# ---------------------------------------------------------------------------
# AC3 — the nudge must not prescribe a verb that cannot be followed
# ---------------------------------------------------------------------------


def test_the_nudge_does_not_tell_an_active_worker_to_start_its_active_task():
    """THE REPORTED DEFECT. `swarm_start_task` on an ACTIVE task answers 'already in
    progress' — so the instruction is a no-op, and it asserts the board shows the task as
    queued when the board shows it ACTIVE."""
    msg = _nudge_message([1656], all_active=True)

    assert "swarm_start_task" not in msg
    assert "queued" not in msg.lower()


def test_the_nudge_still_prescribes_the_start_verb_when_a_task_is_merely_assigned():
    """POSITIVE CONTROL. The start-verb hint exists because an ASSIGNED-but-never-started
    task is real and common; removing it entirely would trade this bug for that one."""
    msg = _nudge_message([1656], all_active=False)

    assert "swarm_start_task" in msg


def test_the_nudge_still_asks_for_status_and_messages_either_way():
    """The core of the nudge — check your board, check your inbox — is what makes it
    actionable at all, and must survive both branches."""
    for all_active in (True, False):
        msg = _nudge_message([1656], all_active=all_active)
        assert "swarm_task_status" in msg
        assert "swarm_check_messages" in msg


def test_the_active_wording_names_the_task_as_in_progress_not_open():
    """When every task IS active, saying so beats the deliberately-vague 'open' — the
    vagueness was correct while the message covered both cases and is not once it does
    not."""
    msg = _nudge_message([1656], all_active=True)

    assert "#1656" in msg
    assert "in progress" in msg.lower()


# ---------------------------------------------------------------------------
# AC2 — recent commits suppress the nudge
# ---------------------------------------------------------------------------


def _watcher(**kw):
    from swarm.drones.idle_watcher import IdleWatcher

    cfg = MagicMock()
    cfg.idle_nudge_interval_seconds = 60.0
    cfg.idle_nudge_debounce_seconds = 0.0
    cfg.idle_nudge_activity_window_seconds = 600.0
    cfg.assign_operator_engagement_minutes = 0.0
    cfg.idle_nudge_max_repeats = 3
    defaults = {
        "drone_config": cfg,
        "task_board": MagicMock(),
        "drone_log": MagicMock(),
        "send_to_worker": MagicMock(),
    }
    defaults.update(kw)
    return IdleWatcher(**defaults)


def _resting_worker(name: str = "sculpt-studio", resting_for: float = 900.0) -> MagicMock:
    from swarm.worker.worker import WorkerState

    w = MagicMock()
    w.name = name
    w.display_state = WorkerState.RESTING
    w.state_duration = resting_for
    w.path = "/tmp/repo"
    return w


def test_a_recent_commit_suppresses_the_nudge():
    """AC2. A worker resting past the 600s window but committing 30s ago is working, not
    idle — the state machine simply cannot see an editor or a test run."""
    watcher = _watcher(commit_activity_check=lambda _w: 30.0)

    reason = watcher._suppression_reason(_resting_worker())

    assert reason is not None
    assert "commit" in reason.lower()


def test_an_old_commit_does_not_suppress_the_nudge():
    """POSITIVE CONTROL, and the reason this watcher exists (#225). A worker that stopped
    and did not come back must still be caught — a commit-based guard that suppressed
    forever would disable the whole sweep."""
    watcher = _watcher(commit_activity_check=lambda _w: 99999.0)

    assert watcher._suppression_reason(_resting_worker()) is None


def test_no_commit_information_falls_through_to_nudging():
    """FAIL-SAFE DIRECTION. `None` means 'could not tell' — not 'recently active'. Absence
    of evidence must not become evidence of work, which is the mistake the `float(MagicMock)`
    coercion made in the #1615 guard directly above this one."""
    watcher = _watcher(commit_activity_check=lambda _w: None)

    assert watcher._suppression_reason(_resting_worker()) is None


def test_a_raising_commit_check_never_breaks_the_sweep():
    """Every other injected check in this class swallows exceptions; a git call is the most
    likely of them to fail (missing repo, detached worktree, git absent)."""

    def _boom(_w):
        raise RuntimeError("git exploded")

    watcher = _watcher(commit_activity_check=_boom)

    assert watcher._suppression_reason(_resting_worker()) is None


def test_the_existing_turn_recency_guard_still_wins_first():
    """NO REGRESSION on #1615: a worker that just finished a turn is suppressed for that
    reason, regardless of commits."""
    watcher = _watcher(commit_activity_check=lambda _w: 99999.0)

    reason = watcher._suppression_reason(_resting_worker(resting_for=30.0))

    assert reason is not None
    assert "finished a turn" in reason


@pytest.mark.parametrize("bad", [True, False, "recently", object()])
def test_a_non_numeric_commit_age_is_not_treated_as_recent(bad):
    """Same trap as #1615's `float(MagicMock())` returning 1.0: a value that is not
    genuinely numeric is not evidence of a commit, and must fall through to nudging.
    `True` is in here on purpose — bool IS an int in Python."""
    watcher = _watcher(commit_activity_check=lambda _w: bad)

    assert watcher._suppression_reason(_resting_worker()) is None


# ---------------------------------------------------------------------------
# AC2 — demonstrated against a REAL repository, not asserted from the code
# ---------------------------------------------------------------------------


def _git(repo, *args):
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
        },
    )


def test_commit_age_is_measured_from_a_real_commit(tmp_path):
    """A REPRODUCTION, not a mock: init a real repo, make a real commit, and confirm the
    signal the suppression depends on actually reads it. The mock-based tests above prove
    the WIRING; this proves the SIGNAL, and a guard whose signal is only ever faked is how
    an inert control ships looking operational."""
    from swarm.drones.idle_watcher import commit_age_seconds

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "f.txt").write_text("x")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "first")

    worker = MagicMock()
    worker.path = str(repo)

    age = commit_age_seconds(worker)
    assert age is not None, "a real commit produced no signal"
    assert age < 60, f"a commit made seconds ago reported {age}s"


def test_a_directory_that_is_not_a_repo_reports_unknown(tmp_path):
    """POSITIVE CONTROL for the None path — and None must mean 'could not tell', which the
    suppression treats as 'nudge', not 'suppress'."""
    from swarm.drones.idle_watcher import commit_age_seconds

    worker = MagicMock()
    worker.path = str(tmp_path)

    assert commit_age_seconds(worker) is None


def test_a_git_worktree_is_read_through_its_gitdir_pointer(tmp_path):
    """Several workers run on worktrees, where `.git` is a FILE containing `gitdir: …`.
    Missing that case would silently return None for exactly those workers — the failure
    would look like 'no commits' rather than 'unsupported layout'."""
    from swarm.drones.idle_watcher import commit_age_seconds

    repo = tmp_path / "main"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "f.txt").write_text("x")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "first")
    tree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(tree), "-b", "side")

    worker = MagicMock()
    worker.path = str(tree)

    age = commit_age_seconds(worker)
    assert age is not None, "worktree layout produced no signal (.git is a file there)"
    assert age < 60
