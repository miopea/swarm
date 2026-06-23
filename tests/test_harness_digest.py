"""Tests for the operator-gated harness-improvement digest (LangChain Loop 4).

Covers the pure aggregator builders, the central SAFETY INVARIANT (display-only
types never carry an apply_action; every apply endpoint is a pre-existing
route), and the read-only route handler. See `analysis/harness_digest.py`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from swarm.analysis.harness_digest import (
    APPLY_ENDPOINTS,
    DISPLAY_ONLY_TYPES,
    build_approval_rule_suggestions,
    build_digest,
    build_dreamer_pattern_suggestions,
    build_playbook_suggestions,
    build_tool_description_suggestions,
    build_tuning_suggestions,
)
from swarm.analysis.tool_usage import ToolStats
from swarm.drones.suggest import RuleSuggestion
from swarm.drones.tuning import TuningSuggestion
from swarm.playbooks.models import Playbook, PlaybookStatus
from swarm.server.routes import harness_digest as hd_route

# Playbook lifecycle thresholds (mirror PlaybookConfig defaults).
PB_THRESHOLDS = dict(promote_uses=3, promote_winrate=0.7, prune_uses=5, prune_winrate=0.3)


def _tool(tool: str, calls: int, errors: int) -> ToolStats:
    st = ToolStats(tool=tool, calls=calls, errors=errors)
    if errors:
        st.error_samples = ["error: boom"]
    return st


def _learning(applied_to: str) -> SimpleNamespace:
    return SimpleNamespace(applied_to=applied_to, context="ctx text", correction="do X instead")


def _rule(
    pattern: str = r"\bx\b", action: str = "approve", confidence: float = 0.9
) -> RuleSuggestion:
    return RuleSuggestion(pattern=pattern, action=action, confidence=confidence, explanation="x")


class TestToolDescriptions:
    def test_hot_error_prone_tool_flagged_display_only(self) -> None:
        out = build_tool_description_suggestions([_tool("swarm_foo", calls=20, errors=8)])
        assert len(out) == 1
        assert out[0].type == "tool_description"
        assert out[0].apply_action is None  # display-only
        assert out[0].evidence["error_samples"] == ["error: boom"]

    def test_clean_tool_not_flagged(self) -> None:
        assert build_tool_description_suggestions([_tool("swarm_ok", calls=50, errors=1)]) == []

    def test_low_call_count_not_flagged(self) -> None:
        # 100% error rate but only 2 calls — below min_calls.
        assert build_tool_description_suggestions([_tool("swarm_rare", calls=2, errors=2)]) == []


class TestApprovalRules:
    def test_confident_suggestion_gets_apply_action(self) -> None:
        rs = RuleSuggestion(
            pattern=r"\bnpm test\b", action="approve", confidence=0.8, explanation="x"
        )
        out = build_approval_rule_suggestions([rs])
        assert len(out) == 1
        act = out[0].apply_action
        assert act is not None
        assert act["endpoint"] == "/api/config/approval-rules"
        assert act["body"] == {"pattern": r"\bnpm test\b", "action": "approve"}

    def test_safety_rejected_suggestion_dropped(self) -> None:
        # suggest_rule returns empty pattern / 0 confidence for unsafe inputs —
        # those must NEVER produce an apply button.
        rs = RuleSuggestion(pattern="", action="approve", confidence=0.0, explanation="rejected")
        assert build_approval_rule_suggestions([rs]) == []


class TestPlaybooks:
    def test_low_winrate_playbook_retire(self) -> None:
        pb = Playbook(name="bad", status=PlaybookStatus.ACTIVE, uses=10, wins=1, losses=9)
        out = build_playbook_suggestions([pb], **PB_THRESHOLDS)
        assert len(out) == 1
        assert out[0].apply_action["endpoint"] == "/api/playbooks/bad/retire"
        assert "reason" in out[0].apply_action["body"]

    def test_strong_candidate_promote(self) -> None:
        pb = Playbook(name="good", status=PlaybookStatus.CANDIDATE, uses=5, wins=4, losses=1)
        out = build_playbook_suggestions([pb], **PB_THRESHOLDS)
        assert len(out) == 1
        assert out[0].apply_action["endpoint"] == "/api/playbooks/good/promote"

    def test_zero_decided_not_pruned(self) -> None:
        # 0.0 winrate but no decided outcomes → must not be retired.
        pb = Playbook(name="new", status=PlaybookStatus.ACTIVE, uses=10, wins=0, losses=0)
        assert build_playbook_suggestions([pb], **PB_THRESHOLDS) == []

    def test_retired_playbook_ignored(self) -> None:
        pb = Playbook(name="old", status=PlaybookStatus.RETIRED, uses=10, wins=1, losses=9)
        assert build_playbook_suggestions([pb], **PB_THRESHOLDS) == []


class TestDisplayOnlySignals:
    def test_dreamer_prefix_filter(self) -> None:
        learnings = [_learning("discovered_by_dreamer:TASK_FAILED:abc"), _learning("manual:note")]
        out = build_dreamer_pattern_suggestions(learnings)
        assert len(out) == 1
        assert out[0].apply_action is None

    def test_tuning_is_display_only(self) -> None:
        ts = TuningSuggestion(
            id="t1",
            description="threshold too low",
            config_path="drones.x",
            current_value="(current)",
            suggested_value="(increase)",
            reason="you approved most escalations",
            override_count=5,
            total_decisions=8,
            override_rate=0.62,
        )
        out = build_tuning_suggestions([ts])
        assert len(out) == 1
        assert out[0].apply_action is None


class TestBuildDigest:
    def _digest(self):
        return build_digest(
            tool_stats=[_tool("swarm_foo", 20, 8)],
            rule_suggestions=[
                RuleSuggestion(pattern=r"\bx\b", action="approve", confidence=0.9, explanation="x")
            ],
            playbooks=[
                Playbook(name="bad", status=PlaybookStatus.ACTIVE, uses=10, wins=1, losses=9)
            ],
            dreamer_learnings=[_learning("discovered_by_dreamer:X:1")],
            tuning_suggestions=[],
            now=1000.0,
            **PB_THRESHOLDS,
        )

    def test_actionable_sorted_first(self) -> None:
        d = self._digest()
        # First suggestions must be the ones with an apply_action.
        first_display_only = next(
            (i for i, s in enumerate(d.suggestions) if s.apply_action is None), len(d.suggestions)
        )
        assert all(s.apply_action is not None for s in d.suggestions[:first_display_only])
        assert all(s.apply_action is None for s in d.suggestions[first_display_only:])

    def test_counts_and_api_shape(self) -> None:
        d = self._digest()
        api = d.to_api()
        assert api["actionable"] == 2  # approval_rule + playbook retire
        assert api["counts"]["tool_description"] == 1
        assert "suggestions" in api

    def test_empty_inputs_no_crash(self) -> None:
        d = build_digest(
            tool_stats=[],
            rule_suggestions=[],
            playbooks=[],
            dreamer_learnings=[],
            tuning_suggestions=[],
            now=1.0,
            **PB_THRESHOLDS,
        )
        assert d.suggestions == []
        assert d.to_api()["actionable"] == 0


class TestSafetyInvariant:
    """The whole point: no autonomous self-rewriting, enforced structurally."""

    def test_display_only_types_never_apply_and_endpoints_are_known(self) -> None:
        d = build_digest(
            tool_stats=[_tool("swarm_foo", 20, 8)],
            rule_suggestions=[
                RuleSuggestion(pattern=r"\bx\b", action="approve", confidence=0.9, explanation="x")
            ],
            playbooks=[
                Playbook(name="bad", status=PlaybookStatus.ACTIVE, uses=10, wins=1, losses=9),
                Playbook(name="good", status=PlaybookStatus.CANDIDATE, uses=5, wins=5, losses=0),
            ],
            dreamer_learnings=[_learning("discovered_by_dreamer:X:1")],
            tuning_suggestions=[
                TuningSuggestion(
                    id="t",
                    description="d",
                    config_path="p",
                    current_value="c",
                    suggested_value="s",
                    reason="r",
                    override_count=5,
                    total_decisions=8,
                    override_rate=0.6,
                )
            ],
            now=1.0,
            **PB_THRESHOLDS,
        )
        for s in d.suggestions:
            if s.type in DISPLAY_ONLY_TYPES:
                assert s.apply_action is None, f"{s.type} must be display-only"
            if s.apply_action is not None:
                endpoint = s.apply_action["endpoint"]
                # Every apply endpoint must belong to a pre-existing route family.
                assert any(endpoint.startswith(prefix) for prefix in APPLY_ENDPOINTS), endpoint
                assert s.apply_action["method"] == "POST"


class _FakeRequest:
    def __init__(self, daemon) -> None:
        self.app = {"daemon": daemon}


class TestRoute:
    @pytest.mark.asyncio
    async def test_handle_digest_tolerates_missing_stores(self) -> None:
        # A daemon with no usable stores must still return a valid empty digest.
        daemon = SimpleNamespace(
            drone_log=SimpleNamespace(_buzz_store=None, store=None),
            playbook_store=None,
            queen_chat=None,
            config=SimpleNamespace(playbooks=None),
        )
        resp = await hd_route.handle_digest(_FakeRequest(daemon))
        data = json.loads(resp.body.decode())
        assert data["suggestions"] == []
        assert data["actionable"] == 0
        assert "counts" in data
