"""The per-cycle API cost stays bounded as the board grows (#1350).

MEASURED 2026-08-09, after the passes were already shipped: refresh_linked_tasks and
reconcile_blockers were each O(open linked tasks) with one full API call per task per
cycle — about 123 calls/cycle on a 55-ticket board, ~14,760/hour across ten devs,
forever. The spec had flagged API budget as an open question and the passes were added
without measuring it.

This file asserts the SHAPE of the cost, not just that the code works, because the
failure is invisible on a small board and only bites once Jira is enabled for a team.
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


@pytest.fixture
def board(tmp_path: Path) -> TaskBoard:
    return TaskBoard(store=SqliteTaskStore(SwarmDB(tmp_path / "swarm.db")))


def _service(board: TaskBoard, jira: Any):
    from swarm.server.jira_service import JiraService

    svc = JiraService.__new__(JiraService)
    svc._task_board = board
    svc._get_jira = lambda: jira
    svc._drone_log = MagicMock()
    svc._broadcast_ws = lambda _p: None
    svc._track_task = lambda _t: None
    svc._message_store = None
    svc._blocker_noted = set()
    svc._blocker_pass_done = False
    return svc


def _linked(board: TaskBoard, key: str) -> SwarmTask:
    t = board.add(SwarmTask(title=key, description="body"))
    board.set_jira_key(t.id, key)
    board.assign(t.id, "api")
    return board.get(t.id)


# --- the refresh is one search, not N reads -----------------------------------


@pytest.mark.asyncio
async def test_refreshing_twenty_tasks_costs_ONE_search(board: TaskBoard):
    for i in range(20):
        _linked(board, f"WWD-{i}")
    jira = MagicMock()
    jira.enabled = True
    jira.fetch_synced_fields = AsyncMock(return_value={})
    jira.refresh_synced_content = AsyncMock(return_value="")

    await _service(board, jira).refresh_linked_tasks()

    assert jira.fetch_synced_fields.await_count == 1, "the batch is called per task"
    assert jira.refresh_synced_content.await_count == 0, (
        "tasks missing from the batch were still fetched individually"
    )


@pytest.mark.asyncio
async def test_the_batch_issues_one_search_per_chunk():
    """Chunked, because a single JQL cannot carry unbounded keys."""
    cfg = JiraConfig(enabled=True, projects=["WWD"])
    mgr = MagicMock()
    mgr.is_connected.return_value = True
    mgr.api_base_url = "https://api.atlassian.com/ex/jira/test"
    svc = JiraSyncService(cfg, token_manager=mgr)
    svc.client.search_issues = AsyncMock(return_value=[])

    await svc.fetch_synced_fields([f"WWD-{i}" for i in range(120)])

    assert svc.client.search_issues.await_count == 3, (
        f"120 keys should be 3 searches of 50, got {svc.client.search_issues.await_count}"
    )


@pytest.mark.asyncio
async def test_a_malformed_key_never_reaches_the_query():
    """Validated, not escaped — the same guard the ownership check uses."""
    cfg = JiraConfig(enabled=True, projects=["WWD"])
    mgr = MagicMock()
    mgr.is_connected.return_value = True
    mgr.api_base_url = "https://api.atlassian.com/ex/jira/test"
    svc = JiraSyncService(cfg, token_manager=mgr)
    svc.client.search_issues = AsyncMock(return_value=[])

    await svc.fetch_synced_fields(['X") OR key = "WWD-1'])

    svc.client.search_issues.assert_not_called()


@pytest.mark.asyncio
async def test_the_single_task_refresh_path_still_works(board: TaskBoard):
    """The manual refresh button fetches one ticket and must keep doing so."""
    cfg = JiraConfig(enabled=True, projects=["WWD"])
    mgr = MagicMock()
    mgr.is_connected.return_value = True
    mgr.api_base_url = "https://api.atlassian.com/ex/jira/test"
    svc = JiraSyncService(cfg, token_manager=mgr)
    svc.client.get_issue = AsyncMock(
        return_value={"fields": {"description": "b", "comment": {"comments": []}}}
    )
    await svc.refresh_synced_content(_linked(board, "WWD-1"))
    svc.client.get_issue.assert_awaited_once()


# --- blockers: no call for a task with nothing to say -------------------------


@pytest.mark.asyncio
async def test_after_the_first_pass_unblocked_tasks_cost_nothing(board: TaskBoard):
    """THE STEADY STATE. Reading every open ticket's comments forever, to discover
    nothing for tasks that were never blocked, is the bulk of the measured cost."""
    for i in range(20):
        _linked(board, f"WWD-{i}")
    jira = MagicMock()
    jira.enabled = True
    jira.sync_blocker_note = AsyncMock(return_value=False)
    svc = _service(board, jira)

    await svc.reconcile_blockers()  # first pass: rebuilds knowledge, checks everything
    first = jira.sync_blocker_note.await_count
    jira.sync_blocker_note.reset_mock()
    await svc.reconcile_blockers()  # steady state

    assert first == 20, f"the first pass must check everything, checked {first}"
    assert jira.sync_blocker_note.await_count == 0, (
        "unblocked tasks with no note are still costing an API call every cycle"
    )


@pytest.mark.asyncio
async def test_a_blocked_task_is_always_checked(board: TaskBoard):
    task = _linked(board, "WWD-1")
    board.block_on_external(task.id, "api", "deploy", "waiting")
    jira = MagicMock()
    jira.enabled = True
    jira.sync_blocker_note = AsyncMock(return_value=True)
    svc = _service(board, jira)

    await svc.reconcile_blockers()
    jira.sync_blocker_note.reset_mock()
    await svc.reconcile_blockers()

    assert jira.sync_blocker_note.await_count == 1, "a blocked task stopped being reported"


@pytest.mark.asyncio
async def test_a_note_is_still_CLEARED_after_the_block_lifts(board: TaskBoard):
    """The narrowing must not strand a posted note: once we have written one, the task
    stays watched until it is cleared."""
    task = _linked(board, "WWD-1")
    board.block_on_external(task.id, "api", "deploy", "waiting")
    jira = MagicMock()
    jira.enabled = True
    jira.sync_blocker_note = AsyncMock(return_value=True)
    svc = _service(board, jira)
    await svc.reconcile_blockers()

    board.unblock(task.id)
    jira.sync_blocker_note.reset_mock()
    await svc.reconcile_blockers()

    assert jira.sync_blocker_note.await_count == 1, "the stale note was never cleared"
    assert jira.sync_blocker_note.await_args.args[1] == "", "it was not cleared, only re-posted"


@pytest.mark.asyncio
async def test_a_note_from_a_PREVIOUS_daemon_is_found_after_restart(board: TaskBoard):
    """The knowledge set lives in memory. Narrowing purely to "blocked now" would strand
    a note written before a restart — which is why the first pass still checks all."""
    _linked(board, "WWD-1")
    jira = MagicMock()
    jira.enabled = True
    jira.sync_blocker_note = AsyncMock(return_value=True)

    fresh = _service(board, jira)  # simulates a restart: empty knowledge, pass not done
    await fresh.reconcile_blockers()

    assert jira.sync_blocker_note.await_count == 1, (
        "a note left by a previous daemon instance would never be discovered"
    )
