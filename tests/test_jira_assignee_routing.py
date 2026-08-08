"""Imports are routed by ASSIGNEE, not by label (Jira integration v2, phase 1).

THE PROBLEM THIS SOLVES. Jira is being enabled for every dev, each running their own
Swarm. The previous query routed by ``labels = "swarm"``, which does not survive that:
every swarm imports the SAME tickets, creates a duplicate task per dev for one issue,
and races to transition it. ``assignee = currentUser()`` gives one answer to "who owns
this" in both systems, uses semantics Jira already has, and needs no per-dev labelling
ritual that someone can forget — where the failure mode is a ticket nobody's swarm
picks up, silently.

WHY statusCategory IS THE TERMINAL TEST. statusCategory is a universal three-value
field (To Do / In Progress / Done) valid in ANY Jira workflow, so "not finished" needs
no per-project discovery. That matters because discovery IS still required for the
export transition map — a hardcoded "Done" was refused by 11 real tickets on
2026-08-07 whose workflow offered only "Waiting for support". Import and export need
different mechanisms, and conflating them is what made the hardcoded map look adequate.

Spec: docs/specs/jira-integration-v2.md
"""

from __future__ import annotations

import logging

import pytest

from swarm.config.models import JiraConfig
from swarm.integrations.jira import JiraSyncService


def _jql(**kwargs) -> str:
    cfg = JiraConfig(enabled=True, **kwargs)
    svc = JiraSyncService.__new__(JiraSyncService)
    svc._config = cfg
    return svc.build_jql()


# --- routing -----------------------------------------------------------------


def test_the_query_routes_by_assignee():
    """The whole point. Without this every dev imports every ticket."""
    jql = _jql(projects=["WWD"])
    assert "assignee = currentUser()" in jql, (
        "imports are not routed by assignee, so enabling this for a second dev "
        "duplicates every ticket and races on every transition"
    )


def test_the_query_no_longer_routes_by_label():
    """`labels = "swarm"` must not appear in the query at all. The label is now
    reserved PROVENANCE — it marks tickets Swarm created — and must never decide what
    comes in."""
    jql = _jql(projects=["WWD"])
    assert "labels" not in jql.lower(), (
        f"the label still routes imports, so the multi-dev duplication remains: {jql}"
    )


def test_the_legacy_routing_settings_are_gone_from_the_model():
    """import_filter and import_label were DELETED (2026.8.8.7), not disabled.

    They were kept briefly as inert fields so an existing config would not error. That
    is the worse state: a setting the operator can still see reads as configuration
    even when nothing consults it. The on-disk keys are handled by the stale-key
    warning instead — see tests/test_jira_workflow_discovery.py.
    """
    cfg = JiraConfig()
    for dead in ("import_filter", "import_label", "lookback_days"):
        assert not hasattr(cfg, dead), f"JiraConfig still carries the dead field {dead}"


def test_an_existing_config_carrying_the_dead_keys_still_loads(tmp_path, caplog):
    """THE UPGRADE PATH. Every dev's swarm.yaml currently has these keys. Removing a
    field must not make an existing config fail to load — and must not report it as a
    typo either, which is what an unknown key normally means."""
    import yaml

    from swarm.config.loader import load_config

    path = tmp_path / "swarm.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "workers": [],
                "jira": {
                    "enabled": True,
                    "projects": ["WWD"],
                    "import_filter": 'labels = "swarm"',
                    "import_label": "swarm",
                    "lookback_days": 30,
                },
            }
        )
    )
    with caplog.at_level(logging.WARNING):
        cfg = load_config(str(path))

    assert cfg.jira.enabled is True, "a config with the old keys failed to load"
    assert cfg.jira.projects == ["WWD"], "the surviving settings were lost"
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "import_filter" in messages, "a removed setting vanished with no word to the operator"
    assert "no longer" in messages.lower() or "removed" in messages.lower(), (
        f"the removed keys are reported as unrecognized (i.e. as typos): {messages}"
    )


# --- scope -------------------------------------------------------------------


def test_multiple_projects_are_supported():
    jql = _jql(projects=["WWD", "IS"])
    assert '"WWD"' in jql and '"IS"' in jql and "project IN" in jql


def test_the_legacy_single_project_field_still_works():
    """An install that only ever set `project` must keep syncing — nothing rewrites
    the operator's config behind their back."""
    jql = _jql(project="WWD")
    assert 'project IN ("WWD")' in jql


def test_no_configured_project_imports_NOTHING():
    """The fail-safe direction. Importing everything by default would put an entire
    Jira site on one dev's board; an empty query imports nothing and is obvious."""
    assert _jql() == "", "an unconfigured install would import the whole site"


def test_terminal_issues_are_excluded_by_status_category():
    """statusCategory is workflow-agnostic, so this works on a project whose states
    nobody has mapped yet."""
    assert "statusCategory != Done" in _jql(projects=["WWD"])


def test_epics_are_not_imported():
    """An Epic is a container, not work: a worker cannot finish one, and it would sit
    open for months — the shape that produced the stale-blocker problems."""
    jql = _jql(projects=["WWD"])
    assert "issuetype IN" in jql
    assert "Epic" not in jql, f"Epics are being imported as ordinary tasks: {jql}"
    for wanted in ("Story", "Task", "Bug", "Sub-task"):
        assert wanted in jql, f"{wanted} is not imported"


def test_issue_types_are_configurable_and_omitted_when_empty():
    """Swarm is used outside this team; a project with custom types must not be
    forced through this list. Empty means 'no type filter', not 'no issues'."""
    jql = _jql(projects=["WWD"], issue_types=[])
    assert "issuetype" not in jql
    assert "assignee = currentUser()" in jql, "dropping the type filter lost the routing"


# --- injection safety --------------------------------------------------------


@pytest.mark.parametrize("hostile", ['A"B', "A\\B", 'x" OR project = "Y'])
def test_project_names_cannot_break_out_of_the_jql_string(hostile: str):
    """Project names reach JQL as string literals. An unescaped quote would let a
    configured value change the query's meaning — the same class as SQL injection,
    against a query that decides what work a dev is handed."""
    import re

    jql = _jql(projects=[hostile])
    body = jql[jql.index("project IN (") : jql.index(") AND assignee")]
    # Count UNESCAPED quotes only. Counting every `"` fails on correctly-escaped input
    # (\" is two characters but zero delimiters) — the first version of this test
    # flagged working escaping as a vulnerability.
    unescaped = len(re.findall(r'(?<!\\)"', body))
    assert unescaped == 2, (
        f"the project literal has {unescaped} unescaped quotes, so the configured "
        f"value can end the string and change the query's meaning: {body}"
    )
    # Deliberately NOT asserting that the hostile text is absent: it is the project
    # NAME, so of course it appears inside the literal. Stripping the escapes and then
    # searching for "OR project" reads the literal's own contents and proves nothing —
    # the balanced-delimiter count above is what shows it cannot become a clause.
    assert body.startswith('project IN ("') and body.endswith('"'), (
        f"the literal is not delimited as expected: {body}"
    )
