"""Tests for scripts/release.py — calver bump + CHANGELOG promotion.

The release script is what ``/ship`` runs before every commit to keep the
version number and the CHANGELOG in sync. These tests cover the three
pure-logic entry points so edits to the script's CLI glue can't silently
break the bump arithmetic or the CHANGELOG rewrite.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

# Make scripts/ importable without installing it.
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from release import (  # noqa: E402
    EmptyUnreleasedError,
    apply_release,
    compute_next_version,
    promote_changelog,
    update_version_files,
)


class TestComputeNextVersion:
    def test_past_date_current_bumps_to_today(self) -> None:
        assert compute_next_version("2026.4.16.4", date(2026, 4, 17)) == "2026.4.17"

    def test_same_date_no_patch_bumps_to_patch_2(self) -> None:
        assert compute_next_version("2026.4.17", date(2026, 4, 17)) == "2026.4.17.2"

    def test_same_date_with_patch_increments_patch(self) -> None:
        assert compute_next_version("2026.4.17.2", date(2026, 4, 17)) == "2026.4.17.3"
        assert compute_next_version("2026.4.17.9", date(2026, 4, 17)) == "2026.4.17.10"

    def test_no_leading_zeros_in_base(self) -> None:
        # April 7 → "2026.4.7" (not "2026.04.07"); that's the format every
        # existing CHANGELOG / pyproject entry uses.
        assert compute_next_version("2026.3.31", date(2026, 4, 7)) == "2026.4.7"

    def test_double_digit_month_and_day(self) -> None:
        assert compute_next_version("2025.11.30", date(2025, 12, 15)) == "2025.12.15"

    def test_year_rollover(self) -> None:
        assert compute_next_version("2025.12.31.4", date(2026, 1, 1)) == "2026.1.1"

    def test_current_ahead_of_today_raises(self) -> None:
        # Shouldn't happen in practice, but protect against clock skew
        # silently rolling the version backwards.
        with pytest.raises(ValueError, match="ahead of today"):
            compute_next_version("2027.1.1", date(2026, 4, 17))


class TestPromoteChangelog:
    def _changelog(self, unreleased_body: str) -> str:
        return f"""# Changelog

Swarm uses calendar versioning.

## Unreleased
{unreleased_body}
---

## v1.0.0

Initial release.
"""

    def test_promotes_populated_unreleased_and_resets(self) -> None:
        body = """
### Features
- Something useful
- Another thing

### Fixes
- A bug squashed
"""
        promoted = promote_changelog(self._changelog(body), "2026.4.17", "2026-04-17")

        # Old content lives under a dated heading now
        assert "## [2026.4.17] - 2026-04-17" in promoted
        assert "Something useful" in promoted
        assert "A bug squashed" in promoted

        # Unreleased still present but reset to the empty sub-header skeleton
        assert "## Unreleased\n\n### Features\n\n### Changes\n\n### Fixes\n" in promoted

        # Historical v1.0.0 is untouched
        assert "## v1.0.0\n\nInitial release." in promoted

        # Dated section appears BETWEEN Unreleased and v1.0.0
        idx_unreleased = promoted.index("## Unreleased")
        idx_dated = promoted.index("## [2026.4.17]")
        idx_v1 = promoted.index("## v1.0.0")
        assert idx_unreleased < idx_dated < idx_v1

    def test_empty_unreleased_refuses(self) -> None:
        """An Unreleased body with only the sub-headings records nothing,
        so promoting it produces a dated heading that says nothing. The
        script used to do exactly that, and 108 consecutive releases
        (2026.8.6 → 2026.8.10.19) shipped hollow. It now refuses."""
        empty_body = "\n\n### Features\n\n### Changes\n\n### Fixes\n\n"
        with pytest.raises(EmptyUnreleasedError, match="no '- ' bullets"):
            promote_changelog(self._changelog(empty_body), "2026.4.17.2", "2026-04-17")

    def test_whitespace_only_unreleased_refuses(self) -> None:
        with pytest.raises(EmptyUnreleasedError):
            promote_changelog(self._changelog("   \n   \n"), "2026.4.17", "2026-04-17")

    def test_bare_dash_is_not_a_bullet(self) -> None:
        """A dash with nothing after it is a typo, not a release note —
        the refusal must not be satisfiable by punctuation alone."""
        with pytest.raises(EmptyUnreleasedError):
            promote_changelog(self._changelog("\n### Fixes\n-\n"), "2026.4.17", "2026-04-17")

    def test_refusal_subclasses_value_error(self) -> None:
        """Callers written against the older ``ValueError`` contract still
        catch the new refusal."""
        assert issubclass(EmptyUnreleasedError, ValueError)

    def test_indented_bullet_counts(self) -> None:
        """Nested bullets under a heading are still content."""
        promoted = promote_changelog(
            self._changelog("\n### Fixes\n  - a nested note\n"), "2026.4.17", "2026-04-17"
        )
        assert "## [2026.4.17] - 2026-04-17" in promoted

    def test_missing_unreleased_section_raises(self) -> None:
        """Loud failure beats silent data loss when the CHANGELOG has
        been hand-edited into a shape the script doesn't recognise."""
        bad = "# Changelog\n\n## v1.0.0\n\nInitial.\n"
        with pytest.raises(ValueError, match="Unreleased"):
            promote_changelog(bad, "2026.4.17", "2026-04-17")

    def test_preserves_horizontal_rule_separator(self) -> None:
        body = "\n### Features\n- a\n"
        promoted = promote_changelog(self._changelog(body), "2026.4.17", "2026-04-17")
        # The `---` separator between Unreleased / dated releases and
        # historical v1.0.0 block must survive.
        assert "\n---\n" in promoted

    def test_idempotent_on_populated_successive_runs(self) -> None:
        """Two real releases back to back stack one dated header each —
        no duplicates, and the first release's notes stay under the first
        heading rather than being re-promoted into the second."""
        first = promote_changelog(
            self._changelog("\n### Fixes\n- first fix\n"), "2026.4.17", "2026-04-17"
        )
        assert first.count("## Unreleased") == 1
        # Second release: the operator writes a new note into the reset skeleton.
        with_new_note = first.replace(
            "## Unreleased\n\n### Features\n",
            "## Unreleased\n\n### Features\n- second thing\n",
            1,
        )
        second = promote_changelog(with_new_note, "2026.4.17.2", "2026-04-17")
        assert second.count("## [2026.4.17] - 2026-04-17") == 1
        assert second.count("## [2026.4.17.2] - 2026-04-17") == 1
        assert second.count("- first fix") == 1
        assert second.count("- second thing") == 1
        assert second.count("## Unreleased") == 1

    def test_second_run_against_reset_skeleton_refuses(self) -> None:
        """The re-run guard: once Unreleased is reset, running again with
        nothing new written refuses rather than appending another empty
        dated heading."""
        first = promote_changelog(
            self._changelog("\n### Fixes\n- a fix\n"), "2026.4.17", "2026-04-17"
        )
        with pytest.raises(EmptyUnreleasedError):
            promote_changelog(first, "2026.4.17.2", "2026-04-17")


class TestApplyReleaseAbortsCleanly:
    """A refusal must not leave the tree half-bumped: if ``pyproject.toml``
    advanced but the CHANGELOG did not, the next run computes a different
    version and the two files disagree about what shipped."""

    def _repo(self, tmp_path: Path, unreleased_body: str) -> Path:
        (tmp_path / "pyproject.toml").write_text('[project]\nversion = "2026.4.17"\n')
        init_dir = tmp_path / "src" / "swarm"
        init_dir.mkdir(parents=True)
        (init_dir / "__init__.py").write_text('__version__ = "2026.4.17"\n')
        (tmp_path / "CHANGELOG.md").write_text(
            "# Changelog\n\n## Unreleased\n"
            + unreleased_body
            + "\n## [2026.4.16] - 2026-04-16\n\n- old\n"
        )
        return tmp_path

    def test_empty_unreleased_writes_nothing(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path, "\n### Features\n\n### Changes\n\n### Fixes\n")
        before_pyproject = (repo / "pyproject.toml").read_text()
        before_init = (repo / "src" / "swarm" / "__init__.py").read_text()
        before_changelog = (repo / "CHANGELOG.md").read_text()

        with pytest.raises(EmptyUnreleasedError):
            apply_release(repo, date(2026, 4, 17))

        assert (repo / "pyproject.toml").read_text() == before_pyproject
        assert (repo / "src" / "swarm" / "__init__.py").read_text() == before_init
        assert (repo / "CHANGELOG.md").read_text() == before_changelog

    def test_populated_unreleased_still_applies(self, tmp_path: Path) -> None:
        """Positive control — without it the test above would pass even if
        ``apply_release`` had stopped writing anything at all."""
        repo = self._repo(tmp_path, "\n### Fixes\n- a real fix\n")

        assert apply_release(repo, date(2026, 4, 17)) == "2026.4.17.2"

        assert 'version = "2026.4.17.2"' in (repo / "pyproject.toml").read_text()
        assert '__version__ = "2026.4.17.2"' in (repo / "src" / "swarm" / "__init__.py").read_text()
        changelog = (repo / "CHANGELOG.md").read_text()
        assert "## [2026.4.17.2] - 2026-04-17" in changelog
        assert "- a real fix" in changelog


class TestUpdateVersionFiles:
    def test_updates_pyproject_and_init(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "swarm-ai"\nversion = "2026.4.16.4"\nother = "x"\n')
        init_dir = tmp_path / "src" / "swarm"
        init_dir.mkdir(parents=True)
        init_file = init_dir / "__init__.py"
        init_file.write_text('"""Swarm."""\n\n__version__ = "2026.4.16.4"\n')

        update_version_files(tmp_path, "2026.4.17")

        assert 'version = "2026.4.17"' in pyproject.read_text()
        assert '__version__ = "2026.4.17"' in init_file.read_text()
        # Other lines unchanged
        assert 'other = "x"' in pyproject.read_text()

    def test_handles_single_quotes_in_pyproject(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nversion = '2026.4.16.4'\n")
        init_dir = tmp_path / "src" / "swarm"
        init_dir.mkdir(parents=True)
        (init_dir / "__init__.py").write_text("__version__ = '2026.4.16.4'\n")

        update_version_files(tmp_path, "2026.4.17")
        assert "2026.4.17" in pyproject.read_text()

    def test_only_replaces_top_level_version(self, tmp_path: Path) -> None:
        """Don't touch dependency version pins that happen to contain
        the word 'version'."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[project]\n"
            'version = "2026.4.16.4"\n'
            'requires-python = ">=3.12"\n'
            "[dependencies]\n"
            'croniter = ">=2.0,<3"  # croniter version policy\n'
        )
        init_dir = tmp_path / "src" / "swarm"
        init_dir.mkdir(parents=True)
        (init_dir / "__init__.py").write_text("__version__ = '2026.4.16.4'\n")

        update_version_files(tmp_path, "2026.4.17")

        text = pyproject.read_text()
        assert text.count('version = "2026.4.17"') == 1
        assert 'croniter = ">=2.0,<3"' in text  # dep pin preserved

    def test_errors_clearly_when_pyproject_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            update_version_files(tmp_path, "2026.4.17")
