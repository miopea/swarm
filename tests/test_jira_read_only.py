"""Read-only mode: try the integration without writing to the team's tracker (#1342).

WHY IT MATTERS NOW. Jira is being enabled for EVERY dev. Without this, a newcomer's
first misconfiguration lands in their team's ticket queue rather than on their own
screen — and verifying v2 required creating seven throwaway tickets in a real shared
project because there was no alternative.

ENFORCED AT THE CLIENT, which is the only place every Jira write passes through.
Hiding buttons in the UI would not help: the sync loop writes on a timer with nobody
watching, which is exactly how 14 real WWD tickets were transitioned on 2026-08-07 by a
settings toggle.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.config.models import JiraConfig
from swarm.integrations.jira import JiraClient, JiraSyncService
from swarm.tasks.task import SwarmTask


def _client(read_only: bool) -> JiraClient:
    cfg = JiraConfig(enabled=True, projects=["WWD"], read_only=read_only)
    mgr = MagicMock()
    mgr.is_connected.return_value = True
    mgr.api_base_url = "https://api.atlassian.com/ex/jira/test"
    client = JiraClient(cfg, mgr)
    # If any write slips past the guard it reaches here and the test fails loudly,
    # rather than silently doing nothing and looking like the guard worked.
    client._ensure_session = AsyncMock(
        side_effect=AssertionError("a write opened a session in read-only mode")
    )
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.transition_issue("WWD-1", "31"),
        lambda c: c.add_comment("WWD-1", "hello"),
        lambda c: c.assign_issue("WWD-1", "acct-1"),
        lambda c: c.add_worklog("WWD-1", 600, "note"),
        lambda c: c.create_issue("WWD", "summary", "body"),
    ],
)
async def test_every_write_is_refused(call: Any):
    """The whole surface, not a sample. A mode that blocks four of five writes is worse
    than none, because it is believed."""
    result = await call(_client(read_only=True))
    assert not result, f"a write returned a truthy result in read-only mode: {result!r}"


@pytest.mark.asyncio
async def test_the_refusal_says_what_it_would_have_done(caplog: Any):
    """Otherwise read-only is indistinguishable from broken."""
    import logging

    with caplog.at_level(logging.WARNING):
        await _client(read_only=True).transition_issue("WWD-9", "31")

    msg = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "READ-ONLY" in msg and "WWD-9" in msg, f"the refusal is not operator-visible: {msg}"


@pytest.mark.asyncio
async def test_writes_still_happen_when_read_only_is_off():
    """The guard must be a gate, not a wall — otherwise the tests above pass on a
    permanently broken integration."""
    client = _client(read_only=False)
    reached = AsyncMock(side_effect=RuntimeError("reached the network layer"))
    client._ensure_session = reached
    with pytest.raises(RuntimeError, match="reached the network layer"):
        await client.transition_issue("WWD-1", "31")
    reached.assert_awaited(), "the guard blocked a write with read-only OFF"


@pytest.mark.asyncio
async def test_reads_are_unaffected():
    """Imports, discovery and reconciliation must still run — a read-only mode that
    stops everything teaches nothing about whether the config is right."""
    cfg = JiraConfig(enabled=True, projects=["WWD"], read_only=True)
    mgr = MagicMock()
    mgr.is_connected.return_value = True
    mgr.api_base_url = "https://api.atlassian.com/ex/jira/test"
    svc = JiraSyncService(cfg, token_manager=mgr)
    assert svc.enabled, "read-only must not disable the integration"
    assert "assignee = currentUser()" in svc.build_jql(), "imports were disabled too"


@pytest.mark.asyncio
async def test_a_promotion_in_read_only_does_not_report_a_fake_ticket():
    """create_issue returns {} rather than a fabricated key, so the caller refuses
    instead of linking a task to a ticket that does not exist."""
    cfg = JiraConfig(enabled=True, projects=["WWD"], read_only=True)
    mgr = MagicMock()
    mgr.is_connected.return_value = True
    mgr.api_base_url = "https://api.atlassian.com/ex/jira/test"
    svc = JiraSyncService(cfg, token_manager=mgr)
    svc.client._ensure_session = AsyncMock(side_effect=AssertionError("wrote in read-only"))
    svc.client.get_myself = AsyncMock(return_value={"accountId": "a"})
    svc.client.search_issues = AsyncMock(return_value=[])

    assert await svc.create_jira_issue(SwarmTask(title="t", description="")) == ""


# --- the durable protection: catch the NEXT unguarded write -------------------


def test_no_write_verb_bypasses_the_guard():
    """Guarding five methods is fine until someone adds a sixth.

    Every function that issues session.post/put/delete must consult _refuse_write.
    A sweep rather than five assertions, because the failure mode is an ADDITION —
    exactly how the three broken MessageStore.send callers accumulated.
    """
    src = Path("src/swarm/integrations/jira.py").read_text()
    tree = ast.parse(src)
    offenders: list[str] = []
    # METHODS only — direct members of a class body. The actual session.post calls sit
    # inside nested `_do()` retry closures while the guard is in the enclosing method,
    # so walking every function node reports the closures and hides whether the method
    # is guarded. The first version of this test did exactly that.
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        for node in cls.body:
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            body = ast.unparse(node)  # includes nested closures
            if not re.search(r"session\.(post|put|delete)\(", body):
                continue
            if node.name != "_refuse_write" and "_refuse_write" not in body:
                offenders.append(f"{cls.name}.{node.name}")
    assert not offenders, (
        f"these issue Jira writes without consulting read-only mode: {offenders}. "
        f"Read-only that blocks most writes is worse than none, because it is believed."
    )


def test_the_scan_can_actually_see_writes():
    """Positive control — if the regex matched nothing the sweep would pass over an
    empty set and prove nothing."""
    src = Path("src/swarm/integrations/jira.py").read_text()
    assert len(re.findall(r"session\.(post|put|delete)\(", src)) >= 5


def test_read_only_survives_a_config_round_trip():
    """Adding a config field is four changes — model, serializer, loader, applier — and
    v2 shipped three of them once already, so the UI reported a setting that vanished
    on restart."""
    from swarm.config import HiveConfig
    from swarm.config.loader import _parse_jira_section
    from swarm.config.serialization import serialize_config

    data = serialize_config(HiveConfig(jira=JiraConfig(enabled=True, read_only=True)))
    assert data["jira"]["read_only"] is True, "not serialized; the setting dies on restart"
    assert _parse_jira_section(data["jira"]).read_only is True, "not loaded back"
    assert _parse_jira_section({"enabled": True}).read_only is False, "default is not safe-off"


def test_the_applier_accepts_it():
    """The fourth layer. Without it the dashboard toggle round-trips back to the old
    value with no error — the exact silent failure the projects box had."""
    src = Path("src/swarm/server/config_appliers/jira.py").read_text()
    assert '"read_only"' in src, "the applier ignores the toggle, so saving it does nothing"
