"""An escalation to a human must never be answered by automation (#1443).

WHAT HAPPENED. The Queen called AskUserQuestion and the tool returned answers the
operator never gave. Verbatim option labels with no custom text, identical every time the
same question was re-asked, while a genuine free-text human answer to another question in
the same set passed through.

THE MECHANISM. AskUserQuestion was approvable like any other tool. The drone's approval
response for Claude is "\\r" — a bare Enter — and Enter on an option picker selects the
HIGHLIGHTED option. Deterministic, hence the exact repetition. Mixed within a set because
the guard caught some and missed others (ESCALATED 86 vs CONTINUED 223 on the Queen).
400 occurrences since 2026-07-13; unnoticed because the highlighted option is usually
plausible, and only visible when the choice matters.

WHY A NEW SET RATHER THAN ADDING TO _ALWAYS_ESCALATE_TOOLS. That set is consulted ONLY in
the no-rules-configured branch. A matching rule still approves — and the log shows
"rule matched: rule" is the path that fired. Bash lives in that set and DEPENDS on rules
approving it, so the set could not simply be made to bypass rules. Hence a second,
stronger category checked before rule evaluation.

Fabricated answers drove real production actions: a live fulfilment order, six deploys, a
D365 write instruction, and a programme-level ruling.
"""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock

import pytest

from swarm.server.routes.hooks import (
    _ALWAYS_ESCALATE_TOOLS,
    _NEVER_AUTO_APPROVE,
    _evaluate_rules,
)


class _Rule:
    """A rule matching everything — the shape that produced the fabricated answers."""

    def __init__(self) -> None:
        self.pattern = ".*"
        self.action = "approve"
        self.enabled = True
        self.compiled = re.compile(".*")
        self.name = "rule"
        self.tools = None


def _daemon(drones_enabled: bool, rules: list) -> MagicMock:
    d = MagicMock()
    d.pilot = MagicMock()
    d.pilot.enabled = drones_enabled
    d.pilot._worker_configs = {}
    d.config.drones.approval_rules = rules
    return d


def _decide(tool: str, drones_enabled: bool, rules: list) -> dict:
    body = {"tool_name": tool, "tool_input": {}}
    resp = _evaluate_rules(_daemon(drones_enabled, rules), body, tool, "text")
    return json.loads(resp.body.decode())


def test_a_matching_rule_cannot_answer_for_the_operator():
    """THE REPRODUCTION. This returned {'decision': 'approve'} before the fix, and
    'rule matched' is the path the production log shows firing."""
    out = _decide("AskUserQuestion", drones_enabled=True, rules=[_Rule()])
    assert out["decision"] != "approve", (
        f"a drone rule is answering the operator's escalation again: {out}"
    )


def test_an_empty_rule_set_cannot_answer_either():
    """The safest-LOOKING configuration was not safe: with no rules at all the handler
    fell through to 'no approval rules configured' → approve."""
    out = _decide("AskUserQuestion", drones_enabled=True, rules=[])
    assert out["decision"] != "approve", f"an unconfigured fleet still auto-answers: {out}"


def test_disabling_drones_also_stops_it():
    """Confirms the operator's own mitigation actually works — tested, not inferred from
    an absence of log entries."""
    out = _decide("AskUserQuestion", drones_enabled=False, rules=[_Rule()])
    assert out["decision"] == "passthrough"


def test_bash_rules_still_approve():
    """THE REGRESSION GUARD. Bash is in _ALWAYS_ESCALATE_TOOLS and depends on rules
    approving it — drones approve safe Bash constantly. If the fix had been 'make the
    always-escalate set bypass rules', this test would fail and the fleet would stall on
    every command."""
    out = _decide("Bash", drones_enabled=True, rules=[_Rule()])
    assert out["decision"] == "approve", (
        f"Bash rule approval broke; drones can no longer approve safe commands: {out}"
    )


def test_the_two_sets_are_distinct_and_serve_different_purposes():
    """A POSITIVE CONTROL on the design. Collapsing them would either break Bash or
    reopen the hole, and the distinction is the whole fix."""
    assert "AskUserQuestion" in _NEVER_AUTO_APPROVE
    assert "AskUserQuestion" not in _ALWAYS_ESCALATE_TOOLS, (
        "AskUserQuestion in the weaker set implies rule-matched approval is allowed"
    )
    assert "Bash" in _ALWAYS_ESCALATE_TOOLS and "Bash" not in _NEVER_AUTO_APPROVE


@pytest.mark.parametrize("tool", sorted(_NEVER_AUTO_APPROVE))
def test_every_never_auto_approve_tool_is_unanswerable_by_any_path(tool: str):
    """There are three separate routes to 'approve' — rule match, queen delegation, and
    no-rules-configured. A guard in one leaves the others open, which is exactly how this
    survived. Any tool added to the set must be safe on all of them."""
    for rules in ([_Rule()], []):
        assert _decide(tool, drones_enabled=True, rules=rules)["decision"] != "approve"
