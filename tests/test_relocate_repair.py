"""A relocated hive that is not running must not report "nothing to do".

Observed on a real box: the relocation completed every removal — state
moved, old unit gone, old shims gone — and the daemon it relocated into
never came up. Re-running the command answered "Already relocated —
nothing to do" and exited 0, because ``already_done`` is defined purely
in terms of the OLD name being gone. True about the old name, useless to
an operator whose dashboard is down, and it sent them to hand-repair
something the command could have finished itself.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from swarm import relocate as rl


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(rl, "_UNIT_DIR", tmp_path / ".config" / "systemd" / "user")
    (tmp_path / ".swarm-legacy").mkdir()
    return tmp_path


def _relocated_plan(*, unit_exists: bool, unit_active: bool, tmp_path: Path) -> rl.RelocationPlan:
    """A plan whose old side is entirely gone — already_done is True."""
    return rl.RelocationPlan(
        source=tmp_path / ".swarm",
        target=tmp_path / ".swarm-legacy",
        move_needed=False,
        old_unit=None,
        new_unit=tmp_path / "swarm-legacy.service",
        unit_active=False,
        old_entrypoints=[],
        new_unit_exists=unit_exists,
        new_unit_active=unit_active,
    )


class TestNeedsRepair:
    def test_healthy_relocation_needs_nothing(self, tmp_path: Path) -> None:
        plan_ = _relocated_plan(unit_exists=True, unit_active=True, tmp_path=tmp_path)
        assert plan_.already_done is True
        assert plan_.needs_repair is False

    def test_missing_unit_is_repairable(self, tmp_path: Path) -> None:
        plan_ = _relocated_plan(unit_exists=False, unit_active=False, tmp_path=tmp_path)
        assert plan_.needs_repair is True

    def test_inactive_unit_is_repairable(self, tmp_path: Path) -> None:
        """The exact shape seen on the box: unit written, daemon not up."""
        plan_ = _relocated_plan(unit_exists=True, unit_active=False, tmp_path=tmp_path)
        assert plan_.already_done is True, "the old name really is gone"
        assert plan_.needs_repair is True, "but the hive is down, so there IS something to do"

    def test_unrelocated_hive_is_never_repairable(self, tmp_path: Path) -> None:
        """needs_repair must not fire on a hive that has not moved yet.

        Folding this into already_done would show the DESTRUCTIVE plan to
        an operator who had already relocated — the regression #1677
        fixed. The two questions stay separate.
        """
        plan_ = rl.RelocationPlan(
            source=tmp_path / ".swarm",
            target=tmp_path / ".swarm-legacy",
            move_needed=True,
            old_unit=tmp_path / "swarm.service",
            new_unit=tmp_path / "swarm-legacy.service",
            unit_active=True,
        )
        assert plan_.already_done is False
        assert plan_.needs_repair is False


class TestRepair:
    def test_writes_a_missing_unit_and_starts_it(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, ...]] = []

        def _fake_systemctl(*args: str) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess(list(args), 0, stdout="", stderr="")

        monkeypatch.setattr(rl, "_systemctl", _fake_systemctl)
        written = home / "written.service"
        monkeypatch.setattr(rl, "_write_unit", lambda: written)

        plan_ = _relocated_plan(unit_exists=False, unit_active=False, tmp_path=home)
        steps = rl.repair(plan_)

        assert any("Wrote missing unit" in s for s in steps)
        assert ("daemon-reload",) in calls
        assert ("enable", rl.LEGACY_UNIT) in calls
        assert ("start", rl.LEGACY_UNIT) in calls

    def test_existing_unit_is_not_rewritten(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repair is not an excuse to clobber a hand-tuned unit."""
        monkeypatch.setattr(
            rl,
            "_systemctl",
            lambda *a: subprocess.CompletedProcess(list(a), 0, stdout="", stderr=""),
        )

        def _boom() -> Path:
            raise AssertionError("_write_unit must not run when the unit already exists")

        monkeypatch.setattr(rl, "_write_unit", _boom)

        plan_ = _relocated_plan(unit_exists=True, unit_active=False, tmp_path=home)
        steps = rl.repair(plan_)

        assert not any("Wrote missing unit" in s for s in steps)

    def test_a_failed_start_is_reported_not_swallowed(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A repair that did not repair must say so."""

        def _fake_systemctl(*args: str) -> subprocess.CompletedProcess[str]:
            if args and args[0] == "start":
                return subprocess.CompletedProcess(
                    list(args), 1, stdout="", stderr="Job failed. See journal."
                )
            return subprocess.CompletedProcess(list(args), 0, stdout="", stderr="")

        monkeypatch.setattr(rl, "_systemctl", _fake_systemctl)

        plan_ = _relocated_plan(unit_exists=True, unit_active=False, tmp_path=home)
        steps = rl.repair(plan_)

        assert any("start failed" in s and "journal" in s for s in steps)

    def test_no_start_leaves_the_service_alone(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(
            rl,
            "_systemctl",
            lambda *a: (
                calls.append(a),
                subprocess.CompletedProcess(list(a), 0, stdout="", stderr=""),
            )[1],
        )
        monkeypatch.setattr(rl, "_write_unit", lambda: home / "written.service")

        rl.repair(_relocated_plan(unit_exists=True, unit_active=False, tmp_path=home), start=False)

        assert not any(a and a[0] == "start" for a in calls)
