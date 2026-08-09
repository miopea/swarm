"""A blocked task says so on its Jira ticket (#1340).

THE GAP: when a worker blocked, the linked ticket said nothing. A PM looking at the
board saw idle work with no explanation and the reason lived only inside Swarm. This is
the item that most directly makes Swarm legible to people who never open it.

A COMMENT, NOT AN ISSUE LINK: a Jira `blocks` link can only express a dependency
between two TICKETS, and most Swarm blockers are on things with no ticket — another
Swarm task, an operator decision, a deploy. The comment covers every case.

ONE COMMENT, UPDATED IN PLACE: this runs on a five-minute loop, so posting on each
block and unblock would turn a ticket into a changelog nobody reads.
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
from swarm.tasks.task import SwarmTask, TaskStatus


@pytest.fixture
def board(tmp_path: Path) -> TaskBoard:
    return TaskBoard(store=SqliteTaskStore(SwarmDB(tmp_path / "swarm.db")))


def _svc(**cfg: Any) -> JiraSyncService:
    defaults: dict[str, Any] = {"enabled": True, "projects": ["WWD"]}
    defaults.update(cfg)
    mgr = MagicMock()
    mgr.is_connected.return_value = True
    mgr.api_base_url = "https://api.atlassian.com/ex/jira/test"
    svc = JiraSyncService(JiraConfig(**defaults), token_manager=mgr)
    assert svc.enabled, "positive control: a disabled service makes every test vacuous"
    svc.client.get_comments = AsyncMock(return_value=[])
    svc.client.add_comment = AsyncMock(return_value=True)
    svc.client.update_comment = AsyncMock(return_value=True)
    return svc


def _task(board: TaskBoard, key: str = "WWD-1") -> SwarmTask:
    t = board.add(SwarmTask(title="t", description=""))
    board.set_jira_key(t.id, key)
    board.assign(t.id, "api")
    return board.get(t.id)


@pytest.mark.asyncio
async def test_a_blocker_is_posted_to_the_ticket(board: TaskBoard):
    svc = _svc()
    assert await svc.sync_blocker_note(_task(board), "waiting on the platform deploy") is True
    body = svc.client.add_comment.await_args.args[1]
    assert "BLOCKED" in body and "waiting on the platform deploy" in body


@pytest.mark.asyncio
async def test_the_note_is_UPDATED_not_duplicated(board: TaskBoard):
    """Five-minute loop. A second comment per cycle would bury the ticket."""
    svc = _svc()
    svc.client.get_comments = AsyncMock(
        return_value=[{"id": "77", "body": "[swarm:blocker:1] Swarm is BLOCKED on this: old"}]
    )
    task = _task(board)

    assert await svc.sync_blocker_note(task, "new reason") is True

    svc.client.add_comment.assert_not_called()
    key, cid, body = svc.client.update_comment.await_args.args
    assert cid == "77" and "new reason" in body


@pytest.mark.asyncio
async def test_an_unchanged_blocker_writes_nothing(board: TaskBoard):
    """THE NOISE GUARD. Without it every cycle rewrites the same sentence forever."""
    svc = _svc()
    task = _task(board)
    same = f"[swarm:blocker:{task.number}] Swarm is BLOCKED on this: waiting on deploy"
    svc.client.get_comments = AsyncMock(return_value=[{"id": "77", "body": same}])

    assert await svc.sync_blocker_note(task, "waiting on deploy") is False
    svc.client.update_comment.assert_not_called()
    svc.client.add_comment.assert_not_called()


@pytest.mark.asyncio
async def test_clearing_rewrites_the_note_rather_than_leaving_it_lying(board: TaskBoard):
    """A ticket that keeps asserting a block after work resumed is worse than silence —
    it is actively misleading the person reading it."""
    svc = _svc()
    svc.client.get_comments = AsyncMock(
        return_value=[{"id": "77", "body": "[swarm:blocker:1] Swarm is BLOCKED on this: x"}]
    )

    assert await svc.sync_blocker_note(_task(board), "") is True
    body = svc.client.update_comment.await_args.args[2]
    assert "No longer blocked" in body


@pytest.mark.asyncio
async def test_an_unblocked_task_with_no_prior_note_says_nothing(board: TaskBoard):
    """Do not announce the absence of a blocker that was never posted — that would
    comment on every linked ticket on every cycle."""
    svc = _svc()
    assert await svc.sync_blocker_note(_task(board), "") is False
    svc.client.add_comment.assert_not_called()


# --- the sweep -----------------------------------------------------------------


def _service(board: TaskBoard, jira: Any):
    from swarm.server.jira_service import JiraService

    svc = JiraService.__new__(JiraService)
    svc._task_board = board
    svc._get_jira = lambda: jira
    svc._drone_log = MagicMock()
    svc._broadcast_ws = lambda _p: None
    svc._track_task = lambda _t: None
    # reconcile_blockers narrows to blocked-or-noted tasks after a first full pass
    # (#1350), so a hand-built service needs the same starting state a real one has.
    svc._blocker_noted = set()
    svc._blocker_pass_done = False
    return svc


@pytest.mark.asyncio
async def test_the_sweep_passes_the_recorded_reason(board: TaskBoard):
    task = _task(board)
    board.block_on_external(task.id, "api", "deploy", "waiting on the platform deploy")
    jira = MagicMock()
    jira.enabled = True
    jira.sync_blocker_note = AsyncMock(return_value=True)

    assert await _service(board, jira).reconcile_blockers() == 1
    _t, reason = jira.sync_blocker_note.await_args.args
    assert "platform deploy" in reason


@pytest.mark.asyncio
async def test_a_blocked_task_with_no_reason_still_reports_something(board: TaskBoard):
    """ "Blocked, reason unrecorded" is still more use to a PM than silence."""
    task = _task(board)
    task.status = TaskStatus.BLOCKED
    jira = MagicMock()
    jira.enabled = True
    jira.sync_blocker_note = AsyncMock(return_value=True)

    await _service(board, jira).reconcile_blockers()
    assert jira.sync_blocker_note.await_args.args[1] != ""


@pytest.mark.asyncio
async def test_finished_tasks_are_skipped(board: TaskBoard):
    done = _task(board, "WWD-9")
    board.complete(done.id, "shipped")
    jira = MagicMock()
    jira.enabled = True
    jira.sync_blocker_note = AsyncMock(return_value=False)

    await _service(board, jira).reconcile_blockers()
    seen = {c.args[0].jira_key for c in jira.sync_blocker_note.call_args_list}
    assert "WWD-9" not in seen


def test_the_sweep_is_wired_into_the_sync_loop():
    """The wiring, not the function — every check above calls reconcile_blockers
    directly, so deleting the call site would leave them all green."""
    src = Path("src/swarm/server/jira_service.py").read_text()
    loop = src[src.index("async def sync_loop") :]
    loop = loop[: loop.index("except asyncio.CancelledError")]
    assert "reconcile_blockers()" in loop, "blockers are never reported to Jira on a schedule"
