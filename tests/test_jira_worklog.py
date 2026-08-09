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
    # FOLLOWS THE CALL. The history lookup moved into _worked_seconds when the backfill
    # started sharing it; asserting against fire_worklog's own body would now check the
    # wrong function, and loosening the assertion would quietly stop testing anything.
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_worked_seconds"
    )
    if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant):
        fn.body = fn.body[1:]
    code = ast.unparse(fn)

    assert "active_seconds" in code and "get_events" in code, (
        "the worklog duration is not reconstructed from task history"
    )
    assert "started_at" not in code, "still subtracting started_at, which activate() resets"

    fire = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "fire_worklog"
    )
    assert "_worked_seconds" in ast.unparse(fire), "fire_worklog no longer uses the helper"


@pytest.mark.asyncio
async def test_the_true_duration_is_sent_and_NOT_rounded_up(board: TaskBoard):
    """MEASURED against real Jira: it truncates timeSpentSeconds to whole minutes —
    3661s reads back as 3660, and a 163s task reads back as "2m".

    We send the true figure and let Jira truncate rather than rounding up ourselves.
    Rounding 163s up to 180s would bill 17 seconds nobody worked; truncation
    under-reports, which is the safe direction for a timesheet.
    """
    svc = _svc()
    await svc.log_work(_task(board), 163)
    sent = svc.client.add_worklog.await_args.args[1]
    assert sent == 163, f"the duration was adjusted before sending: {sent}"
    assert sent < 180, "rounded up to the next minute, billing time nobody worked"


# --- writes must be visible at the operator's default level -------------------


@pytest.mark.asyncio
async def test_a_written_worklog_is_logged_at_WARNING(board: TaskBoard, caplog: Any):
    """FOUND WHILE VERIFYING #1339: the success line was at INFO, operators run at the
    default WARNING, so nothing in the log said a worklog had been written. I had to
    read Jira to confirm it — which is exactly the position an operator would be in.
    """
    import logging

    svc = _svc()
    with caplog.at_level(logging.WARNING):
        assert await svc.log_work(_task(board), 3600) is True

    msg = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "WWD-1" in msg and "3600" in msg, f"the write is invisible at default level: {msg!r}"


def test_no_jira_WRITE_reports_success_at_INFO():
    """The rule, swept rather than pinned per call site.

    Every write Swarm makes to a SHARED tracker must be visible at the operator's
    default level — a transition, a comment, an assignee change, a worklog, a created
    ticket. Five of these were at INFO and therefore invisible; the sixth someone adds
    would be too, which is why this checks the class.

    Deliberately NOT applied to reads, discovery, or the already-terminal path: that one
    records agreement WITHOUT writing, so it is correctly INFO. The rule is about
    changing someone else's data, not about volume.
    """
    import ast

    src = Path("src/swarm/integrations/jira.py").read_text()
    tree = ast.parse(src)
    writers = {
        "transition_issue",
        "add_comment",
        "assign_issue",
        "add_worklog",
        "create_issue",
        "update_comment",
        "log_work",
        "post_completion_comment",
        "assign_to_me",
        "create_jira_issue",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if node.name not in writers:
            continue
        if "_log.info(" in ast.unparse(node):
            offenders.append(node.name)
    assert not offenders, (
        f"these report a Jira WRITE at INFO, invisible to an operator running at the "
        f"default level: {offenders}"
    )


def test_the_writer_sweep_can_see_the_functions_it_checks():
    """Positive control — a misspelled name set would make the sweep vacuous."""
    import ast

    src = Path("src/swarm/integrations/jira.py").read_text()
    names = {
        n.name
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)
    }
    for expected in ("log_work", "transition_issue", "create_issue", "add_worklog"):
        assert expected in names, f"{expected} is not in the module; the sweep checks nothing"


# --- follow-up 1: EVERY completion path logs time -----------------------------


def test_every_completion_entry_point_goes_through_complete_task():
    """I recorded a follow-up in #1339 claiming the dashboard's force-complete path
    logged no worklog. THAT WAS WRONG, and this pins why.

    queen_force_complete_task and the dashboard route both call d.complete_task(...),
    and `force` is a PARAMETER of that same function — the side-effects block containing
    fire_completion runs either way. Rather than delete the claim quietly, the guarantee
    it doubted is now asserted, so a future path that bypasses complete_task is caught.
    """
    coord = Path("src/swarm/server/task_coordinator.py").read_text()
    body = coord[coord.index("def complete_task") :]
    body = body[: body.index("\n    def ", 10)]
    assert "fire_completion" in body, "the shared completion path no longer logs work"

    queen = Path("src/swarm/mcp/queen_handlers/_tasks.py").read_text()
    force = queen[queen.index("def _handle_force_complete_task") :][:3000]
    assert "complete_task(" in force, "force-complete bypasses the shared path again"

    dash = Path("src/swarm/web/routes/tasks.py").read_text()
    route = dash[dash.index("async def handle_action_complete_task") :][:1200]
    assert "complete_task(" in route, "the dashboard bypasses the shared path"


# --- follow-up 2: time refused while unconfirmed is retried, not lost ----------


def _svc_backfill(board: TaskBoard, jira: Any, history: Any = None):
    from swarm.server.jira_service import JiraService

    svc = JiraService.__new__(JiraService)
    svc._task_board = board
    svc._get_jira = lambda: jira
    svc._drone_log = MagicMock()
    svc._broadcast_ws = lambda _p: None
    svc._track_task = lambda _t: None
    svc._task_history = history if history is not None else MagicMock()
    return svc


def _done_linked(board: TaskBoard, key: str, *, age_s: float = 0) -> SwarmTask:
    import time as _t

    t = board.add(SwarmTask(title="t", description=""))
    board.set_jira_key(t.id, key)
    board.assign(t.id, "api")
    board.activate(t.id)
    board.complete(t.id, "done")
    task = board.get(t.id)
    task.completed_at = _t.time() - age_s
    return task


@pytest.mark.asyncio
async def test_a_task_closed_while_unconfirmed_is_retried_later(board: TaskBoard):
    """THE GAP. log_work correctly refuses for an unconfirmed project, but nothing tried
    again — so confirming a workflow silently forfeited the work already done under it."""
    _done_linked(board, "WWD-1")
    jira = MagicMock()
    jira.enabled = True
    jira.log_work = AsyncMock(return_value=True)
    history = MagicMock()
    history.get_events.return_value = []
    svc = _svc_backfill(board, jira, history)
    svc._worked_seconds = lambda _t: 1800.0

    assert await svc.backfill_worklogs() == 1
    jira.log_work.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_already_billed_task_writes_nothing(board: TaskBoard):
    """Idempotent by REUSE: log_work reads the ticket's worklogs and skips its own
    marker, so re-offering a billed task is a no-op and needs no extra bookkeeping."""
    _done_linked(board, "WWD-2")
    jira = MagicMock()
    jira.enabled = True
    jira.log_work = AsyncMock(return_value=False)  # marker already present
    svc = _svc_backfill(board, jira)
    svc._worked_seconds = lambda _t: 1800.0

    assert await svc.backfill_worklogs() == 0


@pytest.mark.asyncio
async def test_old_closures_age_out_of_the_window(board: TaskBoard):
    """Bounded, or a board with hundreds of closed linked tasks re-reads all of them
    every five minutes forever."""
    _done_linked(board, "WWD-OLD", age_s=30 * 24 * 3600)
    jira = MagicMock()
    jira.enabled = True
    jira.log_work = AsyncMock(return_value=True)
    svc = _svc_backfill(board, jira)
    svc._worked_seconds = lambda _t: 1800.0

    assert await svc.backfill_worklogs() == 0
    jira.log_work.assert_not_called()


@pytest.mark.asyncio
async def test_the_per_cycle_cap_holds(board: TaskBoard):
    for i in range(25):
        _done_linked(board, f"WWD-{i}")
    jira = MagicMock()
    jira.enabled = True
    jira.log_work = AsyncMock(return_value=True)
    svc = _svc_backfill(board, jira)
    svc._worked_seconds = lambda _t: 1800.0

    await svc.backfill_worklogs()
    assert jira.log_work.await_count <= 10, (
        f"the backfill read {jira.log_work.await_count} tickets in one cycle"
    )


@pytest.mark.asyncio
async def test_a_task_with_no_substantiated_time_is_skipped(board: TaskBoard):
    """Never invent a timesheet entry to fill a gap."""
    _done_linked(board, "WWD-3")
    jira = MagicMock()
    jira.enabled = True
    jira.log_work = AsyncMock(return_value=True)
    svc = _svc_backfill(board, jira)
    svc._worked_seconds = lambda _t: None

    assert await svc.backfill_worklogs() == 0
    jira.log_work.assert_not_called()


def test_the_backfill_is_wired_into_the_sync_loop():
    src = Path("src/swarm/server/jira_service.py").read_text()
    loop = src[src.index("async def sync_loop") :]
    loop = loop[: loop.index("except asyncio.CancelledError")]
    assert "backfill_worklogs()" in loop, "nothing ever retries a refused worklog"
