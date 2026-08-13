"""#1544 — the remaining invisible log truncations now declare themselves.

A truncated audit field that LOOKS complete is worse than an absent one. An absent field
makes you find another instrument; a silently-cut one lets you run a check that cannot
fire and then believe its answer. #1524's cut produced a false measurement reported to
the operator twice, and the Queen independently misread the same wall as a query bug.

DRIVEN THROUGH THE REAL HANDLERS, not by calling `truncate_for_log` directly. The helper
is already covered by #1524; what was unproven is that these CALL SITES use it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from swarm.drones.log import truncate_for_log


def _details(d: MagicMock) -> list[str]:
    """Every detail string passed to drone_log.add on this daemon."""
    out = []
    for call in d.drone_log.add.call_args_list:
        if len(call.args) >= 3 and isinstance(call.args[2], str):
            out.append(call.args[2])
    return out


def test_a_converted_site_marks_the_cut_and_names_the_true_total():
    """THE POINT. A long park reason is cut, and the cut says so — with the real length,
    which is what tells a reader how much is missing rather than only that something is."""
    from swarm.mcp.handlers._park import _handle_park_task

    d = MagicMock()
    task = MagicMock()
    task.number = 42
    task.id = "abc"
    d.task_board.current_task_for_worker.return_value = task
    d.task_board.get.return_value = task

    long_reason = "x" * 500
    _handle_park_task(d, "swarm", {"reason": long_reason})

    marked = [t for t in _details(d) if "truncated" in t]
    assert marked, f"park logged no truncation marker: {_details(d)}"
    assert "500 chars total" in marked[0]


def test_a_short_value_is_not_marked():
    """THE CONTROL. A helper that always appended would pass the test above while lying
    about every short value — and a false "truncated" claim sends the next reader hunting
    for data that was never cut."""
    from swarm.mcp.handlers._park import _handle_park_task

    d = MagicMock()
    task = MagicMock()
    task.number = 42
    task.id = "abc"
    d.task_board.current_task_for_worker.return_value = task
    d.task_board.get.return_value = task

    _handle_park_task(d, "swarm", {"reason": "short reason"})

    assert not [t for t in _details(d) if "truncated" in t]
    assert any("short reason" in t for t in _details(d))


def test_the_id_prefix_is_deliberately_left_alone():
    """task_manager.py's `task_id[:8]` is an ID PREFIX, not a truncated message.

    Shortening a hex id loses no meaning and a marker would be pure noise. Pinned so a
    later mechanical sweep does not "fix" it — the ticket names this exclusion
    explicitly, and a sweep driven by grep alone would convert it.
    """
    from pathlib import Path

    src = Path("src/swarm/server/task_manager.py").read_text(encoding="utf-8")

    assert "task_id[:8]" in src
    assert "truncate_for_log(task_id" not in src


def test_message_previews_are_deliberately_left_alone():
    """The message-body previews point AT a row that holds the truth — the full body is
    in the `messages` table — so the log line is a pointer, not the evidence. Marking the
    highest-volume log rows in the system for a value nobody greps is noise.

    Pinned as a DECISION rather than an oversight, so the remaining count is known.
    """
    from pathlib import Path

    src = Path("src/swarm/mcp/handlers/_messages.py").read_text(encoding="utf-8")

    assert "content[:80]" in src, "previews were converted; that was a deliberate exclusion"


def test_the_helper_still_behaves_as_1524_specified():
    """Guards the contract these 17 call sites now depend on."""
    assert truncate_for_log("short", 100) == "short"
    out = truncate_for_log("y" * 300, 100)
    assert out.startswith("y" * 100)
    assert "300 chars total" in out
