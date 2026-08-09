"""Both proposal stores must honour the same contract.

FOUND IN PRODUCTION 2026-08-09, by reading the log after a real sync cycle:

    TypeError: SqliteProposalStore.expire_stale() got an unexpected keyword argument
    'assignable_task_ids'

2026.8.8.16 taught ProposalStore to stop expiring a proposal merely because its task is
not ASSIGNABLE. THE DAEMON USES SqliteProposalStore, which was never updated. So the fix
never took effect in production, and once the caller passed the new argument every sweep
raised — expiring nothing at all.

Every test for that fix built the IN-MEMORY store, so all of them passed against code
the daemon never runs. That is the gap this file closes: the same assertions, run
against both implementations.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm.db.core import SwarmDB
from swarm.db.proposal_store import SqliteProposalStore
from swarm.tasks.proposal import AssignmentProposal, ProposalStore


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path):
    """The same tests against both implementations. Parametrised rather than duplicated,
    because a copy of the test file is exactly what did NOT get written last time."""
    if request.param == "memory":
        return ProposalStore()
    return SqliteProposalStore(SwarmDB(tmp_path / "swarm.db"))


def _promotion(task_id: str = "owned") -> AssignmentProposal:
    return AssignmentProposal.jira_promotion(
        worker_name="api", task_id=task_id, task_title="t", project="WWD", reasoning="r"
    )


def test_a_promotion_survives_an_unassignable_task(store) -> None:
    """THE PRODUCTION CASE. A promotion always references a task its worker already
    owns, so it can never be assignable — expiring on that basis kills it seconds after
    it is raised, before an operator can approve it."""
    store.add(_promotion())

    expired = store.expire_stale(
        valid_task_ids={"owned"}, valid_worker_names={"api"}, assignable_task_ids=set()
    )

    assert expired == 0, "the promotion was expired for not being assignable"
    assert len(store.pending) == 1


def test_an_assignment_IS_expired_when_its_task_is_taken(store) -> None:
    """The other half — an assignment proposal for an owned task is genuinely moot."""
    store.add(AssignmentProposal(worker_name="api", task_id="taken", task_title="t"))

    expired = store.expire_stale(
        valid_task_ids={"taken"}, valid_worker_names={"api"}, assignable_task_ids=set()
    )

    assert expired == 1, "a stale assignment proposal survived"


def test_a_proposal_for_a_vanished_task_is_expired(store) -> None:
    store.add(_promotion("gone"))
    assert store.expire_stale({"other"}, {"api"}, assignable_task_ids=set()) == 1


def test_a_proposal_for_a_vanished_worker_is_expired(store) -> None:
    store.add(_promotion())
    assert store.expire_stale({"owned"}, {"someone-else"}, assignable_task_ids={"owned"}) == 1


def test_the_argument_is_optional_on_both(store) -> None:
    """Older callers must keep working; the daemon crashed precisely because a new
    keyword reached a signature that did not accept it."""
    store.add(_promotion())
    store.expire_stale({"owned"}, {"api"})  # must not raise


def test_both_stores_expose_the_same_expire_signature() -> None:
    """Pinned directly, because the divergence is what broke: one implementation grew a
    parameter and the other did not, and nothing compared them."""
    import inspect

    mem = list(inspect.signature(ProposalStore.expire_stale).parameters)
    sql = list(inspect.signature(SqliteProposalStore.expire_stale).parameters)
    assert mem == sql, f"the stores have diverged: {mem} vs {sql}"
