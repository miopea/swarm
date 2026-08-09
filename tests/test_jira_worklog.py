"""Logging work against a Jira ticket when a task closes (#1339).

Swarm already knows how long a task was ACTIVE; devs are measured on time they hate
logging by hand. But this writes to a shared tracker and it is somebody's TIMESHEET, so
every refusal below matters more than the happy path:

* an unconfirmed project is never written to — the same gate as the export sweep;
* a duration the history cannot substantiate logs NOTHING, rather than a guess;
* a completion already logged is not logged twice, checked by READING Jira rather than
  by remembering locally, so it survives a restart or a rebuilt database;
* if the existing worklogs cannot be read, nothing is written — "I cannot tell whether
  I already billed this" must not resolve to "bill it again".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.config.models import JiraConfig
from swarm.db.core import SwarmDB
from swarm.db.task_store import SqliteTaskStore
from swarm.integrations.jira import JiraSyncService
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask
from swarm.tasks.worklog import worklog_marker


@pytest.fixture
def board(tmp_path: Path) -> TaskBoard:
    return TaskBoard(store=SqliteTaskStore(SwarmDB(tmp_path / "swarm.db")))


def _svc(**cfg: Any) -> JiraSyncService:
    defaults: dict[str, Any] = {
        "enabled": True,
        "projects": ["WWD"],
        "project_status_maps": {"WWD": {"done": "Done"}},
        "confirmed_projects": ["WWD"],
    }
    defaults.update(cfg)
    mgr = MagicMock()
    mgr.is_connected.return_value = True
    mgr.api_base_url = "https://api.atlassian.com/ex/jira/test"
    svc = JiraSyncService(JiraConfig(**defaults), token_manager=mgr)
    assert svc.enabled, "positive control: a disabled service makes every test vacuous"
    svc.client.get_worklogs = AsyncMock(return_value=[])
    svc.client.add_worklog = AsyncMock(return_value=True)
    return svc


def _task(board: TaskBoard, key: str = "WWD-1") -> SwarmTask:
    t = board.add(SwarmTask(title="t", description=""))
    board.set_jira_key(t.id, key)
    board.assign(t.id, "api")
    board.activate(t.id)
    board.complete(t.id, "done")
    return board.get(t.id)


@pytest.mark.asyncio
async def test_time_is_logged_against_the_ticket(board: TaskBoard):
    svc = _svc()
    assert await svc.log_work(_task(board), 3600) is True
    key, seconds, comment = svc.client.add_worklog.await_args.args
    assert key == "WWD-1"
    assert seconds == 3600
    assert "task #" in comment


@pytest.mark.asyncio
async def test_an_unconfirmed_project_is_never_written_to(board: TaskBoard):
    """A worklog is a WRITE to a shared tracker. Enabling an integration must not start
    filling in other people's timesheets."""
    svc = _svc(confirmed_projects=[])
    assert await svc.log_work(_task(board), 3600) is False
    svc.client.add_worklog.assert_not_called()


@pytest.mark.asyncio
async def test_the_same_completion_is_not_logged_twice(board: TaskBoard):
    """Double-billing someone's week is the worst outcome here, and a fire-and-forget
    background task can genuinely run twice."""
    svc = _svc()
    task = _task(board)
    marker = worklog_marker(task.number, task.completed_at or 0)
    svc.client.get_worklogs = AsyncMock(
        return_value=[{"comment": f"Worked via Swarm on task #{task.number}. {marker}"}]
    )

    assert await svc.log_work(task, 3600) is False
    svc.client.add_worklog.assert_not_called()


@pytest.mark.asyncio
async def test_an_unreadable_worklog_list_writes_NOTHING(board: TaskBoard):
    """ "I cannot tell whether I already billed this" must not resolve to "bill it
    again"."""
    svc = _svc()
    svc.client.get_worklogs = AsyncMock(side_effect=RuntimeError("500"))
    assert await svc.log_work(_task(board), 3600) is False
    svc.client.add_worklog.assert_not_called()


@pytest.mark.asyncio
async def test_a_zero_or_negative_duration_writes_nothing(board: TaskBoard):
    svc = _svc()
    assert await svc.log_work(_task(board), 0) is False
    assert await svc.log_work(_task(board), -5) is False
    svc.client.add_worklog.assert_not_called()


@pytest.mark.asyncio
async def test_a_sub_minute_span_is_rounded_up_not_dropped(board: TaskBoard):
    """Jira rounds sub-minute worklogs to zero. Rounding up to a minute is honest for a
    task that genuinely took forty seconds; inventing more would overstate."""
    svc = _svc()
    await svc.log_work(_task(board), 40)
    assert svc.client.add_worklog.await_args.args[1] == 60


@pytest.mark.asyncio
async def test_an_unlinked_task_is_skipped(board: TaskBoard):
    svc = _svc()
    t = board.add(SwarmTask(title="no ticket", description=""))
    assert await svc.log_work(board.get(t.id), 3600) is False


# --- the wiring ---------------------------------------------------------------


def test_completion_fires_a_worklog():
    """Wiring, not the function. Every check above calls log_work directly, so deleting
    the call site would leave them all green — the shape that fooled six controls
    earlier in this work."""
    import ast

    src = Path("src/swarm/server/jira_service.py").read_text()
    fn = next(
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "fire_completion"
    )
    body = ast.unparse(fn)
    # Bounded to fire_completion's OWN body. A text window from "def fire_completion" to
    # the next function spans `def fire_worklog` itself, so the name survives even when
    # the CALL is deleted — this test passed with the wiring removed until it was
    # bounded properly.
    assert "self.fire_worklog" in body, "closing a task never logs any time"


def test_the_duration_comes_from_history_not_from_started_at():
    """`completed_at - started_at` under-bills every parked-and-resumed task, because
    activate() resets started_at.

    The CODE is scanned with docstrings and comments stripped. The first version of this
    test failed against the docstring that EXPLAINS why started_at is not used — the
    fourth time in this work that a scan matched the prose describing the fix rather
    than the fix.
    """
    import ast

    src = Path("src/swarm/server/jira_service.py").read_text()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "fire_worklog"
    )
    # Drop the docstring node, then unparse: comments are already gone from the AST.
    if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant):
        fn.body = fn.body[1:]
    code = ast.unparse(fn)

    assert "active_seconds" in code and "get_events" in code, (
        "the worklog duration is not reconstructed from task history"
    )
    assert "started_at" not in code, "still subtracting started_at, which activate() resets"
