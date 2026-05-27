"""Handler for the ``swarm_batch`` MCP tool.

Extracted from ``mcp/tools.py`` (task #518). The handler delegates each
inner op back through the top-level ``handle_tool_call`` dispatcher in
:mod:`swarm.mcp.tools`; that import is lazy so the dispatcher doesn't
become a circular dependency at module-import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import BatchArgs
from swarm.mcp.types import TextContent

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
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
]


def _validate_batch_op(op: object) -> tuple[str, dict[str, Any], str]:
    """Validate a single batch op. Returns ``(tool, args, error)``.

    ``error`` is empty when the op is valid. Otherwise it explains why
    the op cannot run; tool/args are still returned so callers can log
    them in the failure line.
    """
    from swarm.mcp.tools import _TOOL_NAMES

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


def _handle_batch(d: SwarmDaemon, worker_name: str, args: BatchArgs) -> list[TextContent]:
    """Execute a sequence of swarm_* ops in one MCP round-trip.

    Workers that need claim_file + send_message + complete_task today
    pay three JSON-RPC round-trips. ``swarm_batch`` lets them send one
    request. Each op is still logged individually via
    ``handle_tool_call`` so the dashboard shows the real activity, not
    a single opaque "batch" entry.
    """
    from swarm.mcp.tools import handle_tool_call

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


HANDLERS = {"swarm_batch": _handle_batch}
