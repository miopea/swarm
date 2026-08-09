"""The Jira setup doc must stay true as the code changes.

Documentation drifts silently and is believed anyway — config.html told operators for
months that tokens lived in ~/.swarm/jira_tokens.json after they had moved into the
database. This doc is the first thing every developer will follow while pointing a write
-capable integration at a shared tracker, so its factual claims are pinned here.

Only checks claims that are MECHANICALLY verifiable. Prose and ordering advice are not
tested — they are judgement, and a test asserting the wording would just be noise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_DOC = Path(__file__).parent.parent / "docs" / "jira-setup.md"


@pytest.fixture(scope="module")
def doc() -> str:
    assert _DOC.exists(), "the Jira setup doc is missing; every dev needs it to onboard"
    return _DOC.read_text()


def test_every_scope_it_names_is_actually_requested(doc: str):
    """A doc that lists a scope Swarm does not ask for sends the reader to configure
    something that then silently does not work — which is how the read:jira-user gap
    produced unassigned tickets for a week."""
    from swarm.auth.jira import _SCOPE

    for scope in ("read:jira-work", "write:jira-work", "read:jira-user", "offline_access"):
        assert scope in doc, f"{scope} is requested but undocumented"
        assert scope in _SCOPE, f"{scope} is documented but never requested"


def test_the_issue_types_match_the_import_filter(doc: str):
    from swarm.config.models import JiraConfig

    for t in JiraConfig().issue_types:
        assert t in doc, f"{t} is imported but the doc does not say so"
    assert "Epic" not in JiraConfig().issue_types, "Epics are imported; the doc says they are not"


def test_the_sync_marker_it_quotes_is_the_real_one(doc: str):
    """The doc tells readers that text above this marker is preserved. If the marker
    changed, that promise would be about a string that no longer exists."""
    from swarm.tasks.task import JIRA_SYNC_MARKER

    assert JIRA_SYNC_MARKER.strip() in doc


def test_the_reserved_label_it_names_is_the_real_one(doc: str):
    from swarm.integrations.jira import JiraSyncService

    assert f"`{JiraSyncService.PROVENANCE_LABEL}` label is reserved" in doc


def test_the_default_sync_interval_is_right(doc: str):
    from swarm.config.models import JiraConfig

    assert str(int(JiraConfig().sync_interval_minutes)) in doc


def test_it_does_not_repeat_the_stale_token_location(doc: str):
    """config.html claimed ~/.swarm/jira_tokens.json long after tokens moved into the
    secrets table. Pinned so the doc cannot inherit the same mistake."""
    assert "jira_tokens.json" not in doc
    assert "secrets" in doc


def test_the_verbs_and_settings_it_names_exist(doc: str):
    from swarm.config.models import JiraConfig
    from swarm.mcp.tools import _HANDLERS

    assert "swarm_request_jira_ticket" in _HANDLERS
    for field in ("read_only",):
        assert hasattr(JiraConfig(), field), f"the doc documents jira.{field}, which does not exist"


def test_the_read_only_ordering_advice_is_still_sound(doc: str):
    """Step 2 tells the reader to enable read-only BEFORE configuring projects. That is
    only safe advice while read-only actually blocks writes at the client."""
    src = Path("src/swarm/integrations/jira.py").read_text()
    assert "_refuse_write" in src, "read-only no longer blocks writes; step 2 is now wrong"
    assert "Read-only mode" in doc
