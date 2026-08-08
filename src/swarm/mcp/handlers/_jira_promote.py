"""Handler for ``swarm_request_jira_ticket`` — a worker asks for a Jira ticket.

WHY THIS IS A REQUEST AND NOT A CREATE. Jira is a shared tracker for a whole team. A
ticket an agent raised is visible to everyone, gets triaged by someone, and cannot be
un-seen once created. So workers ask and the operator approves.

WHY IT RIDES THE PROPOSALS SURFACE. That surface already has an operator UI,
notifications, and an autonomous-window concept. A second inbox is a thing that
eventually goes unwatched — and an approval queue nobody watches is worse than no
approval queue, because it looks like oversight while providing none.

Spec: docs/specs/jira-integration-v2.md, decision 4.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import RequestJiraTicketArgs
from swarm.mcp.types import TextContent

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_request_jira_ticket",
        "description": (
            "REQUEST that one of your Swarm tasks be raised as a Jira ticket. "
            "This does NOT create the ticket — it puts a request on the "
            "operator's proposals surface for approval, because Jira is a "
            "shared tracker and an agent-raised ticket is visible to the whole "
            "team. Use it when work you are doing needs to be tracked where "
            "non-Swarm people will see it: something another team must action, "
            "a defect that outlives this session, work that needs a paper "
            "trail. Do NOT use it for routine subtasks of work already "
            "tracked — one Jira ticket per unit of work the team cares about, "
            "not per step you took. REFUSED for tasks that are already linked "
            "to a ticket and for finished work (done/failed): Swarm does not "
            "raise tickets for work nobody can action. The created ticket is "
            "assigned to the dev whose swarm raised it, so it routes back here."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "Why this needs a Jira ticket — what the team gains by "
                        "it being tracked there. The operator reads this to "
                        "decide, so 'needs tracking' is not an answer. "
                        "1-2 sentences."
                    ),
                },
                "task_number": {
                    "type": "integer",
                    "description": (
                        "Which of YOUR tasks to promote (its display number). "
                        "Optional only when you own exactly one eligible task; "
                        "required to disambiguate when you own several."
                    ),
                },
                "project": {
                    "type": "string",
                    "description": (
                        "Jira project key (e.g. WWD). Optional — defaults to "
                        "the first configured project. Only pass it when the "
                        "ticket genuinely belongs somewhere else."
                    ),
                },
            },
            "required": ["reason"],
            "examples": [
                {"reason": "Network team must open port 8443; outlives this session"},
                {
                    "reason": "Regression in the billing export, needs a paper trail",
                    "task_number": 412,
                },
            ],
        },
    }
]


_FINISHED = ("done", "failed")


def _text(message: str) -> list[TextContent]:
    return [{"type": "text", "text": message}]


def _select_task(d: SwarmDaemon, worker_name: str, args: RequestJiraTicketArgs) -> Any:
    """Resolve which task to promote, or return a refusal STRING explaining why not.

    Eligibility mirrors the approval handler exactly. Duplicating it here is not
    redundancy: it turns a request that would certainly be refused into an immediate
    actionable answer, instead of something the operator has to triage and reject.
    """
    board = d.task_board
    owned = list(board.tasks_for_worker(worker_name))
    eligible = [t for t in owned if not t.jira_key and t.status.value not in _FINISHED]

    raw_num = args.get("task_number")
    if raw_num is None or str(raw_num).strip() == "":
        if not eligible:
            return (
                f"No eligible task for '{worker_name}' — you own no unlinked, unfinished "
                f"task to promote. Nothing requested."
            )
        if len(eligible) > 1:
            nums = ", ".join(f"#{t.number}" for t in sorted(eligible, key=lambda t: t.number))
            return (
                f"Ambiguous — you own {len(eligible)} promotable tasks ({nums}). "
                f"swarm_request_jira_ticket won't guess which to raise a team-visible "
                f"ticket for. Re-call it with task_number=<n>. Nothing requested."
            )
        return eligible[0]

    try:
        want = int(raw_num)
    except (TypeError, ValueError):
        return f"'task_number' must be a task number, got {raw_num!r}. Nothing requested."

    target = next((t for t in owned if t.number == want), None)
    if target is None:
        # Distinguish "never yours" from "not yours YET". A task created with
        # target_worker is assigned on a background coroutine, so for a moment after
        # swarm_create_task returns it exists with no owner. Reporting that as a flat
        # "not assigned to you" describes a permanent condition for a sub-second window
        # and sends the caller looking for a routing bug that is not there.
        exists = next((t for t in board.all_tasks() if t.number == want), None)
        if exists is not None and not exists.assigned_worker:
            return (
                f"Task #{want} exists but has no owner yet — if you just created it, "
                f"the assignment is still landing. Retry in a moment. Nothing requested."
            )
        return (
            f"Task #{want} is not assigned to you (or doesn't exist) — you can only "
            f"request promotion for your own task. Nothing requested."
        )
    if target.jira_key:
        return (
            f"#{want} is already linked to {target.jira_key}. A second ticket for one "
            f"piece of work is the duplication this integration exists to avoid. "
            f"Nothing requested."
        )
    if target.status.value in _FINISHED:
        return (
            f"#{want} is {target.status.value} — Swarm does not raise tickets for "
            f"finished work; nobody can action it. Nothing requested."
        )
    return target


def _handle_request_jira_ticket(
    d: SwarmDaemon,
    worker_name: str,
    args: RequestJiraTicketArgs,
) -> list[TextContent]:
    """SYNCHRONOUS, like every other MCP handler.

    ``handle_tool_call`` calls handlers WITHOUT awaiting them. Declaring this
    ``async def`` made the dispatcher store a coroutine, which then failed outside its
    try/except — a bare 500 with no traceback in the log, for a verb whose unit tests
    all passed. They passed because they called the handler directly and awaited it;
    nothing exercised the dispatcher. Requesting a promotion has nothing to await
    anyway: the proposal is stored in memory and the Jira call happens at APPROVAL.
    """
    reason = str(args.get("reason", "") or "").strip()
    if not reason:
        return _text("'reason' is required — the operator reads it to decide. Nothing requested.")

    jira = getattr(d, "jira", None)
    if jira is None or not getattr(jira, "enabled", False):
        return _text(
            "Jira is not enabled for this swarm, so there is nothing to promote to. "
            "Nothing requested."
        )

    selected = _select_task(d, worker_name, args)
    if isinstance(selected, str):
        return _text(selected)
    task = selected

    project = str(args.get("project", "") or "").strip()
    if not project:
        project = jira.default_create_project()
    if not project:
        return _text(
            "No Jira project is configured, so a ticket has nowhere to go. Ask the "
            "operator to set one in Settings > Integrations. Nothing requested."
        )

    from swarm.tasks.proposal import AssignmentProposal

    proposal = AssignmentProposal.jira_promotion(
        worker_name=worker_name,
        task_id=task.id,
        task_title=task.title,
        project=project,
        reasoning=reason,
    )
    d.proposals.on_proposal(proposal)

    # Report what the board reads back rather than asserting the request landed. The
    # proposals surface DROPS a proposal when the operator is focused on this worker,
    # and it de-duplicates — so "I sent it" is not the same claim as "it is pending",
    # and the caller cannot check the difference. (#1159's shape.)
    pending = [p for p in d.proposals.pending if p.id == proposal.id]
    if not pending:
        return _text(
            f"Request for #{task.number} was NOT queued — the proposals surface dropped "
            f"it (the operator is focused on this worker, or an identical request is "
            f"already pending). Nothing to approve; try again later or ask directly."
        )
    return _text(
        f"Requested a {project} ticket for #{task.number}. It is PENDING on the "
        f"operator's proposals surface — no ticket exists yet and none will until they "
        f"approve. Carry on with the task; you'll see the link appear on it if approved."
    )


HANDLERS = {"swarm_request_jira_ticket": _handle_request_jira_ticket}
