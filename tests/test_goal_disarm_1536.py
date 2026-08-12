"""#1536 — an armed native /goal must not outlive its task.

THE INCIDENT, 2026-08-12. #1510 was auto-dispatched at 15:12:59 and its goal armed
the same second. The operator had ruled it parked; it was parked minutes later, and
NOTHING disarmed the goal. Arming is also asymmetric — it happens on the dispatch
path only, never on a worker-asserted ``swarm_start_task`` — so the two tasks the
worker self-started afterwards armed nothing and never displaced the stale goal. It
kept grading the worker against #1510's criteria for the rest of the session: nine
firings, every one pushing the worker to override the operator's ruling.

WHAT THESE TESTS PIN, and why each would have caught it:
  * a goal that no longer matches the ACTIVE task is CLEARED (the whole defect)
  * ``/goal clear`` is the wire form — the provider's own un-arm, confirmed against
    installed Claude Code 2.1.228 (``argumentHint: "[<condition> | clear]"``)
  * POSITIVE CONTROL: a goal that DOES match is left alone, so the fix cannot
    degenerate into "clear everything", which would silently disable the feature
  * tracking is dropped even when the send fails, so a phantom entry can't block
    future re-arming
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.server.task_coordinator import TaskCoordinator


def _task(number: int) -> MagicMock:
    t = MagicMock()
    t.number = number
    return t


def _coord(current: Any, *, send_raises: bool = False) -> tuple[TaskCoordinator, MagicMock]:
    """A coordinator whose board reports *current* as the worker's active task."""
    daemon = MagicMock()
    daemon.task_board.current_task_for_worker.return_value = current
    daemon.send_to_worker = AsyncMock(side_effect=Exception("pty dead") if send_raises else None)
    proc = MagicMock()
    proc.is_user_active = False
    proc.send_enter = AsyncMock()
    daemon._require_worker.return_value.process = proc
    return TaskCoordinator(daemon), daemon


@pytest.mark.asyncio
async def test_stale_goal_is_cleared_when_task_no_longer_active():
    """THE DEFECT. Worker parked #1510; nothing cleared the goal."""
    coord, daemon = _coord(current=None)
    coord._armed_goals["swarm"] = 1510

    await coord.reconcile_goals()

    sent = [c.args[1] for c in daemon.send_to_worker.await_args_list]
    assert "/goal clear" in sent, f"stale goal was never cleared; sent={sent}"
    assert "swarm" not in coord._armed_goals


@pytest.mark.asyncio
async def test_stale_goal_is_cleared_when_worker_moved_to_another_task():
    """The exact #1510 shape: worker is active on something ELSE.

    Distinct from the no-active-task case because a worker that moved on looks
    busy and healthy — nothing about its state suggests a stale goal.
    """
    coord, daemon = _coord(current=_task(1536))
    coord._armed_goals["swarm"] = 1510

    await coord.reconcile_goals()

    assert "/goal clear" in [c.args[1] for c in daemon.send_to_worker.await_args_list]
    assert "swarm" not in coord._armed_goals


@pytest.mark.asyncio
async def test_matching_goal_is_left_armed():
    """AC4 POSITIVE CONTROL — the fix must not just clear everything.

    Without this, a reconciler that unconditionally disarmed would pass every
    other test in this file while silently disabling native goals entirely.
    """
    coord, daemon = _coord(current=_task(1510))
    coord._armed_goals["swarm"] = 1510

    await coord.reconcile_goals()

    daemon.send_to_worker.assert_not_awaited()
    assert coord._armed_goals["swarm"] == 1510


@pytest.mark.asyncio
async def test_no_armed_goals_is_a_cheap_noop():
    """Steady state must not touch the board or the worker at all."""
    coord, daemon = _coord(current=None)

    await coord.reconcile_goals()

    daemon.task_board.current_task_for_worker.assert_not_called()
    daemon.send_to_worker.assert_not_awaited()


@pytest.mark.asyncio
async def test_tracking_is_dropped_even_when_the_clear_fails():
    """Erring toward "not armed" is the safe direction.

    A phantom tracking entry left behind by a failed send would suppress future
    re-arming forever, converting a delivery failure into a permanent one.
    """
    coord, _ = _coord(current=None, send_raises=True)
    coord._armed_goals["swarm"] = 1510

    await coord.reconcile_goals()

    assert "swarm" not in coord._armed_goals


@pytest.mark.asyncio
async def test_one_worker_failing_does_not_strand_another():
    """Per-worker isolation — a dead PTY on one worker must not skip the rest."""
    coord, daemon = _coord(current=None, send_raises=True)
    coord._armed_goals["alpha"] = 100
    coord._armed_goals["beta"] = 200

    await coord.reconcile_goals()

    assert coord._armed_goals == {}
    assert daemon.send_to_worker.await_count == 2
