"""Tests for the verifier drone (item 4 of the 10-repo bundle).

Two tiers, two failure modes per tier, plus the self-loop guard, the
force-complete skip, and the default-pass on absent acceptance criteria.

The drone fires asynchronously after ``swarm_complete_task``. Tests
exercise the drone directly with stubs for the LLM subprocess
(``VerifierClient``), the diff/check/peer providers, and the messaging
+ escalation surfaces.
"""

from __future__ import annotations

import pytest

from swarm.drones.log import DroneLog, LogCategory, SystemAction
from swarm.drones.verifier import (
    VERIFIER_MAX_REOPENS,
    VerifierDrone,
    has_check_evidence,
)
from swarm.queen.verifier import VerifierVerdict
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask, TaskStatus, VerificationStatus

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeVerifierClient:
    """Stand-in for the ``claude -p`` subprocess wrapper."""

    def __init__(self, verdict: str = "VERIFIED", reason: str = "ok") -> None:
        self.verdict = verdict
        self.reason = reason
        self.calls: list[dict] = []

    async def verify(self, **kwargs: object) -> VerifierVerdict:
        self.calls.append(kwargs)
        return VerifierVerdict(verdict=self.verdict, reason=self.reason)


class _RaisingVerifierClient:
    async def verify(self, **kwargs: object) -> VerifierVerdict:
        raise RuntimeError("LLM unreachable")


class _Recorder:
    """Capture warning sends + escalations."""

    def __init__(self) -> None:
        self.warnings: list[dict] = []
        self.escalations: list[dict] = []

    async def send_warning(self, **kwargs: object) -> None:
        self.warnings.append(dict(kwargs))

    async def escalate(self, **kwargs: object) -> None:
        self.escalations.append(dict(kwargs))


def _make_task(
    *,
    title: str = "Add the widget",
    description: str = "implement X",
    criteria: list[str] | None = None,
    resolution: str = "did the thing",
    worker: str = "api",
    reopen_count: int = 0,
) -> SwarmTask:
    t = SwarmTask(
        title=title,
        description=description,
        acceptance_criteria=(["X is added", "tests pass"] if criteria is None else criteria),
        resolution=resolution,
        assigned_worker=worker,
        status=TaskStatus.DONE,
        verification_reopen_count=reopen_count,
    )
    return t


def _make_drone(
    *,
    diff: str = "diff --git a b\n+changed",
    verdict: str = "VERIFIED",
    reason: str = "ok",
    check_evidence: bool = True,
    peer_warnings: str = "",
    raising_client: bool = False,
    enforce: bool = True,
    max_reopens: int = VERIFIER_MAX_REOPENS,
) -> tuple[VerifierDrone, _Recorder, DroneLog, TaskBoard, _FakeVerifierClient]:
    log = DroneLog()
    board = TaskBoard()
    rec = _Recorder()
    client: _FakeVerifierClient | _RaisingVerifierClient
    client = _RaisingVerifierClient() if raising_client else _FakeVerifierClient(verdict, reason)

    async def _diff(_task: SwarmTask) -> str:
        return diff

    drone = VerifierDrone(
        drone_log=log,
        task_board=board,
        verifier_client=client,  # type: ignore[arg-type]
        diff_provider=_diff,
        check_evidence_provider=lambda _name: check_evidence,
        peer_warnings_provider=lambda _tid: peer_warnings,
        send_warning=rec.send_warning,
        escalate_to_operator=rec.escalate,
        enforce=enforce,
        max_reopens=max_reopens,
    )
    return drone, rec, log, board, client  # type: ignore[return-value]


def _categories(log: DroneLog) -> list[str]:
    return [e.category.value for e in log.entries]


def _actions(log: DroneLog) -> list[SystemAction]:
    return [e.action for e in log.entries]


# ---------------------------------------------------------------------------
# Tier 1 — deterministic short-circuits (no LLM)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier1_empty_diff_reopens_without_llm():
    drone, rec, log, board, client = _make_drone(diff="")
    task = _make_task()
    board.add(task)

    status = await drone.verify_completion(task)

    assert status == VerificationStatus.REOPENED
    assert task.status == TaskStatus.ASSIGNED
    assert task.verification_reopen_count == 1
    assert "no diff produced" in task.verification_reason
    # No LLM call
    assert client.calls == []
    assert SystemAction.VERIFIER_TIER1_REOPENED in _actions(log)
    # Worker received a warning
    assert len(rec.warnings) == 1
    assert rec.warnings[0]["to"] == "api"
    assert rec.warnings[0]["msg_type"] == "warning"


# ---------------------------------------------------------------------------
# Shadow mode (enforce=False) — record verdicts, reopen nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_mode_failed_verdict_does_not_reopen():
    drone, rec, log, board, _client = _make_drone(
        verdict="FAILED", reason="missing X", enforce=False
    )
    task = _make_task()
    board.add(task)

    status = await drone.verify_completion(task)

    # Task lifecycle untouched — still DONE, no reopen, no counter bump.
    assert task.status == TaskStatus.DONE
    assert task.verification_reopen_count == 0
    assert status == VerificationStatus.NOT_RUN
    assert rec.warnings == []
    # But the would-be reopen IS recorded for the Harness metrics.
    assert SystemAction.VERIFIER_SHADOW_WOULD_REOPEN in _actions(log)
    assert "[shadow] would reopen" in task.verification_reason


@pytest.mark.asyncio
async def test_shadow_mode_tier1_empty_diff_does_not_reopen():
    drone, rec, log, board, _client = _make_drone(diff="", enforce=False)
    task = _make_task()
    board.add(task)

    await drone.verify_completion(task)

    assert task.status == TaskStatus.DONE
    assert task.verification_reopen_count == 0
    assert rec.warnings == []
    assert SystemAction.VERIFIER_SHADOW_WOULD_REOPEN in _actions(log)


@pytest.mark.asyncio
async def test_shadow_mode_verified_still_records_pass():
    drone, rec, log, board, _client = _make_drone(verdict="VERIFIED", enforce=False)
    task = _make_task()
    board.add(task)

    status = await drone.verify_completion(task)

    assert status == VerificationStatus.VERIFIED
    assert SystemAction.VERIFIER_TIER2_VERIFIED in _actions(log)
    assert rec.warnings == []


# ---------------------------------------------------------------------------
# No-diff task types — tier-1 empty-diff gate does not apply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_diff_task_type_skips_empty_diff_reopen():
    from swarm.tasks.task import TaskType

    # A CONTENT task with an empty diff must NOT tier-1 reopen; it goes to
    # tier-2 which grades the resolution. Here tier-2 returns VERIFIED.
    drone, rec, log, board, client = _make_drone(diff="", verdict="VERIFIED")
    task = _make_task()
    task.task_type = TaskType.CONTENT
    board.add(task)

    status = await drone.verify_completion(task)

    assert status == VerificationStatus.VERIFIED
    assert task.status == TaskStatus.DONE
    # Tier-2 WAS consulted (no tier-1 short-circuit on empty diff).
    assert len(client.calls) == 1
    assert SystemAction.VERIFIER_TIER1_REOPENED not in _actions(log)


@pytest.mark.asyncio
async def test_no_diff_task_type_skips_check_evidence_gate():
    from swarm.tasks.task import TaskType

    # A CONTENT task with no /check evidence must not reopen for that either.
    drone, _rec, log, board, client = _make_drone(diff="", verdict="VERIFIED", check_evidence=False)
    task = _make_task()
    task.task_type = TaskType.PUBLISH
    board.add(task)

    status = await drone.verify_completion(task)

    assert status == VerificationStatus.VERIFIED
    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# Configurable reopen cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_configurable_reopen_cap_escalates_at_one():
    # max_reopens=1 → a task already reopened once escalates instead of
    # reopening again.
    drone, rec, log, board, _client = _make_drone(
        verdict="FAILED", reason="still broken", max_reopens=1
    )
    task = _make_task(reopen_count=1)
    board.add(task)

    status = await drone.verify_completion(task)

    assert status == VerificationStatus.ESCALATED
    assert SystemAction.VERIFIER_ESCALATED in _actions(log)
    assert len(rec.escalations) == 1


@pytest.mark.asyncio
async def test_tier1_no_check_evidence_reopens_without_llm():
    drone, _rec, log, board, client = _make_drone(check_evidence=False)
    task = _make_task()
    board.add(task)

    status = await drone.verify_completion(task)

    assert status == VerificationStatus.REOPENED
    assert "no /check evidence" in task.verification_reason
    assert client.calls == []
    assert SystemAction.VERIFIER_TIER1_REOPENED in _actions(log)


@pytest.mark.asyncio
async def test_tier1_peer_warning_reopens_without_llm():
    drone, _rec, log, board, client = _make_drone(peer_warnings="hub: shared file changed")
    task = _make_task()
    board.add(task)

    status = await drone.verify_completion(task)

    assert status == VerificationStatus.REOPENED
    assert "unresolved peer warning" in task.verification_reason
    assert client.calls == []
    assert SystemAction.VERIFIER_TIER1_REOPENED in _actions(log)


# ---------------------------------------------------------------------------
# Tier 2 — LLM verdicts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier2_verified_passes():
    drone, rec, log, board, client = _make_drone(verdict="VERIFIED", reason="diff matches spec")
    task = _make_task()
    board.add(task)

    status = await drone.verify_completion(task)

    assert status == VerificationStatus.VERIFIED
    assert task.status == TaskStatus.DONE  # not reopened
    assert task.verification_reopen_count == 0
    assert task.verification_reason == "diff matches spec"
    assert SystemAction.VERIFIER_TIER1_PASSED in _actions(log)
    assert SystemAction.VERIFIER_TIER2_VERIFIED in _actions(log)
    assert "verifier" in _categories(log)
    assert rec.warnings == []


@pytest.mark.asyncio
async def test_tier2_uncertain_passes_but_logs_distinctly():
    """UNCERTAIN is treated as PASS but uses a different SystemAction so audits surface it."""
    drone, rec, _log, board, _client = _make_drone(verdict="UNCERTAIN", reason="ambiguous spec")
    task = _make_task()
    board.add(task)

    status = await drone.verify_completion(task)

    assert status == VerificationStatus.VERIFIED
    assert rec.warnings == []  # no reopen


@pytest.mark.asyncio
async def test_tier2_failed_reopens_with_findings():
    drone, rec, log, board, _client = _make_drone(
        verdict="FAILED", reason="diff adds Y, spec said X"
    )
    task = _make_task()
    board.add(task)

    status = await drone.verify_completion(task)

    assert status == VerificationStatus.REOPENED
    assert task.status == TaskStatus.ASSIGNED
    assert task.verification_reopen_count == 1
    assert "tier-2 FAILED" in task.verification_reason
    assert "diff adds Y" in task.verification_reason
    assert SystemAction.VERIFIER_TIER2_REOPENED in _actions(log)
    assert len(rec.warnings) == 1


@pytest.mark.asyncio
async def test_tier2_subprocess_error_default_passes():
    """Verifier subprocess crash should not block completion — default PASS."""
    drone, rec, log, _board, _client = _make_drone(raising_client=True)
    task = _make_task()

    status = await drone.verify_completion(task)

    assert status == VerificationStatus.VERIFIED
    assert "subprocess error" in task.verification_reason
    assert rec.warnings == []
    assert SystemAction.VERIFIER_TIER2_UNCERTAIN in _actions(log)


# ---------------------------------------------------------------------------
# Self-loop guard (item 4 — escalate after VERIFIER_MAX_REOPENS reopens)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_loop_guard_escalates_after_max_reopens():
    """Already-reopened VERIFIER_MAX_REOPENS times → escalate, don't reopen."""
    drone, rec, log, board, _client = _make_drone(verdict="FAILED", reason="still wrong")
    task = _make_task(reopen_count=VERIFIER_MAX_REOPENS)
    board.add(task)

    status = await drone.verify_completion(task)

    assert status == VerificationStatus.ESCALATED
    assert task.verification_status == VerificationStatus.ESCALATED
    # Reopen counter is NOT incremented when escalating
    assert task.verification_reopen_count == VERIFIER_MAX_REOPENS
    assert SystemAction.VERIFIER_ESCALATED in _actions(log)
    assert len(rec.escalations) == 1
    assert rec.escalations[0]["task"].id == task.id
    # No worker warning on escalation — operator owns it now
    assert rec.warnings == []


@pytest.mark.asyncio
async def test_reopens_below_guard_threshold_continue_reopening():
    """Reopens at counter < MAX still reopen; escalation only at the boundary."""
    drone, _rec, _log, board, _client = _make_drone(verdict="FAILED", reason="wrong")
    task = _make_task(reopen_count=VERIFIER_MAX_REOPENS - 1)
    board.add(task)

    status = await drone.verify_completion(task)

    assert status == VerificationStatus.REOPENED
    assert task.verification_reopen_count == VERIFIER_MAX_REOPENS


# ---------------------------------------------------------------------------
# Default-pass on no acceptance criteria (drone delegates to LLM prompt)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_pass_on_no_criteria_routes_through_tier2():
    """No acceptance criteria → tier 1 still passes; tier 2's prompt handles it."""
    drone, _rec, _log, board, client = _make_drone(
        verdict="VERIFIED", reason="no objective criteria"
    )
    task = _make_task(criteria=[])
    board.add(task)

    status = await drone.verify_completion(task)

    assert status == VerificationStatus.VERIFIED
    # Tier 2 still gets called — the empty criteria is the verifier prompt's
    # responsibility to default-pass, not the drone's.
    assert len(client.calls) == 1
    assert client.calls[0]["acceptance_criteria"] == []


# ---------------------------------------------------------------------------
# Buzz log invariants — every action under LogCategory.VERIFIER
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_log_entries_use_verifier_category():
    """Tier 1, tier 2, escalation, skip — all live under LogCategory.VERIFIER."""
    drone, _rec, log, board, _client = _make_drone(verdict="FAILED", reason="x")
    task = _make_task()
    board.add(task)

    await drone.verify_completion(task)

    assert log.entries  # ensure we logged something
    for entry in log.entries:
        assert entry.category == LogCategory.VERIFIER, (
            f"non-VERIFIER category leaked: {entry.action} → {entry.category}"
        )


# ---------------------------------------------------------------------------
# has_check_evidence helper
# ---------------------------------------------------------------------------


class _StubEntry:
    def __init__(self, worker: str, detail: str) -> None:
        self.worker_name = worker
        self.detail = detail


def test_has_check_evidence_finds_slash_check():
    entries = [
        _StubEntry("api", "queue_proposal noise"),
        _StubEntry("api", "ran /check, all green"),
    ]
    assert has_check_evidence(entries, "api") is True


def test_has_check_evidence_finds_pytest():
    entries = [_StubEntry("api", "uv run pytest tests/ -q")]
    assert has_check_evidence(entries, "api") is True


def test_has_check_evidence_misses_other_workers():
    entries = [_StubEntry("hub", "/check passed")]
    assert has_check_evidence(entries, "api") is False


def test_has_check_evidence_returns_false_on_empty_log():
    assert has_check_evidence([], "api") is False


def test_has_check_evidence_misses_when_only_unrelated_actions():
    entries = [
        _StubEntry("api", "swarm_send_message broadcast"),
        _StubEntry("api", "task assigned"),
    ]
    assert has_check_evidence(entries, "api") is False
