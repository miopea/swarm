"""Handler for the ``swarm_block_on_external`` MCP tool (task #876).

A first-class "blocked-on-external" state for work that is correctly waiting
on an UPSTREAM/EXTERNAL dependency with no internal swarm task to reference
(a third-party package shipping compat, a vendor PR merging). Distinct from:

- ``swarm_report_blocker`` — requires an INTERNAL swarm task number; there is
  none for an upstream release.
- ``swarm_park_task`` — parks back to ASSIGNED, which the IdleWatcher still
  nudges.

This tool transitions the caller's OWN ASSIGNED/ACTIVE task → BLOCKED, which
is off-active (no IdleWatcher nudges) yet stays visible/tracked on the open
board. Resume is the normal operator re-dispatch (Start → BLOCKED→ACTIVE),
which clears the watch reference.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import BlockExternalArgs
from swarm.mcp.types import TextContent

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
    {
        "name": "swarm_block_on_external",
        "description": (
            "Park your OWN task as BLOCKED-on-external when it's correctly "
            "waiting on an UPSTREAM/EXTERNAL dependency that has no swarm task "
            "to point at — a third-party package shipping a fix, a vendor PR "
            "merging, an API going live. Unlike ``swarm_park_task`` (parks to "
            "ASSIGNED, still nudged) and ``swarm_report_blocker`` (needs an "
            "INTERNAL task number), this holds the task off-active so the "
            "idle-watcher stops nudging you over work you cannot progress, "
            "WHILE keeping it visible/tracked on the board (not completed). "
            "Pass ``watch_ref`` naming the external thing you're waiting on "
            "(e.g. 'npm @org/pkg@^10 release' or a PR URL). The operator "
            "resumes it from the dashboard (Start) once the upstream ships. "
            "Pass ``task_number`` to say exactly which of your tasks; if you "
            "own only one active task you may omit it — if you own several "
            "and omit it the tool REFUSES and lists them rather than guessing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "watch_ref": {
                    "type": "string",
                    "description": (
                        "What external/upstream thing you're waiting on — a "
                        "package@range, a PR/issue URL, a vendor ticket. Shown "
                        "on the board so the operator knows what unblocks it."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Why the task is blocked on it (1 sentence; recorded to history + buzz)."
                    ),
                },
                "task_number": {
                    "type": "integer",
                    "description": (
                        "Which of YOUR tasks to block (its display number). "
                        "Optional only when you own exactly one active task; "
                        "required to disambiguate when you own several. Must "
                        "be owned by you and ASSIGNED or ACTIVE."
                    ),
                },
            },
            "required": ["watch_ref", "reason"],
            "examples": [
                {
                    "watch_ref": "npm eslint@^10 (typescript-eslint compat)",
                    "reason": "eslint-10 fleet migration parked until upstream ships TS6 compat",
                    "task_number": 874,
                },
                {
                    "watch_ref": "https://github.com/vendor/lib/pull/42",
                    "reason": "waiting on vendor PR to merge before I can finish the integration",
                },
            ],
        },
    },
]


def _resolve_target(
    board: Any, worker_name: str, raw_num: Any
) -> tuple[Any, list[TextContent] | None]:
    """Resolve which of the caller's tasks to block, or an error response.

    Returns ``(task, None)`` on success or ``(None, error)`` when the target
    is ambiguous / not owned / wrong-state. Mirrors ``swarm_park_task``'s
    discipline: explicit number targets exactly that task; omitted blocks the
    sole active task iff there is exactly one; omitted with >1 candidate
    REFUSES rather than guessing.
    """
    candidates = board.active_tasks_for_worker(worker_name)
    if raw_num is not None and str(raw_num).strip() != "":
        try:
            want = int(raw_num)
        except (TypeError, ValueError):
            return None, [
                {"type": "text", "text": f"'task_number' must be a number, got {raw_num!r}."}
            ]
        target = next((t for t in board.tasks_for_worker(worker_name) if t.number == want), None)
        if target is None:
            return None, [
                {
                    "type": "text",
                    "text": (
                        f"Task #{want} is not assigned to you (or doesn't exist) — you can only "
                        f"block your own task. Nothing changed."
                    ),
                }
            ]
        if target.id not in {t.id for t in candidates}:
            return None, [
                {
                    "type": "text",
                    "text": (
                        f"Task #{want} is {target.status.value}, not ASSIGNED/ACTIVE — only an "
                        f"active task can be blocked-on-external. Nothing changed."
                    ),
                }
            ]
        return target, None
    if not candidates:
        return None, [{"type": "text", "text": f"No active task to block for '{worker_name}'."}]
    if len(candidates) > 1:
        nums = ", ".join(f"#{t.number}" for t in sorted(candidates, key=lambda t: t.number))
        return None, [
            {
                "type": "text",
                "text": (
                    f"Ambiguous — you own {len(candidates)} active tasks ({nums}). "
                    f"swarm_block_on_external won't guess which to block. Re-call it with "
                    f"task_number=<n>. Nothing changed."
                ),
            }
        ]
    return candidates[0], None


def _handle_block_on_external(
    d: SwarmDaemon, worker_name: str, args: BlockExternalArgs
) -> list[TextContent]:
    """Park one of the caller's OWN ASSIGNED/ACTIVE tasks as BLOCKED-on-external.

    Only the caller's own tasks are touched (cross-worker blocking impossible
    by construction). Disambiguation is delegated to :func:`_resolve_target`.
    """
    watch_ref = str(args.get("watch_ref") or "").strip()
    reason = str(args.get("reason") or "").strip()
    if not watch_ref:
        return [
            {
                "type": "text",
                "text": "Missing 'watch_ref' — name the external/upstream thing you're waiting on.",
            }
        ]
    if not reason:
        return [{"type": "text", "text": "Missing 'reason' — say why the task is blocked on it."}]
    board = getattr(d, "task_board", None)
    if board is None:
        return [{"type": "text", "text": "Task board unavailable on this daemon."}]

    task, error = _resolve_target(board, worker_name, args.get("task_number"))
    if error is not None:
        return error

    if not board.block_on_external(task.id, worker_name, watch_ref, reason):
        return [{"type": "text", "text": f"Could not block #{task.number} (state changed?)."}]

    from swarm.drones.log import LogCategory, SystemAction
    from swarm.tasks.history import TaskAction

    detail = f"#{task.number} blocked-on-external [{watch_ref[:60]}]: {reason[:80]}"
    try:
        d.drone_log.add(SystemAction.TASK_PARKED, worker_name, detail, category=LogCategory.TASK)
        if getattr(d, "task_history", None) is not None:
            d.task_history.append(
                task.id,
                TaskAction.BLOCKED,
                actor=worker_name,
                detail=f"blocked-on-external [{watch_ref}]: {reason}",
            )
    except Exception:
        pass  # audit best-effort — the transition already succeeded
    return [
        {
            "type": "text",
            "text": (
                f"Blocked #{task.number} → BLOCKED on external: {watch_ref}. The idle-watcher "
                f"won't nudge you on it; it stays tracked on the board. The operator resumes it "
                f"from the dashboard once the upstream ships."
            ),
        }
    ]


HANDLERS = {"swarm_block_on_external": _handle_block_on_external}
