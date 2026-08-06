"""A stale resolution must be markable, and the reader must see it (#1274).

A resolution is not an archived note. It becomes ``task.learnings``, and learnings are
recalled into future dispatches by ``playbook_ops.recall_learnings_for_task`` — so a
stale one is actively RE-SERVED as advice, carrying a completed task's authority, to a
worker with no way to know it aged out. #1174's ``delete_branch_on_merge`` claim was
true when written and wrong by the time #1267 read it.

AC-1 WAS VERIFIED BEFORE ANY CODE WAS WRITTEN, against a THROWAWAY closed task on a
COPY of the live DB, and the premise held: ``swarm_complete_task``, ``swarm_edit_task``
and ``queen_edit_task`` all refuse a closed task, and ``queen_save_learning`` only
APPENDS (it calls ``add_learning``, so a stale learning can be supplemented but never
corrected). A fifth finding changed the design: ``TaskBoard.update`` does not accept a
``resolution`` kwarg AT ALL, so resolutions are immutable STRUCTURALLY rather than by
policy — which is why AC-4's test below asserts the absent parameter rather than a
refusal message someone could later soften.

SO THE FIX IS PURELY ADDITIVE. Unlike #1270's HOLD-class edit gap, where the fix was to
ALLOW an edit, an edit here would be wrong: rewriting a closed resolution destroys the
record of what was actually believed and done at the time.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from swarm.tasks.board import TaskBoard
from swarm.tasks.task import TaskStatus


@pytest.fixture
def board():
    return TaskBoard()


def _closed(board, resolution="ORIGINAL — delete_branch_on_merge is disabled fleet-wide"):
    t = board.create(title="audit the merge settings")
    board.assign(t.id, "swarm")
    board.complete(t.id, resolution)
    assert board.get(t.id).status == TaskStatus.DONE
    return t


# --- AC-4 first: the thing that must NOT become possible -------------------


def test_the_resolution_is_structurally_immutable():
    """AC-4, asserted as the ABSENT PARAMETER rather than as a refusal message.

    A message can be softened; a missing kwarg raises. ``TaskBoard.update`` has no
    ``resolution`` parameter, so no caller can rewrite a closed resolution even by
    accident, and the annotation feature does not have to defend that by convention.
    """
    import inspect

    params = inspect.signature(TaskBoard.update).parameters
    assert "resolution" not in params, (
        "TaskBoard.update now accepts `resolution` — closed resolutions became "
        "rewritable, which destroys the record of what was believed at the time. "
        "#1274 depends on this being impossible, not merely discouraged."
    )


def test_annotating_never_changes_the_resolution_text(board):
    """AC-2's core claim. The note goes BESIDE the original, never over it."""
    t = _closed(board)
    original = board.get(t.id).resolution

    assert board.annotate_resolution(t.id, kind="stale", note="enabled 2026-07-30")

    after = board.get(t.id)
    assert after.resolution == original, "the original resolution text was modified"
    assert after.resolution_note == "enabled 2026-07-30"
    assert after.resolution_note_kind == "stale"
    assert after.status == TaskStatus.DONE, "annotating reopened the task"


# --- AC-5: wrong and stale stay distinguishable ---------------------------


@pytest.mark.parametrize("kind", ["stale", "wrong"])
def test_both_kinds_are_accepted_and_recorded(board, kind):
    t = _closed(board)
    assert board.annotate_resolution(t.id, kind=kind, note="because X")
    assert board.get(t.id).resolution_note_kind == kind


def test_an_unknown_kind_is_refused(board):
    """AC-5. Free-text kinds would collapse the distinction by drift."""
    t = _closed(board)
    assert board.annotate_resolution(t.id, kind="outdated", note="x") is False
    assert board.get(t.id).resolution_note_kind == ""


def test_an_empty_note_is_refused(board):
    """An unexplained flag is worse than none: the next reader cannot tell whether it
    still applies, so it becomes noise that trains people to ignore the caveat."""
    t = _closed(board)
    assert board.annotate_resolution(t.id, kind="stale", note="   ") is False
    assert board.get(t.id).resolution_note == ""


def test_an_open_task_is_refused(board):
    """Annotating a resolution that does not exist yet is a note about nothing. Live
    requirements are corrected with swarm_edit_task."""
    t = board.create(title="still open")
    board.assign(t.id, "swarm")
    assert board.annotate_resolution(t.id, kind="stale", note="x") is False


def test_a_failed_task_can_also_be_annotated(board):
    """FAILED is terminal too, and its resolution is recalled the same way."""
    t = board.create(title="failed work")
    board.assign(t.id, "swarm")
    board.fail(t.id)
    assert board.annotate_resolution(t.id, kind="wrong", note="never reproduced")
    assert board.get(t.id).resolution_note_kind == "wrong"


def test_annotating_broadcasts_so_the_dashboard_sees_it(board):
    """#1275's class guard: persisting without notifying leaves every dashboard stale."""
    t = _closed(board)
    events: list[int] = []
    board.on_change(lambda: events.append(1))
    board.annotate_resolution(t.id, kind="stale", note="x")
    assert events, "annotation persisted without firing a change event"


# --- AC-3: THE READER SEES IT. This is the load-bearing one. --------------


def _ops(board):
    from swarm.server.playbook_ops import PlaybookOps

    return PlaybookOps(
        get_store=lambda: None,
        get_synthesizer=lambda: None,
        get_config=MagicMock(),
        drone_log=MagicMock(),
        task_board=board,
        track_task=lambda _t: None,
        get_worker=lambda _n: None,
    )


def _stale_learning_task(board):
    """A task whose LEARNINGS will be recalled for a new task on the same topic."""
    t = board.create(title="branch protection and merge settings audit")
    board.assign(t.id, "swarm")
    board.complete(t.id, "delete_branch_on_merge is disabled on every repo")
    # `learnings` is set by the completion handler, not by board.update (which has no
    # such kwarg), so the fixture assigns it directly on the task the board holds.
    # Verified live before relying on it: 1062 tasks on the board carry non-empty
    # learnings and the most recent was written today, so this field is the CURRENT
    # recall path and not a legacy leftover.
    board.get(t.id).learnings = "delete_branch_on_merge is disabled on every repo"
    return t


def test_a_known_stale_learning_shows_its_caveat_to_the_reader(board):
    """AC-3 verbatim: "demonstrated by injecting a known-stale learning and showing the
    reader sees the caveat".

    Injects exactly #1174's shape, annotates it, and asserts the caveat appears in the
    block that gets pasted into the next worker's PTY.
    """
    stale = _stale_learning_task(board)
    board.annotate_resolution(
        stale.id, kind="stale", note="True until delete_branch_on_merge was enabled 2026-07-30"
    )

    incoming = board.create(
        title="check branch protection and merge settings", description="audit merge settings"
    )
    block = _ops(board).recall_learnings_for_task(board.get(incoming.id))

    assert block, "the stale learning was not recalled at all — test proves nothing"
    assert "delete_branch_on_merge is disabled" in block, "the original text is missing"
    assert "NO LONGER TRUE" in block, "the reader is served the stale claim with NO caveat"
    assert "2026-07-30" in block, "the caveat does not say what changed or when"


def test_an_unannotated_learning_gets_no_caveat_but_does_get_a_date(board):
    """The complement, and it guards two ways: a caveat on every learning would be
    noise, and a date on none of them is the #1174 failure — the recalled text carried
    no timestamp, so nothing prompted the reader to check its age."""
    _stale_learning_task(board)
    incoming = board.create(
        title="check branch protection and merge settings", description="audit merge settings"
    )
    block = _ops(board).recall_learnings_for_task(board.get(incoming.id))

    assert block
    assert "NO LONGER TRUE" not in block and "WAS NEVER CORRECT" not in block
    assert "closed 20" in block, "no date on a recalled learning — #1174's exact gap"


def test_a_wrong_annotation_reads_differently_from_a_stale_one(board):
    """AC-5 at the reader's end, which is where the distinction has to survive.
    'wrong' impugns the original work; 'stale' does not."""
    stale = _stale_learning_task(board)
    board.annotate_resolution(stale.id, kind="wrong", note="the setting never existed")
    incoming = board.create(
        title="check branch protection and merge settings", description="audit merge settings"
    )
    block = _ops(board).recall_learnings_for_task(board.get(incoming.id))
    assert "WAS NEVER CORRECT" in block
    assert "NO LONGER TRUE" not in block


# --- reachability: a board verb with no caller is not a feature -----------


def test_the_verb_is_reachable_from_the_worker_surface():
    """#1268 shipped board.unblock with ZERO callers, so BLOCKED had no reachable exit
    while every board test passed. The annotation is worthless if the worker who is
    served the stale advice cannot flag it."""
    from swarm.mcp.tools import _HANDLERS, TOOLS

    assert "swarm_annotate_resolution" in {t["name"] for t in TOOLS}
    assert "swarm_annotate_resolution" in _HANDLERS


def test_any_worker_may_annotate_a_task_it_does_not_own(board):
    """Deliberate. Whoever was just served the bad advice is who discovers it — gating
    on ownership would put the correction path behind the worker least likely to be
    looking, which is the composition trap #1270 documents."""
    from swarm.mcp.handlers._annotate import _handle_annotate_resolution

    t = board.create(title="someone else's work")
    board.assign(t.id, "sculpt-studio")
    board.complete(t.id, "original finding")

    d = MagicMock()
    d.task_board = board
    d.drone_log = MagicMock()
    d.task_history = MagicMock()
    out = " ".join(
        r["text"]
        for r in _handle_annotate_resolution(
            d, "swarm", {"number": t.number, "kind": "stale", "note": "changed since"}
        )
    )
    assert "flagged as stale" in out
    assert "intact" in out, "the handler did not confirm the original survived"
    assert board.get(t.id).resolution == "original finding"


def test_the_handler_refuses_an_open_task_and_names_the_right_verb(board):
    from swarm.mcp.handlers._annotate import _handle_annotate_resolution

    t = board.create(title="open")
    board.assign(t.id, "swarm")
    d = MagicMock()
    d.task_board = board
    out = " ".join(
        r["text"]
        for r in _handle_annotate_resolution(
            d, "swarm", {"number": t.number, "kind": "stale", "note": "x"}
        )
    )
    assert "not closed" in out
    assert "swarm_edit_task" in out, "refusal does not name the verb that WOULD apply"
