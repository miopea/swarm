"""Relocation of the state directory off the ``swarm`` name.

The command is destructive and runs once on a real hive, so the coverage
here is about the properties that make it survivable: it refuses rather
than merges, it is safe to re-run after a crash, and a half-finished run
converges instead of needing hand repair.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from swarm import relocate as rl
from swarm.paths import ENV_VAR, state_dir, state_path_str


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated HOME, with systemctl stubbed out."""
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(rl, "_UNIT_DIR", tmp_path / ".config" / "systemd" / "user")
    monkeypatch.setattr(
        rl,
        "_systemctl",
        lambda *args: subprocess.CompletedProcess(list(args), 0, stdout="", stderr=""),
    )
    monkeypatch.delenv(ENV_VAR, raising=False)
    return tmp_path


class TestStateDirResolution:
    def test_defaults_to_the_original_directory(self, home: Path) -> None:
        (home / ".swarm").mkdir()
        assert state_dir() == home / ".swarm"

    def test_prefers_the_relocated_directory_once_it_exists(self, home: Path) -> None:
        """A fresh ~/.swarm must not shadow the real, relocated hive.

        Freeing the name is the point of relocating, so something else
        creating ~/.swarm afterwards is expected — and must not silently
        become the hive Legacy reads.
        """
        (home / ".swarm").mkdir()
        (home / ".swarm-legacy").mkdir()
        assert state_dir() == home / ".swarm-legacy"

    def test_env_override_wins_over_both(self, home: Path, monkeypatch) -> None:
        (home / ".swarm-legacy").mkdir()
        monkeypatch.setenv(ENV_VAR, str(home / "elsewhere"))
        assert state_dir() == home / "elsewhere"

    def test_config_strings_stay_home_anchored(self, home: Path) -> None:
        """Serialized config must not freeze today's absolute path."""
        (home / ".swarm-legacy").mkdir()
        assert state_path_str("reports") == "~/.swarm-legacy/reports"


class TestPlan:
    def test_reports_a_move_when_the_old_directory_exists(self, home: Path) -> None:
        (home / ".swarm").mkdir()
        plan = rl.plan()
        assert plan.move_needed is True
        assert plan.already_done is False

    def test_already_done_once_nothing_is_left_behind(self, home: Path) -> None:
        (home / ".swarm-legacy").mkdir()
        assert rl.plan().already_done is True


class TestMove:
    def test_moves_the_directory_with_its_contents(self, home: Path) -> None:
        src = home / ".swarm"
        (src / "uploads").mkdir(parents=True)
        (src / "swarm.db").write_text("data")
        (src / "uploads" / "a.txt").write_text("attachment")

        rl.relocate(rl.plan(), start=False)

        dst = home / ".swarm-legacy"
        assert not src.exists()
        assert (dst / "swarm.db").read_text() == "data"
        assert (dst / "uploads" / "a.txt").read_text() == "attachment"

    def test_refuses_rather_than_merging_two_hives(self, home: Path) -> None:
        """Two state directories must never be silently combined."""
        (home / ".swarm").mkdir()
        (home / ".swarm-legacy").mkdir()
        (home / ".swarm-legacy" / "swarm.db").write_text("the other hive")

        with pytest.raises(rl.RelocationError, match="Target already exists"):
            rl.relocate(rl.plan(), start=False)

        # Both survive untouched — nothing was merged or clobbered.
        assert (home / ".swarm").is_dir()
        assert (home / ".swarm-legacy" / "swarm.db").read_text() == "the other hive"

    def test_rerunning_after_a_completed_move_is_a_no_op(self, home: Path) -> None:
        src = home / ".swarm"
        src.mkdir()
        (src / "swarm.db").write_text("data")
        rl.relocate(rl.plan(), start=False)

        # Second run: the source is gone, so there is nothing to move and
        # nothing to refuse.  A crash mid-relocation lands here.
        result = rl.relocate(rl.plan(), start=False)
        assert result.moved is False
        assert (home / ".swarm-legacy" / "swarm.db").read_text() == "data"


class TestEntrypointAndUnit:
    def test_removes_the_old_command_so_the_name_is_free(self, home: Path) -> None:
        bin_dir = home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "swarm").write_text("#!/bin/sh\n")
        (home / ".swarm").mkdir()

        result = rl.relocate(rl.plan(), start=False)

        assert not (bin_dir / "swarm").exists()
        assert bin_dir / "swarm" in result.entrypoints_removed

    def test_writes_the_renamed_unit_pointing_at_the_new_command(self, home: Path) -> None:
        (home / ".swarm").mkdir()
        result = rl.relocate(rl.plan(), start=False)

        assert result.unit_written is not None
        assert result.unit_written.name == "swarm-legacy.service"
        assert "swarm-legacy serve" in result.unit_written.read_text()

    @pytest.mark.parametrize(
        ("exec_start", "expected"),
        [
            (
                "ExecStart=/home/u/.local/bin/swarm serve",
                "ExecStart=/home/u/.local/bin/swarm-legacy serve",
            ),
            (
                "ExecStart=/usr/bin/uv run swarm serve",
                "ExecStart=/usr/bin/uv run swarm-legacy serve",
            ),
            (
                "ExecStart=/home/u/p/swarm/.venv/bin/swarm serve",
                "ExecStart=/home/u/p/swarm/.venv/bin/swarm-legacy serve",
            ),
        ],
    )
    def test_renames_exec_start_in_both_install_shapes(
        self, exec_start: str, expected: str
    ) -> None:
        """Dev units say ``uv run swarm serve``; production units say ``/bin/swarm serve``.

        Handling only the production shape would leave a dev install with a
        unit invoking the very command the relocation deletes — a service
        that never starts again.
        """
        assert rl._rename_exec_start(exec_start) == expected

    def test_leaves_the_project_directory_name_alone(self) -> None:
        """Only the command is renamed, never a path that contains 'swarm'."""
        unit = "WorkingDirectory=/home/u/projects/swarm\nExecStart=/usr/bin/uv run swarm serve"
        out = rl._rename_exec_start(unit)
        assert "WorkingDirectory=/home/u/projects/swarm" in out
        assert "uv run swarm-legacy serve" in out

    def test_removes_the_old_unit(self, home: Path) -> None:
        unit_dir = home / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        (unit_dir / "swarm.service").write_text("[Unit]\n")
        (home / ".swarm").mkdir()

        result = rl.relocate(rl.plan(), start=False)

        assert result.old_unit_removed is True
        assert not (unit_dir / "swarm.service").exists()

    def test_carries_dev_dropins_across_the_rename(self, home: Path) -> None:
        """A dev override must survive, with its ExecStart renamed too.

        Dropping the old unit without carrying these would leave the
        service starting the wrong way round with no error — the operator
        configured the project venv and would silently get the installed
        build instead.
        """
        unit_dir = home / ".config" / "systemd" / "user"
        dropin = unit_dir / "swarm.service.d"
        dropin.mkdir(parents=True)
        (unit_dir / "swarm.service").write_text("[Unit]\n")
        (dropin / "dev.conf").write_text(
            "[Service]\nExecStart=\nExecStart=/home/u/p/swarm/.venv/bin/swarm serve\n"
        )
        (home / ".swarm").mkdir()

        result = rl.relocate(rl.plan(), start=False)

        carried = unit_dir / "swarm-legacy.service.d" / "dev.conf"
        assert carried in result.dropins_carried
        assert "swarm-legacy serve" in carried.read_text()
        # The old drop-in directory is gone, so it cannot shadow anything.
        assert not dropin.exists()

    def test_no_dropin_directory_is_not_an_error(self, home: Path) -> None:
        (home / ".swarm").mkdir()
        assert rl.relocate(rl.plan(), start=False).dropins_carried == []


class TestLiveProcessDetection:
    def test_reports_a_running_holder(self, home: Path) -> None:
        state = home / ".swarm"
        state.mkdir()
        (state / "holder.pid").write_text(str(os.getpid()))

        live = rl.find_live_processes(state)

        assert [p.kind for p in live] == ["pty-holder"]
        assert live[0].pid == os.getpid()

    def test_ignores_a_stale_pid_file(self, home: Path, monkeypatch) -> None:
        """A dead pid must not scare the operator with a phantom worker."""
        state = home / ".swarm"
        state.mkdir()
        (state / "daemon.lock").write_text("999999")
        monkeypatch.setattr(rl, "_pid_alive", lambda _pid: False)

        assert rl.find_live_processes(state) == []


class TestServiceNamingFollowsRelocation:
    """`swarm init` must not resurrect the unit the relocation removed."""

    def test_unit_name_is_swarm_service_before_relocating(self, home: Path, monkeypatch) -> None:
        from swarm import service as svc

        monkeypatch.undo()  # drop the conftest pin on current_unit_name
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
        (home / ".swarm").mkdir(exist_ok=True)
        assert svc.current_unit_name() == "swarm.service"

    def test_unit_name_follows_the_relocated_state_dir(self, home: Path, monkeypatch) -> None:
        """Otherwise `swarm init` writes swarm.service on a relocated box.

        That re-occupies the name the relocation just freed, and points it
        at a `swarm` entrypoint that no longer exists — a unit that can
        never start, created by a command the operator thought was safe.
        """
        from swarm import service as svc

        monkeypatch.undo()
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
        (home / ".swarm-legacy").mkdir(exist_ok=True)
        assert svc.current_unit_name() == "swarm-legacy.service"
