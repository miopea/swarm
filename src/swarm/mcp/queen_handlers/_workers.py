"""Queen MCP handlers for worker-targeted actions (interrupt, prompt).

Extracted from ``mcp/queen_tools.py`` (task #519). Both handlers fire
async daemon calls via the shared ``_fire_async`` helper in ``_tasks``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.drones.idle_watcher import _format_duration
from swarm.drones.log import truncate_for_log
from swarm.logging import get_logger
from swarm.mcp._arg_types import QueenInterruptWorkerArgs, QueenPromptWorkerArgs
from swarm.mcp.queen_handlers._common import _assert_queen
from swarm.mcp.queen_handlers._tasks import _fire_async
from swarm.mcp.types import TextContent
from swarm.worker.worker import QUEEN_WORKER_NAME

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon

_log = get_logger("mcp.queen.workers")


TOOLS: list[dict[str, Any]] = [
    {
        "name": "queen_answer_prompt",
        "description": (
            "Answer a worker's OPEN selection prompt by option number. Use this when "
            "queen_view_worker_state shows a picker — a permission confirmation, a plan "
            "approval — and you have the authority to decide it. "
            "READ THE PROMPT FIRST: call queen_view_worker_state and pass the "
            "'fingerprint' it reports back here. That is not ceremony — it is what stops "
            "you answering a question that changed between reading and replying, and a "
            "mismatch REFUSES rather than selecting whatever is highlighted now. "
            "For a prompt you want to DENY or dismiss, queen_dismiss_prompt (Escape) is "
            "gentler than queen_interrupt_worker, which cancels the whole turn."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {"type": "string", "description": "Worker showing the prompt."},
                "option": {
                    "type": "integer",
                    "description": "The option NUMBER to select, as rendered (1, 2, 3...).",
                },
                "fingerprint": {
                    "type": "string",
                    "description": (
                        "The prompt fingerprint from queen_view_worker_state. Identifies "
                        "the QUESTION, not the cursor — moving the highlight does not "
                        "change it, but a changed option list does."
                    ),
                },
            },
            "required": ["worker", "option", "fingerprint"],
            "examples": [{"worker": "platform-api", "option": 1, "fingerprint": "a1b2c3d4e5f6"}],
        },
    },
    {
        "name": "queen_dismiss_prompt",
        "description": (
            "Send Escape to a worker showing a selection prompt — the CLI's own documented "
            "dismissal ('Esc to cancel' appears in the prompt footer). "
            "CALL THIS WHEN a worker is stalled on a picker whose answer should be NO, or "
            "when you want the prompt gone without deciding it. "
            "OBSERVED TO WORK 2026-08-15 (#1623), against a picker manufactured on "
            "purpose because waiting for one to appear naturally had left this untested. "
            "The Queen fired it at a real open AskUserQuestion on worker 'swarm': the "
            "picker CLOSED (read-back showed `prompt` null) and that worker's PTY "
            "recorded 'User declined to answer questions'. So Escape DECLINES the "
            "prompt — it does NOT commit the highlighted option, which is the failure "
            "the probe was built to catch (#1443 reached by a new route). "
            "SCOPE, because one prompt type is not the general case: this was measured "
            "on an AskUserQuestion picker ONLY. A permission confirmation is a different "
            "prompt type and may take a different path; it has NOT been measured. The "
            "tool still reads back — report anything that differs. "
            "If a read-back shows the fingerprint unchanged, queen_answer_prompt "
            "selecting the deny option is the fallback. Prefer this over "
            "queen_interrupt_worker regardless: Ctrl-C loses in-flight work and REFUSES "
            "on a picker anyway. Always give a reason; it lands in the buzz log."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker": {"type": "string", "description": "Worker to send Escape to."},
                "reason": {"type": "string", "description": "Why — audited in the buzz log."},
            },
            "required": ["worker", "reason"],
            "examples": [{"worker": "nexus", "reason": "least-privilege probe SHOULD be denied"}],
        },
    },
    {
        "name": "queen_interrupt_worker",
        "description": (
            "Send Ctrl-C to a worker's PTY to interrupt its current turn. "
            "DESTRUCTIVE: cancels in-flight tool use and loses any uncommitted "
            "work.  Use only when the worker is genuinely stuck (queen_view_worker_state "
            "shows long BUZZING with flat token growth) or going the wrong direction "
            "and you've confirmed via the buzz log.  Always provide a reason — it "
            "lands in the buzz log as an OPERATOR entry. "
            "REFUSES when the target is on a selection prompt (#1633), and sends nothing: "
            "SIGINT does not close a picker. Measured on a real prompt 2026-08-14 — the "
            "picker survived it, because a picker is an input WAIT rather than a running "
            "turn and the signal has nothing to cancel. The refusal names "
            "queen_dismiss_prompt and queen_answer_prompt, which do work. "
            "On a worker with no prompt open this reports a DISPATCH, not an outcome; "
            "re-read with queen_view_worker_state before concluding the turn stopped."
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


def _prepare_target(d: SwarmDaemon, worker_name: str, target: str) -> list[TextContent] | None:
    """Shared refusals for the prompt tools: identity, self-target, existence."""
    err = _assert_queen(worker_name)
    if err:
        return err
    if not target:
        return [{"type": "text", "text": "Missing 'worker'."}]
    if target == QUEEN_WORKER_NAME:
        return [{"type": "text", "text": "Refusing to answer the Queen's own prompt."}]
    if not any(w.name == target for w in d.workers):
        return [{"type": "text", "text": f"Worker '{target}' not found."}]
    return None


def _handle_answer_prompt(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[TextContent]:
    """Answer an open selection prompt by option number (#1608).

    SYNCHRONOUS on purpose, unlike its neighbours. `_fire_async` would return "sent"
    before the service had decided anything, so a stale fingerprint would be reported to
    the Queen as success and the refusal — the entire point of the fingerprint — would be
    invisible. #1527 is the standing example of an unawaited call swallowing its outcome.
    """
    target = (args.get("worker") or "").strip()
    refusal = _prepare_target(d, worker_name, target)
    if refusal:
        return refusal
    fingerprint = (args.get("fingerprint") or "").strip()
    if not fingerprint:
        return [
            {
                "type": "text",
                "text": (
                    "Missing 'fingerprint' — read the prompt with queen_view_worker_state "
                    "first. Answering without reading is the failure this guard exists for."
                ),
            }
        ]
    try:
        option = int(args.get("option"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return [{"type": "text", "text": f"'option' must be a number, got {args.get('option')!r}."}]

    worker_svc = getattr(d, "worker_svc", None)
    if worker_svc is None:
        return [{"type": "text", "text": "Worker service unavailable."}]

    # Validate SYNCHRONOUSLY and report that; fire only the keystroke async. This
    # handler runs inside the event loop, so it cannot await — and `_fire_async` would
    # return "sent" before anything was decided, telling the Queen her answer landed
    # while a stale fingerprint was rejected out of band. The refusal IS the feature.
    ok, message = worker_svc.check_prompt_answer(target, option, fingerprint)
    if not ok:
        return [{"type": "text", "text": f"{target}: REFUSED — {message}"}]
    _fire_async(
        worker_svc.answer_open_prompt(target, option, fingerprint),
        label=f"answer_prompt({target})",
        daemon=d,
    )
    # DO NOT SAY "answered". `_fire_async` returns before the keystroke is written, so
    # this handler cannot know the outcome — and the first live use of this tool reported
    # success while the picker stayed open for 16 seconds. That is the same defect #1608
    # was filed about, reproduced inside the fix for it. The service layer reads back
    # after the write and records the verified verdict; this says only what it knows.
    return [
        {
            "type": "text",
            "text": (
                f"{target}: SENT option {option} ({message}) — NOT YET CONFIRMED.\n"
                f"The keystroke is written asynchronously and the effect is not visible "
                f"from here. Re-read with queen_view_worker_state(worker='{target}'): if "
                f"the `prompt` block is gone, it took; if fingerprint {fingerprint} is "
                f"still there, it did not, and queen_dismiss_prompt or the operator is "
                f"the next step. The verified outcome is also written to the buzz log as "
                f"'answered' or 'SENT BUT NOT CONFIRMED'."
            ),
        }
    ]


def _handle_dismiss_prompt(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[TextContent]:
    """Send Escape — the CLI's own documented dismissal for a picker (#1608)."""
    target = (args.get("worker") or "").strip()
    refusal = _prepare_target(d, worker_name, target)
    if refusal:
        return refusal
    reason = (args.get("reason") or "").strip()
    if not reason:
        return [{"type": "text", "text": "Missing 'reason' — dismissals are audited."}]
    worker_svc = getattr(d, "worker_svc", None)
    if worker_svc is None:
        return [{"type": "text", "text": "Worker service unavailable."}]
    from swarm.drones.log import LogCategory, SystemAction

    d.drone_log.add(
        SystemAction.OPERATOR,
        target,
        f"queen dismissed prompt (Esc): {truncate_for_log(reason, 120)}",
        category=LogCategory.OPERATOR,
    )
    # #1623: the last verb still reporting a DISPATCH as an OUTCOME. Validate
    # synchronously so the "no prompt open" case is truthful, then let the service read
    # back and record the verified verdict.
    worker = next((w for w in d.workers if w.name == target), None)
    if worker is not None and _refuse_if_prompt_would_hold(worker, target) is None:
        return [
            {
                "type": "text",
                "text": (
                    f"{target}: no selection prompt is open — nothing was sent. Escape "
                    f"into an ordinary session would cancel whatever the worker was "
                    f"typing, so it is refused rather than sent blindly."
                ),
            }
        ]
    _fire_async(worker_svc.dismiss_open_prompt(target), label=f"dismiss_prompt({target})", daemon=d)
    return [
        {
            "type": "text",
            "text": (
                f"{target}: SENT Escape — NOT YET CONFIRMED.\n"
                f"Escape is written asynchronously and the effect is not visible from "
                f"here. Re-read with queen_view_worker_state(worker='{target}'): if the "
                f"`prompt` block is gone it took; if it is unchanged, Escape does not "
                f"close this picker and queen_answer_prompt selecting the deny option is "
                f"the proven route. The verified outcome is written to the buzz log as "
                f"'dismissed' or 'SENT BUT NOT CONFIRMED'.\n"
                f"NOTE: measured 2026-08-15 (#1623) on an AskUserQuestion picker, Escape "
                f"DECLINED it — the picker closed and the PTY recorded 'User declined to "
                f"answer questions' rather than committing the highlighted option. A "
                f"PERMISSION prompt has not been measured and may differ. Report what "
                f"you see."
            ),
        }
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
    # #1633: REFUSE on a picker rather than warning, and refuse BEFORE the buzz-log entry
    # so the log does not record an interrupt that never happened. The #1608 version
    # dispatched SIGINT and appended a note saying it would not work — better than
    # silence, still wrong: it performs an action MEASURED to be useless, and a warning
    # attached to a completed send reads as advisory. A picker is an input WAIT rather
    # than a running turn, so the signal has nothing to cancel; the picker survived it on
    # a real prompt 2026-08-14.
    target_worker = next((w for w in d.workers if w.name == target), None)
    if target_worker is not None and _refuse_if_prompt_would_hold(target_worker, target):
        return [
            {
                "type": "text",
                "text": (
                    f"NOT SENT — {target} is on a selection prompt, and SIGINT does not "
                    f"close one. A picker is an input WAIT rather than a running turn, "
                    f"so the signal has nothing to cancel; measured on a real prompt "
                    f"2026-08-14, the picker survived.\n"
                    f"To decline it: queen_dismiss_prompt(worker='{target}', reason=…). "
                    f"To choose an option: queen_view_worker_state(worker='{target}') "
                    f"for the options and fingerprint, then queen_answer_prompt."
                ),
            }
        ]
    from swarm.drones.log import LogCategory, SystemAction

    d.drone_log.add(
        SystemAction.OPERATOR,
        target,
        f"queen interrupted (Ctrl-C): {truncate_for_log(reason, 120)}",
        category=LogCategory.OPERATOR,
    )
    worker_svc = getattr(d, "worker_svc", None)
    if worker_svc is None:
        return [{"type": "text", "text": "Worker service unavailable."}]
    # #1608: "Interrupt sent" was TRUE AND USELESS. The signal is dispatched; whether it
    # cancelled anything is a different fact, and the Queen believed the first for the
    # second — reporting to the operator that an interrupt had worked while the picker it
    # was aimed at stayed open. Say what is known and what is not.
    _fire_async(worker_svc.interrupt_worker(target), label=f"interrupt({target})", daemon=d)
    return [
        {
            "type": "text",
            "text": (
                f"SIGINT dispatched to {target} — NOT CONFIRMED as having cancelled "
                f"anything. This sends an OS signal to the process group; whether the "
                f"worker's current activity stops is a separate fact this tool cannot "
                f"see. Re-read with queen_view_worker_state before concluding it "
                f"worked."
            ),
        }
    ]


# How much screen to read when checking for the hold. Matches
# ``pty.process._PROMPT_SCAN_LINES`` — this must see exactly what the guard sees, or the
# handler could promise delivery for a message the guard is about to defer.
_PROMPT_HOLD_SCAN_LINES = 120


def _note_refused_prompt(d: SwarmDaemon, target: str, message: str) -> None:
    """Tell the RECIPIENT a prompt was attempted and refused (#1648).

    The inbox is the one channel not subject to the #1451 PTY hold, so it can reach a
    worker sitting on a picker. It carries the body, not just the fact of the attempt: a
    recipient who learns only THAT something was lost is barely better off than one who
    learns nothing.

    MEASURED 2026-08-15 — a Queen dispatch describing three tickets was refused while a
    picker was open and reached the recipient on neither channel. It survived only
    because a one-line recap appeared in a later message.

    Never raises. This is a courtesy on a failure path, and an exception here would
    replace a truthful refusal with a stack trace.
    """
    try:
        store = getattr(d, "message_store", None)
        if store is None:
            return
        store.send(
            QUEEN_WORKER_NAME,
            target,
            "status",
            (
                "A prompt was sent to you and REFUSED because you had a selection prompt "
                "open — the #1451 guard blocks automated writes into an open picker. It "
                "was never typed into your PTY, so this inbox copy is the only place it "
                "exists. Body follows:\n\n"
                f"{message}"
            ),
        )
    except Exception:
        _log.warning("could not leave a refused-prompt note for %s", target, exc_info=True)


def _refuse_if_prompt_would_hold(
    worker: Any, target: str, message: str | None = None
) -> list[TextContent] | None:
    """Refuse a prompt that the #1451 guard would silently hold (#1608).

    MEASURED 2026-08-14: `queen_prompt_worker` reported "Prompt sent" for a message that
    `send_keys` immediately deferred, because `_fire_async` returns before the guard runs.
    From the Queen's side a HELD message and a DELIVERED one were IDENTICAL — which is
    why she spent a night believing she had no way to act on a stalled worker, while the
    one tool that could reach it kept telling her it had.

    Refusing beats queueing: a message delivered whenever the prompt happens to close is
    a message arriving with no relation to why it was sent.
    """
    proc = getattr(worker, "process", None)
    if proc is None:
        return None
    try:
        from swarm.pty.prompt_guard import has_open_selection_prompt

        if not has_open_selection_prompt(proc.get_content(_PROMPT_HOLD_SCAN_LINES)):
            return None
    except Exception:
        # Unknowable → report the send. Inventing a hold would block the Queen's only
        # channel on a read failure, which is the worse direction.
        return None
    return [
        {
            "type": "text",
            "text": (
                f"NOT SENT — {target} has an open selection prompt, and the #1451 guard "
                f"would have HELD this message rather than delivering it, so it was "
                f"refused here instead. Nothing was queued, and nothing will arrive later "
                f"when the prompt closes — this is a refusal, not a deferral (#1623).\n"
                f"To act on the prompt itself: queen_view_worker_state(worker='{target}', "
                f"lines=200) to read the options, then queen_answer_prompt to select one, "
                f"or queen_dismiss_prompt to send Escape.\n"
                f"queen_interrupt_worker does NOT close a picker: it sends SIGINT to the "
                f"process group, and a picker is an input WAIT rather than a running turn, "
                f"so the signal has nothing to cancel. Measured on a real prompt "
                f"2026-08-14 — the picker survived the interrupt."
            )
            + (
                # #1648: hand the body BACK. "Nothing was queued" told the caller the
                # message was safe to forget while making it their sole responsibility to
                # remember — and a Queen dispatch was lost to exactly that on 2026-08-15.
                # A copy also goes to the recipient's inbox, which the PTY hold cannot
                # reach; this half is so the SENDER does not have to re-derive the text.
                (
                    f"\n\nRESEND THIS ONCE THE PICKER IS CLEARED — the body is reproduced "
                    f"here because it exists nowhere else. A copy has been left in "
                    f"{target}'s inbox, which the PTY hold does not block.\n"
                    f"--- BEGIN UNSENT MESSAGE ---\n{message}\n--- END UNSENT MESSAGE ---"
                )
                if message
                else ""
            ),
        }
    ]


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
    why = truncate_for_log(reason, 80)
    what = truncate_for_log(prompt, 100)
    d.drone_log.add(
        SystemAction.OPERATOR,
        target,
        f"queen prompt{queue_tag}{ack_tag} ({why}): {what} || engagement: {engagement_str}",
        category=LogCategory.OPERATOR,
    )
    worker_svc = getattr(d, "worker_svc", None)
    if worker_svc is None:
        return [{"type": "text", "text": "Worker service unavailable."}]
    # #1608: CHECK FOR THE HOLD BEFORE CLAIMING DELIVERY. `_fire_async` returns
    # immediately, so this handler reported "Prompt sent" for a message that
    # `send_keys` was about to defer — and a HELD message and a DELIVERED one were
    # indistinguishable from the Queen's side. She spent a night believing she had no
    # way to act on a stalled worker, because the only tool that could reach it kept
    # telling her it had.
    refusal = _refuse_if_prompt_would_hold(worker, target, message=prompt)
    if refusal:
        # #1648: the refusal is correct, but the CONTENT used to evaporate. Leave the
        # recipient a copy on the one channel the PTY hold cannot block before returning.
        _note_refused_prompt(d, target, prompt)
        return refusal

    _fire_async(worker_svc.send_to_worker(target, prompt, automated=True, _log_operator=False))
    # #1633: NOT "Prompt sent". The open-prompt check above is synchronous, but the send
    # is fired async — a prompt opening in that gap means the message is HELD while the
    # caller has already been told it arrived. That is the exact failure this handler's
    # refusal path was built to stop, surviving in the narrow window the refusal cannot
    # cover. `send_to_worker` returns delivered:bool (#1608) and `_fire_async` discards
    # it, so the honest report is what this handler can actually see: a dispatch.
    suffix = " — queued for next turn" if will_queue else ""
    lines = [
        f"DISPATCHED to {target}{suffix} — delivery not confirmed from here.",
        f"If {target} opens a selection prompt before this lands, the #1451 guard HOLDS "
        f"it and the buzz log records 'message HELD'. Confirm with "
        f"queen_view_worker_state; a held message needs queen_dismiss_prompt or "
        f"queen_answer_prompt to clear the picker before it can arrive.",
        f"Target engagement: {engagement_str}.",
    ]
    if collided and not ack:
        lines.append(
            "NOTE: target appears freshly engaged; if this prompt is about the same work it "
            "may be redundant — re-issue with acknowledge_engaged=true to suppress this notice."
        )
    return [{"type": "text", "text": "\n".join(lines)}]


QUEEN_STRANDED_INPUT_TOOL: dict[str, Any] = {
    "name": "queen_view_stranded_input",
    "description": (
        "Who is holding UNSENT text on their input line RIGHT NOW, longest-held first. "
        "A LIVE READ of every worker's PTY — not a summary of what has been reported, "
        "which is a different question and the one that misled twice (#1910b). Use it "
        "when you want to know whether anyone is stranded at this moment; the buzz log's "
        "UNSENT_INPUT_DETECTED rows tell you when a stranding was FOUND, and an ongoing "
        "one only re-reports hourly, so a quiet log does NOT mean a clear fleet. "
        "It NEVER submits anything: two of the original three strandings were production "
        "deploy approvals, and nothing can distinguish 'typed it and meant it' from "
        "'typed it and thought better of it'."
    ),
    "inputSchema": {"type": "object", "properties": {}, "examples": [{}]},
}


def _handle_view_stranded_input(
    d: SwarmDaemon, worker_name: str, args: dict[str, Any]
) -> list[TextContent]:
    err = _assert_queen(worker_name)
    if err:
        return err
    watcher = getattr(getattr(d, "pilot", None), "idle_watcher", None)
    if watcher is None or not hasattr(watcher, "stranded_now"):
        # NOT "nobody is stranded" — the check is unavailable, and saying otherwise is
        # the exact defect this tool exists downstream of.
        return [
            {
                "type": "text",
                "text": "Cannot tell — the idle watcher is unavailable on this daemon.",
            }
        ]
    try:
        rows = watcher.stranded_now(list(getattr(d, "workers", []) or []))
    except Exception:
        _log.warning("stranded-input read failed", exc_info=True)
        return [{"type": "text", "text": "Cannot tell — the live read failed. See the log."}]

    if not rows:
        return [
            {
                "type": "text",
                "text": (
                    "No worker is holding unsent input right now. This is a LIVE read of "
                    "every worker's PTY, not an absence of log rows."
                ),
            }
        ]
    lines = [f"{len(rows)} worker(s) holding UNSENT input right now, longest-held first:"]
    for name, text, held in rows:
        age = _format_duration(held) if held else "just found"
        lines.append(f"  {name} — held {age}: {text[:120]}")
    lines.append(
        "Nothing here has been submitted and nothing will be. Answer or clear it yourself, "
        "or ask the operator to."
    )
    return [{"type": "text", "text": "\n".join(lines)}]


TOOLS.append(QUEEN_STRANDED_INPUT_TOOL)

HANDLERS = {
    "queen_view_stranded_input": _handle_view_stranded_input,
    "queen_answer_prompt": _handle_answer_prompt,
    "queen_dismiss_prompt": _handle_dismiss_prompt,
    "queen_interrupt_worker": _handle_interrupt_worker,
    "queen_prompt_worker": _handle_prompt_worker,
}
