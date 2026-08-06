"""The worker title bar names only work IN PROGRESS (operator-reported).

2026-08-06: "Tasks shouldn't appear in the worker's title bar. If it's not set to
in progress, assigned just means it's a pending task for that worker."

Both display sites — ``handle_partial_workers`` (every htmx swap) and
``handle_dashboard`` (the initial render) — built the worker→task map from
``task_board.active_tasks``, whose docstring is "Tasks currently assigned or in
progress". So a task merely QUEUED to a worker was rendered in that worker's
title bar as though the worker were working it: a claim about what a worker is
doing right now, derived from a fact about what it has been given.

``active_tasks`` is deliberately NOT narrowed. The IdleWatcher
(``_bucket_active_tasks_by_worker``) and the directive drone both need ASSIGNED as
well as ACTIVE — a worker with queued work is not idle-with-nothing-to-do — so
changing the predicate would break nudge logic to fix a label. The conflation was
in the display.

Measured at the time of the report: the live board held 4 ASSIGNED and 0 ACTIVE
tasks, so every title being shown was a pending task and none was in progress.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from swarm.tasks.board import TaskBoard
from swarm.tasks.task import TaskStatus
from swarm.web.app import _worker_task_titles


@pytest.fixture
def d():
    daemon = MagicMock()
    daemon.task_board = TaskBoard()
    return daemon


def test_an_assigned_but_not_started_task_is_not_shown(d):
    """The reported defect. ASSIGNED means queued, not being worked."""
    t = d.task_board.create(title="queued work")
    d.task_board.assign(t.id, "swarm")
    assert d.task_board.get(t.id).status == TaskStatus.ASSIGNED
    assert _worker_task_titles(d) == {}, "a pending task was shown as in progress"


def test_an_active_task_is_shown(d):
    """The complement — asserting only the absence above would pass on a helper
    that returned {} unconditionally."""
    t = d.task_board.create(title="real work")
    d.task_board.assign(t.id, "swarm")
    d.task_board.activate(t.id)
    assert _worker_task_titles(d) == {"swarm": "real work"}


def test_active_tasks_still_includes_assigned_for_the_idle_watcher(d):
    """The predicate must NOT be narrowed. If this fails, the nudge logic was
    changed to fix a label and a worker with queued work now looks idle."""
    t = d.task_board.create(title="queued")
    d.task_board.assign(t.id, "swarm")
    assert t.id in {x.id for x in d.task_board.active_tasks}, (
        "active_tasks stopped including ASSIGNED — IdleWatcher and the directive "
        "drone both depend on that; narrow the display instead"
    )


def test_blocked_and_backlog_work_is_not_shown_either(d):
    """A worker is not working a task it is waiting on or that is parked."""
    b = d.task_board.create(title="waiting")
    d.task_board.assign(b.id, "swarm")
    d.task_board.activate(b.id)
    d.task_board.block_on_external(b.id, "swarm", "upstream", "x#1")
    assert _worker_task_titles(d) == {}, "blocked work shown as in progress"


def test_both_render_paths_use_the_same_computation():
    """The two sites had four identical lines each. Fixing one and missing the
    other would leave the initial page render and every subsequent htmx swap
    disagreeing about what a worker is doing — a discrepancy that reads as a
    flicker rather than as a bug, so nobody files it."""
    import inspect

    from swarm.web.routes import pages, partials

    for mod in (pages, partials):
        src = inspect.getsource(mod)
        assert "_worker_task_titles(d)" in src, f"{mod.__name__} does not use the shared helper"
        assert "task_board.active_tasks" not in src, (
            f"{mod.__name__} still derives worker titles from active_tasks, which includes ASSIGNED"
        )
