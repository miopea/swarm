"""#1695 — the citation check fires when a Jira ticket CLOSES.

THE GAP, WITH ITS EXACT INSTANCE. `verify-citations.py` runs on push and pull_request.
A Jira transition reaches neither. So WWD-6829 was closed on 2026-08-16 citing
`rcg-architecture/docs/cross-service/language-es-fr-workflow-plan.md` as its authoritative
decision record — at the exact instant that file was unreadable from origin/main — and
nothing noticed. A human found it hours later reading a merge report.

WHY CLOSE AND NOT CREATION. A citation in an OPEN ticket is often aspirational: the
document is still being written. Firing on creation would cry wolf on legitimate work in
progress, which is how a gate trains people to ignore it. Closing is when a citation stops
being a working note and becomes THE RECORD — the thing the next person is sent to read.
Operator ruling, and it is pinned by a test here rather than left to a comment.

WARN, NOT BLOCK. Blocking a close would strand a worker whose document is merging in a
parallel PR, and would leave the Swarm task DONE while the ticket stayed open — a
divergence this codebase already treats as a defect. The Queen receives the warning
because she triages and is a channel a human reads.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from swarm.integrations import jira as jira_mod
from swarm.integrations.jira import JiraSyncService


def _svc(notify=None, checker="/nonexistent/verify-citations.py"):
    svc = JiraSyncService.__new__(JiraSyncService)
    svc._notify_queen = notify
    svc._config = MagicMock()
    return svc


def _task(desc: str = "", key: str = "WWD-6829"):
    t = MagicMock()
    t.jira_key = key
    t.title = "Adopt the ES/FR workflow"
    t.description = desc
    t.resolution = ""
    return t


CITING_BODY = "Decision record: rcg-architecture/docs/cross-service/language-es-fr-workflow-plan.md"


# ---------------------------------------------------------------------------
# AC6 — creation must NOT fire; only a terminal transition does
# ---------------------------------------------------------------------------


def test_only_a_terminal_transition_triggers_the_check(monkeypatch):
    """THE OPERATOR'S RULING, PINNED. A citation in an open ticket is often aspirational
    because the document is still being written; firing on creation would cry wolf on
    legitimate work in progress."""
    import inspect

    src = inspect.getsource(jira_mod.JiraSyncService.export_status)

    assert "_check_citations_on_close" in src
    guard = src.split("_check_citations_on_close")[0]
    assert "new_status in _TERMINAL_STATUSES" in guard, (
        "the citation check is not gated on a terminal transition — it would fire on "
        "every status change, including ones that mean work has only just begun"
    )


def test_the_check_is_not_reachable_from_any_creation_path():
    """The other direction: nothing outside the terminal branch calls it."""
    import inspect

    whole = inspect.getsource(jira_mod)
    calls = [
        ln.strip()
        for ln in whole.splitlines()
        if "_check_citations_on_close(" in ln and "def " not in ln
    ]

    assert len(calls) == 1, f"expected exactly one call site, found: {calls}"


# ---------------------------------------------------------------------------
# AC2 — the checker is CALLED, not reimplemented
# ---------------------------------------------------------------------------


def test_it_shells_out_to_verify_citations_rather_than_matching_paths_itself(monkeypatch):
    """#1691 declined to build this precisely so a second extractor would not exist.
    Everything that makes extraction correct — URLs stripped first, punctuation trimmed,
    directory refs dropped, bare paths treated as prose — lives in that script."""
    seen: dict = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["input"] = kw.get("input", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(jira_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(jira_mod.Path, "exists", lambda _self: True)

    _svc()._check_citations_on_close(_task(CITING_BODY))

    assert "verify-citations.py" in " ".join(seen["cmd"])
    assert "--scan-text" in seen["cmd"]
    assert CITING_BODY.split(": ")[1] in seen["input"]


def test_no_citation_regex_lives_in_the_jira_module():
    """THE SECOND-RESOLVER TRIPWIRE. A regex here would drift from the script's, and the
    drift would be invisible until it disagreed on a real ticket."""
    import inspect

    src = inspect.getsource(jira_mod)

    assert "rcg-architecture/" not in src.replace("rcg/claude-team-config", "")
    assert "claude-team-config/(" not in src


# ---------------------------------------------------------------------------
# AC4 — warn, not block; and the warning reaches the Queen
# ---------------------------------------------------------------------------


def test_a_dangling_citation_warns_the_queen(monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        jira_mod.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "DANGLING: x/docs/y.md", ""),
    )
    monkeypatch.setattr(jira_mod.Path, "exists", lambda _self: True)

    _svc(notify=lambda k, c: sent.append((k, c)))._check_citations_on_close(_task(CITING_BODY))

    assert len(sent) == 1
    assert "WWD-6829" in sent[0][1]
    assert "DOES NOT RESOLVE" in sent[0][1]


def test_the_queen_message_states_the_limit(monkeypatch):
    """AC5. It verifies the file EXISTS; it does not verify the claim about it is true.
    #1681 is why: measure.py was present the whole time and 'it enforces the equivalent
    rule' was still false. A green close must not read as a verified decision record."""
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        jira_mod.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "DANGLING: x", ""),
    )
    monkeypatch.setattr(jira_mod.Path, "exists", lambda _self: True)

    _svc(notify=lambda k, c: sent.append((k, c)))._check_citations_on_close(_task(CITING_BODY))

    assert "does NOT verify" in sent[0][1] or "DOES NOT verify" in sent[0][1].upper()
    assert "NOT blocked" in sent[0][1]


def test_a_clean_result_says_nothing(monkeypatch):
    """POSITIVE CONTROL. A notifier that fired unconditionally would pass the test above
    and make every close noise — which is how a warning channel gets muted."""
    sent: list = []
    monkeypatch.setattr(
        jira_mod.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")
    )
    monkeypatch.setattr(jira_mod.Path, "exists", lambda _self: True)

    _svc(notify=lambda k, c: sent.append(c))._check_citations_on_close(_task(CITING_BODY))

    assert sent == []


# ---------------------------------------------------------------------------
# It must never be able to break a Jira export
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "boom",
    [OSError("no python"), subprocess.TimeoutExpired("cmd", 30), subprocess.SubprocessError()],
)
def test_a_failing_checker_never_raises(monkeypatch, boom):
    def _raise(cmd, **kw):
        raise boom

    monkeypatch.setattr(jira_mod.subprocess, "run", _raise)
    monkeypatch.setattr(jira_mod.Path, "exists", lambda _self: True)

    _svc()._check_citations_on_close(_task(CITING_BODY))  # must not raise


def test_a_missing_checker_is_LOUD_rather_than_silent(monkeypatch, caplog):
    """'The checker is not installed' and 'the citations are fine' are different claims.
    A check that quietly does nothing is the exact defect this ticket is downstream of."""
    import logging

    monkeypatch.setattr(jira_mod.Path, "exists", lambda _self: False)

    with caplog.at_level(logging.WARNING):
        _svc()._check_citations_on_close(_task(CITING_BODY))

    assert "WITHOUT a citation check" in caplog.text


def test_a_notifier_that_raises_does_not_break_the_close(monkeypatch):
    monkeypatch.setattr(
        jira_mod.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "DANGLING: x", ""),
    )
    monkeypatch.setattr(jira_mod.Path, "exists", lambda _self: True)

    def _boom(_k, _c):
        raise RuntimeError("message store down")

    _svc(notify=_boom)._check_citations_on_close(_task(CITING_BODY))  # must not raise
