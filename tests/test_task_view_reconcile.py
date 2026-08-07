"""The task view must converge on server state, not merely react to a pushed frame.

WHY THIS IS ARCHITECTURE AND NOT ANOTHER PATCH. The task panel was updated purely by
reacting to a ``tasks_changed`` broadcast, and this project has now shipped FOUR
separate fixes for four separate ways that push can be lost: a stranded debounce timer
(#1294), a reconnect that skipped the panel resync, a frame dropped when no event loop
was running, and a filter-restore that failed inside an empty ``catch``. Every one was a
genuine defect. None could have been the last, because a design that only reacts has no
way to notice it missed something — the operator sees a stale board with no error, and
only a manual filter toggle repairs it. He named the result: "flaky", and then "feels
like we just patched it, not fixed it properly".

TWO ROOT CAUSES, both structural, both fixed here rather than worked around.

1. TWO SOURCES OF TRUTH FOR A TASK. The editor was built from ~17 ``data-*`` attributes
   baked into the row at render time, so a row that had not re-rendered made the modal
   display stale values AND write them back on save. That is not hypothetical: it is
   what silently wiped ``target_worker`` on #1301-#1303. Every edit path now fetches
   ``/api/tasks/{id}``, so the editor cannot show or persist anything but current
   server state.

2. NO WAY TO DETECT DRIFT. Reacting to a push is an optimisation for latency;
   correctness needs reconciliation. The board now carries a monotonic version, bumped
   in ``_notify`` — the single choke point every mutating verb already passes through,
   so the counter cannot miss a change without that change also failing to broadcast.
   Renders are stamped with it and the client compares periodically, so a missed frame
   costs one poll interval instead of lasting until someone clicks a filter.

The bug that prompted this: the operator saved #1300 and #1301 repeatedly, saw no
change, and concluded the assignment was not sticking. ``task_history`` showed all
three saves had succeeded and the rows were already ``backlog`` + ``project-root``. He
was re-saving work that had already worked, because the view never told him.
"""

from __future__ import annotations

import re
from pathlib import Path

from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask

_JS = (
    Path(__file__).parent.parent / "src" / "swarm" / "web" / "static" / "dashboard.js"
).read_text()
_TASK_LIST = (
    Path(__file__).parent.parent
    / "src"
    / "swarm"
    / "web"
    / "templates"
    / "partials"
    / "task_list.html"
).read_text()


# --- the version is anchored to the broadcast choke point ---------------------


def test_the_version_advances_on_every_mutation():
    board = TaskBoard()
    seen = [board.version]
    task = board.add(SwarmTask(title="t", description=""))
    seen.append(board.version)
    board.assign(task.id, "api")
    seen.append(board.version)
    board.demote_to_backlog(task.id)
    seen.append(board.version)
    assert seen == sorted(seen) and len(set(seen)) == len(seen), (
        f"the board version did not advance monotonically across mutations: {seen}"
    )


def test_the_version_is_bumped_by_notify_itself():
    """LOAD-BEARING. Bumping inside ``_notify`` — rather than in each verb — is what
    makes the counter trustworthy: a mutation cannot change the version without also
    broadcasting, and cannot broadcast without changing the version. Bump it anywhere
    else and the two can drift, which is the very failure this design removes."""
    board = TaskBoard()
    before = board.version
    board._notify()
    assert board.version == before + 1, (
        "_notify no longer advances the version; if the bump moved into the individual "
        "verbs, a new verb can now broadcast without advancing it — silently "
        "reintroducing undetectable drift"
    )


def test_a_read_only_query_does_not_advance_the_version():
    """Otherwise every poll looks like drift and the client re-renders forever."""
    board = TaskBoard()
    board.add(SwarmTask(title="t", description=""))
    steady = board.version
    _ = board.all_tasks
    _ = board.available_tasks
    _ = board.summary()
    assert board.version == steady, "a read advanced the version; the client would churn"


# --- the version reaches the client ------------------------------------------


def test_the_rendered_partial_carries_the_version():
    """The client can only detect drift if the HTML says which version it is showing."""
    assert 'id="task-board-version"' in _TASK_LIST, (
        "the task list no longer stamps the board version, so the client cannot tell "
        "whether what it is showing is current"
    )
    assert "board_version" in _TASK_LIST


def test_the_partial_handler_supplies_the_version():
    src = Path("src/swarm/web/routes/partials.py").read_text()
    assert '"board_version": d.task_board.version' in src, (
        "handle_partial_tasks does not pass board_version, so the template stamps a "
        "default and every comparison silently succeeds"
    )


def test_the_version_endpoint_is_registered_before_the_id_route():
    """Ordering is load-bearing: aiohttp matches in registration order, so registering
    /api/tasks/version AFTER /api/tasks/{task_id} makes 'version' parse as a task id and
    the probe 404s — which the client swallows, leaving drift undetected."""
    src = Path("src/swarm/server/routes/tasks.py").read_text()
    ver = src.index('add_get("/api/tasks/version"')
    idx = src.index('add_get("/api/tasks/{task_id}"')
    assert ver < idx, (
        "/api/tasks/version is registered after /api/tasks/{task_id}; 'version' will be "
        "matched as a task id"
    )


# --- the editor has exactly one source of truth ------------------------------


def test_no_edit_path_builds_the_modal_from_dom_attributes():
    """ROOT CAUSE 1. A row that has not re-rendered must not be able to populate the
    editor, because whatever it shows also gets written back on save."""
    assert "window.showEditTask = function" not in _JS, (
        "the DOM-sourced editor is back; a stale row can populate the modal again and "
        "saving will persist its stale values (this is what wiped target_worker)"
    )
    dom_opens = re.findall(r"showEditTask\([^)]*dataset", _JS)
    assert not dom_opens, f"an edit path still reads the row's data-* attributes: {dom_opens}"


def test_every_edit_path_fetches_the_task_by_id():
    """Both openers — the Edit button and the row click — must go through the server."""
    assert _JS.count("showTaskEditorById(") >= 3, (
        "expected the definition plus both call sites (Edit button and row click); "
        f"found {_JS.count('showTaskEditorById(')}"
    )
    assert "fetch('/api/tasks/' + encodeURIComponent(taskId)" in _JS, (
        "showTaskEditorById no longer fetches the task, so it is not a source of truth"
    )


def test_the_editor_payload_carries_every_field_the_save_writes_back():
    """The failure mode this must never re-create: a field the editor does not load
    opens blank and is then POSTED blank, overwriting the stored value. Checked against
    the server serializer, so adding a savable field without loading it fails here."""
    src = Path("src/swarm/server/routes/tasks.py").read_text()
    for field in (
        "assigned_worker",
        "source_worker",
        "target_worker",
        "tags",
        "status",
        "priority",
        "acceptance_criteria",
        "context_refs",
        "attachments",
    ):
        assert f'"{field}":' in src, f"the task detail payload omits {field}"
        assert field in _JS, f"showTaskEditorById does not map {field} into the modal"


# --- reconciliation ----------------------------------------------------------


def test_the_client_reconciles_against_the_server_version():
    """ROOT CAUSE 2. Without this the view can only react, and a lost frame is
    permanent until the operator clicks something."""
    assert "function reconcileTaskView" in _JS, "the reconciler is gone"
    assert "/api/tasks/version" in _JS, "the client never asks the server for its version"
    body = _JS[_JS.index("function reconcileTaskView") : _JS.index("window.reconcileTaskView")]
    assert "refreshTasks()" in body, (
        "the reconciler detects drift but does not re-render, so it reports the problem "
        "and leaves it in place"
    )
    assert "document.hidden" in body, (
        "the reconciler polls while the tab is hidden, where nobody can see staleness "
        "and onAppFocus already re-fetches on return"
    )


def test_the_reconciler_is_started_at_init():
    """A reconciler that is never started is the same as not having one — and would
    look identical in every test above."""
    assert "startTaskReconciler();" in _JS, "the reconciler is defined but never started"


def test_the_reconciler_runs_even_when_the_socket_looks_healthy():
    """Every failure it covers — stranded debounce, dead-but-open socket, dropped frame
    — LOOKS like a healthy connection from the browser. Gating the backstop on a
    detectable problem would disable it in exactly the cases that need it."""
    start = _JS.index("function startTaskReconciler")
    body = _JS[start : start + 400]
    assert "setInterval" in body, "the reconciler does not run on a timer"
    for gate in ("ws.readyState", "wasDisconnected", "hasConnectedBefore"):
        assert gate not in body, (
            f"the reconciler is gated on {gate}; the failures it exists to catch all "
            f"present as a healthy socket, so it would be off when it is needed"
        )
