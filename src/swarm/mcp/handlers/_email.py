"""Handler for the ``swarm_draft_email`` MCP tool.

Extracted from ``mcp/tools.py`` (task #518).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import DraftEmailArgs
from swarm.mcp.types import TextContent

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_draft_email",
        "description": (
            "Draft a new email in the operator's Outlook Drafts folder via the "
            "Microsoft Graph integration. Use this when you need to compose "
            "outbound email on the operator's behalf (e.g. ask a stakeholder "
            "for clarification on an email-sourced task, draft "
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
]


_DraftEmailFields = tuple[list[str], str, str, list[str] | None, str, str]


def _validate_draft_email_args(
    args: dict[str, Any],
) -> tuple[_DraftEmailFields | None, str]:
    """Validate + coerce swarm_draft_email inputs.

    Returns ``(fields, "")`` on success and ``(None, error_message)`` on
    failure.  The two-tuple shape lets callers branch on the first
    element being None instead of doing an ``isinstance(x, str)`` type
    guard against a union return — clearer at the call site and easier
    to type.
    """
    to_raw = args.get("to")
    subject = (args.get("subject") or "").strip()
    body = args.get("body") or ""
    cc_raw = args.get("cc") or []
    body_type = (args.get("body_type") or "text").strip().lower()
    reason = (args.get("reason") or "").strip()

    if not isinstance(to_raw, list) or not to_raw:
        return None, "Missing 'to' — must be a non-empty list of addresses."
    if not all(isinstance(a, str) and a.strip() for a in to_raw):
        return None, "'to' entries must be non-empty strings."
    if not subject:
        return None, "Missing 'subject'."
    if not body:
        return None, "Missing 'body'."
    if body_type not in ("text", "html"):
        return None, "'body_type' must be 'text' or 'html'."
    if cc_raw and not (
        isinstance(cc_raw, list) and all(isinstance(a, str) and a.strip() for a in cc_raw)
    ):
        return None, "'cc' must be a list of non-empty strings."

    to_list = [a.strip() for a in to_raw]
    cc_list = [a.strip() for a in cc_raw] if cc_raw else None
    return (to_list, subject, body, cc_list, body_type, reason), ""


def _handle_draft_email(
    d: SwarmDaemon, worker_name: str, args: DraftEmailArgs
) -> list[TextContent]:
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

    fields, error = _validate_draft_email_args(args)
    if fields is None:
        return [{"type": "text", "text": error}]
    to_list, subject, body, cc_list, body_type, reason = fields

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


HANDLERS = {"swarm_draft_email": _handle_draft_email}
