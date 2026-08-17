#!/usr/bin/env python3
"""Swarm says done; Jira does not. Find the board-vs-tracker divergence.

#1707. Twelve tasks sat `done` in swarm with their Jira tickets still open, and NOTHING
would have caught it — it was found by hand. The query is one line, and a divergence
between the board and the tracker is exactly the class the daemon-side verification sweep
exists to surface.

THE CAUSE THAT PRODUCED THE ORIGINAL TWELVE was not a bug in the export logic. Measured:
the Jira OAuth refresh token went invalid at 2026-08-13 19:14:40, and of 70 done tasks
carrying a jira_key, all 58 completed BEFORE that moment exported cleanly and all 12
completed after did not. A clean split on one boundary. So this check is not looking for a
logic error — it is looking for the NEXT outage of any kind between the two systems,
whatever its cause.

COUNTS ONLY LIVE TASKS (#1840). An archived task keeps the status it was archived
with, so one archived while done would be reported here forever as a ticket someone
needs to close — for work deliberately off the board. ``archived_at IS NULL`` is the
board's own authority for whether a task counts, and this uses it.

PRINTS ITS DENOMINATOR. A run that examined zero tasks has not found zero divergences; it
has measured nothing, and the sweep that calls this treats a zero denominator as a FAILED
run rather than a clean one.

EXIT CODES
    0  no divergence
    1  at least one task is done in swarm and not terminal in Jira
    2  could not read the board at all — coverage unknown, fails closed
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# What swarm considers finished. Mirrors integrations.jira._TERMINAL_STATUSES; kept as
# literals so this script has no swarm import and can run from anywhere.
_TERMINAL = ("done", "failed")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(Path.home() / ".swarm" / "swarm.db"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    try:
        con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        # ARCHIVED TASKS ARE NOT DIVERGENCE (#1840). Archiving stamps ``archived_at``
        # and deliberately leaves ``status`` alone, so a task archived while done stays
        # `done` in this query forever — and gets reported as a Jira ticket somebody
        # needs to go close, for work that has left the board on purpose.
        #
        # THIS FILTER CHANGES NOTHING TODAY, AND THAT IS THE POINT. Measured on the live
        # board at the moment of the fix: 70 examined / 12 diverged both before and
        # after, because all 21 archived rows are non-terminal (8 assigned + 1
        # unassigned carry a jira_key; none are done or failed). The checker was correct
        # by accident of what happened to be archived, not by design — and
        # ``queen_archive_task`` explicitly archives ANY task including a finished one,
        # so the first done-and-archived task would have produced a false divergence.
        rows = list(
            con.execute(
                "SELECT number, jira_key, status, jira_exported_status "
                "FROM tasks WHERE jira_key IS NOT NULL AND jira_key <> '' "
                "AND archived_at IS NULL "
                "AND status IN (?, ?)",
                _TERMINAL,
            )
        )
    except sqlite3.Error as exc:
        print(f"UNMEASURABLE — could not read the board: {exc}")
        return 2

    diverged = [r for r in rows if (r["jira_exported_status"] or "") not in _TERMINAL]
    result = {
        "tasks_examined": len(rows),
        "diverged": [
            {
                "number": r["number"],
                "jira_key": r["jira_key"],
                "swarm_status": r["status"],
                "last_exported": r["jira_exported_status"] or "(never)",
            }
            for r in diverged
        ],
        "verifies": "swarm's own record of what it exported",
        "does_not_verify": "the live state of the Jira ticket",
    }

    if a.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("check-jira-divergence")
        print(f"  tasks examined  : {len(rows)}")
        print(f"  diverged        : {len(diverged)}")
        for d in result["diverged"]:
            print(
                f"    #{d['number']} {d['jira_key']} — swarm={d['swarm_status']}, "
                f"last exported {d['last_exported']}"
            )
        print("  NOTE: reads swarm's OWN record of what it exported, not Jira itself.")
        print("        A ticket transitioned by hand still shows as diverged here.")
        print("        Archived tasks are excluded — off the board is not divergence.")

    return 1 if diverged else 0


if __name__ == "__main__":
    sys.exit(main())
