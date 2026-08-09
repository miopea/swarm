"""Sprint awareness (#1341): it PRIORITISES, it never RESTRICTS.

MEASURED BEFORE BUILDING, on the operator's real site 2026-08-09: the sprint field
exists as customfield_10020, but WWD has ZERO issues carrying one and IS rejects sprint
JQL outright — it is a service desk with no board. Neither configured project uses
sprints, so AC-5 ("verified against a real sprint-using project") is not satisfiable
here. That is why the feature ships OFF BY DEFAULT: unverifiable behaviour must not
change import results for teams I cannot test against.

THE DESIGN DECISION THE TICKET LEFT OPEN — restrict or prioritise — is settled as
PRIORITISE. Filtering imports to a sprint would hide genuinely assigned work, which is a
surprising way to lose a ticket. Raising priority makes sprint work sort first while
everything assigned still arrives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.config.models import JiraConfig
from swarm.integrations.jira import JiraSyncService, _raise_priority, in_active_sprint
from swarm.tasks.task import TaskPriority

_FIELD = "customfield_10020"


def _svc(**cfg: Any) -> JiraSyncService:
    defaults: dict[str, Any] = {"enabled": True, "projects": ["WWD"]}
    defaults.update(cfg)
    mgr = MagicMock()
    mgr.is_connected.return_value = True
    mgr.api_base_url = "https://api.atlassian.com/ex/jira/test"
    svc = JiraSyncService(JiraConfig(**defaults), token_manager=mgr)
    assert svc.enabled, "positive control: a disabled service makes every test vacuous"
    return svc


def _issue(key: str, sprint: Any = None, priority: str = "Medium") -> dict[str, Any]:
    fields: dict[str, Any] = {
        "summary": key,
        "description": "body",
        "issuetype": {"name": "Task"},
        "priority": {"name": priority},
    }
    if sprint is not None:
        fields[_FIELD] = sprint
    return {"key": key, "fields": fields}


# --- the predicate -------------------------------------------------------------


def test_only_an_ACTIVE_sprint_counts():
    """An issue sits in a closed sprint and the current one at once when work rolls
    over. A closed sprint is history and must not raise priority."""
    assert in_active_sprint([{"name": "S1", "state": "closed"}]) is False
    assert in_active_sprint([{"name": "S1", "state": "closed"}, {"name": "S2", "state": "active"}])
    assert in_active_sprint([{"name": "S3", "state": "future"}]) is False


def test_the_legacy_string_shape_is_understood():
    """Some instances still emit "...,state=ACTIVE,..." strings for this field."""
    assert in_active_sprint(["com.x.Sprint@1[id=5,state=ACTIVE,name=Sprint 5]"])
    assert in_active_sprint(["com.x.Sprint@1[id=4,state=CLOSED,name=Sprint 4]"]) is False


def test_no_sprint_value_is_not_an_error():
    assert in_active_sprint(None) is False
    assert in_active_sprint([]) is False


# --- the priority ladder -------------------------------------------------------


def test_priority_is_raised_one_step():
    assert _raise_priority(TaskPriority.LOW) is TaskPriority.NORMAL
    assert _raise_priority(TaskPriority.NORMAL) is TaskPriority.HIGH


def test_it_never_reaches_URGENT():
    """Urgent means production is affected; in-sprint means scheduled. Collapsing the
    two makes the signal that wakes people up indistinguishable from planned work."""
    assert _raise_priority(TaskPriority.HIGH) is TaskPriority.HIGH


def test_an_already_urgent_task_is_left_alone():
    assert _raise_priority(TaskPriority.URGENT) is TaskPriority.URGENT


# --- the import path -----------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_by_default_the_field_is_never_requested():
    """Requesting a field id that does not exist makes Jira reject the WHOLE search,
    which would take imports down for a feature nobody enabled."""
    svc = _svc()
    svc.client.discover_sprint_field = AsyncMock(return_value=_FIELD)
    svc.client.search_issues = AsyncMock(return_value=[])

    await svc.import_issues({})

    svc.client.discover_sprint_field.assert_not_called()
    assert svc.client.search_issues.await_args.kwargs.get("fields", "") == ""


@pytest.mark.asyncio
async def test_an_in_sprint_issue_is_raised_when_enabled():
    svc = _svc(sprint_priority_boost=True)
    svc.client.discover_sprint_field = AsyncMock(return_value=_FIELD)
    svc.client.search_issues = AsyncMock(
        return_value=[_issue("WWD-1", [{"name": "S", "state": "active"}])]
    )
    tasks = await svc.import_issues({})
    assert tasks[0].priority is TaskPriority.HIGH


@pytest.mark.asyncio
async def test_an_out_of_sprint_issue_is_untouched():
    svc = _svc(sprint_priority_boost=True)
    svc.client.discover_sprint_field = AsyncMock(return_value=_FIELD)
    svc.client.search_issues = AsyncMock(return_value=[_issue("WWD-2", None)])
    tasks = await svc.import_issues({})
    assert tasks[0].priority is TaskPriority.NORMAL


@pytest.mark.asyncio
async def test_sprint_NEVER_restricts_what_is_imported():
    """THE DECISION THIS FEATURE TURNS ON. Filtering to a sprint would hide genuinely
    assigned work — a surprising way to lose a ticket."""
    svc = _svc(sprint_priority_boost=True)
    svc.client.discover_sprint_field = AsyncMock(return_value=_FIELD)
    svc.client.search_issues = AsyncMock(
        return_value=[
            _issue("WWD-1", [{"name": "S", "state": "active"}]),
            _issue("WWD-2", None),
            _issue("WWD-3", [{"name": "S0", "state": "closed"}]),
        ]
    )
    tasks = await svc.import_issues({})
    assert {t.jira_key for t in tasks} == {"WWD-1", "WWD-2", "WWD-3"}, (
        "sprint membership decided what was imported; it must only decide order"
    )
    assert "sprint" not in svc.build_jql().lower(), "the query itself filters by sprint"


@pytest.mark.asyncio
async def test_a_site_with_no_sprint_field_imports_exactly_as_before():
    """AC-2. This is the operator's real situation on both configured projects."""
    svc = _svc(sprint_priority_boost=True)
    svc.client.discover_sprint_field = AsyncMock(return_value="")
    svc.client.search_issues = AsyncMock(return_value=[_issue("WWD-4", None)])

    tasks = await svc.import_issues({})

    assert svc.client.search_issues.await_args.kwargs.get("fields", "") == ""
    assert tasks[0].priority is TaskPriority.NORMAL


@pytest.mark.asyncio
async def test_discovery_finds_the_field_by_NAME_not_a_hardcoded_id():
    """Hardcoding a per-site id is exactly the mistake the hardcoded 'Done' transition
    made — right on one project, refused by eleven tickets on another."""
    svc = _svc()
    session = MagicMock()
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(
        return_value=[
            {"id": "customfield_99999", "name": "Sprint"},
            {"id": "customfield_10020", "name": "Story Points"},
        ]
    )
    session.get.return_value.__aenter__ = AsyncMock(return_value=resp)
    session.get.return_value.__aexit__ = AsyncMock(return_value=False)
    svc.client._ensure_session = AsyncMock(return_value=session)

    assert await svc.client.discover_sprint_field() == "customfield_99999", (
        "discovery matched a position or a hardcoded id rather than the field NAME"
    )


def test_the_default_is_off():
    """Unverifiable behaviour must not change imports for teams I cannot test against —
    neither of the operator's projects uses sprints."""
    assert JiraConfig().sprint_priority_boost is False


def test_the_setting_survives_a_config_round_trip():
    from swarm.config import HiveConfig
    from swarm.config.loader import _parse_jira_section
    from swarm.config.serialization import serialize_config

    data = serialize_config(HiveConfig(jira=JiraConfig(enabled=True, sprint_priority_boost=True)))
    assert data["jira"]["sprint_priority_boost"] is True
    assert _parse_jira_section(data["jira"]).sprint_priority_boost is True
    assert (
        Path("src/swarm/server/config_appliers/jira.py").read_text().count("sprint_priority_boost")
        >= 1
    ), "the applier ignores it, so saving does nothing"
