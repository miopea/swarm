"""Direct tests for TaskBoard — covers lifecycle, deps, locking, scrub."""

from __future__ import annotations

import threading

import pytest

from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask, TaskPriority, TaskStatus, TaskType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_board() -> TaskBoard:
    """Create a board with no persistence store."""
    return TaskBoard()


def _quick_task(title: str = "test", **kwargs: object) -> SwarmTask:
    return SwarmTask(title=title, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------


class TestAddAndGet:
    def test_add_assigns_number(self):
        board = _make_board()
        task = board.add(_quick_task("first"))
        assert task.number == 1

    def test_sequential_numbers(self):
        board = _make_board()
        t1 = board.add(_quick_task("a"))
        t2 = board.add(_quick_task("b"))
        assert t1.number == 1
        assert t2.number == 2

    def test_get_returns_task(self):
        board = _make_board()
        t = board.add(_quick_task("x"))
        assert board.get(t.id) is t

    def test_get_missing_returns_none(self):
        board = _make_board()
        assert board.get("nonexistent") is None


class TestCreate:
    def test_create_returns_task(self):
        board = _make_board()
        t = board.create("do the thing")
        assert t.title == "do the thing"
        assert t.status == TaskStatus.UNASSIGNED

    def test_create_with_priority(self):
        board = _make_board()
        t = board.create("urgent", priority=TaskPriority.URGENT)
        assert t.priority == TaskPriority.URGENT

    def test_create_with_type(self):
        board = _make_board()
        t = board.create("fix bug", task_type=TaskType.BUG)
        assert t.task_type == TaskType.BUG


class TestRemove:
    def test_remove_existing(self):
        board = _make_board()
        t = board.add(_quick_task("rm me"))
        assert board.remove(t.id) is True
        assert board.get(t.id) is None

    def test_remove_missing(self):
        board = _make_board()
        assert board.remove("nope") is False

    def test_remove_tasks_bulk(self):
        board = _make_board()
        t1 = board.add(_quick_task("a"))
        t2 = board.add(_quick_task("b"))
        board.add(_quick_task("c"))
        removed = board.remove_tasks({t1.id, t2.id})
        assert removed == 2
        assert len(board.all_tasks) == 1


# ---------------------------------------------------------------------------
# Lifecycle transitions
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_assign_pending_task(self):
        board = _make_board()
        t = board.create("work")
        assert board.assign(t.id, "alice") is True
        assert t.status == TaskStatus.ASSIGNED
        assert t.assigned_worker == "alice"

    def test_assign_already_assigned_fails(self):
        board = _make_board()
        t = board.create("work")
        board.assign(t.id, "alice")
        assert board.assign(t.id, "bob") is False

    def test_complete_assigned_task(self):
        board = _make_board()
        t = board.create("work")
        board.assign(t.id, "alice")
        assert board.complete(t.id, resolution="done") is True
        assert t.status == TaskStatus.DONE
        assert t.resolution == "done"

    def test_complete_pending_fails(self):
        board = _make_board()
        t = board.create("work")
        assert board.complete(t.id) is False

    def test_force_complete_from_blocked(self):
        """force_complete closes a wedged BLOCKED task that normal complete
        refuses — the only programmatic way out of the #574 deadlock."""
        board = _make_board()
        t = board.create("wedged")
        board.assign(t.id, "alice")
        board.activate(t.id)
        board.block_for_operator(t.id, "operator hold")
        assert t.status == TaskStatus.BLOCKED
        # Normal complete refuses a BLOCKED task...
        assert board.complete(t.id, resolution="x") is False
        assert t.status == TaskStatus.BLOCKED
        # ...force_complete closes it.
        assert board.force_complete(t.id, resolution="done end-to-end") is True
        assert t.status == TaskStatus.DONE
        assert t.resolution == "done end-to-end"

    def test_force_complete_already_terminal_is_noop(self):
        board = _make_board()
        t = board.create("work")
        board.assign(t.id, "alice")
        board.complete(t.id)
        assert t.status == TaskStatus.DONE
        # Already terminal — force_complete won't re-stamp it.
        assert board.force_complete(t.id, resolution="again") is False

    def test_force_complete_missing_task(self):
        board = _make_board()
        assert board.force_complete("nonexistent") is False

    def test_fail_assigned_task(self):
        board = _make_board()
        t = board.create("work")
        board.assign(t.id, "alice")
        assert board.fail(t.id) is True
        assert t.status == TaskStatus.FAILED

    def test_reopen_completed(self):
        board = _make_board()
        t = board.create("work")
        board.assign(t.id, "alice")
        board.complete(t.id)
        assert board.reopen(t.id) is True
        # v9 cleanup: reopen lands in Backlog so the operator can review
        # the resolution before re-routing.
        assert t.status == TaskStatus.BACKLOG
        assert t.assigned_worker is None

    def test_reopen_pending_fails(self):
        board = _make_board()
        t = board.create("work")
        assert board.reopen(t.id) is False

    def test_unassign_returns_to_pending(self):
        board = _make_board()
        t = board.create("work")
        board.assign(t.id, "alice")
        assert board.unassign(t.id) is True
        assert t.status == TaskStatus.UNASSIGNED
        assert t.assigned_worker is None

    def test_unassign_pending_fails(self):
        board = _make_board()
        t = board.create("work")
        assert board.unassign(t.id) is False

    def test_unassign_worker_releases_all(self):
        board = _make_board()
        t1 = board.create("a")
        t2 = board.create("b")
        board.assign(t1.id, "alice")
        board.assign(t2.id, "alice")
        board.unassign_worker("alice")
        assert t1.status == TaskStatus.UNASSIGNED
        assert t2.status == TaskStatus.UNASSIGNED


class TestActiveExclusivity:
    """Only one ACTIVE task per worker — older ACTIVE rows must be demoted."""

    def test_demote_other_active_keeps_target(self):
        board = _make_board()
        t1 = board.create("a")
        t2 = board.create("b")
        board.assign(t1.id, "alice")
        board.assign(t2.id, "alice")
        t1.start()
        t2.start()
        assert t1.status == TaskStatus.ACTIVE
        assert t2.status == TaskStatus.ACTIVE

        demoted = board.demote_other_active("alice", keep_task_id=t2.id)

        assert demoted == [t1.id]
        assert t1.status == TaskStatus.ASSIGNED
        assert t2.status == TaskStatus.ACTIVE

    def test_demote_other_active_ignores_other_workers(self):
        board = _make_board()
        t1 = board.create("a")
        t2 = board.create("b")
        board.assign(t1.id, "alice")
        board.assign(t2.id, "bob")
        t1.start()
        t2.start()

        board.demote_other_active("alice", keep_task_id="some-other-id")

        # alice's active gets demoted; bob's stays untouched
        assert t1.status == TaskStatus.ASSIGNED
        assert t2.status == TaskStatus.ACTIVE

    def test_demote_other_active_no_competing_tasks(self):
        board = _make_board()
        t = board.create("a")
        board.assign(t.id, "alice")
        t.start()

        demoted = board.demote_other_active("alice", keep_task_id=t.id)

        assert demoted == []
        assert t.status == TaskStatus.ACTIVE

    def test_reconcile_keeps_earliest_started(self):
        """#611 P2: reconcile keeps the EARLIEST-STARTED (in-flight) ACTIVE task
        and demotes the rest — not the newest-by-updated_at, which could demote
        a long-running job. started_at wins even when updated_at disagrees."""
        board = _make_board()
        t1 = board.create("a")
        t2 = board.create("b")
        t3 = board.create("c")
        for t in (t1, t2, t3):
            board.assign(t.id, "alice")
            t.start()

        # t1 started first; t3 was updated most recently. started_at decides.
        t1.started_at, t1.updated_at = 1000.0, 1000.0
        t2.started_at, t2.updated_at = 2000.0, 2000.0
        t3.started_at, t3.updated_at = 3000.0, 3000.0

        result = board.reconcile_active_per_worker()

        assert "alice" in result
        assert set(result["alice"]) == {t2.id, t3.id}
        assert t1.status == TaskStatus.ACTIVE  # earliest-started kept
        assert t2.status == TaskStatus.ASSIGNED
        assert t3.status == TaskStatus.ASSIGNED

    def test_reconcile_no_op_when_one_active_per_worker(self):
        board = _make_board()
        t1 = board.create("a")
        t2 = board.create("b")
        board.assign(t1.id, "alice")
        board.assign(t2.id, "bob")
        t1.start()
        t2.start()

        result = board.reconcile_active_per_worker()

        assert result == {}
        assert t1.status == TaskStatus.ACTIVE
        assert t2.status == TaskStatus.ACTIVE

    def test_reconcile_skips_assigned_tasks(self):
        board = _make_board()
        t1 = board.create("a")
        t2 = board.create("b")
        board.assign(t1.id, "alice")
        board.assign(t2.id, "alice")
        t1.start()
        # t2 stays ASSIGNED

        result = board.reconcile_active_per_worker()

        assert result == {}
        assert t1.status == TaskStatus.ACTIVE
        assert t2.status == TaskStatus.ASSIGNED


# ---------------------------------------------------------------------------
# Dependency management
# ---------------------------------------------------------------------------


class TestDependencies:
    def test_simple_dependency(self):
        board = _make_board()
        t1 = board.create("first")
        t2 = board.create("second", depends_on=[t1.id])
        assert t2.depends_on == [t1.id]

    def test_available_tasks_respects_deps(self):
        board = _make_board()
        t1 = board.create("first")
        board.create("second", depends_on=[t1.id])
        available = board.available_tasks
        # Only t1 should be available (t2 blocked by t1)
        assert len(available) == 1
        assert available[0].id == t1.id

    def test_deps_unblock_after_completion(self):
        board = _make_board()
        t1 = board.create("first")
        t2 = board.create("second", depends_on=[t1.id])
        board.assign(t1.id, "alice")
        board.complete(t1.id)
        available = board.available_tasks
        assert t2.id in [t.id for t in available]

    def test_circular_dependency_direct(self):
        """A depends on B, B depends on A."""
        board = _make_board()
        t1 = board.create("a")
        with pytest.raises(ValueError, match="circular"):
            board.create("b", depends_on=[t1.id])
            # Now try to make t1 depend on t2
            t2 = board.all_tasks[-1]
            board.update(t1.id, depends_on=[t2.id])

    def test_circular_dependency_via_update(self):
        """A -> B -> C, then try to make C -> A."""
        board = _make_board()
        t1 = board.create("a")
        t2 = board.create("b", depends_on=[t1.id])
        t3 = board.create("c", depends_on=[t2.id])
        with pytest.raises(ValueError, match="circular"):
            board.update(t1.id, depends_on=[t3.id])

    def test_self_referential_dependency(self):
        """A task cannot depend on itself."""
        board = _make_board()
        t = board.create("self")
        with pytest.raises(ValueError, match="circular"):
            board.update(t.id, depends_on=[t.id])

    def test_scrub_dependency_on_remove(self):
        """When a task is removed, it should be scrubbed from depends_on."""
        board = _make_board()
        t1 = board.create("dep")
        t2 = board.create("dependent", depends_on=[t1.id])
        board.remove(t1.id)
        assert t2.depends_on == []

    def test_scrub_dependency_on_bulk_remove(self):
        board = _make_board()
        t1 = board.create("dep")
        t2 = board.create("dependent", depends_on=[t1.id])
        board.remove_tasks({t1.id})
        assert t2.depends_on == []


# ---------------------------------------------------------------------------
# Query methods
# ---------------------------------------------------------------------------


class TestQueries:
    def test_all_tasks_sorted_by_priority(self):
        board = _make_board()
        low = board.create("low", priority=TaskPriority.LOW)
        urgent = board.create("urgent", priority=TaskPriority.URGENT)
        normal = board.create("normal", priority=TaskPriority.NORMAL)
        tasks = board.all_tasks
        assert tasks[0].id == urgent.id
        assert tasks[1].id == normal.id
        assert tasks[2].id == low.id

    def test_active_tasks(self):
        board = _make_board()
        t1 = board.create("a")
        t2 = board.create("b")
        board.assign(t1.id, "alice")
        active = board.active_tasks
        assert len(active) == 1
        assert active[0].id == t1.id
        assert t2.id not in [t.id for t in active]

    def test_tasks_for_worker(self):
        board = _make_board()
        t1 = board.create("a")
        board.create("b")
        board.assign(t1.id, "alice")
        assert len(board.tasks_for_worker("alice")) == 1
        assert len(board.tasks_for_worker("bob")) == 0

    def test_active_tasks_for_worker_excludes_completed(self):
        board = _make_board()
        t1 = board.create("a")
        t2 = board.create("b")
        board.assign(t1.id, "alice")
        board.assign(t2.id, "alice")
        board.complete(t1.id)
        active = board.active_tasks_for_worker("alice")
        assert len(active) == 1
        assert active[0].id == t2.id

    def test_summary(self):
        board = _make_board()
        board.create("a")
        t2 = board.create("b")
        board.assign(t2.id, "alice")
        summary = board.summary()
        assert "2 tasks" in summary
        assert "1 unassigned" in summary
        assert "1 in progress" in summary


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


class TestUpdate:
    def test_update_title(self):
        board = _make_board()
        t = board.create("old")
        board.update(t.id, title="new")
        assert t.title == "new"

    def test_update_missing_returns_false(self):
        board = _make_board()
        assert board.update("nope", title="x") is False

    def test_update_preserves_unset_fields(self):
        board = _make_board()
        t = board.create("work", priority=TaskPriority.HIGH)
        board.update(t.id, title="updated")
        assert t.priority == TaskPriority.HIGH


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------


class TestEvents:
    def test_add_emits_change(self):
        board = _make_board()
        events: list[str] = []
        board.on_change(lambda: events.append("change"))
        board.add(_quick_task("x"))
        assert "change" in events

    def test_remove_emits_change(self):
        board = _make_board()
        t = board.add(_quick_task("x"))
        events: list[str] = []
        board.on_change(lambda: events.append("change"))
        board.remove(t.id)
        assert "change" in events

    def test_assign_emits_change(self):
        board = _make_board()
        t = board.create("x")
        events: list[str] = []
        board.on_change(lambda: events.append("change"))
        board.assign(t.id, "alice")
        assert "change" in events


# ---------------------------------------------------------------------------
# RLock reentrancy — callback re-enters board during emit
# ---------------------------------------------------------------------------


class TestRLockReentrancy:
    def test_callback_can_read_board_during_emit(self):
        """Event callback that reads available_tasks should not deadlock."""
        board = _make_board()
        results: list[int] = []

        def on_change():
            # This re-enters the board's RLock via available_tasks
            results.append(len(board.available_tasks))

        board.on_change(on_change)
        board.create("test")
        assert len(results) == 1
        assert results[0] >= 0  # didn't deadlock


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_add_no_duplicate_numbers(self):
        """Adding tasks from multiple threads should not produce duplicate numbers."""
        board = _make_board()
        barrier = threading.Barrier(10)

        def add_task():
            barrier.wait()
            board.create(f"task-{threading.current_thread().name}")

        threads = [threading.Thread(target=add_task) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        numbers = [t.number for t in board.all_tasks]
        assert len(numbers) == 10
        assert len(set(numbers)) == 10  # all unique


# ---------------------------------------------------------------------------
# Query (filter, sort, paginate)
# ---------------------------------------------------------------------------


class TestQuery:
    def test_filter_by_status(self):
        board = _make_board()
        board.add(_quick_task("a"))
        t2 = board.add(_quick_task("b"))
        board.assign(t2.id, "w1")
        tasks, total = board.query(status="assigned")
        assert total == 1
        assert tasks[0].title == "b"

    def test_filter_by_priority(self):
        board = _make_board()
        board.add(_quick_task("lo", priority=TaskPriority.LOW))
        board.add(_quick_task("hi", priority=TaskPriority.HIGH))
        tasks, total = board.query(priority="high")
        assert total == 1
        assert tasks[0].title == "hi"

    def test_filter_by_worker(self):
        board = _make_board()
        t = board.add(_quick_task("x"))
        board.assign(t.id, "api")
        tasks, total = board.query(worker="api")
        assert total == 1

    def test_search(self):
        board = _make_board()
        board.add(_quick_task("Fix login bug"))
        board.add(_quick_task("Add feature"))
        tasks, total = board.query(search="login")
        assert total == 1
        assert tasks[0].title == "Fix login bug"

    def test_pagination(self):
        board = _make_board()
        for i in range(10):
            board.add(_quick_task(f"task-{i}"))
        tasks, total = board.query(limit=3, offset=0)
        assert len(tasks) == 3
        assert total == 10
        tasks2, _ = board.query(limit=3, offset=3)
        assert len(tasks2) == 3
        assert tasks[0].id != tasks2[0].id

    def test_sort_by_created_at(self):
        board = _make_board()
        board.add(_quick_task("old"))
        board.add(_quick_task("new"))
        tasks, _ = board.query(sort="created_at", desc=True)
        assert tasks[0].title == "new"

    def test_no_filters_returns_all(self):
        board = _make_board()
        board.add(_quick_task("a"))
        board.add(_quick_task("b"))
        tasks, total = board.query()
        assert total == 2


# ---------------------------------------------------------------------------
# Operator-blocked park (#auto-park): ACTIVE → BLOCKED, stable, unparkable
# ---------------------------------------------------------------------------


class TestBlockForOperator:
    def _active(self, board: TaskBoard) -> SwarmTask:
        t = board.add(_quick_task("stalled prog"))
        board.assign(t.id, "project-root")
        board.activate(t.id)
        assert t.status == TaskStatus.ACTIVE
        return t

    def test_block_for_operator_active_to_blocked(self) -> None:
        board = _make_board()
        t = self._active(board)
        assert board.block_for_operator(t.id, "operator-blocked: awaiting hand-back")
        assert t.status == TaskStatus.BLOCKED
        assert "awaiting hand-back" in t.block_reason
        # No longer in the worker's active set → churn loops skip it.
        assert t not in board.active_tasks_for_worker("project-root")

    def test_block_for_operator_rejects_non_active(self) -> None:
        board = _make_board()
        t = board.add(_quick_task("x"))
        board.assign(t.id, "project-root")  # ASSIGNED, not ACTIVE
        assert board.block_for_operator(t.id, "r") is False
        assert t.status == TaskStatus.ASSIGNED
        assert board.block_for_operator("nope", "r") is False

    def test_activate_unparks_and_clears_block_reason(self) -> None:
        board = _make_board()
        t = self._active(board)
        board.block_for_operator(t.id, "operator-blocked: waiting")
        assert board.activate(t.id)  # operator re-dispatch / hand-back
        assert t.status == TaskStatus.ACTIVE
        assert t.block_reason == ""

    def test_reconcile_does_not_revert_unbound_blocked(self) -> None:
        board = _make_board()
        t = self._active(board)
        board.block_for_operator(t.id, "operator-blocked")
        # Worker not "working" and NO blocker binding — reconciler only
        # mutates ACTIVE tasks, so a BLOCKED task is left untouched.
        repairs = board.reconcile_invariants(working_workers=set(), blocked_task_ids=set())
        assert t.status == TaskStatus.BLOCKED
        assert all(r["task_id"] != t.id for r in repairs)
