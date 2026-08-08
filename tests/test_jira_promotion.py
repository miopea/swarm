"""Workers REQUEST Jira tickets; the operator approves (v2 phases 4-5).

WHY A REQUEST AND NOT A CREATE. Jira is a shared tracker. A ticket an agent raised is
visible to a whole team, gets triaged by someone, and cannot be un-seen. So workers ask
and the operator approves — and it rides the EXISTING proposals surface rather than a
second inbox, because an approval queue nobody watches is worse than none at all: it
looks like oversight while providing none.

THE PROPERTY THIS FILE EXISTS FOR. A proposal sits until a human looks at it, and the
world moves while it waits. The task can be finished, archived, or linked by someone
else in between. So every refusal is re-checked AT APPROVAL TIME rather than trusted
from request time — otherwise approval is a rubber stamp on a fact that stopped being
true, which is the same class as the acknowledged-status bug that transitioned 14 real
tickets.

Spec: docs/specs/jira-integration-v2.md, decisions 4 and 5.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.config.models import JiraConfig
from swarm.db.core import SwarmDB
from swarm.db.task_store import SqliteTaskStore
from swarm.integrations.jira import JiraSyncService
from swarm.tasks.board import TaskBoard
from swarm.tasks.proposal import AssignmentProposal, ProposalType
from swarm.tasks.task import SwarmTask, TaskStatus


@pytest.fixture
def board(tmp_path: Path) -> TaskBoard:
    return TaskBoard(store=SqliteTaskStore(SwarmDB(tmp_path / "swarm.db")))


def _jira(**cfg: Any) -> JiraSyncService:
    """An ENABLED service with the HTTP client stubbed.

    The token manager is not scaffolding: `enabled` requires a connected one, and a
    service built without it refuses everything for reasons unrelated to what is under
    test. Asserted below as a positive control.
    """
    defaults: dict[str, Any] = {"enabled": True, "projects": ["WWD"]}
    defaults.update(cfg)
    mgr = MagicMock()
    mgr.is_connected.return_value = True
    mgr.api_base_url = "https://api.atlassian.com/ex/jira/test-cloud"
    svc = JiraSyncService(JiraConfig(**defaults), token_manager=mgr)
    assert svc.enabled, "positive control: a disabled service makes every test vacuous"
    svc.client.create_issue = AsyncMock(return_value={"key": "WWD-500", "id": "1"})
    svc.client.get_myself = AsyncMock(return_value={"accountId": "acct-123"})
    return svc


def _task(board: TaskBoard, worker: str = "api", title: str = "work") -> SwarmTask:
    task = board.add(SwarmTask(title=title, description="d"))
    board.assign(task.id, worker)
    return task


# --- phase 5: provenance ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_created_ticket_carries_the_swarm_provenance_label(board: TaskBoard):
    """`swarm` means exactly one thing now: an agent raised this."""
    jira = _jira()
    await jira.create_jira_issue(_task(board))

    labels = jira.client.create_issue.await_args.kwargs["labels"]
    assert labels == ["swarm"], f"the provenance label was not applied: {labels}"


@pytest.mark.asyncio
async def test_the_provenance_label_cannot_cause_an_echo_loop(board: TaskBoard):
    """THE TRAP THIS AVOIDS. The old import filter was `labels = "swarm"`. Had created
    tickets carried that label while it still drove routing, Swarm would re-import its
    own output as a new task — an echo loop. Provenance ("came from Swarm") and routing
    ("route to Swarm", the assignee) are separate, which makes the loop impossible
    rather than merely deduped against."""
    jql = _jira().build_jql()
    assert "labels" not in jql.lower(), (
        f"the label Swarm stamps on its own tickets is back in the import query: {jql}"
    )


@pytest.mark.asyncio
async def test_swarm_does_not_label_tickets_it_merely_transitions(board: TaskBoard):
    """Labelling on export would write to other people's tickets on every sync."""
    jira = _jira(
        project_status_maps={"WWD": {"done": "Done"}},
        confirmed_projects=["WWD"],
    )
    jira.client.get_transitions = AsyncMock(return_value=[{"id": "31", "name": "Done"}])
    jira.client.transition_issue = AsyncMock(return_value=True)
    task = _task(board)
    board.set_jira_key(task.id, "WWD-1")

    await jira.export_status(board.get(task.id), TaskStatus.DONE)

    jira.client.create_issue.assert_not_called()
    # transition_issue takes (key, transition_id) and nothing else — there is no path
    # by which a transition can add a label.
    assert jira.client.transition_issue.await_args.args == ("WWD-1", "31")


# --- the created ticket routes home ------------------------------------------


@pytest.mark.asyncio
async def test_the_ticket_is_assigned_to_the_dev_whose_swarm_raised_it(board: TaskBoard):
    """So the outbound rule and the assignee-routing rule agree and it comes back to
    this board rather than sitting unassigned in a shared project."""
    jira = _jira()
    await jira.create_jira_issue(_task(board))

    assert jira.client.create_issue.await_args.kwargs["assignee_account_id"] == "acct-123"


@pytest.mark.asyncio
async def test_an_unresolvable_account_still_creates_the_ticket(board: TaskBoard):
    """Not fatal, deliberately: an unassigned ticket that EXISTS is recoverable; a
    promotion lost because an identity lookup 500'd is just gone."""
    jira = _jira()
    jira.client.get_myself = AsyncMock(side_effect=RuntimeError("boom"))

    key = await jira.create_jira_issue(_task(board))

    assert key == "WWD-500"
    assert jira.client.create_issue.await_args.kwargs["assignee_account_id"] == ""


# --- which project ------------------------------------------------------------


@pytest.mark.asyncio
async def test_creation_uses_the_configured_project_not_the_legacy_field(board: TaskBoard):
    """It used `self._config.project` — the LEGACY single-project field. On a v2 config
    that only sets `projects` it is EMPTY, which Jira rejects, and on a multi-project
    config it silently pinned creation to whichever project was in the old field."""
    jira = _jira(projects=["WWD", "IS"], project="")
    await jira.create_jira_issue(_task(board))

    assert jira.client.create_issue.await_args.kwargs["project"] == "WWD"


@pytest.mark.asyncio
async def test_an_explicit_project_wins(board: TaskBoard):
    jira = _jira(projects=["WWD", "IS"])
    await jira.create_jira_issue(_task(board), project="IS")
    assert jira.client.create_issue.await_args.kwargs["project"] == "IS"


@pytest.mark.asyncio
async def test_no_configured_project_refuses_rather_than_posting_an_empty_key(board: TaskBoard):
    jira = _jira(projects=[], project="")
    with pytest.raises(RuntimeError, match="no Jira project configured"):
        await jira.create_jira_issue(_task(board))
    jira.client.create_issue.assert_not_called()


# --- phase 4: the request lands on the proposals surface ----------------------


def _manager(board: TaskBoard, jira: JiraSyncService | None):
    """A real ProposalManager with real collaborators where it matters.

    The approval path under test is `_approve_jira_promotion`; the notification bus and
    drone log are stubs because they are outside that seam, and the task board and Jira
    service are real because they are inside it.
    """
    from swarm.server.proposals import ProposalManager
    from swarm.tasks.proposal import ProposalStore

    mgr = ProposalManager(
        store=ProposalStore(),
        broadcast_ws=lambda _p: None,
        drone_log=MagicMock(),
        notification_bus=MagicMock(),
        task_board=board,
        get_worker=lambda name: MagicMock(name=name),
        get_workers=list,
        get_pilot=lambda: None,
        assign_task=AsyncMock(),
        complete_task=MagicMock(),
        execute_escalation=AsyncMock(),
        get_jira=lambda: jira,
        task_history=MagicMock(),
    )
    return mgr


def _promotion(task: SwarmTask, project: str = "WWD") -> AssignmentProposal:
    return AssignmentProposal.jira_promotion(
        worker_name="api",
        task_id=task.id,
        task_title=task.title,
        project=project,
        reasoning="the network team must action this",
    )


def test_the_request_is_a_proposal_not_a_ticket(board: TaskBoard):
    """The whole point of phase 4: asking must not create anything."""
    jira = _jira()
    mgr = _manager(board, jira)
    task = _task(board)

    mgr.on_proposal(_promotion(task))

    assert len(mgr.pending) == 1
    assert mgr.pending[0].proposal_type is ProposalType.JIRA_PROMOTION
    jira.client.create_issue.assert_not_called()
    assert board.get(task.id).jira_key == "", "a ticket was linked before anyone approved"


@pytest.mark.asyncio
async def test_approval_creates_the_ticket_and_links_it(board: TaskBoard):
    """The gate must be a gate, not a wall."""
    jira = _jira()
    mgr = _manager(board, jira)
    task = _task(board)
    mgr.on_proposal(_promotion(task))

    assert await mgr.approve(mgr.pending[0].id) is True

    jira.client.create_issue.assert_called_once()
    assert board.get(task.id).jira_key == "WWD-500", "the ticket was created but never linked"


@pytest.mark.asyncio
async def test_the_approved_project_is_the_one_that_was_requested(board: TaskBoard):
    """Re-deriving the project at approval time would let what is created differ from
    what the operator was shown — they would be approving something they never saw."""
    jira = _jira(projects=["WWD", "IS"])
    mgr = _manager(board, jira)
    task = _task(board)
    mgr.on_proposal(_promotion(task, project="IS"))

    await mgr.approve(mgr.pending[0].id)

    assert jira.client.create_issue.await_args.kwargs["project"] == "IS"


# --- the world moves while the request waits ---------------------------------


@pytest.mark.asyncio
async def test_a_task_finished_while_the_request_waited_is_refused(board: TaskBoard):
    """THE PROPERTY. "Never for closed work" is checked at APPROVAL, not only at
    request: short-lived tasks routinely finish before anyone looks at the queue, and a
    ticket raised for finished work is noise a whole team has to triage."""
    from swarm.server.daemon import TaskOperationError

    jira = _jira()
    mgr = _manager(board, jira)
    task = _task(board)
    mgr.on_proposal(_promotion(task))

    board.complete(task.id, "shipped")

    with pytest.raises(TaskOperationError, match="does not raise tickets for finished work"):
        await mgr.approve(mgr.pending[0].id)
    jira.client.create_issue.assert_not_called()


@pytest.mark.asyncio
async def test_a_task_linked_while_the_request_waited_is_refused(board: TaskBoard):
    """A second ticket for one piece of work is exactly the duplication the
    assignee-routing decision exists to prevent."""
    from swarm.server.daemon import TaskOperationError

    jira = _jira()
    mgr = _manager(board, jira)
    task = _task(board)
    mgr.on_proposal(_promotion(task))

    board.set_jira_key(task.id, "WWD-9")

    with pytest.raises(TaskOperationError, match="already linked to WWD-9"):
        await mgr.approve(mgr.pending[0].id)
    jira.client.create_issue.assert_not_called()


@pytest.mark.asyncio
async def test_jira_disconnected_while_the_request_waited_is_refused(board: TaskBoard):
    from swarm.server.daemon import TaskOperationError

    mgr = _manager(board, None)
    task = _task(board)
    mgr.on_proposal(_promotion(task))

    with pytest.raises(TaskOperationError, match="Jira is not enabled"):
        await mgr.approve(mgr.pending[0].id)


@pytest.mark.asyncio
async def test_a_create_that_returns_no_key_does_not_report_success(board: TaskBoard):
    """#1159's shape: the dangerous failure is the one that reports success. An empty
    key means nothing was linked, and saying "promoted" would be a claim the operator
    cannot check."""
    from swarm.server.daemon import TaskOperationError

    jira = _jira()
    jira.client.create_issue = AsyncMock(return_value={})
    mgr = _manager(board, jira)
    task = _task(board)
    mgr.on_proposal(_promotion(task))

    with pytest.raises(TaskOperationError, match="no issue key"):
        await mgr.approve(mgr.pending[0].id)
    assert board.get(task.id).jira_key == ""


# --- the worker-facing verb ---------------------------------------------------


def test_the_mcp_verb_is_registered():
    from swarm.mcp.tools import _HANDLERS, TOOLS

    assert "swarm_request_jira_ticket" in _HANDLERS
    tool = next(t for t in TOOLS if t["name"] == "swarm_request_jira_ticket")
    # The description must not read as though it creates the ticket — a worker that
    # believes it created one will report to the operator that it did.
    assert "does not create" in tool["description"].lower()


# --- the operator can tell what they are approving ---------------------------


def _dashboard_js() -> str:
    return Path("src/swarm/web/static/dashboard.js").read_text()


def test_a_promotion_does_not_render_as_an_assignment():
    """Without its own branch it falls through to the ASSIGNMENT shape and shows an
    "ASSIGN" badge — so the operator believes they are approving a task assignment while
    actually authorising a ticket in a tracker their whole team reads.

    An approval surface that mislabels what it is approving is worse than none: it
    produces consent that was never informed.

    Asserts the DISPATCH, not merely that the strings exist somewhere. An earlier
    version checked `"showJiraPromotion" in js`, which stayed true after the routing
    branch was deleted — the function was still defined, just never reached. It passed
    with the fix removed.
    """
    js = _dashboard_js()
    detail = js[js.index("window.showProposalDetail") :]
    detail = detail[: detail.index("window.showJiraPromotion")]
    assert "jira_promotion" in detail, (
        "showProposalDetail has no branch for promotions, so one opens the ASSIGN modal"
    )
    assert "showJiraPromotion" in detail, "the promotion branch does not open its own view"


def test_the_modal_states_the_consequence_not_just_the_request():
    """Approving on a shared tracker is not undoable the way approving an assignment is:
    the ticket exists, the team sees it, someone triages it."""
    js = _dashboard_js()
    fn = js[js.index("window.showJiraPromotion") :]
    fn = fn[: fn.index("window.showQueenAssignment")]
    assert "If you approve" in fn, "the modal never says what approving will do"
    for expected in ("assigned to you", "swarm", "team"):
        assert expected in fn, f"the consequence text omits {expected!r}"
    assert "Nothing is created unless you approve" in fn


def test_the_target_project_is_visible_before_approving():
    """The project decides who sees the ticket. Approving without it shown is approving
    an unknown."""
    js = _dashboard_js()
    fn = js[js.index("window.showJiraPromotion") :]
    fn = fn[: fn.index("window.showQueenAssignment")]
    assert "p.message" in fn, "the target project is not shown on the approval modal"


# --- the dispatcher contract --------------------------------------------------


def test_every_mcp_handler_is_synchronous():
    """FOUND IN PRODUCTION 2026-08-08, after 6092 unit tests passed.

    ``handle_tool_call`` calls handlers WITHOUT awaiting them. An ``async def`` handler
    therefore returns a coroutine that is never run: the dispatcher's try/except does
    not catch it (the failure happens on the next line, indexing the "content"), so the
    caller gets a bare 500 with NO traceback in the log.

    The unit tests for the verb all passed because they called the handler directly and
    awaited it. Nothing went through the dispatcher — the seam where the contract lives.
    This checks the contract for EVERY handler rather than just the one that broke it,
    because the next person to add a verb will reach for `async def` too.
    """
    import inspect

    from swarm.mcp.tools import _HANDLERS

    coroutines = sorted(name for name, fn in _HANDLERS.items() if inspect.iscoroutinefunction(fn))
    assert not coroutines, (
        f"these MCP handlers are async but the dispatcher never awaits them, so each "
        f"returns an un-run coroutine and 500s: {coroutines}"
    )


def test_the_verb_dispatches_end_to_end_and_returns_content(board: TaskBoard):
    """Drives the REAL dispatcher, which is what the 500 came through.

    Asserting on the returned content block rather than on the handler's return value:
    the bug was invisible at the handler boundary and only appeared one layer out.
    """
    from swarm.mcp.tools import handle_tool_call

    daemon = MagicMock()
    daemon.jira = _jira()
    daemon.task_board = board
    _task(board, worker="api", title="promote me")

    result = handle_tool_call(daemon, "api", "swarm_request_jira_ticket", {"reason": "because"})

    assert isinstance(result, list), f"the dispatcher did not get a content list: {type(result)}"
    assert result and result[0].get("type") == "text", f"malformed content block: {result}"


def test_an_unknown_caller_is_refused_not_crashed(board: TaskBoard):
    """Production sends `worker_name='unknown'` when the MCP identity matches no
    registered worker. That must be an ordinary refusal, not an exception."""
    from swarm.mcp.tools import handle_tool_call

    daemon = MagicMock()
    daemon.jira = _jira()
    daemon.task_board = board

    result = handle_tool_call(daemon, "unknown", "swarm_request_jira_ticket", {"reason": "x"})

    text = result[0]["text"]
    assert "Error:" not in text, f"an unknown caller produced an exception: {text}"
    assert "No eligible task" in text, f"unexpected refusal text: {text}"


# --- resolving the account without the read:jira-user scope -------------------
#
# FOUND AGAINST REAL JIRA 2026-08-08. /rest/api/3/myself returned
# 401 "Unauthorized; scope does not match" while create and search succeeded on the
# same token: the OAuth app requested read:jira-work + write:jira-work only. Every
# promoted ticket was created UNASSIGNED, so it did not route back to the swarm that
# raised it — the entire point of assignee routing.
#
# The scope is now requested, but existing tokens keep the scopes they were granted.
# Without a fallback, every dev who authorized earlier would silently keep producing
# unassigned tickets until they happened to reconnect.


@pytest.mark.asyncio
async def test_the_account_is_derived_when_myself_is_forbidden(board: TaskBoard):
    jira = _jira()
    jira.client.get_myself = AsyncMock(side_effect=RuntimeError("401 scope does not match"))
    jira.client.search_issues = AsyncMock(
        return_value=[{"fields": {"assignee": {"accountId": "acct-fallback"}}}]
    )

    await jira.create_jira_issue(_task(board))

    assert jira.client.create_issue.await_args.kwargs["assignee_account_id"] == "acct-fallback", (
        "an install without read:jira-user produced an unassigned ticket"
    )


@pytest.mark.asyncio
async def test_the_fallback_asks_for_the_assignee_field(board: TaskBoard):
    """THE TRAP. The default search field set does NOT include `assignee`, so a caller
    that forgets to ask receives issues without it and reads the absence as "no result"
    — a fallback that silently never works, indistinguishable from having no assigned
    issues."""
    jira = _jira()
    jira.client.get_myself = AsyncMock(side_effect=RuntimeError("401"))
    jira.client.search_issues = AsyncMock(return_value=[])

    await jira.create_jira_issue(_task(board))

    assert jira.client.search_issues.await_args.kwargs.get("fields") == "assignee", (
        "the fallback did not request the assignee field, so it can never resolve one"
    )


@pytest.mark.asyncio
async def test_myself_is_preferred_when_it_works(board: TaskBoard):
    """The fallback is a safety net, not the primary path: one extra search per
    promotion on installs that do not need it."""
    jira = _jira()
    jira.client.search_issues = AsyncMock(return_value=[])

    await jira.create_jira_issue(_task(board))

    assert jira.client.create_issue.await_args.kwargs["assignee_account_id"] == "acct-123"
    jira.client.search_issues.assert_not_called()


def test_the_oauth_request_asks_for_the_user_scope():
    """New authorizations must not need the fallback at all."""
    from swarm.auth.jira import _SCOPE

    assert "read:jira-user" in _SCOPE, (
        "the scope /myself requires is still not requested, so every new install "
        "depends on the fallback"
    )


# --- proposals must survive long enough to be approved ------------------------


def test_a_promotion_proposal_is_not_expired_for_being_unassignable():
    """FOUND IN PRODUCTION 2026-08-08, on the second live test.

    ``expire_stale`` validated every proposal's task against ``available_tasks`` —
    UNASSIGNED and not on hold. A promotion proposal ALWAYS references a task assigned
    to the worker that requested it, so it could never be in that set and was expired on
    the very next sweep, seconds after being raised.

    The first live test survived only because the operator approved it inside the sweep
    window. Nothing failed; the proposal simply vanished from the surface, which is the
    worst shape — an approval queue that silently drops what it is asked to hold.

    Applies equally to COMPLETION and PARK, whose tasks are ACTIVE and therefore just as
    unassignable, so this is fixed as a rule rather than as a special case.
    """
    from swarm.tasks.proposal import ProposalStore

    store = ProposalStore()
    task_id = "owned-task"
    store.add(
        AssignmentProposal.jira_promotion(
            worker_name="api",
            task_id=task_id,
            task_title="t",
            project="WWD",
            reasoning="because",
        )
    )

    # The task exists and is open, but is NOT assignable — it belongs to the worker.
    expired = store.expire_stale(
        valid_task_ids={task_id},
        valid_worker_names={"api"},
        assignable_task_ids=set(),
    )

    assert expired == 0, "the promotion request was expired before anyone could approve it"
    assert len(store.pending) == 1


def test_an_assignment_proposal_IS_still_expired_when_the_task_is_taken():
    """The other half: assignment proposals are about giving a task to somebody, so a
    task that already has an owner genuinely makes one moot. Relaxing that would leave
    dead proposals on the surface."""
    from swarm.tasks.proposal import ProposalStore

    store = ProposalStore()
    store.add(AssignmentProposal(worker_name="api", task_id="taken", task_title="t"))

    expired = store.expire_stale(
        valid_task_ids={"taken"},
        valid_worker_names={"api"},
        assignable_task_ids=set(),
    )

    assert expired == 1, "an assignment proposal for an already-owned task survived"


def test_a_promotion_for_a_deleted_task_is_still_expired():
    """ "Owned" must not become "never expires" — a proposal whose task is gone is dead."""
    from swarm.tasks.proposal import ProposalStore

    store = ProposalStore()
    store.add(
        AssignmentProposal.jira_promotion(
            worker_name="api", task_id="gone", task_title="t", project="WWD"
        )
    )

    assert store.expire_stale({"other"}, {"api"}, assignable_task_ids=set()) == 1


def test_the_MANAGER_keeps_a_promotion_for_an_owned_task(board: TaskBoard):
    """Drives ProposalManager.expire_stale against a real board.

    The three checks above exercise the STORE. The defect was in what the MANAGER passed
    it — `valid_task_ids` built from `available_tasks` — so all three stayed green with
    the manager regressed. Sixth time in this work that a control passed because the test
    sat on the wrong side of the wiring.
    """
    from swarm.server.proposals import ProposalManager
    from swarm.tasks.proposal import ProposalStore

    task = _task(board, worker="api")  # ASSIGNED, therefore NOT assignable
    mgr = ProposalManager(
        store=ProposalStore(),
        broadcast_ws=lambda _p: None,
        drone_log=MagicMock(),
        notification_bus=MagicMock(),
        task_board=board,
        get_worker=lambda name: MagicMock(name=name),
        get_workers=lambda: [SimpleNamespace(name="api")],
        get_pilot=lambda: None,
        assign_task=AsyncMock(),
        complete_task=MagicMock(),
        execute_escalation=AsyncMock(),
    )
    mgr.on_proposal(_promotion(task))
    assert len(mgr.pending) == 1

    mgr.expire_stale()

    assert len(mgr.pending) == 1, (
        "the manager expired a promotion for a task its own worker owns — the request "
        "disappears from the operator's surface before it can be approved"
    )
