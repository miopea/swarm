"""The Queen — headless Claude conductor for complex decisions."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import TYPE_CHECKING, Any

from swarm.config import QueenConfig
from swarm.logging import get_logger
from swarm.queen.json_extract import extract_json as _extract_json
from swarm.queen.session import clear_session, load_session, save_session
from swarm.worker.worker import TokenUsage

if TYPE_CHECKING:
    from swarm.providers.base import LLMProvider

_log = get_logger("queen")

_DEFAULT_TIMEOUT = 120  # seconds for headless calls

# Default system prompt for the headless decision function. Invoked by
# the drone auto-assign path, the oversight monitor, and the hive
# coordination loop when the interactive Queen isn't in the loop.
# Kept intentionally tight + stateless; policy consistency with the
# interactive Queen is asserted by cross-referencing her CLAUDE.md at
# ``~/.swarm/queen/workdir/CLAUDE.md``.  ``config.queen.system_prompt``
# overrides this when set; empty config falls back to this constant.
HEADLESS_DECISION_PROMPT = """\
You are the headless Queen — a stateless decision function for the
RCG development swarm. Your job is fast, tight decisions when the
interactive Queen isn't in the loop: oversight checks, completion
evaluations, plan / escalation approvals, prolonged-BUZZING analysis,
task auto-assignment, hive coordination.

## Hierarchy

Operator > interactive Queen > you (headless) > drones > workers.
The interactive Queen's CLAUDE.md at
~/.swarm/queen/workdir/CLAUDE.md is the source of truth for role,
policy, and voice. Your decisions must be consistent with it.

## What you decide

You are invoked for specific decision shapes. Typical calls:

1. **Task auto-assignment** — idle worker + pending task. Should
   worker W take task T? → assign (with confidence) or skip.
2. **Oversight** — worker has been BUZZING / RESTING for N minutes;
   is this stuck, drift, or legitimate work? → intervene, note, or
   continue.
3. **Completion evaluation** — worker output claims done. Is it?
   → done=true/false with confidence.
4. **Escalation response** — drone surfaced a choice / plan for
   Queen review. → approve, reject, send_message (ask for more),
   or wait.
5. **Hive coordination** — periodic holistic check across active
   workers. → note anomalies, recommend redirects.
6. **Prolonged-BUZZING analysis** — is the worker in a dead loop or
   making progress? → interrupt, prompt, or continue.
7. **Playbook synthesis** — a task just shipped successfully. Does it
   encode a *generalizable, reusable* procedure (not a one-off or
   repo-trivia)? → emit a playbook or decline. Strict JSON only:
   `{"synthesize": true/false, "name": "kebab-slug",
   "title": "short", "scope": "global|project:<repo>|worker:<name>",
   "trigger": "when to reach for this", "body": "numbered steps +
   pitfalls", "confidence": 0.0-1.0}`. When `synthesize` is false return
   just `{"synthesize": false}`. Decline unless the procedure would help
   a *different* task later: prefer false for narrow bug-specific fixes,
   pure config edits, or anything you can't state as reusable steps.
8. **Playbook consolidation** — two same-scope playbooks (A, B) may be
   near-duplicates. Are they the SAME procedure (one supersedes/absorbs
   the other), or genuinely distinct? Strict JSON only:
   `{"merge": true/false, "keep": "A|B", "title": "short",
   "trigger": "when to reach for this", "body": "merged numbered steps
   + pitfalls"}`. When `merge` is false return just `{"merge": false}`.
   Only merge when one truly subsumes the other; distinct procedures
   that merely share keywords are NOT a merge.

## Decision rules

- **High confidence action** (clear evidence in provided context):
  act. Confidence >= 0.85.
- **Low confidence** (ambiguous or missing evidence): return `wait`
  or route to operator. Confidence < 0.6 -> wait.
- **Destructive / production actions** (force-push, prod deploys,
  slot swaps, data drops, external sends): always `wait` unless
  operator has durably authorized via a learning.
- **Cross-worker file overlap**: never assign overlapping files /
  modules to two workers that share a codebase (git worktrees).
- **Redirect requires contradiction, not topical mismatch**: an
  oversight `major: redirect` is an interruption — it must cite a
  specific line from the task description that the worker's PTY
  activity actually contradicts. Surface-keyword divergence between
  the task title/description and the worker's current focus is NOT
  drift; admin endpoints, maintenance routes, and refactors are
  routine vehicles for many task types. If you cannot quote a
  contradicted line, emit `note` (or no intervention) — not
  `redirect`.

## Evidence you read

- Worker PTY tail — what they just did / are doing (primary signal).
- Task board — assigned / in_progress / completed state.
- Recent buzz log — drone actions, state changes.
- Inter-worker messages — findings, warnings, dependencies.
- Queen learnings — past operator corrections (primacy over all).

When PTY tail and drone speculation conflict, trust the PTY.

## Output

Terse structured output per the calling decision type. No narration,
no markdown preamble, no self-reference. The caller parses your
response directly. If uncertain, return the lowest-risk option (wait,
continue, done=false) rather than guessing.

## Don't

- Don't draft prose. Don't explain beyond what the caller asks.
- Don't invent task numbers, worker names, or file paths.
- Don't override durable operator instructions captured in learnings.
- Don't approve plans or destructive operations without explicit
  durable authorization.

Operate fast, tight, and conservatively. Defer to the operator when
unsure.
"""


class Queen:
    def __init__(
        self,
        config: QueenConfig | None = None,
        session_name: str = "default",
        provider: LLMProvider | None = None,
    ) -> None:
        cfg = config or QueenConfig()
        self.session_name = session_name
        self.session_id: str | None = None
        self.enabled = cfg.enabled
        self.cooldown = cfg.cooldown
        self.system_prompt = cfg.system_prompt
        self.min_confidence = cfg.min_confidence
        self.auto_assign_tasks = cfg.auto_assign_tasks
        self.usage = TokenUsage()
        self._last_call: float = 0.0
        self._last_coordination: float = 0.0
        self._lock = asyncio.Lock()
        # Session rotation: clear session after N calls or M seconds
        self._max_session_calls = cfg.max_session_calls
        self._max_session_age = cfg.max_session_age
        self._session_call_count: int = 0
        self._session_start: float = time.time()
        # Provider for headless invocations (defaults to Claude)
        if provider is None:
            from swarm.providers import get_provider

            provider = get_provider()
        self._provider = provider
        # Load persisted session ID
        self.session_id = load_session(self.session_name)
        if self.session_id:
            _log.info("restored Queen session: %s", self.session_id)

    @property
    def provider_display_name(self) -> str:
        """Human-readable name for the Queen's LLM provider."""
        return self._provider.display_name

    @property
    def can_call(self) -> bool:
        return self.enabled and time.time() - self._last_call >= self.cooldown

    @property
    def cooldown_remaining(self) -> float:
        """Seconds until the Queen can be called again."""
        remaining = self.cooldown - (time.time() - self._last_call)
        return max(0.0, remaining)

    def _clean_env(self) -> dict[str, str]:
        """Build a clean environment for headless subprocesses.

        Strips provider-specific env vars that leak from the parent session,
        preventing the child from targeting an interactive session.
        """
        prefixes = self._provider.env_strip_prefixes()
        return {k: v for k, v in os.environ.items() if not any(k.startswith(p) for p in prefixes)}

    async def _run_headless(self, args: list[str]) -> tuple[bytes, bytes, int]:
        """Run a headless LLM subprocess and return (stdout, stderr, returncode).

        Uses ``~/.swarm/queen/`` as the working directory so that the LLM CLI
        scopes Queen sessions separately from the user's project sessions.
        """
        from swarm.queen.session import STATE_DIR

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._clean_env(),
            cwd=STATE_DIR,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_DEFAULT_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            _log.warning("Queen call timed out after %ds", _DEFAULT_TIMEOUT)
            return b"", b"timeout", -1
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            _log.info("Queen call cancelled (shutdown)")
            return b"", b"cancelled", -2
        return stdout, stderr, proc.returncode or 0

    def _prepend_system_prompt(self, prompt: str) -> str:
        """Prepend operator system prompt if configured."""
        if self.system_prompt:
            return f"[Operator instructions]\n{self.system_prompt}\n\n{prompt}"
        return prompt

    def _accumulate_usage(self, result: dict[str, Any]) -> None:
        """Extract and accumulate token usage via provider-specific parsing."""
        call_usage = self._provider.parse_usage(result)
        if call_usage is not None:
            self.usage.add(call_usage)

    async def ask(
        self,
        prompt: str,
        *,
        _coordination: bool = False,
        force: bool = False,
        stateless: bool = False,
    ) -> dict[str, Any]:
        """Ask the Queen a question using claude -p with JSON output.

        When *_coordination* is True (periodic background check), the call
        uses a separate cooldown timer so it doesn't block reactive calls
        like task-completion analysis or escalation handling.

        When *force* is True (user-initiated), the cooldown is bypassed.

        When *stateless* is True, ``--resume`` is NOT used, so the call
        has no memory of previous conversations.  This prevents stale state
        from prior hive-wide analyses bleeding into per-worker queries.
        """
        prompt = self._prepend_system_prompt(prompt)

        rate_error, session_id = await self._check_rate_limit(
            force=force, _coordination=_coordination, stateless=stateless
        )
        if rate_error:
            return rate_error

        _log.info("Queen call: %d chars, session=%s", len(prompt), bool(session_id))
        call_start = time.time()

        args = self._provider.headless_command(prompt, output_format="json", session_id=session_id)

        stdout, stderr, returncode = await self._run_headless(args)
        _log.info("Queen call completed in %.1fs (rc=%d)", time.time() - call_start, returncode)
        if returncode == -1:
            return {"error": f"Queen call timed out after {_DEFAULT_TIMEOUT}s"}
        if returncode == -2:
            return {"error": "Queen call cancelled (shutdown)"}

        stdout, stderr, returncode = await self._retry_on_stale_session(
            prompt, session_id, stdout, stderr, returncode
        )
        if returncode == -1:
            return {"error": f"Queen call timed out after {_DEFAULT_TIMEOUT}s"}
        if returncode != 0:
            _log.warning("Queen process exited with code %d: %s", returncode, stderr.decode()[:200])

        return await self._parse_response(stdout)

    async def _check_rate_limit(
        self,
        *,
        force: bool,
        _coordination: bool,
        stateless: bool,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Check rate limits and rotate session if needed.

        Returns (error_dict, session_id).  error_dict is None when the call
        is allowed to proceed.
        """
        async with self._lock:
            if force:
                self._last_call = time.time()
            elif _coordination:
                if not self.enabled or time.time() - self._last_coordination < self.cooldown:
                    rem = self.cooldown - (time.time() - self._last_coordination)
                    return {"error": f"Coordination rate limited ({max(0, rem):.0f}s)"}, None
                self._last_coordination = time.time()
            else:
                if not self.can_call:
                    wait = self.cooldown_remaining
                    return {"error": f"Rate limited — try again in {wait:.0f}s"}, None
                self._last_call = time.time()
            session_id = None if stateless else self.session_id

            if session_id and self._max_session_calls > 0:
                age = time.time() - self._session_start
                if (
                    self._session_call_count >= self._max_session_calls
                    or age >= self._max_session_age
                ):
                    _log.info(
                        "Rotating Queen session (calls=%d, age=%.0fs)",
                        self._session_call_count,
                        age,
                    )
                    clear_session(self.session_name)
                    self.session_id = None
                    session_id = None
                    self._session_call_count = 0
                    self._session_start = time.time()

        return None, session_id

    async def _retry_on_stale_session(
        self,
        prompt: str,
        session_id: str | None,
        stdout: bytes,
        stderr: bytes,
        returncode: int,
    ) -> tuple[bytes, bytes, int]:
        """Retry without --resume if the session is stale."""
        if (
            returncode != 0
            and session_id
            and "No conversation found" in stderr.decode(errors="replace")
        ):
            _log.warning("Stale Queen session %s — clearing and retrying", session_id)
            clear_session(self.session_name)
            async with self._lock:
                self.session_id = None
            args = self._provider.headless_command(prompt, output_format="json")
            stdout, stderr, returncode = await self._run_headless(args)
        return stdout, stderr, returncode

    async def _parse_response(self, stdout: bytes) -> dict[str, Any]:
        """Parse Queen subprocess output into a result dict."""
        try:
            result = json.loads(stdout.decode())
            if isinstance(result, dict):
                self._accumulate_usage(result)
            if isinstance(result, dict) and "session_id" in result:
                async with self._lock:
                    self.session_id = result["session_id"]
                    self._session_call_count += 1
                    save_session(self.session_name, result["session_id"])
            inner = result.get("result", "") if isinstance(result, dict) else ""
            if isinstance(inner, str):
                parsed = _extract_json(inner)
                if isinstance(parsed, dict):
                    _log.info(
                        "Queen result: action=%s confidence=%s",
                        parsed.get("action", "N/A"),
                        parsed.get("confidence", "N/A"),
                    )
                    _log.debug("Queen response: %s", json.dumps(parsed)[:500])
                    return parsed
            return result
        except json.JSONDecodeError:
            text, sid = self._provider.parse_headless_response(stdout)
            if sid and not self.session_id:
                async with self._lock:
                    self.session_id = sid
                    save_session(self.session_name, sid)
            parsed = _extract_json(text)
            if isinstance(parsed, dict):
                return parsed
            _log.warning("Queen returned non-JSON: %s", text[:200])
            return {"result": text, "raw": True}

    async def analyze_worker(
        self,
        worker_name: str,
        worker_output: str,
        hive_context: str = "",
        *,
        force: bool = False,
        task_info: str = "",
        idle_duration_seconds: float | None = None,
        worker_state: str | None = None,
    ) -> dict[str, Any]:
        """Ask the Queen to analyze a worker and recommend action.

        Per-worker calls are **stateless** (no ``--resume``) so stale hive
        state from previous coordination calls doesn't bleed in.
        """
        hive_section = ""
        if hive_context:
            hive_section = f"""
## Full Hive State
{hive_context}
"""

        task_section = ""
        if task_info:
            task_section = f"""
## Assigned Task
{task_info}
"""

        timing_section = ""
        is_waiting = worker_state == "WAITING"
        if idle_duration_seconds is not None:
            if is_waiting:
                # WAITING workers are blocked on a prompt — they need a decision,
                # not "wait".  Don't bias the Queen toward inaction.
                timing_section = (
                    "\n## Timing\n"
                    f"Worker idle for {idle_duration_seconds:.0f}s.\n"
                    "This worker is WAITING at a permission/choice prompt and is "
                    "BLOCKED until someone responds.  Evaluate the prompt and decide:\n"
                    '- "send_message" with the correct choice (e.g. "1" for Yes) '
                    "if the action is safe\n"
                    '- "wait" only if the prompt is genuinely dangerous or unclear\n'
                    "Do NOT default to wait — the worker cannot make progress without input.\n"
                )
            else:
                timing_section = (
                    "\n## Timing\n"
                    f"Worker idle for {idle_duration_seconds:.0f}s. "
                    'Workers idle <120s are likely between steps — prefer "wait".\n'
                    "HARD RULE: If worker idle < 60 seconds, confidence MUST be below 0.50. "
                    "Returning confidence >= 0.50 for short idle is a calibration error.\n"
                    "\nOVERRIDE: If the worker output shows clear completion evidence "
                    "(commit pushed, tests passing, 'done'/'complete', task deliverable visible), "
                    'use "complete_task" regardless of idle time. '
                    "Completion evidence overrides the idle-time bias.\n"
                )

        prompt = f"""You are the Queen of a swarm of {self.provider_display_name} agents.

Analyze ONLY worker '{worker_name}'. Do NOT reference or make claims about
other workers — you have no information about them in this call.

Note: Drones handle routine approvals automatically using configured rules.
Escalated choices (destructive operations) are sent to you for review.
Low-confidence assessments will be presented to the operator for confirmation.

IMPORTANT: If the worker is presenting a plan for approval (plan mode),
you MUST set confidence to 0.0 and action to "wait". Plans always require
human review — never auto-approve or auto-reject a plan.

Current worker output (recent):
```
{worker_output}
```
{hive_section}{task_section}{timing_section}
Analyze the situation and respond with ONLY a JSON object (no extra text):
{{
  "assessment": "brief description of what's happening with THIS worker",
  "action": "continue" | "send_message" | "complete_task" | "restart" | "wait",
  "message": "message to send if action is send_message",
  "reasoning": "why you chose this action",
  "confidence": 0.0 to 1.0 — calibrate as a PRECISE decimal:
    0.93-0.97: Absolutely certain (explicit evidence in output)
    0.83-0.89: High confidence, clear evidence, minor uncertainty
    0.73-0.79: Reasonable but notable ambiguity
    0.50-0.65: Genuinely uncertain
    Below 0.40: Very low confidence — flag for human
    CRITICAL: Never use 0.80, 0.70, 0.90, 0.60 — these round numbers
    indicate lazy calibration. Use 0.82, 0.73, 0.91, 0.64 instead.
}}

Action guide:
- "continue": Press Enter to accept a prompt/choice (worker waiting for input)
- "send_message": Send a specific message to the worker
- "complete_task": The assigned task is DONE — worker shows evidence of completion
  (commits pushed, tests passing, deployment succeeded, explicit "done" message).
  Use this when the worker is idle at prompt and the task output shows success.
- "restart": Restart the worker (crashed/stuck)
- "wait": No action needed right now"""
        _log.info("Queen.analyze_worker(%s)", worker_name)
        # Per-worker analysis is stateless to avoid stale hive-state memory.
        # Escalation calls (with hive_context) use the session for continuity.
        use_session = bool(hive_context)
        return await self.ask(prompt, force=force, stateless=not use_session)

    async def assign_tasks(
        self,
        idle_workers: list[str],
        available_tasks: list[dict[str, Any]],
        hive_context: str = "",
    ) -> list[dict[str, Any]]:
        """Ask the Queen to match idle workers to available tasks.

        Returns a list of assignments: [{"worker": str, "task_id": str, "message": str}]
        """
        _log.info(
            "Queen.assign_tasks: %d workers, %d tasks", len(idle_workers), len(available_tasks)
        )
        if not idle_workers or not available_tasks:
            return []

        task_lines: list[str] = []
        for t in available_tasks:
            task_type = t.get("task_type", "chore")
            line = f"- [{t['id']}] {t['title']} (priority={t['priority']}, type={task_type})"
            desc = t.get("description", "")
            if desc:
                task_lines.append(line)
                task_lines.append(f"  Description: {desc}")
            else:
                task_lines.append(line)
            attachments = t.get("attachments", [])
            if attachments:
                fnames = [a.rsplit("/", 1)[-1] for a in attachments]
                task_lines.append(f"  Attachments: {', '.join(fnames)}")
            tags = t.get("tags", [])
            if tags:
                task_lines.append(f"  Tags: {', '.join(tags)}")
        tasks_desc = "\n".join(task_lines)
        workers_desc = ", ".join(idle_workers)

        ctx_section = f"\n## Hive Context\n{hive_context}" if hive_context else ""

        prompt = f"""You are the Queen of a swarm of {self.provider_display_name} agents.

Idle workers needing tasks: {workers_desc}

Available tasks:
{tasks_desc}
{ctx_section}

Match idle workers to the most appropriate available tasks.
Use worker descriptions/paths and task content to find the best match.
Not every worker needs a task — only assign if there's a good match.
Drones have approval rules configured and will auto-handle routine choices.
Escalated choices will come back for your review.

Each task has a "type" field (bug, verify, feature, chore). Tailor your instructions accordingly:
- bug: TDD workflow — trace root cause, write failing test, minimal fix, validate, commit
- verify: Pull latest, run tests, verify specific behavior, report pass/fail (no code changes)
- feature: Read existing patterns, implement minimally, write tests, validate, commit
- chore: Complete the task, validate, commit

Your "message" field is the ONLY instruction the worker receives. Include:
- The full task description
- Attachment file paths (if any)
- Workflow instructions matching the task type
- Clear instructions on what to do

Respond with a JSON object:
{{
  "assignments": [
    {{
      "worker": "worker_name",
      "task_id": "task_id",
      "message": "full task instructions for the worker",
      "confidence": 0.0 to 1.0 — calibrate as a PRECISE decimal:
        0.93-0.97: Perfect match (worker skills align exactly, task is clear)
        0.83-0.89: Strong match with clear evidence
        0.73-0.79: Reasonable match but some ambiguity
        0.50-0.65: Uncertain — could assign to multiple workers
        Below 0.40: Poor match — flag for human review
        CRITICAL: Never use 0.80, 0.70, 0.90, 0.60 — these round numbers
        indicate lazy calibration. Use 0.82, 0.73, 0.91, 0.64 instead.
    }}
  ],
  "reasoning": "brief explanation of matching logic"
}}"""
        # Task assignment is critical — bypass the general cooldown so idle
        # workers aren't starved by a recent escalation or coordination call.
        result = await self.ask(prompt, force=True)
        if isinstance(result, dict):
            return result.get("assignments", [])
        return []

    async def draft_email_reply(self, task_title: str, task_type: str, resolution: str) -> str:
        """Draft a short, professional email reply for a completed task.

        Returns plain text suitable for the Graph API ``comment`` field.
        Falls back to a simple default if Claude fails.
        """
        prompt = (
            "Draft a brief, professional email reply (2-4 sentences) explaining "
            "what was done. Keep it non-technical and friendly. Do NOT include a "
            "subject line, greeting, or sign-off — just the reply body.\n\n"
            f"Task: {task_title}\n"
            f"Type: {task_type}\n"
            f"Resolution: {resolution}\n\n"
            "Return ONLY the reply text, nothing else."
        )
        args = self._provider.headless_command(prompt, output_format="text", max_turns=1)
        stdout, _stderr, returncode = await self._run_headless(args)
        if returncode == 0 and stdout.strip():
            return stdout.decode().strip()
        _log.warning("draft_email_reply failed (rc=%d), using fallback", returncode)
        return (
            f"This has been addressed. {resolution}" if resolution else "This has been addressed."
        )

    # ``coordinate_hive`` removed in task #253 follow-up (Task B of
    # docs/specs/headless-queen-architecture.md).  The periodic hive-
    # coordination cycle was redundant with IdleWatcher,
    # InterWorkerMessageWatcher, FileOwnership, and PressureManager — all
    # specialized drones that cover the same anomaly-detection surface more
    # cheaply.  If a legitimate use case resurfaces, prefer a dedicated
    # drone over a cross-worker LLM sweep.
