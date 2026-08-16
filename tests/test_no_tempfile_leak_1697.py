"""#1697 — `tempfile.mktemp` leaked a file per call, forever. 16G, disk at 95%.

WHAT IT COST. /tmp held 52,943 `tmp*.db` files totalling 16G — full copies of the swarm
database, ~320KB each — plus 33,237 `tmp*.jsonl` TaskHistory logs. The operator's root
filesystem hit 95% (3.1G free of 61G) and the dev dashboard would not start. Disk
exhaustion does not fail politely: it corrupts SQLite writes and produces errors that name
the wrong subsystem.

WHY IT LEAKED. `tempfile.mktemp()` allocates a NAME and nothing else. No file object, no
context manager, no fixture teardown — so every call that then created a file left it
behind permanently. It is also deprecated for a separate reason (a TOCTOU race), so the
fix addresses both.

THE TICKET NAMED ONE LINE; THERE WERE FOURTEEN, across ten files. Counting by BYTES the
`.db` copies dominate, but counting by INODES the `.jsonl` TaskHistory logs were three
times more numerous — and inode exhaustion is its own outage. A fix scoped to the reported
line would have left the larger population untouched.

THIS SWEEP IS THE POINT OF THE FILE. Replacing fourteen call sites fixes today; the sweep
is what stops the fifteenth, and no runtime test of the existing fourteen would catch a new
one.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# mkdtemp is permitted in exactly one place: the session-wide DB directory in conftest,
# which exists before any fixture can (it has to beat code paths that fire earlier) and
# is reaped by pytest_sessionfinish. Everything else must be owned by a fixture.
_ALLOWED_MKDTEMP = {"tests/conftest.py"}

# THIS FILE ITSELF. It names `mktemp` in its docstring to explain the defect and writes it
# into a fixture to prove the sweep can see one — both deliberate. Excluding it is not a
# hole: the two positive controls below verify the regex still catches a real offender and
# still clears a clean file, which is what the exclusion could otherwise disguise.
_SELF = "tests/test_no_tempfile_leak_1697.py"


def _offenders(pattern: str, allowed: set[str] | None = None) -> list[str]:
    hits: list[str] = []
    for path in sorted((_ROOT / "tests").rglob("*.py")):
        rel = str(path.relative_to(_ROOT))
        if rel == _SELF or (allowed and rel in allowed):
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(pattern, line):
                hits.append(f"{rel}:{i}  {line.strip()[:80]}")
    return hits


def test_no_test_uses_tempfile_mktemp():
    """THE GUARD. `mktemp` returns a name nothing owns; every file created at that name
    survives the run. Use the `tmp_path` fixture, which pytest reaps."""
    offenders = _offenders(r"tempfile\.mktemp\s*\(")

    assert not offenders, (
        "tempfile.mktemp leaks a file per call — 16G of them filled the operator's disk "
        "in #1697. Use the `tmp_path` fixture instead:\n  " + "\n  ".join(offenders)
    )


def test_mkdtemp_is_confined_to_the_reaped_session_directory():
    """`mkdtemp` leaks a DIRECTORY the same way. One use is legitimate — conftest's
    session dir, which must exist before fixtures do and is removed in
    pytest_sessionfinish."""
    offenders = _offenders(r"tempfile\.mkdtemp\s*\(", allowed=_ALLOWED_MKDTEMP)

    assert not offenders, "mkdtemp outside the reaped session dir:\n  " + "\n  ".join(offenders)


def test_the_session_directory_is_actually_reaped():
    """POSITIVE CONTROL for the exemption above. The allowance is only defensible while
    something removes it — otherwise this test is licensing the leak it exempts."""
    conftest = (_ROOT / "tests" / "conftest.py").read_text()

    assert "def pytest_sessionfinish" in conftest
    assert "rmtree(_TEST_DB_DIR" in conftest


def test_the_sweep_can_find_an_offender(tmp_path, monkeypatch):
    """POSITIVE CONTROL FOR THE SWEEP ITSELF. A regex that matched nothing would report a
    clean tree forever and read exactly like success — the defect class this codebase has
    hit repeatedly. Point it at a file that definitely offends."""
    bad = tmp_path / "tests" / "bad_example.py"
    bad.parent.mkdir(parents=True)
    bad.write_text("import tempfile\np = tempfile.mktemp(suffix='.db')\n")
    monkeypatch.setattr("tests.test_no_tempfile_leak_1697._ROOT", tmp_path)

    assert any("bad_example.py" in f for f in _offenders(r"tempfile\.mktemp\s*\("))


def test_the_sweep_accepts_a_clean_file(tmp_path, monkeypatch):
    """The other direction: a sweep that flagged everything would be switched off as fast
    as one that flagged nothing."""
    good = tmp_path / "tests" / "good_example.py"
    good.parent.mkdir(parents=True)
    good.write_text("def test_x(tmp_path):\n    p = tmp_path / 'x.db'\n")
    monkeypatch.setattr("tests.test_no_tempfile_leak_1697._ROOT", tmp_path)

    assert _offenders(r"tempfile\.mktemp\s*\(") == []
