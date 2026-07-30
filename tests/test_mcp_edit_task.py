"""#1060 — correcting a filed task's requirements through MCP.

Before this, once a task was filed nobody could fix its description:
workers had ``swarm_create_task`` and no edit, and the Queen had no edit
verb either. The failure mode is SILENT — the task keeps its stale
description and the next reader works from wrong requirements with
nothing signalling that a correction was attempted and lost.

Authority is the existing ownership rule, not a new one: a worker may
edit a task assigned to it, exactly as ``swarm_complete_task`` requires.
``acceptance_criteria`` is Queen-only because the verifier grades a
completion against it — an assignee editing its own criteria is
self-grading.
"""

from __future__ import annotations

from swarm.mcp.queen_handlers._tasks import _handle_edit_task as _queen_edit
from swarm.mcp.tools import handle_tool_call
from swarm.tasks.history import TaskAction
from swarm.tasks.task import TaskStatus
from tests.conftest import make_daemon

QUEEN = "queen"


def _daemon(monkeypatch):
    # Use the fixture's OWN board: TaskManager holds its own reference, so
    # swapping d.task_board here would leave d.edit_task looking at a
    # different board than the test writes to.
    return make_daemon(monkeypatch)


def _owned(d, worker="api", title="original title", desc="original description"):
    t = d.task_board.create(title=title, description=desc)
    d.task_board.assign(t.id, worker)
    return t


def _edit(d, worker, **args):
    return str(handle_tool_call(d, worker, "swarm_edit_task", args))


# --- the happy path -----------------------------------------------------


def test_assignee_can_correct_its_own_task(monkeypatch) -> None:
    d = _daemon(monkeypatch)
    t = _owned(d)

    out = _edit(d, "api", number=t.number, description="corrected requirements")

    assert "updated" in out
    got = d.task_board.get(t.id)
    assert got.description == "corrected requirements"
    assert got.title == "original title", "title must be untouched when not passed"


def test_title_and_description_together(monkeypatch) -> None:
    d = _daemon(monkeypatch)
    t = _owned(d)

    _edit(d, "api", number=t.number, title="new title", description="new body")

    got = d.task_board.get(t.id)
    assert (got.title, got.description) == ("new title", "new body")


# --- authority: the guard is reused, not weakened -----------------------


def test_non_assignee_is_refused(monkeypatch) -> None:
    """The unauthorised-edit case. A worker must not be able to rewrite
    another worker's requirements — same rule as swarm_complete_task."""
    d = _daemon(monkeypatch)
    t = _owned(d, worker="api")

    out = _edit(d, "web", number=t.number, description="hijacked")

    assert "not assigned to you" in out
    assert d.task_board.get(t.id).description == "original description"


def test_unassigned_task_is_refused(monkeypatch) -> None:
    """No adopt-to-edit: that trick belongs to swarm_complete_task."""
    d = _daemon(monkeypatch)
    t = d.task_board.create(title="orphan", description="original description")

    out = _edit(d, "api", number=t.number, description="grabbed")

    assert "unassigned" in out
    assert d.task_board.get(t.id).description == "original description"


def test_unknown_identity_names_the_mcp_url_not_ownership(monkeypatch) -> None:
    """#1045's fail-fast: an unresolved caller must not look like an
    ownership failure, or the reader chases the wrong problem."""
    d = _daemon(monkeypatch)
    t = _owned(d)

    out = _edit(d, "unknown", number=t.number, description="x")

    assert ".mcp.json" in out
    assert d.task_board.get(t.id).description == "original description"


def test_terminal_task_is_refused(monkeypatch) -> None:
    d = _daemon(monkeypatch)
    t = _owned(d)
    d.task_board.complete(t.id, resolution="done")
    assert d.task_board.get(t.id).status == TaskStatus.DONE

    out = _edit(d, "api", number=t.number, description="rewriting closed work")

    assert "done" in out.lower()
    assert d.task_board.get(t.id).description == "original description"


def test_no_fields_is_a_noop_with_a_message(monkeypatch) -> None:
    d = _daemon(monkeypatch)
    t = _owned(d)
    out = _edit(d, "api", number=t.number)
    assert "nothing to change" in out.lower()


# --- the edit cannot be silent ------------------------------------------


def test_edit_is_recorded_in_history_naming_the_field(monkeypatch) -> None:
    """An edit that rewrites requirements invisibly is a worse hazard than
    the stale description it fixes. The history entry existed before but
    carried an EMPTY detail — it proved something changed, never what."""
    d = _daemon(monkeypatch)
    t = _owned(d)

    _edit(d, "api", number=t.number, description="corrected requirements")

    entries = [e for e in d.task_history.get_events(t.id) if e.action == TaskAction.EDITED]
    assert entries, "the edit must appear in task history"
    detail = entries[-1].detail
    assert "description" in detail
    assert "original description" in detail, "must name the value it replaced"
    assert entries[-1].actor == "api"


# --- acceptance_criteria is Queen-only ----------------------------------


def test_worker_cannot_edit_acceptance_criteria(monkeypatch) -> None:
    """Self-grading guard: the verifier grades against these, so the
    assignee must not be able to relax a criterion it is about to fail.
    The field is not on the worker schema, and passing it is inert."""
    from swarm.mcp.handlers._edit import TOOLS

    props = TOOLS[0]["inputSchema"]["properties"]
    assert "acceptance_criteria" not in props

    d = _daemon(monkeypatch)
    t = _owned(d)
    d.edit_task(t.id, acceptance_criteria=["must be fast"], actor="queen")

    _edit(d, "api", number=t.number, acceptance_criteria=["anything goes"])

    assert d.task_board.get(t.id).acceptance_criteria == ["must be fast"]


def test_queen_can_edit_acceptance_criteria(monkeypatch) -> None:
    d = _daemon(monkeypatch)
    t = _owned(d)

    out = str(
        _queen_edit(d, QUEEN, {"number": t.number, "acceptance_criteria": ["must be correct"]})
    )

    assert "updated" in out
    assert d.task_board.get(t.id).acceptance_criteria == ["must be correct"]


def test_queen_may_edit_a_task_she_does_not_own(monkeypatch) -> None:
    """Oversight: the Queen edits regardless of owner. That is the whole
    reason she has the verb — the addenda that motivated #1060 were hers."""
    d = _daemon(monkeypatch)
    t = _owned(d, worker="api")

    _queen_edit(d, QUEEN, {"number": t.number, "description": "queen addendum"})

    assert d.task_board.get(t.id).description == "queen addendum"


def test_queen_edit_rejects_non_queen_caller(monkeypatch) -> None:
    d = _daemon(monkeypatch)
    t = _owned(d, worker="api")

    out = str(_queen_edit(d, "api", {"number": t.number, "description": "escalation"}))

    assert d.task_board.get(t.id).description == "original description"
    assert "queen" in out.lower()
