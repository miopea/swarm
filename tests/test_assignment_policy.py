"""The assignability rule is stated once, and no layer may restate it (#1 of four).

"Can this task be assigned?" used to be answered independently in three places:
``TaskBoard.assign``, ``TaskCoordinator.assign_task``, and ``/action/task/assign``,
which "normalised" the status first so the other two would accept it. Every divergence
between those copies has been a bug, and three of them landed in one evening:

* #894 / #1281 — ``is_available`` means "the auto-assign DRONE may take this". Using it
  to gate an OPERATOR's explicit routing answered a different question, so nobody could
  assign a HOLD task.
* the route worked around that gate by calling ``approve()`` on a BACKLOG task, which
  UN-PARKED it: "when I assign a worker and click save it moves to assigned, even if it
  was already on backlog."
* relaxing the board's copy without the coordinator's turned "silently un-parks" into
  "409, cannot assign" — the same bug in different clothes, because the second copy
  still refused.

``task_coordinator`` already carried a comment saying a change here must be made in
lockstep with the board. A rule that has to be edited in lockstep across layers should
be written down once, which is what ``swarm.tasks.policy`` is.

THE POLICY RETURNS THE REASON, not a boolean, so the refusal text cannot drift from the
check it explains. #939 cost the Queen an hour on the theory that the target worker's
load mattered — it never does; the target is not consulted — because one layer's
message said only "(not available)".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from swarm.tasks.policy import assignment_refusal, is_assignable
from swarm.tasks.task import SwarmTask, TaskStatus

_SRC = Path(__file__).parent.parent / "src" / "swarm"


def _task(status: TaskStatus, *, tags: list[str] | None = None, worker: str | None = None):
    t = SwarmTask(title="t", description="", tags=tags or [])
    t.status = status
    t.assigned_worker = worker
    return t


# --- the rule itself ----------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [TaskStatus.UNASSIGNED, TaskStatus.BACKLOG],
)
def test_assignable_statuses(status: TaskStatus):
    assert assignment_refusal(_task(status)) is None, f"{status.value} should be assignable"


def test_backlog_is_assignable_so_parked_work_can_be_routed():
    """The operator's decision. Safe because no dispatch path accepts BACKLOG and
    ``SwarmTask.assign`` preserves the status, so routing cannot un-park it."""
    assert is_assignable(_task(TaskStatus.BACKLOG))


def test_a_hold_task_is_refused_without_override_and_allowed_with_it():
    held = _task(TaskStatus.UNASSIGNED, tags=["hold"])
    refusal = assignment_refusal(held)
    assert refusal is not None and "hold" in refusal.lower()
    assert assignment_refusal(held, override_hold=True) is None, (
        "an explicit operator assignment must be able to take a HOLD task (#894/#1281)"
    )


@pytest.mark.parametrize("status", [TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.MIGRATED])
def test_closed_work_is_refused_and_the_reason_names_the_status(status: TaskStatus):
    refusal = assignment_refusal(_task(status))
    assert refusal is not None
    assert status.value in refusal, f"the refusal does not name {status.value}: {refusal}"


@pytest.mark.parametrize("status", [TaskStatus.ASSIGNED, TaskStatus.ACTIVE, TaskStatus.BLOCKED])
def test_owned_work_is_refused_and_points_at_release(status: TaskStatus):
    refusal = assignment_refusal(_task(status, worker="api"))
    assert refusal is not None
    assert status.value in refusal
    assert "release" in refusal.lower(), (
        f"the refusal must name what would resolve it (#1057): {refusal}"
    )


def test_no_refusal_blames_the_target_worker():
    """#939, as a property over EVERY refusal this policy can produce. The target's
    workload is never consulted, so no message may imply it is the problem."""
    cases = [
        _task(TaskStatus.DONE),
        _task(TaskStatus.FAILED),
        _task(TaskStatus.MIGRATED),
        _task(TaskStatus.ASSIGNED, worker="api"),
        _task(TaskStatus.ACTIVE, worker="api"),
        _task(TaskStatus.BLOCKED, worker="api"),
        _task(TaskStatus.UNASSIGNED, tags=["hold"]),
    ]
    for t in cases:
        refusal = assignment_refusal(t)
        assert refusal, "positive control: these must all refuse"
        assert "not available" not in refusal.lower(), (
            f"'not available' reads as though the TARGET is unavailable (#939): {refusal}"
        )


# --- no layer may restate the rule -------------------------------------------


def test_the_board_and_coordinator_both_defer_to_the_policy():
    board = (_SRC / "tasks" / "board.py").read_text()
    coord = (_SRC / "server" / "task_coordinator.py").read_text()
    for name, src in (("board.assign", board), ("coordinator.assign_task", coord)):
        assert "assignment_refusal(" in src, f"{name} no longer calls the shared policy"


def test_no_layer_re_implements_the_gate():
    """THE POINT OF THE CONSOLIDATION, asserted as a property so a fourth caller — a
    Jira sync, say — cannot quietly add a fifth copy of the rule.

    Looks for the specific shape the duplicates had: an is_available check combined
    with a hold/status special case. ``is_available`` itself stays legitimate for the
    DRONE's selection (``available_tasks``), which is a different question.
    """
    offenders = []
    for path in (_SRC / "tasks" / "board.py", _SRC / "server" / "task_coordinator.py"):
        src = path.read_text()
        code = "\n".join(ln for ln in src.split("\n") if not ln.strip().startswith(("#", '"', "*")))
        for m in re.finditer(r"[^\n]*is_available[^\n]*", code):
            line = m.group(0)
            if "override_hold" in line or "is_on_hold" in line:
                offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders, (
        "a layer is re-implementing the assignability rule instead of calling "
        f"swarm.tasks.policy.assignment_refusal: {offenders}"
    )


def test_the_route_no_longer_normalises_backlog_to_get_past_the_gate():
    """The workaround the duplication forced. Promoting a BACKLOG task so another
    layer's copy of the rule would accept it is what un-parked the operator's task."""
    src = (_SRC / "web" / "routes" / "tasks.py").read_text()
    body = src[src.index("async def handle_action_assign_task") :][:2500]
    code = "\n".join(ln for ln in body.split("\n") if not ln.strip().startswith("#"))
    assert "existing.approve()" not in code, (
        "the assign route promotes a BACKLOG task again to satisfy a downstream gate, "
        "which un-parks work the operator deliberately parked"
    )


# --- status transitions are also stated once (blocker 1 for the Jira work) ----


def test_the_transition_grid_lives_outside_the_web_route():
    """The blocker that had to clear before a Jira integration could be built.

    The whole transition ruleset lived in ``swarm/web/routes/tasks.py`` with exactly
    one caller, which made arbitrary status changes a DASHBOARD-ONLY capability. A Jira
    sync is fundamentally a status-transition consumer ("Done in Jira → close the
    task"), so it would have had to duplicate the grid or import from a web route —
    becoming the fourth copy of a rule whose every previous divergence was a bug
    (#1280, #1288, the 2026-08-07 un-parking).
    """
    route = (_SRC / "web" / "routes" / "tasks.py").read_text()
    code = "\n".join(ln for ln in route.split("\n") if not ln.strip().startswith(("#", '"', "*")))
    body = code[code.index("def _apply_status_change") : code.index("def _unsupported_reason")]
    assert "change_status(" in body, "the route no longer delegates to the shared path"
    for verb in ("demote_to_backlog", "mark_task_in_progress", "approve_task", "reopen_task"):
        assert verb not in body, (
            f"the web route dispatches {verb} itself again — the grid is back in a "
            f"route module and only the dashboard can perform transitions"
        )


def test_a_non_web_surface_can_perform_a_transition():
    """The capability the move exists to provide, asserted structurally: the executor
    is on TaskCoordinator, reachable by any surface, not behind an HTTP handler."""
    coord = (_SRC / "server" / "task_coordinator.py").read_text()
    assert "def change_status(" in coord, (
        "TaskCoordinator has no change_status; a Jira sync or MCP verb would have "
        "nothing to call but the dashboard's route"
    )
    assert "is_legal_transition(" in coord, (
        "change_status does not consult the shared policy, so it can diverge from the "
        "refusal the operator is shown"
    )


def test_the_refusal_wording_comes_from_the_policy_not_the_route():
    """One rule, one explanation — the same property proven for assignment."""
    route = (_SRC / "web" / "routes" / "tasks.py").read_text()
    body = route[
        route.index("def _unsupported_reason") : route.index("def _leave_blocked")
        if "def _leave_blocked" in route
        else route.index("def _unsupported_reason") + 900
    ]
    assert "status_transition_refusal(" in body, (
        "the route composes its own refusal text again, which can drift from the rule "
        "that produced it (#939's failure mode)"
    )


@pytest.mark.parametrize(
    ("current", "target", "legal"),
    [
        ("assigned", "active", True),
        ("unassigned", "active", False),
        ("blocked", "assigned", True),
        ("blocked", "done", False),
        ("active", "backlog", True),
        ("done", "assigned", True),
        ("assigned", "blocked", False),
    ],
)
def test_the_policy_answers_transition_legality_directly(current, target, legal):
    """Callable without a daemon, a board or a mock. That is what makes it usable from
    a Jira sync — and what keeps the grid testable without the MagicMock that
    invalidated the first version of the transition sweep."""
    from swarm.tasks.policy import is_legal_transition

    assert is_legal_transition(current, target) is legal, f"{current} → {target}"
