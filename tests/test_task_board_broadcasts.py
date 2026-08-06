"""Every board mutation the dashboard can trigger must BROADCAST (#1275).

OPERATOR-REPORTED: "The task board doesn't update automatically when status changes
assigned/in-progress etc. I have to click on a filter to make it refresh."

THE PATH, traced end to end and found correctly wired:

    board mutation → ``_notify()`` → ``emit("change")``
      → ``daemon._on_task_board_changed`` (daemon.py:573 subscribes)
      → ``publisher.on_task_board_changed`` (state_publisher.py:96)
      → ``_broadcast_ws({"type": "tasks_changed"})``
      → dashboard.js:724 ``case 'tasks_changed'`` → ``refreshTasks()`` + ``refreshWorkers()``

So this file does not test that the board mutated — it tests that the mutation
reaches the *change event*, which is the only thing the dashboard can observe.
That distinction is #1275's AC-3 verbatim: a test asserting the board changed would
pass on every one of the paths that cannot refresh the UI.

WHAT THE AUDIT FOUND. Sweeping every mutating verb on ``TaskBoard`` for ``_notify()``
turned up one genuine hole: ``reassign_worker`` persisted and never notified, while
its immediate sibling ``unassign_worker`` does both. A worker rename moves every one
of that worker's tasks with no event — and the ``tasks_changed`` handler in
dashboard.js even carries a comment ASSUMING reassignment fires it. That is fixed
here.

WHAT IT DID NOT FIND, recorded so nobody re-derives a false cause: every status
transition the edit modal can request already notifies. So on current code the
server side of the operator's symptom is sound, and if staleness persists the fault
is client-side. His original report was made while the running daemon predated the
working tree by a day (#1275 AC-4 exists because of that), so the observation may
have been against code that has since changed.
"""

from __future__ import annotations

import pytest

from swarm.tasks.board import TaskBoard
from swarm.tasks.task import TaskStatus


@pytest.fixture
def board_and_events():
    board = TaskBoard()
    events: list[int] = []
    board.on_change(lambda: events.append(1))
    return board, events


def _assigned(board, worker="alice"):
    t = board.create(title="work")
    board.assign(t.id, worker)
    return t


# --- positive control -------------------------------------------------------


def test_the_change_subscription_actually_fires(board_and_events):
    """POSITIVE CONTROL. Every test below asserts "the event count went up"; if the
    subscription were not wired, they would all fail — but if the counter were wired
    to something that always increments, they would all pass. Pin a known-good verb
    first so an absence below means something."""
    board, events = board_and_events
    board.create(title="anything")
    assert events, "board.on_change never fired for create() — the harness is broken"


# --- the transitions the dashboard's edit modal can request -----------------


@pytest.mark.parametrize(
    "verb",
    [
        "assign",
        "activate",
        "unassign",
        "complete",
        "fail",
        "release",
        "unblock",
        "park",
        "demote_to_backlog",
        "approve_task",
        "reopen",
    ],
)
def test_each_dashboard_reachable_verb_broadcasts(board_and_events, verb):
    """AC-3. Not "did the board change" — "did the change EVENT fire", which is the
    only thing the dashboard can see. A verb that mutates and persists without
    notifying leaves the UI stale until an unrelated event or a manual refresh, which
    is exactly the reported symptom."""
    board, events = board_and_events

    # Drive each verb from a state it accepts, through real verbs only.
    if verb == "assign":
        t = board.create(title="x")
        before = len(events)
        assert board.assign(t.id, "alice")
    elif verb == "activate":
        t = _assigned(board)
        before = len(events)
        assert board.activate(t.id) is not None
    elif verb == "unassign":
        t = _assigned(board)
        before = len(events)
        assert board.unassign(t.id)
    elif verb == "complete":
        t = _assigned(board)
        before = len(events)
        assert board.complete(t.id, "done")
    elif verb == "fail":
        t = _assigned(board)
        before = len(events)
        assert board.fail(t.id)
    elif verb == "release":
        t = _assigned(board)
        before = len(events)
        assert board.release(t.id)
    elif verb == "unblock":
        t = _assigned(board)
        board.activate(t.id)
        board.block_on_external(t.id, "alice", "upstream", "x#1")
        before = len(events)
        assert board.unblock(t.id)
    elif verb == "park":
        t = _assigned(board)
        board.activate(t.id)
        before = len(events)
        assert board.park(t.id, "alice", "setting it down")
    elif verb == "demote_to_backlog":
        t = _assigned(board)
        before = len(events)
        assert board.demote_to_backlog(t.id)
    elif verb == "approve_task":
        t = board.create(title="x")
        board.demote_to_backlog(t.id)
        before = len(events)
        assert board.approve_task(t.id)
    elif verb == "reopen":
        t = _assigned(board)
        board.complete(t.id, "done")
        before = len(events)
        assert board.reopen(t.id)
    else:  # pragma: no cover - parametrisation guard
        raise AssertionError(f"unhandled verb {verb}")

    assert len(events) > before, (
        f"board.{verb}() mutated and persisted but fired NO change event — the "
        f"dashboard cannot learn about it, so the board stays stale until the "
        f"operator clicks something (#1275)"
    )


# --- the hole the audit found ----------------------------------------------


def test_reassign_worker_broadcasts(board_and_events):
    """The one genuine gap the sweep found. ``reassign_worker`` persisted without
    notifying while its sibling ``unassign_worker`` does both, so renaming a worker
    moved every one of its tasks with no event at all. dashboard.js's
    ``tasks_changed`` handler even carries a comment assuming reassignment fires
    it."""
    board, events = board_and_events
    t = _assigned(board, "old-name")
    before = len(events)

    board.reassign_worker("old-name", "new-name")

    assert board.get(t.id).assigned_worker == "new-name", "the rename did not apply"
    assert len(events) > before, (
        "reassign_worker moved tasks between workers without firing a change event"
    )


def test_reassign_worker_that_matches_nothing_is_quiet(board_and_events):
    """Symmetry with the rest of the board: a no-op must not spam the WebSocket.
    Every connected dashboard re-fetches the task list on ``tasks_changed``, so a
    broadcast for zero changes is real wasted work, not a harmless extra."""
    board, events = board_and_events
    _assigned(board, "alice")
    before = len(events)

    board.reassign_worker("nobody-by-that-name", "someone-else")

    assert len(events) == before, "a no-op reassign broadcast anyway"


# --- the sweep itself, so a new verb cannot quietly skip notifying ----------


def test_no_mutating_verb_persists_without_notifying():
    """Guards the whole class rather than the instances above.

    ``reassign_worker`` was found by sweeping for verbs that call ``_persist()`` and
    never ``_notify()`` — persisting means the change is real and durable, so
    persisting without notifying is precisely "the board changed and nothing can
    tell". A new verb with that shape now fails here instead of becoming the next
    stale-dashboard report.
    """
    import inspect

    offenders = []
    for name, fn in sorted(vars(TaskBoard).items()):
        if name.startswith("_") or not callable(fn):
            continue
        try:
            src = inspect.getsource(fn)
        except (OSError, TypeError):
            continue
        if "_persist()" in src and "_notify()" not in src:
            offenders.append(name)

    assert not offenders, (
        f"these verbs persist a mutation but never fire a change event, so the "
        f"dashboard cannot see it: {offenders}. Add self._notify() after "
        f"self._persist(), or route through a verb that does."
    )


def test_that_sweep_can_actually_see_the_verbs():
    """Positive control for the sweep: if ``inspect.getsource`` failed or the class
    were empty, the assertion above would pass by finding nothing."""
    import inspect

    persisting = [
        n
        for n, f in vars(TaskBoard).items()
        if not n.startswith("_")
        and callable(f)
        and "_persist()" in (inspect.getsource(f) if _safe(f) else "")
    ]
    assert len(persisting) > 5, f"sweep sees only {persisting} — it is not working"


def _safe(fn) -> bool:
    import inspect

    try:
        inspect.getsource(fn)
        return True
    except (OSError, TypeError):
        return False


def test_status_after_park_is_assigned_not_active(board_and_events):
    """Sanity anchor: the parametrised test asserts events, not outcomes, so pin one
    real outcome so a verb that fires an event while doing nothing useful is still
    caught."""
    board, _events = board_and_events
    t = _assigned(board)
    board.activate(t.id)
    board.park(t.id, "alice", "down")
    assert board.get(t.id).status == TaskStatus.ASSIGNED


# --- AC-1: the FRAME, through the real publisher ---------------------------


def test_a_status_change_produces_a_tasks_changed_frame():
    """AC-1 verbatim: "evidenced by whether a 'tasks_changed' frame arrives at the
    moment of the status change".

    The tests above stop at the board's change event. This carries it through the
    REAL ``StatePublisher.on_task_board_changed`` to the websocket callback, wiring
    the subscription exactly as ``daemon.py:573`` does, so the whole server side of
    the path is covered rather than its first hop. If this passes and the operator
    still sees a stale board, the remaining fault is client-side — which is the
    discrimination AC-1 asks for.
    """
    from unittest.mock import MagicMock

    from swarm.server.state_publisher import StatePublisher

    frames: list[dict] = []
    publisher = StatePublisher(
        broadcast_ws=frames.append,
        get_workers=lambda: [],
        get_worker_task_map=dict,
        expire_proposals=lambda: None,
        broadcast_proposals=lambda: None,
        clear_worker_inflight=lambda _w: None,
        pending_for_worker=lambda _w: [],
        clear_resolved_proposals=lambda: None,
        update_proposal_status=lambda *_a: None,
        push_notification=lambda *_a, **_k: None,
        notification_bus=MagicMock(),
        drone_log=MagicMock(),
        emit=lambda _e: None,
        get_pressure_level=lambda: "normal",
        pipeline_engine=MagicMock(),
        service_registry=MagicMock(),
        track_task=lambda _t: None,
        mark_dirty=lambda: None,
    )

    board = TaskBoard()
    # Mirrors daemon.py:573 — the one subscription the whole path depends on.
    board.on_change(publisher.on_task_board_changed)

    t = board.create(title="watch me change status")
    board.assign(t.id, "alice")
    frames.clear()

    board.activate(t.id)  # ASSIGNED → ACTIVE, the operator's "assigned/in-progress"

    assert frames, "no websocket frame at all for a status change"
    assert any(f.get("type") == "tasks_changed" for f in frames), (
        f"a frame was sent but none was tasks_changed: {[f.get('type') for f in frames]}"
    )
