"""Editing a cross-project task must not silently destroy its routing.

TWO OPERATOR-REPORTED BUGS, 2026-08-07, from one edit that only added a tag:

    "it was a cross task and when I added the tag and saved it lost the target worker"
    "when I went back in and removed the source worker it still remained a CROSS task"

BUG 1 — SILENT DATA LOSS, and the mechanism is entirely in the browser. The modal's
worker ``<select>`` options are built from the workers currently rendered on the page.
A cross-project task can legitimately target a worker that is NOT in that list: another
project's worker, a renamed one, a decommissioned one. #1301-#1303 targeted
``claude-team-config``, which is not a configured worker here. Assigning
``select.value = "claude-team-config"`` when no matching ``<option>`` exists is a SILENT
no-op — the browser leaves the select on "—" and reports ``value === ""``.
``submitTaskModal`` then unconditionally posts ``target_worker=""``,
``_extract_edit_kwargs`` includes it because the key is present, and the stored target
is overwritten with empty. An edit that touched only TAGS destroyed a field the
operator never opened, with no error and no warning.

BUG 2 — A ONE-WAY LATCH, server-side, and the same shape as #1294's debounce.
``_apply_cross_fields`` ended with::

    if source_worker or target_worker:
        task.is_cross_project = True

which could only ever set the flag. Nothing cleared it. ``task_list.html`` gates the
CROSS badge purely on that flag, so a task whose source and target were both removed
kept rendering as cross-project permanently. Combined with bug 1 this is how three tasks
ended up as ``is_cross_project=1`` with ``source_worker=''`` and ``target_worker=''`` —
a state that cannot be reached deliberately and cannot be undone through the UI.

The two compose: bug 1 empties the fields, bug 2 makes the badge outlive them.
"""

from __future__ import annotations

import re
from pathlib import Path

from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask

_JS = (
    Path(__file__).parent.parent / "src" / "swarm" / "web" / "static" / "dashboard.js"
).read_text()


def _cross_task(board: TaskBoard) -> SwarmTask:
    task = board.add(SwarmTask(title="routed work", description="x"))
    board.update(task.id, source_worker="project-root", target_worker="claude-team-config")
    assert task.is_cross_project is True, "positive control: it must start as cross-project"
    return task


# --- bug 2: the flag must be recomputed, not latched --------------------------


def test_clearing_both_workers_clears_the_cross_flag():
    """The reported symptom. Removing the routing must remove the badge."""
    board = TaskBoard()
    task = _cross_task(board)
    board.update(task.id, source_worker="", target_worker="")
    assert task.is_cross_project is False, (
        "is_cross_project is still True with no source and no target, so the CROSS "
        "badge renders forever — the flag is latched rather than recomputed"
    )


def test_clearing_only_the_source_keeps_it_cross_while_a_target_remains():
    """A task still routed somewhere IS still cross-project. The fix must recompute
    from the resulting state, not blanket-clear whenever a field is touched."""
    board = TaskBoard()
    task = _cross_task(board)
    board.update(task.id, source_worker="")
    assert task.is_cross_project is True, (
        "clearing only the source dropped the cross flag while a target_worker remains"
    )
    assert task.target_worker == "claude-team-config", "the surviving target was disturbed"


def test_an_edit_that_never_mentions_the_workers_leaves_the_flag_alone():
    """The guard that keeps the recompute honest: only an edit that TOUCHES the routing
    may re-derive it.

    Seeded in the exact broken state the operator's #1301-#1303 are in —
    ``is_cross_project`` True with both workers already empty — because that is the only
    state where a correct recompute and an over-broad one DISAGREE. With a normal
    cross-task the two produce the same answer, so an earlier version of this test
    passed against a deliberately broken `if True:` recompute and proved nothing.
    """
    board = TaskBoard()
    task = board.add(SwarmTask(title="routed work", description="x"))
    task.is_cross_project = True  # flag set, routing already lost (the #1301 state)
    board.update(task.id, tags=["hold"])
    assert task.is_cross_project is True, (
        "a tags-only edit silently reclassified the task — the recompute is running on "
        "edits that never mentioned source_worker or target_worker"
    )
    assert task.tags == ["hold"], "positive control: the edit must actually have applied"


def test_setting_a_target_still_marks_it_cross():
    """The original behaviour must survive the fix — this is a recompute, not a removal."""
    board = TaskBoard()
    task = board.add(SwarmTask(title="local work", description="x"))
    assert task.is_cross_project is False
    board.update(task.id, target_worker="hub")
    assert task.is_cross_project is True


# --- bug 1: an off-list worker must survive the round trip --------------------


def _modal_region() -> str:
    """The helper function's body ONLY, bounded by its closing brace.

    A fixed-size window here was a false negative: 1400 chars ran past the helper into
    the neighbouring option-population loop, which has its own ``sel.appendChild(opt)``,
    so deleting the helper's line still found a match. Same too-wide-window mistake as
    the classifier in tests/test_select_worker_ordering.py.
    """
    start = _JS.index("function setSelectValuePreservingUnknown")
    end = _JS.index("\n        }", start)
    return _JS[start:end]


def test_the_scan_finds_the_helper():
    """Positive control — every assertion below reads this region."""
    body = _modal_region()
    assert "createElement('option')" in body, "helper scan is broken"


def test_an_unknown_worker_gets_an_option_instead_of_being_dropped():
    """The fix. Without an option the browser discards the assignment silently, and the
    next save posts an empty string over the stored value."""
    body = _modal_region()
    assert re.search(r"o\.value === val", body), (
        "the helper no longer checks whether an option already matches the value"
    )
    assert "sel.appendChild(opt)" in body, (
        "an off-list worker no longer gets its own <option>, so select.value silently "
        "resolves to '' and the next save wipes the stored target_worker"
    )


def test_the_cross_worker_selects_use_the_preserving_setter():
    """Assigning .value directly is the bug. Both cross-project selects must go through
    the helper, or the one that does not will lose data exactly as before."""
    for field in ("tm-source-worker", "tm-target-worker"):
        direct = re.search(r"getElementById\(['\"]" + field + r"['\"]\)\.value\s*=\s*\w+Val", _JS)
        assert direct is None, (
            f"{field} is still assigned with a direct .value =, which silently drops an "
            f"off-list worker and lets the next save overwrite it with ''"
        )
        assert f"setSelectValuePreservingUnknown('{field}'" in _JS, (
            f"{field} does not go through the preserving setter"
        )


def test_an_empty_value_still_clears_the_select():
    """Clearing must remain possible — the operator explicitly removing a worker has to
    work, so the helper must not treat '' as an unknown value and invent an option."""
    body = _modal_region()
    assert re.search(r"if\s*\(!val\)\s*\{\s*sel\.value\s*=\s*''", body), (
        "the helper no longer short-circuits on an empty value, so clearing a worker "
        "would create a blank option instead of clearing the field"
    )
