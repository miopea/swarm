"""#405 daemon wiring: invariant reconciliation + INV-2 on state change."""

from __future__ import annotations

import asyncio

import pytest

from swarm.config import DroneConfig
from swarm.drones.log import SystemAction
from swarm.tasks.task import TaskStatus
from swarm.worker.worker import Worker, WorkerState
from tests.conftest import make_daemon


@pytest.fixture
def daemon(monkeypatch):
    return make_daemon(monkeypatch)


def _worker(name, state):
    w = Worker(name=name, path=f"/tmp/{name}")
    w.state = state
    return w


def _active(daemon, title, worker):
    t = daemon.task_board.create(title=title)
    daemon.task_board.assign(t.id, worker)
    daemon.task_board.activate(t.id)
    return t


def test_working_workers_only_buzzing_or_waiting(daemon):
    daemon.workers = [
        _worker("a", WorkerState.BUZZING),
        _worker("b", WorkerState.WAITING),
        _worker("c", WorkerState.RESTING),
        _worker("d", WorkerState.SLEEPING),
    ]
    assert daemon._working_workers() == {"a", "b"}


def test_reconcile_demotes_active_on_absent_worker_and_buzzes(daemon):
    """#1538 RETARGETED THIS FROM RESTING TO STUNG.

    It used to drive a RESTING worker and assert the demotion, which encoded the
    defect: a pause is not abandonment, and demoting on it was undoing the
    worker's own assertion. What the test was really guarding — that a genuinely
    stale ACTIVE row IS repaired, buzz-logged, and the pass is idempotent — is
    unchanged and still asserted here, against an ABSENT worker.
    """
    daemon.workers = [_worker("w1", WorkerState.STUNG)]
    t = _active(daemon, "stuck", "w1")
    assert daemon.task_board.get(t.id).status == TaskStatus.ACTIVE

    daemon._run_invariant_reconciliation("test")

    assert daemon.task_board.get(t.id).status == TaskStatus.ASSIGNED
    actions = [e.action for e in daemon.drone_log.entries]
    assert SystemAction.TASK_RECONCILED in actions
    # Idempotent — second pass is a no-op (no new repairs / log spam).
    n = len(daemon.drone_log.entries)
    daemon._run_invariant_reconciliation("test")
    assert len(daemon.drone_log.entries) == n


def test_a_resting_worker_keeps_its_active_task(daemon):
    """#1538, the other half: RESTING is a pause, not abandonment.

    Paired with the test above so the two states cannot be conflated again —
    together they pin that the reconciler discriminates rather than blanket-demoting.
    """
    daemon.workers = [_worker("w1", WorkerState.RESTING)]
    t = _active(daemon, "paused mid-task", "w1")

    daemon._run_invariant_reconciliation("test")

    assert daemon.task_board.get(t.id).status == TaskStatus.ACTIVE


def test_working_worker_keeps_its_active_task(daemon):
    daemon.workers = [_worker("w1", WorkerState.BUZZING)]
    t = _active(daemon, "in flight", "w1")
    daemon._run_invariant_reconciliation("test")
    assert daemon.task_board.get(t.id).status == TaskStatus.ACTIVE


def test_on_state_changed_to_resting_reconciles_without_stealing_the_row(daemon):
    """#1538 INVERTED THIS ASSERTION, DELIBERATELY.

    The hook still fires on the transition — that is worth keeping and is what
    the test is named for. What changed is the consequence: dropping to RESTING
    is a worker pausing between turns, and it must NOT cost the worker its ACTIVE
    row. This reactive path was the one doing most of the damage; 404 of 418
    TASK_RECONCILED rows were `active→assigned`, attributed to `state→RESTING`.
    """
    w = _worker("w1", WorkerState.BUZZING)
    daemon.workers = [w]
    t = _active(daemon, "x", "w1")

    w.state = WorkerState.RESTING
    daemon._on_state_changed(w)

    assert daemon.task_board.get(t.id).status == TaskStatus.ACTIVE


def test_on_state_changed_to_stung_does_demote(daemon):
    """The same hook must still repair a genuinely dead worker's row — otherwise
    the change above would have disabled the reactive path rather than corrected it."""
    w = _worker("w1", WorkerState.BUZZING)
    daemon.workers = [w]
    t = _active(daemon, "x", "w1")

    w.state = WorkerState.STUNG
    daemon._on_state_changed(w)

    assert daemon.task_board.get(t.id).status == TaskStatus.ASSIGNED


def test_self_heals_multi_active_resting_worker(daemon):
    """#1538 CHANGED THE EXPECTED END STATE FROM ZERO ACTIVE TO EXACTLY ONE.

    The corrupt shape being healed is >1 ACTIVE (INV-1), and INV-1 still collapses
    it — that guarantee is untouched. What is no longer corrupt is the SURVIVOR: one
    ACTIVE row on a merely paused worker is the normal, correct state, and demoting
    it to zero was the defect. Absence, not pause, is what costs a worker its row —
    see the sibling test below.
    """
    daemon.workers = [_worker("w1", WorkerState.RESTING)]
    a = daemon.task_board.create(title="a")
    b = daemon.task_board.create(title="b")
    for t in (a, b):
        daemon.task_board.assign(t.id, "w1")
        daemon.task_board._tasks[t.id].status = TaskStatus.ACTIVE

    daemon._run_invariant_reconciliation("startup")

    statuses = [daemon.task_board.get(t.id).status for t in (a, b)]
    assert statuses.count(TaskStatus.ACTIVE) == 1, "INV-1 must still collapse the duplicate"
    assert statuses.count(TaskStatus.ASSIGNED) == 1


def test_self_heals_multi_active_absent_worker_to_zero(daemon):
    """#405's original guarantee, preserved: an ABSENT worker keeps no ACTIVE row.

    Without this, the change above would read as "we now tolerate stale rows"
    rather than "we distinguish paused from gone".
    """
    daemon.workers = [_worker("w1", WorkerState.STUNG)]
    a = daemon.task_board.create(title="a")
    b = daemon.task_board.create(title="b")
    for t in (a, b):
        daemon.task_board.assign(t.id, "w1")
        daemon.task_board._tasks[t.id].status = TaskStatus.ACTIVE

    daemon._run_invariant_reconciliation("startup")

    statuses = {daemon.task_board.get(a.id).status, daemon.task_board.get(b.id).status}
    assert statuses == {TaskStatus.ASSIGNED}  # zero ACTIVE — fully healed


def test_complete_task_force_closes_blocked(daemon):
    """#609: complete_task(force=True) closes a wedged BLOCKED task that the
    normal status-gated path refuses — the clean force-close capability that
    replaces the #574 fail→reopen→approve→assign→complete workaround."""
    from swarm.server.daemon import TaskOperationError

    t = daemon.task_board.create(title="wedged")
    daemon.task_board.assign(t.id, "w1")
    daemon.task_board.activate(t.id)
    daemon.task_board.block_for_operator(t.id, "operator hold")
    assert t.status == TaskStatus.BLOCKED

    # Normal completion refuses a BLOCKED task.
    with pytest.raises(TaskOperationError):
        daemon.complete_task(t.id, resolution="x")
    assert daemon.task_board.get(t.id).status == TaskStatus.BLOCKED

    # Force path closes it end-to-end.
    assert daemon.complete_task(t.id, resolution="done e2e", force=True) is True
    closed = daemon.task_board.get(t.id)
    assert closed.status == TaskStatus.DONE
    assert closed.resolution == "done e2e"


# --- P1 (#611): periodic invariant-reconcile loop ---


def test_drone_config_has_reconcile_interval():
    """#611 P1: DroneConfig exposes a periodic-reconcile interval (default 90s)."""
    assert DroneConfig().reconcile_interval_seconds == 90.0


@pytest.mark.asyncio
async def test_invariant_reconcile_loop_ticks(daemon, monkeypatch):
    """#611 P1: the periodic loop calls _run_invariant_reconciliation on each
    tick, independent of any worker state change (closes the unhealed-while-
    BUZZING window that left platform #604/#605 both ACTIVE)."""
    daemon.config.drones.reconcile_interval_seconds = 90.0
    calls: list[str] = []
    monkeypatch.setattr(
        daemon, "_run_invariant_reconciliation", lambda reason: calls.append(reason)
    )

    # First sleep returns; second raises CancelledError to break the loop after
    # exactly one reconcile tick.
    state = {"n": 0}

    async def fake_sleep(_seconds):
        state["n"] += 1
        if state["n"] >= 2:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr("swarm.server.daemon.asyncio.sleep", fake_sleep)
    await daemon._invariant_reconcile_loop()
    assert calls == ["periodic"]


@pytest.mark.asyncio
async def test_invariant_reconcile_loop_disabled_skips(daemon, monkeypatch):
    """interval <= 0 disables the reconcile (loop idles without reconciling)."""
    daemon.config.drones.reconcile_interval_seconds = 0.0
    calls: list[str] = []
    monkeypatch.setattr(
        daemon, "_run_invariant_reconciliation", lambda reason: calls.append(reason)
    )

    state = {"n": 0}

    async def fake_sleep(_seconds):
        state["n"] += 1
        if state["n"] >= 2:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr("swarm.server.daemon.asyncio.sleep", fake_sleep)
    await daemon._invariant_reconcile_loop()
    assert calls == []  # disabled — never reconciles


# --- P5 (#611): web routes go through guarded board methods ---


class _FakeReq:
    """Minimal request: handle_action_create_task only needs app['daemon'] +
    an awaitable post() form."""

    def __init__(self, daemon, data: dict[str, str]):
        self.app = {"daemon": daemon}
        self._data = data

    async def post(self):
        from multidict import MultiDict

        return MultiDict(self._data)


def test_apply_status_change_backlog_to_unassigned(daemon):
    """#611 P5: Backlog→Unassigned routes through board.approve_task (guarded)
    rather than a raw task.approve() + manual persist."""
    from swarm.web.routes.tasks import _apply_status_change

    t = daemon.task_board.create(title="b")
    t.status = TaskStatus.BACKLOG
    _apply_status_change(daemon, t.id, "backlog", "unassigned")
    assert daemon.task_board.get(t.id).status == TaskStatus.UNASSIGNED


@pytest.mark.asyncio
async def test_create_task_refuses_active_status(daemon):
    """#611 P5: the create route refuses to author a task straight into ACTIVE
    (must go through the activate() chokepoint / INV-1); it stays in its
    default lane instead."""
    import json

    from swarm.web.routes.tasks import handle_action_create_task

    resp = await handle_action_create_task(_FakeReq(daemon, {"title": "x", "status": "active"}))
    created = daemon.task_board.get(json.loads(resp.text)["id"])
    assert created.status != TaskStatus.ACTIVE


@pytest.mark.asyncio
async def test_create_task_allows_terminal_authoring(daemon):
    """Lane/terminal authoring (recording historical work) is still allowed."""
    import json

    from swarm.web.routes.tasks import handle_action_create_task

    resp = await handle_action_create_task(_FakeReq(daemon, {"title": "hist", "status": "done"}))
    assert daemon.task_board.get(json.loads(resp.text)["id"]).status == TaskStatus.DONE
