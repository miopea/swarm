"""``swarm_edit_task`` must reach the HOLD class (#1270).

Follow-up 3 of 3 from the #1104 state-machine audit, failing property (g): a
precondition that is structurally unsatisfiable for a whole task CLASS is the
same defect as a missing verb.

THE DEFECT, and it is the cleanest on the audit's list because NEITHER VERB IS
INDIVIDUALLY WRONG:

  * HOLD tasks are UNASSIGNED **by design** — that is the mechanism that stops
    the auto-assign drone (#894, after a HOLD eslint-10 item was auto-dispatched
    to wifi-portal in the 2026-06-26 incident).
  * ``swarm_edit_task`` requires assignment — reasonably, since it exists so a
    worker can correct its OWN task and not another worker's.
  * Therefore no worker could ever correct the description of any HOLD task.

The gap existed only in COMPOSITION, which is why auditing either verb alone
would never have surfaced it. Verified live 2026-08-05 on 2 of 2 attempted
(#1104, #1018), verbatim: "Task #1104 is unassigned — swarm_edit_task only
corrects a task assigned to you. Nothing changed."

WHY THIS CLASS IS THE WORST ONE TO LOSE THE EDIT VERB ON: HOLDs sit parked
longest, so their premises rot most. Both corrections needed on 2026-08-05
existed BECAUSE a HOLD had gone stale, and #1128 had to be closed outright once
the architecture it described no longer existed. A class of task that cannot be
corrected is a class of task that silently becomes misinformation.

APPROACH CHOSEN — option (a) from the task: let ``swarm_edit_task`` accept an
UNASSIGNED task that is tagged HOLD, without adopting it.

  * The ownership rule exists to stop a worker rewriting ANOTHER worker's
    assigned work. An unassigned HOLD task has no owner, so in this case the
    rule has no owner to protect — it is guarding nothing and only blocking.
  * Rejected (b) "a separate verb": a second verb for the same intent doubles
    discovery cost and invites drift between two implementations of one
    correction path. #1268 deliberately shared helpers between surfaces for
    exactly that reason.
  * Rejected (c) "accept the Queen as relay": that is the status quo, and it
    costs a Queen round trip per correction — two were needed in a single day.
    It also leaves the asymmetry that made #1270 itself uneditable by its owner.

WHAT MUST NOT REGRESS, and the tests below assert each of these BEFORE the new
capability:
  * editing must not adopt — the task stays UNASSIGNED and the auto-assigner
    still skips it, so "can edit" does not leak into "can start" (#894, #1281)
  * a plain UNASSIGNED task with no hold tag is still refused
  * another worker's ASSIGNED task is still refused
  * closed work is still refused
  * the Queen surface still works (operator used ``queen_edit_task`` 4 times on
    2026-08-05; a fix that tightened the shared rule would remove the only path
    that worked)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from swarm.mcp.handlers._edit import _handle_edit_task
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import TaskStatus


def _text(result) -> str:
    return " ".join(r["text"] for r in result)


@pytest.fixture
def d():
    daemon = MagicMock()
    daemon.task_board = TaskBoard()
    board = daemon.task_board

    # Route the daemon proxy at the real board so edits actually land — a
    # MagicMock here would let every assertion below pass without the edit
    # happening, which is the failure mode that hid the second is_available gate
    # in #1281.
    def _edit(task_id, title=None, description=None, actor="user", **kw):
        return board.update(task_id, title=title, description=description)

    daemon.edit_task = _edit
    return daemon


def _hold_task(d, title="parked work", desc="original premise"):
    t = d.task_board.create(title=title, description=desc, tags=["hold"])
    assert t.is_on_hold and not t.assigned_worker
    assert t.status == TaskStatus.UNASSIGNED
    return t


# --- the constraints, asserted before the capability ----------------------


def test_editing_a_hold_task_does_not_adopt_or_make_it_dispatchable(d):
    """AC-2, and the constraint the task states most emphatically. The
    unassigned-ness IS the hold mechanism; an edit that quietly assigned or
    un-held the task would trade a documentation defect for the auto-dispatch
    incident #894 exists to prevent."""
    t = _hold_task(d)
    _handle_edit_task(d, "swarm", {"number": t.number, "description": "corrected premise"})

    after = d.task_board.get(t.id)
    assert after.description == "corrected premise", "the edit did not land"
    assert not after.assigned_worker, "editing ADOPTED the task — it is now owned"
    assert after.status == TaskStatus.UNASSIGNED, "editing moved it out of UNASSIGNED"
    assert after.is_on_hold, "editing stripped the hold tag"
    assert after.is_available is False, "the edited task entered the drone's candidate set"
    assert t.id not in {x.id for x in d.task_board.available_tasks}, (
        "a HOLD task became available after being edited — auto-dispatch is back"
    )


def test_a_plain_unassigned_task_is_still_refused(d):
    """The ownership rule must keep working where it has something to protect.
    Only the HOLD class is exempted, not unassigned tasks generally."""
    t = d.task_board.create(title="ordinary unassigned")
    out = _text(_handle_edit_task(d, "swarm", {"number": t.number, "description": "x"}))
    assert "unassigned" in out.lower()
    assert d.task_board.get(t.id).description != "x", "refusal still mutated the task"


def test_another_workers_assigned_task_is_still_refused(d):
    """AC-5: existing ownership semantics for ASSIGNED tasks are unchanged."""
    t = d.task_board.create(title="theirs")
    d.task_board.assign(t.id, "sculpt-studio")
    out = _text(_handle_edit_task(d, "swarm", {"number": t.number, "description": "x"}))
    assert "not assigned to you" in out.lower()
    assert "sculpt-studio" in out
    assert d.task_board.get(t.id).description != "x"


def test_a_closed_hold_task_is_still_refused(d):
    """Editing closed work rewrites the record rather than correcting live
    requirements. The HOLD exemption must not reopen that path — #1274 is the
    task for stale CLOSED resolutions, and it must stay a separate mechanism."""
    t = _hold_task(d)
    # override_hold is required to assign a HOLD task at all (#1281) — without it
    # this fixture silently never completed and the test failed on the
    # "unassigned" refusal instead of the "done" one.
    assert d.task_board.assign(t.id, "swarm", override_hold=True)
    assert d.task_board.complete(t.id, "done")
    out = _text(_handle_edit_task(d, "swarm", {"number": t.number, "description": "x"}))
    assert "done" in out.lower()
    assert d.task_board.get(t.id).description != "x"


# --- the capability -------------------------------------------------------


def test_a_worker_can_correct_an_unassigned_hold_task(d):
    """AC-1. The verbatim refusal this replaces: "Task #1104 is unassigned —
    swarm_edit_task only corrects a task assigned to you.\""""
    t = _hold_task(d)
    out = _text(
        _handle_edit_task(d, "swarm", {"number": t.number, "description": "premise corrected"})
    )
    assert "updated" in out.lower(), f"still refused: {out}"
    assert d.task_board.get(t.id).description == "premise corrected"


def test_the_title_can_be_corrected_too(d):
    t = _hold_task(d)
    _handle_edit_task(d, "swarm", {"number": t.number, "title": "clearer title"})
    assert d.task_board.get(t.id).title == "clearer title"


def test_any_worker_may_correct_a_hold_task_not_only_a_notional_owner(d):
    """A HOLD task has no owner, so there is no owner-match to apply. Pinned
    because the obvious "fix" of comparing against a filer or target worker would
    re-close the verb for the class in a way that looks like it works."""
    t = _hold_task(d)
    out = _text(_handle_edit_task(d, "sculpt-studio", {"number": t.number, "description": "y"}))
    assert "updated" in out.lower(), f"a different worker was refused: {out}"
    assert d.task_board.get(t.id).description == "y"


# --- AC-4: the HOLD class specifically, so it cannot silently re-close -----


def test_the_hold_exemption_is_keyed_on_the_hold_predicate(d):
    """AC-4. Asserts the exemption is expressed through ``is_on_hold`` rather
    than an incidental condition, so a future change to the ownership rule cannot
    silently re-close the verb for this class.

    #1270 recurred as #1281 on a SECOND verb (``assign``) while sitting in the
    tracker describing itself — the class is not reliably exhausted by fixing one
    verb, so this pins the mechanism and not just the outcome.
    """
    import inspect

    src = inspect.getsource(_handle_edit_task)
    assert "is_on_hold" in src, (
        "the HOLD exemption is not keyed on is_on_hold — if it is keyed on "
        "something incidental (tag string, status alone), a change to either "
        "will re-close the verb for the whole class without failing a test"
    )


def test_the_queen_surface_can_still_edit_a_hold_task(d):
    """The operator's ADDITIONAL constraint (2026-08-05): queen_edit_task was the
    only verb that could correct #1018 and #1104 while they were stale, and he
    used it 4 times in one day. A fix that closed the worker gap by tightening
    the shared rule would remove the only working path — a regression dressed as
    a fix."""
    from swarm.mcp.queen_handlers._tasks import _handle_edit_task as queen_edit

    t = _hold_task(d)
    queen_edit(d, "queen", {"number": t.number, "description": "queen correction"})
    after = d.task_board.get(t.id)
    assert after.description == "queen correction", "the Queen path stopped working"
    assert not after.assigned_worker, "the Queen path adopted the task"
