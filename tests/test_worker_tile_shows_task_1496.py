"""#1496 — worker tiles must show WHICH task, and whether it is started.

Before #1486, dispatch failed silently and almost nothing reached ACTIVE, so an
ACTIVE-only tile was blank for every worker: the operator could not tell "nothing
assigned" from "assigned and stuck". The tile now shows both, differentiated.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest
from jinja2 import Environment, FileSystemLoader

from swarm.tasks.task import TaskStatus
from swarm.web.app import _worker_task_cards, _worker_task_titles

TPL = "src/swarm/web/templates"


def _task(num: int, title: str, status: TaskStatus, worker: str | None):
    return types.SimpleNamespace(number=num, title=title, status=status, assigned_worker=worker)


def _daemon(*tasks) -> MagicMock:
    d = MagicMock()
    d.task_board.all_tasks = list(tasks)
    return d


# --------------------------------------------------------------------- data


def test_card_carries_number_title_and_status() -> None:
    d = _daemon(_task(1496, "Dashboard worker tiles show no task", TaskStatus.ACTIVE, "swarm"))
    assert _worker_task_cards(d) == {
        "swarm": {
            "number": 1496,
            "title": "Dashboard worker tiles show no task",
            "status": "active",
        }
    }


def test_assigned_is_reported_as_assigned_not_hidden_and_not_promoted() -> None:
    """The whole point: visible, but NOT dressed up as in-progress.

    #1159 removed daemon-side activation inference after the promoter activated
    the wrong task. A worker that skipped swarm_start_task reads ASSIGNED here
    even while working — the honest answer, not a guess.
    """
    d = _daemon(_task(1498, "claim hook", TaskStatus.ASSIGNED, "swarm"))
    card = _worker_task_cards(d)["swarm"]
    assert card["status"] == "assigned", "an assigned task must not be reported as active"


def test_active_wins_when_a_worker_somehow_holds_both() -> None:
    """ACTIVE is the asserted one, so it beats the merely-queued sibling."""
    d = _daemon(
        _task(1, "queued", TaskStatus.ASSIGNED, "swarm"),
        _task(2, "started", TaskStatus.ACTIVE, "swarm"),
    )
    assert _worker_task_cards(d)["swarm"]["number"] == 2

    d2 = _daemon(
        _task(2, "started", TaskStatus.ACTIVE, "swarm"),
        _task(1, "queued", TaskStatus.ASSIGNED, "swarm"),
    )
    assert _worker_task_cards(d2)["swarm"]["number"] == 2, "order of all_tasks must not decide it"


@pytest.mark.parametrize("status", [TaskStatus.DONE, TaskStatus.BLOCKED, TaskStatus.BACKLOG])
def test_finished_or_parked_work_is_not_shown_as_current(status: TaskStatus) -> None:
    d = _daemon(_task(9, "not current", status, "swarm"))
    assert _worker_task_cards(d) == {}


def test_worker_with_no_task_is_absent_from_the_map() -> None:
    """The template renders the idle state; the data layer says nothing."""
    assert _worker_task_cards(_daemon()) == {}


def test_active_only_helper_is_unchanged() -> None:
    """_worker_task_titles keeps its ACTIVE-only guarantee (2026-08-06 ruling).

    The new card function is additive. If this ever starts returning assigned
    work, the old ruling has been silently reversed.
    """
    d = _daemon(_task(1, "queued", TaskStatus.ASSIGNED, "swarm"))
    assert _worker_task_titles(d) == {}


# ------------------------------------------------------------------ render


def _render(cards: dict) -> str:
    env = Environment(loader=FileSystemLoader(TPL), autoescape=True)
    tpl = env.get_template("partials/worker_list.html")
    w = types.SimpleNamespace(
        name="swarm",
        state="BUZZING",
        path="/tmp",
        provider="claude",
        in_config=True,
        revive_count=0,
        worktree_branch="",
        needs_operator_input=False,
        context_pct=0.0,
        exit_code=None,
        crash_tail="",
        state_duration="2m",
    )
    return tpl.render(
        workers=[w], selected_worker="swarm", worker_tasks={}, worker_task_cards=cards, queen=None
    )


def test_tile_renders_number_and_title() -> None:
    html = _render(
        {"swarm": {"number": 1496, "title": "Worker tiles show no task", "status": "active"}}
    )
    assert "#1496" in html
    assert "Worker tiles show no task" in html


def test_tile_distinguishes_active_from_assigned() -> None:
    active = _render({"swarm": {"number": 1, "title": "t", "status": "active"}})
    assigned = _render({"swarm": {"number": 1, "title": "t", "status": "assigned"}})

    assert "task-chip-active" in active and "working" in active
    assert "task-chip-assigned" in assigned and "queued" in assigned
    assert active != assigned, "the two states must not render identically"


def test_idle_worker_renders_cleanly_not_blank_and_not_an_error() -> None:
    html = _render({})
    assert "task-chip-idle" in html
    assert "no task assigned" in html
    assert "None" not in html, "a missing card must not leak a Python None into the tile"
    assert "Undefined" not in html


def test_status_is_not_conveyed_by_colour_alone() -> None:
    """A greyscale screenshot — which is how the operator reported this — must
    still distinguish the two states, so the chip carries a word."""
    active = _render({"swarm": {"number": 1, "title": "t", "status": "active"}})
    assigned = _render({"swarm": {"number": 1, "title": "t", "status": "assigned"}})
    assert "working" in active and "working" not in assigned
    assert "queued" in assigned
