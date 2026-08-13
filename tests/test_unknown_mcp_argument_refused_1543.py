"""#1543 fix (a) — an argument the tool does not declare must not return success.

THE DEFECT. Nothing validated argument keys anywhere in the dispatcher, so ANY
misspelled parameter to ANY of ~40 verbs was silently discarded and the call still
returned "Task created: #NNNN". Five tasks in one session were filed with
`assigned_worker=<name>` — a key `swarm_create_task` does not declare, it declares
`target_worker` — and every one landed ownerless while the named workers slept. Three
were launch-critical. A rejected argument would have been fixed in seconds.

THIS IS THE HALF THAT WOULD HAVE CAUGHT THE REPORTED FIVE. The sibling fixes on this
ticket (`target_worker` validation, `priority` application) catch bad VALUES; this
catches a bad KEY, which is what actually happened.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from swarm.mcp.tools import _ALLOWED_ARGS, _unknown_argument_error, handle_tool_call


def _text(result) -> str:
    blocks = result.get("content") if isinstance(result, dict) else result
    return blocks[0]["text"]


# ---------------------------------------------------------------------------
# The reported defect
# ---------------------------------------------------------------------------


def test_the_exact_reported_call_is_refused():
    """`assigned_worker` on swarm_create_task — the key that cost five tasks."""
    d = MagicMock()

    args = {"title": "t", "assigned_worker": "admin"}
    out = _text(handle_tool_call(d, "queen", "swarm_create_task", args))

    assert "unknown argument 'assigned_worker'" in out


def test_the_refusal_names_the_argument_that_was_meant():
    """The suggestion is what turns a refusal into a fix. A caller told only
    "unknown argument" guesses again — the loop that made the silence expensive."""
    args = {"title": "t", "assigned_worker": "admin"}
    out = _text(handle_tool_call(MagicMock(), "queen", "swarm_create_task", args))

    assert "did you mean 'target_worker'" in out


def test_the_handler_never_runs():
    """Refused BEFORE dispatch, so nothing is half-done. Asserted separately from
    the message because a refusal that still created the task would read identically
    to the caller — which is precisely the original defect."""
    d = MagicMock()

    handle_tool_call(d, "queen", "swarm_create_task", {"title": "t", "assigned_worker": "admin"})

    d.create_task.assert_not_called()


def test_a_valid_call_still_works():
    """POSITIVE CONTROL. Without it, a check that refused EVERYTHING would pass every
    test above while breaking all ~40 verbs."""
    d = MagicMock()

    handle_tool_call(d, "queen", "swarm_create_task", {"title": "t", "target_worker": "platform"})

    d.create_task.assert_called_once()


# ---------------------------------------------------------------------------
# Fail-open — the direction this ticket got backwards twice
# ---------------------------------------------------------------------------


def test_a_tool_with_no_schema_accepts_everything():
    """THE MOST LIKELY WAY THIS CHANGE GOES WRONG, so it is pinned rather than
    trusted to a comment.

    A tool absent from _ALLOWED_ARGS has no usable schema. That means "could not
    determine what is allowed", NOT "nothing is allowed" — refusing there would
    break the verb outright, a worse failure than the one being fixed. The same
    direction was got backwards twice on this ticket (an empty worker roster, and
    a display-derived worker state).
    """
    assert _unknown_argument_error("tool-with-no-schema", {"anything": 1, "at": "all"}) is None


def test_an_empty_property_set_is_treated_as_unknowable_not_as_forbidding_all():
    """A schema present but with no properties is the same class as an absent one."""
    assert "totally-made-up-tool" not in _ALLOWED_ARGS
    assert _unknown_argument_error("totally-made-up-tool", {"x": 1}) is None


# ---------------------------------------------------------------------------
# Shape of the guard
# ---------------------------------------------------------------------------


def test_every_registered_tool_with_a_schema_is_covered():
    """The point of a CENTRAL check: ~40 verbs inherit it, rather than each needing
    its own guard — the mistake hooks.py records for _NEVER_AUTO_APPROVE, where a
    guard in one of several paths left the rest open."""
    assert len(_ALLOWED_ARGS) > 30, f"only {len(_ALLOWED_ARGS)} tools have enforced schemas"
    assert "swarm_create_task" in _ALLOWED_ARGS
    assert "target_worker" in _ALLOWED_ARGS["swarm_create_task"]
    assert "assigned_worker" not in _ALLOWED_ARGS["swarm_create_task"]


def test_all_unknown_keys_are_named_not_just_the_first():
    """A caller fixing one and re-running to find the next is the same guess-again
    loop the suggestion exists to prevent."""
    args = {"title": "t", "bogus_one": 1, "bogus_two": 2}
    out = _text(handle_tool_call(MagicMock(), "queen", "swarm_create_task", args))

    assert "bogus_one" in out and "bogus_two" in out


@pytest.mark.parametrize("tool", ["swarm_report_progress", "swarm_send_message"])
def test_the_guard_is_not_specific_to_create_task(tool: str):
    """It is a dispatcher-level rule, so an undeclared key is refused on any verb."""
    out = _text(handle_tool_call(MagicMock(), "queen", tool, {"definitely_not_declared": 1}))

    assert "unknown argument 'definitely_not_declared'" in out
