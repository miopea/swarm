"""#1070 — declaring "blocked on a human decision".

hub was nudged THREE TIMES on #1065: PR #299 done and green, blocked only
on operator authorization to merge. ``swarm_report_blocker`` needs an
integer ``blocked_by_task`` and no task represents "Brad has not approved
the merge yet", so the state could not be declared at all.

Repeated nudges on unactionable state train workers to ignore nudges,
which is exactly when a real one gets missed.

Every test below uses that scenario as the fixture.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from swarm.drones.idle_watcher import IdleWatcher
from swarm.mcp.handlers._task_format import _apply_task_filter
from swarm.mcp.tools import handle_tool_call
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import AWAITING_OPERATOR_REF, TaskStatus
from tests.conftest import make_daemon

# hub's real case, verbatim.
HUB_REASON = "PR #299 is green (CI run 30568610413), needs merge authorization to main"


def _hub_1065(d, worker: str = "api"):
    """#1065: done, green, waiting on a merge the worker cannot authorize."""
    t = d.task_board.create(title="rcg-hub: 67 frontend test files never run")
    d.task_board.assign(t.id, worker)
    d.task_board.activate(t.id)
    return d.task_board.get(t.id)


# --- AC-1: declare it without inventing a placeholder task --------------


def test_worker_declares_a_human_blocker_with_no_task_number(monkeypatch) -> None:
    d = make_daemon(monkeypatch)
    t = _hub_1065(d)

    out = str(handle_tool_call(d, "api", "swarm_block_on_operator", {"reason": HUB_REASON}))

    assert "AWAITING OPERATOR" in out
    got = d.task_board.get(t.id)
    assert got.status is TaskStatus.BLOCKED
    assert got.is_awaiting_operator
    assert got.block_reason == HUB_REASON
    # The whole point: no placeholder task was invented to satisfy a
    # required integer field.
    assert len(d.task_board.all_tasks) == 1


def test_reason_is_required(monkeypatch) -> None:
    """A blocker the operator cannot act on without a follow-up question
    is barely better than no blocker."""
    d = make_daemon(monkeypatch)
    _hub_1065(d)
    out = str(handle_tool_call(d, "api", "swarm_block_on_operator", {"reason": "  "}))
    assert "Missing 'reason'" in out


def test_task_stays_owned_by_the_worker(monkeypatch) -> None:
    d = make_daemon(monkeypatch)
    t = _hub_1065(d, worker="api")
    handle_tool_call(d, "api", "swarm_block_on_operator", {"reason": HUB_REASON})
    assert d.task_board.get(t.id).assigned_worker == "api"


# --- AC-2: the IdleWatcher stops nudging --------------------------------


def test_idle_watcher_stops_nudging_an_awaiting_operator_task(monkeypatch) -> None:
    """The nudge loop hub actually hit. A BLOCKED task leaves
    ``active_tasks``, which is what the watcher buckets over."""
    d = make_daemon(monkeypatch)
    t = _hub_1065(d)

    before = [x.id for x in d.task_board.active_tasks]
    assert t.id in before, "precondition: it was nudgeable"

    handle_tool_call(d, "api", "swarm_block_on_operator", {"reason": HUB_REASON})

    after = [x.id for x in d.task_board.active_tasks]
    assert t.id not in after

    watcher = IdleWatcher(
        task_board=d.task_board,
        send_to_worker=MagicMock(),
        drone_log=d.drone_log,
        drone_config=d.config.drones,
    )
    assert "api" not in watcher._bucket_active_tasks_by_worker()


# --- AC-3: it CLEARS, and the worker resumes without a Queen prompt -----


def test_operator_action_unblocks_and_the_worker_resumes(monkeypatch) -> None:
    """A blocker you can set but not clear just moves the problem — #1059
    found BLOCKED tasks had no exit at all. This one ships with its exit."""
    d = make_daemon(monkeypatch)
    t = _hub_1065(d, worker="api")
    handle_tool_call(d, "api", "swarm_block_on_operator", {"reason": HUB_REASON})

    assert d.task_board.unblock(t.id) is True

    got = d.task_board.get(t.id)
    assert got.status is TaskStatus.ASSIGNED, "back in the worker's queue"
    assert got.assigned_worker == "api", "still theirs — no reassignment needed"
    assert not got.is_awaiting_operator
    assert got.block_reason == "" and got.external_blocker_ref == ""
    # Resumable by the normal momentum machinery, so no Queen prompt.
    assert got in d.task_board.active_tasks_for_worker("api")


def test_unblock_refuses_a_task_that_is_not_blocked(monkeypatch) -> None:
    d = make_daemon(monkeypatch)
    t = _hub_1065(d)
    assert d.task_board.unblock(t.id) is False


def test_unblock_preserves_one_active_task_per_worker(monkeypatch) -> None:
    """#405 INV-1 / #611: unblock lands in ASSIGNED, never ACTIVE, so it
    cannot mint a second active task for a worker already running one."""
    d = make_daemon(monkeypatch)
    parked = _hub_1065(d, worker="api")
    handle_tool_call(d, "api", "swarm_block_on_operator", {"reason": HUB_REASON})

    other = d.task_board.create(title="something else")
    d.task_board.assign(other.id, "api")
    d.task_board.activate(other.id)

    d.task_board.unblock(parked.id)

    active = [x for x in d.task_board.all_tasks if x.status is TaskStatus.ACTIVE]
    assert [x.id for x in active] == [other.id]


# --- AC-4: visible and filterable so the Queen can batch ----------------


def test_awaiting_operator_is_filterable(monkeypatch) -> None:
    d = make_daemon(monkeypatch)
    waiting = _hub_1065(d, worker="api")
    handle_tool_call(d, "api", "swarm_block_on_operator", {"reason": HUB_REASON})

    busy = d.task_board.create(title="ordinary in-flight work")
    d.task_board.assign(busy.id, "web")
    d.task_board.activate(busy.id)

    got = _apply_task_filter(
        d.task_board.all_tasks, "awaiting-operator", "api", include_completed=False
    )

    assert [x.id for x in got] == [waiting.id]


def test_queen_board_view_filters_awaiting_operator(monkeypatch) -> None:
    """The Queen's batching view — one operator ask instead of N relays."""
    from swarm.mcp.queen_handlers._views import HANDLERS

    d = make_daemon(monkeypatch)
    _hub_1065(d, worker="api")
    handle_tool_call(d, "api", "swarm_block_on_operator", {"reason": HUB_REASON})
    ordinary = d.task_board.create(title="not waiting on anyone")
    d.task_board.assign(ordinary.id, "web")

    out = str(HANDLERS["queen_view_task_board"](d, "queen", {"status": "awaiting-operator"}))

    assert "67 frontend test files" in out
    assert "not waiting on anyone" not in out


def test_awaiting_operator_is_distinct_from_blocked_on_an_artifact(monkeypatch) -> None:
    """Blocked-on-upstream is a different class: someone in the swarm may
    still be able to move it, and it must not land in the operator batch."""
    d = make_daemon(monkeypatch)
    t = _hub_1065(d)
    d.task_board.block_on_external(t.id, "api", "https://github.com/vendor/lib/pull/42", "waiting")

    got = d.task_board.get(t.id)
    assert got.status is TaskStatus.BLOCKED
    assert not got.is_awaiting_operator
    assert (
        _apply_task_filter(
            d.task_board.all_tasks, "awaiting-operator", "api", include_completed=False
        )
        == []
    )


def test_the_marker_is_a_named_constant_not_a_magic_string() -> None:
    board = TaskBoard()
    t = board.create(title="x")
    board.assign(t.id, "api")
    board.activate(t.id)
    board.block_on_external(t.id, "api", AWAITING_OPERATOR_REF, "why")
    assert board.get(t.id).is_awaiting_operator
