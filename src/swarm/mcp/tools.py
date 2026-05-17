"""MCP tool definitions for the Swarm server.

Each tool is a dict conforming to MCP's Tool schema:
  {name, description, inputSchema: {type, properties, required}}

Handler functions take (daemon, worker_name, arguments) and return content.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


def _hash_source(path: Path) -> str:
    """Return sha256 of *path*, or empty string if the file can't be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


# Path + hash of this module captured at import time. The hash frozen here is
# the one the running daemon is actually serving — if tools.py on disk
# changes, the daemon keeps publishing the old ``TOOLS`` list until it
# reloads. ``tools_source_drift()`` compares stored vs current so the
# dashboard can prompt the operator to reload before live MCP calls hit a
# stale schema (regression scenario: task #169 fix landed but workers
# kept seeing the old no-``number`` schema until the daemon restarted).
_SOURCE_PATH: Path = Path(__file__).resolve()
_SOURCE_HASH_AT_IMPORT: str = _hash_source(_SOURCE_PATH)


def tools_source_drift() -> dict[str, Any]:
    """Return whether ``tools.py`` on disk differs from the imported copy.

    Output shape::

        {
          "drift": bool,           # True when current hash != import-time hash
          "source_path": str,      # absolute path to tools.py
          "startup_hash": str,     # sha256 captured when daemon started
          "current_hash": str,     # sha256 of the file right now ('' on read error)
        }

    Intended for the dashboard's dev-mode footer: when ``drift`` is True,
    tell the operator to hit Reload before MCP tool schemas go out of sync
    with the source of truth on disk.
    """
    current = _hash_source(_SOURCE_PATH)
    return {
        "drift": bool(current) and current != _SOURCE_HASH_AT_IMPORT,
        "source_path": str(_SOURCE_PATH),
        "startup_hash": _SOURCE_HASH_AT_IMPORT,
        "current_hash": current,
    }


# ---------------------------------------------------------------------------
# Tool definitions (MCP schema format)
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_check_messages",
        "description": (
            "Check the Swarm inbox for pending messages from other workers or the operator. "
            "Call this at three moments: (1) at the start of every task so you don't miss "
            "dependency warnings or operator hints, (2) after completing a task so downstream "
            "workers' replies don't stack up, and (3) whenever you encounter unexpected state "
            "(files changed under you, tests failing that passed last run) — another worker "
            "may have sent a 'warning' or 'finding' that explains it. Messages are marked read "
            "on retrieval, so don't call speculatively."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "examples": [{}],
        },
    },
    {
        "name": "swarm_send_message",
        "description": (
            "Send a direct message to another worker (or broadcast to '*'). Use this whenever "
            "you learn something that affects another worker's ability to do their job "
            "correctly. Message types:\n"
            "  - 'finding'    — a discovery that might be useful (schema shape, gotcha, pattern)\n"
            "  - 'warning'    — you are about to change something that will break their build\n"
            "  - 'dependency' — they need to do X before you can finish Y (blocks your task)\n"
            "  - 'status'     — routine progress update, not action-required\n"
            "Prefer direct messages over '*' broadcast — broadcast only for changes that "
            "truly affect every worker (e.g., a shared type signature changed)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": (
                        "Recipient worker name (e.g. 'hub', 'platform'), or '*' for "
                        "broadcast to all workers."
                    ),
                },
                "type": {
                    "type": "string",
                    "enum": ["finding", "warning", "dependency", "status"],
                    "description": "Message type — see tool description for semantics.",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "The message body. Be concrete: include file paths, function "
                        "names, and any action the recipient needs to take."
                    ),
                },
            },
            "required": ["to", "type", "content"],
            "examples": [
                {
                    "to": "platform",
                    "type": "warning",
                    "content": (
                        "Renamed ContactDto.emailAddress → ContactDto.email in hub "
                        "PR #321; please update your imports."
                    ),
                },
                {
                    "to": "*",
                    "type": "finding",
                    "content": (
                        "The /api/v1/contacts endpoint now requires X-Tenant-Id "
                        "header as of platform commit abc123."
                    ),
                },
            ],
        },
    },
    {
        "name": "swarm_report_blocker",
        "description": (
            "Declare that one of your in-progress tasks is blocked on another task and "
            "should not trigger idle-watcher nudges until the blocker clears. Call this "
            "when you have nothing to do autonomously on a ticket — e.g. 'scaffolded 60 "
            "percent, cannot proceed further until platform #245 ships the backend "
            "field'. The idle-watcher drone will skip nudges for you on that task "
            "until either (a) ``blocked_by_task`` flips to completed, or (b) a new "
            "message lands in your inbox. Re-call with the same ``task_number`` "
            "anytime to refresh the reason or reset the message-since window. You "
            "can also clear a blocker early by completing the blocked task normally."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_number": {
                    "type": "integer",
                    "description": "The display number of YOUR in-progress task that is blocked.",
                },
                "blocked_by_task": {
                    "type": "integer",
                    "description": (
                        "The display number of the task whose completion would unblock "
                        "you. The watcher auto-clears this blocker when that task's "
                        "status flips to completed."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Short human-readable explanation of the blocker so the "
                        "operator and Queen can audit it. 1-2 sentences."
                    ),
                },
            },
            "required": ["task_number", "blocked_by_task"],
            "examples": [
                {
                    "task_number": 246,
                    "blocked_by_task": 245,
                    "reason": "scaffolded UI; needs platform #245 backend field to ship",
                },
            ],
        },
    },
    {
        "name": "swarm_park_task",
        "description": (
            "Hand your OWN in-progress task back to ASSIGNED with a reason — "
            "an intentional set-down, NOT a blocker. Call this the moment you "
            "stop actively working a task you still own: an operator preempt, "
            "a scope change, or you're switching to something urgent and want "
            "the board to immediately tell the truth (no daemon reload, no "
            "fabricated blocker). The task stays yours (still ASSIGNED to "
            "you) so you can resume it later. Different from "
            "``swarm_report_blocker`` (which means 'I'm waiting on an "
            "upstream task') and from ``swarm_complete_task`` (which means "
            "'done'). Pass ``task_number`` to say exactly which of your "
            "active tasks to set down; if you own only one active task you "
            "may omit it. If you own more than one and omit ``task_number`` "
            "the tool REFUSES and lists them rather than guessing — never "
            "silently parks an arbitrary task."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "Why you're setting it down (operator preempt, scope "
                        "change, pivot). 1 sentence; recorded to history + buzz."
                    ),
                },
                "task_number": {
                    "type": "integer",
                    "description": (
                        "Which of YOUR active tasks to park (its display "
                        "number). Optional only when you own exactly one "
                        "active task; required to disambiguate when you own "
                        "several. Must be owned by you and ACTIVE."
                    ),
                },
            },
            "required": ["reason"],
            "examples": [
                {"reason": "operator preempt — pivoting to urgent #405", "task_number": 401},
                {"reason": "scope changed; re-planning before continuing"},
            ],
        },
    },
    {
        "name": "swarm_note_to_queen",
        "description": (
            "Send a lightweight side-channel note to the Queen. Use this when you have "
            "a coordination-question, a pre-response reminder, or an 'FYI' directed at "
            "the Queen that doesn't rise to a formal 'finding' / 'warning' / 'dependency' "
            "message — short things like 'should I /clear before this next run?' or "
            "'FYI queen, I'm about to branch off X'. Every note is persisted in the "
            "inter-worker message log AND auto-relayed into the Queen's PTY (same path "
            "as ``swarm_send_message(to='queen', ...)``), so her next turn sees it "
            "naturally. Workers MAY NOT use this to prompt each other — the elevated "
            "relay channel is Queen-only. Self-notes (queen → queen) are a no-op."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "The note body. Keep it short — 1-3 sentences is ideal. For "
                        "longer structured memos use ``swarm_send_message(to='queen', "
                        "type='finding'|'status')`` instead."
                    ),
                },
            },
            "required": ["content"],
            "examples": [
                {"content": "Should I /clear before the 8-task dispatch run?"},
                {"content": "FYI queen: I'm branching off to investigate #247 first."},
            ],
        },
    },
    {
        "name": "swarm_draft_email",
        "description": (
            "Create an email draft in the operator's Outlook Drafts folder via "
            "the Microsoft Graph integration. The draft is NEVER sent automatically "
            "— it lands in Drafts where the operator reviews and sends manually. "
            "Use this when you need the operator to reach out to someone (e.g. "
            "ask a stakeholder for clarification on an email-sourced task, draft "
            "a status update, compose a new outreach). For replies to existing "
            "email-sourced tasks, use ``swarm_complete_task`` with a resolution — "
            "that auto-drafts a reply in-thread. Requires the Graph integration "
            "to be configured (same config the existing email-task flow uses). "
            "Every draft creation is logged as a ``DRAFT_OK`` buzz entry for "
            "audit. Returns the draft's Graph ID + web link so the operator "
            "can find it quickly."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Recipient email address(es). Must be a non-empty list. "
                        "Each entry is a bare address like ``alice@example.com``."
                    ),
                },
                "subject": {
                    "type": "string",
                    "description": "Subject line for the draft.",
                },
                "body": {
                    "type": "string",
                    "description": (
                        "Email body. Plain text by default; set ``body_type='html'`` "
                        "for HTML content (e.g. links, formatting)."
                    ),
                },
                "cc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional CC recipients, same format as ``to``.",
                },
                "body_type": {
                    "type": "string",
                    "enum": ["text", "html"],
                    "description": (
                        "Body format. Default ``text``. Use ``html`` only when you "
                        "need formatting the operator will see in Outlook."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Short human-readable audit note — why are you drafting this? "
                        "Surfaces in the buzz log alongside the draft ID."
                    ),
                },
            },
            "required": ["to", "subject", "body"],
            "examples": [
                {
                    "to": ["ops@example.com"],
                    "subject": "Request for schema clarification — project v6",
                    "body": (
                        "Hi team,\n\nCould you confirm whether the new "
                        "`visibility` field replaces the existing `is_published` "
                        "flag or supplements it?\n\nThanks,\n"
                        "Swarm (drafted on behalf of operator)"
                    ),
                    "reason": "task #301 needs schema decision before implementation",
                },
            ],
        },
    },
    {
        "name": "swarm_task_status",
        "description": (
            "Query the Swarm task board. Call this when you need to see what work is queued, "
            "who owns what, or to check whether a task you created has been picked up yet. "
            "Use filter='mine' to list only your own tasks, 'unassigned' to find queen-eligible "
            "work, 'assigned' for anything with an owner, or omit filter for everything. "
            "Open tasks (backlog/unassigned/assigned/active) come first, newest-by-number first; "
            "done/failed tasks sort after, most-recently-completed first. Results are "
            "capped at ``limit`` (default 50, max 500); when output is truncated a summary "
            "footer names the total. For ``filter='mine'``, completed history is suppressed "
            "unless ``include_completed`` is true — the default surfaces your actionable work "
            "rather than bury it behind old closeouts. Pass ``number`` to look up a single task "
            "by its display number (bypasses all other filters)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "enum": ["all", "backlog", "unassigned", "assigned", "active", "mine"],
                    "description": "Which tasks to return (default: 'all').",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "Maximum rows to return (default 50, max 500).",
                },
                "include_completed": {
                    "type": "boolean",
                    "description": (
                        "Include completed/failed tasks when filter='mine'. "
                        "Default false (open tasks only). Ignored for other filters."
                    ),
                },
                "number": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Look up a single task by its display number "
                        "(e.g. 142). Overrides filter/limit."
                    ),
                },
            },
            "examples": [
                {"filter": "mine"},
                {"filter": "mine", "include_completed": True},
                {"filter": "unassigned", "limit": 100},
                {"number": 142},
                {},
            ],
        },
    },
    {
        "name": "swarm_claim_file",
        "description": (
            "Place an advisory lock on a file before editing it, so other workers can see "
            "the claim and avoid concurrent edits. Call this right before you start editing "
            "any shared file — config files (package.json, pyproject.toml), shared utilities, "
            "API contracts, shared types. Claims auto-expire so you don't need to release; "
            "a fresh claim renews the timer. Path MUST be absolute (the daemon will reject "
            "relative paths). If another worker holds the claim, the tool returns an error "
            "naming them — ask them via swarm_send_message rather than forcing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute filesystem path to claim. Use realpath output — "
                        "symlinks are resolved server-side."
                    ),
                },
            },
            "required": ["path"],
            "examples": [
                {"path": "/home/user/projects/repo/src/shared/types.ts"},
                {"path": "/home/user/projects/repo/pyproject.toml"},
            ],
        },
    },
    {
        "name": "swarm_complete_task",
        "description": (
            "Mark one of your assigned tasks as completed. Call this only after you have "
            "verified your work (tests pass, /check clean, feature demonstrably works). The "
            "resolution is stored as task learnings and shown to future workers picking up "
            "similar tasks — write it for *them*, not for a manager. A good resolution names "
            "the root cause (for bugs), the files you touched, and any followup work you "
            "spotted but didn't do. When you have exactly one active assignment, ``number`` "
            "can be omitted. When you have multiple active assignments, pass ``number`` "
            "explicitly — the tool refuses to guess which task you mean, because silent "
            "guessing is how resolutions get attached to the wrong record. Fails if you "
            "have no active task or the specified number isn't assigned to you."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "resolution": {
                    "type": "string",
                    "description": (
                        "What was done. Name files touched, root cause for bugs, "
                        "and any followup worth flagging."
                    ),
                },
                "number": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Display number of the task you are closing (e.g. 169). "
                        "Required when you have more than one active assignment. "
                        "Optional when you have exactly one."
                    ),
                },
            },
            "required": ["resolution"],
            "examples": [
                {
                    "resolution": (
                        "Fixed null pointer in ContactService.resolveTenant "
                        "(src/services/contact.ts:142) — missing guard for anonymous "
                        "sessions. Added regression test. Followup: refactor tenant "
                        "resolution out of service constructor (noted but not done)."
                    ),
                },
                {
                    "number": 169,
                    "resolution": (
                        "Added disambiguation to swarm_complete_task (src/swarm/mcp/tools.py). "
                        "Workers with multiple in_progress tasks must now pass ``number``."
                    ),
                },
            ],
        },
    },
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
    {
        "name": "swarm_get_learnings",
        "description": (
            "Search learnings captured from previously-completed tasks. Call this when you "
            "start a task that sounds similar to something already done, or when you hit "
            "an unfamiliar error — another worker may have documented the fix. Results are "
            "capped at 5, so pass a specific query (function name, error message, file path) "
            "rather than a broad topic. If you find relevant learnings, cite them in your "
            "own resolution so the knowledge compounds."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Substring to filter learnings (case-insensitive). Omit "
                        "to return all (capped at 5)."
                    ),
                },
            },
            "examples": [
                {"query": "tenant resolution"},
                {"query": "MailParser"},
                {},
            ],
        },
    },
    {
        "name": "swarm_get_playbooks",
        "description": (
            "Recall reusable PLAYBOOKS — generalizable procedures synthesized "
            "from previously-successful tasks (distinct from learnings, which "
            "are operator corrections). Call this at the start of a task that "
            "resembles work the swarm has done before: a matching playbook "
            "gives you vetted steps + known pitfalls so you don't re-derive "
            "the approach. Pass a specific query (the task's goal, an error, a "
            "subsystem). Only active playbooks are returned. If one applies, "
            "follow it and note it in your resolution."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What you're about to do (goal / error / subsystem). "
                        "Omit to list recent active playbooks."
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Optional exact scope filter: 'global', "
                        "'project:<repo>', or 'worker:<name>'."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max playbooks to return (default 5, max 20).",
                },
            },
            "examples": [
                {"query": "flaky pytest under load"},
                {"query": "add retry to an outbound sender", "scope": "global"},
                {},
            ],
        },
    },
    {
        "name": "swarm_batch",
        "description": (
            "Execute multiple swarm_* calls in a single round-trip. Use this "
            "when you're about to make two or more back-to-back swarm calls — "
            "e.g. claim_file + send_message + complete_task after a fix — so "
            "you pay one JSON-RPC round-trip instead of N. Ops execute "
            "sequentially in the order given (message ordering matters) and "
            "the combined result lists each op's outcome. Pass "
            "``fail_fast: true`` (default) to stop at the first error, or "
            "``fail_fast: false`` to continue and report each error inline. "
            "Cannot contain nested swarm_batch calls."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ops": {
                    "type": "array",
                    "description": (
                        "Ordered list of ``{tool, args}`` pairs to execute. "
                        "``tool`` must name one of the other swarm_* tools."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string"},
                            "args": {"type": "object"},
                        },
                        "required": ["tool"],
                    },
                },
                "fail_fast": {
                    "type": "boolean",
                    "description": (
                        "If true (default), abort the batch on the first error. "
                        "If false, run every op and surface each error inline."
                    ),
                },
            },
            "required": ["ops"],
            "examples": [
                {
                    "ops": [
                        {
                            "tool": "swarm_claim_file",
                            "args": {"path": "/home/user/repo/src/shared.ts"},
                        },
                        {
                            "tool": "swarm_send_message",
                            "args": {
                                "to": "platform",
                                "type": "warning",
                                "content": "About to edit shared.ts",
                            },
                        },
                        {
                            "tool": "swarm_complete_task",
                            "args": {"resolution": "Edited shared.ts"},
                        },
                    ]
                },
                {
                    "ops": [
                        {"tool": "swarm_check_messages", "args": {}},
                        {"tool": "swarm_task_status", "args": {"filter": "mine"}},
                    ],
                    "fail_fast": False,
                },
            ],
        },
    },
    {
        "name": "swarm_report_progress",
        "description": (
            "Report structured progress on your current task. The operator sees these in the "
            "dashboard and uses them to decide when to intervene. Call this at meaningful "
            "milestones — finished reading, starting implementation, test passing, hit a "
            "blocker — not on every trivial step. If you're blocked (waiting on another "
            "worker, missing credentials, flaky test), set blockers to the specific thing "
            "that would unblock you so the operator can act."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "phase": {
                    "type": "string",
                    "description": (
                        "Current phase label. Conventional values: 'reading', "
                        "'planning', 'implementing', 'testing', 'debugging', "
                        "'shipping'."
                    ),
                },
                "pct": {
                    "type": "number",
                    "description": (
                        "Estimated completion percentage 0-100. Be honest — "
                        "overestimates frustrate the operator."
                    ),
                },
                "blockers": {
                    "type": "string",
                    "description": "Specific blocker, if any. Empty string when making progress.",
                },
            },
            "examples": [
                {"phase": "implementing", "pct": 40},
                {
                    "phase": "debugging",
                    "pct": 60,
                    "blockers": "Waiting on platform worker to deploy schema change from PR #87.",
                },
                {"phase": "shipping", "pct": 95},
            ],
        },
    },
]

# Tool name → handler function mapping
_TOOL_NAMES = {t["name"] for t in TOOLS}


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def handle_tool_call(
    daemon: SwarmDaemon,
    worker_name: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> list[dict[str, Any]] | dict[str, Any]:
    """Dispatch a tool call and return MCP content blocks.

    Phase 3 (2026-05-08): handlers may now return either the legacy
    bare ``list[dict]`` content array OR a dict wrapper with
    ``content`` + optional ``structuredContent``/``_meta`` keys.
    Claude Code 2.1.x prefers ``structuredContent`` when present
    (verified in the leaked source at ``services/mcp/client.ts:2662``)
    so view-side tools can deliver typed JSON the model can query
    directly without re-parsing markdown summaries.

    The dispatcher passes either shape through verbatim. The MCP
    server-side ``_handle_tools_call`` strips the wrapper before
    serializing into the JSON-RPC envelope.
    """
    from swarm.drones.log import LogCategory, SystemAction

    handler = _HANDLERS.get(tool_name)
    if not handler:
        return [{"type": "text", "text": f"Unknown tool: {tool_name}"}]
    try:
        result = handler(daemon, worker_name, arguments)
    except Exception as e:
        return [{"type": "text", "text": f"Error: {e}"}]

    # Extract the operator-facing detail line for the buzz log audit
    # entry, regardless of which return shape the handler used.
    if isinstance(result, dict):
        content = result.get("content") or []
    else:
        content = result
    short_name = tool_name.removeprefix("swarm_")
    detail = content[0].get("text", "")[:120] if content else ""
    daemon.drone_log.add(
        SystemAction.OPERATOR,
        worker_name,
        f"mcp:{short_name} → {detail}",
        category=LogCategory.WORKER,
    )
    return result


def _handle_check_messages(
    d: SwarmDaemon, worker_name: str, _args: dict[str, Any]
) -> list[dict[str, Any]]:
    messages = d.message_store.get_unread(worker_name)
    if not messages:
        return [{"type": "text", "text": "No pending messages."}]
    # Mark as read
    d.message_store.mark_read(worker_name, [m.id for m in messages])
    lines = []
    for m in messages:
        lines.append(f"[{m.msg_type}] from {m.sender}: {m.content}")
    return [{"type": "text", "text": "\n".join(lines)}]


def _handle_send_message(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    recipient = args.get("to", "")
    msg_type = args.get("type", "finding")
    content = args.get("content", "")
    if not recipient or not content:
        return [{"type": "text", "text": "Missing 'to' or 'content'"}]
    from swarm.drones.log import LogCategory, SystemAction
    from swarm.worker.worker import QUEEN_WORKER_NAME

    # Wildcard = broadcast to every *registered* worker (minus the sender).
    # send(..., "*", ...) would write a single row whose read_at column
    # belongs to whichever worker called get_unread() first — so the
    # broadcast "won" by the first reader and nobody else saw it.
    #
    # The roster is sourced from ``d.config.workers`` (the configured
    # roster), NOT ``d.workers`` (the currently-running PTYs). Messages
    # persist in SQLite, so workers that aren't running at send time
    # still pick up the broadcast when they start and call get_unread().
    # Iterating live processes only would silently skip offline workers —
    # the original bug users reported as "broadcast returned success but
    # never arrived."
    if recipient == "*":
        configured = getattr(getattr(d, "config", None), "workers", None) or []
        roster_names: list[str] = []
        seen: set[str] = set()
        for w in configured:
            name = getattr(w, "name", None)
            if not name or name == worker_name or name in seen:
                continue
            seen.add(name)
            roster_names.append(name)
        ids = d.message_store.broadcast(worker_name, roster_names, msg_type, content)
        d.drone_log.add(
            SystemAction.OPERATOR,
            worker_name,
            f"→ * ({len(ids)} recipient(s)): {content[:80]}",
            category=LogCategory.MESSAGE,
        )
        if not ids:
            return [{"type": "text", "text": "No other workers registered to receive broadcast."}]
        # Broadcast reached the Queen if she's in the configured roster.
        if QUEEN_WORKER_NAME in roster_names and worker_name != QUEEN_WORKER_NAME:
            # broadcast() preserves ``recipients`` order for successful sends.
            # Our pre-filtered roster already drops empties + the sender, so
            # in the happy path ``ids`` and ``roster_names`` align 1:1. Only
            # a mid-broadcast sqlite failure (send returns None) would shorten
            # ids; in that edge case skip mark-read rather than mis-target
            # another worker's row.
            queen_msg_id: int | None = None
            if len(ids) == len(roster_names):
                queen_msg_id = ids[roster_names.index(QUEEN_WORKER_NAME)]
            _auto_relay_to_queen(d, worker_name, msg_type, content, message_id=queen_msg_id)
        recipients_list = ", ".join(sorted(roster_names))
        return [
            {
                "type": "text",
                "text": f"Broadcast sent to {len(ids)} worker(s): {recipients_list}.",
            }
        ]

    msg_id = d.message_store.send(worker_name, recipient, msg_type, content)
    if msg_id:
        d.drone_log.add(
            SystemAction.OPERATOR,
            worker_name,
            f"→ {recipient}: {content[:80]}",
            category=LogCategory.MESSAGE,
        )
        # Task #235 Phase 1: when a worker sends to the Queen, inject a
        # short relay notification into the Queen's PTY so her next turn
        # processes the reply naturally — same ergonomic as #225's task
        # auto-dispatch. Skipped when the Queen messages herself
        # (self-loop guard) and when a worker messages another worker
        # (workers deliberately can't auto-interrupt each other — that
        # bypass is Queen-only).
        if recipient == QUEEN_WORKER_NAME and worker_name != QUEEN_WORKER_NAME:
            _auto_relay_to_queen(d, worker_name, msg_type, content, message_id=msg_id)
        return [{"type": "text", "text": f"Message sent to {recipient}."}]
    return [{"type": "text", "text": "Failed to send message."}]


def _handle_report_blocker(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    """Persist a worker-reported blocker so the IdleWatcher can skip it.

    Task #250: workers nudged by the idle-watcher while waiting on a
    peer's dependency burned tokens replying "still blocked" every
    3 minutes. This tool gives them a first-class way to say "don't
    ping me about this one until #X completes or my inbox changes".
    """
    task_number = args.get("task_number")
    blocked_by = args.get("blocked_by_task")
    reason = (args.get("reason") or "").strip()
    if task_number is None or blocked_by is None:
        return [
            {
                "type": "text",
                "text": "Missing 'task_number' or 'blocked_by_task'.",
            }
        ]
    try:
        task_number = int(task_number)
        blocked_by = int(blocked_by)
    except (TypeError, ValueError):
        return [{"type": "text", "text": "'task_number' and 'blocked_by_task' must be integers."}]

    store = getattr(d, "blocker_store", None)
    if store is None:
        return [{"type": "text", "text": "Blocker store unavailable on this daemon."}]
    try:
        store.report(worker_name, task_number, blocked_by, reason=reason)
    except Exception as exc:  # defensive — DB errors shouldn't crash the handler
        return [{"type": "text", "text": f"Failed to record blocker: {exc}"}]

    from swarm.drones.log import LogCategory, SystemAction

    detail = f"#{task_number} blocked by #{blocked_by}"
    if reason:
        detail = f"{detail} — {reason[:120]}"
    d.drone_log.add(
        SystemAction.OPERATOR,
        worker_name,
        detail,
        category=LogCategory.WORKER,
    )
    return [
        {
            "type": "text",
            "text": (
                f"Blocker recorded: #{task_number} blocked by #{blocked_by}. "
                "IdleWatcher will skip nudges for this task until the blocker clears."
            ),
        }
    ]


_DraftEmailFields = tuple[list[str], str, str, list[str] | None, str, str]


def _validate_draft_email_args(args: dict[str, Any]) -> _DraftEmailFields | str:
    """Validate + coerce swarm_draft_email inputs. Returns tuple on success,
    or a short error string on failure."""
    to_raw = args.get("to")
    subject = (args.get("subject") or "").strip()
    body = args.get("body") or ""
    cc_raw = args.get("cc") or []
    body_type = (args.get("body_type") or "text").strip().lower()
    reason = (args.get("reason") or "").strip()

    if not isinstance(to_raw, list) or not to_raw:
        return "Missing 'to' — must be a non-empty list of addresses."
    if not all(isinstance(a, str) and a.strip() for a in to_raw):
        return "'to' entries must be non-empty strings."
    if not subject:
        return "Missing 'subject'."
    if not body:
        return "Missing 'body'."
    if body_type not in ("text", "html"):
        return "'body_type' must be 'text' or 'html'."
    if cc_raw and not (
        isinstance(cc_raw, list) and all(isinstance(a, str) and a.strip() for a in cc_raw)
    ):
        return "'cc' must be a list of non-empty strings."

    to_list = [a.strip() for a in to_raw]
    cc_list = [a.strip() for a in cc_raw] if cc_raw else None
    return to_list, subject, body, cc_list, body_type, reason


def _handle_draft_email(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    """Create a draft email in the operator's Outlook Drafts via Graph.

    Mirrors the existing email-reply flow that fires when an email-sourced
    task is completed, but lets workers initiate new drafts on demand.
    The draft is NEVER sent — it lands in the operator's Drafts folder
    where they review + send manually.

    Fire-and-forget: the MCP dispatch surface is sync, so this validates
    + schedules the Graph call as a background task and returns
    immediately.  Success / failure gets written to the buzz log
    (``DRAFT_OK`` / ``DRAFT_FAILED``) so the dashboard surfaces the
    outcome for the operator.  Workers see "draft queued" and can
    verify the result in Outlook or the dashboard.
    """
    from swarm.drones.log import LogCategory, SystemAction

    validated = _validate_draft_email_args(args)
    if isinstance(validated, str):
        return [{"type": "text", "text": validated}]
    to_list, subject, body, cc_list, body_type, reason = validated

    graph_mgr = getattr(d, "graph_mgr", None)
    if graph_mgr is None or not graph_mgr.is_connected():
        return [
            {
                "type": "text",
                "text": (
                    "Microsoft Graph integration is not connected. Operator "
                    "needs to complete the Graph OAuth flow from the config "
                    "page before workers can draft email."
                ),
            }
        ]

    async def _create_and_log() -> None:
        result = await graph_mgr.create_draft(
            to_list, subject, body, cc=cc_list, body_type=body_type
        )
        if result is None:
            d.drone_log.add(
                SystemAction.DRAFT_FAILED,
                worker_name,
                f"draft email failed — to={to_list[0]} subj='{subject[:60]}'",
                category=LogCategory.SYSTEM,
                is_notification=True,
            )
            return
        audit_detail = f"draft email to {to_list[0]}: {subject[:80]}"
        if reason:
            audit_detail = f"{audit_detail} — {reason[:120]}"
        web_link = result.get("web_link", "")
        if web_link:
            audit_detail = f"{audit_detail} [outlook: {web_link[:80]}]"
        d.drone_log.add(
            SystemAction.DRAFT_OK,
            worker_name,
            audit_detail,
            category=LogCategory.SYSTEM,
        )

    import asyncio

    try:
        loop = asyncio.get_running_loop()
        bg = loop.create_task(_create_and_log())
        bg.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    except RuntimeError:
        # No running event loop (unit test / CLI context) — run synchronously
        # via asyncio.run so the caller still sees the log entries land.
        try:
            asyncio.run(_create_and_log())
        except Exception:
            # Failures surface via DRAFT_FAILED buzz entry from the
            # coroutine itself; swallow here so a transient Graph error
            # doesn't take down the whole MCP response.
            pass

    return [
        {
            "type": "text",
            "text": (
                f"Draft queued for the operator's Outlook Drafts folder "
                f"(to={to_list[0]}). The draft will NOT be sent — operator "
                f"reviews + sends manually. Check the dashboard buzz log "
                f"for DRAFT_OK / DRAFT_FAILED confirmation in a few seconds."
            ),
        }
    ]


def _handle_park_task(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    """#406/#407: park one of the caller's OWN ACTIVE tasks back to ASSIGNED.

    Only ever touches *this caller's* own tasks, so cross-worker parking
    is impossible by construction. Not a blocker — no binding is created.
    Composes with #405: the worker has no ACTIVE task right after, so the
    board is truthful immediately (no reload/reconciler).

    #407: #406 shipped with NO task argument — it parked "the" active
    task. When a worker owns >1 ACTIVE task (legal pre-#405-reload /
    un-reconciled state) that silently set down an arbitrary one (the
    2026-05-17 public-website wrong-task footgun). Now: an explicit
    ``task_number`` parks exactly that task (must be owned + ACTIVE);
    omitted parks the sole ACTIVE task iff there is exactly one; omitted
    with >1 candidate REFUSES and lists them — never a silent guess, no
    mutation on the refusal/rejection paths.
    """
    reason = str(args.get("reason") or "").strip()
    if not reason:
        return [{"type": "text", "text": "Missing 'reason' — say why you're setting it down."}]
    board = getattr(d, "task_board", None)
    if board is None:
        return [{"type": "text", "text": "Task board unavailable on this daemon."}]

    parkable = board.parkable_tasks_for_worker(worker_name)
    raw_num = args.get("task_number")

    if raw_num is not None and str(raw_num).strip() != "":
        try:
            want = int(raw_num)
        except (TypeError, ValueError):
            return [
                {
                    "type": "text",
                    "text": (
                        f"'task_number' must be a task number, got {raw_num!r}. Nothing parked."
                    ),
                }
            ]
        target = next((t for t in board.tasks_for_worker(worker_name) if t.number == want), None)
        if target is None:
            return [
                {
                    "type": "text",
                    "text": (
                        f"Task #{want} is not assigned to you (or doesn't exist) — "
                        f"you can only park your own task. Nothing changed."
                    ),
                }
            ]
        if target.id not in {t.id for t in parkable}:
            return [
                {
                    "type": "text",
                    "text": (
                        f"Task #{want} is {target.status.value}, not ACTIVE — only an "
                        f"active task can be parked. Nothing changed."
                    ),
                }
            ]
        task = target
    else:
        if not parkable:
            return [{"type": "text", "text": f"No active task to park for '{worker_name}'."}]
        if len(parkable) > 1:
            nums = ", ".join(f"#{t.number}" for t in sorted(parkable, key=lambda t: t.number))
            return [
                {
                    "type": "text",
                    "text": (
                        f"Ambiguous — you own {len(parkable)} active tasks ({nums}). "
                        f"swarm_park_task won't guess which to set down. Re-call it "
                        f"with task_number=<n>. Nothing changed."
                    ),
                }
            ]
        task = parkable[0]

    if not board.park(task.id, worker_name, reason):
        return [{"type": "text", "text": f"Could not park #{task.number} (state changed?)."}]

    from swarm.drones.log import LogCategory, SystemAction
    from swarm.tasks.history import TaskAction

    detail = f"#{task.number} parked: {reason[:120]}"
    try:
        d.drone_log.add(SystemAction.TASK_PARKED, worker_name, detail, category=LogCategory.TASK)
        if getattr(d, "task_history", None) is not None:
            d.task_history.append(
                task.id, TaskAction.UNASSIGNED, actor=worker_name, detail=f"parked: {reason}"
            )
    except Exception:
        pass  # audit best-effort — the transition already succeeded
    return [
        {
            "type": "text",
            "text": (
                f"Parked #{task.number} → ASSIGNED (still yours). Board is "
                f"truthful now — no reload needed. Resume it anytime."
            ),
        }
    ]


def _handle_note_to_queen(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    """Persist a side-channel note addressed to the Queen + auto-relay it.

    Task #248: workers often address the Queen via PTY text (pre-
    response reminders, inline coordination questions) that never goes
    through ``swarm_send_message``. This tool is a lightweight shortcut
    — the note is persisted with ``msg_type="note"`` (so it shows up
    alongside formal messages in ``queen_view_messages``) AND fires
    the same ``_auto_relay_to_queen`` path as #235 so the Queen's PTY
    sees it the same turn.
    """
    from swarm.drones.log import LogCategory, SystemAction
    from swarm.worker.worker import QUEEN_WORKER_NAME

    content = args.get("content", "")
    if not content:
        return [{"type": "text", "text": "Missing 'content'"}]

    if worker_name == QUEEN_WORKER_NAME:
        # Self-relay would pump the Queen's own PTY on every
        # note-to-self and potentially loop. No real use case.
        return [
            {
                "type": "text",
                "text": "No-op: queen cannot note-to-queen (self-loop guard).",
            }
        ]

    msg_id = d.message_store.send(worker_name, QUEEN_WORKER_NAME, "note", content)
    if not msg_id:
        return [{"type": "text", "text": "Failed to persist note."}]

    d.drone_log.add(
        SystemAction.OPERATOR,
        worker_name,
        f"→ queen (note): {content[:80]}",
        category=LogCategory.MESSAGE,
    )
    _auto_relay_to_queen(d, worker_name, "note", content, message_id=msg_id)
    return [{"type": "text", "text": "Note queued for the Queen."}]


def _auto_relay_to_queen(
    d: SwarmDaemon,
    sender: str,
    msg_type: str,
    content: str,
    message_id: int | None = None,
) -> None:
    """Fire-and-forget inject a short inbox relay into the Queen's PTY.

    Keeps the relay prompt small and action-oriented so Claude's next
    turn uses it as a cue to pull the full message via
    ``queen_view_messages``. Skipped silently when the daemon doesn't
    expose ``send_to_worker`` (test fakes) or when there's no running
    event loop.

    Task #277: when ``message_id`` is provided, the queen's inbox row is
    marked read at relay time. The Queen has no ``swarm_check_messages``
    equivalent — ``queen_view_messages`` is a read-only log view — so
    without this the dashboard unread count drifts from functional
    reality: Queen acts on the note, dashboard still shows it UNREAD
    indefinitely. The relay IS the consumption event, per Option A in
    the task write-up.
    """
    from swarm.drones.log import LogCategory, SystemAction
    from swarm.worker.worker import QUEEN_WORKER_NAME

    preview = (content or "")[:200].replace("\n", " ")
    suffix = "..." if len(content) > 200 else ""
    relay = (
        f"[msg to queen] {msg_type} from {sender}: {preview}{suffix}\n"
        "Full thread: `queen_view_messages worker=queen limit=5`"
    )

    send = getattr(d, "send_to_worker", None)
    if send is None:
        return
    try:
        import asyncio

        coro = send(QUEEN_WORKER_NAME, relay, _log_operator=False)
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(coro)
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        except RuntimeError:
            # No event loop (CLI/test context). Close the coroutine we
            # just created so Python doesn't warn about it.
            try:
                coro.close()
            except Exception:
                pass
    except Exception:
        return

    try:
        d.drone_log.add(
            SystemAction.INBOX_AUTO_RELAY,
            QUEEN_WORKER_NAME,
            f"from {sender}: {preview[:80]}{suffix}",
            category=LogCategory.MESSAGE,
        )
    except Exception:
        pass

    if message_id is not None:
        store = getattr(d, "message_store", None)
        mark_read = getattr(store, "mark_read", None) if store is not None else None
        if mark_read is not None:
            try:
                mark_read(QUEEN_WORKER_NAME, [message_id])
            except Exception:
                # mark_read failure shouldn't break the relay — the worst
                # outcome is the pre-#277 status quo (row stays UNREAD).
                pass

    # Command Center: surface this worker→queen message as an Attention card.
    # Reuses queen_threads/queen_messages so the dashboard renders it via the
    # existing queen.thread / queen.message WS events. One active thread per
    # sender → coalesces a sender's recent messages into one card.
    _upsert_attention_thread(d, sender, msg_type, content)


def _upsert_attention_thread(
    d: SwarmDaemon,
    sender: str,
    msg_type: str,
    content: str,
) -> None:
    chat = getattr(d, "queen_chat", None)
    if chat is None:
        return
    try:
        active = chat.list_threads(
            status="active", kind="worker-message", worker_name=sender, limit=1
        )
        if active:
            thread = active[0]
        else:
            title = f"{sender}: {(content or '').splitlines()[0][:80]}"
            thread = chat.create_thread(title=title, kind="worker-message", worker_name=sender)
        msg = chat.add_message(thread.id, role="system", content=f"[{msg_type}] {content}")
    except Exception:
        # Attention surfacing is best-effort — never break the PTY relay path.
        return

    try:
        from swarm.server.routes.queen import _broadcast_message, _broadcast_thread

        _broadcast_thread(d, thread.id, "created" if not active else "updated")
        _broadcast_message(d, thread.id, msg.to_dict())
    except Exception:
        pass


_TASK_STATUS_DEFAULT_LIMIT = 50
_TASK_STATUS_MAX_LIMIT = 500
_OPEN_STATUSES = {"backlog", "unassigned", "assigned", "active"}


def _format_task_line(t: Any) -> str:
    w = t.assigned_worker or "unassigned"
    return f"#{t.number} [{t.status.value}] {t.title} ({w})"


def _enum_value(v: Any) -> str:
    return v.value if hasattr(v, "value") else str(v)


def _format_task_meta_line(t: Any) -> str:
    parts = [f"worker={t.assigned_worker or 'unassigned'}"]
    if getattr(t, "priority", None):
        parts.append(f"priority={_enum_value(t.priority)}")
    if getattr(t, "task_type", None):
        parts.append(f"type={_enum_value(t.task_type)}")
    if getattr(t, "tags", None):
        parts.append(f"tags={','.join(t.tags)}")
    return "  " + " | ".join(parts)


def _format_cross_project_line(t: Any) -> str | None:
    if not getattr(t, "is_cross_project", False):
        return None
    parts: list[str] = []
    if getattr(t, "source_worker", None):
        parts.append(f"from={t.source_worker}")
    if getattr(t, "target_worker", None):
        parts.append(f"to={t.target_worker}")
    if getattr(t, "dependency_type", None):
        parts.append(f"dep_type={_enum_value(t.dependency_type)}")
    return ("  cross-project: " + " | ".join(parts)) if parts else None


def _format_section(label: str, items: list[Any], bullet: str = "  - ") -> list[str]:
    if not items:
        return []
    out = ["", f"{label}:"]
    out.extend(f"{bullet}{x}" for x in items)
    return out


def _format_task_detail(t: Any) -> str:
    """Multi-line view used for single-task lookups by number — gives the
    worker the full context (description, acceptance criteria, attachments,
    etc.) instead of just the title."""
    lines = [f"#{t.number} [{t.status.value}] {t.title}", _format_task_meta_line(t)]

    cross = _format_cross_project_line(t)
    if cross:
        lines.append(cross)

    deps = getattr(t, "depends_on", None) or []
    if deps:
        formatted_deps = [f"#{d}" if isinstance(d, int) else str(d) for d in deps]
        lines.append("  depends_on: " + ", ".join(formatted_deps))

    if getattr(t, "jira_key", None):
        lines.append(f"  jira: {t.jira_key}")

    desc = (getattr(t, "description", None) or "").strip()
    if desc:
        lines.extend(["", "Description:", desc])

    acceptance = getattr(t, "acceptance_criteria", None) or []
    refs = getattr(t, "context_refs", None) or []
    attachments = getattr(t, "attachments", None) or []
    lines.extend(_format_section("Acceptance criteria", acceptance))
    lines.extend(_format_section("Context refs", refs))
    lines.extend(_format_section("Attachments", attachments))

    if t.status.value == "done" and getattr(t, "resolution", None):
        lines.extend(["", "Resolution:", t.resolution])

    return "\n".join(lines)


def _sort_tasks_for_display(tasks: list[Any]) -> list[Any]:
    """Open tasks first (newest-by-number DESC), then completed/failed by
    completed_at DESC (falling back to number DESC). Older implementations
    sorted ASC and sliced the head, which hid newer assignments — see task
    #142."""

    def key(t: Any) -> tuple[int, float, int]:
        is_open = t.status.value in _OPEN_STATUSES
        # Primary: open first (0) vs closed (1).
        # Secondary: most recent first — completed_at for closed tasks,
        # or number for open ones (a proxy for recency without requiring
        # a db timestamp).
        recency = -(t.completed_at or 0.0) if not is_open else -float(t.number)
        return (0 if is_open else 1, recency, -t.number)

    return sorted(tasks, key=key)


def _lookup_task_by_number(d: SwarmDaemon, raw: Any) -> list[dict[str, Any]]:
    try:
        target = int(raw)
    except (TypeError, ValueError):
        return [{"type": "text", "text": f"Invalid 'number': {raw!r}"}]
    for t in d.task_board.all_tasks:
        if t.number == target:
            return [{"type": "text", "text": _format_task_detail(t)}]
    return [{"type": "text", "text": f"No task found with number #{target}."}]


def _coerce_limit(raw: Any) -> int | str:
    """Return a clamped integer limit or a user-facing error string."""
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return f"Invalid 'limit': {raw!r}"
    if limit < 1:
        return "'limit' must be >= 1."
    return min(limit, _TASK_STATUS_MAX_LIMIT)


def _apply_task_filter(
    tasks: list[Any], filt: str, worker_name: str, *, include_completed: bool
) -> list[Any]:
    if filt == "backlog":
        return [t for t in tasks if t.status.value == "backlog"]
    if filt == "unassigned":
        return [t for t in tasks if t.status.value == "unassigned"]
    if filt == "active":
        return [t for t in tasks if t.status.value == "active"]
    if filt == "assigned":
        return [t for t in tasks if t.assigned_worker is not None]
    if filt == "mine":
        mine = [t for t in tasks if t.assigned_worker == worker_name]
        # Default for 'mine' surfaces actionable work. Completed/failed rows
        # used to crowd out newer assignments from the old fixed 20-row
        # window (task #142). Opt back in with include_completed=True.
        if not include_completed:
            mine = [t for t in mine if t.status.value in _OPEN_STATUSES]
        return mine
    return tasks


def _handle_task_status(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[dict[str, Any]] | dict[str, Any]:
    if not d.task_board:
        return [{"type": "text", "text": "No task board available."}]

    # Single-task lookup by display number — bypasses filter/limit so a worker
    # that hears about task #142 from another channel can always pull it up.
    if (number := args.get("number")) is not None:
        return _lookup_task_by_number(d, number)

    limit = _coerce_limit(args.get("limit", _TASK_STATUS_DEFAULT_LIMIT))
    if isinstance(limit, str):
        return [{"type": "text", "text": limit}]

    tasks = _apply_task_filter(
        list(d.task_board.all_tasks),
        args.get("filter", "all"),
        worker_name,
        include_completed=bool(args.get("include_completed", False)),
    )
    total = len(tasks)
    shown = _sort_tasks_for_display(tasks)[:limit]
    if not shown:
        return [{"type": "text", "text": "No tasks found."}]

    lines = [_format_task_line(t) for t in shown]
    if total > len(shown):
        lines.append(
            f"\n… {total - len(shown)} more not shown "
            f"(total={total}, limit={limit}). "
            "Pass a higher 'limit' or a more specific 'filter'."
        )
    payload = [_task_to_payload(t) for t in shown]
    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "structuredContent": {
            "tasks": payload,
            "shown": len(payload),
            "total": total,
            "filter": args.get("filter", "all"),
            "limit": limit,
            "include_completed": bool(args.get("include_completed", False)),
        },
    }


def _task_to_payload(t: Any) -> dict[str, Any]:
    """Project a SwarmTask onto a JSON-friendly dict for structuredContent.

    Carries only the fields the model needs to reason about — title,
    status, assignment, type/priority, criteria, dependencies. Avoids
    leaking internal fields (raw timestamps beyond completed_at, cost
    accounting, verifier internals) that would bloat the payload
    without helping the Queen.
    """
    return {
        "number": t.number,
        "title": t.title,
        "status": t.status.value,
        "assigned_worker": t.assigned_worker or None,
        "priority": _enum_value(getattr(t, "priority", None)),
        "task_type": _enum_value(getattr(t, "task_type", None)),
        "tags": list(getattr(t, "tags", []) or []),
        "depends_on": list(getattr(t, "depends_on", []) or []),
        "acceptance_criteria": list(getattr(t, "acceptance_criteria", []) or []),
        "is_cross_project": bool(getattr(t, "is_cross_project", False)),
        "source_worker": getattr(t, "source_worker", "") or None,
        "target_worker": getattr(t, "target_worker", "") or None,
        "completed_at": getattr(t, "completed_at", None),
    }


def _handle_claim_file(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    import os
    import time

    path = args.get("path", "")
    if not path:
        return [{"type": "text", "text": "Missing 'path'"}]
    resolved = os.path.realpath(path)
    now = time.time()
    lock = d.file_locks.get(resolved)
    if lock:
        owner, ts = lock
        if owner != worker_name and (now - ts) < d._file_lock_ttl:
            return [{"type": "text", "text": f"File claimed by {owner}."}]
    d.file_locks[resolved] = (worker_name, now)
    return [{"type": "text", "text": f"File claimed: {path}"}]


_ACTIVE_STATUSES = ("assigned", "active")


def _handle_complete_task(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    resolution = args.get("resolution", "")
    if not d.task_board:
        return [{"type": "text", "text": "No task board."}]

    # Task #275: the server resolves worker identity from the MCP URL query
    # string on every request. When a session's `.mcp.json` lacks
    # `?worker=<name>` (common after editing .mcp.json live — Claude Code's
    # HTTP MCP transport keeps using the bootstrap URL), `worker_name` here
    # is `"unknown"`. Every ownership check below would fail with a message
    # that points at the wrong root cause ("not assigned to you", "no active
    # task"). Fail fast with the diagnostic so the caller fixes the URL
    # instead of chasing the assignment.
    if worker_name == "unknown":
        return [
            {
                "type": "text",
                "text": (
                    "Cannot identify calling worker (worker_name=unknown). "
                    "swarm_complete_task requires caller identity, which the "
                    "server reads from the MCP URL. Check that .mcp.json "
                    "includes `?worker=<name>` in the swarm MCP server URL. "
                    "If you just edited .mcp.json, restart Claude Code so the "
                    "MCP transport picks up the new URL."
                ),
            }
        ]

    requested = args.get("number")
    active = [
        t
        for t in d.task_board.all_tasks
        if t.assigned_worker == worker_name and t.status.value in _ACTIVE_STATUSES
    ]

    # Explicit lookup wins — validate ownership and status before closing.
    # Runs even when ``active`` is empty so the caller gets a targeted error
    # (e.g. "not assigned to you") instead of a generic "no active task".
    if requested is not None:
        try:
            target_num = int(requested)
        except (TypeError, ValueError):
            return [{"type": "text", "text": f"Invalid 'number': {requested!r}"}]
        match = next(
            (t for t in d.task_board.all_tasks if t.number == target_num),
            None,
        )
        if match is None:
            return [{"type": "text", "text": f"No task found with number #{target_num}."}]
        if match.assigned_worker != worker_name:
            owner = match.assigned_worker or "nobody"
            return [
                {
                    "type": "text",
                    "text": (
                        f"Task #{target_num} is not assigned to you (assigned_worker={owner})."
                    ),
                }
            ]
        if match.status.value not in _ACTIVE_STATUSES:
            return [
                {
                    "type": "text",
                    "text": (
                        f"Task #{target_num} is not in progress "
                        f"(status={match.status.value}) — nothing to complete."
                    ),
                }
            ]
        d.complete_task(match.id, actor=worker_name, resolution=resolution)
        return [{"type": "text", "text": f"Task #{target_num} completed."}]

    if not active:
        return [{"type": "text", "text": "No active task found."}]

    # Multiple active assignments and no ``number`` — refuse to guess. The
    # pre-#169 behaviour closed whichever task iteration happened to yield
    # first, attaching the resolution to the wrong record. Listing the
    # candidate numbers gives the worker everything it needs to retry.
    if len(active) > 1:
        numbers = ", ".join(f"#{t.number}" for t in sorted(active, key=lambda t: t.number))
        return [
            {
                "type": "text",
                "text": (
                    f"You have {len(active)} active tasks ({numbers}); pass "
                    f"'number' to specify which to complete."
                ),
            }
        ]

    task = active[0]
    d.complete_task(task.id, actor=worker_name, resolution=resolution)
    return [{"type": "text", "text": f"Task #{task.number} completed."}]


def _handle_create_task(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    title = args.get("title", "")
    if not title:
        return [{"type": "text", "text": "Missing 'title'"}]
    attachments = args.get("attachments") or None
    if attachments:
        from pathlib import Path

        validated: list[str] = []
        for p in attachments:
            rp = Path(p).resolve()
            if not rp.exists():
                return [{"type": "text", "text": f"Attachment not found: {p}"}]
            validated.append(str(rp))
        attachments = validated
    task = d.create_task(
        title=title,
        description=args.get("description", ""),
        attachments=attachments,
        actor=worker_name,
    )
    # Acceptance criteria flow through edit_task to keep create_task's
    # signature small. The field has lived on SwarmTask since v1 but
    # was unread until Phase 2 wired it into the verifier (2026-05-08).
    raw_criteria = args.get("acceptance_criteria")
    if isinstance(raw_criteria, list):
        cleaned = [str(c).strip() for c in raw_criteria if str(c).strip()]
        if cleaned:
            d.edit_task(task.id, acceptance_criteria=cleaned, actor=worker_name)
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
        import asyncio

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
        if should_dispatch:
            coro = d.assign_and_start_task(task.id, target, actor=worker_name)
        else:
            coro = d.assign_task(task.id, target, actor=worker_name)
        try:
            loop = asyncio.get_running_loop()
            _task = loop.create_task(coro)
            _task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        except RuntimeError:
            # No running event loop (test/CLI context): close the coroutine
            # we created above so Python doesn't emit "coroutine was never
            # awaited" and fall back to the synchronous board-level assign.
            try:
                coro.close()
            except Exception:
                pass
            d.task_board.assign(task.id, target)
    return [{"type": "text", "text": f"Task created: #{task.number} {title}"}]


def _handle_get_learnings(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    if not d.task_board:
        return [{"type": "text", "text": "No task board."}]
    query = args.get("query", "").lower()
    results = []
    for t in d.task_board.all_tasks:
        if not t.learnings:
            continue
        if query and query not in t.title.lower() and query not in t.learnings.lower():
            continue
        results.append(f"Task #{t.number} ({t.title}):\n{t.learnings}")
    if not results:
        return [{"type": "text", "text": "No learnings found."}]
    return [{"type": "text", "text": "\n---\n".join(results[:5])}]


def _handle_get_playbooks(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    from swarm.playbooks.models import PlaybookStatus

    store = getattr(d, "playbook_store", None)
    if store is None:
        return [{"type": "text", "text": "No playbook store."}]
    query = str(args.get("query", "")).strip()
    scope = str(args.get("scope", "")).strip() or None
    try:
        limit = min(int(args.get("limit", 5)), 20)
    except (TypeError, ValueError):
        limit = 5
    if query:
        hits = store.search(query, scope=scope, status=PlaybookStatus.ACTIVE, limit=limit)
    else:
        hits = store.list(scope=scope, status=PlaybookStatus.ACTIVE, limit=limit)
    if not hits:
        return [{"type": "text", "text": "No matching playbooks."}]
    blocks = []
    for pb in hits:
        blocks.append(
            f"## {pb.title or pb.name}  [{pb.scope}]\n"
            f"Trigger: {pb.trigger}\n"
            f"(uses={pb.uses} winrate={pb.winrate:.0%} conf={pb.confidence:.2f})\n\n"
            f"{pb.body}"
        )
    return [{"type": "text", "text": "\n\n---\n\n".join(blocks)}]


def _handle_report_progress(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[dict[str, Any]]:
    phase = args.get("phase", "")
    pct = args.get("pct", -1)
    blockers = args.get("blockers", "")
    parts = []
    if phase:
        parts.append(f"phase={phase}")
    if pct >= 0:
        parts.append(f"{pct}%")
    if blockers:
        parts.append(f"blockers: {blockers}")
    summary = ", ".join(parts) if parts else "progress update"
    from swarm.drones.log import LogCategory, SystemAction

    d.drone_log.add(
        SystemAction.OPERATOR,
        worker_name,
        f"progress: {summary}",
        category=LogCategory.WORKER,
    )
    d.broadcast_ws(
        {
            "type": "worker_progress",
            "worker": worker_name,
            "phase": phase,
            "pct": pct,
            "blockers": blockers,
        }
    )
    return [{"type": "text", "text": "Progress reported."}]


def _validate_batch_op(op: Any) -> tuple[str, dict[str, Any], str]:
    """Validate a single batch op. Returns ``(tool, args, error)``.

    ``error`` is empty when the op is valid. Otherwise it explains why
    the op cannot run; tool/args are still returned so callers can log
    them in the failure line.
    """
    if not isinstance(op, dict):
        return "", {}, "invalid op: not an object"
    tool = op.get("tool", "")
    if tool == "swarm_batch":
        return tool, {}, "nested swarm_batch is not allowed"
    if tool not in _TOOL_NAMES:
        return tool, {}, "unknown tool"
    op_args = op.get("args") or {}
    if not isinstance(op_args, dict):
        return tool, {}, "'args' must be an object"
    return tool, op_args, ""


def _handle_batch(d: SwarmDaemon, worker_name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    """Execute a sequence of swarm_* ops in one MCP round-trip.

    Workers that need claim_file + send_message + complete_task today
    pay three JSON-RPC round-trips. ``swarm_batch`` lets them send one
    request. Each op is still logged individually via
    ``handle_tool_call`` so the dashboard shows the real activity, not
    a single opaque "batch" entry.
    """
    if "ops" not in args:
        return [{"type": "text", "text": "Missing 'ops' — provide a non-empty array of ops."}]
    ops = args.get("ops") or []
    if not isinstance(ops, list) or not ops:
        return [
            {
                "type": "text",
                "text": "'ops' must be a non-empty array — batch needs at least one op to execute.",
            }
        ]

    fail_fast = args.get("fail_fast", True)
    total = len(ops)
    lines: list[str] = [f"Batch results ({total} ops):"]
    aborted = False

    for idx, op in enumerate(ops, start=1):
        tool, op_args, error = _validate_batch_op(op)
        label = tool or "?"
        if error:
            lines.append(f"[{idx}/{total}] {label} → error: {error}")
            if fail_fast:
                aborted = True
                break
            continue
        op_result = handle_tool_call(d, worker_name, tool, op_args)
        text = op_result[0].get("text", "") if op_result else ""
        lines.append(f"[{idx}/{total}] {tool} → {text}")

    if aborted:
        lines.append(f"Batch aborted after error (stopped with {len(lines) - 1}/{total} ops).")
    return [{"type": "text", "text": "\n".join(lines)}]


_HANDLERS = {
    "swarm_check_messages": _handle_check_messages,
    "swarm_send_message": _handle_send_message,
    "swarm_note_to_queen": _handle_note_to_queen,
    "swarm_report_blocker": _handle_report_blocker,
    "swarm_park_task": _handle_park_task,
    "swarm_draft_email": _handle_draft_email,
    "swarm_task_status": _handle_task_status,
    "swarm_claim_file": _handle_claim_file,
    "swarm_complete_task": _handle_complete_task,
    "swarm_create_task": _handle_create_task,
    "swarm_get_learnings": _handle_get_learnings,
    "swarm_get_playbooks": _handle_get_playbooks,
    "swarm_report_progress": _handle_report_progress,
    "swarm_batch": _handle_batch,
}


# Queen-only tools live in their own module to keep the core tools.py
# focused on the shared worker surface. They're folded into the live
# TOOLS list and _HANDLERS map at import time so the MCP server
# publishes a single unified tool catalog.
from swarm.mcp.queen_tools import QUEEN_HANDLERS, QUEEN_TOOLS  # noqa: E402

TOOLS.extend(QUEEN_TOOLS)
_HANDLERS.update(QUEEN_HANDLERS)
_TOOL_NAMES.update(QUEEN_HANDLERS.keys())
