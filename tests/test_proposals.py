"""Tests for tasks/proposal.py — ProposalStore and AssignmentProposal."""

from __future__ import annotations

from swarm.tasks.proposal import (
    AssignmentProposal,
    ProposalStatus,
    ProposalStore,
    build_worker_task_info,
)


def test_add_and_get():
    store = ProposalStore()
    p = AssignmentProposal(worker_name="api", task_id="t1", task_title="Fix bug")
    store.add(p)
    assert store.get(p.id) is p


def test_pending_property():
    store = ProposalStore()
    p1 = AssignmentProposal(worker_name="api", task_id="t1", task_title="Fix bug")
    p2 = AssignmentProposal(worker_name="web", task_id="t2", task_title="Add feature")
    store.add(p1)
    store.add(p2)
    assert len(store.pending) == 2

    p1.status = ProposalStatus.APPROVED
    assert len(store.pending) == 1
    assert store.pending[0].id == p2.id


def test_pending_for_task():
    store = ProposalStore()
    p1 = AssignmentProposal(worker_name="api", task_id="t1", task_title="Fix bug")
    p2 = AssignmentProposal(worker_name="web", task_id="t2", task_title="Add feature")
    store.add(p1)
    store.add(p2)
    assert len(store.pending_for_task("t1")) == 1
    assert store.pending_for_task("t1")[0].worker_name == "api"
    assert len(store.pending_for_task("t3")) == 0


def test_pending_for_worker():
    store = ProposalStore()
    p1 = AssignmentProposal(worker_name="api", task_id="t1", task_title="Fix bug")
    p2 = AssignmentProposal(worker_name="api", task_id="t2", task_title="Add feature")
    store.add(p1)
    store.add(p2)
    assert len(store.pending_for_worker("api")) == 2
    assert len(store.pending_for_worker("web")) == 0


def test_remove():
    store = ProposalStore()
    p = AssignmentProposal(worker_name="api", task_id="t1", task_title="Fix bug")
    store.add(p)
    assert store.remove(p.id) is True
    assert store.get(p.id) is None
    assert store.remove("nonexistent") is False


def test_expire_stale():
    store = ProposalStore()
    p1 = AssignmentProposal(worker_name="api", task_id="t1", task_title="Fix bug")
    p2 = AssignmentProposal(worker_name="web", task_id="t2", task_title="Add feature")
    p3 = AssignmentProposal(worker_name="api", task_id="t2", task_title="Another")
    store.add(p1)
    store.add(p2)
    store.add(p3)

    # t1 still valid, t2 still valid, but "web" worker is gone
    expired = store.expire_stale(valid_task_ids={"t1", "t2"}, valid_worker_names={"api"})
    assert expired == 1  # p2 (web worker gone)
    assert p2.status == ProposalStatus.EXPIRED

    # t1 valid but t2 removed
    expired = store.expire_stale(valid_task_ids={"t1"}, valid_worker_names={"api"})
    assert expired == 1  # p3 (t2 gone)
    assert p3.status == ProposalStatus.EXPIRED


def test_clear_resolved():
    store = ProposalStore()
    p1 = AssignmentProposal(worker_name="api", task_id="t1", task_title="Fix bug")
    p2 = AssignmentProposal(worker_name="web", task_id="t2", task_title="Add feature")
    store.add(p1)
    store.add(p2)

    p1.status = ProposalStatus.APPROVED
    p2.status = ProposalStatus.REJECTED

    count = store.clear_resolved()
    assert count == 2
    assert len(store.all_proposals) == 0
    # Resolved proposals should now be in history
    assert len(store.history) == 2
    assert store.history[0].id == p2.id  # newest first
    assert store.history[1].id == p1.id


def test_clear_resolved_keeps_pending():
    store = ProposalStore()
    p1 = AssignmentProposal(worker_name="api", task_id="t1", task_title="Fix bug")
    p2 = AssignmentProposal(worker_name="web", task_id="t2", task_title="Add feature")
    store.add(p1)
    store.add(p2)

    p1.status = ProposalStatus.APPROVED
    count = store.clear_resolved()
    assert count == 1
    assert len(store.all_proposals) == 1
    assert store.get(p2.id) is p2


def test_proposal_age():
    import time

    p = AssignmentProposal(
        worker_name="api", task_id="t1", task_title="Fix bug", created_at=time.time() - 120
    )
    assert p.age >= 119  # allow tiny drift


def test_proposal_defaults():
    """New fields have sensible defaults."""
    p = AssignmentProposal(worker_name="api")
    assert p.task_id == ""
    assert p.confidence == 1.0
    assert p.proposal_type == "assignment"
    assert p.assessment == ""
    assert p.queen_action == ""


def test_escalation_proposal():
    """Escalation proposals have no task_id."""
    p = AssignmentProposal(
        worker_name="api",
        proposal_type="escalation",
        assessment="Worker is stuck on permission prompt",
        queen_action="send_message",
        message="yes",
        confidence=0.85,
    )
    assert p.task_id == ""
    assert p.proposal_type == "escalation"
    assert p.confidence == 0.85
    assert p.queen_action == "send_message"


def test_expire_stale_escalation_no_task():
    """Escalation proposals without task_id should not expire due to missing task."""
    store = ProposalStore()
    p = AssignmentProposal(
        worker_name="api",
        proposal_type="escalation",
        queen_action="continue",
    )
    store.add(p)

    # api is valid, no task_id to check
    expired = store.expire_stale(valid_task_ids=set(), valid_worker_names={"api"})
    assert expired == 0
    assert p.status == ProposalStatus.PENDING


def test_expire_stale_escalation_worker_gone():
    """Escalation proposals expire when the worker is gone."""
    store = ProposalStore()
    p = AssignmentProposal(
        worker_name="gone-worker",
        proposal_type="escalation",
        queen_action="wait",
    )
    store.add(p)

    expired = store.expire_stale(valid_task_ids=set(), valid_worker_names={"api"})
    assert expired == 1
    assert p.status == ProposalStatus.EXPIRED


# --- Factory classmethod tests ---


def test_factory_escalation():
    p = AssignmentProposal.escalation(
        worker_name="api",
        action="send_message",
        assessment="Worker stuck",
        message="yes",
        confidence=0.85,
    )
    assert p.proposal_type == "escalation"
    assert p.queen_action == "send_message"
    assert p.assessment == "Worker stuck"
    assert p.message == "yes"
    assert p.confidence == 0.85
    assert p.reasoning == "Worker stuck"  # defaults to assessment
    assert p.task_id == ""


def test_factory_escalation_defaults():
    p = AssignmentProposal.escalation(
        worker_name="api",
        action="continue",
        assessment="Stuck on prompt",
    )
    assert p.confidence == 0.6
    assert p.message == ""
    assert p.reasoning == "Stuck on prompt"


def test_factory_completion():
    p = AssignmentProposal.completion(
        worker_name="web",
        task_id="t1",
        task_title="Fix bug",
        assessment="All tests pass",
        reasoning="Worker idle 60s",
        confidence=0.9,
    )
    assert p.proposal_type == "completion"
    assert p.queen_action == "complete_task"
    assert p.task_id == "t1"
    assert p.task_title == "Fix bug"
    assert p.assessment == "All tests pass"
    assert p.reasoning == "Worker idle 60s"
    assert p.confidence == 0.9


def test_factory_completion_defaults():
    p = AssignmentProposal.completion(
        worker_name="web",
        task_id="t1",
        task_title="Fix bug",
        assessment="Done",
    )
    assert p.confidence == 0.8
    assert p.reasoning == ""


def test_factory_assignment():
    p = AssignmentProposal.assignment(
        worker_name="api",
        task_id="t2",
        task_title="Add feature",
        message="Please implement X",
        reasoning="Best fit",
        confidence=0.75,
    )
    assert p.proposal_type == "assignment"
    assert p.queen_action == ""
    assert p.task_id == "t2"
    assert p.task_title == "Add feature"
    assert p.message == "Please implement X"
    assert p.reasoning == "Best fit"
    assert p.confidence == 0.75


def test_factory_assignment_defaults():
    p = AssignmentProposal.assignment(
        worker_name="api",
        task_id="t2",
        task_title="Add feature",
        message="Do this",
    )
    assert p.confidence == 0.8
    assert p.reasoning == ""


# --- Guard method tests ---


def test_has_pending_escalation():
    store = ProposalStore()
    store.add(
        AssignmentProposal.escalation(worker_name="api", action="continue", assessment="stuck")
    )
    store.add(AssignmentProposal(worker_name="api", task_id="t1", task_title="Bug"))
    assert store.has_pending_escalation("api") is True
    assert store.has_pending_escalation("web") is False


def test_has_pending_completion():
    store = ProposalStore()
    store.add(
        AssignmentProposal.completion(
            worker_name="api", task_id="t1", task_title="Bug", assessment="done"
        )
    )
    assert store.has_pending_completion("api", "t1") is True
    assert store.has_pending_completion("api", "t2") is False
    assert store.has_pending_completion("web", "t1") is False


def test_has_pending_completion_ignores_non_completion():
    store = ProposalStore()
    store.add(AssignmentProposal(worker_name="api", task_id="t1", task_title="Bug"))
    assert store.has_pending_completion("api", "t1") is False


# --- History tests ---


def test_history_property():
    """History returns resolved proposals newest-first."""
    store = ProposalStore()
    p1 = AssignmentProposal(worker_name="api", task_id="t1", task_title="First")
    p2 = AssignmentProposal(worker_name="web", task_id="t2", task_title="Second")
    p3 = AssignmentProposal(worker_name="db", task_id="t3", task_title="Third")
    store.add(p1)
    store.add(p2)
    store.add(p3)

    p1.status = ProposalStatus.APPROVED
    p3.status = ProposalStatus.REJECTED
    store.clear_resolved()

    history = store.history
    assert len(history) == 2
    assert history[0].id == p3.id  # newest first
    assert history[1].id == p1.id


def test_recent_rejected_escalations_basic():
    """Returns only rejected escalations, newest-first, honoring limit."""
    import time

    store = ProposalStore()
    # A rejected escalation (should appear)
    e1 = AssignmentProposal.escalation(
        worker_name="api", action="send_message", assessment="select 1", rule_pattern="grep.*"
    )
    e1.created_at = time.time() - 100
    e1.status = ProposalStatus.REJECTED
    e1.rejection_reason = "not now"
    # A newer rejected escalation (should appear first)
    e2 = AssignmentProposal.escalation(
        worker_name="web", action="continue", assessment="nudge", rule_pattern="ls.*"
    )
    e2.created_at = time.time()
    e2.status = ProposalStatus.REJECTED
    # An APPROVED escalation (excluded — not rejected)
    e3 = AssignmentProposal.escalation(worker_name="db", action="continue", assessment="x")
    e3.status = ProposalStatus.APPROVED
    # A rejected COMPLETION (excluded — not an escalation)
    c1 = AssignmentProposal.completion(
        worker_name="api", task_id="t1", task_title="done?", assessment="y"
    )
    c1.status = ProposalStatus.REJECTED
    for p in (e1, e2, e3, c1):
        store.add(p)

    result = store.recent_rejected_escalations(limit=10)
    ids = [p.id for p in result]
    assert ids == [e2.id, e1.id]  # newest first, only rejected escalations
    # Limit honored
    assert store.recent_rejected_escalations(limit=1) == [e2]


def test_recent_rejected_escalations_includes_history():
    """Finds rejected escalations even after clear_resolved moved them to history."""
    store = ProposalStore()
    e = AssignmentProposal.escalation(
        worker_name="api", action="send_message", assessment="select 1"
    )
    store.add(e)
    e.status = ProposalStatus.REJECTED
    store.clear_resolved()  # moves it out of _proposals into _history

    result = store.recent_rejected_escalations()
    assert [p.id for p in result] == [e.id]


def test_recent_rejected_escalations_empty():
    """No rejections → empty list (the Queen gets no dead section)."""
    store = ProposalStore()
    assert store.recent_rejected_escalations() == []


def test_history_cap():
    """History is capped at ProposalStore._HISTORY_CAP entries."""
    store = ProposalStore()
    cap = ProposalStore._HISTORY_CAP
    for i in range(cap + 20):
        p = AssignmentProposal(worker_name=f"w{i}", task_id=f"t{i}", task_title=f"Task {i}")
        p.status = ProposalStatus.APPROVED
        store.add_to_history(p)
    assert len(store.history) == cap


def test_add_to_history():
    """add_to_history directly inserts into history without going through _proposals."""
    store = ProposalStore()
    p = AssignmentProposal.escalation(
        worker_name="api",
        action="continue",
        assessment="Auto-acted",
    )
    p.status = ProposalStatus.APPROVED
    store.add_to_history(p)

    assert len(store.all_proposals) == 0  # not in active proposals
    assert len(store.history) == 1
    assert store.history[0].id == p.id


# --- build_worker_task_info tests ---


def test_build_worker_task_info_no_board():
    assert build_worker_task_info(None, "api") == ""


def test_build_worker_task_info_no_active_tasks():
    class FakeBoard:
        def tasks_for_worker(self, name):
            return []

    assert build_worker_task_info(FakeBoard(), "api") == ""


def test_build_worker_task_info_with_tasks():
    from types import SimpleNamespace

    from swarm.tasks.task import TaskStatus

    t = SimpleNamespace(
        id="abcdef123456789",
        title="Fix the tests",
        status=TaskStatus.ASSIGNED,
        description="Run pytest and fix failures",
        acceptance_criteria=[],
    )

    class FakeBoard:
        def tasks_for_worker(self, name):
            return [t]

    result = build_worker_task_info(FakeBoard(), "api")
    assert "abcdef123456" in result
    assert "Fix the tests" in result
    assert "status=assigned" in result
    assert "Run pytest" in result


def test_build_worker_task_info_includes_acceptance_criteria():
    from types import SimpleNamespace

    from swarm.tasks.task import TaskStatus

    t = SimpleNamespace(
        id="abcdef123456789",
        title="Build feature",
        status=TaskStatus.ASSIGNED,
        description="Implement the widget",
        acceptance_criteria=["Tests pass", "No lint errors"],
    )

    class FakeBoard:
        def tasks_for_worker(self, name):
            return [t]

    result = build_worker_task_info(FakeBoard(), "api")
    assert "Acceptance Criteria:" in result
    assert "1. Tests pass" in result
    assert "2. No lint errors" in result


def test_build_worker_task_info_skips_completed():
    from types import SimpleNamespace

    from swarm.tasks.task import TaskStatus

    done = SimpleNamespace(
        id="done123456789",
        title="Already done",
        status=TaskStatus.DONE,
        description="",
    )

    class FakeBoard:
        def tasks_for_worker(self, name):
            return [done]

    assert build_worker_task_info(FakeBoard(), "api") == ""


# ── Time-based proposal expiry ──────────────────────────────────────


def test_expire_old_expires_old_proposals():
    """Proposals older than max_age should be expired."""
    import time

    store = ProposalStore()
    old = AssignmentProposal(
        worker_name="api",
        task_id="t1",
        task_title="Old",
        created_at=time.time() - 7200,
    )
    young = AssignmentProposal(
        worker_name="web",
        task_id="t2",
        task_title="Young",
    )
    store.add(old)
    store.add(young)

    count = store.expire_old(max_age=3600)
    assert count == 1
    assert old.status == ProposalStatus.EXPIRED
    assert young.status == ProposalStatus.PENDING


def test_expire_old_keeps_young_proposals():
    """Proposals within max_age should not be expired."""
    store = ProposalStore()
    p = AssignmentProposal(worker_name="api", task_id="t1", task_title="Fresh")
    store.add(p)

    count = store.expire_old(max_age=3600)
    assert count == 0
    assert p.status == ProposalStatus.PENDING


def test_expire_stale_includes_ttl():
    """expire_stale should also expire old proposals via TTL."""
    import time

    store = ProposalStore()
    old = AssignmentProposal(
        worker_name="api",
        task_id="t1",
        task_title="Old",
        created_at=time.time() - 7200,
    )
    store.add(old)

    count = store.expire_stale(valid_task_ids={"t1"}, valid_worker_names={"api"})
    assert count == 1
    assert old.status == ProposalStatus.EXPIRED
