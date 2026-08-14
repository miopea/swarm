"""Every TaskStatus needs a reachable, non-falsifying exit. Audit #1104.

THE PATTERN, seven instances: a verb set with an ENTRY and no EXIT, or a state
reachable but not re-shapeable. Full enumeration and assessment in
docs/specs/taskboard-state-machine-audit.md.

WHY A UNIT TEST ON THE BOARD COULD NEVER HAVE CAUGHT THIS. ``board.unblock``
exists, works, and is covered by tests/test_board.py. It is also called by
NOTHING in src/ — so BLOCKED's only non-falsifying exit is dead code, reachable
from neither the worker nor the Queen surface. The board tests pass. The verb is
unreachable. Testing a transition proves the transition; it says nothing about
whether any surface can invoke it, and property (b) is precisely about that.

So this file asserts REACHABILITY, not correctness of transitions: for each
status, which exits can actually be invoked from each surface, and whether the
reachable ones falsify history.

The known gap is marked ``xfail(strict=True)`` rather than deleted or softened.
Strict matters in both directions: a NEW status with no exit fails immediately,
and if someone FIXES the BLOCKED cell the test fails as "unexpectedly passed"
and forces them to un-mark it — so the gap cannot be silently closed and
forgotten either.
"""

from __future__ import annotations

import inspect

import pytest

from swarm.tasks.board import TaskBoard
from swarm.tasks.task import TaskStatus

# --- the enumeration, derived from source and verified by reading -----------
#
# TERMINAL statuses legitimately need no onward exit beyond reopen/release.
_TERMINAL = {TaskStatus.DONE, TaskStatus.FAILED}

# Board-level exits per status: (board verb, resulting status, falsifies?)
_BOARD_EXITS: dict[TaskStatus, list[tuple[str, str, bool]]] = {
    # #1636: ``assign`` was listed here as BACKLOG→ASSIGNED and has not been an exit
    # since 2026-08-07, when assign() started KEEPING backlog status deliberately
    # ("Backlog means parked, not for now"). Nothing caught the drift: property (a)
    # only asks that the exit set be non-empty, and the two survivors satisfied it.
    # The audit was measuring an exit that no longer existed.
    #
    # It is replaced by ``activate``, which IS a real exit — reachable from the worker
    # surface via swarm_start_task(unpark=true) on a task the caller owns. Before #1636
    # BACKLOG's only exits were Queen-side, which is why an owned backlog task could be
    # neither started nor completed and had to be force-completed by the operator.
    TaskStatus.BACKLOG: [
        ("approve_task", "unassigned", False),
        ("reject_task", "failed", False),
        ("activate", "active", False),
    ],
    TaskStatus.UNASSIGNED: [
        ("assign", "assigned", False),
        ("release", "unassigned", False),
    ],
    TaskStatus.ASSIGNED: [
        ("activate", "active", False),
        ("complete", "done", False),
        ("unassign", "unassigned", False),
        ("block_on_external", "blocked", False),
    ],
    TaskStatus.ACTIVE: [
        ("complete", "done", False),
        ("park", "assigned", False),
        ("unassign", "unassigned", False),
        ("block_on_external", "blocked", False),
        ("block_for_operator", "blocked", False),
    ],
    TaskStatus.BLOCKED: [
        # CORRECTED 2026-08-05: the audit's first pass OMITTED release, which
        # accepts BLOCKED (only DONE/FAILED and already-ownerless are refused)
        # and is reached from the Queen via queen_reassign_task. That omission
        # produced a false headline — "BLOCKED has no reachable honest exit".
        # It does; release just DROPS THE OWNER.
        ("release", "unassigned", False),
        # #1268: the OWNER-PRESERVING exit. Had zero callers; now on both surfaces.
        ("unblock", "assigned", False),
        # Exits BLOCKED by recording completion for work that is still open.
        ("force_complete", "done", True),
    ],
    TaskStatus.DONE: [("reopen", "backlog", False), ("release", "unassigned", False)],
    TaskStatus.FAILED: [("reopen", "backlog", False), ("release", "unassigned", False)],
}


def _src(pkg: str) -> str:
    """Concatenated source of every module under a package dir."""
    import pathlib

    root = pathlib.Path(inspect.getfile(TaskBoard)).parent.parent / pkg
    return "\n".join(p.read_text() for p in root.rglob("*.py"))


def _reachable_board_verbs(surface: str) -> set[str]:
    """Board verbs a surface can invoke, directly or via a daemon proxy.

    Deliberately a source scan rather than a registry: there is no registry, and
    inventing one for the test would let the test and reality drift apart.
    """
    src = _src("mcp/handlers" if surface == "worker" else "mcp/queen_handlers")
    verbs = set()
    for verb, *_ in {v for exits in _BOARD_EXITS.values() for v in exits}:
        # direct board call, or the daemon proxy that wraps it
        if f"board.{verb}(" in src or f"d.{verb}_task(" in src or f"d.{verb}(" in src:
            verbs.add(verb)
    # Daemon proxies whose names differ from the board verb they reach.
    proxy_map = {
        "complete_task": "complete",
        "force_complete_task": "force_complete",
        "fail_task": "fail",
        "reopen_task": "reopen",
        "unassign_task": "unassign",
        "assign_task": "assign",
        "start_task": "activate",
    }
    for proxy, verb in proxy_map.items():
        # Only a call THROUGH the daemon counts. An earlier version also matched
        # a bare "{proxy}(" anywhere in the surface source, which matched the
        # handler's own function definitions and reported verbs as reachable
        # that nothing invoked — a scan that finds what it is looking for by
        # accident is worse than one that finds nothing.
        if f"d.{proxy}(" in src or f"daemon.{proxy}(" in src:
            verbs.add(verb)
    return verbs


# --- AC-1: the enumeration is complete ------------------------------------


def test_every_status_is_enumerated():
    """A status added later must not slip past this file unnoticed."""
    assert set(_BOARD_EXITS) == set(TaskStatus), (
        f"unenumerated statuses: {set(TaskStatus) - set(_BOARD_EXITS)} — "
        f"add them to _BOARD_EXITS and reassess properties (a)-(g)"
    )


# --- AC-2 property (a): non-empty ------------------------------------------


@pytest.mark.parametrize("status", list(TaskStatus))
def test_every_status_has_at_least_one_board_level_exit(status):
    """Property (a). Cheapest of the seven and the only one that always held."""
    assert _BOARD_EXITS[status], f"{status.value} has no exit at all"


# --- AC-2 property (c): a non-falsifying exit must exist -------------------


@pytest.mark.parametrize("status", [s for s in TaskStatus if s not in _TERMINAL])
def test_every_non_terminal_status_has_a_non_falsifying_exit(status):
    """Property (c) at board level. BLOCKED passes here — ``unblock`` does not
    falsify. That is exactly why (c) alone was not enough to catch the defect:
    the honest exit exists, it just cannot be invoked."""
    honest = [v for v, _, falsifies in _BOARD_EXITS[status] if not falsifies]
    assert honest, f"{status.value}'s only exits falsify history: {_BOARD_EXITS[status]}"


# --- AC-2 property (b): REACHABLE from a surface — the failing cell ---------


def test_the_board_verb_inventory_is_honest():
    """Positive control for the scan below.

    A reachability check built on a source scan is worthless if the scan matches
    nothing — an empty result would read as "no verbs reachable" and every
    assertion below would pass or fail for the wrong reason. So assert the scan
    finds verbs we know ARE wired up before trusting any absence it reports.
    """
    worker = _reachable_board_verbs("worker")
    assert "activate" in worker, "scan cannot see swarm_start_task's activate call"
    assert "park" in worker, "scan cannot see swarm_park_task"
    assert "block_on_external" in worker, "scan cannot see swarm_block_on_external"


def test_blocked_has_a_non_falsifying_exit_reachable_from_a_surface():
    """Property (b) crossed with (c) — the combination nothing tested before.

    Was xfail(strict=True) while BLOCKED had no owner-preserving exit reachable
    from any surface. #1268 wired ``unblock`` to both surfaces, so this now
    passes normally and a regression fails it directly — the strictness is not
    weakened, it is simply no longer needed to hold a known gap open.
    """
    honest = {v for v, _, falsifies in _BOARD_EXITS[TaskStatus.BLOCKED] if not falsifies}
    reachable = _reachable_board_verbs("worker") | _reachable_board_verbs("queen")
    assert honest & reachable, (
        f"BLOCKED's honest exits {honest} are reachable from no surface; "
        f"reachable verbs are {sorted(reachable)}"
    )


# --- AC-5 property (e): no gate is racy rather than strict -----------------


def test_park_accepts_assigned_so_the_inv2_reconciler_cannot_race_it():
    """Property (e). ``park`` originally required ACTIVE, and the INV-2
    reconciler demotes ACTIVE→ASSIGNED on every RESTING transition (27 times in
    10h on #1158) — so the window was seconds wide and not under the caller's
    control. #1159 relaxed it; this pins that it stays relaxed."""
    src = inspect.getsource(TaskBoard.park)
    assert "TaskStatus.ACTIVE, TaskStatus.ASSIGNED" in src, (
        "park's precondition narrowed again — the INV-2 reconciler will race it"
    )


# --- AC-4 property (f): no silent undo ------------------------------------


def test_every_activate_caller_writes_history():
    """Property (f). Absence of a ``task_history`` row is what settled #1159's
    write-failed-vs-write-reverted question, because ``_promote_one_assigned``
    was the one caller that wrote nothing. It no longer activates at all.

    STILL EXACTLY 2 AFTER #1282, deliberately. That task addressed "tasks sit in
    ASSIGNED" by TEACHING ``swarm_start_task`` — the dispatch instructions and the
    workflow templates never named it — rather than by adding an automatic
    promoter. A backstop hook was considered and rejected: it would be a second
    path to one transition, and the narrower "worker owns exactly one assigned
    task" variant still infers WHETHER the worker is on a task at all, since a
    BUZZING worker may be doing inline work. See the rejected-alternatives table
    in docs/specs/worker-asserted-active.md.

    So this count needing no change is the evidence #1282 did not touch the state
    machine. If a future change raises it to 3, that must be a deliberate decision
    recorded alongside, not an incidental consequence of making the board look
    livelier.
    """
    src = _src("mcp/handlers") + _src("server") + _src("drones")
    callers = src.count(".activate(")
    assert callers == 2, (
        f"expected exactly 2 activate() callers (worker-asserted start + "
        f"_activate_with_history), found {callers} — a new one may not write history"
    )
    from swarm.drones import state_tracker

    promoter = inspect.getsource(state_tracker.WorkerStateTracker._promote_one_assigned)
    assert "activate(" not in promoter, "the promoter can activate again (#1159)"


# --- AC-6: a known-correct case that must not be "fixed" ------------------


def test_block_for_operator_stays_active_only():
    """AC-6. ACTIVE-only is CORRECT here — it is the Queen's auto-park path,
    where "no longer ACTIVE" legitimately means the stall resolved. The
    worker-facing path routes through ``block_on_external``, which already
    accepts ASSIGNED. Collapsing them would break the auto-park semantics."""
    for_op = inspect.getsource(TaskBoard.block_for_operator)
    external = inspect.getsource(TaskBoard.block_on_external)
    assert "TaskStatus.ACTIVE" in for_op
    assert "TaskStatus.ASSIGNED" not in for_op, (
        "block_for_operator now accepts ASSIGNED — it was collapsed with the "
        "worker path; see #1104 AC-6, this is a regression not a fix"
    )
    assert "TaskStatus.ASSIGNED" in external, "block_on_external stopped accepting ASSIGNED"
