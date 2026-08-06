"""A parked task must be startable, and the refusal must name a real action (#1286).

FOUND 2026-08-06 trying to start #1269. Verbatim, twice:

    #1269 is parked. Nothing changed. Starting it will un-park it —
    re-call this to resume it deliberately.

Re-calling produced the IDENTICAL refusal. ``_start.py`` returned that whenever
``target.is_on_hold``, with no confirmation token, no parameter, and no state that
could make a second call differ. The named resolution was a provable no-op, and no
code path ever removed the hold tag either — so even a caller who got past the
refusal would have left a parked task in progress.

WORSE THAN #1057's SHAPE, which this file's own module docstring cites: #1057 was a
refusal that WITHHELD the resolving fact. This one STATED a resolving fact that was
false, which is strictly worse — a caller who trusts it retries forever. An agent
caller does exactly that, and did.

THIRD INSTANCE OF HOLD-CLASS UNREACHABILITY ON A THIRD VERB: #1270 (edit,
structurally unreachable), #1281 (assign, refused at two layers by the
auto-assigner's predicate), and now start. #1270's own resolution predicted it:
"the class is probably not exhausted."
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from swarm.mcp.handlers._start import _handle_start_task
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import TaskStatus


def _text(result) -> str:
    return " ".join(r["text"] for r in result)


@pytest.fixture
def d():
    daemon = MagicMock()
    daemon.task_board = TaskBoard()
    daemon.task_history = MagicMock()
    daemon.drone_log = MagicMock()
    daemon.jira_svc = MagicMock()
    return daemon


def _parked_assigned(d, worker="swarm"):
    t = d.task_board.create(title="parked work", tags=["hold"])
    assert d.task_board.assign(t.id, worker, override_hold=True)
    assert d.task_board.get(t.id).status == TaskStatus.ASSIGNED
    assert d.task_board.get(t.id).is_on_hold
    return t


def test_the_refusal_names_an_action_that_actually_works(d):
    """THE LOAD-BEARING TEST, and it is written as the two-call sequence rather than
    as a string check on purpose: asserting the refusal *mentions* unpark would pass
    even if unpark did nothing, which is the exact defect being fixed. So do what the
    refusal says and assert the outcome changes."""
    t = _parked_assigned(d)

    first = _text(_handle_start_task(d, "swarm", {"task_number": t.number}))
    assert "unpark=true" in first, f"refusal does not name the resolving action: {first}"
    assert d.task_board.get(t.id).status == TaskStatus.ASSIGNED, "refusal mutated the board"

    second = _text(_handle_start_task(d, "swarm", {"task_number": t.number, "unpark": True}))

    after = d.task_board.get(t.id)
    assert after.status == TaskStatus.ACTIVE, (
        f"doing what the refusal instructed did not start the task: {second}"
    )
    assert not after.is_on_hold, "task is in progress but still parked — the hold was not cleared"


def test_repeating_the_bare_call_is_still_refused_not_silently_accepted(d):
    """The old text invited a bare retry. A bare retry must keep refusing — the point
    is that HOLD requires an explicit statement, not persistence."""
    t = _parked_assigned(d)
    for _ in range(3):
        out = _text(_handle_start_task(d, "swarm", {"task_number": t.number}))
        assert "unpark=true" in out
    assert d.task_board.get(t.id).status == TaskStatus.ASSIGNED


def test_unpark_does_not_make_other_workers_able_to_pick_it_up(d):
    """#894's constraint. Clearing the hold on an EXPLICIT start by the owner is
    safe, but an unstarted parked task must stay out of the auto-assigner's candidate
    set — otherwise this fix hands parked work to the drone."""
    parked = d.task_board.create(title="still parked", tags=["hold"])
    assert parked.is_available is False
    assert parked.id not in {x.id for x in d.task_board.available_tasks}

    # And the one we DO start is ACTIVE, so it is not available either.
    t = _parked_assigned(d)
    _handle_start_task(d, "swarm", {"task_number": t.number, "unpark": True})
    assert d.task_board.get(t.id).id not in {x.id for x in d.task_board.available_tasks}


def test_unpark_on_a_task_that_is_not_parked_is_harmless(d):
    """Passing the flag unnecessarily must not strip unrelated tags or fail."""
    t = d.task_board.create(title="ordinary", tags=["backend", "urgent-ish"])
    d.task_board.assign(t.id, "swarm")
    _handle_start_task(d, "swarm", {"task_number": t.number, "unpark": True})
    after = d.task_board.get(t.id)
    assert after.status == TaskStatus.ACTIVE
    assert set(after.tags) == {"backend", "urgent-ish"}, "unpark stripped non-hold tags"


def test_unparking_keeps_non_hold_tags(d):
    """Only the hold tags go. Losing a task's other tags would be a silent data loss
    hiding inside a convenience flag."""
    t = d.task_board.create(title="mixed", tags=["hold", "backend", "dormant"])
    d.task_board.assign(t.id, "swarm", override_hold=True)
    _handle_start_task(d, "swarm", {"task_number": t.number, "unpark": True})
    after = d.task_board.get(t.id)
    assert after.status == TaskStatus.ACTIVE
    assert set(after.tags) == {"backend"}, f"expected only hold tags removed, got {after.tags}"


def test_no_mcp_refusal_promises_an_action_that_cannot_work():
    """AC-2 of #1286, generalised past this one instance.

    Sweeps refusal strings that tell the caller to retry and requires each to name a
    parameter or a different verb — a bare "re-call this" with nothing to change is
    the defect. Carries a positive control so an empty scan cannot pass.
    """
    import ast
    import pathlib
    import re

    root = pathlib.Path(__file__).parent.parent / "src" / "swarm" / "mcp"
    paths = sorted(root.rglob("*.py"))
    assert len(paths) > 5, f"scan found only {len(paths)} mcp modules — it is broken"

    def _messages(tree: ast.AST) -> list[str]:
        """Every string message in the module, as the reader receives it.

        AST rather than regex over source, deliberately. A regex over flattened
        source bleeds past the end of the string into surrounding code — which made
        an earlier version of this test pass against a deliberately planted bad
        refusal, since the following code always contains an '='. Comments are
        excluded for free, which also matters: the fix in _start.py carries a comment
        QUOTING the old bad text.
        """
        out: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.append(node.value)
            elif isinstance(node, ast.JoinedStr):
                # f-string: keep the literal parts, stand in for interpolations.
                parts = [
                    p.value if isinstance(p, ast.Constant) and isinstance(p.value, str) else "{}"
                    for p in node.values
                ]
                out.append("".join(parts))
        return out

    offenders = []
    for path in paths:
        for msg in _messages(ast.parse(path.read_text())):
            if not re.search(r"re-call (?:this|it)", msg, re.I):
                continue
            # Actionable if the SAME message names a parameter, value or other verb.
            if not re.search(r"=|with (?:the )?\w+|task_number|unpark|swarm_\w+", msg, re.I):
                offenders.append(f"{path.name}: {msg.strip()[:90]}")

    assert not offenders, (
        "these refusals tell the caller to re-call without naming anything to "
        f"change, so the retry cannot succeed: {offenders}"
    )
