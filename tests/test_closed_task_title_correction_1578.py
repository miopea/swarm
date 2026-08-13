"""#1578 — a closed task's misleading TITLE gets a supported correction path.

THE GAP, measured on #1538 by two identities before any code was written:
    swarm_edit_task(1538, title=…)  -> "#1538 is done — editing closed work rewrites the record"
    queen_edit_task(1538, title=…)  -> the SAME closed-task refusal when the Queen ran it
So it was never a permissions problem. `swarm_annotate_resolution` (#1274) was the designed
remedy for a closed task, but it attaches to the RESOLUTION — and a resolution annotation
does not change what the task is CALLED. Anyone scanning a board, a search result or a
learning header sees the wrong title and never reaches the note that corrects it.

WHY A TITLE IS CORRECTABLE WHEN A RESOLUTION IS NOT, which is the asymmetry this file
exists to pin: a resolution records what was believed and done, so rewriting it destroys an
audit trail. A title is a POINTER — no verifier grades it, no worker acts on it — and a
permanently wrong pointer on a task re-served as a learning sends every future reader to
the wrong layer. #1538 named `queen_prompt_worker` and the dispatch path when the defect
was a reconciler undoing a correct write.

THE CORRECTED TEXT REPLACES `title` rather than rendering beside it, so all ~34 render
sites pick it up structurally. A parallel `display_title` would have needed a 34-site sweep
in which a missed site silently shows the stale pointer — the shape this repo already
records for a guard added to one of several paths.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from swarm.mcp.tools import handle_tool_call
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import TaskStatus

ORIGINAL = "A worker driven into work by queen_prompt_worker never flips ASSIGNED to ACTIVE"
CORRECTED = "INV-2 demoted an ACTIVE task on an ordinary RESTING pause — a reconciler undo"


@pytest.fixture
def board():
    return TaskBoard()


def _daemon(board: TaskBoard) -> MagicMock:
    d = MagicMock()
    d.task_board = board
    return d


def _closed(board: TaskBoard, title: str = ORIGINAL) -> object:
    t = board.create(title=title)
    board.assign(t.id, "swarm")
    board.complete(t.id, "ORIGINAL RESOLUTION — do not touch")
    assert board.get(t.id).status == TaskStatus.DONE
    return t


def _annotate(d: MagicMock, number: int, **extra) -> str:
    args = {"number": number, "kind": "stale", "note": "the title named the wrong layer"}
    args.update(extra)
    result = handle_tool_call(d, "swarm", "swarm_annotate_resolution", args)
    blocks = result.get("content") if isinstance(result, dict) else result
    return blocks[0]["text"]


# ---------------------------------------------------------------------------
# AC1 / AC4 — through the real MCP handler
# ---------------------------------------------------------------------------


def test_a_closed_task_title_is_corrected_through_the_real_handler(board):
    """AC1 + AC4. Driven through `handle_tool_call`, not the board method, because the
    dispatcher is where a caller actually reaches this — and #1543 showed the dispatcher
    silently dropping arguments the handler never saw."""
    t = _closed(board)
    d = _daemon(board)

    out = _annotate(d, t.number, corrected_title=CORRECTED)

    assert board.get(t.id).title == CORRECTED
    assert "Title corrected" in out


def test_the_original_title_is_preserved_not_discarded(board):
    """AC2. The record of what it was called is what lets a reader reconcile the board
    with any older message or commit that quoted the original wording."""
    t = _closed(board)

    _annotate(_daemon(board), t.number, corrected_title=CORRECTED)

    assert board.get(t.id).title_original == ORIGINAL


def test_an_open_task_is_still_refused_and_named_the_right_verb(board):
    """AC4's CONTROL, and the more important half. The closed-task rule must not be
    softened as a side effect — a live task's requirements are corrected by editing it."""
    t = board.create(title="a live task")
    board.assign(t.id, "swarm")
    d = _daemon(board)

    out = _annotate(d, t.number, corrected_title="something else")

    assert board.get(t.id).title == "a live task", "an OPEN task was retitled"
    assert "swarm_edit_task" in out


def test_the_correction_reaches_the_display_path(board):
    """AC3, asserted through the REAL formatter rather than by reading the column.

    This is the whole justification for putting the corrected text into `title`: the
    board, search and learning headers all render through these, so they need no changes.
    Checking `task.title_original` instead would prove the storage and nothing about what
    a reader sees.
    """
    from swarm.mcp.handlers._task_format import _format_task_detail, _format_task_line

    t = _closed(board)
    _annotate(_daemon(board), t.number, corrected_title=CORRECTED)
    task = board.get(t.id)

    for rendered in (_format_task_line(task), _format_task_detail(task)):
        assert CORRECTED in rendered
        assert ORIGINAL not in rendered


# ---------------------------------------------------------------------------
# The ways this goes wrong
# ---------------------------------------------------------------------------


def test_a_second_correction_does_not_overwrite_the_original(board):
    """The original is the ORIGINAL, not the previous value. Getting this wrong would
    quietly replace the real title with someone's first guess at a correction, and the
    loss would be invisible — the column would still look populated."""
    t = _closed(board)
    d = _daemon(board)

    _annotate(d, t.number, corrected_title=CORRECTED)
    _annotate(d, t.number, corrected_title="a third wording")

    assert board.get(t.id).title == "a third wording"
    assert board.get(t.id).title_original == ORIGINAL


def test_correcting_a_title_leaves_the_resolution_untouched(board):
    """#1274's guarantee must survive this change. The two live on the same verb now, so
    a bug in one could plausibly reach the other."""
    t = _closed(board)

    _annotate(_daemon(board), t.number, corrected_title=CORRECTED)

    assert board.get(t.id).resolution == "ORIGINAL RESOLUTION — do not touch"


def test_annotating_without_a_corrected_title_still_works(board):
    """REGRESSION CONTROL for #1274. `corrected_title` is additive and optional; the
    original single-purpose call must behave exactly as before."""
    t = _closed(board)

    out = _annotate(_daemon(board), t.number)

    assert board.get(t.id).title == ORIGINAL
    assert board.get(t.id).resolution_note_kind == "stale"
    assert board.get(t.id).title_original == ""
    assert "Title corrected" not in out


def test_a_note_is_still_required_when_retitling(board):
    """A retitle with no stated reason is indistinguishable from vandalism to a later
    reader, so it goes through the same gate as every other annotation."""
    t = _closed(board)
    d = _daemon(board)

    result = handle_tool_call(
        d,
        "swarm",
        "swarm_annotate_resolution",
        {"number": t.number, "kind": "stale", "note": "", "corrected_title": CORRECTED},
    )
    blocks = result.get("content") if isinstance(result, dict) else result

    assert "note" in blocks[0]["text"].lower()
    assert board.get(t.id).title == ORIGINAL


def test_a_no_op_correction_does_not_claim_a_retitle(board):
    """The handler reads the board back instead of trusting its own argument. Reporting a
    change that did not happen is the #1159 park failure: a claim the caller cannot check.
    """
    t = _closed(board)

    out = _annotate(_daemon(board), t.number, corrected_title=ORIGINAL)

    assert "Title corrected" not in out
    assert board.get(t.id).title_original == "", "a no-op must not consume the original slot"


def test_the_tool_schema_advertises_the_new_argument(board):
    """#1543's lesson: the dispatcher refuses undeclared keys, so a `corrected_title` that
    is not in `inputSchema.properties` would be rejected before the handler ever ran —
    the feature would be unreachable while every unit test on the board method passed."""
    from swarm.mcp.tools import _ALLOWED_ARGS

    assert "corrected_title" in _ALLOWED_ARGS["swarm_annotate_resolution"]
