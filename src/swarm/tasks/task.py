"""SwarmTask — internal task model for agent coordination."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

_log = logging.getLogger("swarm.tasks.task")


class TaskStatus(Enum):
    """Lifecycle states for a SwarmTask.

    Vocabulary chosen to mirror operator triage (see
    ``docs/specs/headless-queen-architecture.md`` and the abundant-greeting-muffin
    plan):

    * ``BACKLOG`` — parked. Nothing happens automatically; awaits operator
      action. This is where Queen-drafted proposals and reopened tasks
      land. Operator promotes via "Hand to Queen" → ``UNASSIGNED`` or
      directly assigns a worker → ``ASSIGNED``.
    * ``UNASSIGNED`` — operator endorsed; the auto-assign drone is
      eligible to pick a worker (when enabled in config).
    * ``ASSIGNED`` — a specific worker has it; sitting in their queue
      or mid-dispatch. Inter-worker (#225 task-push) tasks land here
      directly.
    * ``ACTIVE`` — worker is actually running it (state-tracker confirmed
      engagement).
    * ``DONE`` — completed successfully.
    * ``FAILED`` — worker hit a wall and gave up.
    """

    BACKLOG = "backlog"
    UNASSIGNED = "unassigned"
    ASSIGNED = "assigned"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    # #405 INV-2: a worker's ACTIVE task moves here (instead of ASSIGNED)
    # when parked on a worker-reported blocker binding. Held — not
    # auto-assignable, not "active".
    BLOCKED = "blocked"


class VerificationStatus(Enum):
    """Verifier drone outcome for a task (item 4 of the 10-repo bundle).

    The verifier drone fires asynchronously after ``swarm_complete_task``
    and either confirms the work or reopens the task. Status values:

    * ``NOT_RUN`` — verifier hasn't fired yet (default).
    * ``VERIFIED`` — tier 1 + tier 2 both pass; task ships.
    * ``REOPENED`` — tier 1 or tier 2 reopened the task; worker is
      receiving findings via inbox warning.
    * ``ESCALATED`` — self-loop guard hit (max reopens); operator
      thread filed via the Queen.
    * ``SKIPPED`` — ``queen_force_complete_task`` overrode verification.
    """

    NOT_RUN = "not_run"
    VERIFIED = "verified"
    REOPENED = "reopened"
    ESCALATED = "escalated"
    SKIPPED = "skipped"


class TaskPriority(Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class TaskType(Enum):
    BUG = "bug"
    VERIFY = "verify"
    FEATURE = "feature"
    CHORE = "chore"
    CONTENT = "content"
    REVIEW = "review"
    PUBLISH = "publish"
    INGEST = "ingest"
    # #405: operator-only action (e.g. a GitHub org-admin change) that no
    # worker can execute. Must never occupy a worker-ACTIVE state.
    OPERATOR = "operator"


class DependencyType(Enum):
    BLOCKS = "blocks"
    ENHANCES = "enhances"
    ENABLES = "enables"


@dataclass
class SwarmTask:
    """A unit of work that can be assigned to a worker."""

    title: str
    description: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: TaskStatus = TaskStatus.UNASSIGNED
    priority: TaskPriority = TaskPriority.NORMAL
    task_type: TaskType = TaskType.CHORE
    assigned_worker: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    # #611 P2: when the task last went ACTIVE — the signal _recon_inv1 uses to
    # keep the in-flight task (earliest-started) rather than newest-by-updated_at
    # (updated_at bumps on any edit, so it could demote a long-running job).
    # None for never-started / legacy tasks → callers fall back to created_at.
    started_at: float | None = None
    depends_on: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)  # file paths
    resolution: str = ""  # explanation of what was done (filled on completion)
    block_reason: str = ""  # #405: why this task is BLOCKED (held off-active)
    # #876: free-text reference to the EXTERNAL/upstream dependency a BLOCKED
    # task is waiting on (e.g. an npm package@range release, a vendor PR URL).
    # Set by the blocked-on-external flow (``board.block_on_external`` /
    # ``swarm_block_on_external``); empty for internal operator/blocker parks.
    # Cleared on resume (``task.start``). Persisted (schema v15).
    external_blocker_ref: str = ""
    source_email_id: str = ""  # Graph message ID if created from email
    jira_key: str = ""  # Jira ticket key (e.g. "PROJ-123") if synced from Jira
    number: int = 0  # auto-incrementing display number (set by TaskBoard)
    # Cross-project task fields
    is_cross_project: bool = False
    source_worker: str = ""
    target_worker: str = ""
    dependency_type: str = "blocks"  # store as string for simplicity
    acceptance_criteria: list[str] = field(default_factory=list)
    context_refs: list[str] = field(default_factory=list)
    # Cost budgeting (0.0 = unlimited)
    cost_budget: float = 0.0
    cost_spent: float = 0.0
    _cost_warned: bool = field(default=False, repr=False)
    # Token-budget governor (#762): per-task OUTPUT-token burn accounting.
    # Runtime-only (NOT persisted) — accumulated from worker usage deltas
    # while this task is ACTIVE and reset on daemon restart along with the
    # worker delta tracking. The ceiling itself lives in
    # ``DroneConfig.task_token_ceiling`` (0 = disabled). ``_token_ceiling_
    # breached`` is the one-shot guard so the escalate+park fires once.
    tokens_spent: int = 0
    _token_ceiling_breached: bool = field(default=False, repr=False)
    # Knowledge consolidation: learnings captured on completion
    learnings: str = ""
    # Verifier drone state (item 4 of the 10-repo bundle).
    # ``verification_status`` flips through NOT_RUN → VERIFIED / REOPENED
    # / ESCALATED / SKIPPED as the verifier acts. ``verification_reason``
    # carries the verifier's rationale for the most recent verdict.
    # ``verification_reopen_count`` is the self-loop guard counter —
    # incremented on every verifier reopen; once it crosses
    # ``VERIFIER_MAX_REOPENS`` the task escalates to the operator
    # instead of reopening.
    verification_status: VerificationStatus = VerificationStatus.NOT_RUN
    verification_reason: str = ""
    verification_reopen_count: int = 0

    def assign(self, worker_name: str) -> None:
        self.assigned_worker = worker_name
        self.status = TaskStatus.ASSIGNED
        self.updated_at = time.time()

    def unassign(self) -> None:
        self.assigned_worker = None
        self.status = TaskStatus.UNASSIGNED
        self.updated_at = time.time()

    def start(self) -> None:
        now = time.time()
        self.status = TaskStatus.ACTIVE
        self.started_at = now
        # Resuming clears any stale hold reason — covers the operator
        # unpark of an operator-blocked task (BLOCKED→ACTIVE via
        # board.activate) and any #405 blocker-binding resume. #876: also
        # clears the external watch reference so a resumed task no longer
        # advertises a (now-satisfied) upstream dependency.
        self.block_reason = ""
        self.external_blocker_ref = ""
        self.updated_at = now

    def block(self, reason: str = "", external_ref: str = "") -> None:
        """#405 INV-2: park this task off-ACTIVE on a blocker binding.

        Keeps ``assigned_worker`` (the same worker resumes when the
        blocker clears) but the task no longer counts as worker-active.

        #876: ``external_ref`` records an EXTERNAL/upstream dependency the
        task is waiting on (no internal task number exists for it) — e.g. a
        package release or vendor PR. Left empty for internal operator /
        blocker-binding parks.
        """
        self.status = TaskStatus.BLOCKED
        self.block_reason = reason
        self.external_blocker_ref = external_ref
        self.updated_at = time.time()

    @property
    def is_operator_action(self) -> bool:
        """Operator-only task (no worker can execute it) — #405 rule:
        never occupies a worker-ACTIVE state."""
        return self.task_type == TaskType.OPERATOR

    def complete(self, resolution: str = "") -> None:
        self.status = TaskStatus.DONE
        self.completed_at = time.time()
        self.updated_at = time.time()
        if resolution:
            self.resolution = resolution

    def fail(self) -> None:
        self.status = TaskStatus.FAILED
        self.updated_at = time.time()

    def reopen(self) -> None:
        """Reopen a Done/Failed task. Lands in Backlog so the operator can
        review the resolution and decide whether to promote, retarget, or
        remove. (Vocabulary cleanup: pre-rename this dropped to PENDING.)"""
        self.status = TaskStatus.BACKLOG
        self.assigned_worker = None
        self.completed_at = None
        self.resolution = ""
        self.updated_at = time.time()

    def reopen_for_verifier(self, *, reason: str) -> None:
        """Reopen a task that the verifier rejected, keeping the worker.

        Differs from :meth:`reopen` in two ways:

        * Status flips to ASSIGNED (not BACKLOG) — the worker who
          claimed completion is the right person to address the
          verifier's findings.
        * The previously-assigned worker is preserved. The existing
          ``IdleWatcher`` drone picks them up on its next sweep so
          they receive a nudge tied to the verifier's warning
          message.

        Increments ``verification_reopen_count`` for the self-loop
        guard. Resolution is cleared so the next completion is fresh.
        """
        self.status = TaskStatus.ASSIGNED
        self.completed_at = None
        self.resolution = ""
        self.verification_status = VerificationStatus.REOPENED
        self.verification_reason = reason
        self.verification_reopen_count += 1
        self.updated_at = time.time()

    def approve(self) -> None:
        """Promote a Backlog task to Unassigned ("Hand to Queen").

        Used both for cross-project proposals and operator-promoted
        backlog rows. The auto-assign drone picks up Unassigned tasks
        (when enabled).
        """
        assert self.status == TaskStatus.BACKLOG, (
            f"Cannot approve task in {self.status.value} state"
        )
        self.status = TaskStatus.UNASSIGNED
        self.updated_at = time.time()

    def reject(self, resolution: str = "") -> None:
        """Reject a Backlog task, marking it Failed."""
        assert self.status == TaskStatus.BACKLOG, f"Cannot reject task in {self.status.value} state"
        self.status = TaskStatus.FAILED
        self.updated_at = time.time()
        if resolution:
            self.resolution = resolution

    @property
    def is_available(self) -> bool:
        """True when the auto-assign drone is allowed to pick this task up.

        In the new vocabulary only ``UNASSIGNED`` qualifies — Backlog
        tasks are explicitly parked and need an operator promotion before
        they enter the work pipeline.
        """
        return self.status == TaskStatus.UNASSIGNED

    @property
    def age(self) -> float:
        return time.time() - self.created_at


# Canonical display constants — single source of truth for all UIs.
# Vocabulary: Backlog / Unassigned / Assigned / Active / Done / Failed.
# Keep this map in sync with ``TaskStatus`` — the label-coverage test in
# ``tests/test_status_label_map.py`` enforces every member has an entry.
STATUS_ICON = {
    TaskStatus.BACKLOG: "◇",
    TaskStatus.UNASSIGNED: "○",
    TaskStatus.ASSIGNED: "◐",
    TaskStatus.ACTIVE: "●",
    TaskStatus.BLOCKED: "⊘",
    TaskStatus.DONE: "✓",
    TaskStatus.FAILED: "✗",
}

STATUS_LABEL: dict[TaskStatus, str] = {
    TaskStatus.BACKLOG: "Backlog",
    TaskStatus.UNASSIGNED: "Unassigned",
    TaskStatus.ASSIGNED: "Assigned",
    TaskStatus.ACTIVE: "In Progress",
    TaskStatus.BLOCKED: "Blocked",
    TaskStatus.DONE: "Done",
    TaskStatus.FAILED: "Failed",
}

DEPENDENCY_TYPE_MAP: dict[str, DependencyType] = {
    "blocks": DependencyType.BLOCKS,
    "enhances": DependencyType.ENHANCES,
    "enables": DependencyType.ENABLES,
}

PRIORITY_LABEL = {
    TaskPriority.URGENT: "!!",
    TaskPriority.HIGH: "!",
    TaskPriority.NORMAL: "",
    TaskPriority.LOW: "↓",
}

PRIORITY_MAP: dict[str, TaskPriority] = {
    "low": TaskPriority.LOW,
    "normal": TaskPriority.NORMAL,
    "high": TaskPriority.HIGH,
    "urgent": TaskPriority.URGENT,
}

TYPE_MAP: dict[str, TaskType] = {
    "bug": TaskType.BUG,
    "verify": TaskType.VERIFY,
    "feature": TaskType.FEATURE,
    "chore": TaskType.CHORE,
    "content": TaskType.CONTENT,
    "review": TaskType.REVIEW,
    "publish": TaskType.PUBLISH,
    "ingest": TaskType.INGEST,
}


def validate_priority(raw: str) -> TaskPriority:
    """Parse and validate a priority string.

    Raises ``ValueError`` on invalid input.
    """
    if raw not in PRIORITY_MAP:
        opts = ", ".join(sorted(PRIORITY_MAP))
        raise ValueError(f"priority must be one of: {opts}")
    return PRIORITY_MAP[raw]


def validate_task_type(raw: str) -> TaskType:
    """Parse and validate a task_type string.

    Raises ``ValueError`` on invalid input.
    """
    if raw not in TYPE_MAP:
        opts = ", ".join(sorted(TYPE_MAP))
        raise ValueError(f"task_type must be one of: {opts}")
    return TYPE_MAP[raw]


TASK_TYPE_LABEL: dict[TaskType, str] = {
    TaskType.BUG: "Bug Fix",
    TaskType.VERIFY: "Verification",
    TaskType.FEATURE: "Feature",
    TaskType.CHORE: "Chore",
    TaskType.CONTENT: "Content",
    TaskType.REVIEW: "Review",
    TaskType.PUBLISH: "Publish",
    TaskType.INGEST: "Ingest",
}

# Keywords for auto-classification (checked against title + description, case-insensitive)
_BUG_KEYWORDS = (
    "bug",
    "fix",
    "broken",
    "crash",
    "error",
    "fail",
    "issue",
    "defect",
    "regression",
    "wrong",
    "incorrect",
    "not working",
    "doesn't work",
)
_VERIFY_KEYWORDS = (
    "verify",
    "check",
    "confirm",
    "test",
    "validate",
    "qa",
    "review",
    "ensure",
    "audit",
    "inspect",
)
_FEATURE_KEYWORDS = (
    "add",
    "new",
    "feature",
    "implement",
    "create",
    "build",
    "introduce",
    "support",
    "enable",
    "extend",
)


def auto_classify_type(title: str, description: str = "") -> TaskType:
    """Classify task type from title and description using keyword matching.

    Returns the best-match TaskType, defaulting to CHORE if ambiguous.
    """
    text = f"{title} {description}".lower()

    bug_score = sum(1 for kw in _BUG_KEYWORDS if kw in text)
    verify_score = sum(1 for kw in _VERIFY_KEYWORDS if kw in text)
    feature_score = sum(1 for kw in _FEATURE_KEYWORDS if kw in text)

    best = max(bug_score, verify_score, feature_score)
    if best == 0:
        return TaskType.CHORE

    # Require clear winner (no ties with another category)
    scores = [bug_score, verify_score, feature_score]
    if scores.count(best) > 1:
        return TaskType.CHORE

    if bug_score == best:
        return TaskType.BUG
    if verify_score == best:
        return TaskType.VERIFY
    return TaskType.FEATURE


def _decode_payload(part, *, strip_html: bool = False) -> str:
    """Decode a MIME part payload to a string."""
    import re as _re

    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        text = payload.decode(charset)
    except (UnicodeDecodeError, LookupError):
        text = payload.decode("latin-1", errors="replace")
    if strip_html:
        text = _re.sub(r"<[^>]+>", "", text).strip()
    return text


def parse_email(raw_bytes: bytes, *, filename: str = "") -> dict[str, Any]:
    """Parse a .eml or .msg file and extract subject, body, and attachments.

    Returns ``{"subject": str, "body": str, "attachments": [{"filename": str, "data": bytes}]}``.
    """
    if filename.lower().endswith(".msg") or _looks_like_msg(raw_bytes):
        return _parse_msg(raw_bytes)
    return _parse_eml(raw_bytes)


def _looks_like_msg(data: bytes) -> bool:
    """Check for OLE2 magic bytes (Outlook .msg files)."""
    return data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _parse_eml(raw_bytes: bytes) -> dict[str, Any]:
    """Parse an RFC 822 .eml file."""
    import email
    import email.policy

    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    subject = str(msg.get("subject", "")).strip()

    body = ""
    attachments: list[dict] = []

    if msg.is_multipart():
        for part in msg.walk():
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition:
                fname = part.get_filename() or "attachment"
                data = part.get_payload(decode=True) or b""
                attachments.append({"filename": fname, "data": data})
            elif part.get_content_type() == "text/plain" and not body:
                body = _decode_payload(part)
            elif part.get_content_type() == "text/html" and not body:
                body = _decode_payload(part, strip_html=True)
    else:
        is_html = msg.get_content_type() == "text/html"
        body = _decode_payload(msg, strip_html=is_html)

    message_id = str(msg.get("Message-ID", "")).strip()
    return {
        "subject": subject,
        "body": body.strip(),
        "attachments": attachments,
        "message_id": message_id,
    }


def _parse_msg(raw_bytes: bytes) -> dict[str, Any]:
    """Parse an Outlook .msg file using extract-msg."""
    import re as _re
    import tempfile

    try:
        import extract_msg
    except ImportError:
        _log.warning("extract-msg not installed — cannot parse .msg files")
        return {"subject": "", "body": "", "attachments": []}

    with tempfile.NamedTemporaryFile(suffix=".msg", delete=True) as tmp:
        tmp.write(raw_bytes)
        tmp.flush()
        msg = extract_msg.openMsg(tmp.name)

    subject = (msg.subject or "").strip()
    body = (msg.body or "").strip()
    if not body:
        html = msg.htmlBody
        if html:
            text = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
            body = _re.sub(r"<[^>]+>", " ", text).strip()
            body = _re.sub(r"\s+", " ", body).strip()

    attachments: list[dict] = []
    for att in msg.attachments or []:
        fname = getattr(att, "longFilename", None) or getattr(att, "shortFilename", "attachment")
        data = getattr(att, "data", b"") or b""
        if fname and data:
            attachments.append({"filename": fname, "data": data})

    message_id = getattr(msg, "messageId", "") or ""
    msg.close()
    return {"subject": subject, "body": body, "attachments": attachments, "message_id": message_id}


async def smart_title(description: str, max_len: int = 80) -> str:
    """Generate a concise task title using a headless LLM call.

    Uses the configured provider's headless command with a 15-second timeout.
    Falls back to :func:`auto_title` on any failure.
    """
    if not description or not description.strip():
        return ""
    truncated = description[:2000]  # limit prompt size
    prompt = (
        f"Generate a concise task title (max {max_len} chars) for this task. "
        f"Return ONLY the title, no quotes or extra text.\n\n{truncated}"
    )
    try:
        from swarm.providers import get_provider

        provider = get_provider()
        args = provider.headless_command(prompt, output_format="text", max_turns=1)
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace").strip()[:200]
            _log.warning("smart_title: LLM exited %d: %s", proc.returncode, err_msg)
            return auto_title(description, max_len)
        title = stdout.decode().strip().strip('"').strip("'").strip()
        if not title:
            _log.warning("smart_title: LLM returned empty output")
            return auto_title(description, max_len)
        # Truncate if too long
        if len(title) > max_len:
            title = title[: max_len - 1] + "\u2026"
        _log.debug("smart_title: generated %r", title)
        return title
    except TimeoutError:
        _log.warning("smart_title: LLM timed out after 15s")
    except FileNotFoundError:
        _log.warning("smart_title: LLM binary not found")
    except OSError as e:
        _log.warning("smart_title: OS error spawning LLM: %s", e)
    return auto_title(description, max_len)


def auto_title(description: str, max_len: int = 80) -> str:
    """Generate a title from the first line/sentence of a description.

    Returns the first non-empty line, truncated to *max_len* characters.
    Returns ``""`` when *description* is blank.
    """
    if not description or not description.strip():
        return ""
    first_line = description.strip().splitlines()[0].strip()
    if len(first_line) <= max_len:
        return first_line
    return first_line[: max_len - 1] + "\u2026"
