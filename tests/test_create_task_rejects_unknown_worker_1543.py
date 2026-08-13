"""#1543 — routing a task to a worker that does not exist must FAIL, not return success.

WHAT HAPPENED. `target_worker` was taken, persisted onto the row, and handed to an async
assign that failed to resolve it — and the tool returned "Task created: #NNNN" anyway.
Measured with probe #1567: an invented worker name produced a success return and a row
reading `status=unassigned, assigned_worker=NULL, target_worker='<the invented name>'`.

Five tasks in one session landed ownerless, three of them launch-critical, sitting for
about an hour beside the exact workers named in the routing. Nothing nudged them either:
IdleWatcher's trigger needs an ASSIGNED task, and these had no owner at all, so the one
mechanism meant to catch stalled work was blind to them.

SCOPE, because it is easy to over-read these tests: this closes the invalid-VALUE half.
The reported five passed a wrong KEY (`assigned_worker`, which this tool does not
declare), and that is a separate dispatcher-level fix. These tests would NOT have caught
the original five.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from swarm.mcp.handlers._create import _handle_create_task


def _daemon(*names: str) -> MagicMock:
    d = MagicMock()
    workers = []
    for n in names:
        w = MagicMock()
        w.name = n
        workers.append(w)
    d.workers = workers
    d.config.workers = workers
    return d


def _text(result) -> str:
    return result[0]["text"]


def test_an_unknown_target_is_refused():
    """THE DEFECT. Probe #1567's exact shape."""
    d = _daemon("platform")

    out = _text(_handle_create_task(d, "queen", {"title": "t", "target_worker": "not-real"}))

    assert "No worker named 'not-real'" in out


def test_the_refusal_creates_nothing():
    """REFUSED BEFORE CREATION, not after.

    Validating later would leave an orphan row behind every rejection — trading a
    silent mis-route for silent litter. Asserted separately from the message because
    a refusal that still created the task would read identically to the caller.
    """
    d = _daemon("platform")

    _handle_create_task(d, "queen", {"title": "t", "target_worker": "not-real"})

    d.create_task.assert_not_called()


def test_a_real_worker_still_creates_normally():
    """POSITIVE CONTROL. Without it, a fix that refused EVERYTHING would pass every
    other test in this file while breaking all routing."""
    d = _daemon("platform")

    _handle_create_task(d, "queen", {"title": "t", "target_worker": "platform"})

    d.create_task.assert_called_once()


def test_a_registered_but_not_running_worker_is_still_valid():
    """A stopped worker is a legitimate routing target — the task waits in its queue.

    Validating against the live roster alone would refuse real work whenever the
    target happened to be down, which is a worse failure than the one being fixed:
    it would block routing precisely when a worker most needs queued work.
    """
    d = MagicMock()
    d.workers = []  # nothing running
    cfg_worker = MagicMock()
    cfg_worker.name = "hub"
    d.config.workers = [cfg_worker]

    _handle_create_task(d, "queen", {"title": "t", "target_worker": "hub"})

    d.create_task.assert_called_once()


@pytest.mark.parametrize("target", ["", "   ", None])
def test_an_absent_target_is_the_ordinary_unrouted_case(target):
    """No target is not an error — it is how an unowned task is filed on purpose."""
    d = _daemon("platform")

    _handle_create_task(d, "queen", {"title": "t", "target_worker": target})

    d.create_task.assert_called_once()


def test_the_refusal_names_the_roster():
    """A caller that cannot see the valid options guesses again — the same loop that
    made the original silence expensive."""
    d = _daemon("platform", "hub")

    out = _text(_handle_create_task(d, "queen", {"title": "t", "target_worker": "hubb"}))

    assert "hub" in out and "platform" in out
    assert "omit target_worker" in out


# ---------------------------------------------------------------------------
# The THIRD defect on this path: a declared parameter that was never read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("given", ["high", "urgent", "low", "HIGH", " high "])
def test_priority_reaches_create_task(given):
    """`priority` is DECLARED in the schema, documented in the examples, and was
    never read — so create_task's TaskPriority.NORMAL default applied to every
    MCP-created task whatever the caller sent.

    Reproduced live post-reload by #1574: filed `high`, landed `normal`, gating a
    time-boxed page nobody would have re-prioritised because the call reported
    success. Case and whitespace variants included because the schema's examples
    are lowercase while the enum is not.
    """
    from swarm.tasks.task import TaskPriority

    d = _daemon("platform")

    _handle_create_task(d, "queen", {"title": "t", "priority": given})

    assert d.create_task.call_args.kwargs["priority"] == TaskPriority(given.strip().lower())


def test_priority_defaults_to_normal_when_absent():
    """POSITIVE CONTROL. Without it, a fix that hard-coded HIGH would pass the
    test above for the wrong reason."""
    from swarm.tasks.task import TaskPriority

    d = _daemon("platform")

    _handle_create_task(d, "queen", {"title": "t"})

    assert d.create_task.call_args.kwargs["priority"] == TaskPriority.NORMAL


def test_an_unrecognised_priority_falls_back_rather_than_refusing():
    """THE ASYMMETRY WITH target_worker, and it is deliberate.

    An unusable priority is cosmetic — the task still needs to exist, and refusing
    to file it would lose real work over a typo in a sort key. An unusable
    target_worker produces an OWNERLESS task nobody is watching, which is why that
    one refuses. Same handler, two arguments, two different right answers.
    """
    from swarm.tasks.task import TaskPriority

    d = _daemon("platform")

    _handle_create_task(d, "queen", {"title": "t", "priority": "extremely-urgent"})

    d.create_task.assert_called_once()
    assert d.create_task.call_args.kwargs["priority"] == TaskPriority.NORMAL
