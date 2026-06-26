"""Task #876: blocked-on-external state — a worker parks its own task as
BLOCKED on an UPSTREAM/EXTERNAL dependency (no internal task number), which
suppresses idle-watcher nudges, stays tracked on the open board, carries a
watch reference, and clears back to active on resume.
"""

from __future__ import annotations

import pytest

from swarm.db.task_store import _row_to_task, _task_to_row
from swarm.drones.log import SystemAction
from swarm.mcp.handlers._block_external import _handle_block_on_external
from swarm.mcp.handlers._task_format import _apply_task_filter
from swarm.mcp.tools import _HANDLERS, TOOLS
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskAction
from swarm.tasks.task import TaskStatus
from tests.conftest import make_daemon


@pytest.fixture
def board():
    return TaskBoard()


@pytest.fixture
def daemon(monkeypatch):
    return make_daemon(monkeypatch)


def _active(board, title, worker):
    t = board.create(title=title)
    board.assign(t.id, worker)
    board.activate(t.id)
    return board.get(t.id)


# --- TaskBoard.block_on_external ---------------------------------------


def test_block_active_task_records_ref_and_reason(board):
    t = _active(board, "eslint-10 migration", "swarm")
    assert board.block_on_external(t.id, "swarm", "npm eslint@^10", "awaiting upstream") is True
    got = board.get(t.id)
    assert got.status == TaskStatus.BLOCKED
    assert got.external_blocker_ref == "npm eslint@^10"
    assert got.block_reason == "awaiting upstream"
    assert got.assigned_worker == "swarm"  # still owns it


def test_block_assigned_task_also_works(board):
    t = board.create(title="queued")
    board.assign(t.id, "swarm")  # ASSIGNED, not yet ACTIVE
    assert board.block_on_external(t.id, "swarm", "vendor PR #42", "waiting") is True
    assert board.get(t.id).status == TaskStatus.BLOCKED


def test_block_rejects_non_owner(board):
    t = _active(board, "x", "swarm")
    assert board.block_on_external(t.id, "other", "ref", "r") is False
    assert board.get(t.id).status == TaskStatus.ACTIVE  # untouched


def test_block_rejects_terminal_and_missing(board):
    t = _active(board, "x", "swarm")
    board.complete(t.id)  # DONE
    assert board.block_on_external(t.id, "swarm", "ref", "r") is False
    assert board.block_on_external("missing-id", "swarm", "ref", "r") is False


# --- Criterion #1: not in active_tasks → idle-watcher never nudges -------


def test_blocked_task_excluded_from_active_tasks(board):
    t = _active(board, "x", "swarm")
    board.block_on_external(t.id, "swarm", "ref", "r")
    # active_tasks is the IdleWatcher's nudge input — BLOCKED must be absent.
    assert board.active_tasks == []
    assert board.active_tasks_for_worker("swarm") == []


# --- Criterion #3: still tracked / visible on the open board ------------


def test_blocked_task_still_in_all_tasks(board):
    t = _active(board, "x", "swarm")
    board.block_on_external(t.id, "swarm", "ref", "r")
    assert t.id in {x.id for x in board.all_tasks}


def test_blocked_task_shows_in_mine_filter(board):
    t = _active(board, "x", "swarm")
    board.block_on_external(t.id, "swarm", "ref", "r")
    # Default 'mine' (include_completed=False) must still surface a BLOCKED
    # task — it's open/tracked work, not a closeout.
    mine = _apply_task_filter(list(board.all_tasks), "mine", "swarm", include_completed=False)
    assert t.id in {x.id for x in mine}


# --- Criterion #4: manual clear back to active wipes the ref ------------


def test_activate_clears_external_ref(board):
    t = _active(board, "x", "swarm")
    board.block_on_external(t.id, "swarm", "npm eslint@^10", "awaiting upstream")
    board.activate(t.id)  # operator resume
    got = board.get(t.id)
    assert got.status == TaskStatus.ACTIVE
    assert got.external_blocker_ref == ""
    assert got.block_reason == ""


# --- swarm_block_on_external MCP handler --------------------------------


def test_handler_blocks_sole_active_task(daemon):
    t = daemon.task_board.create(title="initiative")
    daemon.task_board.assign(t.id, "swarm")
    daemon.task_board.activate(t.id)

    out = _handle_block_on_external(
        daemon, "swarm", {"watch_ref": "npm eslint@^10", "reason": "upstream not ready"}
    )

    assert "BLOCKED on external" in out[0]["text"]
    got = daemon.task_board.get(t.id)
    assert got.status == TaskStatus.BLOCKED
    assert got.external_blocker_ref == "npm eslint@^10"
    assert SystemAction.TASK_PARKED in [e.action for e in daemon.drone_log.entries]


def test_handler_records_history(daemon):
    t = daemon.task_board.create(title="x")
    daemon.task_board.assign(t.id, "swarm")
    daemon.task_board.activate(t.id)
    _handle_block_on_external(daemon, "swarm", {"watch_ref": "ref", "reason": "r"})
    actions = [e.action for e in daemon.task_history.get_events(t.id)]
    assert TaskAction.BLOCKED in actions


def test_handler_requires_watch_ref(daemon):
    t = daemon.task_board.create(title="x")
    daemon.task_board.assign(t.id, "swarm")
    daemon.task_board.activate(t.id)
    out = _handle_block_on_external(daemon, "swarm", {"reason": "r"})
    assert "watch_ref" in out[0]["text"]
    assert daemon.task_board.get(t.id).status == TaskStatus.ACTIVE  # untouched


def test_handler_requires_reason(daemon):
    t = daemon.task_board.create(title="x")
    daemon.task_board.assign(t.id, "swarm")
    daemon.task_board.activate(t.id)
    out = _handle_block_on_external(daemon, "swarm", {"watch_ref": "ref"})
    assert "reason" in out[0]["text"]
    assert daemon.task_board.get(t.id).status == TaskStatus.ACTIVE


def test_handler_explicit_task_number(daemon):
    t = daemon.task_board.create(title="x")
    daemon.task_board.assign(t.id, "swarm")
    daemon.task_board.activate(t.id)
    out = _handle_block_on_external(
        daemon, "swarm", {"watch_ref": "ref", "reason": "r", "task_number": t.number}
    )
    assert "BLOCKED on external" in out[0]["text"]
    assert daemon.task_board.get(t.id).status == TaskStatus.BLOCKED


def test_handler_refuses_ambiguous_without_number(daemon):
    t1 = daemon.task_board.create(title="a")
    daemon.task_board.assign(t1.id, "swarm")
    t2 = daemon.task_board.create(title="b")
    daemon.task_board.assign(t2.id, "swarm")
    # Both ASSIGNED (candidates) — no number → refuse, mutate nothing.
    out = _handle_block_on_external(daemon, "swarm", {"watch_ref": "ref", "reason": "r"})
    assert "Ambiguous" in out[0]["text"]
    assert daemon.task_board.get(t1.id).status == TaskStatus.ASSIGNED
    assert daemon.task_board.get(t2.id).status == TaskStatus.ASSIGNED


def test_handler_rejects_not_owned_number(daemon):
    t = daemon.task_board.create(title="x")
    daemon.task_board.assign(t.id, "other")
    daemon.task_board.activate(t.id)
    out = _handle_block_on_external(
        daemon, "swarm", {"watch_ref": "ref", "reason": "r", "task_number": t.number}
    )
    assert "not assigned to you" in out[0]["text"]
    assert daemon.task_board.get(t.id).status == TaskStatus.ACTIVE


def test_handler_no_active_task(daemon):
    out = _handle_block_on_external(daemon, "swarm", {"watch_ref": "ref", "reason": "r"})
    assert "No active task" in out[0]["text"]


# --- Registration + persistence ----------------------------------------


def test_tool_registered():
    assert any(t["name"] == "swarm_block_on_external" for t in TOOLS)
    assert "swarm_block_on_external" in _HANDLERS


def test_external_ref_roundtrips_through_task_store(board):
    t = _active(board, "x", "swarm")
    board.block_on_external(t.id, "swarm", "npm eslint@^10", "awaiting")
    row = _task_to_row(board.get(t.id))
    assert row["external_blocker_ref"] == "npm eslint@^10"
    restored = _row_to_task(row)
    assert restored.external_blocker_ref == "npm eslint@^10"
    assert restored.status == TaskStatus.BLOCKED


def test_legacy_row_without_ref_defaults_empty():
    # A pre-v15 row has no external_blocker_ref column → _safe_get returns "".
    row = _task_to_row(__import__("swarm.tasks.task", fromlist=["SwarmTask"]).SwarmTask(title="x"))
    del row["external_blocker_ref"]
    restored = _row_to_task(row)
    assert restored.external_blocker_ref == ""
