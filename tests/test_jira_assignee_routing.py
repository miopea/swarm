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
    """`labels = "swarm"` must not appear even when the legacy field is still set —
    the operator's live config still carries it."""
    jql = _jql(projects=["WWD"], import_label="swarm")
    assert "labels" not in jql.lower(), (
        f"the legacy label still routes imports, so the multi-dev duplication remains: {jql}"
    )


def test_a_legacy_filter_is_ignored_but_not_silently(caplog):
    """Silently ignoring configuration the operator can still see in the UI turns a
    setting into a lie about what the system is doing."""
    cfg = JiraConfig(enabled=True, projects=["WWD"], import_filter='labels = "swarm"')
    svc = JiraSyncService.__new__(JiraSyncService)
    svc._config = cfg
    with caplog.at_level(logging.WARNING):
        jql = svc.build_jql()
    assert 'labels = "swarm"' not in jql, "the legacy JQL is still being used"
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("LEGACY" in m or "legacy" in m.lower() for m in warnings), (
        f"a configured import_filter was ignored with no warning: {warnings}"
    )


def test_the_legacy_warning_fires_once_not_every_sync():
    """It runs every sync interval; warning each time would bury real signal — the
    same noise problem that made the export reconciler retry 11 tickets forever."""
    cfg = JiraConfig(enabled=True, projects=["WWD"], import_filter="x")
    svc = JiraSyncService.__new__(JiraSyncService)
    svc._config = cfg
    logger = logging.getLogger("swarm.integrations.jira")
    seen: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    handler = _Cap()
    logger.addHandler(handler)
    try:
        for _ in range(5):
            svc.build_jql()
    finally:
        logger.removeHandler(handler)
    legacy = [m for m in seen if "legacy" in m.lower() or "LEGACY" in m]
    assert len(legacy) == 1, f"the legacy warning fired {len(legacy)} times, expected once"


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
