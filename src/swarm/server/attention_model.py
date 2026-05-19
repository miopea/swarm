"""Exception-queue classifier for the Attention panel.

The Attention panel used to be the operator's *coordinator feed* — every
worker→Queen message, every worker idle >15s, recency-sorted, with bare
Reply/Dismiss buttons. Now the Queen coordinates the swarm, so most of that
feed is already being handled and the panel reads as noise.

This module reframes it as an **exception queue**: an item is surfaced only
when it is genuinely escalated to a human or is a hard failure the autonomous
layers can't resolve. Everything the Queen/drones are actively handling is
demoted to a collapsed "Queen is handling" drawer.

``classify()`` is a **pure function** — it takes plain snapshots (no daemon,
no DB, no clock side effects) so the policy is unit-testable in isolation.
The route handler in ``server/routes/attention.py`` gathers the snapshots
from the live stores and calls this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ----------------------------------------------------------------------
# Tunables (route injects from DroneConfig where applicable)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class AttentionConfig:
    # A waiting worker nudged within this window is being handled → drawer.
    nudge_window_seconds: float = 900.0
    # Pending proposals younger than this are still being evaluated by the
    # autonomous layer (drone rules / headless Queen) → drawer.
    proposal_autonomous_window_seconds: float = 180.0
    # A worker-message thread is "in flight" only if it was touched within
    # this window OR the Queen is actively BUSY. Older + idle Queen ⇒ she
    # already dealt with it (threads aren't auto-resolved) ⇒ drop it.
    worker_message_fresh_seconds: float = 600.0
    # A DECISION item unresolved past this auto-promotes to CRITICAL — this
    # is the fix for "a stale proposal looks like a fresh crash".
    stale_promote_seconds: float = 1800.0
    # >= this many REVIVED in the crash-loop window → crash-loop CRITICAL.
    crash_loop_min: int = 3


# ----------------------------------------------------------------------
# Inputs (plain snapshots — keep classify() pure)
# ----------------------------------------------------------------------


@dataclass
class ThreadSnap:
    id: str
    kind: str
    title: str
    worker_name: str | None
    task_id: str | None
    created_at: float
    updated_at: float
    latest_message: str | None = None


@dataclass
class ProposalSnap:
    id: str
    proposal_type: str
    worker_name: str
    task_id: str | None
    task_title: str
    reasoning: str
    assessment: str
    confidence: float
    is_plan: bool
    created_at: float


@dataclass
class WorkerSnap:
    name: str
    state: str  # WorkerState.value: "WAITING" | "STUNG" | "RESTING" | ...
    state_duration: float
    needs_operator_input: bool
    in_revive_grace: bool
    task_id: str | None = None
    waiting_excerpt: str | None = None
    revive_count: int = 0  # REVIVED entries in the crash-loop window
    last_stung_detail: str | None = None


# ----------------------------------------------------------------------
# Outputs
# ----------------------------------------------------------------------

SEVERITY_CRITICAL = "critical"
SEVERITY_DECISION = "decision"


@dataclass
class ExceptionItem:
    id: str
    ref_id: str  # thread_id | proposal_id | worker name
    kind: str
    severity: str
    title: str
    detail: str
    worker_name: str | None
    task_id: str | None
    age_seconds: float
    updated_at: float
    actions: list[str] = field(default_factory=list)
    # For worker-waiting on a choice prompt: the worker's own options so
    # the operator answers inline instead of opening a terminal. Each is
    # {"value": <keystroke to send>, "label": <display text>}.
    options: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ref_id": self.ref_id,
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "worker_name": self.worker_name,
            "task_id": self.task_id,
            "age_seconds": round(self.age_seconds, 1),
            "updated_at": self.updated_at,
            "actions": list(self.actions),
            "options": [dict(o) for o in self.options],
        }


@dataclass
class HandledItem:
    worker_name: str | None
    kind: str
    title: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_name": self.worker_name,
            "kind": self.kind,
            "title": self.title,
            "reason": self.reason,
        }


@dataclass
class AttentionView:
    critical: list[ExceptionItem] = field(default_factory=list)
    decision: list[ExceptionItem] = field(default_factory=list)
    handled: list[HandledItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "critical": [i.to_dict() for i in self.critical],
            "decision": [i.to_dict() for i in self.decision],
            "handled": {
                "count": len(self.handled),
                "items": [i.to_dict() for i in self.handled],
            },
        }


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _clip(text: str | None, n: int = 200) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _human_age(seconds: float) -> str:
    s = max(0.0, seconds)
    if s < 90:
        return f"{int(s)}s"
    if s < 5400:
        return f"{int(s / 60)}m"
    if s < 172800:
        return f"{int(s / 3600)}h"
    return f"{int(s / 86400)}d"


# Choice-prompt option lines, matching the provider's own detection
# (swarm.providers.claude `_RE_CURSOR_OPTION` / `_RE_OTHER_OPTION`): a
# focused option carries a `>`/`❯` cursor; the rest are plain numbered
# lines. We require BOTH a cursor option and another option before
# treating the text as a real menu, so coincidental "1." numbering in
# prose doesn't sprout fake buttons.
_RE_CHOICE_CURSOR = re.compile(r"^[^\S\n]*[>❯][^\S\n]*(\d+)\.[^\S\n]*(.+?)[^\S\n]*$", re.MULTILINE)
_RE_CHOICE_PLAIN = re.compile(r"^[^\S\n]+(\d+)\.[^\S\n]*(.+?)[^\S\n]*$", re.MULTILINE)


def extract_choice_options(text: str | None) -> list[dict[str, str]]:
    """Parse a worker's WAITING choice prompt into selectable options.

    Returns ``[{"value": "<digit to send>", "label": "<text>"}, ...]``
    ordered by option number, or ``[]`` when the text isn't a real
    numbered choice menu (free-form question, plain prompt, prose).
    Pure — regex over the captured PTY tail, no provider import.
    """
    if not text:
        return []
    cursor = _RE_CHOICE_CURSOR.findall(text)
    plain = _RE_CHOICE_PLAIN.findall(text)
    if not cursor or not plain:
        return []
    by_num: dict[str, str] = {}
    for num, label in [*plain, *cursor]:  # cursor wins on dup number
        by_num[num] = " ".join(label.split())[:60]
    return [{"value": n, "label": by_num[n]} for n in sorted(by_num, key=int)]


# Thread kinds that are operator-review intent when present (Queen-authored).
_QUEEN_REVIEW_KINDS = ("queen-escalation", "oversight", "escalation", "anomaly", "proposal")


# ----------------------------------------------------------------------
# Classifier (pure)
# ----------------------------------------------------------------------


def classify(
    *,
    threads: list[ThreadSnap],
    proposals: list[ProposalSnap],
    workers: list[WorkerSnap],
    nudged_workers: set[str],
    blocked_workers: set[str],
    resource_snapshot: dict[str, Any] | None,
    now: float,
    queen_busy: bool = False,
    cfg: AttentionConfig | None = None,
) -> AttentionView:
    """Partition all candidate signals into critical / decision / handled.

    Pure: no I/O, no wall-clock reads — ``now`` and every store read is
    passed in. Sorting within a tier is oldest-first so nothing rots.
    """
    cfg = cfg or AttentionConfig()
    view = AttentionView()
    _classify_threads(view, threads, now, queen_busy, cfg)
    _classify_proposals(view, proposals, now, cfg)
    _classify_workers(view, workers, nudged_workers, blocked_workers, now, cfg)
    _classify_resources(view, resource_snapshot or {}, now)
    _apply_stale_promotion(view, cfg)

    # Oldest-first within each tier so nothing rots at the bottom.
    view.critical.sort(key=lambda i: -i.age_seconds)
    view.decision.sort(key=lambda i: -i.age_seconds)
    return view


def _classify_threads(
    view: AttentionView,
    threads: list[ThreadSnap],
    now: float,
    queen_busy: bool,
    cfg: AttentionConfig,
) -> None:
    for t in threads:
        if t.kind == "operator":
            continue  # Ask-Queen conversation, never an attention item
        if t.kind == "worker-message":
            # Relayed into the Queen's PTY (#235). Threads aren't
            # auto-resolved, so an old one with an idle Queen means she
            # already dealt with it — drop it rather than imply she's
            # still working. Only surface what's plausibly in flight.
            fresh = (now - t.updated_at) < cfg.worker_message_fresh_seconds
            if not (queen_busy or fresh):
                continue
            reason = "with the Queen now" if queen_busy else "relayed — awaiting her next turn"
            view.handled.append(
                HandledItem(
                    worker_name=t.worker_name,
                    kind=t.kind,
                    title=t.title or "(worker message)",
                    reason=reason,
                )
            )
            continue
        if t.kind in _QUEEN_REVIEW_KINDS:
            view.decision.append(
                ExceptionItem(
                    id=f"thread:{t.id}",
                    ref_id=t.id,
                    kind="queen-escalation",
                    severity=SEVERITY_DECISION,
                    title=t.title or "Queen escalation",
                    detail=_clip(t.latest_message) or "Queen flagged this for your review.",
                    worker_name=t.worker_name,
                    task_id=t.task_id,
                    age_seconds=max(0.0, now - t.created_at),
                    updated_at=t.updated_at,
                    actions=["reply", "focus", "dismiss"],
                )
            )


def _classify_proposals(
    view: AttentionView,
    proposals: list[ProposalSnap],
    now: float,
    cfg: AttentionConfig,
) -> None:
    for p in proposals:
        age = max(0.0, now - p.created_at)
        if age < cfg.proposal_autonomous_window_seconds:
            view.handled.append(
                HandledItem(
                    worker_name=p.worker_name,
                    kind="proposal",
                    title=_proposal_title(p),
                    reason=f"drones evaluating ({_human_age(age)})",
                )
            )
            continue
        why = p.reasoning or p.assessment
        detail = f"conf {p.confidence:.2f} · pending {_human_age(age)}"
        if why:
            detail += " · " + _clip(why, 160)
        view.decision.append(
            ExceptionItem(
                id=f"proposal:{p.id}",
                ref_id=p.id,
                kind="proposal",
                severity=SEVERITY_DECISION,
                title=_proposal_title(p),
                detail=detail,
                worker_name=p.worker_name,
                task_id=p.task_id or None,
                age_seconds=age,
                updated_at=p.created_at,
                actions=["approve", "reject", "focus"],
            )
        )


def _classify_workers(
    view: AttentionView,
    workers: list[WorkerSnap],
    nudged_workers: set[str],
    blocked_workers: set[str],
    now: float,
    cfg: AttentionConfig,
) -> None:
    for w in workers:
        if w.state == "STUNG":
            _classify_stung(view, w, now, cfg)
        elif w.needs_operator_input:
            _classify_waiting(view, w, nudged_workers, blocked_workers, now)


def _classify_stung(view: AttentionView, w: WorkerSnap, now: float, cfg: AttentionConfig) -> None:
    if w.in_revive_grace:
        view.handled.append(
            HandledItem(
                worker_name=w.name,
                kind="worker-stung",
                title=f"{w.name} crashed",
                reason="reviving…",
            )
        )
        return
    if w.revive_count >= cfg.crash_loop_min:
        title = (
            f"{w.name}: crash loop — revived {w.revive_count}× in {_human_age(w.state_duration)}"
        )
    else:
        title = f"{w.name} crashed — needs revive"
    detail = _clip(w.last_stung_detail) or (
        "Worker process exited; autonomous revive did not recover it."
    )
    view.critical.append(
        ExceptionItem(
            id=f"worker:{w.name}",
            ref_id=w.name,
            kind="worker-stung",
            severity=SEVERITY_CRITICAL,
            title=title,
            detail=detail,
            worker_name=w.name,
            task_id=w.task_id,
            age_seconds=w.state_duration,
            updated_at=now - w.state_duration,
            actions=["revive", "focus"],
        )
    )


def _classify_waiting(
    view: AttentionView,
    w: WorkerSnap,
    nudged_workers: set[str],
    blocked_workers: set[str],
    now: float,
) -> None:
    if w.name in blocked_workers:
        view.handled.append(
            HandledItem(
                worker_name=w.name,
                kind="worker-waiting",
                title=f"{w.name} is waiting",
                reason="blocked by a reported dependency",
            )
        )
        return
    if w.name in nudged_workers:
        view.handled.append(
            HandledItem(
                worker_name=w.name,
                kind="worker-waiting",
                title=f"{w.name} is waiting",
                reason="Queen/idle-watcher nudging",
            )
        )
        return
    detail = _clip(w.waiting_excerpt) or (
        f"Waiting {_human_age(w.state_duration)} · no autonomous nudge — needs your input."
    )
    # If the worker is on a numbered choice menu, surface its own
    # options as buttons so the operator answers inline instead of
    # opening a terminal to type the pick.
    options = extract_choice_options(w.waiting_excerpt)
    view.decision.append(
        ExceptionItem(
            id=f"worker:{w.name}",
            ref_id=w.name,
            kind="worker-waiting",
            severity=SEVERITY_DECISION,
            title=f"{w.name} is waiting for your input",
            detail=detail,
            worker_name=w.name,
            task_id=w.task_id,
            age_seconds=w.state_duration,
            updated_at=now - w.state_duration,
            actions=["focus", "force_rest"],
            options=options,
        )
    )


def _classify_resources(view: AttentionView, snap: dict[str, Any], now: float) -> None:
    if str(snap.get("pressure_level") or "").lower() == "critical":
        mem = snap.get("mem_percent")
        swp = snap.get("swap_percent")
        view.critical.append(
            ExceptionItem(
                id="resource:pressure",
                ref_id="resource:pressure",
                kind="resource",
                severity=SEVERITY_CRITICAL,
                title="System memory pressure CRITICAL",
                detail=f"mem {mem}% · swap {swp}% — workers may be suspended.",
                worker_name=None,
                task_id=None,
                age_seconds=0.0,
                updated_at=now,
                actions=["resources"],
            )
        )
    dstate = snap.get("dstate_pids") or {}
    if dstate:
        view.critical.append(
            ExceptionItem(
                id="resource:dstate",
                ref_id="resource:dstate",
                kind="resource",
                severity=SEVERITY_CRITICAL,
                title=f"{len(dstate)} process(es) stuck in uninterruptible sleep",
                detail="D-state processes block the scheduler; investigate disk/IO.",
                worker_name=None,
                task_id=None,
                age_seconds=0.0,
                updated_at=now,
                actions=["resources"],
            )
        )


def _apply_stale_promotion(view: AttentionView, cfg: AttentionConfig) -> None:
    """Age-escalation: a DECISION item unresolved past the staleness
    threshold becomes CRITICAL — the fix for "a stale proposal looks
    the same as a fresh crash"."""
    still_decision: list[ExceptionItem] = []
    for item in view.decision:
        if item.age_seconds > cfg.stale_promote_seconds:
            item.severity = SEVERITY_CRITICAL
            item.detail = f"STALE {_human_age(item.age_seconds)} — needs you · " + item.detail
            view.critical.append(item)
        else:
            still_decision.append(item)
    view.decision = still_decision


def _proposal_title(p: ProposalSnap) -> str:
    label = (p.proposal_type or "proposal").title()
    if p.task_title:
        return f"{label}: {p.task_title} → {p.worker_name}"
    return f"{label} for {p.worker_name}"
