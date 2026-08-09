"""Reporter, due date, and criteria for imported tickets.

MEASURED FIRST, on 50 real tickets across both projects (2026-08-09):

    reporter   100% WWD / 100% IS
    duedate     36% WWD /  12% IS
    components / parent / environment / fixVersions / issuelinks — ZERO on both
    descriptions mentioning "acceptance" — ZERO on both

So reporter and due date are imported, the never-populated fields are deliberately not,
and acceptance criteria are SYNTHESISED rather than parsed — a parser would import
nothing, because nobody writes them.
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


def _svc(tmp_path: Path) -> JiraSyncService:
    mgr = MagicMock()
    mgr.is_connected.return_value = True
    mgr.api_base_url = "https://api.atlassian.com/ex/jira/test"
    svc = JiraSyncService(
        JiraConfig(enabled=True, projects=["WWD"]), token_manager=mgr, uploads_dir=tmp_path
    )
    assert svc.enabled, "positive control: a disabled service makes every test vacuous"
    return svc


def _issue(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"description": "BODY", "comment": {"comments": []}, "attachment": []}
    base.update(fields)
    return {"fields": base}


def _task(board: TaskBoard) -> SwarmTask:
    t = board.add(SwarmTask(title="t", description="BODY"))
    board.set_jira_key(t.id, "WWD-1")
    return board.get(t.id)


# --- the two fields worth importing -------------------------------------------


@pytest.mark.asyncio
async def test_the_reporter_reaches_the_worker(tmp_path: Path, board: TaskBoard):
    """100% populated on both projects — who asked is context a worker needs before
    reading the thread."""
    svc = _svc(tmp_path)
    svc.client.get_issue = AsyncMock(return_value=_issue(reporter={"displayName": "Larissa Oxley"}))
    task = _task(board)
    await svc.refresh_synced_content(task)
    assert "Reported by: Larissa Oxley" in task.description


@pytest.mark.asyncio
async def test_the_due_date_reaches_the_worker(tmp_path: Path, board: TaskBoard):
    svc = _svc(tmp_path)
    svc.client.get_issue = AsyncMock(return_value=_issue(duedate="2026-08-20"))
    task = _task(board)
    await svc.refresh_synced_content(task)
    assert "Due: 2026-08-20" in task.description


@pytest.mark.asyncio
async def test_a_moved_due_date_updates_on_the_next_sync(tmp_path: Path, board: TaskBoard):
    """THE REASON THEY LIVE IN THE REGENERATED BLOCK. A copy stored on SwarmTask would
    silently go stale the moment someone moved the date in Jira, and neither field is
    Swarm's to own."""
    svc = _svc(tmp_path)
    task = _task(board)
    svc.client.get_issue = AsyncMock(return_value=_issue(duedate="2026-08-20"))
    await svc.refresh_synced_content(task)

    svc.client.get_issue = AsyncMock(return_value=_issue(duedate="2026-09-01"))
    await svc.refresh_synced_content(task)

    assert "Due: 2026-09-01" in task.description
    assert "2026-08-20" not in task.description, "the old date survived the rebuild"


@pytest.mark.asyncio
async def test_absent_fields_add_no_noise(tmp_path: Path, board: TaskBoard):
    """duedate is populated on 12-36% of tickets. The majority must not gain an empty
    'Due:' line."""
    svc = _svc(tmp_path)
    svc.client.get_issue = AsyncMock(return_value=_issue())
    task = _task(board)
    await svc.refresh_synced_content(task)
    assert "Due:" not in task.description
    assert "Reported by:" not in task.description


def test_the_never_populated_fields_are_not_requested():
    """Measured at ZERO on 50 tickets. Requesting them would repeat the sprint mistake:
    building for a field nobody fills."""
    from swarm.integrations.jira import _JIRA_ISSUE_FIELDS

    for absent in ("components", "environment", "fixVersions", "issuelinks"):
        assert absent not in _JIRA_ISSUE_FIELDS, f"{absent} is imported but never populated"
    for present in ("reporter", "duedate"):
        assert present in _JIRA_ISSUE_FIELDS


def test_due_date_does_not_silently_reorder_the_board():
    """A due date is a FACT the worker sees. Letting it change priority is a separate
    decision with its own blast radius — the sprint work showed how easily an unverified
    prioritisation rule ships."""
    import ast

    src = Path("src/swarm/integrations/jira.py").read_text()
    fn = next(
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "_format_ticket_facts"
    )
    # Drop the docstring before scanning. The first version of this test failed against
    # the prose EXPLAINING that priority is not touched — the fifth time in this work a
    # scan matched the comment describing the fix rather than the fix.
    if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant):
        fn.body = fn.body[1:]
    assert "priority" not in ast.unparse(fn).lower(), (
        "the due date is reaching task priority; that is a separate decision"
    )


# --- criteria on assign --------------------------------------------------------


def test_criteria_are_synthesized_on_assign_not_parsed_from_the_ticket():
    """Sampling 50 real tickets, ZERO mention acceptance criteria — a parser would
    import nothing. And criteria parsed at import go stale the moment someone edits the
    ticket, because the refresh is additive and does not touch them."""
    import ast

    src = Path("src/swarm/server/task_coordinator.py").read_text()
    fn = next(
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_synthesize_criteria_if_missing"
    )
    code = ast.unparse(fn)
    assert "apply_synthesized_criteria" in code
    assert "jira_key" in code, "it is not scoped to imported tasks"


def test_assign_calls_it_before_returning():
    """Wiring, not the helper. Assignment is the last point before dispatch, so the
    criteria reach the worker in its task message — which is why it is awaited."""
    import ast

    src = Path("src/swarm/server/task_coordinator.py").read_text()
    fn = next(
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "assign_task"
    )
    assert "_synthesize_criteria_if_missing" in ast.unparse(fn), (
        "imported tasks still dispatch with no acceptance criteria, so the verifier "
        "default-passes every one of them"
    )
