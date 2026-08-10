"""Connected-but-switched-off must not look like working (Vicky, 2026-08-10).

THE REPORT. A second developer set Jira up, saw "✓ Connected — OAuth active (cloud:
c6642249…)", then hit "Discover workflow" for WWD and got "Jira integration not enabled".
Both statements were true and they contradict each other on screen.

TWO INDEPENDENT FLAGS:
  * ``connected``  — an OAuth token exists (``/auth/jira/status``)
  * ``enabled``    — the integration's own setting, which EVERY /api/jira/* route gates on

The banner was rendered from the OAuth payload alone, and that payload never carried
``enabled``. So the UI could not have told her, and the error named the flag without
saying where it lives — which reads as "your connection failed" when the connection is
fine.

Onboarding cost: she had no way to get from the error to the checkbox. That is the bug —
not the refusal itself, which is correct.
"""

from __future__ import annotations

from pathlib import Path

_AUTH = Path("src/swarm/web/routes/auth.py").read_text(encoding="utf-8")
_CONFIG = Path("src/swarm/web/templates/config.html").read_text(encoding="utf-8")
_JIRA = Path("src/swarm/server/routes/jira.py").read_text(encoding="utf-8")


def test_the_oauth_status_reports_the_enabled_flag():
    """The UI cannot show what it is never told."""
    i = _AUTH.index("async def handle_jira_auth_status")
    body = _AUTH[i : i + 1400]
    assert '"enabled"' in body, (
        "/auth/jira/status still omits `enabled`, so the banner cannot distinguish "
        "'connected and working' from 'connected and switched off'"
    )


def test_the_banner_calls_out_connected_but_disabled():
    """The exact state Vicky was in. Green + working-looking is the failure."""
    assert "data.enabled === false" in _CONFIG, (
        "the banner still reports a bare 'Connected' while every Jira action refuses"
    )


def test_that_case_is_not_styled_as_success():
    """Amber, not green: the connection is real but nothing will work."""
    i = _CONFIG.index("data.enabled === false")
    branch = _CONFIG[i : _CONFIG.index("} else if (data.connected)", i)]
    assert "var(--honey)" in branch, "connected-but-disabled is still styled as success"
    assert "status-connected" not in branch, "it still uses the success badge class"


def test_the_banner_says_where_to_fix_it():
    """A warning that does not name the control is only a nicer dead end."""
    i = _CONFIG.index("data.enabled === false")
    branch = _CONFIG[i : _CONFIG.index("} else if (data.connected)", i)]
    assert "enabled" in branch.lower() and "save" in branch.lower(), (
        "the banner does not point at the enabled checkbox"
    )


def test_the_api_error_is_actionable_everywhere():
    """Seven routes returned the same bare string. It is the message a user meets AFTER
    a successful connection, so it has to say where to look."""
    assert "_NOT_ENABLED" in _JIRA, "the shared actionable message is gone"
    assert 'json_error("Jira integration not enabled"' not in _JIRA, (
        "a route still returns the bare, unactionable message"
    )
    i = _JIRA.index("_NOT_ENABLED = (")
    msg = _JIRA[i : i + 400]
    assert "Settings" in msg and "enabled" in msg, (
        "the message names neither the setting nor where it lives"
    )
