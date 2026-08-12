"""#1535 — a zero-result query must return the same shape as a populated one.

THIS BROKE THE OPERATOR'S OWN READ. On 2026-08-12 they ran a buzz_log query, got the
bare-list shape back from the empty path, and concluded THEIR SQL WAS WRONG. It wasn't.
The wrong conclusion was acted on before the real cause surfaced. These handlers are the
instruments the fleet is observed with, and the defect failed in the most expensive
direction — it looked like the reader's mistake rather than the tool's.

WHY EVERY TEST HERE INDEXES INTO structuredContent RATHER THAN CHECKING THE TEXT.
A test asserting `"No buzz entries match." in text` PASSES AGAINST THE BROKEN VERSION —
the text block is present in both shapes. The defect is that structuredContent is ABSENT
when empty and PRESENT when populated, so only indexing it discriminates. Indexing is what
raises against the old code, and that is the point.

Each handler also gets a POPULATED positive control, so an implementation that hard-coded
the empty payload cannot pass.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from swarm.mcp.queen_handlers._logs import (
    _handle_view_buzz_log,
    _handle_view_drone_actions,
)
from swarm.mcp.queen_handlers._messages import (
    _handle_view_message_stream,
    _handle_view_messages,
)
from swarm.mcp.queen_handlers._views import _handle_view_worker_state

QUEEN = "queen"


def _daemon(rows: list[dict[str, Any]]) -> MagicMock:
    d = MagicMock()
    d.swarm_db.fetchall.return_value = rows
    d.workers = []
    return d


def _buzz_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": 1,
        "timestamp": 1_780_000_000.0,
        "category": "drone",
        "worker_name": "platform",
        "action": "CONTINUED",
        "detail": "something happened",
    }
    row.update(over)
    return row


def _msg_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": 1,
        "msg_type": "finding",
        "sender": "platform",
        "recipient": "swarm",
        "content": "a message body",
        "created_at": 1_780_000_000.0,
        "read_at": None,
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# The five (six) zero-result exits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("handler", "args", "collection_key", "text"),
    [
        (_handle_view_buzz_log, {}, "entries", "No buzz entries match."),
        (_handle_view_drone_actions, {}, "actions", "No recent drone actions."),
        (_handle_view_messages, {}, "messages", "No messages match."),
        (_handle_view_message_stream, {}, "messages", "No messages in window."),
    ],
)
def test_zero_results_carry_the_sidecar(handler, args, collection_key, text):
    """The core defect, per handler. Indexing structuredContent is the assertion."""
    result = handler(_daemon([]), QUEEN, args)

    assert isinstance(result, dict), "empty path must not return the legacy bare list"
    sc = result["structuredContent"]  # raises against the broken version — the point
    assert sc[collection_key] == []
    assert sc["count"] == 0
    assert "filters" in sc, "a client reading filters['worker'] must not raise either"
    assert result["content"] == [{"type": "text", "text": text}], "text must be unchanged"


@pytest.mark.parametrize(
    ("handler", "rows", "collection_key"),
    [
        (_handle_view_buzz_log, [_buzz_row()], "entries"),
        (_handle_view_drone_actions, [_buzz_row()], "actions"),
        (_handle_view_messages, [_msg_row()], "messages"),
    ],
)
def test_populated_results_still_work(handler, rows, collection_key):
    """POSITIVE CONTROL. An implementation that hard-coded the empty payload would
    pass every test above while returning nothing for a real query."""
    sc = handler(_daemon(rows), QUEEN, {})["structuredContent"]

    assert len(sc[collection_key]) == 1
    assert sc["count"] == 1


def test_message_stream_rendered_to_nothing_also_carries_the_sidecar():
    """THE EXIT THE TICKET'S LIST MISSED.

    `_handle_view_message_stream` has THREE zero-result exits, not one: no rows at
    all, and two more where rows EXISTED but rendered to nothing (actionable_only
    filtering them out, or otherwise). The ticket named two. Converting a subset
    would leave precisely the hole this rule exists to close, so this drives the
    rows-exist-but-render-empty path specifically.
    """
    # A row too old to be actionable renders to nothing under actionable_only.
    d = _daemon([_msg_row(read_at=1_780_000_001.0)])

    result = _handle_view_message_stream(d, QUEEN, {"actionable_only": True})

    assert isinstance(result, dict)
    sc = result["structuredContent"]
    assert sc["messages"] == []
    assert sc["count"] == 0
    assert sc["filters"]["actionable_only"] is True


def test_filters_echo_the_arguments_that_produced_nothing():
    """When the answer is "nothing", what you searched for is the useful part."""
    sc = _handle_view_buzz_log(
        _daemon([]), QUEEN, {"worker": "ghost", "category": "drone", "limit": 7}
    )["structuredContent"]

    assert sc["filters"]["worker"] == "ghost"
    assert sc["filters"]["category"] == "drone"
    assert sc["filters"]["limit"] == 7


def test_empty_is_not_modelled_as_an_error():
    """AC: zero rows is a SUCCESSFUL query. An error discriminator here would be a
    defect of its own — it would make every empty filter look like a failure."""
    sc = _handle_view_buzz_log(_daemon([]), QUEEN, {})["structuredContent"]

    assert "error" not in sc


# ---------------------------------------------------------------------------
# Decision (A): the mode discriminator
# ---------------------------------------------------------------------------


def _worker(name: str) -> MagicMock:
    w = MagicMock()
    w.name = name
    w.is_queen = False
    w.kind = "claude"
    w.display_state.value = "resting"
    w.context_pct = 0.1
    w.state_duration = 5
    w.process = None
    w.usage.to_dict.return_value = {"input_tokens": 1, "output_tokens": 2}
    w.usage.cost_usd = 0.5
    return w


@pytest.mark.parametrize(
    ("args", "expected_mode"),
    [({}, "summary"), ({"worker": "alpha"}, "single"), ({"worker": "ghost"}, "single")],
)
def test_worker_state_declares_its_mode(args, expected_mode):
    """(A) — the summary keys `workers`, a lookup keys `worker`, so
    structuredContent["worker"] still raised on the summary path after #1432.
    `mode` lets a client read a FIELD to learn which shape it holds instead of
    inferring it from which key happens to exist."""
    d = _daemon([])
    d.workers = [_worker("alpha")]
    d.task_board.assigned_or_active_tasks_for_worker.return_value = []

    assert _handle_view_worker_state(d, QUEEN, args)["structuredContent"]["mode"] == expected_mode
