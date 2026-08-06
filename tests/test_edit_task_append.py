"""An edit must not be able to silently shorten a description (#1289).

I CAUSED THIS AND MEASURED IT. Working #1274 I appended AC-1 findings to a 3,819-char
description, produced 6,124 chars in a staging file, then called ``swarm_edit_task`` by
retyping the text. A later read returned 3,950 chars — roughly 2,200 characters of
verified findings gone. **The call reported success.** I noticed hours later by
accident, because a length looked wrong.

Two properties made it dangerous: the failure is silent and reports success (#1159's
shape), and it scales with the value of the record — the longer and more carefully-built
the description, the more one edit destroys and the less likely anyone notices.

THE FAILURE MODE HERE IS A SUCCESS MESSAGE, which dictates how these tests are written.
A test asserting `"updated" in reply` would have passed throughout the incident. So the
load-bearing assertions are on CHARACTER COUNTS, and the reproduction below uses the
real before/after numbers rather than a synthetic pair.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from swarm.mcp.handlers._edit import _handle_edit_task
from swarm.tasks.board import TaskBoard


def _text(result) -> str:
    return " ".join(r["text"] for r in result)


@pytest.fixture
def d():
    daemon = MagicMock()
    board = TaskBoard()
    daemon.task_board = board
    daemon.task_history = MagicMock()

    def _edit(task_id, title=None, description=None, actor="user", **kw):
        return board.update(task_id, title=title, description=description)

    daemon.edit_task = _edit
    return daemon


def _owned(d, description: str):
    t = d.task_board.create(title="a task", description=description)
    d.task_board.assign(t.id, "swarm")
    return t


# --- AC-1 / AC-2: the reproduction, with real counts ----------------------


def test_a_truncating_replace_now_reports_the_loss(d):
    """AC-1 + AC-2. Reproduces the #1274 incident at its real magnitude and asserts the
    reply NAMES the shortfall. Before this, the identical call answered
    "Task #N updated (description). Recorded in history." and nothing else."""
    original = "A" * 3950
    t = _owned(d, original)

    # The mistake: a caller retypes the text and drops a chunk.
    truncated = "A" * 1750
    out = _text(_handle_edit_task(d, "swarm", {"number": t.number, "description": truncated}))

    assert d.task_board.get(t.id).description == truncated, "the edit did not apply"
    assert "3950" in out and "1750" in out, f"reply does not report before/after: {out}"
    assert "-2200" in out, f"reply does not name the 2200-char loss: {out}"


def test_a_growing_edit_reports_a_positive_delta(d):
    t = _owned(d, "A" * 100)
    out = _text(_handle_edit_task(d, "swarm", {"number": t.number, "description": "A" * 350}))
    assert "100" in out and "350" in out and "+250" in out


def test_no_delta_is_reported_when_the_description_is_unchanged(d):
    """The number has to mean something when it appears. Reporting a delta of +0 on
    every title-only edit would train the reader to skip it."""
    t = _owned(d, "unchanged text")
    out = _text(_handle_edit_task(d, "swarm", {"number": t.number, "title": "new title"}))
    assert "chars" not in out, f"delta reported for an edit that did not touch it: {out}"


# --- AC-1: append, so the retyping that caused it is unnecessary ----------


def test_append_adds_without_reproducing_the_existing_text(d):
    """The ergonomics half. The caller supplies ONLY the new text, so there is no
    opportunity to lose the old — this is what removes the reason to route around the
    verb, which is what I actually did on #1274 (the AC-6 measurement went into a
    progress note because retyping 6.2k chars again felt worse than the alternative)."""
    original = "ORIGINAL PARAGRAPH." * 20
    t = _owned(d, original)

    out = _text(
        _handle_edit_task(
            d, "swarm", {"number": t.number, "append_description": "NEW FINDING: measured."}
        )
    )

    after = d.task_board.get(t.id).description
    assert after.startswith(original), "the original text did not survive byte-for-byte"
    assert after.endswith("NEW FINDING: measured.")
    assert after == f"{original}\n\nNEW FINDING: measured.", "separator is not a blank line"
    assert "+24" in out, f"append did not report its delta: {out}"


def test_append_on_an_empty_description_does_not_lead_with_blank_lines(d):
    t = _owned(d, "")
    _handle_edit_task(d, "swarm", {"number": t.number, "append_description": "first note"})
    assert d.task_board.get(t.id).description == "first note"


def test_passing_both_description_and_append_is_refused(d):
    """Guessing which was meant is how a caller who meant append performs a replace."""
    t = _owned(d, "keep me")
    out = _text(
        _handle_edit_task(
            d, "swarm", {"number": t.number, "description": "replace", "append_description": "add"}
        )
    )
    assert "not both" in out
    assert d.task_board.get(t.id).description == "keep me", "refusal mutated the task"


def test_an_empty_append_is_refused_and_changes_nothing(d):
    t = _owned(d, "keep me")
    out = _text(_handle_edit_task(d, "swarm", {"number": t.number, "append_description": "   "}))
    assert "nothing to add" in out
    assert d.task_board.get(t.id).description == "keep me"


# --- AC-3: a closed task's resolution stays out of reach ------------------


def test_append_cannot_touch_a_closed_task(d):
    """AC-3. The terminal guard runs before any description resolution, so the new
    parameter is not a way around it. #1274 established that corrections to closed work
    go through swarm_annotate_resolution, which annotates without rewriting."""
    t = _owned(d, "original description")
    d.task_board.complete(t.id, "ORIGINAL RESOLUTION")

    out = _text(_handle_edit_task(d, "swarm", {"number": t.number, "append_description": "more"}))

    after = d.task_board.get(t.id)
    assert "done" in out.lower()
    assert after.description == "original description", "append reached a closed task"
    assert after.resolution == "ORIGINAL RESOLUTION", "the resolution was modified"


def test_the_resolution_is_still_structurally_unreachable():
    """AC-3's stronger half, asserted as the absent parameter rather than a message a
    later change could soften (carried over from #1274)."""
    import inspect

    assert "resolution" not in inspect.signature(TaskBoard.update).parameters


# --- AC-4: the audit trail distinguishes a correction from a deletion -----


def test_history_records_the_character_delta():
    """AC-4. The preview alone could not distinguish "fixed a line" from "dropped a
    third of the text" — it shows the head of the old value, which looks identical
    either way. That is exactly why #1274's loss left no hint in its history entry."""
    from swarm.server.task_manager import _describe_edit

    before = MagicMock()
    before.title = "t"
    before.description = "A" * 3950
    before.acceptance_criteria = []

    detail = _describe_edit(before, description="A" * 1750)
    assert "3950" in detail and "1750" in detail and "-2200" in detail, detail


def test_history_delta_distinguishes_a_small_correction():
    from swarm.server.task_manager import _describe_edit

    before = MagicMock()
    before.title = "t"
    before.description = "A" * 3950
    before.acceptance_criteria = []

    detail = _describe_edit(before, description="A" * 3949 + "B")
    assert "+0" in detail, f"a one-character correction should read as +0: {detail}"
    assert "-2200" not in detail
