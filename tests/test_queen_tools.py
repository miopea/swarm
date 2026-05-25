"""Tests for :mod:`swarm.mcp.queen_tools` — the Queen-only MCP surface.

Every Queen tool gates on caller identity via ``_assert_queen``: a
non-Queen caller must get the permission-denied payload, the Queen
must get the real response. These tests pin both halves for every
tool plus the per-tool validation rules (required args, missing
targets, free-text reason audit gates).

Action-tool side-effects are verified through MagicMock daemons —
``swarm.queen_tools`` is sync-call-into-async via ``_fire_async``,
which falls through to a coroutine-close fallback when no event loop
is running, so the action handlers are safe to invoke from a sync
test body.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from swarm.mcp.queen_tools import (
    QUEEN_HANDLERS,
    QUEEN_TOOLS,
    _assert_queen,
    _clamp,
)
from swarm.worker.worker import QUEEN_WORKER_NAME, WorkerState


def _text(result: object) -> str:
    """Pull the first text block from either MCP return shape."""
    if isinstance(result, dict):
        blocks = result.get("content") or []
    else:
        blocks = result
    return blocks[0].get("text", "") if blocks else ""


def _make_worker_mock(
    name: str,
    *,
    state: WorkerState = WorkerState.RESTING,
    is_queen: bool = False,
    context_pct: float = 0.25,
    pty_tail: str = "$ ",
) -> MagicMock:
    w = MagicMock()
    w.name = name
    w.kind = "claude"
    w.is_queen = is_queen
    w.state = state
    w.display_state = state
    w.state_duration = 1.5
    w.context_pct = context_pct
    w.usage.cost_usd = 0.001
    w.usage.to_dict.return_value = {"input_tokens": 100, "output_tokens": 50}
    w.process = MagicMock()
    w.process.get_content.return_value = pty_tail
    return w


def _make_task_mock(
    number: int = 1,
    title: str = "Fix the bug",
    *,
    assigned_worker: str | None = None,
    status_value: str = "assigned",
    task_id: str = "task-1",
    completed_at: float = 0.0,
) -> MagicMock:
    t = MagicMock()
    t.id = task_id
    t.number = number
    t.title = title
    t.assigned_worker = assigned_worker
    t.status.value = status_value
    t.completed_at = completed_at
    return t


@pytest.fixture
def daemon() -> MagicMock:
    """Minimal daemon fake matching the queen_tools handler contract."""
    d = MagicMock()
    d.drone_log = MagicMock()
    d.workers = []
    d.task_board = MagicMock()
    d.task_board.all_tasks = []
    d.task_board.active_tasks_for_worker.return_value = []
    d.task_board.get.return_value = None
    d.task_board.assign.return_value = True
    d.task_board.unassign.return_value = True
    d.queen_chat = MagicMock()
    d.queen_chat.add_learning.return_value = MagicMock(id="learn-1")
    d.worker_svc = MagicMock()
    d.complete_task.return_value = True
    return d


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


class TestClamp:
    def test_default_on_garbage(self) -> None:
        assert _clamp("not a number", 50, 1, 100) == 50
        assert _clamp(None, 50, 1, 100) == 50

    def test_within_range(self) -> None:
        assert _clamp(42, 50, 1, 100) == 42

    def test_floor_and_ceiling(self) -> None:
        assert _clamp(0, 50, 1, 100) == 1
        assert _clamp(999, 50, 1, 100) == 100

    def test_string_int_coerced(self) -> None:
        assert _clamp("17", 50, 1, 100) == 17


class TestAssertQueen:
    def test_non_queen_returns_payload(self) -> None:
        result = _assert_queen("alice")
        assert result is not None
        assert "Permission denied" in result[0]["text"]

    def test_queen_returns_none(self) -> None:
        assert _assert_queen(QUEEN_WORKER_NAME) is None


# ---------------------------------------------------------------------------
# Tool registry sanity
# ---------------------------------------------------------------------------


class TestToolRegistry:
    def test_every_tool_has_handler(self) -> None:
        tool_names = {t["name"] for t in QUEEN_TOOLS}
        handler_names = set(QUEEN_HANDLERS.keys())
        assert tool_names == handler_names, (
            f"Tool/handler mismatch: only in TOOLS={tool_names - handler_names}, "
            f"only in HANDLERS={handler_names - tool_names}"
        )

    def test_every_tool_name_is_queen_prefixed(self) -> None:
        for tool in QUEEN_TOOLS:
            assert tool["name"].startswith("queen_"), tool["name"]

    def test_every_tool_has_input_schema(self) -> None:
        for tool in QUEEN_TOOLS:
            schema = tool.get("inputSchema")
            assert isinstance(schema, dict), tool["name"]
            assert schema.get("type") == "object", tool["name"]


# ---------------------------------------------------------------------------
# Permission gates — every handler must reject a non-Queen caller
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", list(QUEEN_HANDLERS.keys()))
def test_handler_denies_non_queen(daemon: MagicMock, tool_name: str) -> None:
    handler = QUEEN_HANDLERS[tool_name]
    result = handler(daemon, "alice", {})
    text = _text(result)
    assert "Permission denied" in text, f"{tool_name} did not deny a non-queen caller"


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


class TestViewWorkerState:
    def test_summary_when_no_target(self, daemon: MagicMock) -> None:
        daemon.workers = [_make_worker_mock("alpha"), _make_worker_mock("beta")]
        result = QUEEN_HANDLERS["queen_view_worker_state"](daemon, QUEEN_WORKER_NAME, {})
        # Dict-shape response with structuredContent.
        assert isinstance(result, dict)
        assert "alpha" in _text(result)
        assert "beta" in _text(result)
        payload = result["structuredContent"]["workers"]
        names = {w["name"] for w in payload}
        assert names == {"alpha", "beta"}

    def test_unknown_target_returns_error(self, daemon: MagicMock) -> None:
        daemon.workers = [_make_worker_mock("alpha")]
        result = QUEEN_HANDLERS["queen_view_worker_state"](
            daemon, QUEEN_WORKER_NAME, {"worker": "ghost"}
        )
        # Error path: legacy list shape.
        assert isinstance(result, list)
        assert "not found" in _text(result)

    def test_targeted_returns_body_with_pty_tail(self, daemon: MagicMock) -> None:
        daemon.workers = [_make_worker_mock("alpha", pty_tail="line1\nline2")]
        result = QUEEN_HANDLERS["queen_view_worker_state"](
            daemon, QUEEN_WORKER_NAME, {"worker": "alpha", "lines": 5}
        )
        assert isinstance(result, dict)
        body = _text(result)
        assert "worker: alpha" in body
        assert "line1" in body
        assert result["structuredContent"]["worker"]["name"] == "alpha"

    def test_no_workers_handled(self, daemon: MagicMock) -> None:
        daemon.workers = []
        result = QUEEN_HANDLERS["queen_view_worker_state"](daemon, QUEEN_WORKER_NAME, {})
        assert "No workers." in _text(result)


class TestViewTaskBoard:
    def test_empty_board(self, daemon: MagicMock) -> None:
        result = QUEEN_HANDLERS["queen_view_task_board"](daemon, QUEEN_WORKER_NAME, {})
        # Either dict or list shape — pull text.
        assert _text(result) != ""

    def test_filters_by_status(self, daemon: MagicMock) -> None:
        daemon.task_board.all_tasks = [
            _make_task_mock(1, "open one", status_value="active"),
            _make_task_mock(2, "done one", status_value="done"),
        ]
        result = QUEEN_HANDLERS["queen_view_task_board"](
            daemon, QUEEN_WORKER_NAME, {"status": "open"}
        )
        text = _text(result)
        assert "open one" in text
        assert "done one" not in text

    def test_filters_by_worker(self, daemon: MagicMock) -> None:
        daemon.task_board.all_tasks = [
            _make_task_mock(1, "alice's task", assigned_worker="alice"),
            _make_task_mock(2, "bob's task", assigned_worker="bob"),
        ]
        result = QUEEN_HANDLERS["queen_view_task_board"](
            daemon, QUEEN_WORKER_NAME, {"worker": "alice"}
        )
        text = _text(result)
        assert "alice's task" in text
        assert "bob's task" not in text


# ---------------------------------------------------------------------------
# Action tools — validation
# ---------------------------------------------------------------------------


class TestReassignTask:
    def test_missing_to_worker(self, daemon: MagicMock) -> None:
        result = QUEEN_HANDLERS["queen_reassign_task"](
            daemon, QUEEN_WORKER_NAME, {"number": 1, "reason": "x"}
        )
        assert "Missing 'to_worker'" in _text(result)

    def test_missing_reason(self, daemon: MagicMock) -> None:
        result = QUEEN_HANDLERS["queen_reassign_task"](
            daemon,
            QUEEN_WORKER_NAME,
            {"number": 1, "to_worker": "bob"},
        )
        assert "Missing 'reason'" in _text(result)

    def test_no_task_lookup_keys(self, daemon: MagicMock) -> None:
        result = QUEEN_HANDLERS["queen_reassign_task"](
            daemon,
            QUEEN_WORKER_NAME,
            {"to_worker": "bob", "reason": "context"},
        )
        assert "Missing 'number' or 'task_id'" in _text(result)

    def test_task_not_found(self, daemon: MagicMock) -> None:
        daemon.task_board.all_tasks = []
        result = QUEEN_HANDLERS["queen_reassign_task"](
            daemon,
            QUEEN_WORKER_NAME,
            {"to_worker": "bob", "reason": "context", "number": 999},
        )
        assert "No task with number" in _text(result)

    def test_no_op_when_already_assigned(self, daemon: MagicMock) -> None:
        task = _make_task_mock(1, assigned_worker="bob")
        daemon.task_board.all_tasks = [task]
        result = QUEEN_HANDLERS["queen_reassign_task"](
            daemon,
            QUEEN_WORKER_NAME,
            {"to_worker": "bob", "reason": "double-check", "number": 1},
        )
        assert "already assigned" in _text(result)
        daemon.task_board.assign.assert_not_called()

    def test_successful_reassign_calls_board(self, daemon: MagicMock) -> None:
        task = _make_task_mock(1, assigned_worker="alice")
        daemon.task_board.all_tasks = [task]
        result = QUEEN_HANDLERS["queen_reassign_task"](
            daemon,
            QUEEN_WORKER_NAME,
            {"to_worker": "bob", "reason": "context", "number": 1},
        )
        daemon.task_board.unassign.assert_called_once_with("task-1")
        daemon.task_board.assign.assert_called_once_with("task-1", "bob")
        assert "Reassigned #1" in _text(result)
        assert "ASSIGNED, not started" in _text(result)


class TestInterruptWorker:
    def test_missing_worker(self, daemon: MagicMock) -> None:
        result = QUEEN_HANDLERS["queen_interrupt_worker"](
            daemon, QUEEN_WORKER_NAME, {"reason": "stuck"}
        )
        assert "Missing 'worker'" in _text(result)

    def test_missing_reason(self, daemon: MagicMock) -> None:
        result = QUEEN_HANDLERS["queen_interrupt_worker"](
            daemon, QUEEN_WORKER_NAME, {"worker": "alice"}
        )
        assert "Missing 'reason'" in _text(result)

    def test_refuse_self_interrupt(self, daemon: MagicMock) -> None:
        result = QUEEN_HANDLERS["queen_interrupt_worker"](
            daemon,
            QUEEN_WORKER_NAME,
            {"worker": QUEEN_WORKER_NAME, "reason": "x"},
        )
        assert "Refusing to interrupt the Queen" in _text(result)

    def test_unknown_worker(self, daemon: MagicMock) -> None:
        daemon.workers = []
        result = QUEEN_HANDLERS["queen_interrupt_worker"](
            daemon, QUEEN_WORKER_NAME, {"worker": "ghost", "reason": "x"}
        )
        assert "not found" in _text(result)

    def test_no_worker_svc(self, daemon: MagicMock) -> None:
        daemon.workers = [_make_worker_mock("alice")]
        daemon.worker_svc = None
        result = QUEEN_HANDLERS["queen_interrupt_worker"](
            daemon,
            QUEEN_WORKER_NAME,
            {"worker": "alice", "reason": "stuck"},
        )
        assert "unavailable" in _text(result)

    def test_success_logs_and_returns_ok(self, daemon: MagicMock) -> None:
        daemon.workers = [_make_worker_mock("alice")]
        result = QUEEN_HANDLERS["queen_interrupt_worker"](
            daemon,
            QUEEN_WORKER_NAME,
            {"worker": "alice", "reason": "drift"},
        )
        assert "Interrupt sent to alice" in _text(result)
        daemon.drone_log.add.assert_called()


class TestForceCompleteTask:
    def test_missing_resolution(self, daemon: MagicMock) -> None:
        result = QUEEN_HANDLERS["queen_force_complete_task"](
            daemon,
            QUEEN_WORKER_NAME,
            {"number": 1, "reason": "done elsewhere"},
        )
        assert "Missing 'resolution'" in _text(result)

    def test_missing_reason(self, daemon: MagicMock) -> None:
        result = QUEEN_HANDLERS["queen_force_complete_task"](
            daemon,
            QUEEN_WORKER_NAME,
            {"number": 1, "resolution": "shipped"},
        )
        assert "Missing 'reason'" in _text(result)

    def test_complete_failure_returns_error(self, daemon: MagicMock) -> None:
        task = _make_task_mock(1, assigned_worker="alice", status_value="active")
        daemon.task_board.all_tasks = [task]
        daemon.complete_task.return_value = False
        result = QUEEN_HANDLERS["queen_force_complete_task"](
            daemon,
            QUEEN_WORKER_NAME,
            {"number": 1, "resolution": "done", "reason": "audit"},
        )
        assert "Failed to complete" in _text(result)

    def test_success(self, daemon: MagicMock) -> None:
        task = _make_task_mock(1, assigned_worker="alice")
        daemon.task_board.all_tasks = [task]
        result = QUEEN_HANDLERS["queen_force_complete_task"](
            daemon,
            QUEEN_WORKER_NAME,
            {"number": 1, "resolution": "done", "reason": "audit"},
        )
        daemon.complete_task.assert_called_once()
        assert "Force-completed #1" in _text(result)


class TestPromptWorker:
    def test_missing_worker(self, daemon: MagicMock) -> None:
        result = QUEEN_HANDLERS["queen_prompt_worker"](
            daemon, QUEEN_WORKER_NAME, {"prompt": "hi", "reason": "x"}
        )
        assert "Missing 'worker'" in _text(result)

    def test_missing_prompt(self, daemon: MagicMock) -> None:
        result = QUEEN_HANDLERS["queen_prompt_worker"](
            daemon,
            QUEEN_WORKER_NAME,
            {"worker": "alice", "reason": "x"},
        )
        assert "Missing 'prompt'" in _text(result)

    def test_missing_reason(self, daemon: MagicMock) -> None:
        result = QUEEN_HANDLERS["queen_prompt_worker"](
            daemon,
            QUEEN_WORKER_NAME,
            {"worker": "alice", "prompt": "hello"},
        )
        assert "Missing 'reason'" in _text(result)

    def test_refuse_self(self, daemon: MagicMock) -> None:
        result = QUEEN_HANDLERS["queen_prompt_worker"](
            daemon,
            QUEEN_WORKER_NAME,
            {"worker": QUEEN_WORKER_NAME, "prompt": "hi", "reason": "x"},
        )
        assert "Refusing to prompt the Queen" in _text(result)

    def test_unknown_worker(self, daemon: MagicMock) -> None:
        daemon.workers = []
        result = QUEEN_HANDLERS["queen_prompt_worker"](
            daemon,
            QUEEN_WORKER_NAME,
            {"worker": "ghost", "prompt": "hi", "reason": "x"},
        )
        assert "not found" in _text(result)

    def test_refuse_stung_worker(self, daemon: MagicMock) -> None:
        daemon.workers = [_make_worker_mock("alice", state=WorkerState.STUNG)]
        result = QUEEN_HANDLERS["queen_prompt_worker"](
            daemon,
            QUEEN_WORKER_NAME,
            {"worker": "alice", "prompt": "hi", "reason": "x"},
        )
        assert "STUNG" in _text(result)


class TestSaveLearning:
    def test_missing_context_or_correction(self, daemon: MagicMock) -> None:
        result = QUEEN_HANDLERS["queen_save_learning"](
            daemon, QUEEN_WORKER_NAME, {"context": "only context"}
        )
        assert "Missing required" in _text(result)

    def test_success_calls_queen_chat(self, daemon: MagicMock) -> None:
        result = QUEEN_HANDLERS["queen_save_learning"](
            daemon,
            QUEEN_WORKER_NAME,
            {"context": "ctx", "correction": "do this", "applied_to": "alice"},
        )
        daemon.queen_chat.add_learning.assert_called_once()
        kwargs = daemon.queen_chat.add_learning.call_args.kwargs
        assert kwargs["context"] == "ctx"
        assert kwargs["correction"] == "do this"
        assert kwargs["applied_to"] == "alice"
        assert "Learning saved" in _text(result)
