"""Tiered verifier drone — adversarial post-completion check.

Item 4 of the 10-repo research bundle (plan
``~/.claude/plans/sequential-churning-meerkat.md``).

Drift in multi-agent flows compounds: N workers means N opportunities
for "I'm done" claims that don't actually match the spec. This drone
fires asynchronously after every ``swarm_complete_task`` and either
confirms the work shipped clean or reopens the task with the verifier's
findings as a peer warning.

Two tiers, by design:

**Tier 1 — deterministic (no LLM, fast, free):**

  - Empty git diff since task start? → reopen.
  - No ``/check`` evidence in the worker's recent buzz log? → reopen.
  - Open peer warning/blocker on this task? → reopen.

  Any tier-1 fail short-circuits — we never burn an LLM call when the
  failure is mechanically obvious. Most rejections happen here.

**Tier 2 — dedicated subprocess (LLM, gray area only):**

  Read-only verifier role (``swarm.queen.verifier`` — separate from
  headless Queen per ``docs/specs/headless-queen-architecture.md``)
  reads task spec + diff + resolution and returns
  VERIFIED / UNCERTAIN / FAILED. UNCERTAIN treated as PASS, logged for
  audit. Default-pass when no objective criteria are present.

**Self-loop guard:** ``VERIFIER_MAX_REOPENS`` (= 2). After the second
verifier reopen still failing, the drone escalates to the operator via
a Queen thread instead of reopening a third time. By that point either
the verifier is wrong or the worker can't fix it without human input —
either way, human.

**Skip on force-complete:** the daemon's ``complete_task(verify=False)``
path lets ``queen_force_complete_task`` opt out of verification.
Operators who explicitly override completion with a documented reason
should not have the verifier second-guessing them.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from swarm.drones.log import LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.tasks.task import TaskType, VerificationStatus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from swarm.drones.log import DroneEntry, DroneLog, SystemEntry
    from swarm.queen.verifier import VerifierClient
    from swarm.tasks.board import TaskBoard
    from swarm.tasks.task import SwarmTask


_log = get_logger("drones.verifier")

# Default self-loop guard. After the verifier reopens a task this many times
# in a row and the resulting completion still fails verification, the drone
# escalates to the operator instead of reopening again. Per the plan: "by
# reopen 3, either the verifier is wrong or the worker can't fix it without
# human input — either way, human." Now overridable per-deployment via
# ``DroneConfig.verify_reopen_cap`` (this constant is the fallback default).
VERIFIER_MAX_REOPENS = 2

# Task types that legitimately produce NO git diff (research write-ups,
# content, external-system changes, operator actions). For these the tier-1
# empty-diff / no-/check-evidence gates don't apply — the verifier grades the
# resolution text instead of a diff. Everything else is treated as
# code-producing and must show a diff + validation evidence.
_NO_DIFF_TASK_TYPES: frozenset[TaskType] = frozenset(
    {TaskType.CONTENT, TaskType.REVIEW, TaskType.PUBLISH, TaskType.INGEST, TaskType.OPERATOR}
)

# How many recent buzz-log entries to inspect when looking for ``/check``
# evidence. Workers typically run /check immediately before completing,
# so a short window keeps the cost trivial without missing recent runs.
_CHECK_LOOKBACK = 30

# Tokens that indicate the worker actually ran the project's validation
# suite. Conservative — we'd rather false-negative on this check (and
# miss reopening a task that did run /check via a different signal) than
# false-positive (rubber-stamp a worker who skipped /check).
_CHECK_EVIDENCE_TOKENS: tuple[str, ...] = (
    "/check",
    "ruff format",
    "ruff check",
    "uv run pytest",
    "pytest",
    "npm run test",
    "npm run check",
)


def _format_verification_reason(
    *,
    prose: str,
    criteria_results: list[dict],
) -> str:
    """Combine the LLM's prose verdict with the failed-criterion summary.

    Surfaces *which* acceptance criteria the diff missed when the
    verifier returned per-criterion verdicts. Passed criteria don't
    pollute the reason. When the criteria list is empty, or all
    criteria pass, the prose is returned unchanged so backwards-compat
    callers see no behaviour change.
    """
    failed = [c.get("text", "") for c in criteria_results if c.get("passed") is False]
    if not failed:
        return prose
    cited = ", ".join(f"'{c}'" for c in failed if c)
    if not cited:
        return prose
    return f"{prose} (failed criteria: {cited})"


class VerifierDrone:
    """Adversarial completion-checker that fires after ``swarm_complete_task``.

    Parameters
    ----------
    drone_log:
        Every tier-1 verdict, tier-2 verdict, reopen, and escalation is
        appended under :data:`LogCategory.VERIFIER`.
    task_board:
        Source of truth for task state — used to reopen + look up peer
        warnings on the task.
    verifier_client:
        Tier-2 LLM judge. Stateless ``claude -p`` subprocess wrapper.
    diff_provider:
        Async ``(task) -> str`` that returns the git diff produced for
        this task. Daemon wires this to ``git diff <start_sha>..HEAD``
        scoped to the worker's repo.
    check_evidence_provider:
        Sync ``(worker_name) -> bool`` that returns True when the
        worker's recent buzz log shows evidence of a ``/check`` run.
        Defaults to a permissive stub when None (tests without the full
        wiring still exercise verdict paths).
    peer_warnings_provider:
        Sync ``(task_id) -> str`` returning any unresolved peer warnings
        addressed to this task. Empty string = clean.
    send_warning:
        Async callable used to deliver verifier findings to the worker
        as a ``swarm_send_message(msg_type="warning", from_="verifier")``.
        Mirrors the messaging surface the IdleWatcher uses to nudge.
    escalate_to_operator:
        Async callable invoked when the self-loop guard trips. Daemon
        wires this to a Queen thread (kind=verifier-escalation) so the
        operator sees the runaway in the dashboard.
    """

    def __init__(
        self,
        *,
        drone_log: DroneLog,
        task_board: TaskBoard,
        verifier_client: VerifierClient,
        diff_provider: Callable[[SwarmTask], Awaitable[str]],
        check_evidence_provider: Callable[[str], bool] | None = None,
        peer_warnings_provider: Callable[[str], str] | None = None,
        send_warning: Callable[..., Awaitable[None]] | None = None,
        escalate_to_operator: Callable[..., Awaitable[None]] | None = None,
        on_verdict: Callable[..., Awaitable[None]] | None = None,
        enforce: bool = True,
        max_reopens: int = VERIFIER_MAX_REOPENS,
    ) -> None:
        self._drone_log = drone_log
        self._task_board = task_board
        self._verifier = verifier_client
        self._diff_provider = diff_provider
        self._check_evidence = check_evidence_provider or (lambda _name: True)
        self._peer_warnings = peer_warnings_provider or (lambda _tid: "")
        self._send_warning = send_warning
        self._escalate = escalate_to_operator
        # Phase 2 playbook outcome attribution: invoked once per completed
        # verification with the terminal status. Decoupled — the verifier
        # knows nothing about playbooks; the daemon wires this.
        self._on_verdict = on_verdict
        # Shadow mode (enforce=False): compute + record verdicts but NEVER
        # reopen or escalate. The default-off rollout for a gate that has
        # never run in production — the operator flips
        # ``DroneConfig.verifier_enforce`` on from the dashboard once the
        # recorded verdict stream looks trustworthy.
        self._enforce = enforce
        self._max_reopens = max_reopens

    async def verify_completion(self, task: SwarmTask) -> VerificationStatus:
        """Run tier-1 then tier-2 verification on ``task``.

        Returns the verification status that was applied. Side effects:
        updates ``task.verification_*`` fields, may reopen the task,
        may send a worker warning, may escalate to the operator. Caller
        is the daemon's ``complete_task`` path; failure to fire (e.g.,
        verifier subprocess crashes) is logged and treated as a default
        pass — we never block ``complete_task`` from returning.
        """
        # Tier 1 — deterministic short-circuits
        tier1 = await self._tier1(task)
        if tier1 is not None:
            await self._handle_negative(task, reason=tier1, source="tier1")
            return task.verification_status
        self._log(
            SystemAction.VERIFIER_TIER1_PASSED,
            task,
            f"#{task.number}: tier-1 checks passed",
        )

        # Tier 2 — LLM judgment for gray-area cases
        try:
            verdict = await self._verifier.verify(
                task_title=task.title,
                task_description=task.description,
                acceptance_criteria=task.acceptance_criteria,
                diff=await self._diff_provider(task),
                resolution=task.resolution,
                peer_warnings=self._peer_warnings(task.id),
            )
        except Exception:
            _log.warning("verifier subprocess raised for task #%d", task.number, exc_info=True)
            # Treat hard failures as PASS so the verifier never silently
            # blocks completion when the LLM path is unreachable. The
            # operator still sees the completion in the dashboard.
            task.verification_status = VerificationStatus.VERIFIED
            task.verification_reason = "verifier subprocess error — default pass"
            self._log(
                SystemAction.VERIFIER_TIER2_UNCERTAIN,
                task,
                f"#{task.number}: subprocess error → default pass",
            )
            return VerificationStatus.VERIFIED

        if verdict.is_failed:
            failed_reason = _format_verification_reason(
                prose=verdict.reason,
                criteria_results=verdict.criteria_results,
            )
            await self._handle_negative(
                task,
                reason=f"tier-2 FAILED: {failed_reason}",
                source="tier2",
            )
            return task.verification_status

        # VERIFIED or UNCERTAIN both pass; only the buzz-log action differs.
        if verdict.verdict == "VERIFIED":
            action = SystemAction.VERIFIER_TIER2_VERIFIED
        else:
            action = SystemAction.VERIFIER_TIER2_UNCERTAIN
        task.verification_status = VerificationStatus.VERIFIED
        task.verification_reason = _format_verification_reason(
            prose=verdict.reason,
            criteria_results=verdict.criteria_results,
        )
        self._log(
            action,
            task,
            f"#{task.number}: {verdict.verdict} — {task.verification_reason}",
        )
        return VerificationStatus.VERIFIED

    @staticmethod
    def _expects_diff(task: SwarmTask) -> bool:
        """Whether this task type is expected to produce a git diff.

        Research/content/external-system/operator tasks legitimately produce
        none — for those the empty-diff and /check-evidence tier-1 gates don't
        apply and tier-2 grades the resolution text instead.
        """
        return task.task_type not in _NO_DIFF_TASK_TYPES

    async def _handle_negative(self, task: SwarmTask, *, reason: str, source: str) -> None:
        """Act on a failing verdict — reopen/escalate when enforcing, else shadow-record.

        In shadow mode (``enforce=False``) the would-be reopen is logged for
        the Harness metrics and the reason is stamped for the dashboard, but the
        task's lifecycle is left untouched — nothing is reopened or escalated.
        """
        if self._enforce:
            await self._reopen_or_escalate(task, reason=reason, source=source)
            return
        task.verification_reason = f"[shadow] would reopen: {reason}"
        self._log(
            SystemAction.VERIFIER_SHADOW_WOULD_REOPEN,
            task,
            f"#{task.number}: [shadow] would reopen ({source}) — {reason}",
        )

    async def _tier1(self, task: SwarmTask) -> str | None:
        """Run all tier-1 deterministic checks. Returns reason on first fail.

        Each check is independent. Returning the FIRST failure short-
        circuits the rest — once we've decided to reopen, we don't need
        to keep collecting reasons. If all checks pass, returns None.
        """
        # Checks 1 & 2 only apply to code-producing tasks. A no-diff task
        # (research, content, operator action) is handed straight to tier-2,
        # which grades its resolution text against the acceptance criteria.
        if self._expects_diff(task):
            # 1. Empty diff
            try:
                diff = await self._diff_provider(task)
            except Exception:
                _log.warning("diff provider raised for task #%d", task.number, exc_info=True)
                diff = ""
            if not diff.strip():
                return "tier-1: no diff produced — completion has no code change"

            # 2. /check evidence
            worker = task.assigned_worker or ""
            if worker and not self._check_evidence(worker):
                return "tier-1: no /check evidence in recent buzz log"

        # 3. Unresolved peer warning (applies to every task)
        peer = self._peer_warnings(task.id).strip()
        if peer:
            return f"tier-1: unresolved peer warning — {peer[:200]}"

        return None

    async def _reopen_or_escalate(
        self,
        task: SwarmTask,
        *,
        reason: str,
        source: str,
    ) -> None:
        """Apply the reopen verdict, escalating instead if the guard tripped."""
        # Self-loop guard fires BEFORE the reopen counter is incremented.
        # Counter is the number of *previous* verifier reopens; we
        # escalate when the next reopen would push it past the limit.
        if task.verification_reopen_count >= self._max_reopens:
            await self._escalate_now(task, reason=reason, source=source)
            return
        task.reopen_for_verifier(reason=reason)
        # Persist the reopen on the board so dashboard + idle-watcher see
        # the ASSIGNED status flip immediately. The task object is the
        # one already living in board._tasks — we only need to surface
        # the mutation.
        self._task_board.persist(task)
        action = (
            SystemAction.VERIFIER_TIER1_REOPENED
            if source == "tier1"
            else SystemAction.VERIFIER_TIER2_REOPENED
        )
        self._log(action, task, f"#{task.number}: reopened — {reason}")
        await self._notify_worker(task, reason=reason)

    async def _escalate_now(
        self,
        task: SwarmTask,
        *,
        reason: str,
        source: str,
    ) -> None:
        """Self-loop guard tripped — surface to the operator instead."""
        task.verification_status = VerificationStatus.ESCALATED
        task.verification_reason = reason
        task.updated_at = time.time()
        self._task_board.persist(task)
        self._log(
            SystemAction.VERIFIER_ESCALATED,
            task,
            (
                f"#{task.number}: escalated after {self._max_reopens} reopens "
                f"(source={source}) — {reason}"
            ),
            is_notification=True,
        )
        if self._escalate is None:
            return
        try:
            await self._escalate(
                task=task,
                reason=reason,
                reopen_count=task.verification_reopen_count,
            )
        except Exception:
            _log.warning(
                "verifier escalation callback raised for task #%d",
                task.number,
                exc_info=True,
            )

    async def _notify_worker(self, task: SwarmTask, *, reason: str) -> None:
        """Send the verifier's findings to the worker as a peer warning."""
        if self._send_warning is None or not task.assigned_worker:
            return
        body = (
            f"Verifier reopened task #{task.number} — your previous completion "
            f"didn't pass. Reason: {reason}\n\n"
            "The task is back in your queue (status=ASSIGNED). The IdleWatcher "
            "will nudge you on its next sweep; address the verifier's finding "
            "and re-complete when fixed."
        )
        try:
            await self._send_warning(
                to=task.assigned_worker,
                msg_type="warning",
                content=body,
                from_="verifier",
            )
        except Exception:
            _log.warning("verifier warning send failed for task #%d", task.number, exc_info=True)

    def _log(
        self,
        action: SystemAction,
        task: SwarmTask,
        detail: str,
        *,
        is_notification: bool = False,
    ) -> None:
        """Append an entry under ``LogCategory.VERIFIER``."""
        self._drone_log.add(
            action,
            task.assigned_worker or "verifier",
            detail,
            category=LogCategory.VERIFIER,
            is_notification=is_notification,
            metadata={
                "task_id": task.id,
                "task_number": task.number,
                "reopen_count": task.verification_reopen_count,
            },
        )


async def fire_and_forget(drone: VerifierDrone, task: SwarmTask) -> None:
    """Run :meth:`VerifierDrone.verify_completion` without bubbling exceptions.

    Daemons schedule this with ``asyncio.create_task`` so the verifier
    never blocks ``complete_task`` from returning. Any exception is
    logged and swallowed — the worker has already shipped, the
    verifier is best-effort safety net.
    """
    try:
        status = await drone.verify_completion(task)
        if drone._on_verdict is not None:
            try:
                await drone._on_verdict(task, status)
            except Exception:
                _log.warning("verifier on_verdict hook raised for #%d", task.number, exc_info=True)
    except Exception:
        _log.warning("verifier fire-and-forget raised for #%d", task.number, exc_info=True)


def has_check_evidence(buzz_entries: list[DroneEntry | SystemEntry], worker_name: str) -> bool:
    """Inspect recent buzz-log entries for ``/check`` evidence by *worker_name*.

    Helper extracted so the daemon can wire :class:`VerifierDrone` with a
    closure over the live drone log without importing the drone-specific
    knowledge of "what does evidence look like".

    A "/check" run leaves traces in multiple places (slash-command
    invocation, ruff format/lint output captured by tool hooks, pytest
    invocations). We accept any of the well-known tokens in the recent
    buzz tail; matching is intentionally loose — false negatives
    (genuine /check missed) cause a tier-1 reopen the worker can argue
    against; false positives silently rubber-stamp.
    """
    needle_set = _CHECK_EVIDENCE_TOKENS
    matched = 0
    for entry in reversed(buzz_entries[-_CHECK_LOOKBACK:]):
        if getattr(entry, "worker_name", "") != worker_name:
            continue
        detail = (getattr(entry, "detail", "") or "").lower()
        if any(token in detail for token in needle_set):
            return True
        matched += 1
        if matched >= _CHECK_LOOKBACK:
            break
    return False


async def safe_git_diff(repo_path: str, base_ref: str = "HEAD~1") -> str:
    """Best-effort ``git diff`` for tier-1 / tier-2 inputs.

    Tier-1 wants "is there ANY diff"; tier-2 wants the diff text itself.
    Both run through this helper so the implementation lives in one
    place. ``base_ref`` defaults to ``HEAD~1`` because workers ideally
    commit before completing; daemon callers pass the recorded
    task-start SHA when available for tighter scoping.

    Returns an empty string on any subprocess failure; tier-1 treats
    empty as "no diff produced" which is the conservative reopen case.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            base_ref,
            "--",
            ".",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=repo_path,
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except (OSError, TimeoutError):
        return ""
    return stdout.decode(errors="replace") if proc.returncode == 0 else ""
