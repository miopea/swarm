"""BLOCKED must be visible on every task surface (#1277, #1278, #1279).

OPERATOR-REPORTED, 2026-08-06: BLOCKED tasks not visible in the task editor at
all, and open tasks (#1275 named) could not be edited. Neither was an edit-
permission defect — ``task_manager.edit_task`` has no status or assignment
guard, and ``task_list.html`` has no ``show = false`` rule for the edit action,
so the button renders for every status. **The rows were simply never rendered.**

ONE CLASS ON THREE SURFACES, measured against the live board (1235 tasks):

1. **The list** (#1277) — ``_paginate`` truncates the unfiltered view at
   ``MAX_QUERY_LIMIT`` (1000) while ``all_tasks`` sorts OLDEST-FIRST, and 1226 of
   1235 tasks are DONE, so the newest open work is exactly what gets pushed out.
2. **The filter bar** (#1278) — no ``blocked`` chip, so BLOCKED could only be
   asked for under "All", where cause 1 then truncated it.
3. **The count** (#1279) — ``board.summary()`` counts backlog, unassigned,
   assigned+active, done and failed, and NEVER counts blocked, so the total does
   not add up.

Composed, 1 and 2 left #1255 visible in **no reachable filter state at all**.
Cause 3 is what produced the number the operator actually quoted: reconstructed
from ``task_history`` at 2026-08-06 00:20–00:30 the board held 9 open tasks and
``summary()`` reported 6, omitting the three that were BLOCKED. The Queen's
"9 − 3 = 6" was the correct explanation, not the coincidence I first called it.

WHY THE LIMIT IS NOT THE THING TO RAISE. ``_paginate``'s own docstring records
this defect shipping once before at limit 100 ("silently truncated any swarm
with more than 100 tasks the moment a filter chip was clicked"). The fix raised
the ceiling to 1000, and it recurred the moment the board crossed 1000. So these
tests assert the two properties that survive any ceiling: **open work is never
the part that gets dropped**, and **a truncation the user cannot see is the
defect regardless of the number**.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from swarm.server.helpers import MAX_QUERY_LIMIT
from swarm.tasks.task import TaskStatus

_WEB = Path(__file__).parent.parent / "src" / "swarm" / "web"
_DASHBOARD = _WEB / "templates" / "dashboard.html"
_TASK_LIST = _WEB / "templates" / "partials" / "task_list.html"


# --- the filter chips the operator can actually click ----------------------


def _chips() -> set[str]:
    """Status filter values offered by the dashboard.

    Read from the markup rather than a constant: the chips ARE the reachable
    filter states, and a constant would let the list and the UI drift.
    """
    html = _DASHBOARD.read_text()
    bar = html.split('id="task-filters"', 1)[1].split("</div>", 1)[0]
    return set(re.findall(r'data-filter="([a-z]+)"', bar))


def test_the_chip_scan_is_honest():
    """Positive control. A scan that silently matched nothing would make every
    assertion below pass for the wrong reason — which is exactly how an empty
    board once "confirmed" a truncation that was really a 401."""
    chips = _chips()
    assert "all" in chips, "chip scan cannot see the All chip — the scan is broken"
    assert "done" in chips, "chip scan cannot see the Done chip — the scan is broken"
    assert len(chips) >= 5, f"chip scan found only {chips} — markup shape changed"


@pytest.mark.parametrize("status", [s for s in TaskStatus if s is not TaskStatus.BACKLOG])
def test_every_status_has_its_own_filter_chip(status):
    """Cause 2. Without a chip, a status is reachable only under "All", where
    truncation can then hide it — so "no chip" is not cosmetic, it removes the
    only view that could have shown the task once "All" overflows.

    BACKLOG is exempt only because it already has a chip; it is excluded from
    the parametrisation to keep the failure message about the missing one.
    """
    assert status.value in _chips(), (
        f"no dashboard filter chip for status '{status.value}' — it is reachable "
        f"only under All, where _paginate can truncate it out of existence. "
        f"Chips present: {sorted(_chips())}"
    )


# --- the truncation must be observable ------------------------------------


def test_the_template_surfaces_truncation():
    """``handle_partial_tasks`` already computes ``task_total`` and
    ``task_has_more`` and the template used NEITHER — the signal was computed
    and thrown away. A limit the user cannot observe is the defect; the value of
    the limit is not."""
    tpl = _TASK_LIST.read_text()
    assert "task_has_more" in tpl or "task_total" in tpl, (
        "task_list.html renders no truncation notice, so dropping 234 tasks "
        "looks identical to having 1000 — this is what made the defect silent"
    )


# --- open work is never what gets dropped ---------------------------------


def _display_order(tasks: list[Any]) -> list[Any]:
    """The display ordering under test, imported from the code that owns it."""
    from swarm.server.helpers import _display_sort

    return _display_sort(tasks)


class _T:
    """Minimal stand-in for a task dict as ``_task_dicts`` emits it."""

    def __init__(self, number: int, status: str, priority: str = "normal"):
        self.number = number
        self.status = status
        self.priority = priority

    def __repr__(self) -> str:
        return f"#{self.number}:{self.status}"


def _as_dicts(items: list[_T]) -> list[dict[str, Any]]:
    return [{"number": t.number, "status": t.status, "priority": t.priority} for t in items]


def test_open_tasks_survive_truncation_at_the_real_board_shape():
    """The load-bearing assertion, at the shape that actually broke.

    The live board was 1226 DONE out of 1234. Under an oldest-first sort the
    open tasks are the newest rows, so they are precisely what a 1000-item
    ceiling discards. This builds that shape and asserts every open task is
    inside the rendered window.
    """
    done = [_T(n, "done") for n in range(1, 1227)]
    open_ = [
        _T(1269, "unassigned"),
        _T(1270, "unassigned"),
        _T(1274, "unassigned"),
        _T(1275, "unassigned"),
        _T(1255, "blocked"),
        _T(915, "blocked"),
        _T(1276, "assigned"),
        _T(1277, "active"),
    ]
    ordered = _display_order(_as_dicts(done + open_))
    window = {t["number"] for t in ordered[:MAX_QUERY_LIMIT]}

    missing = sorted(t.number for t in open_ if t.number not in window)
    assert not missing, (
        f"open tasks {missing} fall outside the first {MAX_QUERY_LIMIT} rendered "
        f"rows — the operator cannot see or edit them from the default view"
    )


def test_truncation_drops_completed_work_not_open_work():
    """The complement: something still gets dropped past the ceiling, and it
    must be finished work. Asserting only "open survives" would also pass if the
    limit were quietly raised to infinity, which is the recurrence this file
    exists to prevent."""
    done = [_T(n, "done") for n in range(1, 1227)]
    open_ = [_T(9001, "unassigned"), _T(9002, "blocked")]
    ordered = _display_order(_as_dicts(done + open_))

    dropped = ordered[MAX_QUERY_LIMIT:]
    assert dropped, "nothing dropped at this size — the ceiling stopped applying"
    assert all(t["status"] == "done" for t in dropped), (
        "truncation discarded a non-done task: "
        f"{[t['number'] for t in dropped if t['status'] != 'done']}"
    )


def test_the_partial_actually_applies_the_display_sort():
    """WIRING, and the reason the two tests above are not sufficient on their own.

    They exercise ``_display_sort`` directly, so they would both still pass if
    ``handle_partial_tasks`` never called it — a correct helper that nothing
    invokes is precisely the shape of #1104's dead ``board.unblock``. This drives
    the real handler at the real board shape and asserts the open tasks come back
    in the rendered page.
    """
    import asyncio
    from unittest.mock import MagicMock

    from aiohttp.test_utils import make_mocked_request

    import swarm.web.app  # noqa: F401  # breaks the partials<->app circular import
    from swarm.web.routes import partials

    open_numbers = {1275, 1274, 1270, 1269, 1255, 915}
    rows = [{"number": n, "status": "done", "priority": "normal"} for n in range(1, 1227)]
    rows += [
        {
            "number": n,
            "status": "blocked" if n in (1255, 915) else "unassigned",
            "priority": "normal",
        }
        for n in sorted(open_numbers)
    ]

    daemon = MagicMock()
    daemon.task_board.summary.return_value = "1232 tasks"
    daemon.config.task_buttons = []
    request = make_mocked_request("GET", "/partials/tasks")
    request.app["daemon"] = daemon

    monkey = partials._task_dicts
    try:
        partials._task_dicts = lambda _d: list(rows)  # type: ignore[assignment]
        ctx = asyncio.run(partials.handle_partial_tasks.__wrapped__(request))
    finally:
        partials._task_dicts = monkey  # type: ignore[assignment]

    assert ctx["task_total"] == len(rows), "positive control: the handler saw every row"
    rendered = {t["number"] for t in ctx["tasks"]}
    missing = sorted(open_numbers - rendered)
    assert not missing, (
        f"the handler dropped open tasks {missing} — _display_sort is not wired "
        f"into handle_partial_tasks, or runs after _paginate instead of before"
    )
    assert ctx["task_has_more"] is True, (
        "nothing was truncated at 1232 rows, so this test is no longer exercising "
        "the condition it was written for"
    )


# --- the count must account for every task (#1279) ------------------------


def test_summary_accounts_for_every_task_including_blocked():
    """#1279, and the surface that produced the operator's number.

    ``summary()`` counted backlog + unassigned + assigned/active + done + failed
    and never blocked, so a blocked task was in ``total`` and in no category. The
    dashboard renders this string into ``#task-summary`` verbatim
    (``dashboard.js:11262``), so it told the operator 6 while 9 were open.

    Asserted as a COMPLETENESS INVARIANT — the parts must sum to the total —
    rather than as "the word blocked appears". A blocked-specific assertion would
    pass while the next status added without a category vanished exactly the same
    way, and a status with no category is how both this and #1278 happened.
    """
    from swarm.tasks.board import TaskBoard

    board = TaskBoard()
    made = {}
    for status in TaskStatus:
        t = board.create(title=f"a {status.value} task")
        made[status] = t
        # Drive each task to its status through the real verbs, so this reflects
        # reachable board states rather than hand-set fields.
        if status is TaskStatus.UNASSIGNED:
            board.assign(t.id, "alice")
            board.unassign(t.id)
        elif status is TaskStatus.ASSIGNED:
            board.assign(t.id, "alice")
        elif status is TaskStatus.ACTIVE:
            board.assign(t.id, "bob")
            board.activate(t.id)
        elif status is TaskStatus.BLOCKED:
            board.assign(t.id, "carol")
            board.activate(t.id)
            board.block_on_external(t.id, "carol", "upstream", "platform#1")
        elif status is TaskStatus.DONE:
            board.assign(t.id, "dave")
            board.complete(t.id, "done")
        elif status is TaskStatus.FAILED:
            board.assign(t.id, "erin")
            board.fail(t.id)

    present = {t.status for t in board.all_tasks}
    assert TaskStatus.BLOCKED in present, (
        "positive control: no blocked task on the board, so this test could not "
        "detect the defect it was written for"
    )

    summary = board.summary()
    total = len(board.all_tasks)
    counted = sum(int(n) for n in re.findall(r"(\d+)\s+(?!tasks)", summary))
    assert counted == total, (
        f"summary() accounts for {counted} of {total} tasks — some status is in "
        f"no category and is silently uncounted. Statuses present: "
        f"{sorted(s.value for s in present)}. Summary was: {summary!r}"
    )


def test_display_sort_does_not_reorder_the_dispatch_view():
    """CONSTRAINT (#1270): visible must not become startable. The display sort
    lives in the web layer and must not be reachable from the board's own
    ordering, which dispatch reads — 40 call sites depend on ``all_tasks``."""
    board_src = (Path(__file__).parent.parent / "src" / "swarm" / "tasks" / "board.py").read_text()
    assert "_display_sort" not in board_src, (
        "the display sort leaked into tasks/board.py — dispatch ordering reads "
        "all_tasks, and reordering it changes which task a worker picks up"
    )


# --- #1283: ASSIGNED and ACTIVE are separate lanes in the count -------------


def test_summary_distinguishes_queued_from_in_progress():
    """#1283, the sixth surface of one conflation.

    ``summary()`` summed ASSIGNED and ACTIVE into a single "in progress" figure, so
    the live board read "4 in progress" while exactly ONE task was ACTIVE. Same
    defect as queued work appearing in the worker title bar (2026.8.6.6) and the
    idle nudge calling queued tasks "active" (2026.8.6.7).
    """
    from swarm.tasks.board import TaskBoard

    board = TaskBoard()
    queued = board.create(title="queued")
    board.assign(queued.id, "alice")
    running = board.create(title="running")
    board.assign(running.id, "bob")
    board.activate(running.id)

    summary = board.summary()
    assert "1 queued" in summary, f"ASSIGNED not reported as queued: {summary!r}"
    assert "1 in progress" in summary, f"ACTIVE not reported as in progress: {summary!r}"


def test_every_status_has_a_summary_lane_even_when_zero():
    """Stronger than the sum invariant, which only catches an omitted status if a
    task in that status happens to exist. #1279's BLOCKED omission was detectable
    only because a blocked task was on the board. Deriving the lanes from
    ``TaskStatus`` makes the omission impossible; this asserts that it stays so."""
    import inspect

    from swarm.tasks.board import TaskBoard

    src = inspect.getsource(TaskBoard.summary)
    missing = [s.name for s in TaskStatus if f"TaskStatus.{s.name}" not in src]
    assert not missing, (
        f"statuses with no lane in summary(): {missing} — a task in one of those "
        f"would be counted nowhere and the line would not add up"
    )


def test_summary_has_no_stray_punctuation():
    """The parts used to be appended pre-punctuated and joined with a space, which
    produced "1233 done , 2 blocked" on the operator's dashboard."""
    from swarm.tasks.board import TaskBoard

    board = TaskBoard()
    t = board.create(title="blocked one")
    board.assign(t.id, "alice")
    board.activate(t.id)
    board.block_on_external(t.id, "alice", "upstream", "x#1")
    summary = board.summary()
    assert " ," not in summary, f"stray space before a comma: {summary!r}"
    assert ",," not in summary
    assert not summary.rstrip().endswith(","), f"trailing comma: {summary!r}"


# --- what the DEFAULT view returns, and why a finished task stays on screen (#1294)


def _run_partial(rows: list[dict[str, Any]], query: str = "") -> dict[str, Any]:
    """Drive the real handler over *rows*, optionally with a query string."""
    import asyncio
    from unittest.mock import MagicMock

    from aiohttp.test_utils import make_mocked_request

    import swarm.web.app  # noqa: F401  # breaks the partials<->app circular import
    from swarm.web.routes import partials

    daemon = MagicMock()
    daemon.task_board.summary.return_value = "x"
    daemon.config.task_buttons = []
    request = make_mocked_request("GET", "/partials/tasks" + (f"?{query}" if query else ""))
    request.app["daemon"] = daemon

    original = partials._task_dicts
    try:
        partials._task_dicts = lambda _d: list(rows)  # type: ignore[assignment]
        return asyncio.run(partials.handle_partial_tasks.__wrapped__(request))
    finally:
        partials._task_dicts = original  # type: ignore[assignment]


def _rows() -> list[dict[str, Any]]:
    """Three open tasks, one just-finished normal task, and older finished work at
    higher priority — the live board's shape on 2026-08-06."""
    return [
        {"number": 1294, "status": "active", "priority": "normal"},
        {"number": 1290, "status": "blocked", "priority": "normal"},
        {"number": 1255, "status": "assigned", "priority": "normal"},
        {"number": 1292, "status": "done", "priority": "normal"},  # just completed
        {"number": 334, "status": "done", "priority": "high"},
        {"number": 326, "status": "done", "priority": "urgent"},
    ]


def test_the_unfiltered_view_returns_finished_tasks():
    """With NO status filter the handler returns finished tasks too. Pinned so that
    hiding finished work by default would be a deliberate decision with this test
    updated, rather than a silent change to what the operator is looking at — he
    confirmed 2026-08-06 that the panel should keep obeying the chips as it does.

    THIS IS NOT AN EXPLANATION OF #1294, and an earlier version of this docstring
    wrongly offered it as one. I argued the unfiltered view would re-render a
    completed row instead of removing it, so "it didn't disappear" might be correct
    behaviour rather than a stale panel. The operator's screenshot killed that: his
    chips are Backlog+Unassigned+Assigned+In Progress+Blocked with Done and Failed
    OFF, so #1292 was never in his filtered set once it closed and a real refresh
    would have dropped it. #1294 is a genuine live-update failure. Kept as a
    reminder that a plausible mechanism found by reading code is not evidence about
    what the operator was looking at — one screenshot outranked the whole argument."""
    numbers = {t["number"] for t in _run_partial(_rows())["tasks"]}
    assert 1292 in numbers, (
        "the unfiltered view no longer returns finished tasks. If that is the new "
        "intended default, update this test and #1294 — it changes the meaning of "
        "every past report about rows not disappearing"
    )
    assert {1294, 1290, 1255} <= numbers, "positive control: open work is still returned"


def test_an_open_status_filter_excludes_finished_tasks():
    """The other half of the fork. Under a filter that names only open statuses, a
    completed task IS dropped — so if the operator had such a filter active, the row
    failing to disappear WOULD be a stale panel. The two cases are distinguishable
    only by which chip was active, which is why that is the question to ask him."""
    ctx = _run_partial(_rows(), "status=assigned,active,blocked")
    numbers = {t["number"] for t in ctx["tasks"]}
    assert 1292 not in numbers, "a finished task survived an open-status-only filter"
    assert {1294, 1290, 1255} <= numbers, "the filter dropped open work it should keep"


def test_a_just_finished_task_does_not_render_next_to_the_open_work():
    """The non-obvious mechanism, and worth pinning because it is what makes the
    unfiltered view confusing rather than merely verbose.

    ``_display_sort`` orders finished tasks by PRIORITY before recency, so a
    just-completed normal-priority task sorts BELOW every older high/urgent one.
    On the live board that put #1292 at row 26 — under ~22 months-old done tasks —
    so it neither disappears nor appears where the operator would look for it."""
    ordered = _run_partial(_rows())["tasks"]
    positions = {t["number"]: i for i, t in enumerate(ordered)}
    assert positions[1292] > positions[326], (
        "a just-finished normal task now sorts above older urgent finished work; if "
        "that was deliberate, this test and #1294's analysis both need revisiting"
    )
    assert max(positions[n] for n in (1294, 1290, 1255)) < positions[326], (
        "positive control: all open work must still sort above all finished work"
    )
