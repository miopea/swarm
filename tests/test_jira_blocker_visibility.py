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
    task = _task(board)
    task.external_blocker_ref = "platform release 6.2"
    assert await svc.sync_blocker_note(task, "waiting on the platform deploy") is True
    body = svc.client.add_comment.await_args.args[1]
    # CHANGED 2026-08-09: the note names the ARTIFACT, not the free-text reason, which
    # is written worker-to-operator and read verbatim by whoever raised the ticket.
    assert "Work on this is paused" in body and "platform release 6.2" in body
    assert "waiting on the platform deploy" not in body


@pytest.mark.asyncio
async def test_the_note_is_UPDATED_not_duplicated(board: TaskBoard):
    """Five-minute loop. A second comment per cycle would bury the ticket."""
    svc = _svc()
    svc.client.get_comments = AsyncMock(
        return_value=[
            {
                "id": "77",
                "body": (
                    "[swarm:blocker:1] Work on this is paused while we wait on: "
                    "old thing. We will update this ticket when it resumes."
                ),
            }
        ]
    )
    task = _task(board)
    task.external_blocker_ref = "the new thing"

    assert await svc.sync_blocker_note(task, "reason text") is True

    svc.client.add_comment.assert_not_called()
    key, cid, body = svc.client.update_comment.await_args.args
    assert cid == "77" and "the new thing" in body


@pytest.mark.asyncio
async def test_an_unchanged_blocker_writes_nothing(board: TaskBoard):
    """THE NOISE GUARD. Without it every cycle rewrites the same sentence forever."""
    svc = _svc()
    task = _task(board)
    task.external_blocker_ref = "a deploy"
    same = (
        f"[swarm:blocker:{task.number}] Work on this is paused while we wait on: "
        f"a deploy. We will update this ticket when it resumes."
    )
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
        return_value=[
            {"id": "77", "body": "[swarm:blocker:1] Work on this is paused while we wait on: x."}
        ]
    )

    assert await svc.sync_blocker_note(_task(board), "") is True
    body = svc.client.update_comment.await_args.args[2]
    assert "Work on this has resumed" in body


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


# --- the note is written for the reporter, not the operator --------------------


def _blocked(board: TaskBoard, ref: str, reason: str) -> SwarmTask:
    t = _task(board)
    t.number = 1347
    t.external_blocker_ref = ref
    t.block_reason = reason
    return t


def test_the_raw_internal_reason_never_reaches_the_ticket():
    """OBSERVED ON WWD-6743 2026-08-09. The note posted the block reason verbatim:

        "Swarm is BLOCKED on this: Closing-comment template needs the 2026.8.9.17
         reload before I can test it — that release fixes synced content being..."

    Block reasons are written worker-to-operator. On a service-desk ticket the reporter
    reads that and learns nothing — the same internal-voice problem the closing comment
    had, on a different surface.
    """
    from swarm.integrations.jira import _blocker_note_body
    from swarm.tasks.task import AWAITING_OPERATOR_REF

    task = _blocked(_board_for(), AWAITING_OPERATOR_REF, "needs the 2026.8.9.17 reload")
    body = _blocker_note_body(task, task.block_reason, "[swarm:blocker:1347]")

    assert "2026.8.9.17" not in body, "an internal version number reached the ticket"
    assert "reload" not in body.lower()
    assert "pending a decision from the team" in body


def test_an_external_blocker_NAMES_the_artifact():
    """external_blocker_ref is an artifact by design — the verb asks for "npm
    eslint@^10" or a PR URL — so naming it tells a reader something true and checkable,
    unlike the free-text reason."""
    from swarm.integrations.jira import _blocker_note_body

    task = _blocked(_board_for(), "platform release 6.2", "some long internal narrative")
    body = _blocker_note_body(task, task.block_reason, "[swarm:blocker:1347]")

    assert "platform release 6.2" in body
    assert "internal narrative" not in body


def test_a_blocker_with_no_artifact_says_only_what_is_certain():
    from swarm.integrations.jira import _blocker_note_body

    task = _blocked(_board_for(), "", "internal-only explanation")
    body = _blocker_note_body(task, task.block_reason, "[swarm:blocker:9]")

    assert "internal-only" not in body
    assert "wait on a dependency" in body


def test_clearing_reads_plainly():
    from swarm.integrations.jira import _blocker_note_body

    body = _blocker_note_body(_blocked(_board_for(), "", ""), "", "[swarm:blocker:9]")
    assert body == "[swarm:blocker:9] Work on this has resumed."


def test_it_does_not_say_BLOCKED_in_swarm_jargon():
    """ "Swarm is BLOCKED on this" is our vocabulary, not the reporter's."""
    from swarm.integrations.jira import _blocker_note_body

    task = _blocked(_board_for(), "a PR", "r")
    body = _blocker_note_body(task, "r", "[swarm:blocker:1]")
    assert "BLOCKED" not in body
    assert "Work on this is paused" in body


def _board_for() -> TaskBoard:
    """A throwaway board — these check pure formatting, not persistence."""
    import tempfile

    from swarm.db.core import SwarmDB
    from swarm.db.task_store import SqliteTaskStore

    return TaskBoard(store=SqliteTaskStore(SwarmDB(Path(tempfile.mkdtemp()) / "b.db")))
