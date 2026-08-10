"""A Jira-synced task must show, and link to, its ticket (#1359).

THE REPORT. The operator watched the first synced ticket land and said: "there is
nothing in the issue that links back to the jira ticket or makes it obvious it was a
jira synced item (except a little text)."

He was exactly right, and the cause was mundane: ``jira_key`` has been in the task API
payload all along (routes/tasks.py) and was rendered NOWHERE. The only trace was prose
under the ``--- Jira sync ---`` marker in the description.

WHY THE URL COMES FROM THE SERVER. The client cannot build it. Swarm talks to Jira via
``api.atlassian.com/ex/jira/<cloudId>``, which is not a browsable address. The
human-facing host is the ``url`` field of the OAuth accessible-resources response,
discovered and persisted at auth time as ``_site_url``.
"""

from __future__ import annotations

from pathlib import Path

_PARTIAL = Path("src/swarm/web/templates/partials/task_list.html").read_text(encoding="utf-8")
_PAGES = Path("src/swarm/web/routes/pages.py").read_text(encoding="utf-8")
_BASE = Path("src/swarm/web/templates/base.html").read_text(encoding="utf-8")


def test_the_key_is_rendered_at_all():
    """THE BUG, at its simplest: the field existed and nothing displayed it."""
    assert "t.jira_key" in _PARTIAL, "the Jira key is still not rendered on task rows"


def test_it_links_to_the_ticket_when_a_site_is_known():
    assert "/browse/{{ t.jira_key }}" in _PARTIAL, "the badge does not link to the issue"
    assert 'target="_blank"' in _PARTIAL and 'rel="noopener"' in _PARTIAL, (
        "the Jira link opens in place or without noopener"
    )


def test_clicking_the_link_does_not_also_open_the_task():
    """The row itself is clickable (`task-row-clickable`). Without stopPropagation the
    Jira link would open the ticket AND the task modal — every single time."""
    i = _PARTIAL.index("jira-badge")
    assert "event.stopPropagation()" in _PARTIAL[i : i + 400], (
        "the badge does not stop the row's click handler, so it also opens the task"
    )


def test_provenance_survives_a_missing_site_url():
    """Tokens predating site_url discovery exist. Losing the LINK is acceptable; losing
    the fact that a task came from Jira is the original complaint returning."""
    assert "jira-badge-plain" in _PARTIAL, "no fallback badge when the site URL is absent"


def test_the_site_url_is_supplied_by_the_server():
    """The client genuinely cannot derive this — the API host is not browsable."""
    assert '"jira_site_url"' in _PAGES, "the page context does not expose the site URL"
    assert "_site_url" in _PAGES, "the site URL is not read from the token manager"


def test_a_missing_or_broken_jira_never_breaks_the_dashboard():
    """A dashboard that will not render because Jira is unconfigured is a far worse bug
    than a missing hyperlink."""
    i = _PAGES.index("def _jira_site_url")
    body = _PAGES[i : i + 700]
    assert "except Exception" in body and 'return ""' in body


def test_the_badge_is_styled_as_a_link_not_a_button():
    """It navigates away. Dressing navigation as an action is how people click it
    expecting a dialog."""
    assert ".jira-badge {" in _BASE, "the badge has no styling and will inherit link blue"
    assert "a.jira-badge:hover" in _BASE
