"""Local release tool — bump calver + promote CHANGELOG Unreleased to dated.

Run by the project-local ``/ship`` skill (``.claude/commands/ship.md``)
before every commit so the version number and the CHANGELOG stay in
lockstep. Replaces the prior ``.github/workflows/version-bump.yml``
which produced noisy ``chore(version): bump X [skip ci]`` commits on
every push without touching the CHANGELOG.

Calendar versioning rules (matching the retired workflow):

- Today's first bump from a past date → ``YYYY.M.D`` (no patch).
- Bumping again the same day from ``YYYY.M.D`` → ``YYYY.M.D.2``.
- Bumping again from ``YYYY.M.D.N`` → ``YYYY.M.D.(N+1)``.

Usage::

    python scripts/release.py                       # print next version
    python scripts/release.py --apply               # write files + return version
    python scripts/release.py --apply --quiet       # write files, no stdout

The script is deliberately side-effect free unless ``--apply`` is
passed so dry runs are safe. All logic lives in pure functions so the
test suite can exercise the arithmetic and the CHANGELOG rewrite
without touching the filesystem.

``--apply`` **refuses and exits 1 when ``## Unreleased`` has no bullets**,
writing nothing. See ``promote_changelog`` for why: promoting an empty
Unreleased is how 108 consecutive releases came to record nothing at all.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

_VERSION_RE = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})(?:\.(\d+))?$")


def compute_next_version(current: str, today: date) -> str:
    """Return the next calver version after ``current`` on ``today``.

    Raises ``ValueError`` if ``current`` parses as a date later than
    ``today`` — that's almost always a clock-skew bug and silent
    roll-back would be worse than failing loudly.
    """
    m = _VERSION_RE.match(current.strip())
    if not m:
        # Non-calver strings (e.g. "0.1.0-dev") just bump to today.
        return f"{today.year}.{today.month}.{today.day}"

    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    patch = int(m.group(4)) if m.group(4) else None
    try:
        current_date = date(year, month, day)
    except ValueError as exc:
        raise ValueError(f"Invalid calver date in {current!r}: {exc}") from exc

    if current_date > today:
        raise ValueError(
            f"Current version {current!r} is ahead of today {today.isoformat()} — "
            "refusing to roll the version backwards."
        )

    if current_date < today:
        # New day: drop any patch and reset to the bare date.
        return f"{today.year}.{today.month}.{today.day}"

    # Same day — increment the patch. First bump of the day goes to .2
    # (not .1) to match the pre-existing workflow and keep ordering
    # intuitive: YYYY.M.D < YYYY.M.D.2.
    next_patch = 2 if patch is None else patch + 1
    return f"{today.year}.{today.month}.{today.day}.{next_patch}"


# CHANGELOG promotion -------------------------------------------------------

_UNRELEASED_HEADING = "## Unreleased"
_EMPTY_SKELETON = "## Unreleased\n\n### Features\n\n### Changes\n\n### Fixes\n"

# A list item with actual text after the dash. ``### Fixes`` alone is not
# content, and neither is a stray ``-`` on its own line.
_BULLET_RE = re.compile(r"(?m)^[ \t]*-[ \t]+\S")


class EmptyUnreleasedError(ValueError):
    """Raised when ``## Unreleased`` carries no bullets at release time.

    Subclasses ``ValueError`` so callers that already catch the malformed-
    CHANGELOG case keep working; ``main()`` catches it by type to exit
    non-zero with an actionable message rather than a traceback.
    """


def promote_changelog(changelog_text: str, version: str, iso_date: str) -> str:
    """Insert a dated ``## [version] - YYYY-MM-DD`` section carrying the
    current Unreleased content, then reset Unreleased to the empty
    Features/Changes/Fixes skeleton.

    **Refuses (raises ``EmptyUnreleasedError``) when the Unreleased body
    contains no ``- `` bullets.** This function used to promote regardless,
    on the theory that a dated anchor with an empty skeleton was easier to
    backfill than to reconstruct later. That was wrong in exactly the way a
    silent failure usually is: nothing ever pushed back, so **108
    consecutive releases — every one from 2026.8.6 through 2026.8.10.19 —
    shipped as hollow ``### Features / ### Changes / ### Fixes`` headings
    with no bullets under any of them** (113 empty dated sections in all,
    counting five stragglers from April and May), while README.md advertised
    the CHANGELOG as "the authoritative record of what has shipped".
    Backfilling them meant reconstructing five days of history from commit
    bodies — precisely the work the empty anchors were supposed to save.

    The anchor was never the scarce thing — the notes were. Refusing costs
    the releaser one sentence at the moment they still remember what they
    changed; not refusing costs an archaeologist an afternoon.

    Idempotence is preserved on the path that matters: promoting populated
    content twice still produces one dated section per version, and a second
    run against the freshly-reset skeleton now refuses instead of stacking
    another empty heading.
    """
    if _UNRELEASED_HEADING not in changelog_text:
        raise ValueError(
            f"CHANGELOG has no {_UNRELEASED_HEADING!r} heading — refusing to rewrite "
            "an unrecognised structure."
        )

    before, _, rest = changelog_text.partition(_UNRELEASED_HEADING)

    # Capture everything between Unreleased and the next ``## `` section
    # (or end of file). That content gets relocated under the dated
    # heading. We keep the original separator ``---`` (if present) in
    # place by splitting on the next ``## `` marker.
    next_section_marker = re.search(r"\n(## )", rest)
    if next_section_marker is None:
        unreleased_body = rest
        tail = ""
    else:
        idx = next_section_marker.start()
        unreleased_body = rest[:idx]
        tail = rest[idx:]  # includes the leading newline + next heading

    if not _BULLET_RE.search(unreleased_body):
        raise EmptyUnreleasedError(
            f"{_UNRELEASED_HEADING} has no '- ' bullets — refusing to cut {version}. "
            "Promoting an empty Unreleased is what silently produced 108 hollow "
            "CHANGELOG sections (2026.8.6 through 2026.8.10.19). Add at least one "
            "bullet under ### Features, ### Changes or ### Fixes describing what "
            "this release changes, then re-run."
        )

    dated_section = f"\n\n## [{version}] - {iso_date}\n{unreleased_body.rstrip()}\n"

    # Reset Unreleased to the skeleton, normalising whatever indentation /
    # spacing the previous editor left behind. Reaching here means the body
    # had bullets, so nothing is being discarded — it moved to the dated
    # section above.
    rebuilt = before + _EMPTY_SKELETON + dated_section + tail
    # Normalise more than two consecutive blank lines into exactly one.
    rebuilt = re.sub(r"\n{3,}", "\n\n", rebuilt)
    return rebuilt


# File I/O ------------------------------------------------------------------

_PYPROJECT_VERSION_RE = re.compile(r'^version\s*=\s*["\'][^"\']+["\']', re.MULTILINE)
_INIT_VERSION_RE = re.compile(r'^__version__\s*=\s*["\'][^"\']+["\']', re.MULTILINE)


def update_version_files(repo_root: Path, new_version: str) -> None:
    """Rewrite the version string in ``pyproject.toml`` and
    ``src/swarm/__init__.py``. Raises ``FileNotFoundError`` if either
    is missing."""
    pyproject = repo_root / "pyproject.toml"
    init_file = repo_root / "src" / "swarm" / "__init__.py"
    if not pyproject.exists():
        raise FileNotFoundError(f"pyproject.toml not found at {pyproject}")
    if not init_file.exists():
        raise FileNotFoundError(f"swarm __init__.py not found at {init_file}")

    py_text = pyproject.read_text()
    new_py = _PYPROJECT_VERSION_RE.sub(f'version = "{new_version}"', py_text, count=1)
    if new_py == py_text:
        raise RuntimeError(f"No top-level ``version = ...`` line found in {pyproject} — aborting.")
    pyproject.write_text(new_py)

    init_text = init_file.read_text()
    new_init = _INIT_VERSION_RE.sub(f'__version__ = "{new_version}"', init_text, count=1)
    if new_init == init_text:
        raise RuntimeError(f"No ``__version__ = ...`` line found in {init_file} — aborting.")
    init_file.write_text(new_init)


def read_current_version(repo_root: Path) -> str:
    pyproject = repo_root / "pyproject.toml"
    text = pyproject.read_text()
    m = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not m:
        raise RuntimeError("pyproject.toml has no top-level ``version`` line")
    return m.group(1)


def apply_release(repo_root: Path, today: date | None = None) -> str:
    """End-to-end: compute next version, rewrite files + CHANGELOG,
    return the new version string. Raises on any malformed input.

    The CHANGELOG is promoted **in memory first** so a refusal (empty
    Unreleased, unrecognised structure) aborts before anything is written.
    Bumping ``pyproject.toml`` and then refusing would leave the tree in a
    state where re-running computes a *different* next version.
    """
    today = today or date.today()
    current = read_current_version(repo_root)
    new_version = compute_next_version(current, today)

    changelog = repo_root / "CHANGELOG.md"
    promoted: str | None = None
    if changelog.exists():
        promoted = promote_changelog(changelog.read_text(), new_version, today.isoformat())

    update_version_files(repo_root, new_version)
    if promoted is not None:
        changelog.write_text(promoted)
    return new_version


def _repo_root() -> Path:
    """The script lives in ``<repo>/scripts/`` — its parent is the repo."""
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes to pyproject.toml, __init__.py, CHANGELOG.md. "
        "Without this flag, only the next version is printed (dry run).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress stdout (useful when another tool captures the version "
        "from a stdout-less context).",
    )
    args = parser.parse_args(argv)

    repo = _repo_root()
    if args.apply:
        try:
            new_version = apply_release(repo)
        except EmptyUnreleasedError as exc:
            # Non-zero exit so /ship stops here rather than committing a
            # release that records nothing. Nothing has been written.
            print(f"release: {exc}", file=sys.stderr)
            return 1
    else:
        new_version = compute_next_version(read_current_version(repo), date.today())

    if not args.quiet:
        print(new_version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
