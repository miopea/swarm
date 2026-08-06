"""Workers must be TOLD to assert ACTIVE (#1282).

2026.8.5.5 made ACTIVE worker-asserted via ``swarm_start_task`` and removed
``WorkerStateTracker._promote_one_assigned``, which used to guess which ASSIGNED
task a BUZZING worker was on. That removal was right — it picked the
most-recently-updated task and hoped, which produced #1159.

But nothing ever taught workers the verb. Measured across ``src/`` before this
change, ``swarm_start_task`` appeared only in its own arg-type docstring, two
comments in ``drones/state_tracker.py``, and ``swarm_unblock_task``'s success text.
Meanwhile ``server/messages.py`` appended a block to EVERY dispatch teaching the
closing verb, with no counterpart for starting, and all five inline
``WORKFLOW_TEMPLATES`` ended with a closing step while none opened with marking work
in progress.

So "tasks are stuck in ASSIGNED" was never evidence against the design. It was
evidence that the mechanism the design depends on was undiscoverable.

WHAT THIS FILE DOES NOT DO: assert that any automatic promotion exists. #1282
deliberately added no third ``activate()`` caller —
``docs/specs/worker-asserted-active.md`` rejects a backstop hook as "two paths to
one transition, the thing #1104 exists to audit", and the narrower
exactly-one-assigned-task variant still infers WHETHER the worker is on a task at
all. ``test_status_exit_reachability.py`` still pins the caller count at exactly 2,
and that it needed no change is the evidence this work did not touch the state
machine.
"""

from __future__ import annotations

from swarm.tasks.task import TaskType
from swarm.tasks.workflows import WORKFLOW_TEMPLATES

_VERB = "swarm_start_task"


def _dispatch_instructions() -> str:
    from swarm.server.messages import _LIFECYCLE_INSTRUCTIONS

    return _LIFECYCLE_INSTRUCTIONS


# --- positive control, first ------------------------------------------------


def test_the_instruction_block_scan_is_honest():
    """POSITIVE CONTROL. Every assertion below is "this string contains that
    substring", which passes trivially if the string is empty or the import moved.

    Twice today a scan that silently measured nothing produced a confident wrong
    answer: an empty ``TaskBoard()`` looked like "every task invisible", and a 401
    returning 0 rows looked exactly like truncation. So prove the block is really
    the dispatch text by finding the verb that was ALREADY there.
    """
    block = _dispatch_instructions()
    assert "swarm_complete_task" in block, (
        "the pre-existing completion instruction is not in this block — the scan is "
        "reading the wrong string, so nothing below proves anything"
    )
    assert len(block) > 100, f"instruction block implausibly short ({len(block)} chars)"


# --- the dispatch body -----------------------------------------------------


def test_the_dispatch_instructions_teach_the_start_verb():
    """The core gap: a worker was told how to finish a task and never how to start
    one, while the board depended on it saying so."""
    assert _VERB in _dispatch_instructions(), (
        f"{_VERB} is absent from the dispatch instructions, so ACTIVE depends on a "
        f"verb no worker is ever told about"
    )


def test_the_start_instruction_is_conditional_not_unconditional():
    """Guards against trading the daemon's inference for the worker's.

    A worker told to "always mark this in progress" would assert ACTIVE for a task
    it may not be working, which reproduces #1159 one layer up instead of removing
    it. The wording must scope the assertion to work actually underway.
    """
    block = _dispatch_instructions().lower()
    assert "actually working" in block, (
        "the start instruction does not scope the assertion to the task actually "
        "being worked — an unconditional 'always call this' moves the deleted "
        "promoter's inference into the worker"
    )


def test_the_instruction_says_a_dispatched_task_is_already_started():
    """Otherwise every dispatched worker makes a redundant call and learns the verb
    is noise — the dispatch path already activates via start_task."""
    assert "already in progress" in _dispatch_instructions().lower()


# --- the inline workflow templates -----------------------------------------


_EXECUTABLE = [t for t in WORKFLOW_TEMPLATES if t is not TaskType.OPERATOR]


def test_there_are_executable_templates_to_check():
    """Control for the parametrised test below: an empty list would make it vacuous
    (0 tests collected reads as green)."""
    assert len(_EXECUTABLE) >= 4, f"expected several executable templates, got {_EXECUTABLE}"


def test_every_executable_workflow_template_names_the_start_verb():
    """Each template already ended with a closing step; none opened with marking the
    work in progress."""
    missing = [t.value for t in _EXECUTABLE if _VERB not in WORKFLOW_TEMPLATES[t]]
    assert not missing, f"workflow templates with no {_VERB} step: {missing}"


def test_the_operator_template_is_deliberately_excluded():
    """The OPERATOR template says DO NOT EXECUTE — it is a manual operator action no
    worker can perform. Telling a worker to mark it in progress would contradict the
    only instruction that template exists to give."""
    body = WORKFLOW_TEMPLATES[TaskType.OPERATOR]
    assert "DO NOT EXECUTE" in body, "the OPERATOR template changed shape; recheck this exclusion"
    assert _VERB not in body, (
        "the OPERATOR template now tells a worker to start a task it must not execute"
    )


# --- the idle nudge --------------------------------------------------------


def test_the_idle_nudge_carries_the_hint_and_does_not_misreport_status():
    """The nudge buckets from ``task_board.active_tasks``, which is ASSIGNED **or**
    ACTIVE, so calling them all "active" told the worker the board said something it
    did not — the same conflation that put queued tasks in the worker title bar.

    The hint rides on this existing message rather than getting a dedicated nudge:
    the design doc rejects a nudge about unasserted tasks, and this one already
    fires at exactly the worker who can resolve the ambiguity.
    """
    from swarm.drones.idle_watcher import _nudge_message

    msg = _nudge_message([1282])
    assert "#1282" in msg
    assert _VERB in msg, "the nudge does not name the verb that would resolve the ambiguity"
    assert "active but appear idle" not in msg, (
        "the nudge still calls ASSIGNED tasks 'active' — it is bucketed from "
        "active_tasks, which includes merely queued work"
    )
