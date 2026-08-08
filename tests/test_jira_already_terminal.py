"""A ticket already finished in Jira is agreement, not a failed export.

MEASURED ON THE OPERATOR'S REAL BOARD, 2026-08-08. All 10 unacknowledged IS tasks were
`done` in Swarm and already `Resolved` in Jira — statusCategory `done` — offering only a
`Waiting for support` transition, which REOPENS. The reconciler had been retrying an
impossible transition every sync interval to reach a state the tickets were already in,
two WARNING lines each, and the in-memory refused-set meant every daemon restart tried
the whole set again.

The spec filed this as "unreachable transitions: surface once, decide once — remap /
mark already-done / unlink". Measuring first dissolved the decision: there was nothing
to remap and nothing to unlink. Jira was right, Swarm had simply never recorded it.

WHY NAME EQUALITY WAS THE WRONG TEST. This project calls finished `Resolved`; the
confirmed map targets `Done`. Both mean the work is over. statusCategory is universal
across every Jira workflow, so it answers "is this finished?" without per-project
discovery — the same property that makes it the right filter for imports.

The distinction this file guards: "Jira refused the transition" is NOT evidence Jira is
in the desired state, but "Jira reports statusCategory=done and Swarm says done" is.
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
    defaults: dict[str, Any] = {
        "enabled": True,
        "projects": ["IS"],
        "project_status_maps": {"IS": {"done": "Done", "failed": "Canceled"}},
        "confirmed_projects": ["IS"],
    }
    defaults.update(cfg)
    mgr = MagicMock()
    mgr.is_connected.return_value = True
    mgr.api_base_url = "https://api.atlassian.com/ex/jira/test"
    svc = JiraSyncService(JiraConfig(**defaults), token_manager=mgr)
    assert svc.enabled, "positive control: a disabled service makes every test vacuous"
    # The REAL shape from IS: no transition to Done, only a reopen.
    svc.client.get_transitions = AsyncMock(
        return_value=[{"id": "9", "name": "Waiting for support"}]
    )
    svc.client.transition_issue = AsyncMock(return_value=True)
    return svc


def _issue(category: str, name: str = "Resolved") -> dict[str, Any]:
    return {"fields": {"status": {"name": name, "statusCategory": {"key": category}}}}


def _task(board: TaskBoard, key: str = "IS-10278") -> SwarmTask:
    t = board.add(SwarmTask(title="t", description=""))
    board.set_jira_key(t.id, key)
    return board.get(t.id)


@pytest.mark.asyncio
async def test_an_already_resolved_ticket_reports_agreement(board: TaskBoard):
    """THE 10-TICKET CASE. Returning False here is what made it retry forever."""
    svc = _svc()
    svc.client.get_issue = AsyncMock(return_value=_issue("done"))

    ok = await svc.export_status(_task(board), TaskStatus.DONE)

    assert ok is True, "an already-finished ticket is still reported as a failed export"
    svc.client.transition_issue.assert_not_called(), "it wrote to a ticket that needed nothing"


@pytest.mark.asyncio
async def test_agreement_does_not_write_to_jira(board: TaskBoard):
    """It is a COMPARISON. Reopening these tickets to close them again would be
    catastrophic on a shared service desk."""
    svc = _svc()
    svc.client.get_issue = AsyncMock(return_value=_issue("done"))

    await svc.export_status(_task(board), TaskStatus.DONE)

    svc.client.transition_issue.assert_not_called()


@pytest.mark.asyncio
async def test_an_open_ticket_is_still_a_real_failure(board: TaskBoard):
    """The guard must not swallow genuine divergence: a ticket that is NOT finished and
    cannot be transitioned is exactly the case an operator needs to hear about."""
    svc = _svc()
    svc.client.get_issue = AsyncMock(return_value=_issue("indeterminate", "Waiting for support"))

    ok = await svc.export_status(_task(board), TaskStatus.DONE)

    assert ok is False, "an unfinished, untransitionable ticket was reported as agreement"


@pytest.mark.asyncio
async def test_a_done_ticket_does_NOT_satisfy_a_non_terminal_swarm_status(board: TaskBoard):
    """A closed ticket while Swarm says ACTIVE is a real divergence, not a match — the
    asymmetry is the point."""
    svc = _svc(project_status_maps={"IS": {"active": "In Progress"}})
    svc.client.get_issue = AsyncMock(return_value=_issue("done"))

    ok = await svc.export_status(_task(board), TaskStatus.ACTIVE)

    assert ok is False, "a finished ticket was accepted as agreement for in-progress work"


@pytest.mark.asyncio
async def test_an_unreadable_ticket_does_not_claim_agreement(board: TaskBoard):
    """Cannot tell is not the same as up to date. Silence here would convert an
    unreachable API into a false 'Jira agrees'."""
    svc = _svc()
    svc.client.get_issue = AsyncMock(side_effect=RuntimeError("500"))

    assert await svc.export_status(_task(board), TaskStatus.DONE) is False


@pytest.mark.asyncio
async def test_a_reachable_transition_is_still_performed(board: TaskBoard):
    """The check must not short-circuit tickets that CAN be moved: a ticket that can be
    transitioned should be, not have its current state accepted."""
    svc = _svc()
    svc.client.get_transitions = AsyncMock(return_value=[{"id": "31", "name": "Done"}])
    svc.client.get_issue = AsyncMock(return_value=_issue("done"))

    ok = await svc.export_status(_task(board), TaskStatus.DONE)

    assert ok is True
    svc.client.transition_issue.assert_called_once_with("IS-10278", "31")
    svc.client.get_issue.assert_not_called(), "the happy path paid for an extra API call"
