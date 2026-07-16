"""Task message builder — pure functions for formatting task messages to workers."""

from __future__ import annotations

from pathlib import Path

from swarm.tasks.task import SwarmTask, TaskType

_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"})
# Plain-text-ish formats Read handles natively.
_TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".json",
        ".yaml",
        ".yml",
        ".csv",
        ".tsv",
        ".log",
        ".xml",
        ".html",
        ".htm",
        ".ini",
        ".toml",
        ".eml",
    }
)
_DOCX_EXTENSIONS = frozenset({".docx", ".doc"})
_PDF_EXTENSIONS = frozenset({".pdf"})
_XLSX_EXTENSIONS = frozenset({".xlsx", ".xls", ".ods"})
_PPTX_EXTENSIONS = frozenset({".pptx", ".ppt", ".odp"})

_COMPLETION_INSTRUCTIONS = """\

When done, use the swarm_complete_task MCP tool with a brief resolution summary.
If the task originated from another worker, send them a swarm_send_message with your findings."""

# Plan-mode gate for user-request tasks. The rule: tasks where
# ``SwarmTask.source_worker`` is empty came from a user channel (Jira sync,
# email import, operator dashboard) and get a plan-approval gate prepended.
# Tasks created by another worker (cross-project handoff, MCP
# ``swarm_create_task`` with ``source_worker`` set, or the auto-handoff drone)
# bypass — that peer already reasoned about the work, so a second plan-mode
# round would just slow the swarm down.
#
# Workers cooperate with this by calling Claude Code's ``ExitPlanMode`` tool,
# which surfaces the plan in the PTY and parks the worker in WAITING until
# the operator approves from the dashboard. The mechanism is already wired:
# ``server/routes/workers.py`` detects "plan mode on" prompts, and the
# interactive Queen has plan-presentation handling at
# ``queen/queen.py``. No new approval UI is required.
_PLAN_MODE_PREAMBLE = """\
This task came from a user request (Jira ticket, email, or the operator dashboard). \
Use plan mode BEFORE making any changes:

1. Read the task description below and any linked context.
2. Investigate read-only — open relevant files, search the codebase, check git \
history, verify assumptions against the real system if external (database, \
third-party API, CRM, etc.).
3. Call the ExitPlanMode tool with a concrete proposed approach: what you'll \
change, which files, what tests you'll add, what the failure modes are, and \
what you've ruled out.
4. WAIT for the operator to approve the plan from the dashboard.
5. After approval, execute the plan as agreed.

DO NOT edit files, run mutating shell commands, invoke skills, or call \
swarm_complete_task before plan approval. If the task body below invokes a \
skill like /feature or /fix-and-ship, wrap the plan around the skill \
invocation — don't run the skill yet. Worker-to-worker handoffs skip this \
gate; this preamble appears because the task came from a user channel.

--- TASK ---
"""


# Environmental-causes nudge for bug-fix tasks. Debugging cycles get burned on
# stale/dev data, file locks, and missing env vars that masquerade as code bugs.
# Scoped to ``TaskType.BUG`` only — feature/chore/verify work doesn't hit the
# "is it the environment or a real bug?" question, so this would just be noise.
_ENV_CAUSES_PREAMBLE = """\
Before assuming a code bug, first rule out environmental causes — stale/dev \
data, file locks, missing env vars — and state which you ruled out.

"""


def _enrichment_block(task: SwarmTask, *, claude_gated: bool) -> str:
    """Build the dispatch-enrichment block (P2): the acceptance criteria as an
    explicit, graded done-definition plus an advisory effort tier.

    The criteria are the same rubric the verifier grades against, so surfacing
    them here aligns what's asked with what's checked. The effort tier is
    ADVISORY and only rendered for providers that support workflows/subagents
    (``claude_gated``) — Swarm never enforces a worker's internal fan-out, and
    non-Claude providers can't act on the hint. Empty string when the task has
    neither criteria nor an actionable tier (context-engineering: no noise).
    """
    lines: list[str] = []
    crits = [c for c in task.acceptance_criteria if c.strip()]
    if crits:
        lines.append("\n--- ACCEPTANCE CRITERIA (your completion is graded against these) ---")
        lines.extend(f"- {c}" for c in crits)
        lines.append("Ensure each is satisfied before calling swarm_complete_task.")
    if claude_gated and task.effort_tier == "high":
        lines.append(
            "\nComplexity: high — this looks cross-cutting. Consider fanning out "
            "subagents or a dynamic workflow to cover it thoroughly."
        )
    elif claude_gated and task.effort_tier == "medium":
        lines.append("\nComplexity: medium — scope may span multiple files; plan before diving in.")
    return "\n".join(lines)


def requires_plan_approval(task: SwarmTask, *, enabled: bool = True) -> bool:
    """Tasks from user channels (no peer worker source) require a plan gate.

    Returns ``True`` when the task originated from the operator, the Jira
    sync, or the email import (i.e. ``source_worker`` is empty) AND the
    feature is enabled via ``DroneConfig.user_request_plan_mode``.

    Returns ``False`` when another worker created the task (``source_worker``
    set) — cross-project handoffs and MCP ``swarm_create_task`` calls — or
    when the operator disabled the gate globally.
    """
    if not enabled:
        return False
    return not bool(task.source_worker)


def _attachment_hint(path: str) -> str:
    """Return a one-line instruction telling the worker how to read this file.

    Read handles plain-text and images natively. Office formats are zipped
    XML / proprietary binary — Read returns garbled bytes — so we name the
    common conversion command (pandoc / pdftotext / docx2txt / openpyxl)
    so the worker doesn't have to guess.
    """
    ext = Path(path).suffix.lower()
    if ext in _IMAGE_EXTENSIONS:
        return f"IMAGE: {path} — Use the Read tool to view this image file."
    if ext in _TEXT_EXTENSIONS:
        return f"TEXT: {path} — Use the Read tool for context."
    if ext in _DOCX_EXTENSIONS:
        return (
            f"WORD DOC: {path} — Read returns binary garbage on .docx; convert first. "
            f"Try: `pandoc {path!r} -t plain` "
            f'or `python -c "import docx2txt; print(docx2txt.process({path!r}))"`.'
        )
    if ext in _PDF_EXTENSIONS:
        return (
            f"PDF: {path} — Convert to text first. "
            f"Try: `pdftotext {path!r} -` "
            f'or `python -c "import pypdf; r=pypdf.PdfReader({path!r}); '
            f"print('\\n'.join(p.extract_text() for p in r.pages))\"`."
        )
    if ext in _XLSX_EXTENSIONS:
        return (
            f"SPREADSHEET: {path} — Read won't help; load with openpyxl or convert to CSV. "
            f'Try: `python -c "import openpyxl; wb=openpyxl.load_workbook({path!r}); '
            f'[print(s.title, list(s.values)) for s in wb]"`.'
        )
    if ext in _PPTX_EXTENSIONS:
        return (
            f"PRESENTATION: {path} — Convert to text. "
            f"Try: `pandoc {path!r} -t plain` "
            f'or `python -c "import pptx; p=pptx.Presentation({path!r}); ..."`.'
        )
    return (
        f"{path} — Try the Read tool first. If it returns binary, run "
        f"`file {path!r}` to identify the format, then pick a converter."
    )


def task_detail_parts(task: SwarmTask) -> list[str]:
    """Collect title, description, tags, and source metadata into a parts list."""
    parts: list[str] = [f"#{task.number}: {task.title}" if task.number else task.title]
    if task.description:
        parts.append(task.description)
    if task.tags:
        parts.append(f"Tags: {', '.join(task.tags)}")
    # Source metadata — lets the worker know the task's external origin
    source_parts: list[str] = []
    if task.jira_key:
        source_parts.append(f"Jira: {task.jira_key}")
    if task.source_email_id:
        source_parts.append(f"Email: {task.source_email_id}")
    if source_parts:
        parts.append(f"Source: {', '.join(source_parts)}")
    return parts


def attachment_lines(task: SwarmTask) -> str:
    """Format attachment paths as separate lines for the worker.

    Per-format hints tell the worker which tool to use — Read for images
    and plain text, conversion commands (pandoc / pdftotext / openpyxl)
    for Office binary formats where Read would just return zip bytes.
    """
    if not task.attachments:
        return ""
    lines = ["\nAttachments:"]
    for a in task.attachments:
        lines.append(f"  - {_attachment_hint(a)}")
    return "\n".join(lines)


def _build_inline_body(task: SwarmTask, completion: str) -> str:
    """Inline workflow-instruction body for task types without a dedicated
    skill (CHORE, unknown types, non-Claude providers)."""
    from swarm.tasks.workflows import get_workflow_instructions

    prefix = f"Task #{task.number}: " if task.number else "Task: "
    parts = [f"{prefix}{task.title}"]
    if task.description:
        parts.append(f"\n{task.description}")
    atts = attachment_lines(task)
    if atts:
        parts.append(atts)
    if task.tags:
        parts.append(f"\nTags: {', '.join(task.tags)}")
    source_parts: list[str] = []
    if task.jira_key:
        source_parts.append(f"Jira: {task.jira_key}")
    if task.source_email_id:
        source_parts.append(f"Email: {task.source_email_id}")
    if source_parts:
        parts.append(f"\nSource: {', '.join(source_parts)}")
    workflow = get_workflow_instructions(task.task_type)
    if workflow:
        parts.append(f"\n{workflow}")
    parts.append(completion)
    return "\n".join(parts)


def build_task_message(
    task: SwarmTask,
    *,
    supports_slash_commands: bool = True,
    plan_mode_for_user_requests: bool = True,
    enrich_dispatch: bool = True,
) -> str:
    """Build a message string describing a task for a worker.

    If the task type has a dedicated Claude Code skill (e.g. ``/feature``),
    the message is formatted as a skill invocation so the worker's Claude
    session handles the full pipeline.  Otherwise, inline workflow steps
    are appended as before.

    When *supports_slash_commands* is False (non-Claude providers), skill
    invocations are skipped and inline workflow instructions are used instead.

    When *plan_mode_for_user_requests* is True (default), tasks originating
    from a user channel (Jira/email/operator — i.e. ``source_worker`` empty)
    get a plan-mode preamble prepended so the worker investigates read-only
    and presents a plan for operator approval before making changes. Worker-
    to-worker handoffs always skip the gate.

    Attachments are always listed on separate lines (never squished into
    the skill command's quoted argument) so the worker can see and read them.
    """
    from swarm.tasks.workflows import get_skill_command

    completion = _COMPLETION_INSTRUCTIONS.format(task_id=task.id)

    skill = get_skill_command(task.task_type) if supports_slash_commands else None
    if skill:
        desc = " ".join(task_detail_parts(task))
        msg = f'{skill} "{desc}"'
        atts = attachment_lines(task)
        if atts:
            msg = f"{msg}{atts}"
        body = msg + completion
    else:
        body = _build_inline_body(task, completion)

    # Dispatch enrichment (P2): append the acceptance-criteria done-definition
    # + advisory effort tier. Sits inside the preambles below (which stay
    # outermost) so plan-mode/bug guidance is still read first.
    if enrich_dispatch:
        block = _enrichment_block(task, claude_gated=supports_slash_commands)
        if block:
            body = f"{body}\n{block}"

    # Bug-fix nudge sits inside the plan-mode preamble (which stays outermost).
    if task.task_type == TaskType.BUG:
        body = _ENV_CAUSES_PREAMBLE + body
    if requires_plan_approval(task, enabled=plan_mode_for_user_requests):
        body = _PLAN_MODE_PREAMBLE + body
    return body


# Native /goal: the provider's evaluator only judges what's already in the
# session transcript — it does not run tools or read files. So the condition
# must be phrased as something the worker's own output demonstrates, and
# (per the docs) carry an explicit runaway bound. ≤ 4000 chars, one line.
_GOAL_MAX_LEN = 4000


def render_goal_condition(criteria: list[str], *, max_turns: int) -> str:
    """Render a task's acceptance criteria as a one-line native /goal condition.

    Empty criteria → "" (caller skips — no goal is set). The string is
    the argument to ``/goal`` (the caller prepends ``/goal ``).
    """
    cleaned = [" ".join(str(c).split()) for c in criteria if str(c).strip()]
    if not cleaned:
        return ""
    enumerated = "; ".join(f"({i}) {c}" for i, c in enumerate(cleaned, 1))
    condition = (
        f"All of these hold, each demonstrated in your own output: {enumerated}"
        f" — or stop after {max_turns} turns and report what's blocking."
    )
    if len(condition) > _GOAL_MAX_LEN:
        condition = condition[:_GOAL_MAX_LEN]
    return condition
