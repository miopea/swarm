"""Criteria reach every path that can make a Jira-linked task assignable (#1354).

WHY THIS MATTERS BEYOND BOOKKEEPING. The verifier DEFAULT-PASSES a task with no
acceptance criteria, so a task that becomes assignable without them is unverifiable by
construction — it is graded as passing whatever happens. project-root filed 109 Jira
tickets (WWD, labels = "v4v6-audit") whose release mechanism is Queen assignment, one at
a time, and Queen held the whole backlog until this landed.

TWO SEPARATE GAPS, and fixing only the first leaves all 109 broken.

GAP A — create-then-link. ``_synthesize_criteria_if_missing`` is scoped to tasks that
already carry a jira_key. Right for the import flow, backwards for the dashboard's
"create Jira issue" button: measured on #1352, the task was CREATED and ASSIGNED at
17:03:19 with no key, creation-time synthesis returned nothing at 17:03:26, the link
landed at 17:03:31, and nothing retried. Fixed at LINK time — that is when the Jira
context arrives, and re-running at assign time would feed synthesis the same pre-Jira
description that already came back empty.

GAP B — the Queen assign path never synthesized AT ALL, and never even recorded an
ASSIGNED row. ``_handle_reassign_task`` assigns through the raw ``task_board.assign``,
bypassing the coordinator's ``assign_task`` which is what writes history and fires the
hook. Measured on #1358 (WWD-6726, a real v4v6-audit import): assigned by the Queen,
history contained neither an ASSIGNED row nor a synthesis attempt.

The ASSIGNED row is part of the fix, not decoration: it is what distinguishes "synthesis
never fired" from "synthesis fired and returned empty" (the latter logs an EDITED row
with an empty detail). Without it the two are indistinguishable from the criteria field
alone, which is what made this take two sittings to characterise.
"""

from __future__ import annotations

import re
from pathlib import Path

_QUEEN = Path("src/swarm/mcp/queen_handlers/_tasks.py").read_text(encoding="utf-8")
_JIRA_ROUTE = Path("src/swarm/server/routes/jira.py").read_text(encoding="utf-8")
_COORD = Path("src/swarm/server/task_coordinator.py").read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    i = src.index("def " + name)
    nxt = src.find("\ndef ", i + 1)
    return src[i : nxt if nxt != -1 else len(src)]


# --- Gap B: the release gate -------------------------------------------------------


def test_the_queen_assign_path_writes_an_ASSIGNED_row():
    """Without this, a missing synthesis is indistinguishable from an empty one."""
    body = _fn(_QUEEN, "_handle_reassign_task")
    assert "TaskAction.ASSIGNED" in body, (
        "queen_reassign_task still records no ASSIGNED row, so history cannot tell "
        "'never fired' from 'fired and returned empty'"
    )


def test_the_queen_assign_path_synthesizes_criteria():
    """THE GATE for the 109-ticket backlog."""
    body = _fn(_QUEEN, "_handle_reassign_task")
    assert "apply_synthesized_criteria" in body, (
        "the Queen assign path still never synthesizes; every v4v6-audit ticket it "
        "releases arrives unverifiable"
    )


def test_the_queen_path_keeps_the_linked_and_empty_scoping():
    """A model call must not be added to flows that never lacked criteria."""
    body = _fn(_QUEEN, "_handle_reassign_task")
    i = body.index("apply_synthesized_criteria")
    guard = body[max(0, i - 400) : i]
    assert "jira_key" in guard, "synthesis is no longer scoped to linked tasks"
    assert "acceptance_criteria" in guard, (
        "a task that already has criteria would trigger another model call"
    )


# --- Gap A: create-then-link -------------------------------------------------------


def test_linking_a_task_triggers_synthesis():
    """The moment the Jira context arrives is the moment worth retrying."""
    assert "apply_synthesized_criteria" in _JIRA_ROUTE, (
        "linking a task to a Jira issue still never synthesizes; a create-then-link "
        "task stays criteria-less forever"
    )


def test_the_link_trigger_skips_a_task_that_already_has_criteria():
    # Anchored on the CALL, not the name: the comment above it also mentions
    # apply_synthesized_criteria, and index() found the prose instead of the code — the
    # same "matched my own comment" mistake this codebase has hit repeatedly.
    i = _JIRA_ROUTE.index("await d.tasks.apply_synthesized_criteria")
    guard = _JIRA_ROUTE[max(0, i - 300) : i]
    assert "acceptance_criteria" in guard, "linking re-synthesizes over existing criteria"


def test_the_assign_hook_stays_scoped_to_linked_tasks():
    """Deliberately NOT widened. Gap A is fixed at link time instead, which is cheaper
    and is where the Jira description actually exists."""
    body = _fn(_COORD, "_synthesize_criteria_if_missing")
    assert 'getattr(task, "jira_key", "")' in body, (
        "the assign hook was widened; that adds a model call to locally-created tasks "
        "that already get criteria at creation"
    )


def test_the_reason_for_each_trigger_point_is_written_down():
    """Required by the ticket, and load-bearing: the next person will otherwise 'simplify'
    one of these back out. Both blocks cite the measured task they came from."""
    assert "#1354" in _QUEEN and "#1354" in _JIRA_ROUTE
    assert re.search(r"#135[28]", _QUEEN + _JIRA_ROUTE), (
        "the measured evidence (#1352 for Gap A, #1358 for Gap B) is not cited"
    )
