"""Harness-improvement digest — operator-gated hill-climbing (LangChain Loop 4).

Swarm already *collects* hill-climbing signals (error-prone MCP tools via
:mod:`swarm.analysis.tool_usage`, suggested approval rules via
:mod:`swarm.drones.suggest`, override patterns via :mod:`swarm.drones.tuning`,
the dreamer's mined failure patterns, playbook win-rates) but surfaces them
piecemeal and never feeds them back into the harness. This module **aggregates**
them into one digest for a dashboard review surface.

The load-bearing safety property: this is **operator-gated, never autonomous**.
A suggestion either carries an ``apply_action`` that names an EXISTING,
already-validated endpoint (add an approval rule, retire/promote a playbook) or
is **display-only** (``apply_action=None``) — tool-description and prompt
rewrites are code/judgment changes a human must make. The aggregator itself
never mutates anything; it only describes. This matches the article's #1
principle: codify the easy wins, reserve live review for sensitive actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from swarm.analysis.tool_usage import ToolStats
    from swarm.drones.suggest import RuleSuggestion
    from swarm.drones.tuning import TuningSuggestion
    from swarm.playbooks.models import Playbook

# Suggestion types whose changes are code/judgment edits — NEVER auto-applied.
# ``verifier_metrics`` and ``stale_learning`` are display-only: the former is a
# read-out (nothing to apply), the latter surfaces stale learnings for the
# operator to retire by hand (never auto-deleted — the #331 principle).
DISPLAY_ONLY_TYPES = frozenset(
    {"tool_description", "dreamer_pattern", "tuning", "verifier_metrics", "stale_learning"}
)

# Verifier buzz actions counted for the metrics read-out, mapped to UI labels.
_VERIFIER_ACTION_LABELS = {
    "VERIFIER_TIER2_VERIFIED": "verified",
    "VERIFIER_TIER2_UNCERTAIN": "uncertain",
    "VERIFIER_TIER1_REOPENED": "reopened (tier-1)",
    "VERIFIER_TIER2_REOPENED": "reopened (tier-2)",
    "VERIFIER_SHADOW_WOULD_REOPEN": "shadow would-reopen",
    "VERIFIER_ESCALATED": "escalated",
}

# Dreamer learnings older than this (days) are surfaced for operator review.
_STALE_LEARNING_AGE_DAYS = 60

# The only endpoints an apply_action may target — all pre-existing, already
# validated server-side. Keeping this a closed set is what makes "no novel
# apply path" a structural guarantee (asserted in tests).
APPLY_ENDPOINTS = frozenset({"/api/config/approval-rules", "/api/playbooks"})

# Default flagging thresholds for the (display-only) tool-description signal.
_TOOL_MIN_CALLS = 10
_TOOL_MIN_ERROR_RATE = 0.2

_DREAMER_TAG_PREFIX = "discovered_by_dreamer:"


@dataclass
class ImprovementSuggestion:
    """One actionable-or-informational harness improvement."""

    type: str  # approval_rule | tool_description | playbook | dreamer_pattern | tuning
    title: str
    detail: str
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)
    # {endpoint, method, body} naming an EXISTING route, or None for display-only.
    apply_action: dict[str, Any] | None = None

    def to_api(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "title": self.title,
            "detail": self.detail,
            "confidence": round(self.confidence, 3),
            "evidence": self.evidence,
            "apply_action": self.apply_action,
        }


@dataclass
class HarnessDigest:
    """The aggregated set of suggestions plus per-type counts for the UI."""

    generated_at: float
    suggestions: list[ImprovementSuggestion] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.suggestions:
            out[s.type] = out.get(s.type, 0) + 1
        return out

    def to_api(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "counts": self.counts,
            "actionable": sum(1 for s in self.suggestions if s.apply_action is not None),
            "suggestions": [s.to_api() for s in self.suggestions],
        }


# --------------------------------------------------------------------------- #
# Pure builders — each takes already-fetched data so it is hermetically tested.
# --------------------------------------------------------------------------- #
def build_tool_description_suggestions(
    tool_stats: list[ToolStats],
    *,
    min_calls: int = _TOOL_MIN_CALLS,
    min_error_rate: float = _TOOL_MIN_ERROR_RATE,
) -> list[ImprovementSuggestion]:
    """Flag hot, error-prone MCP tools whose descriptions may need a rewrite.

    Display-only: a tool-description rewrite is a code change, so the operator
    reads the ``error_samples`` and decides. ``apply_action`` is always None.
    """
    out: list[ImprovementSuggestion] = []
    for st in tool_stats:
        if st.calls < min_calls or st.error_rate < min_error_rate:
            continue
        out.append(
            ImprovementSuggestion(
                type="tool_description",
                title=f"{st.tool}: {st.error_rate:.0%} error rate over {st.calls} calls",
                detail=(
                    f"`{st.tool}` failed {st.errors}/{st.calls} times. Consider "
                    "tightening its description / parameter docs so workers call "
                    "it correctly. (Manual code change — not auto-applied.)"
                ),
                confidence=min(1.0, st.error_rate),
                evidence={
                    "calls": st.calls,
                    "errors": st.errors,
                    "error_rate": round(st.error_rate, 3),
                    "error_samples": list(st.error_samples),
                },
                apply_action=None,
            )
        )
    return out


def build_approval_rule_suggestions(
    rule_suggestions: list[RuleSuggestion],
) -> list[ImprovementSuggestion]:
    """Wrap suggested approval rules; one-click apply via the existing endpoint.

    Drops empty-pattern / zero-confidence suggestions — :func:`suggest_rule`
    already returns those for inputs it deemed unsafe (matched ALWAYS_ESCALATE
    or a dangerous command), so they must never produce an apply button.
    """
    out: list[ImprovementSuggestion] = []
    for rs in rule_suggestions:
        if not rs.pattern or rs.confidence <= 0.0:
            continue
        out.append(
            ImprovementSuggestion(
                type="approval_rule",
                title=f"Add {rs.action} rule: {rs.pattern}",
                detail=rs.explanation,
                confidence=rs.confidence,
                evidence={"pattern": rs.pattern, "action": rs.action},
                apply_action={
                    "endpoint": "/api/config/approval-rules",
                    "method": "POST",
                    "body": {"pattern": rs.pattern, "action": rs.action},
                },
            )
        )
    return out


def build_playbook_suggestions(
    playbooks: list[Playbook],
    *,
    promote_uses: int,
    promote_winrate: float,
    prune_uses: int,
    prune_winrate: float,
) -> list[ImprovementSuggestion]:
    """Suggest retiring low-win-rate playbooks and promoting strong candidates.

    Mirrors :meth:`PlaybookStore.evaluate_lifecycle` — never prunes on a 0.0
    win-rate that merely reflects no decided outcomes yet. Both actions reuse
    the existing playbook routes and are reversible.
    """
    from swarm.playbooks.models import PlaybookStatus

    out: list[ImprovementSuggestion] = []
    for pb in playbooks:
        decided = pb.wins + pb.losses
        if (
            pb.status == PlaybookStatus.CANDIDATE
            and pb.uses >= promote_uses
            and pb.winrate >= promote_winrate
        ):
            out.append(
                ImprovementSuggestion(
                    type="playbook",
                    title=f"Promote playbook '{pb.name}' ({pb.winrate:.0%} win rate)",
                    detail=(
                        f"Candidate '{pb.name}' has {pb.uses} uses and a "
                        f"{pb.winrate:.0%} win rate — promote it to active."
                    ),
                    confidence=pb.winrate,
                    evidence={"name": pb.name, "uses": pb.uses, "winrate": round(pb.winrate, 3)},
                    apply_action={
                        "endpoint": f"/api/playbooks/{pb.name}/promote",
                        "method": "POST",
                        "body": {},
                    },
                )
            )
        elif (
            pb.status != PlaybookStatus.RETIRED
            and pb.uses >= prune_uses
            and decided > 0
            and pb.winrate < prune_winrate
        ):
            out.append(
                ImprovementSuggestion(
                    type="playbook",
                    title=f"Retire playbook '{pb.name}' ({pb.winrate:.0%} win rate)",
                    detail=(
                        f"'{pb.name}' has {pb.uses} uses but only a {pb.winrate:.0%} "
                        f"win rate over {decided} decided outcomes — retire it."
                    ),
                    confidence=1.0 - pb.winrate,
                    evidence={"name": pb.name, "uses": pb.uses, "winrate": round(pb.winrate, 3)},
                    apply_action={
                        "endpoint": f"/api/playbooks/{pb.name}/retire",
                        "method": "POST",
                        "body": {"reason": "harness-digest: low win rate"},
                    },
                )
            )
    return out


def build_dreamer_pattern_suggestions(learnings: list[Any]) -> list[ImprovementSuggestion]:
    """Surface the dreamer's mined recurring-failure patterns (display-only).

    ``learnings`` are :class:`QueenLearning` rows; we keep only dreamer-tagged
    ones (``applied_to`` starts with ``discovered_by_dreamer:``) — a prefix
    filter, since ``query_learnings`` only supports exact ``applied_to`` match.
    """
    out: list[ImprovementSuggestion] = []
    for lr in learnings:
        applied_to = getattr(lr, "applied_to", "") or ""
        if not applied_to.startswith(_DREAMER_TAG_PREFIX):
            continue
        out.append(
            ImprovementSuggestion(
                type="dreamer_pattern",
                title=f"Recurring pattern: {getattr(lr, 'context', '')[:80]}",
                detail=getattr(lr, "correction", ""),
                confidence=0.5,
                evidence={"applied_to": applied_to},
                apply_action=None,
            )
        )
    return out


def build_tuning_suggestions(tuning: list[TuningSuggestion]) -> list[ImprovementSuggestion]:
    """Surface override-derived config-tuning hints (display-only).

    The suggested values are intentionally vague ("(increase by 30%)"), so this
    is guidance, never a one-click apply.
    """
    out: list[ImprovementSuggestion] = []
    for ts in tuning:
        out.append(
            ImprovementSuggestion(
                type="tuning",
                title=ts.description,
                detail=f"{ts.reason} (suggested: {ts.config_path} → {ts.suggested_value})",
                confidence=ts.override_rate,
                evidence={
                    "config_path": ts.config_path,
                    "override_count": ts.override_count,
                    "override_rate": round(ts.override_rate, 3),
                },
                apply_action=None,
            )
        )
    return out


def build_verifier_metrics_suggestions(
    buzz_rows: list[Any], *, criteria_coverage: tuple[int, int] | None = None
) -> list[ImprovementSuggestion]:
    """Display-only read-out of the verifier's recent verdict mix + criteria
    coverage. This is the surface that makes SHADOW mode useful: the operator
    watches the recorded verdicts here before enabling ``verifier_enforce``.
    """
    counts: dict[str, int] = {}
    for row in buzz_rows:
        action = row.get("action") or "" if isinstance(row, dict) else getattr(row, "action", "")
        if action in _VERIFIER_ACTION_LABELS:
            counts[action] = counts.get(action, 0) + 1
    if not counts and not criteria_coverage:
        return []
    parts: list[str] = []
    if counts:
        mix = ", ".join(f"{_VERIFIER_ACTION_LABELS[a]}: {n}" for a, n in sorted(counts.items()))
        parts.append(f"Recent verifier verdicts — {mix}.")
    if criteria_coverage is not None:
        have, total = criteria_coverage
        pct = (100.0 * have / total) if total else 0.0
        parts.append(f"Acceptance-criteria coverage: {have}/{total} tasks ({pct:.0f}%).")
    shadow = counts.get("VERIFIER_SHADOW_WOULD_REOPEN", 0)
    if shadow:
        parts.append(
            f"{shadow} would-be reopen(s) recorded in SHADOW mode (enforcement off) — "
            "review these before enabling verifier_enforce."
        )
    return [
        ImprovementSuggestion(
            type="verifier_metrics",
            title="Verifier verdict metrics",
            detail=" ".join(parts),
            confidence=1.0,
            evidence={"counts": counts, "coverage": list(criteria_coverage or ())},
            apply_action=None,
        )
    ]


def build_stale_learning_suggestions(
    dreamer_learnings: list[Any], *, now: float, max_age_days: int = _STALE_LEARNING_AGE_DAYS
) -> list[ImprovementSuggestion]:
    """Display-only: surface dreamer learnings older than ``max_age_days`` for
    the operator to retire by hand. NEVER auto-deleted (the #331 principle) —
    this only flags candidates."""
    cutoff = now - max_age_days * 86_400.0
    stale = []
    for lrn in dreamer_learnings:
        tag = getattr(lrn, "applied_to", "") or ""
        if not tag.startswith(_DREAMER_TAG_PREFIX):
            continue
        created = float(getattr(lrn, "created_at", 0.0) or 0.0)
        if created and created < cutoff:
            stale.append(lrn)
    if not stale:
        return []
    return [
        ImprovementSuggestion(
            type="stale_learning",
            title=f"{len(stale)} dreamer learning(s) older than {max_age_days}d — review",
            detail=(
                "These auto-mined learnings haven't refreshed in a while and may be stale. "
                "Retire any that are no longer relevant (operator action — never auto-deleted)."
            ),
            confidence=0.5,
            evidence={
                "ids": [getattr(lrn, "id", "") for lrn in stale][:20],
                "count": len(stale),
            },
            apply_action=None,
        )
    ]


def build_digest(
    *,
    tool_stats: list[ToolStats],
    rule_suggestions: list[RuleSuggestion],
    playbooks: list[Playbook],
    dreamer_learnings: list[Any],
    tuning_suggestions: list[TuningSuggestion],
    promote_uses: int,
    promote_winrate: float,
    prune_uses: int,
    prune_winrate: float,
    now: float,
    verifier_buzz_rows: list[Any] | None = None,
    criteria_coverage: tuple[int, int] | None = None,
) -> HarnessDigest:
    """Compose all builders into one digest, actionable items sorted first."""
    suggestions: list[ImprovementSuggestion] = []
    suggestions += build_approval_rule_suggestions(rule_suggestions)
    suggestions += build_playbook_suggestions(
        playbooks,
        promote_uses=promote_uses,
        promote_winrate=promote_winrate,
        prune_uses=prune_uses,
        prune_winrate=prune_winrate,
    )
    suggestions += build_tool_description_suggestions(tool_stats)
    suggestions += build_dreamer_pattern_suggestions(dreamer_learnings)
    suggestions += build_tuning_suggestions(tuning_suggestions)
    suggestions += build_verifier_metrics_suggestions(
        verifier_buzz_rows or [], criteria_coverage=criteria_coverage
    )
    suggestions += build_stale_learning_suggestions(dreamer_learnings, now=now)
    # Actionable (apply_action present) first, then by confidence desc.
    suggestions.sort(key=lambda s: (s.apply_action is None, -s.confidence))
    return HarnessDigest(generated_at=now, suggestions=suggestions)


# --------------------------------------------------------------------------- #
# Impure collector — route-only; tolerant of every missing store.
# --------------------------------------------------------------------------- #
def _criteria_coverage(daemon: Any) -> tuple[int, int] | None:
    """(#tasks-with-criteria, #tasks) over the board, or None if unavailable."""
    board = getattr(daemon, "task_board", None)
    if board is None:
        return None
    try:
        tasks = list(board.all_tasks)
    except Exception:
        return None
    if not tasks:
        return None
    have = sum(1 for t in tasks if getattr(t, "acceptance_criteria", None))
    return (have, len(tasks))


def collect_digest(daemon: Any, *, window_days: int = 14) -> HarnessDigest:
    """Gather the live signals off ``daemon`` and build the digest.

    Every store access is guarded → a missing/empty store contributes an empty
    list, never an exception. The route is the only caller.
    """
    import time

    from swarm.analysis.tool_usage import aggregate
    from swarm.drones.suggest import suggest_rule

    now = time.time()
    since = now - window_days * 86_400.0

    buzz = getattr(getattr(daemon, "drone_log", None), "_buzz_store", None)

    # 1. Tool stats from the buzz log. Fetch the window's rows once and reuse
    #    them for both tool aggregation and the verifier-metrics read-out.
    tool_stats: list[ToolStats] = []
    buzz_rows: list[Any] = []
    if buzz is not None:
        try:
            buzz_rows = list(buzz.query(since=since, limit=2000))
            tool_stats = aggregate(buzz_rows)
        except Exception:
            tool_stats = []
            buzz_rows = []

    # 2. Approval-rule suggestion from overridden auto-approvals (operator
    #    rejected a CONTINUE → suggest an escalate rule for that pattern).
    rule_suggestions: list[RuleSuggestion] = []
    if buzz is not None:
        try:
            overridden = buzz.query(overridden=True, since=since, limit=200)
            details = [r.get("detail", "") for r in overridden if r.get("detail")]
            if details:
                rs = suggest_rule(details, action="escalate")
                rule_suggestions = [rs]
        except Exception:
            rule_suggestions = []

    # 3. Playbooks (all statuses) for promote/retire suggestions.
    playbooks: list[Playbook] = []
    pb_store = getattr(daemon, "playbook_store", None)
    if pb_store is not None:
        try:
            playbooks = list(pb_store.list(status=None))
        except Exception:
            playbooks = []

    # 4. Dreamer-mined learnings (prefix-filtered in the builder).
    dreamer_learnings: list[Any] = []
    qc = getattr(daemon, "queen_chat", None)
    if qc is not None:
        try:
            dreamer_learnings = list(qc.query_learnings(limit=50))
        except Exception:
            dreamer_learnings = []

    # 5. Override-tuning hints (LogStore may be None under the buzz-store daemon).
    tuning_suggestions: list[TuningSuggestion] = []
    store = getattr(getattr(daemon, "drone_log", None), "store", None)
    if store is not None:
        try:
            from swarm.drones.tuning import analyze_overrides

            tuning_suggestions = list(analyze_overrides(store))
        except Exception:
            tuning_suggestions = []

    # 6. Acceptance-criteria coverage over current tasks (verifier metrics).
    criteria_coverage = _criteria_coverage(daemon)

    # Playbook lifecycle thresholds — reuse PlaybookConfig (single source).
    pb_cfg = getattr(getattr(daemon, "config", None), "playbooks", None)
    return build_digest(
        tool_stats=tool_stats,
        rule_suggestions=rule_suggestions,
        playbooks=playbooks,
        dreamer_learnings=dreamer_learnings,
        tuning_suggestions=tuning_suggestions,
        promote_uses=getattr(pb_cfg, "auto_promote_uses", 3),
        promote_winrate=getattr(pb_cfg, "auto_promote_winrate", 0.7),
        prune_uses=getattr(pb_cfg, "prune_min_uses", 5),
        prune_winrate=getattr(pb_cfg, "prune_max_winrate", 0.3),
        now=now,
        verifier_buzz_rows=buzz_rows,
        criteria_coverage=criteria_coverage,
    )
