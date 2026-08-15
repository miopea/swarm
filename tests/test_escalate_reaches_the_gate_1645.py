"""#1645 — an `escalate` verdict must reach the real gate, for EVERY tool.

`_evaluate_rules` used to convert an escalate into an APPROVE whenever a Queen object
happened to exist: `_queen_can_approve` checked `queen is not None and queen.enabled and
queen.can_call` and nothing else. No message was sent to her, nothing was queued for her,
she was never shown the call. "Approved under queen oversight" meant only that a Queen
was configured — which is always.

Bash was the single exception (`_ALWAYS_ESCALATE_TOOLS`), and Bash is precisely what
demonstrated the correct behaviour all night: it abstains, Claude Code's own permission
gate decides, and nothing was ever stuck on it. OPERATOR RULING 2026-08-15: delete the
branch rather than invent a consultation mechanism. Stop making an exception of the one
tool that works.

MEASURED BEFORE THE CHANGE, from the live buzz log: 519 queen-delegated approvals in 24h
(~21.6/h), led by Edit (208), Write (91) and the chrome MCP tools (~147 combined). The
branch had ZERO test coverage.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from swarm.config.models import DroneApprovalRule, DroneConfig
from swarm.server.routes.hooks import _ALWAYS_ESCALATE_TOOLS, _evaluate_rules

# Matches the tool text built for the calls below; action=escalate is the whole point.
ESCALATE_RULE = DroneApprovalRule(pattern=r"deploy-the-thing", action="escalate")
APPROVE_RULE = DroneApprovalRule(pattern=r"perfectly-safe", action="approve")


def _daemon(rules: list[DroneApprovalRule], *, queen_up: bool = True) -> MagicMock:
    d = MagicMock()
    d.pilot.enabled = True
    d.pilot._worker_configs = {}
    d.config.drones = DroneConfig(approval_rules=list(rules), allowed_read_paths=[])
    d.workers = []
    d.queen.enabled = queen_up
    d.queen.can_call = queen_up
    return d


def _decide(d: MagicMock, tool_name: str, tool_text: str) -> dict:
    resp = _evaluate_rules(d, {"cwd": "/nowhere"}, tool_name, tool_text)
    return json.loads(resp.body)


@pytest.mark.parametrize(
    "tool_name",
    ["Edit", "Write", "mcp__claude-in-chrome__javascript_tool", "ToolSearch", "Monitor"],
)
def test_a_non_bash_escalate_passes_through_even_with_a_queen_up(tool_name: str):
    """THE DEFECT. Every one of these auto-approved on an escalate verdict, and the five
    named here are the top of the measured 24h distribution."""
    verdict = _decide(_daemon([ESCALATE_RULE]), tool_name, "deploy-the-thing")

    assert verdict["decision"] == "passthrough"
    assert "approve" != verdict["decision"]


def test_bash_still_passes_through_unchanged():
    """POSITIVE CONTROL and the reference behaviour. Bash was already excluded from the
    deleted branch; this is what every tool now does."""
    verdict = _decide(_daemon([ESCALATE_RULE]), "Bash", "deploy-the-thing")

    assert verdict["decision"] == "passthrough"


def test_an_approve_verdict_still_approves():
    """POSITIVE CONTROL. A change that returned passthrough unconditionally would pass
    every test above while switching the drone off entirely — the auto-approval of safe
    work is the feature, and only the ESCALATE path was wrong."""
    verdict = _decide(_daemon([APPROVE_RULE]), "Edit", "perfectly-safe")

    assert verdict["decision"] == "approve"


def test_the_queen_being_absent_changes_nothing_now():
    """Before, the outcome depended on whether a Queen object existed. The verdict must
    now be the same either way — that IS the fix."""
    with_queen = _decide(_daemon([ESCALATE_RULE], queen_up=True), "Edit", "deploy-the-thing")
    without = _decide(_daemon([ESCALATE_RULE], queen_up=False), "Edit", "deploy-the-thing")

    assert with_queen["decision"] == without["decision"] == "passthrough"


def test_no_queen_delegation_path_survives_in_the_module():
    """The helper is gone, not merely unreferenced. A dormant `_queen_can_approve` is an
    invitation to re-wire it, and this file is the record of why it went."""
    from swarm.server.routes import hooks

    assert not hasattr(hooks, "_queen_can_approve")


def test_ALWAYS_ESCALATE_TOOLS_is_retained_for_the_no_rules_branch():
    """IT DID NOT BECOME REDUNDANT. It has a second, independent use: with NO approval
    rules configured, every tool blanket-approves EXCEPT the ones in this set. Deleting
    it with the queen branch would have handed Bash a blanket approval on an empty
    config — a strictly worse failure than the one being fixed."""
    assert "Bash" in _ALWAYS_ESCALATE_TOOLS

    # No rules at all: a non-Bash tool takes the blanket approval...
    assert _decide(_daemon([]), "Edit", "anything at all")["decision"] == "approve"
    # ...and Bash does not.
    assert _decide(_daemon([]), "Bash", "anything at all")["decision"] != "approve"
