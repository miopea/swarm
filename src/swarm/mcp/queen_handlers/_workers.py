"""Queen MCP handlers for worker-targeted actions (interrupt, prompt).

Extracted from ``mcp/queen_tools.py`` (task #519). Both handlers fire
async daemon calls via the shared ``_fire_async`` helper in ``_tasks``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.mcp._arg_types import QueenInterruptWorkerArgs, QueenPromptWorkerArgs
from swarm.mcp.queen_handlers._common import _assert_queen
from swarm.mcp.queen_handlers._tasks import _fire_async
from swarm.mcp.types import TextContent
from swarm.worker.worker import QUEEN_WORKER_NAME

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


TOOLS: list[dict[str, Any]] = [
    {
        "name": "queen_interrupt_worker",
        "description": (
            "Send Ctrl-C to a worker's PTY to interrupt its current turn. "
            "DESTRUCTIVE: cancels in-flight tool use and loses any uncommitted "
            "work.  Use only when the worker is genuinely stuck (queen_view_worker_state "
            "shows long BUZZING with flat token growth) or going the wrong direction "
            "and you've confirmed via the buzz log.  Always provide a reason — it "
            "lands in the buzz log as an OPERATOR entry."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {
                    "type": "string",
                    "description": "Name of the worker to interrupt.",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Why you're interrupting.  Required — surfaces in buzz log "
                        "so the operator can audit."
                    ),
                },
            },
            "required": ["worker", "reason"],
            "examples": [
                {"worker": "hub", "reason": "BUZZING 20m, 3 low-delta ticks, likely stuck"},
            ],
        },
    },
    {
        "name": "queen_prompt_worker",
        "description": (
            "Push a prompt directly into a worker's PTY — the worker sees it "
            "exactly as if the operator had typed it in the dashboard chat.  "
            "Use this when you want a worker to DO something now (take a task, "
            "answer a question, run a check), not just when you want them to "
            "know something (use queen_send_message for the inbox channel).  "
            "Safe to call on BUZZING workers: Claude Code queues the text and "
            "injects it as a new user turn after the current one completes — "
            "no interruption, no lost work.  Refuses only when the target is "
            "the Queen herself or the worker is STUNG (dead process).  "
            "Always include a reason; it lands in the buzz log as an "
            "OPERATOR entry for audit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {
                    "type": "string",
                    "description": "Name of the worker to prompt.",
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "Text to inject into the worker's PTY.  Enter is sent "
                        "automatically after the text (same as operator typing)."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Why you're prompting this worker now.  Required — "
                        "shows up in the buzz log so the operator can audit."
                    ),
                },
                "acknowledge_engaged": {
                    "type": "boolean",
                    "description": (
                        "Set true to acknowledge the target may already be "
                        "engaged on this work and suppress the advisory NOTE. "
                        "The prompt sends either way — this only records that "
                        "you saw the engagement context (logged for audit). "
                        "Use it when re-issuing a prompt the tool flagged as a "
                        "possible collision but which you intend regardless "
                        "(P1, scope correction, pause)."
                    ),
                },
            },
            "required": ["worker", "prompt", "reason"],
            "examples": [
                {
                    "worker": "hub",
                    "prompt": "Please run /check and paste the output.",
                    "reason": "verifying pre-commit hooks before asking for a PR",
                },
                {
                    "worker": "platform",
                    "prompt": "Pause current work — rate limit warning.",
                    "reason": "5hr window at 88%",
                },
            ],
        },
    },
]


def _handle_interrupt_worker(
    d: SwarmDaemon, worker_name: str, args: QueenInterruptWorkerArgs
) -> list[TextContent]:
    err = _assert_queen(worker_name)
    if err:
        return err
    target = (args.get("worker") or "").strip()
    reason = (args.get("reason") or "").strip()
    if not target:
        return [{"type": "text", "text": "Missing 'worker'."}]
    if not reason:
        return [{"type": "text", "text": "Missing 'reason' — interrupts must be audited."}]
    if target == QUEEN_WORKER_NAME:
        return [{"type": "text", "text": "Refusing to interrupt the Queen herself."}]
    if not any(w.name == target for w in d.workers):
        return [{"type": "text", "text": f"Worker '{target}' not found."}]
    from swarm.drones.log import LogCategory, SystemAction

    d.drone_log.add(
        SystemAction.OPERATOR,
        target,
        f"queen interrupted (Ctrl-C): {reason[:120]}",
        category=LogCategory.OPERATOR,
    )
    worker_svc = getattr(d, "worker_svc", None)
    if worker_svc is None:
        return [{"type": "text", "text": "Worker service unavailable."}]
    _fire_async(worker_svc.interrupt_worker(target))
    return [{"type": "text", "text": f"Interrupt sent to {target}."}]


def _handle_prompt_worker(
    d: SwarmDaemon, worker_name: str, args: QueenPromptWorkerArgs
) -> list[TextContent]:
    """Push a prompt into a worker's PTY — Queen-initiated direct chat.

    Claude Code queues PTY input while a turn is in progress, so sending
    to a BUZZING worker does NOT interrupt current work — it lands as a
    new user turn after the current one completes.  Hard refusals:
    self-target (Queen prompting herself) and STUNG (dead process).
    """
    err = _assert_queen(worker_name)
    if err:
        return err
    target = (args.get("worker") or "").strip()
    prompt = args.get("prompt") or ""
    reason = (args.get("reason") or "").strip()
    if not target:
        return [{"type": "text", "text": "Missing 'worker'."}]
    if not prompt:
        return [{"type": "text", "text": "Missing 'prompt'."}]
    if not reason:
        return [{"type": "text", "text": "Missing 'reason' — prompts must be audited."}]
    if target == QUEEN_WORKER_NAME:
        return [{"type": "text", "text": "Refusing to prompt the Queen herself."}]
    worker = next((w for w in d.workers if w.name == target), None)
    if worker is None:
        return [{"type": "text", "text": f"Worker '{target}' not found."}]

    from swarm.worker.worker import WorkerState

    if worker.state == WorkerState.STUNG:
        return [{"type": "text", "text": f"Worker '{target}' is STUNG — revive before prompting."}]

    import time as _time

    from swarm.drones.log import LogCategory, SystemAction
    from swarm.server.engagement import engagement_snapshot

    # #913: engagement awareness. Surface the target's live engagement to the
    # Queen and soft-flag a likely collision (target freshly engaged on work
    # that may be the same) — but the prompt ALWAYS sends. This is advisory:
    # the Queen must be able to reach a busy worker (P1, pause, scope fix).
    # ``send_to_worker`` is untouched.
    ack = bool(args.get("acknowledge_engaged"))
    drones_cfg = getattr(d.config, "drones", None)
    window = float(getattr(drones_cfg, "prompt_collision_window_seconds", 0.0))
    # #939: surface the target's live PROCESS state too — a worker with no
    # board task can still be BUZZING (e.g. a task-less audit run), and
    # "no ACTIVE task" was being misread as "idle/free".
    snap = engagement_snapshot(
        getattr(d, "task_board", None),
        getattr(d, "message_store", None),
        target,
        now=_time.time(),
        process_state=worker.display_state.value,
        process_state_ago=worker.state_duration,
    )
    collided = snap.collides_within(window)
    engagement_str = snap.summary()

    # Note in the buzz log whether the prompt will queue (worker mid-turn)
    # or land on an idle worker — auditing benefits from that distinction.
    will_queue = worker.state == WorkerState.BUZZING
    queue_tag = " [queued, worker BUZZING]" if will_queue else ""
    ack_tag = " [ack-engaged]" if ack else (" [COLLISION]" if collided else "")
    d.drone_log.add(
        SystemAction.OPERATOR,
        target,
        f"queen prompt{queue_tag}{ack_tag} ({reason[:80]}): {prompt[:100]} "
        f"|| engagement: {engagement_str}",
        category=LogCategory.OPERATOR,
    )
    worker_svc = getattr(d, "worker_svc", None)
    if worker_svc is None:
        return [{"type": "text", "text": "Worker service unavailable."}]
    _fire_async(worker_svc.send_to_worker(target, prompt, _log_operator=False))
    suffix = " — queued for next turn" if will_queue else ""
    lines = [f"Prompt sent to {target}{suffix}.", f"Target engagement: {engagement_str}."]
    if collided and not ack:
        lines.append(
            "NOTE: target appears freshly engaged; if this prompt is about the same work it "
            "may be redundant — re-issue with acknowledge_engaged=true to suppress this notice."
        )
    return [{"type": "text", "text": "\n".join(lines)}]


HANDLERS = {
    "queen_interrupt_worker": _handle_interrupt_worker,
    "queen_prompt_worker": _handle_prompt_worker,
}
