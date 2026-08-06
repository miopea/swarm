"""The CLIENT half of a live task-panel update (#1294 AC-4).

Every guard added for this bug class so far asserts the SERVER emitted a frame:
tests/test_task_board_broadcasts.py (the change event fires for all 11 verbs) and
tests/test_mcp_completion_reaches_the_browser.py (a real client receives it over a real
socket). Both would stay green if the frame arrived and changed nothing on screen —
which is precisely the operator's report. AC-4 asks for the other half.

Three ways a delivered frame changes nothing, all silent, none caught by a server-side
test:

1. **The swap has nowhere to land.** ``refreshTasks`` does
   ``htmx.ajax('GET', url, '#task-list')``. Rename that container in the template and
   htmx resolves nothing, throws nothing, and logs nothing — the request may even
   succeed. Asserted for every htmx target in the file, not just this one, because the
   failure mode is shared.

2. **The handler stops calling the refresher.** The ``tasks_changed`` case is the only
   thing that turns a frame into a re-fetch.

3. **The re-fetch loses the filter.** This is the sharpest one and it reproduces the
   report exactly. The operator's chips are Backlog + Unassigned + Assigned + In
   Progress + Blocked with Done OFF. If ``refreshTasks`` sends no ``status=`` param the
   server returns the UNFILTERED list — which includes DONE (see
   tests/test_task_list_visibility.py) — so the closed row is re-rendered rather than
   removed, and clicking any chip then filters it away. Frame delivered, refresh
   performed, nothing appears to happen. So the param must come from the SAME state the
   chip handler mutates; two sources of truth for the filter is the defect.

The restore-on-load path is where 3 can actually happen: it populates
``activeTaskFilters`` from localStorage inside a ``try``, and that ``catch`` used to be
empty. A part-way failure left the Set empty while the chips still read as active from
the previous render. Now it logs.

WHAT THIS FILE CANNOT DO: it reads source, so it cannot prove the browser executed any
of it. It closes the "arrives and changes nothing" gap by construction, not by
observation. #1294's remaining unknown — whether the frame reaches his device at all —
needs his browser, and that is stated on the ticket rather than papered over here.
"""

from __future__ import annotations

import re
from pathlib import Path

_WEB = Path(__file__).parent.parent / "src" / "swarm" / "web"
_JS = (_WEB / "static" / "dashboard.js").read_text()
_TEMPLATES = list((_WEB / "templates").rglob("*.html"))
_TEMPLATE_TEXT = "\n".join(p.read_text() for p in _TEMPLATES)


def _strip_comment_lines(src: str) -> str:
    """Blank whole-line comments. Scans here have matched the prose explaining a bug
    instead of the bug three times now; the comment added with this fix names
    ``status=`` and ``activeTaskFilters`` while describing the failure."""
    return "\n".join(
        "" if ln.lstrip().startswith(("//", "/*", "*/", "* ")) else ln for ln in src.split("\n")
    )


_CODE = _strip_comment_lines(_JS)


def test_the_scan_finds_the_real_call_sites():
    """Positive control. Without it every assertion below could pass over an empty set."""
    assert _TEMPLATES, "no templates found; the path is wrong"
    assert "htmx.ajax(" in _CODE, "no htmx.ajax calls found; this scan is broken"
    assert "case 'tasks_changed'" in _CODE, "the tasks_changed case is gone"


def test_every_htmx_swap_target_exists_in_a_template():
    """Failure 1. A swap into a missing id is the purest form of "the frame arrived and
    changed nothing": no error, no log, and the fetch may even return 200."""
    targets = re.findall(r"htmx\.ajax\(\s*'[A-Z]+'\s*,[^,]+,\s*'#([\w-]+)'", _CODE)
    assert targets, "no htmx.ajax swap targets parsed; the regex no longer matches"
    missing = [t for t in set(targets) if f'id="{t}"' not in _TEMPLATE_TEXT]
    assert not missing, (
        f"htmx swaps into id(s) {sorted(missing)} that no template defines, so those "
        f"refreshes silently do nothing. Parsed targets: {sorted(set(targets))}"
    )


def test_the_tasks_changed_handler_refreshes_the_task_list():
    """Failure 2. This case is the only thing that converts a frame into a re-fetch."""
    idx = _CODE.index("case 'tasks_changed'")
    body = _CODE[idx : _CODE.index("break", idx)]
    assert "refreshTasks()" in body, (
        "the tasks_changed WS case no longer calls refreshTasks(), so a delivered frame "
        "cannot update the panel"
    )


def _refresh_tasks_body() -> str:
    start = _CODE.index("function refreshTasks()")
    return _CODE[start : _CODE.index("\n    }", start)]


def test_the_refresh_sends_the_status_filter():
    """Failure 3, the one that reproduces the operator's report exactly. No ``status=``
    param means the unfiltered list comes back, DONE included, and the closed row is
    re-rendered instead of removed."""
    body = _refresh_tasks_body()
    assert "status=" in body, (
        "refreshTasks no longer sends a status= param, so it re-fetches the UNFILTERED "
        "list. With the operator's chips (Done OFF) a completed task would be "
        "re-rendered rather than removed — the frame arrives and nothing appears to "
        "happen, which is #1294's symptom"
    )
    assert "activeTaskFilters" in body, (
        "refreshTasks builds status= from something other than activeTaskFilters"
    )


def test_the_refresh_and_the_chip_handler_read_the_same_filter_state():
    """Two sources of truth for the filter IS the bug, not a style question: the chips
    can read as active while the re-fetch asks for everything."""
    assert "window.switchTaskFilter" in _CODE, (
        "the chip handler switchTaskFilter is gone; if it was renamed, this test and "
        "refreshTasks must be re-pointed at whatever now owns the filter state"
    )
    start = _CODE.index("window.switchTaskFilter")
    chip_body = _CODE[start : _CODE.index("\n    }", start)]

    # A conjunction, deliberately. An `or` here would pass on finding the handler at all,
    # which says nothing about the two halves agreeing — and their disagreeing is the bug.
    assert "activeTaskFilters" in chip_body, (
        "the chip handler no longer mutates activeTaskFilters, so clicking a chip and "
        "re-fetching on a frame now read different state"
    )
    assert "activeTaskFilters" in _refresh_tasks_body(), (
        "refreshTasks no longer reads activeTaskFilters, so a frame-driven refresh can "
        "ask for a different filter than the chips display"
    )


def test_restoring_saved_filters_does_not_fail_silently():
    """The path where failure 3 actually happens. Restoring localStorage filters runs in
    a try; that catch used to be `catch(e) {}`. A part-way failure left
    activeTaskFilters EMPTY while the chips still read as active from the previous
    render, so the next refresh quietly fetched everything."""
    idx = _CODE.index("localStorage.getItem('swarm_task_filter')")
    # The enclosing IIFE's catch, bounded by the end of that block.
    tail = _CODE[idx : idx + 2500]
    catch_at = tail.index("catch")
    catch_body = tail[catch_at : catch_at + 400]
    assert re.search(r"console\.(error|warn)", catch_body), (
        "the saved-filter restore still swallows its exception. If it throws part-way "
        "the panel runs UNFILTERED behind chips that look active, and nothing says so"
    )
