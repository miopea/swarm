"""Discover a Jira project's workflow and propose a status map (v2 phase 2).

WHY DISCOVERY EXISTS. The status map was hardcoded — ``done -> "Done"`` — and on
2026-08-07 that was refused by 11 real tickets whose workflow offered only
``Waiting for support``. The failure was invisible until it happened, then repeated
every sync interval. A map typed once is also the artefact that silently rots when a
Jira admin edits a workflow.

WHY THE PROPOSAL IS SEPARATE FROM THE CONFIRMATION. ``Done`` / ``Resolved`` / ``Closed``
are rarely interchangeable, and picking the wrong one writes to real tickets. So this
module PROPOSES and the operator confirms; nothing here decides on its own.

DELIBERATELY PURE. Discovery talks to Jira; this does not. The heuristic that decides
"which of this project's statuses means done?" is the part most likely to be wrong, and
it should be testable against a recorded workflow without a network, a token or a
sample ticket.
"""

from __future__ import annotations

from typing import Any

# Jira's statusCategory key is universal across every workflow: "new" (To Do),
# "indeterminate" (In Progress), "done" (Done). Names are per-project and can be
# anything; categories cannot. So the category is the reliable signal and the name is
# only a tie-breaker.
_CATEGORY_NEW = "new"
_CATEGORY_IN_PROGRESS = "indeterminate"
_CATEGORY_DONE = "done"

# What each Swarm status wants from the target workflow, in order of preference:
# (statusCategory, name hints). The hints break ties WITHIN a category — they never
# override it, because a status literally named "Done" that sits in the To Do category
# would otherwise be chosen to mean finished.
_INTENT: dict[str, tuple[str, tuple[str, ...]]] = {
    "backlog": (_CATEGORY_NEW, ("backlog", "to do", "open", "new")),
    "unassigned": (_CATEGORY_NEW, ("to do", "open", "backlog", "new")),
    "assigned": (_CATEGORY_NEW, ("to do", "open", "selected", "ready")),
    "active": (_CATEGORY_IN_PROGRESS, ("in progress", "in development", "doing")),
    "blocked": (_CATEGORY_IN_PROGRESS, ("blocked", "waiting", "on hold", "impediment")),
    "done": (_CATEGORY_DONE, ("done", "closed", "resolved", "complete")),
    "failed": (_CATEGORY_DONE, ("won't do", "cancelled", "canceled", "rejected", "closed")),
}


def flatten_statuses(payload: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Reduce ``/project/{key}/statuses`` to a de-duplicated list of statuses.

    The endpoint returns one entry per ISSUE TYPE, each carrying its own status list,
    so the same status appears many times. Callers want the project's vocabulary, not
    the per-type breakdown.
    """
    seen: dict[str, dict[str, str]] = {}
    for issue_type in payload or []:
        for status in issue_type.get("statuses", []) or []:
            name = str(status.get("name", "")).strip()
            if not name:
                continue
            category = str((status.get("statusCategory") or {}).get("key", "")).strip().lower()
            # First occurrence wins; the same name cannot hold two categories in one
            # project, and taking the first keeps the result stable across calls.
            seen.setdefault(name.lower(), {"name": name, "category": category})
    return sorted(seen.values(), key=lambda s: s["name"].lower())


def propose_status_map(statuses: list[dict[str, str]]) -> dict[str, str]:
    """Propose ``swarm status -> jira status name`` from a project's real vocabulary.

    Returns only what it can justify: a Swarm status with no plausible target is LEFT
    OUT rather than pointed at a guess. An absent mapping means "we do not know", which
    the export path can refuse honestly; a wrong mapping transitions someone's ticket
    to the wrong state and looks successful.
    """
    by_category: dict[str, list[dict[str, str]]] = {}
    for status in statuses:
        by_category.setdefault(status.get("category", ""), []).append(status)

    proposal: dict[str, str] = {}
    for swarm_status, (category, hints) in _INTENT.items():
        candidates = by_category.get(category, [])
        if not candidates:
            continue
        chosen = _best_match(candidates, hints)
        if chosen:
            proposal[swarm_status] = chosen
    return proposal


def _normalise(value: str) -> str:
    """Lowercase and strip non-alphanumerics, so "ToDo" and "To Do" compare equal.

    Found against the operator's real IS project: the hint "to do" failed to match a
    status literally named "ToDo" purely because of the space, and the mapping fell
    through to a much worse candidate.
    """
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _best_match(candidates: list[dict[str, str]], hints: tuple[str, ...]) -> str:
    """Pick a status name from one category, preferring the earliest hint.

    THREE TIERS, in order, and the middle one is why this is not a plain substring
    search. Against the operator's real IS workflow — Canceled, Done, In Progress,
    Reopened, Resolved, ToDo, Waiting for customer, Waiting for support, Waiting On,
    Work in progress — a substring match sent backlog, assigned and unassigned all to
    "Reopened", because the hint "open" appears inside "Re-open-ed". Whole-word
    matching rejects that and lets "ToDo" win instead.

    Falls back to the single candidate when a category has exactly one status: that is
    not a guess, it is the only option. With several candidates and no match, returns
    "" so the caller leaves the mapping unset rather than picking arbitrarily.
    """
    normalised = [(c["name"], _normalise(c["name"])) for c in candidates]

    # 1. exact, ignoring spacing and punctuation ("ToDo" == "to do")
    for hint in hints:
        target = _normalise(hint)
        for original, norm in normalised:
            if norm == target:
                return original

    # 2. the hint appears as a WHOLE WORD in the status name. "waiting" matches
    #    "Waiting for customer"; "open" does NOT match "Reopened".
    for hint in hints:
        hint_words = hint.lower().split()
        for original, _norm in normalised:
            words = [w.strip("-_/") for w in original.lower().split()]
            if all(hw in words for hw in hint_words):
                return original

    if len(candidates) == 1:
        return candidates[0]["name"]
    return ""


def terminal_status_names(statuses: list[dict[str, str]]) -> list[str]:
    """Status names in the DONE category — the project's terminal vocabulary.

    Imports already exclude finished work with ``statusCategory != Done``, which needs
    no discovery. This is for the operator's confirmation screen, so "which of these
    count as finished?" is answered from the project rather than assumed.
    """
    return [s["name"] for s in statuses if s.get("category") == _CATEGORY_DONE]
