"""A relocation that cannot succeed must refuse BEFORE it destroys anything.

Observed on a live hive. The box was already relocated — 1843 tasks in
``~/.swarm-legacy`` — and something recreated a 3 MB ``~/.swarm``. That
made ``move_needed`` true, so the dashboard offered "Move now" on a move
that ``_move_state`` refuses outright, because merging two hives is not
something it will do.

The refusal was real. Its POSITION was the defect: ``_stop_live()`` ran
first, so clicking the button would stop the service, SIGKILL the daemon
and the holder, kill every worker — and only then raise, inside a
detached helper whose output went to /dev/null. Full destructive price,
zero chance of success, no error anyone could read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm import relocate as rl


def _plan(*, source: Path, target: Path, move_needed: bool = True) -> rl.RelocationPlan:
    return rl.RelocationPlan(
        source=source,
        target=target,
        move_needed=move_needed,
        old_unit=None,
        new_unit=target.parent / "swarm-legacy.service",
        unit_active=False,
        target_exists=target.exists(),
    )


class TestBlockedReason:
    def test_both_directories_existing_blocks_the_move(self, tmp_path: Path) -> None:
        source = tmp_path / ".swarm"
        target = tmp_path / ".swarm-legacy"
        source.mkdir()
        target.mkdir()

        reason = _plan(source=source, target=target).blocked_reason

        assert reason is not None
        assert str(source) in reason and str(target) in reason
        assert "merge two hives" in reason

    def test_normal_relocation_is_not_blocked(self, tmp_path: Path) -> None:
        source = tmp_path / ".swarm"
        source.mkdir()
        target = tmp_path / ".swarm-legacy"  # does not exist — the ordinary case

        assert _plan(source=source, target=target).blocked_reason is None

    def test_already_relocated_is_not_blocked(self, tmp_path: Path) -> None:
        """Target exists, source does not — that is just a finished relocation."""
        target = tmp_path / ".swarm-legacy"
        target.mkdir()

        plan_ = _plan(source=tmp_path / ".swarm", target=target, move_needed=False)

        assert plan_.blocked_reason is None


class TestPreflightOrdering:
    def test_nothing_is_stopped_when_the_move_cannot_succeed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression that matters: refuse before killing the hive."""
        source = tmp_path / ".swarm"
        target = tmp_path / ".swarm-legacy"
        source.mkdir()
        target.mkdir()
        (target / "swarm.db").write_bytes(b"the real hive")

        stopped: list[str] = []
        monkeypatch.setattr(rl, "_stop_live", lambda p, **kw: stopped.append("stopped"))
        monkeypatch.setattr(rl, "_check_socket_path_fits", lambda t: None)

        with pytest.raises(rl.RelocationError, match="merge two hives"):
            rl.relocate(_plan(source=source, target=target))

        assert stopped == [], "the service must not be stopped for a move that cannot happen"
        assert (target / "swarm.db").read_bytes() == b"the real hive", "hive untouched"
        assert source.is_dir(), "source untouched"

    def test_preflight_runs_the_socket_check_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both pre-flights belong on the same side of _stop_live."""
        checked: list[Path] = []
        monkeypatch.setattr(rl, "_check_socket_path_fits", lambda t: checked.append(t))

        source = tmp_path / ".swarm"
        source.mkdir()
        target = tmp_path / ".swarm-legacy"
        rl.preflight(_plan(source=source, target=target))

        assert checked == [target]

    def test_a_permitted_relocation_still_proceeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard must not block the ordinary path — positive control."""
        source = tmp_path / ".swarm"
        source.mkdir()
        target = tmp_path / ".swarm-legacy"
        monkeypatch.setattr(rl, "_check_socket_path_fits", lambda t: None)

        rl.preflight(_plan(source=source, target=target))  # must not raise


class TestSystemdPreflight:
    """systemd is what starts the hive again — verify it BEFORE stopping anything."""

    def test_missing_systemd_refuses_without_stopping_the_hive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise: service stopped, state moved, unit nothing can load.

        The operator is then left with no dashboard and no obvious way
        back — from pressing one button in a web page.
        """
        source = tmp_path / ".swarm"
        source.mkdir()
        (source / "swarm.db").write_bytes(b"the hive")

        stopped: list[str] = []
        monkeypatch.setattr(rl, "_stop_live", lambda p, **kw: stopped.append("stopped"))
        monkeypatch.setattr(rl, "_check_socket_path_fits", lambda t: None)
        monkeypatch.setattr("swarm.service._check_systemd", lambda: "systemctl not found.")

        with pytest.raises(rl.RelocationError, match="systemctl not found"):
            rl.relocate(_plan(source=source, target=tmp_path / ".swarm-legacy"))

        assert stopped == [], "nothing may be stopped when nothing can restart it"
        assert (source / "swarm.db").read_bytes() == b"the hive"

    def test_the_refusal_says_nothing_was_changed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A destructive command refusing must say the hive is untouched."""
        source = tmp_path / ".swarm"
        source.mkdir()
        monkeypatch.setattr(rl, "_check_socket_path_fits", lambda t: None)
        monkeypatch.setattr("swarm.service._check_systemd", lambda: "systemd unavailable")

        with pytest.raises(rl.RelocationError, match="still running where it was"):
            rl.preflight(_plan(source=source, target=tmp_path / ".swarm-legacy"))

    def test_available_systemd_does_not_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / ".swarm"
        source.mkdir()
        monkeypatch.setattr(rl, "_check_socket_path_fits", lambda t: None)
        monkeypatch.setattr("swarm.service._check_systemd", lambda: None)

        rl.preflight(_plan(source=source, target=tmp_path / ".swarm-legacy"))  # must not raise
