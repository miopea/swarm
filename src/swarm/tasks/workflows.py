"""Workflow templates — type-specific instructions embedded in task assignments.

Task types with a matching Claude Code skill (global ~/.claude/commands/) use
a skill invocation instead of inline workflow steps.  The skill handles the
full pipeline (tooling detection, planning gates, TDD loops, commit offers).

The ``workflows:`` section in ``swarm.yaml`` can override or extend these
defaults.  For example::

    workflows:
      bug: /fix-and-ship
      feature: /feature
      verify: /verify
      chore: /my-custom-chore-skill

At runtime the (optional) ``SkillsStore`` supersedes both the defaults and
the config overrides — giving operators a DB-backed registry to inspect
and tweak without editing config. Usage is recorded per skill so stale
entries are easy to spot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from swarm.tasks.task import TaskType

if TYPE_CHECKING:
    from swarm.db.skills_store import SkillsStore

# Built-in defaults — overridable via config ``workflows:`` section.
_DEFAULT_SKILL_COMMANDS: dict[TaskType, str] = {
    TaskType.BUG: "/fix-and-ship",
    TaskType.FEATURE: "/feature",
    TaskType.VERIFY: "/verify",
}

# Description metadata for the default skills — used to seed the SkillsStore
# so the registry has something useful to show on first boot.
_DEFAULT_SKILL_DESCRIPTIONS: dict[str, tuple[str, list[str]]] = {
    "/fix-and-ship": ("Autonomous bug-fix pipeline: diagnose → TDD → validate → commit.", ["bug"]),
    "/feature": ("Implement a new feature with TDD and /check validation.", ["feature"]),
    "/verify": ("Read-only verification and QA pass.", ["verify"]),
}

# Fallback inline templates for types without a skill.
WORKFLOW_TEMPLATES: dict[TaskType, str] = {
    # #1282: every template ended with a closing step and none opened with
    # marking the work in progress, so ACTIVE — which is worker-asserted since
    # 2026.8.5.5 — was never asked for. The OPERATOR template below is
    # deliberately EXCLUDED: it says DO NOT EXECUTE, and telling a worker to
    # mark in progress a task no worker may perform would contradict it.
    TaskType.CHORE: """\
## Workflow: General Task
1. Mark it in progress (swarm_start_task) if it isn't already
2. Complete the task as described
3. Validate your changes (run tests if applicable)
4. Commit when done""",
    TaskType.CONTENT: """\
## Workflow: Content Task
1. Mark it in progress (swarm_start_task) if it isn't already
2. Research and gather source material
3. Draft the content (script, article, plan)
4. Review and refine
5. Mark complete when ready for next step""",
    TaskType.REVIEW: """\
## Workflow: Review Task
1. Mark it in progress (swarm_start_task) if it isn't already
2. Review the deliverable against acceptance criteria
3. Provide feedback or approve
4. Mark complete when satisfied""",
    TaskType.PUBLISH: """\
## Workflow: Publish Task
1. Mark it in progress (swarm_start_task) if it isn't already
2. Prepare the content for the target platform
3. Publish or schedule publication
4. Verify the published content""",
    TaskType.INGEST: """\
## Workflow: Ingest Task
1. Mark it in progress (swarm_start_task) if it isn't already
2. Connect to the data source
3. Extract and transform the data
4. Store results for downstream processing""",
    # #405: operator-only action — no worker can execute it. If this ever
    # reaches a worker PTY, the worker should NOT attempt it.
    TaskType.OPERATOR: """\
## Operator action — DO NOT EXECUTE
This task requires a manual operator action (e.g. a GitHub org-admin
change) that no worker can perform. Do not attempt it. If you received
this, report it back to the operator/Queen — it should never have been
dispatched to a worker (see #405).""",
}

# Resolved map — starts as defaults, merged with config at init time.
SKILL_COMMANDS: dict[TaskType, str] = dict(_DEFAULT_SKILL_COMMANDS)


def apply_config_overrides(overrides: dict[str, str]) -> None:
    """Merge ``workflows:`` config section into the skill commands map.

    Called once at daemon startup.  Keys are TaskType value strings
    (``bug``, ``feature``, ``verify``, ``chore``).  An empty/null value
    removes the skill for that type (falls back to inline template).
    """
    type_lookup = {t.value: t for t in TaskType}
    for key, cmd in overrides.items():
        task_type = type_lookup.get(key.lower())
        if task_type is None:
            continue
        if cmd:
            SKILL_COMMANDS[task_type] = cmd
        else:
            SKILL_COMMANDS.pop(task_type, None)


# DB-backed registry — populated at daemon startup via ``attach_skills_store``.
# Kept at module scope so ``get_skill_command`` doesn't need to be threaded
# through every caller.
_SKILLS_STORE: SkillsStore | None = None


def attach_skills_store(store: SkillsStore) -> None:
    """Wire a ``SkillsStore`` into skill resolution.

    Seeds the store with built-in defaults (idempotent — existing rows
    are untouched) so a fresh DB starts with the canonical mapping.
    """
    global _SKILLS_STORE
    _SKILLS_STORE = store
    store.seed_defaults(_DEFAULT_SKILL_DESCRIPTIONS)


def detach_skills_store() -> None:
    """Clear the attached store (primarily for tests)."""
    global _SKILLS_STORE
    _SKILLS_STORE = None


def get_skills_store() -> SkillsStore | None:
    return _SKILLS_STORE


def _lookup_from_store(task_type: TaskType) -> str | None:
    """Return the first registered skill whose ``task_types`` includes
    *task_type*. Returns ``None`` when no store is attached or when no
    skill claims this type — callers fall back to the in-memory map.
    """
    if _SKILLS_STORE is None:
        return None
    try:
        for skill in _SKILLS_STORE.list_all():
            if task_type.value in skill.task_types:
                return skill.name
    except Exception:
        return None
    return None


def get_skill_command(task_type: TaskType) -> str | None:
    """Return the slash-command for *task_type*, or ``None`` if it has no skill.

    Resolution order: DB-backed registry → in-memory ``SKILL_COMMANDS``
    map (defaults + ``workflows:`` config overrides). On a cache hit
    from either source the call is logged as usage in the registry so
    stale/unused skills become visible.
    """
    from_store = _lookup_from_store(task_type)
    chosen = from_store or SKILL_COMMANDS.get(task_type)
    if chosen and _SKILLS_STORE is not None:
        try:
            _SKILLS_STORE.record_usage(chosen)
        except Exception:
            pass
    return chosen


def get_workflow_instructions(task_type: TaskType) -> str:
    """Return inline workflow instructions for the given task type.

    Only returns text for types that do NOT have a dedicated skill.
    """
    return WORKFLOW_TEMPLATES.get(task_type, WORKFLOW_TEMPLATES[TaskType.CHORE])
