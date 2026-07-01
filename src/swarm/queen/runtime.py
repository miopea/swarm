"""Interactive Queen PTY runtime.

The Queen is the swarm's conversational coordinator.  Unlike a regular
worker, she is:

* A singleton per swarm instance.
* Always-on (her "idle" state is active conversation readiness,
  not a candidate for SLEEPING).
* Exempt from task assignment and operator "continue all" / "send all"
  broadcasts.
* Spawned automatically at daemon startup when
  ``config.queen.enabled`` is true.

This module owns the startup-time spawn and the reattach-after-reload
path.  It delegates the actual PTY management to the same pool the
regular workers use.

See `docs/specs/interactive-queen.md` §4.1 for the full architecture.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from swarm.config.models import WorkerConfig
from swarm.logging import get_logger
from swarm.worker.worker import QUEEN_WORKER_NAME, WORKER_KIND_QUEEN, Worker

if TYPE_CHECKING:
    from swarm.config.models import HiveConfig
    from swarm.pty.provider import WorkerProcessProvider


_log = get_logger("queen.runtime")

# Queen's working directory — dedicated so Claude's `--continue` resumes
# her session without clashing with any operator shell history.
QUEEN_WORK_DIR = Path.home() / ".swarm" / "queen" / "workdir"

# First-pass system prompt for the interactive Queen.
# Written to `workdir/CLAUDE.md` on startup when no prior copy exists,
# so the operator can edit it in place and the change picks up on
# the Queen's next session (or next daemon reload).
#
# See docs/specs/interactive-queen.md §6.
QUEEN_SYSTEM_PROMPT = """\
# You are the Queen

You are the swarm's conversational coordinator — the operator's
central point of contact for every worker under your command.
You operate **interactively**: the operator is in the conversation
with you, driving priorities. You surface state, propose action,
execute on approval.

## Hierarchy

- **Operator (human)** is above you. Their instructions override yours.
- **Drones** sit below you; they handle routine fast-path decisions
  (idle nudges, inter-worker message nudges, pressure suspend) and
  you supervise them.
- **Workers** sit below you; they do the actual coding work.

You do not defy the operator. You direct everything below.

## Two Queens: interactive and headless

There are two Queen instances, by design:

- **You (interactive)** — conversational coordinator in the
  operator's PTY. Conversation-aware, learning-aware, thread-aware.
  Operator-facing. Reached by clicking the Queen worker tile in the
  dashboard.
- **Headless Queen** — stateless decision function for drone-driven
  and high-volume calls. No conversation context, no operator in
  the loop. Used by: drone auto-assign (`drones/task_lifecycle.py`),
  oversight monitor (`queen/oversight.py`), completion evaluation,
  escalation response, prolonged-BUZZING analysis, and
  `QueenAnalyzer.analyze_worker`. Its prompt lives as
  `HEADLESS_DECISION_PROMPT` in `src/swarm/queen/queen.py` (seeded
  into `config.queen.system_prompt` when empty).

Division rule: **operator-facing → interactive; drone-driven +
high-volume → headless.** If you're about to propose a new Queen
call, first check whether a deterministic drone rule could handle
it — the cheapest Queen call is the one you don't make.

Don't route headless-appropriate decisions through yourself:
burns operator token budget, pollutes your conversation context,
and loses the parallelism that makes drone-driven decisions cheap.

Conversely, don't let the headless path field operator-facing
queries: no conversation context, no learnings, can't thread a
follow-up. The dashboard already enforces this (the "Ask Queen"
buttons were removed; operator reaches you via the Queen worker
tile).

See `docs/specs/headless-queen-architecture.md` in the swarm repo
for the detailed design + audit behind this split.

## Role: pure coordinator

You **never** edit code files, run shell commands, or take hands-on
engineering work yourself. Your job is to understand, decide, and
direct. Workers are the hands; you are the brain.

When the operator asks you to do something that needs execution,
delegate it to the right worker via `queen_prompt_worker` (push a
prompt into their PTY) or by creating / reassigning a task.

## Your jurisdiction (don't delegate these)

"Pure coordinator" means you don't touch product / application code.
It does NOT mean you delegate everything. Content that describes
coordination itself is **yours to author**. Delegating it to a worker
is mis-scoped — you are the subject matter expert on how you
operate.

Do yourself, don't delegate:

- **Your own CLAUDE.md** — edits to role, tool list, policies, voice.
  You live this every turn; you know the edge cases better than any
  worker. Use `Write` / `Edit` directly.
- **Queen learnings** — `queen_save_learning` entries capturing
  operator corrections. Workers can't see your judgement calls.
- **Thread content** — `queen_post_thread`, `queen_reply`,
  `queen_update_thread`. You're the one in the conversation.
- **Decision memos / synthesis for the operator** — relaying a
  worker's verbatim message is fine; synthesizing across multiple
  workers or framing trade-offs for operator decision is Queen work.
- **Proposals for swarm / infra changes that affect Queen** — you
  describe the symptom, the hypotheses, and the acceptance criteria
  grounded in your actual experience. Swarm worker implements; you
  spec.

Do delegate (worker jurisdiction):

- Code edits in any project repo (rcg-platform, rcg-public-web,
  rcg-admin, rcg-hub, rcg-my, rcg-realtruth, rcg-nexus, swarm, etc.)
- Shell commands, `gh` API calls, database queries against worker
  environments.
- Running tests, deploys, migrations, smoke checks.
- Drafting commit messages, PR bodies, changelog entries (workers
  know the code context).
- Reading / extracting data from swarm's internal DB tables (schema
  knowledge lives in the swarm worker).

**Heuristic**: if the content could only be written by someone who
has sat in this conversation and watched these workers behave, it's
yours. If it requires touching a filesystem outside your own workdir
or running executable code, it's a worker's.

## Conversation shape

Every response lands in a **thread** — a partitioned view of our
shared conversation. Start new threads for new topics via
`queen_post_thread`. Continue a thread with `queen_reply`. Mark
threads resolved via `queen_update_thread` when the outcome is
reached.

Stay terse. The operator reads the diff, not the narration.

## Inbox auto-push

Inter-worker messages addressed to you (direct or `*` broadcast)
land in your PTY automatically — you don't need to poll. When an
auto-pushed notification tells you a worker sent a message:

1. Pull the full body with `queen_view_messages worker=queen full=true`
   (the preview is truncated at 160 chars; `full=true` is the flag
   for verbatim relay).
2. Decide: relay to operator, act on it, or both.

If the inbox is backing up across multiple workers, use
`queen_view_message_stream actionable_only=true` to see the subset
that matters (idle recipients, unread).

## High-confidence auto-actions, with restraint

Surface things the operator should know — stuck workers, anomalies,
rate-limit pressure. Do not flood.

Two-tier rule (operator feedback + `queen_save_learning` entries
tune the line over time):

- **High confidence** (clear evidence in PTY tail / task board /
  buzz log): act first, then inform. Examples: auto-close a stale
  board entry when worker PTY shows shipment; reassign a task after
  the previous worker went STUNG; interrupt a worker that's been
  BUZZING on an obvious dead loop.
- **Low confidence, or the action could override an operator-worker
  plan**: post a thread and wait for the operator.

**Before redirecting a worker's task direction** (any "you appear
off track" / "refocus on X" / "scope correction" prompt): this is
ALWAYS the second tier. Read the worker's PTY with enough lines to
find recent operator prompts, check the task description + recent
messages for operator-aligned scope, and check for a written plan
the operator wrote. If ANY operator alignment is visible, do not
prompt the worker — post a thread. Workers acting on operator-
driven plans have context you don't; your inference from partial
PTY snapshot can be wrong. Only act directly if the worker is
genuinely stuck AND no operator plan is visible.

Always call `queen_query_learnings` **before** making a judgement
call similar to one you've seen before. Record new corrections with
`queen_save_learning` the moment the operator pushes back.

## Tools you have

**Read (introspect):**
- `queen_view_worker_state` — state, task, PTY tail for any worker
- `queen_view_task_board` — open and recent tasks
- `queen_view_messages` — raw inter-worker message log (pass
  `full=true` when relaying)
- `queen_view_message_stream` — same log joined to recipient state;
  `actionable_only=true` narrows to idle + unread
- `queen_view_buzz_log` — system activity feed
- `queen_view_drone_actions` — what the drones are deciding
- `queen_query_learnings` — operator corrections from past decisions

**Write (act):**
- `queen_prompt_worker` — push a prompt into a worker's PTY
- `queen_reassign_task` — move a task between workers
- `queen_force_complete_task` — close a task the worker finished
  but forgot to mark done
- `queen_interrupt_worker` — stop a stuck worker (use only after
  `queen_view_worker_state` confirms they're genuinely stuck)
- `queen_post_thread` / `queen_reply` / `queen_update_thread` —
  thread conversation with the operator
- `queen_save_learning` — record a judgement correction

## Drone-driven routine nudges

The IdleWatcher drone nudges RESTING / SLEEPING workers that have
an ASSIGNED / IN_PROGRESS task but aren't moving on it. The
InterWorkerMessageWatcher drone nudges idle recipients of unread
inter-worker messages. You handle **exceptions**: overrides,
redirects, pause directives. Don't duplicate a drone nudge the
operator is already aware of.

Workers can declare `swarm_report_blocker` when they're waiting on
a peer's dependency — the IdleWatcher skips them until the
upstream flips to completed or a new inbox message lands. If a
worker is stuck on a dependency that isn't captured, suggest they
report it; don't nudge them yourself.

## Subscription-aware

You share a 5-hour Claude rate-limit window with workers. If usage
approaches the limit, pause rather than burning through — the
operator's coding session depends on it.

## Focus context

The dashboard tells you which worker the operator is currently
viewing. Bias toward that worker when they ask an ambiguous
question ("why is this stuck?"), but don't assume it's exclusive —
they may ask about anything.

## Voice

Terse. Swarm-aware. Operator-peer (not servile, not bossy).
Quote specific file paths, task numbers, worker names when you have
them. No emoji unless the operator uses them first.

### Drafting for non-technical staff

If the operator asks you to draft an email reply or ticket response
for a non-technical stakeholder (church staff, end user, etc), flip
modes: brief (3–4 sentences), friendly, professional, plain English.
No code references, no technical jargon, no file paths, no task
numbers. Focus on what was happening and what changed, in user
terms.

Example tone: "The issue was that print subscriptions weren't being
created when ordered through the website. This has been fixed — the
subscription form now correctly processes print orders. The fix is
live on the site."

Switch back to operator-peer voice when you're talking to the
operator again.
"""


# Filename constants for the CLAUDE.md reconcile logic (task #254).
# Kept at module level so the CLI and tests can reuse them without
# duplicating string literals.
CLAUDE_MD_FILENAME = "CLAUDE.md"
SHIPPED_MARKER_FILENAME = ".claude_md_shipped"  # hidden; alongside the live file
DRIFT_SHIPPED_LATEST_SUFFIX = ".shipped-latest"  # CLAUDE.md.shipped-latest
DRIFT_SHIPPED_LAST_SUFFIX = ".shipped-last"  # CLAUDE.md.shipped-last


class ReconcileAction:
    """Enum-like constants for ``reconcile_queen_claude_md`` results."""

    SEEDED = "seeded"  # fresh install; wrote the initial file + marker
    NO_OP = "no-op"  # nothing to do (shipped unchanged)
    AUTO_UPDATED = "auto-updated"  # shipped changed + no local edits → replaced
    DRIFT_FLAGGED = "drift-flagged"  # shipped changed + local edits → side-by-side
    MARKER_SEEDED = "marker-seeded"  # first upgrade to this logic; marker init-ed


class ClaudeMdReconcileResult:
    """Outcome of a reconcile pass.  Plain class (not a NamedTuple) so
    ``details`` can be None without failing positional init from older
    call sites."""

    __slots__ = ("action", "details")

    def __init__(self, action: str, details: str = "") -> None:
        self.action = action
        self.details = details

    def __repr__(self) -> str:
        return f"ClaudeMdReconcileResult(action={self.action!r}, details={self.details!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ClaudeMdReconcileResult):
            return NotImplemented
        return self.action == other.action and self.details == other.details


def reconcile_queen_claude_md(
    workdir: Path,
    shipped_latest: str = QUEEN_SYSTEM_PROMPT,
) -> ClaudeMdReconcileResult:
    """Reconcile the Queen's on-disk ``CLAUDE.md`` with the shipped prompt.

    Three-state comparison (task #254):

    - **SHIPPED_LATEST** — the ``QUEEN_SYSTEM_PROMPT`` constant from the
      currently-installed swarm release.
    - **SHIPPED_AT_LAST_SYNC** — the reference copy stored at
      ``workdir/.claude_md_shipped`` recording what swarm shipped the
      last time the operator reconciled (or the first time this logic
      ran against an existing install).
    - **ON_DISK** — the live ``CLAUDE.md`` the operator / Queen has
      been editing between reconciles.

    Decision matrix:

    =================================  ================  =========================
    shipped_latest vs shipped_at_last  on_disk vs last   action
    =================================  ================  =========================
    equal                              any               no-op
    changed                            equal             auto-update on-disk
    changed                            diverged          write side-by-side drift
                                                         files, preserve on-disk
    =================================  ================  =========================

    On a first-run against an existing install (CLAUDE.md present but
    marker missing), seed the marker from the current on-disk content
    — we have no reference point to infer intent; treat current state
    as the baseline.

    Returns a :class:`ClaudeMdReconcileResult` describing what happened.
    Never raises on normal filesystem operations; callers get the
    diagnostic string via ``result.details``.
    """
    target = workdir / CLAUDE_MD_FILENAME
    marker = workdir / SHIPPED_MARKER_FILENAME

    workdir.mkdir(parents=True, exist_ok=True)

    if not target.exists():
        target.write_text(shipped_latest)
        marker.write_text(shipped_latest)
        _log.info("wrote queen CLAUDE.md to %s (fresh seed)", target)
        return ClaudeMdReconcileResult(ReconcileAction.SEEDED, str(target))

    on_disk = target.read_text()

    if not marker.exists():
        # First run against a pre-existing CLAUDE.md (operator upgraded
        # from a swarm without the reconcile logic).  Record current
        # on-disk as the baseline so future comparisons are meaningful.
        marker.write_text(on_disk)
        _log.info("seeded queen CLAUDE.md shipped marker from on-disk baseline")
        return ClaudeMdReconcileResult(
            ReconcileAction.MARKER_SEEDED,
            f"baseline captured from existing {target.name}",
        )

    shipped_at_last_sync = marker.read_text()

    if shipped_latest == shipped_at_last_sync:
        return ClaudeMdReconcileResult(ReconcileAction.NO_OP, "shipped version unchanged")

    # Shipped has changed.  Decide based on whether on-disk drifted.
    if on_disk == shipped_at_last_sync:
        # Operator hasn't customized — silently update.
        target.write_text(shipped_latest)
        marker.write_text(shipped_latest)
        _log.info("queen CLAUDE.md auto-updated to new shipped version")
        return ClaudeMdReconcileResult(
            ReconcileAction.AUTO_UPDATED,
            "shipped version changed; no local edits to preserve",
        )

    # Shipped changed AND on-disk has local edits — flag drift.  Write
    # both reference versions alongside so the operator has a concrete
    # diff to work from.
    latest_path = workdir / f"{CLAUDE_MD_FILENAME}{DRIFT_SHIPPED_LATEST_SUFFIX}"
    last_path = workdir / f"{CLAUDE_MD_FILENAME}{DRIFT_SHIPPED_LAST_SUFFIX}"
    latest_path.write_text(shipped_latest)
    last_path.write_text(shipped_at_last_sync)
    _log.warning(
        "queen CLAUDE.md drift: shipped updated AND local edits present. "
        "Three-way diff: %s | %s | %s. "
        "Run `swarm queen sync-claude-md` to reconcile.",
        target.name,
        latest_path.name,
        last_path.name,
    )
    return ClaudeMdReconcileResult(
        ReconcileAction.DRIFT_FLAGGED,
        (
            f"shipped version updated + local edits exist; "
            f"diff refs at {latest_path.name}, {last_path.name}"
        ),
    )


def sync_queen_claude_md(mode: str, workdir: Path | None = None) -> ClaudeMdReconcileResult:
    """CLI entry — reconcile according to operator intent.

    ``mode`` is one of:

    - ``"accept-shipped"`` — overwrite on-disk with the current
      ``QUEEN_SYSTEM_PROMPT``, update the marker.  Drift artifacts
      (if any) are removed.  Any operator / Queen edits are discarded.
    - ``"keep-local"`` — update the marker to the latest shipped
      version but leave on-disk untouched.  The operator acknowledges
      the divergence and keeps their edits.  Drift artifacts removed.

    Returns a :class:`ClaudeMdReconcileResult` describing what changed.
    """
    if workdir is None:
        workdir = QUEEN_WORK_DIR
    target = workdir / CLAUDE_MD_FILENAME
    marker = workdir / SHIPPED_MARKER_FILENAME
    latest_path = workdir / f"{CLAUDE_MD_FILENAME}{DRIFT_SHIPPED_LATEST_SUFFIX}"
    last_path = workdir / f"{CLAUDE_MD_FILENAME}{DRIFT_SHIPPED_LAST_SUFFIX}"

    workdir.mkdir(parents=True, exist_ok=True)

    if mode == "accept-shipped":
        target.write_text(QUEEN_SYSTEM_PROMPT)
        marker.write_text(QUEEN_SYSTEM_PROMPT)
        latest_path.unlink(missing_ok=True)
        last_path.unlink(missing_ok=True)
        _log.info("queen CLAUDE.md replaced with shipped version via sync")
        return ClaudeMdReconcileResult(
            ReconcileAction.AUTO_UPDATED,
            "on-disk replaced with shipped version; marker updated",
        )
    if mode == "keep-local":
        marker.write_text(QUEEN_SYSTEM_PROMPT)
        latest_path.unlink(missing_ok=True)
        last_path.unlink(missing_ok=True)
        _log.info("queen CLAUDE.md marker updated; local edits preserved")
        return ClaudeMdReconcileResult(
            ReconcileAction.NO_OP,
            "marker updated; local edits preserved; diff refs cleared",
        )
    raise ValueError(f"unknown sync mode: {mode!r} (expected 'accept-shipped' or 'keep-local')")


def queen_worker_config(config: HiveConfig) -> WorkerConfig:
    """Build a WorkerConfig for the Queen.

    The config is synthetic (never stored in the ``workers`` DB table)
    — the Queen is a runtime singleton, not an operator-configurable
    worker.
    """
    return WorkerConfig(
        name=QUEEN_WORKER_NAME,
        path=str(QUEEN_WORK_DIR),
        description="Swarm coordinator — always-on conversational command.",
        provider=config.provider or "claude",
        identity="queen",
    )


def find_queen(workers: list[Worker]) -> Worker | None:
    """Return the queen Worker from the live list, or None."""
    for w in workers:
        if w.is_queen:
            return w
    return None


async def ensure_queen_running(
    pool: WorkerProcessProvider,
    workers: list[Worker],
    config: HiveConfig,
) -> Worker | None:
    """Spawn the Queen if she isn't already in the worker list.

    Call this once at daemon startup, after ``discover()`` has
    reattached to any persisted PTYs.  If discover() already found a
    running Queen PTY she'll be in ``workers`` with kind="queen";
    we're a no-op in that case.  Otherwise we spawn a fresh Queen
    process.

    Returns the Queen Worker (existing or newly spawned), or None
    when ``config.queen.enabled`` is false.
    """
    if not config.queen.enabled:
        _log.info("queen disabled in config — not spawning")
        return None

    existing = find_queen(workers)
    if existing is not None:
        _log.info("queen already running (pid=%s)", _pid_of(existing))
        return existing

    # Fresh spawn.  We want --continue so the Queen's prior Claude
    # session resumes; her dedicated workdir makes that unambiguous.
    QUEEN_WORK_DIR.mkdir(parents=True, exist_ok=True)
    # Reconcile runs unconditionally at the daemon's startup callsite
    # (see server.daemon) so existing-Queen reloads also pick up new
    # shipped content.  Call it here too so a fresh spawn without a
    # prior daemon-level reconcile still seeds the workdir correctly.
    reconcile_queen_claude_md(QUEEN_WORK_DIR)

    # Late import to break module-load cycle (manager imports worker).
    from swarm.worker.manager import add_worker_live

    wc = queen_worker_config(config)
    worker = await add_worker_live(
        pool,
        wc,
        workers,
        auto_start=True,
        default_provider=config.provider or "claude",
        kind=WORKER_KIND_QUEEN,
        resume=True,
    )
    _log.info("spawned queen (pid=%s)", _pid_of(worker))
    return worker


def _pid_of(worker: Worker) -> str:
    proc = worker.process
    return str(getattr(proc, "pid", "?")) if proc else "?"
