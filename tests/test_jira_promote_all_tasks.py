"""#1445: ``board.all_tasks`` is a property, and one call site called it (#1445).

This was not a typing nit. ``all_tasks`` is decorated ``@property`` and returns
``list[SwarmTask]``, so ``board.all_tasks()`` raises
``TypeError: 'list' object is not callable`` at runtime. It survived because it
sits in the "task exists but has no owner YET" branch — reachable only in the
sub-second window between swarm_create_task returning and the background
assignment coroutine landing, which no test and few humans ever hit.

mypy named it as ``"list[SwarmTask]" not callable``; it was in the pre-existing
error set that CI had been red on, so nobody read it.
"""

from __future__ import annotations

import inspect

from swarm.tasks.board import TaskBoard


def test_all_tasks_is_a_property_not_a_method() -> None:
    assert isinstance(inspect.getattr_static(TaskBoard, "all_tasks"), property), (
        "all_tasks stopped being a property — every `board.all_tasks` "
        "reader is now returning a bound method instead of a list"
    )


def test_no_source_file_calls_all_tasks_as_a_method() -> None:
    """Guards the whole class of defect, not the single site that had it."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "swarm"
    offenders = [
        f"{p.relative_to(src)}:{i}"
        for p in src.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if ".all_tasks()" in line
    ]
    assert not offenders, (
        f"all_tasks is a property; calling it raises TypeError at runtime. "
        f"Offending call sites: {offenders}"
    )
