"""A Backlog task may carry an owner, and still must never be dispatched.

OPERATOR-REPORTED 2026-08-07: "when I set something for backlog, I assigned it to
sculpt-studio, but when I saved it it lost the assignment. Backlog is meant to be a
backlog for tasks not to get picked up for now, but should be able to carry an
assignment."

THREE separate obstacles stood between him and that, and fixing any one alone would
have looked like a fix while still failing:

1. ``SwarmTask.demote_to_backlog`` DROPPED the owner — deliberately and documented,
   reasoning that showing an owner claims a worker holds work that is out of play.
2. ``TaskBoard.assign`` gated on ``is_available``, which requires UNASSIGNED, so a
   Backlog task could not be assigned at all — the call just returned False.
3. ``SwarmTask.assign`` forced ``status = ASSIGNED``, so even once permitted, assigning
   a Backlog task would have UN-PARKED it — the opposite of the request.

The operator overrode obstacle 1 knowingly (2026-08-07). The old rationale was about
DISPLAY, not safety, and the safety half survives untouched: no dispatch path accepts
BACKLOG. That is asserted here directly rather than assumed, because "it is safe by
construction" is exactly the claim that rots when a new dispatch path is added.

Deliberately NOT changed: ``reopen`` still drops the owner. Reopening finished work is
a different act from parking live work and was not part of the decision.
"""

from __future__ import annotations

from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask, TaskStatus


def _daemon(monkeypatch):
    """Real daemon with a real board — the route reads both."""
    from tests.conftest import make_daemon

    return make_daemon(monkeypatch)


def _assigned(board: TaskBoard, worker: str = "sculpt-studio") -> SwarmTask:
    task = board.add(SwarmTask(title="later work", description="x"))
    assert board.assign(task.id, worker) is True, "positive control: the seed must assign"
    return task


# --- the reported workflow ----------------------------------------------------


def test_demoting_to_backlog_keeps_the_owner():
    """The exact report: park it, and it stays sculpt-studio's."""
    board = TaskBoard()
    task = _assigned(board)
    assert board.demote_to_backlog(task.id) is True
    assert task.status is TaskStatus.BACKLOG
    assert task.assigned_worker == "sculpt-studio", (
        "parking the task dropped its owner — 'this is sculpt-studio's, later' is not "
        "expressible and the operator's assignment vanishes on save"
    )


def test_a_backlog_task_can_be_assigned_without_being_un_parked():
    """The other direction, and obstacle 3. Assigning must give it an owner WITHOUT
    promoting it out of Backlog, or routing a parked item silently un-parks it."""
    board = TaskBoard()
    task = board.add(SwarmTask(title="later work", description="x"))
    board.demote_to_backlog(task.id)
    assert task.status is TaskStatus.BACKLOG

    assert board.assign(task.id, "sculpt-studio") is True, (
        "a BACKLOG task still cannot be assigned; board.assign gates on is_available, "
        "which answers a different question (may the DRONE take this)"
    )
    assert task.assigned_worker == "sculpt-studio"
    assert task.status is TaskStatus.BACKLOG, (
        "assigning promoted the task out of Backlog, un-parking the very thing the operator parked"
    )


def test_assigning_a_normal_task_still_promotes_it():
    """Regression guard on the status-preserving branch: everything that is NOT
    Backlog must still move to ASSIGNED, or ordinary routing quietly stops working."""
    board = TaskBoard()
    task = board.add(SwarmTask(title="normal work", description="x"))
    assert task.status is TaskStatus.UNASSIGNED
    board.assign(task.id, "hub")
    assert task.status is TaskStatus.ASSIGNED
    assert task.assigned_worker == "hub"


# --- the safety half of the old rationale, asserted rather than assumed -------


def test_an_owned_backlog_task_is_not_available_to_the_auto_assign_drone():
    """``is_available`` is the drone's predicate. Owning the task must not change it."""
    board = TaskBoard()
    task = _assigned(board)
    board.demote_to_backlog(task.id)
    assert task.is_available is False, "an owned Backlog task became drone-dispatchable"
    assert task.id not in {t.id for t in board.available_tasks}, (
        "an owned Backlog task appeared in available_tasks, which is what the drone and "
        "the Queen both select from"
    )


def test_an_owned_backlog_task_is_not_picked_up_by_the_auto_start_chain():
    """``auto_start_next_assigned`` selects ``status == ASSIGNED and not is_on_hold``.
    Asserted against the real predicate rather than a copy of it, so a change to that
    gate shows up here."""
    board = TaskBoard()
    task = _assigned(board)
    board.demote_to_backlog(task.id)
    candidates = [
        t
        for t in board.assigned_or_active_tasks_for_worker("sculpt-studio")
        if t.status == TaskStatus.ASSIGNED and not t.is_on_hold
    ]
    assert task.id not in {t.id for t in candidates}, (
        "an owned Backlog task is a candidate for auto-start, so parking it would not "
        "actually stop it being worked"
    )


def test_the_owner_survives_a_round_trip_through_the_board():
    """The operator's failure was on SAVE, so the owner has to survive persistence,
    not merely the in-memory mutation."""
    board = TaskBoard()
    task = _assigned(board)
    board.demote_to_backlog(task.id)
    reread = next(t for t in board.all_tasks if t.id == task.id)
    assert reread.assigned_worker == "sculpt-studio"
    assert reread.status is TaskStatus.BACKLOG


def test_reopen_still_drops_the_owner():
    """The deliberate divergence, pinned so it is a decision rather than an oversight.
    If reopen is ever changed to match, this test should be updated knowingly."""
    board = TaskBoard()
    task = _assigned(board)
    board.complete(task.id, "done")
    board.reopen(task.id)
    assert task.status is TaskStatus.BACKLOG
    assert task.assigned_worker is None, (
        "reopen now keeps the owner too — if that was intended, update this test and "
        "demote_to_backlog's docstring, which records the divergence"
    )


# --- the Queen surface, where routing parked work must not un-park it ---------


def test_the_queen_can_route_parked_work_without_un_parking_it(monkeypatch):
    """``queen_reassign_task`` used to REFUSE a Backlog task, because it ends in
    ``board.assign`` and that required UNASSIGNED. Now that parked work is routable the
    refusal is gone — but the danger swaps sides: the handler must not silently promote
    the task out of Backlog while giving it an owner.

    Also pins that the reply reads the status BACK from the board (#1268's AC). It was
    a hardcoded "(ASSIGNED, not started)", which became a lie the moment assignment
    stopped implying that status.
    """
    from swarm.mcp.queen_handlers._tasks import _handle_reassign_task
    from swarm.queen.runtime import QUEEN_WORKER_NAME
    from tests.conftest import make_daemon

    d = make_daemon(monkeypatch)
    task = d.task_board.create(title="parked work")
    d.task_board.demote_to_backlog(task.id)
    assert d.task_board.get(task.id).status is TaskStatus.BACKLOG, "positive control"

    out = _handle_reassign_task(
        d,
        QUEEN_WORKER_NAME,
        {"number": task.number, "to_worker": "hub", "reason": "hub owns this later"},
    )
    text = out[0]["text"]
    after = d.task_board.get(task.id)

    assert after.assigned_worker == "hub", f"the Queen could not route parked work: {text}"
    assert after.status is TaskStatus.BACKLOG, (
        f"routing parked work UN-PARKED it (status={after.status.value}) — the task the "
        f"operator explicitly took out of play is now queued for a worker"
    )
    assert "backlog" in text.lower(), (
        f"the reply does not report the real resulting status, so it claims a "
        f"transition that did not happen: {text}"
    )


# --- the dashboard save path, which is where the operator actually hit it -----


def test_the_assign_action_route_leaves_a_backlog_task_parked(monkeypatch):
    """OPERATOR-REPORTED 2026-08-07, the second round: "when I assign a worker and click
    save it moves to assigned, even if it was already on backlog."

    The board layer was already correct — he confirmed setting it back to Backlog
    "holds fine" — so this is a different path. ``/action/task/assign`` NORMALISED the
    task to UNASSIGNED first, calling ``existing.approve()`` on a BACKLOG one, purely
    so the old ``is_available`` gate would accept the assign. That promotion un-parked
    the task before ``board.assign`` ever ran, which is why fixing ``SwarmTask.assign``
    alone did nothing for him.

    Driven through the REAL route rather than the board, because the board-level tests
    above all passed while this was broken. A fix verified one layer below the reported
    symptom is not verified.
    """
    import asyncio

    from aiohttp.test_utils import make_mocked_request

    from swarm.web.routes.tasks import handle_action_assign_task

    d = _daemon(monkeypatch)
    task = d.task_board.create(title="parked work")
    d.task_board.demote_to_backlog(task.id)
    assert d.task_board.get(task.id).status is TaskStatus.BACKLOG, "positive control"

    async def _go() -> None:
        request = make_mocked_request(
            "POST",
            "/action/task/assign",
            payload=None,  # body supplied via post() stub
        )
        request.app["daemon"] = d

        async def _post():
            return {"task_id": task.id, "worker": "api", "auto_start": "false"}

        request.post = _post  # type: ignore[method-assign]
        await handle_action_assign_task(request)

    asyncio.run(_go())

    after = d.task_board.get(task.id)
    assert after.assigned_worker == "api", "the route failed to assign the parked task"
    assert after.status is TaskStatus.BACKLOG, (
        f"assigning through the dashboard un-parked the task (status={after.status.value}) "
        f"— the operator has to set it back to Backlog and save a second time"
    )


def test_the_assign_action_route_never_auto_starts_parked_work(monkeypatch):
    """The hazard directly below the one he reported, and worse. ``auto_start`` defaults
    to true, so once a BACKLOG task stays parked through assignment, the start branch
    would hand an idle worker the very task the operator took out of play."""
    import asyncio

    from aiohttp.test_utils import make_mocked_request

    from swarm.web.routes.tasks import handle_action_assign_task

    d = _daemon(monkeypatch)
    task = d.task_board.create(title="parked work")
    d.task_board.demote_to_backlog(task.id)

    # The worker MUST be idle, or the start branch never runs and this test passes
    # no matter what the guard does — which is exactly how an earlier version of it
    # survived a control that deleted the guard entirely.
    from swarm.worker.worker import WorkerState

    worker = d.get_worker("api")
    assert worker is not None, "fixture worker missing"
    worker.state = WorkerState.RESTING

    started: list[str] = []

    async def _start(task_id, actor="user"):
        started.append(task_id)
        return True

    d.start_task = _start  # type: ignore[method-assign]

    async def _go() -> None:
        request = make_mocked_request("POST", "/action/task/assign")
        request.app["daemon"] = d

        async def _post():
            return {"task_id": task.id, "worker": "api", "auto_start": "true"}

        request.post = _post  # type: ignore[method-assign]
        await handle_action_assign_task(request)

    asyncio.run(_go())

    assert not started, (
        "parked work was auto-started on assignment — the operator explicitly took it "
        "out of play and it went straight to a worker"
    )
    assert d.task_board.get(task.id).status is TaskStatus.BACKLOG
