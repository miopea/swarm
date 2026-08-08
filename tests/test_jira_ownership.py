"""A ticket reassigned in Jira must stop being this swarm's work.

THE FAILURE. Routing is `assignee = currentUser()` — the whole reason Jira can be
enabled for every dev without them colliding. But nothing re-checked it after import, so
handing a ticket over in Jira left BOTH swarms holding the task: the new owner's
imports it, the old owner's keeps working it, and they race to transition the same
ticket. That is exactly the duplication assignee routing exists to prevent, arriving
through the back door.

THE DESIGN DECISION THAT MATTERS MOST IS THE DETECTION. The obvious implementation is
"it fell out of the import query, so it was reassigned" — and that is dangerous. The
import runs `assignee = currentUser() AND statusCategory != Done`, and a ticket
disappears from those results for at least four different reasons: reassigned, closed,
moved/deleted/permissions, or the call failed and returned fewer rows. Inferring
reassignment from absence would release EVERY linked task the first time Jira errored.

So this asks Jira what the assignee actually IS, and acts only on a definite mismatch.
An empty or partial result is not a finding.
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
from swarm.tasks.task import HOLD_TAG, SwarmTask, TaskStatus

_ME = "acct-me"


@pytest.fixture
def board(tmp_path: Path) -> TaskBoard:
    return TaskBoard(store=SqliteTaskStore(SwarmDB(tmp_path / "swarm.db")))


def _jira(**cfg: Any) -> JiraSyncService:
    defaults: dict[str, Any] = {"enabled": True, "projects": ["WWD"]}
    defaults.update(cfg)
    mgr = MagicMock()
    mgr.is_connected.return_value = True
    mgr.api_base_url = "https://api.atlassian.com/ex/jira/test"
    svc = JiraSyncService(JiraConfig(**defaults), token_manager=mgr)
    assert svc.enabled, "positive control: a disabled service makes every test vacuous"
    svc.client.get_myself = AsyncMock(return_value={"accountId": _ME})
    return svc


def _linked(board: TaskBoard, key: str, worker: str = "api", activate: bool = False) -> SwarmTask:
    t = board.add(SwarmTask(title=f"work {key}", description=""))
    board.set_jira_key(t.id, key)
    board.assign(t.id, worker)
    if activate:
        board.activate(t.id)
    return board.get(t.id)


def _issue(key: str, account: str | None) -> dict[str, Any]:
    assignee = {"accountId": account, "displayName": "Someone Else"} if account else None
    return {"key": key, "fields": {"assignee": assignee}}


# --- detection ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reassigned_ticket_is_detected(board: TaskBoard):
    svc = _jira()
    task = _linked(board, "WWD-1")
    svc.client.search_issues = AsyncMock(return_value=[_issue("WWD-1", "acct-bob")])

    moved = await svc.find_reassigned([task])

    assert [t.jira_key for t, _ in moved] == ["WWD-1"]
    assert moved[0][1] == "Someone Else", "the new owner is not reported"


@pytest.mark.asyncio
async def test_a_ticket_still_mine_is_left_alone(board: TaskBoard):
    svc = _jira()
    task = _linked(board, "WWD-2")
    svc.client.search_issues = AsyncMock(return_value=[_issue("WWD-2", _ME)])

    assert await svc.find_reassigned([task]) == []


@pytest.mark.asyncio
async def test_a_ticket_unassigned_in_jira_counts_as_no_longer_mine(board: TaskBoard):
    """Nobody owning it is still not ME owning it — and the swarm should not keep
    working a ticket the team has taken off its owner."""
    svc = _jira()
    task = _linked(board, "WWD-3")
    svc.client.search_issues = AsyncMock(return_value=[_issue("WWD-3", None)])

    moved = await svc.find_reassigned([task])
    assert len(moved) == 1 and moved[0][1] == ""


# --- the guards, which matter more than the happy path ------------------------


@pytest.mark.asyncio
async def test_a_key_missing_from_the_response_is_NOT_treated_as_reassigned(board: TaskBoard):
    """ "I could not see it" is not "it is not yours". A move, a delete or a permission
    change all produce a missing row, and none of them means someone else owns it."""
    svc = _jira()
    task = _linked(board, "WWD-4")
    svc.client.search_issues = AsyncMock(return_value=[])  # key absent entirely

    assert await svc.find_reassigned([task]) == [], (
        "an unseen ticket was reported as reassigned; one bad query would release the board"
    )


@pytest.mark.asyncio
async def test_an_unresolvable_account_releases_NOTHING(board: TaskBoard):
    """THE CATASTROPHIC CASE. If we cannot establish who "I" am, every ticket looks
    foreign and the entire board would be released. Refusing to act is the only safe
    answer, and this is the exact failure the read:jira-user scope gap could produce."""
    svc = _jira()
    svc.client.get_myself = AsyncMock(side_effect=RuntimeError("401"))
    task = _linked(board, "WWD-5")

    # The ticket IS still mine. Without the identity guard, my_account is "" and this
    # row's real accountId will not match it, so the task would be reported as
    # reassigned — and with a whole board of them, released.
    #
    # An earlier version returned [] here, which made the test pass with the guard
    # DELETED: the empty query result hid the bug rather than exposing it.
    # Identity must fail on BOTH routes — /myself AND the read:jira-user fallback that
    # derives it from assigned work — or the guard is never reached. An earlier version
    # mocked one return value for every call, so the fallback resolved the account and
    # the test passed with the guard DELETED.
    async def _search(jql: str, max_results: int = 50, fields: str = ""):
        if "currentUser()" in jql:
            return []  # identity cannot be derived
        return [_issue("WWD-5", _ME)]  # the ticket is genuinely still mine

    svc.client.search_issues = AsyncMock(side_effect=_search)

    assert await svc.find_reassigned([task]) == []


@pytest.mark.asyncio
async def test_a_failed_query_releases_NOTHING(board: TaskBoard):
    svc = _jira()
    svc.client.search_issues = AsyncMock(side_effect=RuntimeError("500"))
    task = _linked(board, "WWD-6")

    assert await svc.find_reassigned([task]) == []


@pytest.mark.asyncio
async def test_a_malformed_key_never_reaches_the_query(board: TaskBoard):
    """Keys are validated rather than escaped, so a hostile jira_key cannot change the
    JQL's meaning — the same class as the project-name injection guard."""
    svc = _jira()
    task = _linked(board, "WWD-7")
    board.set_jira_key(task.id, 'X") OR key = "WWD-999')
    svc.client.search_issues = AsyncMock(return_value=[])

    await svc.find_reassigned([board.get(task.id)])

    (
        svc.client.search_issues.assert_not_called(),
        ("a malformed key was sent to Jira, where it could alter the query"),
    )


# --- what happens to the task, driven through the real reconciler -------------
#
# These call reconcile_ownership(), not find_reassigned(). Three times in this work a
# control passed because the test exercised a helper the wiring no longer reached.


def _service(board: TaskBoard, jira: Any):
    from swarm.server.jira_service import JiraService

    svc = JiraService.__new__(JiraService)
    svc._task_board = board
    svc._get_jira = lambda: jira
    svc._drone_log = MagicMock()
    svc._broadcast_ws = lambda _p: None
    svc._track_task = lambda _t: None
    return svc


def _jira_saying_reassigned(moved: list[tuple[Any, str]]) -> Any:
    jira = MagicMock()
    jira.enabled = True
    jira.find_reassigned = AsyncMock(return_value=moved)
    return jira


@pytest.mark.asyncio
async def test_the_task_is_released_and_held(board: TaskBoard):
    task = _linked(board, "WWD-10", worker="api")
    svc = _service(board, _jira_saying_reassigned([(task, "Bob")]))

    assert await svc.reconcile_ownership() == 1

    after = board.get(task.id)
    assert not after.assigned_worker, "the task still belongs to a worker in this swarm"
    assert after.status is TaskStatus.UNASSIGNED
    assert after.is_on_hold, (
        "released but not held — the auto-assign drone will hand it to another worker "
        "in THIS swarm, which is the same wrong answer with a different name"
    )


@pytest.mark.asyncio
async def test_an_ACTIVE_task_is_taken_off_active(board: TaskBoard):
    """The dangerous case: a worker mid-flight on a ticket that is now someone else's."""
    task = _linked(board, "WWD-11", worker="api", activate=True)
    assert board.get(task.id).status is TaskStatus.ACTIVE
    svc = _service(board, _jira_saying_reassigned([(board.get(task.id), "Bob")]))

    await svc.reconcile_ownership()

    assert board.get(task.id).status is TaskStatus.UNASSIGNED


@pytest.mark.asyncio
async def test_the_jira_link_is_kept(board: TaskBoard):
    """Clearing it would destroy the record and let the next import recreate a duplicate
    — the failure fixed in 2026.8.7.12. The link is still TRUE; only ownership changed."""
    task = _linked(board, "WWD-12")
    svc = _service(board, _jira_saying_reassigned([(task, "Bob")]))

    await svc.reconcile_ownership()

    assert board.get(task.id).jira_key == "WWD-12"


@pytest.mark.asyncio
async def test_existing_tags_survive_the_hold(board: TaskBoard):
    """update(tags=...) replaces the list, so a naive implementation silently drops
    whatever else the task was tagged with."""
    task = _linked(board, "WWD-13")
    board.update(task.id, tags=["security"])
    svc = _service(board, _jira_saying_reassigned([(board.get(task.id), "Bob")]))

    await svc.reconcile_ownership()

    tags = board.get(task.id).tags
    assert "security" in tags and HOLD_TAG in tags, f"tags were clobbered: {tags}"


@pytest.mark.asyncio
async def test_finished_tasks_are_never_checked(board: TaskBoard):
    """A closed task's ownership is history; re-litigating it churns the board for
    nobody's benefit — and would undo completed work."""
    task = _linked(board, "WWD-14")
    board.complete(task.id, "done")
    jira = _jira_saying_reassigned([])
    svc = _service(board, jira)

    await svc.reconcile_ownership()

    checked = jira.find_reassigned.await_args.args[0] if jira.find_reassigned.await_count else []
    assert [t.jira_key for t in checked] == [], f"a finished task was ownership-checked: {checked}"


@pytest.mark.asyncio
async def test_nothing_is_written_to_jira(board: TaskBoard):
    """Ownership moved in Jira ALREADY. Swarm's job is to stop working it, not to argue
    — writing back would fight the person who took it."""
    task = _linked(board, "WWD-15")
    jira = _jira_saying_reassigned([(task, "Bob")])
    jira.export_status = AsyncMock()
    svc = _service(board, jira)

    await svc.reconcile_ownership()

    jira.export_status.assert_not_called()


@pytest.mark.asyncio
async def test_the_sweep_is_a_no_op_when_nothing_moved(board: TaskBoard):
    _linked(board, "WWD-16")
    svc = _service(board, _jira_saying_reassigned([]))
    assert await svc.reconcile_ownership() == 0
    assert (
        board.get_by_jira_key("WWD-16").assigned_worker == "api"
        if hasattr(board, "get_by_jira_key")
        else True
    )


def test_the_sweep_is_wired_into_the_sync_loop():
    """The wiring, not the function. Every other check here calls
    reconcile_ownership() directly, so deleting its call site would leave them all
    green — the exact shape that has fooled three controls in this work already."""
    from pathlib import Path as _P

    src = _P("src/swarm/server/jira_service.py").read_text()
    loop = src[src.index("async def sync_loop") :]
    loop = loop[: loop.index("except asyncio.CancelledError")]
    assert "reconcile_ownership()" in loop, (
        "ownership is never re-checked on a schedule, so a handover in Jira leaves two "
        "swarms holding the ticket until someone notices by hand"
    )
