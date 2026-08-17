"""#1840 — check-jira-divergence must not report archived tasks.

WHY THIS TEST EXISTS AND THE MEASUREMENT DOES NOT PROVE IT. The filter was measured on the
live board either side of the change: 70 examined / 12 diverged BEFORE, 70 / 12 AFTER.
Identical. That is not evidence the filter works — it is evidence there was nothing to
remove: all 21 archived rows were non-terminal (8 assigned + 1 unassigned carried a
jira_key; none were done or failed), because #1839's purge archived tasks that were
`assigned`.

So the checker was correct BY ACCIDENT of what happened to be archived. ``status`` is
deliberately not overwritten on archive (#1839), and ``queen_archive_task`` archives ANY
task including a finished one — so the first done-and-archived task produces a false
divergence: a Jira ticket reported as needing to be closed, for work taken off the board
on purpose.

A behaviour change that changes no numbers is exactly the kind that gets reverted as
pointless. This test is the fixture the live board did not supply.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-jira-divergence.py"


def _db(path: Path, rows: list[tuple[int, str, str, str, float | None]]) -> Path:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE tasks (number INTEGER, jira_key TEXT, status TEXT, "
        "jira_exported_status TEXT, archived_at REAL)"
    )
    con.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    return path


def _run(db: Path) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--db", str(db), "--json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, json.loads(proc.stdout)


def test_a_done_and_archived_task_is_not_divergence(tmp_path):
    """THE CASE THE LIVE BOARD DID NOT CONTAIN. Archived while done, never exported —
    without the filter this is a ticket somebody is told to go close."""
    db = _db(tmp_path / "s.db", [(1, "WWD-1", "done", "", 1_700_000_000.0)])

    code, result = _run(db)

    assert result["tasks_examined"] == 0
    assert result["diverged"] == []
    assert code == 0


def test_the_same_row_unarchived_IS_divergence(tmp_path):
    """POSITIVE CONTROL, and the one that makes the test above mean anything. Identical
    row with ``archived_at`` NULL — if this did not report, the test above would pass
    against a checker that finds nothing at all."""
    db = _db(tmp_path / "s.db", [(1, "WWD-1", "done", "", None)])

    code, result = _run(db)

    assert result["tasks_examined"] == 1
    assert [d["number"] for d in result["diverged"]] == [1]
    assert code == 1


def test_live_divergence_survives_alongside_archived_rows(tmp_path):
    """The mixed case: the filter must remove the archived row and ONLY the archived row.
    An over-broad filter that dropped everything would pass both tests above."""
    db = _db(
        tmp_path / "s.db",
        [
            (1, "WWD-1", "done", "", 1_700_000_000.0),  # archived — excluded
            (2, "WWD-2", "done", "", None),  # live, never exported — diverged
            (3, "WWD-3", "done", "done", None),  # live, exported — clean
            (4, "WWD-4", "active", "", None),  # not terminal — out of scope
            (5, "", "done", "", None),  # no jira_key — out of scope
        ],
    )

    code, result = _run(db)

    assert result["tasks_examined"] == 2, "the denominator lost or kept the wrong rows"
    assert [d["number"] for d in result["diverged"]] == [2]
    assert code == 1


@pytest.mark.parametrize("status", ["done", "failed"])
def test_both_terminal_statuses_are_filtered_when_archived(tmp_path, status):
    db = _db(tmp_path / f"{status}.db", [(1, "WWD-1", status, "", 1_700_000_000.0)])

    _, result = _run(db)

    assert result["tasks_examined"] == 0


def test_the_filter_is_in_the_query_not_applied_afterwards():
    """The denominator has to shrink too. Filtering the RESULTS would leave
    ``tasks_examined`` counting archived rows — and that number is what the verification
    sweep reads to decide whether the run measured anything."""
    src = _SCRIPT.read_text()
    query = src.split("con.execute(")[1].split(")")[0]

    assert "archived_at IS NULL" in query
