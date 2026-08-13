"""Handler for the ``swarm_create_task`` MCP tool.

Extracted from ``mcp/tools.py`` (task #518) — split into its own module
to keep both this file and the sibling ``_tasks.py`` under the audit's
≤ 300 LOC per-module budget without breaking the
schema-and-handler-co-located pattern.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swarm.logging import get_logger
from swarm.mcp._arg_types import CreateTaskArgs
from swarm.mcp.types import TextContent
from swarm.tasks.authority_guard import AuthorityVerdict, screen_task_authority
from swarm.tasks.task import HOLD_TAG, TaskPriority
from swarm.worker.worker import QUEEN_WORKER_NAME

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("mcp.create")


async def _synthesize_then_dispatch(
    d: SwarmDaemon, task_id: str, actor: str, dispatch: Awaitable[Any]
) -> Any:
    """Synthesize the Outcomes rubric for a just-created task, THEN await its
    assign/dispatch coroutine — so the criteria are visible in the task message
    the target worker receives and available to the verifier. Runs inside the
    scheduled background coroutine (not the sync tool call), so swarm_create_task
    returns immediately and the synthesis latency is absorbed before dispatch,
    not before the reply. Synthesis failure never blocks dispatch.
    """
    task = d.task_board.get(task_id)
    if task is not None:
        try:
            await d.tasks.apply_synthesized_criteria(task, actor=actor)
        except Exception:
            _log.warning("criteria synthesis failed for task %s", task_id, exc_info=True)
    return await dispatch


async def _dispatch_then_synthesize(
    d: SwarmDaemon, task_id: str, actor: str, dispatch: Awaitable[Any]
) -> Any:
    """Assign FIRST, synthesize after — for the case where nothing is dispatched.

    THE RACE THIS CLOSES, hit 2026-08-08. ``swarm_create_task`` returns as soon as the
    row exists, and the assignment rides a background coroutine that waits on Outcomes
    synthesis — an LLM call. So a caller that created a task with ``target_worker`` and
    then immediately acted on it saw the task as UNASSIGNED for however long synthesis
    took, and got told "not assigned to you" about a task it had just routed to itself.

    The synthesize-then-dispatch order is deliberate and stays that way when there IS a
    dispatch: the criteria have to be in the message the target worker receives. With
    ``start=False`` no message is ever sent, so nothing needs the criteria first, and
    ownership lands on the next loop tick instead of behind the model.
    """
    result = await dispatch
    task = d.task_board.get(task_id)
    if task is not None:
        try:
            await d.tasks.apply_synthesized_criteria(task, actor=actor)
        except Exception:
            _log.warning("criteria synthesis failed for task %s", task_id, exc_info=True)
    return result


def _schedule_synth_dispatch(
    d: SwarmDaemon,
    task_id: str,
    target: str,
    worker_name: str,
    dispatch: Awaitable[Any],
    *,
    dispatching: bool = True,
) -> None:
    """Schedule synthesis+dispatch on the running loop, or fall back to a
    synchronous board-level assign when there's no loop (test/CLI context).

    Extracted from ``_handle_create_task`` to keep that handler under the
    complexity budget. ``dispatch`` is an already-created coroutine (so the
    assign call is recorded synchronously); on the no-loop path it is closed
    to avoid an un-awaited-coroutine warning.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            dispatch.close()  # type: ignore[attr-defined]
        except Exception:
            pass
        d.task_board.assign(task_id, target)
        return
    runner = _synthesize_then_dispatch if dispatching else _dispatch_then_synthesize
    _task = loop.create_task(runner(d, task_id, worker_name, dispatch))
    _task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_create_task",
        "description": (
            "File a new task on the Swarm task board. Use this when you discover work that "
            "needs doing but shouldn't block your current task — a bug in another module, "
            "a refactor opportunity, a followup from a fix, a cross-project change another "
            "worker owns. Set target_worker to route cross-project work (see the worker name "
            "table in CLAUDE.md). Priority defaults to 'normal'; use 'urgent' only for "
            "production-impacting issues. Attachments must be absolute paths to existing "
            "files (typically screenshots captured during debugging)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": (
                        "Short imperative title (e.g. 'Fix tenant resolution in "
                        "anonymous sessions')."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "What needs doing and why. Include repro steps for bugs, "
                        "acceptance criteria for features."
                    ),
                },
                "target_worker": {
                    "type": "string",
                    "description": (
                        "Worker name to assign to (e.g. 'hub', 'platform', "
                        "'project-root'). Omit to leave unassigned."
                    ),
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high", "urgent"],
                    "description": "'urgent' only for production-impacting issues.",
                },
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Absolute paths to existing files (typically screenshots).",
                },
                "start": {
                    "type": "boolean",
                    "description": (
                        "Whether to dispatch the task into the target_worker's PTY "
                        "immediately (default true). Pass false to queue the task "
                        "in ASSIGNED status without interrupting the target's "
                        "current turn — useful when lining up follow-up work."
                    ),
                },
                "acceptance_criteria": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Machine-checkable success criteria, one per item. Each "
                        "should be a short, verifiable statement (e.g. 'returns "
                        "200 for new tasks' / 'logs creation event'). The verifier "
                        "drone reads these post-completion and cites failed ones "
                        "in the verification reason — leaving them empty falls back "
                        "to the verifier's default-pass behaviour for tasks without "
                        "objective criteria."
                    ),
                },
                "hold": {
                    "type": "boolean",
                    "description": (
                        "File the task as HOLD/dormant (default false). A HOLD "
                        "task stays UNASSIGNED and visible/tracked on the board but "
                        "is NOT auto-dispatched to a worker — use it for deferred "
                        "work you're deliberately parking (e.g. 'hold this jQuery "
                        "3→4 upgrade until we decide'). An operator assigns it "
                        "manually when it's time; the auto-assigner leaves it alone."
                    ),
                },
            },
            "required": ["title"],
            "examples": [
                {
                    "title": "Remove dead feature flag FEATURE_X_ENABLED",
                    "description": (
                        "Flag has been 100% rolled out for 4 weeks. Remove from "
                        "config.ts and all call sites."
                    ),
                    "priority": "low",
                },
                {
                    "title": "Nexus: emails over 1MB fail to ingest",
                    "description": (
                        "Reproduced with attached sample. Root cause likely "
                        "base64 buffer in MailParser. Repro: POST "
                        "/api/v1/nexus/ingest with the attached eml."
                    ),
                    "target_worker": "nexus",
                    "priority": "high",
                    "attachments": ["/home/user/bug-evidence/large-email.eml"],
                },
            ],
        },
    },
]


def _resolve_attachments(args: CreateTaskArgs) -> tuple[list[str] | None, list[TextContent] | None]:
    """Resolve + existence-check attachment paths. Returns (paths, error)."""
    attachments = args.get("attachments") or None
    if not attachments:
        return None, None
    validated: list[str] = []
    for p in attachments:
        rp = Path(p).resolve()
        if not rp.exists():
            return None, [{"type": "text", "text": f"Attachment not found: {p}"}]
        validated.append(str(rp))
    return validated, None


def _park_for_authority_review(
    d: SwarmDaemon, worker_name: str, task: Any, matched: str
) -> list[TextContent]:
    """#894: an auto-generated task fabricated operator authority — log a
    warning + return the parked-for-review response (NOT dispatched)."""
    from swarm.drones.log import LogCategory, SystemAction

    try:
        d.drone_log.add(
            SystemAction.TASK_AUTHORITY_GATED,
            worker_name,
            (
                f"#{task.number} cites operator authority without a verifiable source "
                f"('{matched}') — parked HOLD for review, not dispatched"
            ),
            category=LogCategory.TASK,
        )
    except Exception:
        pass
    return [
        {
            "type": "text",
            "text": (
                f"Task #{task.number} created but PARKED (HOLD) for operator review: its text "
                f"claims operator authority ('{matched}') without a verifiable source. "
                f"Auto-generated tasks can't assert operator decisions — if this is real, cite "
                f"the operator's approval (a thread/message/link) or have the operator dispatch "
                f"it. NOT auto-dispatched."
            ),
        }
    ]


def _coerce_priority(raw: object) -> TaskPriority:
    """Turn the caller's ``priority`` string into a TaskPriority (#1543).

    FALLS BACK TO NORMAL RATHER THAN REFUSING. An unrecognised priority is a
    cosmetic mistake — the task still needs to exist, and refusing to file it would
    lose real work over a typo in a sort key. That is the opposite trade from
    ``target_worker``, where an unroutable value produces an OWNERLESS task nobody
    is watching, so refusing is the safer failure there. Same handler, two
    arguments, two different right answers.

    Accepts the enum directly so a programmatic caller is not forced through a
    string round-trip, and is case-insensitive because the schema's examples are
    lowercase while the enum is not.
    """
    if isinstance(raw, TaskPriority):
        return raw
    if not raw:
        return TaskPriority.NORMAL
    try:
        return TaskPriority(str(raw).strip().lower())
    except ValueError:
        return TaskPriority.NORMAL


def _known_worker_names(d: SwarmDaemon) -> set[str]:
    """Every name a task can legitimately be routed to.

    Unions the LIVE roster with the CONFIGURED one on purpose. A worker that is
    registered but not currently running is a perfectly valid routing target —
    the task waits in its queue — so validating against ``d.workers`` alone would
    refuse legitimate work whenever the target happened to be stopped.
    """
    names = {w.name for w in getattr(d, "workers", []) if getattr(w, "name", "")}
    cfg = getattr(d, "config", None)
    for w in getattr(cfg, "workers", []) or []:
        if getattr(w, "name", ""):
            names.add(w.name)
    return names


def _validate_target_worker(d: SwarmDaemon, args: CreateTaskArgs) -> list[TextContent] | None:
    """Refuse a task routed to a worker that does not exist (#1543).

    THE DEFECT THIS CLOSES. ``target_worker`` was taken, persisted onto the row,
    and handed to an async assign that then failed to resolve it — and the tool
    returned "Task created: #NNNN" regardless. The result was an ownerless task
    carrying a target nobody would ever read. Measured 2026-08-13 with probe
    #1567: an invented worker name produced a success return and a row with
    ``status=unassigned, assigned_worker=NULL, target_worker='<the invented
    name>'``.

    It cost real time: five tasks in one session landed unassigned, three of them
    launch-critical, sitting ownerless for about an hour beside the exact workers
    named in the dropped routing. Nothing nudged them either — IdleWatcher needs
    an ASSIGNED task, and these had no owner at all, so the one mechanism meant to
    catch stalled work could not see them.

    REFUSES BEFORE THE TASK IS CREATED, not after. Validating later would leave an
    orphan row behind every rejection, which trades a silent mis-route for a
    silent litter. Returns None when there is nothing to check — an absent or
    empty ``target_worker`` is the ordinary unrouted-task case, not an error.

    Names the roster in the refusal. A caller that cannot see the valid options
    guesses again, which is the same loop that made the original silence
    expensive.
    """
    target = (args.get("target_worker") or "").strip()
    if not target:
        return None
    known = _known_worker_names(d)
    # FAILS OPEN ON AN UNKNOWABLE ROSTER, deliberately. An empty set means we could
    # not determine who exists — not that nobody does. Refusing every route in that
    # state would be a worse failure than the one this closes: it would block ALL
    # routing whenever the roster lookup is unavailable, where the original defect
    # only mis-routed. Same reasoning `_check_file_lock` records for unknown
    # identity. In production the roster is never legitimately empty; when it is,
    # there is nobody to route to and the assign is a no-op anyway.
    if not known or target in known:
        return None
    roster = ", ".join(sorted(known))
    return [
        {
            "type": "text",
            "text": (
                f"No worker named '{target}' — task NOT created. Nothing was routed "
                f"and no row was left behind.\n"
                f"Known workers: {roster}\n"
                f"If you meant to file this without an owner, omit target_worker."
            ),
        }
    ]


def _handle_create_task(
    d: SwarmDaemon, worker_name: str, args: CreateTaskArgs
) -> list[TextContent]:
    title = args.get("title", "")
    if not title:
        return [{"type": "text", "text": "Missing 'title'"}]
    if (routing_error := _validate_target_worker(d, args)) is not None:
        return routing_error
    attachments, att_error = _resolve_attachments(args)
    if att_error is not None:
        return att_error
    description = args.get("description", "")
    # #894: a task arriving here is AUTO-GENERATED (a worker/drone filed it via
    # swarm_create_task — operator tasks come through the dashboard). If its
    # text CITES operator authority / a policy amendment with no verifiable
    # source, it's fabricated authorization (the @types/node "operator opted
    # in, amendment in flight" case). Park it HOLD for operator review instead
    # of dispatching — never let an auto-task invent authority to act.
    # #939: the Queen is the operator's authorized relay — surfacing operator
    # reports ("operator says…", "operator reported…") IS her job, so the
    # authority guard must not park every Queen-authored task. Exempt her
    # (mirrors her #873 fanout-cap exemption); the guard still catches genuine
    # auto-generated / worker-spawned fabrications.
    if worker_name == QUEEN_WORKER_NAME:
        authority = AuthorityVerdict(flagged=False, matched="")
    else:
        authority = screen_task_authority(title, description)
    # A HOLD task is filed UNASSIGNED but tagged so the auto-assign drone won't
    # grab it (see SwarmTask.is_available). Stays visible/tracked. Authority-
    # flagged tasks are forced HOLD regardless of the caller's ``hold`` arg.
    on_hold = bool(args.get("hold")) or authority.flagged
    tags = [HOLD_TAG] if on_hold else None
    task = d.create_task(
        title=title,
        description=description,
        attachments=attachments,
        tags=tags,
        actor=worker_name,
        # #1543: `priority` is DECLARED in this tool's inputSchema, documented in its
        # examples, and was never read — so `create_task`'s TaskPriority.NORMAL
        # default applied to every MCP-created task no matter what the caller sent.
        # Reproduced live post-reload by #1574, which was filed `high`, landed
        # `normal`, and gates a time-boxed page nobody would have re-prioritised
        # because the call reported success.
        #
        # Distinct from the other two defects on this path: `assigned_worker` is an
        # UNDECLARED key dropped by a dispatcher that validates nothing, and
        # `target_worker` was a declared key accepted but never resolved. This one is
        # a declared key never read at all.
        priority=_coerce_priority(args.get("priority")),
    )
    # Acceptance criteria flow through edit_task to keep create_task's
    # signature small. The field has lived on SwarmTask since v1 but
    # was unread until Phase 2 wired it into the verifier (2026-05-08).
    raw_criteria = args.get("acceptance_criteria")
    if isinstance(raw_criteria, list):
        cleaned = [str(c).strip() for c in raw_criteria if str(c).strip()]
        if cleaned:
            d.edit_task(task.id, acceptance_criteria=cleaned, actor=worker_name)
    if authority.flagged:
        return _park_for_authority_review(d, worker_name, task, authority.matched)
    target = args.get("target_worker")
    # Record cross-project attribution BEFORE assigning. When a worker
    # files a task for a *different* worker, the calling worker is the
    # source and the arg is the target — without this the task row
    # lands in the DB with ``source_worker=''`` and cross-project
    # lineage is lost. Self-targeted tasks aren't cross-project and
    # are skipped.
    if target and target != worker_name:
        source = worker_name if worker_name and worker_name != "unknown" else ""
        d.edit_task(
            task.id,
            source_worker=source,
            target_worker=target,
            actor=worker_name,
        )
    if target:
        # Phase 1 of task #225: by default, assignment DISPATCHES the task
        # into the target worker's PTY. The old behaviour stopped at
        # ``assign_task`` (ASSIGNED status only), which left workers sitting
        # on queued work because nothing pushed the task body into their
        # input buffer. ``start=False`` opts out for Queen/operator flows
        # that want to line up work without interrupting the target
        # worker's current turn. Self-targeted tasks never dispatch —
        # injecting a task description back into the caller's own PTY
        # would interleave with the response it is currently producing.
        should_dispatch = bool(args.get("start", True)) and target != worker_name
        # Create the dispatch coroutine eagerly (records the assign call), then
        # schedule it behind Outcomes-rubric synthesis.
        if should_dispatch:
            dispatch = d.assign_and_start_task(task.id, target, actor=worker_name)
        else:
            dispatch = d.assign_task(task.id, target, actor=worker_name)
        _schedule_synth_dispatch(
            d, task.id, target, worker_name, dispatch, dispatching=should_dispatch
        )
    suffix = " [HOLD — parked, not auto-dispatched]" if (tags and not target) else ""
    return [{"type": "text", "text": f"Task created: #{task.number} {title}{suffix}"}]


HANDLERS = {"swarm_create_task": _handle_create_task}
