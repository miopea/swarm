"""PlaybookOps — recall, synthesis, and outcome attribution glue.

Extracted from :class:`~swarm.server.daemon.SwarmDaemon` (audit
finding #1, Phase 2 of ``docs/specs/daemon-god-object-refactor.md``).
Owns the four daemon methods that bridge the rest of the system to
:mod:`swarm.playbooks`:

* :meth:`fire_synthesis` — post-completion fire-and-forget into
  :class:`PlaybookSynthesizer`.
* :meth:`recall_for_task` — pre-dispatch ACTIVE-playbook query that
  feeds the prompt's "Relevant playbooks" block.
* :meth:`attribute_outcome` — verifier verdict → win/loss signal on
  every playbook applied to the task.
* :meth:`log_verifier_skip` — force-complete audit stamp (lives here
  because the verifier and the playbook lifecycle share the
  ``VerificationStatus`` enum).

The daemon keeps thin proxy shims so existing callers
(``daemon._fire_playbook_synthesis`` etc.) and tests don't change.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from swarm.drones.log import LogCategory, SystemAction
from swarm.logging import get_logger
from swarm.server.task_utils import log_task_exception as _log_task_exception

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.config import PlaybookConfig
    from swarm.db.playbook_store import PlaybookStore
    from swarm.drones.log import DroneLog
    from swarm.playbooks.synthesizer import PlaybookSynthesizer
    from swarm.tasks.board import TaskBoard
    from swarm.tasks.task import SwarmTask
    from swarm.worker.worker import Worker


_log = get_logger("server.playbook_ops")

# Max playbooks injected into a task dispatch — duplicated here from
# daemon.py's module constant for the same reason: scoping recall
# breadth without a deeper config knob.
_PLAYBOOK_RECALL_LIMIT = 3

# P3 learning preload: max prior-task learnings injected into a dispatch, and
# the minimum keyword overlap for a learning to count as relevant. Kept small
# per the context-engineering token budget ("just-in-time + some upfront").
_LEARNING_RECALL_LIMIT = 3
_LEARNING_MIN_OVERLAP = 2
# #1185: the recall block is injected into a worker's PTY as ONE PASTE, so it
# needs a CHARACTER bound and not only an item count — three entries of 10k
# chars is a 30k-char paste. Measured on the live board when this landed: real
# resolutions average 2077 chars, p90 3748, max 10539, so an uncapped block of
# three routinely ran to 6k+ and could reach 31k.
#
# Truncation cuts on a LINE boundary. The defect this fixes was a screen scrape
# that cut at the terminal pane width, leaving 26.6% of stored learnings
# starting mid-word; a cap that cut mid-line would reintroduce exactly that.
# Nothing is lost either way — the truncation marker names
# ``swarm_get_learnings``, the deliberate pull path, which stays uncapped.
_LEARNING_CHARS_PER_ITEM = 800
_LEARNING_BLOCK_CHARS = 2400
# Common >=4-char words that carry no topical signal — excluded from the
# keyword-overlap relevance so learnings aren't matched on boilerplate.
_LEARNING_STOPWORDS = frozenset(
    {
        "this",
        "that",
        "with",
        "from",
        "have",
        "will",
        "when",
        "then",
        "task",
        "code",
        "test",
        "tests",
        "into",
        "your",
        "should",
        "which",
        "there",
        "their",
        "using",
        "make",
        "need",
    }
)


class PlaybookOps:
    """Recall + attribution + synthesis glue around :class:`PlaybookStore`.

    Constructed once by the daemon and bound to its long-lived
    subsystems.  All methods are best-effort: an exception in
    synthesis or recall must never break the task lifecycle path that
    called them.
    """

    def __init__(
        self,
        *,
        get_store: Callable[[], PlaybookStore | None],
        get_synthesizer: Callable[[], PlaybookSynthesizer | None],
        get_config: Callable[[], PlaybookConfig],
        drone_log: DroneLog,
        task_board: TaskBoard | None,
        track_task: Callable[[asyncio.Task[object]], None],
        get_worker: Callable[[str], Worker | None],
    ) -> None:
        # Store + synthesizer + config come through getters so tests that
        # reassign ``daemon.playbook_store`` / ``daemon.playbook_synthesizer``
        # / ``daemon.config.playbooks`` post-construction still pick up the
        # new value — matches the ``get_pilot`` / ``get_worker_svc`` pattern.
        self._get_store = get_store
        self._get_synthesizer = get_synthesizer
        self._get_config = get_config
        self._drone_log = drone_log
        self._task_board = task_board
        self._track_task = track_task
        # #1185: no production method reads this any more — it existed for
        # ``consolidate_learnings``' PTY scrape, which is gone. Kept because
        # removing it is a 7-call-site change to daemon wiring and test
        # fixtures for no functional gain, and because the #1185 regression
        # test injects a worker here whose ``get_content`` raises, which is
        # what actively proves the scrape has not come back. Retire it in a
        # deliberate cleanup, not as a side effect of a bug fix.
        self._get_worker = get_worker

    def fire_synthesis(self, task: SwarmTask, resolution: str) -> None:
        """Schedule playbook synthesis for ``task`` as fire-and-forget.

        No-op without a running event loop (sync/CLI callers) or a wired
        synthesizer. ``PlaybookSynthesizer.synthesize`` never raises
        into the caller (it swallows everything but CancelledError),
        and the ``_log_task_exception`` callback catches anything stray
        — task completion must be unaffected by synthesis.
        """
        synth = self._get_synthesizer()
        if synth is None:
            return
        worker = task.assigned_worker or ""
        try:
            t = asyncio.create_task(synth.synthesize(task, worker=worker, resolution=resolution))
            t.add_done_callback(_log_task_exception)
            self._track_task(t)
        except RuntimeError:
            # No running event loop (sync/CLI context).
            return

    def recall_for_task(self, task: SwarmTask, worker_name: str) -> str:
        """Phase 2 recall-at-dispatch: a delimited block of the most
        relevant ACTIVE in-scope playbooks for this task ('' if none /
        disabled / store absent). Marks each as applied + buzz-logs.
        Best-effort — never raises into the dispatch path.
        """
        store = self._get_store()
        if store is None or not self._get_config().enabled:
            return ""
        try:
            from swarm.playbooks.models import PlaybookStatus

            query = f"{task.title} {task.description or ''}".strip()
            if not query:
                return ""
            repo = getattr(task, "repo", "") or getattr(task, "project", "")
            allowed = {"global", f"worker:{worker_name}"}
            if repo:
                allowed.add(f"project:{repo}")
            hits = store.search(
                query,
                scope=None,
                status=PlaybookStatus.ACTIVE,
                limit=_PLAYBOOK_RECALL_LIMIT * 3,
            )
            chosen = [pb for pb in hits if pb.scope in allowed][:_PLAYBOOK_RECALL_LIMIT]
            if not chosen:
                return ""
            lines = [
                "",
                "--- Relevant playbooks (vetted from past successful work — "
                "apply if they fit, cite if used) ---",
            ]
            for pb in chosen:
                lines.append(f"\n[{pb.name}] {pb.title}\nWhen: {pb.trigger}\n{pb.body}")
                try:
                    store.mark_applied(pb.id, task_id=task.id, worker=worker_name)
                except Exception:
                    _log.debug("playbook mark_applied failed for %s", pb.name, exc_info=True)
            lines.append("--- end playbooks ---")
            if self._drone_log is not None:
                self._drone_log.add(
                    SystemAction.PLAYBOOK_APPLIED,
                    worker_name,
                    f"#{task.number}: injected {len(chosen)} playbook(s)",
                    category=LogCategory.DRONE,
                )
            return "\n".join(lines)
        except Exception:
            _log.warning("playbook recall failed — dispatching without", exc_info=True)
            return ""

    @staticmethod
    def _clip_learning(text: str, limit: int = _LEARNING_CHARS_PER_ITEM) -> str:
        """Clip ``text`` to ``limit`` chars on a LINE boundary (#1185).

        Whole lines only: the defect this cap exists alongside was a PTY screen
        scrape that cut at the pane width, so a cap that cut mid-word would
        reproduce the same unreadable output from the other direction. A single
        line longer than the limit is kept whole rather than split — better one
        oversized line than a sentence severed mid-clause.

        Returns the text unchanged when it already fits, so short learnings
        carry no marker and read exactly as the author wrote them.
        """
        text = text.strip()
        if len(text) <= limit:
            return text
        kept: list[str] = []
        used = 0
        for line in text.splitlines():
            if kept and used + len(line) + 1 > limit:
                break
            kept.append(line)
            used += len(line) + 1
        return "\n".join(kept) + "\n… (truncated — swarm_get_learnings for the full text)"

    @staticmethod
    def _keywords(text: str) -> set[str]:
        """Significant (>=4-char, non-stopword) lowercase tokens for overlap."""
        return {
            w
            for w in re.findall(r"[a-z0-9_]+", text.lower())
            if len(w) >= 4 and w not in _LEARNING_STOPWORDS
        }

    def recall_learnings_for_task(self, task: SwarmTask) -> str:
        """P3 preload: a delimited block of the most relevant prior-task
        learnings for this task ('' if none). Server-side equivalent of what a
        worker would get from ``swarm_get_learnings``, but pushed into the
        dispatch so the worker starts with it. Relevance = keyword overlap
        between this task's title/description and each candidate's title +
        learnings. Best-effort — never raises into the dispatch path.
        """
        if self._task_board is None:
            return ""
        try:
            wanted = self._keywords(f"{task.title} {task.description or ''}")
            if not wanted:
                return ""
            scored: list[tuple[int, SwarmTask]] = []
            for other in self._task_board.all_tasks:
                if other.id == task.id or not (other.learnings or "").strip():
                    continue
                overlap = len(wanted & self._keywords(f"{other.title} {other.learnings}"))
                if overlap >= _LEARNING_MIN_OVERLAP:
                    scored.append((overlap, other))
            scored.sort(key=lambda pair: pair[0], reverse=True)
            chosen = [t for _score, t in scored[:_LEARNING_RECALL_LIMIT]]
            if not chosen:
                return ""
            lines = [
                "",
                "--- Relevant learnings from past tasks (apply if they fit) ---",
            ]
            # #1185: bound the block, not just the item count. Each entry is
            # clipped, and we stop adding once the running total is spent — a
            # dispatch is a single paste into a live PTY, so its size is a
            # feature of the message, not an afterthought.
            budget = _LEARNING_BLOCK_CHARS
            for t in chosen:
                entry = f"\n[#{t.number} {t.title}]\n{self._clip_learning(t.learnings)}"
                if lines[2:] and len(entry) > budget:
                    break
                lines.append(entry)
                budget -= len(entry)
            lines.append("--- end learnings ---")
            return "\n".join(lines)
        except Exception:
            _log.warning("learning recall failed — dispatching without", exc_info=True)
            return ""

    async def attribute_outcome(self, task: SwarmTask, status: object) -> None:
        """Phase 2 win/loss attribution, wired into the verifier's
        ``on_verdict`` hook. VERIFIED → win for every playbook applied
        to this task; REOPENED/ESCALATED → loss; SKIPPED/NOT_RUN → no
        signal. Then evaluate auto-promote / prune. Best-effort —
        never raises into the verification path.
        """
        store = self._get_store()
        if store is None:
            return
        try:
            from swarm.tasks.task import VerificationStatus

            if status == VerificationStatus.VERIFIED:
                win = True
            elif status in (VerificationStatus.REOPENED, VerificationStatus.ESCALATED):
                win = False
            else:
                return  # SKIPPED / NOT_RUN — no outcome signal
            applied = store.playbooks_applied_to_task(task.id)
            if not applied:
                return
            cfg = self._get_config()
            for pid in applied:
                store.record_outcome(pid, win, task_id=task.id)
                pb = store.get_by_id(pid)
                if pb is None:
                    continue
                verdict = store.evaluate_lifecycle(
                    pb.name,
                    promote_uses=cfg.auto_promote_uses,
                    promote_winrate=cfg.auto_promote_winrate,
                    prune_uses=cfg.prune_min_uses,
                    prune_winrate=cfg.prune_max_winrate,
                )
                if verdict and self._drone_log is not None:
                    action = (
                        SystemAction.PLAYBOOK_PROMOTED
                        if verdict == "promoted"
                        else SystemAction.PLAYBOOK_RETIRED
                    )
                    self._drone_log.add(
                        action,
                        task.assigned_worker or "",
                        f"{pb.name}: {verdict} (winrate={pb.winrate:.0%}, uses={pb.uses})",
                        category=LogCategory.DRONE,
                    )
        except Exception:
            _log.warning("playbook outcome attribution failed", exc_info=True)

    def log_verifier_skip(self, task: SwarmTask, *, actor: str) -> None:
        """Log a force-complete skip under LogCategory.VERIFIER."""
        from swarm.tasks.task import VerificationStatus

        task.verification_status = VerificationStatus.SKIPPED
        task.verification_reason = f"force-completed by {actor}"
        if self._task_board is not None:
            self._task_board.persist(task)
        if self._drone_log is not None:
            self._drone_log.add(
                SystemAction.VERIFIER_SKIPPED,
                task.assigned_worker or actor,
                f"#{task.number}: skipped — force-completed by {actor}",
                category=LogCategory.VERIFIER,
                metadata={"task_id": task.id, "task_number": task.number, "actor": actor},
            )

    def consolidate_learnings(self, task: SwarmTask) -> None:
        """Record the worker's resolution as the task's learnings.

        The resolution is the text a worker wrote for *this* audience —
        ``swarm_complete_task`` tells them so explicitly ("shown to future
        workers picking up similar tasks — write it for *them*"). It is
        therefore the right thing to recall, and until #1185 it was thrown
        away.

        WHAT THIS REPLACED, so nobody restores it: this method used to scrape
        the worker's PTY — ``get_content(30)``, strip CSI escapes, keep the
        last 15 non-blank lines — and store THAT as knowledge. A terminal
        screen is not a writeup. Measured across 951 stored learnings when the
        bug was found: 37.8% of all lines were Claude Code UI chrome, 96.5% of
        rows carried the footer tray, and 26.6% began mid-sentence because a
        screen capture cuts at the pane width. One row's "learnings" ended with
        the thinking indicator, a box rule, and *the operator's next prompt*.
        Those rows were then replayed verbatim into other workers' dispatches,
        which is how the bug surfaced — as a giant paste of somebody else's
        terminal arriving with a task.

        No resolution → no learnings. A force-complete carries none, and
        recording nothing is the honest outcome: recall skips the task rather
        than serving a screenful of chrome as if it were a finding.
        """
        if not task.assigned_worker:
            return
        task.learnings = (task.resolution or "").strip()
