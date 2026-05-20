"""Schedule expression helpers — normalize, humanize, preview next firings.

Three responsibilities, intentionally separated from the engine so the
pipeline editor's preview endpoint can call them without dragging in the
full pipeline lifecycle surface:

* ``normalize_schedule`` — collapses the legacy ``HH:MM`` shorthand into a
  5-field cron expression so downstream code never has to special-case it.
* ``humanize_schedule`` — best-effort one-line plain-English description
  of common patterns, with a "Custom: <expr>" fallback for anything off
  the beaten path. This is intentionally not a full cron-to-prose
  translator (e.g. ``cron-descriptor``) — we cover the patterns the
  preset builder UI actually emits and degrade gracefully for the rest.
* ``preview_schedule`` — returns up to N upcoming fire datetimes in the
  pipeline's timezone, so the editor can show "Next: Mon 5/20 2:30 PM"
  before the operator commits.

All three are pure functions; the engine and the route handler call them.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

# Same regex the engine uses for the legacy HH:MM shorthand — kept in
# sync intentionally. Duplicated rather than imported to avoid a circular
# dependency through engine -> schedule -> engine.
_LEGACY_HHMM = re.compile(r"^(\*|\d{1,2}):(\*|\d{1,2})$")

_DAY_NAMES = {
    "0": "Sun",
    "1": "Mon",
    "2": "Tue",
    "3": "Wed",
    "4": "Thu",
    "5": "Fri",
    "6": "Sat",
    "7": "Sun",  # croniter accepts both 0 and 7 for Sunday
}


def normalize_schedule(expr: str) -> str:
    """Return the canonical 5-field cron form of ``expr``.

    ``"14:30"`` → ``"30 14 * * *"`` (every day at 2:30 PM).
    Already-cron expressions pass through unchanged. Empty input → empty.
    """
    expr = (expr or "").strip()
    if not expr:
        return ""
    legacy = _LEGACY_HHMM.match(expr)
    if not legacy:
        return expr
    hour, minute = legacy.group(1), legacy.group(2)
    return f"{minute} {hour} * * *"


def _format_hm(hour: str, minute: str) -> str:
    """Render a 24h cron hour/minute pair as "HH:MM" or "*:MM" / "HH:*"."""
    if hour == "*" and minute == "*":
        return "every minute"
    if hour == "*":
        return f"every hour at :{int(minute):02d}"
    if minute == "*":
        return f"every minute of hour {int(hour):02d}"
    return f"{int(hour):02d}:{int(minute):02d}"


def _humanize_every_day(hour: str, minute: str, time_phrase: str) -> str:
    """Branch of humanize_schedule for ``* * * * *`` family (dow=*)."""
    if hour == "*" and minute == "*":
        return "Every minute"
    if hour == "*":
        return f"Every hour at :{int(minute):02d}"
    if minute == "*":
        return f"Every minute of hour {int(hour):02d}"
    return f"Daily at {time_phrase}"


def _humanize_dow_set(dow: str, time_phrase: str, expr: str) -> str:
    """Branch for day-of-week sets/ranges. Returns ``Custom: <expr>`` on
    any token we can't safely name."""
    tokens = re.split(r"[,\-]", dow)
    names = [_DAY_NAMES[t] for t in tokens if t in _DAY_NAMES]
    if len(names) != len(tokens):
        return f"Custom: {expr}"
    if "-" in dow:
        return f"{names[0]}–{names[-1]} at {time_phrase}"
    return f"{', '.join(names)} at {time_phrase}"


def humanize_schedule(expr: str) -> str:
    """Return a short plain-English description of ``expr``.

    Covers the patterns the preset builder emits (daily / weekly / hourly
    / per-minute). Falls back to ``"Custom: <expr>"`` for anything else
    — the editor still shows ``preview_schedule()`` output below this so
    the operator can see the fire times even when the human label is
    generic.
    """
    expr = (expr or "").strip()
    if not expr:
        return "Not scheduled"
    expr = normalize_schedule(expr)
    parts = expr.split()
    if len(parts) != 5:
        return f"Custom: {expr}"
    minute, hour, dom, month, dow = parts

    # Day-of-month + month should both be "*" for any of our handled
    # patterns. Anything else routes straight to the "Custom" fallback.
    if dom != "*" or month != "*":
        return f"Custom: {expr}"

    time_phrase = _format_hm(hour, minute)
    if dow == "*":
        return _humanize_every_day(hour, minute, time_phrase)
    if dow == "1-5":
        return f"Weekdays at {time_phrase}"
    if dow in ("0,6", "6,0"):
        return f"Weekends at {time_phrase}"
    if "," in dow or "-" in dow:
        return _humanize_dow_set(dow, time_phrase, expr)
    if dow in _DAY_NAMES:
        return f"Every {_DAY_NAMES[dow]} at {time_phrase}"
    return f"Custom: {expr}"


def preview_schedule(
    expr: str,
    tz: str = "",
    count: int = 5,
) -> dict[str, Any]:
    """Return a structured preview of ``expr`` for the editor UI.

    ``{"valid": bool, "human": str, "next": [iso8601, ...], "error": str}``

    Empty ``tz`` falls back to server-local time, matching how an empty
    ``Pipeline.timezone`` is evaluated at firing time.
    """
    normalized = normalize_schedule(expr)
    out: dict[str, Any] = {"valid": False, "human": "", "next": [], "error": ""}

    if not normalized:
        out["human"] = "Not scheduled"
        out["valid"] = True
        return out

    out["human"] = humanize_schedule(expr)

    try:
        from croniter import croniter  # imported lazily to mirror engine
    except ImportError:
        out["error"] = "croniter not installed"
        return out

    # Build the reference datetime in the requested zone — same logic as
    # _schedule_matches so the preview lines up with what the engine
    # will actually fire on.
    base: datetime
    if tz:
        try:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        except ImportError:
            out["error"] = "zoneinfo not available"
            return out
        try:
            base = datetime.now(ZoneInfo(tz))
        except ZoneInfoNotFoundError:
            out["error"] = f"Unknown timezone: {tz}"
            return out
    else:
        base = datetime.now()

    try:
        itr = croniter(normalized, base)
    except (ValueError, KeyError) as exc:
        out["error"] = f"Invalid cron expression: {exc}"
        return out

    upcoming: list[str] = []
    try:
        for _ in range(max(1, min(count, 10))):
            upcoming.append(itr.get_next(datetime).isoformat())
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        out["error"] = f"Could not project schedule: {exc}"
        return out

    out["next"] = upcoming
    out["valid"] = True
    return out
