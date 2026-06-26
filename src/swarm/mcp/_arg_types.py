"""TypedDict definitions for every MCP tool's argument payload.

Mirrors the ``inputSchema.properties`` of each tool in
:mod:`swarm.mcp.tools` and :mod:`swarm.mcp.queen_tools`. The JSON-RPC
wire layer hands handlers a ``dict[str, Any]``; binding it to one of
these TypedDicts gives type checkers + IDEs concrete field names and
flags caller typos that ``dict[str, Any]`` would silently swallow.

All TypedDicts use ``total=False`` because the runtime input from
Claude Code may omit any field — required-field enforcement happens
inside each handler's body (the ``if not field: return error`` guard),
not at the type-system layer. This is intentional: a JSON-RPC mis-send
should produce a polite tool error, not a Python ``KeyError``.

Conventions:

* Enum-like fields (``msg_type``, ``priority``) are typed as plain
  ``str`` rather than ``Literal[...]`` — the handlers already validate
  the value and the ``Literal`` would force every test fixture to
  cast, with marginal benefit.
* Arrays of dicts (``swarm_batch.ops``, ``queen_post_thread.widgets``)
  stay as ``list[dict[str, Any]]`` — the per-element shape is its own
  schema (e.g. each ``ops`` entry is a ``{tool, args}`` pair) and
  defining a nested TypedDict for every variant would re-introduce the
  Any-soup the audit flagged.
* No fields are reordered relative to their schema — easier to diff
  against the tool definition during review.
"""

from __future__ import annotations

from typing import Any, TypedDict

# ---------------------------------------------------------------------------
# Worker-facing tools (swarm_*)
# ---------------------------------------------------------------------------


class CheckMessagesArgs(TypedDict, total=False):
    """``swarm_check_messages`` — no args."""


class SendMessageArgs(TypedDict, total=False):
    """``swarm_send_message`` — direct or broadcast worker message."""

    to: str  # recipient name or "*" for broadcast
    type: str  # "finding" | "warning" | "dependency" | "status"
    content: str


class ReportBlockerArgs(TypedDict, total=False):
    """``swarm_report_blocker`` — declare a task is blocked on another."""

    task_number: int
    blocked_by_task: int
    reason: str


class ParkTaskArgs(TypedDict, total=False):
    """``swarm_park_task`` — voluntarily park an active task."""

    reason: str
    task_number: int


class BlockExternalArgs(TypedDict, total=False):
    """``swarm_block_on_external`` — park a task on an upstream/external wait."""

    watch_ref: str
    reason: str
    task_number: int


class NoteToQueenArgs(TypedDict, total=False):
    """``swarm_note_to_queen`` — side-channel note to the Queen."""

    content: str


class DraftEmailArgs(TypedDict, total=False):
    """``swarm_draft_email`` — stage a draft for operator review."""

    to: list[str]
    subject: str
    body: str
    cc: list[str]
    body_type: str  # "text" | "html"
    reason: str


class TaskStatusArgs(TypedDict, total=False):
    """``swarm_task_status`` — query the task board."""

    filter: str  # "all" | "backlog" | "unassigned" | "assigned" | "active" | "mine"
    limit: int
    include_completed: bool
    number: int


class QueryPeersArgs(TypedDict, total=False):
    """``swarm_query_peers`` — read-only snapshot of peer worker state."""

    state: str  # optional filter, e.g. "RESTING"


class ClaimFileArgs(TypedDict, total=False):
    """``swarm_claim_file`` — advisory lock on a path."""

    path: str


class CompleteTaskArgs(TypedDict, total=False):
    """``swarm_complete_task`` — finish a task with a resolution."""

    resolution: str
    number: int


class CreateTaskArgs(TypedDict, total=False):
    """``swarm_create_task`` — create work on the board."""

    title: str
    description: str
    target_worker: str
    priority: str  # "low" | "normal" | "high" | "urgent"
    attachments: list[str]
    start: bool
    acceptance_criteria: list[str]


class GetLearningsArgs(TypedDict, total=False):
    """``swarm_get_learnings`` — search the operator's correction log."""

    query: str


class GetPlaybooksArgs(TypedDict, total=False):
    """``swarm_get_playbooks`` — search the synthesized procedural memory."""

    query: str
    scope: str
    limit: int


class BatchArgs(TypedDict, total=False):
    """``swarm_batch`` — run multiple tool calls sequentially.

    Each ``ops`` entry is ``{tool: str, args: dict[str, Any]}``;
    keeping it as a list of dicts mirrors the on-wire shape rather
    than introducing a nested TypedDict for the inner pair.
    """

    ops: list[dict[str, Any]]
    fail_fast: bool


class ReportProgressArgs(TypedDict, total=False):
    """``swarm_report_progress`` — phase / percent / blockers update."""

    phase: str
    pct: float
    blockers: str


# ---------------------------------------------------------------------------
# Queen-only tools (queen_*)
# ---------------------------------------------------------------------------


class QueenViewWorkerStateArgs(TypedDict, total=False):
    worker: str
    lines: int


class QueenViewTaskBoardArgs(TypedDict, total=False):
    status: str
    worker: str
    limit: int


class QueenViewMessagesArgs(TypedDict, total=False):
    worker: str
    since_seconds: int
    limit: int
    full: bool


class QueenViewMessageStreamArgs(TypedDict, total=False):
    since_seconds: int
    limit: int
    actionable_only: bool
    full: bool


class QueenViewBuzzLogArgs(TypedDict, total=False):
    worker: str
    category: str
    since_seconds: int
    limit: int


class QueenViewDroneActionsArgs(TypedDict, total=False):
    worker: str
    since_seconds: int
    limit: int


class QueenPostThreadArgs(TypedDict, total=False):
    """``queen_post_thread`` — start a new operator-visible thread.

    ``widgets`` is a list of inline dashboard widgets; each entry is a
    ``{type, ...}`` dict whose shape depends on widget kind, kept as
    free-form ``dict[str, Any]`` here.
    """

    title: str
    body: str
    kind: str
    worker: str
    task_id: str
    widgets: list[dict[str, Any]]


class QueenReplyArgs(TypedDict, total=False):
    thread_id: str
    body: str
    widgets: list[dict[str, Any]]


class QueenUpdateThreadArgs(TypedDict, total=False):
    thread_id: str
    status: str
    reason: str


class QueenSaveLearningArgs(TypedDict, total=False):
    context: str
    correction: str
    applied_to: str
    thread_id: str


class QueenQueryLearningsArgs(TypedDict, total=False):
    applied_to: str
    search: str
    limit: int


class QueenReassignTaskArgs(TypedDict, total=False):
    number: int
    task_id: str
    to_worker: str
    start: bool
    reason: str


class QueenInterruptWorkerArgs(TypedDict, total=False):
    worker: str
    reason: str


class QueenForceCompleteTaskArgs(TypedDict, total=False):
    number: int
    task_id: str
    resolution: str
    reason: str


class QueenPromptWorkerArgs(TypedDict, total=False):
    worker: str
    prompt: str
    reason: str
