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

# Captured before the `home` fixture stubs it, so the no-systemd test can
# exercise the real guard instead of a stub that fakes the failure.
_REAL_SYSTEMCTL = rl._systemctl


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
    monkeypatch.delenv("UV_TOOL_BIN_DIR", raising=False)
    monkeypatch.delenv("XDG_BIN_HOME", raising=False)
    # Pin the discovery sources inside the sandbox. Without this the shim
    # search finds the *test runner's* own bin directory, which makes every
    # assertion depend on the developer's checkout.
    monkeypatch.setattr(rl.sys, "argv", [str(tmp_path / ".local" / "bin" / "swarm-legacy")])
    monkeypatch.setattr(rl, "_uv_bin_dir", lambda: None)
    monkeypatch.setattr(rl, "_receipt_bin_dirs", list)
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
        """Two state directories must never be silently combined.

        The refusal now comes from ``preflight()`` rather than from
        ``_move_state`` — same contract, but raised before ``_stop_live``
        rather than after it had killed every worker. Ordering is pinned
        in test_relocate_preflight.py.
        """
        (home / ".swarm").mkdir()
        (home / ".swarm-legacy").mkdir()
        (home / ".swarm-legacy" / "swarm.db").write_text("the other hive")

        with pytest.raises(rl.RelocationError, match="merge two hives"):
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


class TestUpdateDoesNotReoccupyTheName:
    """`uv tool install` rewrites every declared console script.

    So each in-app update hands `swarm` back to Legacy even though the
    operator deliberately gave that name up. Verified against the real
    behaviour, not assumed: a reinstall on a relocated box does recreate it.
    """

    def _shim(self, home: Path, target: Path) -> Path:
        bin_dir = home / ".local" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        shim = bin_dir / "swarm"
        shim.symlink_to(target)
        return shim

    def test_removes_the_shim_the_update_recreated(self, home: Path) -> None:
        from swarm.update import _drop_reoccupied_entrypoint

        (home / ".swarm-legacy").mkdir()
        tools = home / ".local" / "share" / "uv" / "tools" / "swarm-ai" / "bin"
        tools.mkdir(parents=True)
        (tools / "swarm").write_text("#!/bin/sh\n")
        shim = self._shim(home, tools / "swarm")

        assert _drop_reoccupied_entrypoint() == [shim]
        assert not shim.exists()

    def test_leaves_a_swarm_that_is_not_ours_alone(self, home: Path) -> None:
        """Once something else owns `swarm`, deleting it would be destructive."""
        from swarm.update import _drop_reoccupied_entrypoint

        (home / ".swarm-legacy").mkdir()
        other = home / "somewhere" / "bin" / "swarm"
        other.parent.mkdir(parents=True)
        other.write_text("#!/bin/sh\n# a different project\n")
        shim = self._shim(home, other)

        assert _drop_reoccupied_entrypoint() == []
        assert shim.exists()
        assert other.exists()

    def test_does_nothing_on_an_un_relocated_install(self, home: Path) -> None:
        """`swarm` is the correct name there — removing it would break them."""
        from swarm.update import _drop_reoccupied_entrypoint

        (home / ".swarm").mkdir()
        tools = home / ".local" / "share" / "uv" / "tools" / "swarm-ai" / "bin"
        tools.mkdir(parents=True)
        (tools / "swarm").write_text("#!/bin/sh\n")
        shim = self._shim(home, tools / "swarm")

        assert _drop_reoccupied_entrypoint() == []
        assert shim.exists()


class TestSurvivesAnAwkwardMachine:
    """Edge cases that would otherwise strand an operator mid-relocation."""

    def test_no_systemd_does_not_abort_after_the_move(self, home: Path, monkeypatch) -> None:
        """macOS / systemd-less WSL have no `systemctl` binary at all.

        Unguarded, FileNotFoundError aborted the run *after* the state
        directory had already moved — a correct-but-alarming half-finish
        reported as a raw traceback.
        """

        def _no_systemctl(*_a, **_kw):
            raise FileNotFoundError("systemctl")

        # Restore the real _systemctl and remove the binary underneath it,
        # so the guard inside it is what gets tested.
        monkeypatch.setattr(rl, "_systemctl", _REAL_SYSTEMCTL)
        monkeypatch.setattr(rl.subprocess, "run", _no_systemctl)
        src = home / ".swarm"
        src.mkdir()
        (src / "swarm.db").write_text("data")

        # Must complete rather than raise.
        result = rl.relocate(rl.plan(), start=True)

        assert result.moved is True
        assert (home / ".swarm-legacy" / "swarm.db").read_text() == "data"

    def test_relocating_before_first_run_still_marks_it_relocated(self, home: Path) -> None:
        """An install relocated before it ever created a state directory.

        Without creating the target, the unit and entrypoint get renamed
        while `state_dir()` still resolves to the old path — an install
        that looks relocated but writes its state straight back to the
        name it was supposed to free.
        """
        bin_dir = home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "swarm").write_text("#!/bin/sh\n")
        assert not (home / ".swarm").exists()

        rl.relocate(rl.plan(), start=False)

        assert (home / ".swarm-legacy").is_dir()
        assert state_dir() == home / ".swarm-legacy"
        from swarm.paths import is_relocated

        assert is_relocated() is True

    def test_a_dangling_enable_link_alone_is_not_already_done(self, home: Path) -> None:
        """Otherwise re-running reports success and never cleans it up."""
        unit_dir = home / ".config" / "systemd" / "user"
        wants = unit_dir / "default.target.wants"
        wants.mkdir(parents=True)
        (wants / "swarm.service").symlink_to(unit_dir / "swarm.service")
        (home / ".swarm-legacy").mkdir()

        plan = rl.plan()
        assert plan.stale_enable_link is True
        assert plan.already_done is False

    def test_clears_a_dangling_enable_link(self, home: Path) -> None:
        """A unit file removed by hand leaves the wants symlink behind.

        systemd then complains on every daemon-reload. `_remove_old_unit`
        used to return early when the file was already gone, so nothing
        ever cleaned it up.
        """
        unit_dir = home / ".config" / "systemd" / "user"
        wants = unit_dir / "default.target.wants"
        wants.mkdir(parents=True)
        dangling = wants / "swarm.service"
        dangling.symlink_to(unit_dir / "swarm.service")  # target does not exist
        assert dangling.is_symlink() and not dangling.exists()
        (home / ".swarm").mkdir()

        rl.relocate(rl.plan(), start=False)

        assert not dangling.is_symlink()

    def test_finds_a_shim_in_a_custom_uv_bin_dir(self, home: Path, monkeypatch) -> None:
        """uv honours $UV_TOOL_BIN_DIR; missing it leaves the name occupied."""
        custom = home / "opt" / "bin"
        custom.mkdir(parents=True)
        (custom / "swarm").write_text("#!/bin/sh\n")
        monkeypatch.setenv("UV_TOOL_BIN_DIR", str(custom))
        (home / ".swarm").mkdir()

        result = rl.relocate(rl.plan(), start=False)

        assert custom / "swarm" in result.entrypoints_removed
        assert not (custom / "swarm").exists()


class TestUpdateDoesNotDestroyWhatOwnsTheNameNow:
    """`uv tool install --force` overwrites whatever sits at a script name.

    Verified against the real command, not assumed: a foreign `swarm` on
    PATH is silently replaced by ours. On a relocated install that name has
    been deliberately handed to something else, so an unguarded update
    would destroy the binary standing there.
    """

    def test_preserves_and_restores_a_foreign_swarm(self, home: Path, monkeypatch) -> None:
        from swarm.update import (
            _preserve_foreign_entrypoints,
            _restore_foreign_entrypoints,
        )

        (home / ".swarm-legacy").mkdir()
        bin_dir = home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        foreign = bin_dir / "swarm"
        foreign.write_text("#!/bin/sh\necho other project\n")

        saved = _preserve_foreign_entrypoints()
        assert saved and saved[0][0] == foreign
        assert not foreign.exists()  # out of uv's way

        # uv installs its own copy at the same name.
        foreign.write_text("#!/bin/sh\necho swarm-ai\n")
        _restore_foreign_entrypoints(saved)

        assert "other project" in foreign.read_text()
        assert not saved[0][1].exists()  # backup consumed

    def test_leaves_our_own_shim_for_the_normal_cleanup(self, home: Path) -> None:
        """Ours is not preserved — it is meant to be removed, not restored."""
        from swarm.update import _preserve_foreign_entrypoints

        (home / ".swarm-legacy").mkdir()
        tools = home / ".local" / "share" / "uv" / "tools" / "swarm-ai" / "bin"
        tools.mkdir(parents=True)
        (tools / "swarm").write_text("#!/bin/sh\n")
        bin_dir = home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "swarm").symlink_to(tools / "swarm")

        assert _preserve_foreign_entrypoints() == []

    def test_does_nothing_before_relocation(self, home: Path) -> None:
        """`swarm` is legitimately ours there; moving it would break them."""
        from swarm.update import _preserve_foreign_entrypoints

        (home / ".swarm").mkdir()
        bin_dir = home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "swarm").write_text("#!/bin/sh\n")

        assert _preserve_foreign_entrypoints() == []
        assert (bin_dir / "swarm").exists()


class TestJourneyGotchas:
    """Found by rehearsing the developer journey end to end."""

    def test_refuses_a_move_that_would_break_the_holder_socket(
        self, home: Path, monkeypatch
    ) -> None:
        """`sockaddr_un.sun_path` caps a Unix socket path at ~104 bytes.

        The relocated directory name is seven bytes longer than the
        original, so a deep home can push `<state>/holder.sock` over the
        limit. The holder then cannot bind and no worker starts — after a
        one-way move. Refuse before touching anything.
        """
        deep = home / ("d" * 200)
        with pytest.raises(rl.RelocationError, match="Unix socket"):
            rl._check_socket_path_fits(deep / ".swarm-legacy")

    def test_the_normal_case_still_fits(self, home: Path) -> None:
        rl._check_socket_path_fits(home / ".swarm-legacy")  # must not raise

    def test_finds_the_shim_beside_the_running_command(self, home: Path, monkeypatch) -> None:
        """$UV_TOOL_BIN_DIR is an install-time variable, usually unset later.

        Without this the command reported success while leaving `swarm`
        occupied in a custom bin directory — failing at the one thing it
        exists to do.
        """
        odd = home / "opt" / "somewhere" / "bin"
        odd.mkdir(parents=True)
        (odd / "swarm").write_text("#!/bin/sh\n")
        (odd / "swarm-legacy").write_text("#!/bin/sh\n")
        monkeypatch.delenv("UV_TOOL_BIN_DIR", raising=False)
        monkeypatch.setattr(rl.sys, "argv", [str(odd / "swarm-legacy"), "relocate"])
        monkeypatch.setattr(rl, "_uv_bin_dir", lambda: None)

        assert odd / "swarm" in rl._entrypoint_candidates()

    def test_reports_when_the_name_is_still_occupied(self, home: Path, monkeypatch) -> None:
        """Never claim success the run did not achieve."""
        bin_dir = home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "swarm").write_text("#!/bin/sh\n")
        (home / ".swarm").mkdir()

        # Removal fails (read-only dir, permissions, whatever the cause).
        monkeypatch.setattr(rl, "_remove_old_entrypoints", lambda _paths: [])

        result = rl.relocate(rl.plan(), start=False)

        assert bin_dir / "swarm" in result.still_occupied


class TestPostMoveFailuresAreActionable:
    def test_unit_generation_does_not_depend_on_PATH(self, home: Path, monkeypatch) -> None:
        """`generate_unit()` locates the binary with shutil.which.

        Invoking the command by absolute path, or from a shell whose PATH
        lacks the bin directory, made it raise *after* the state directory
        had already moved — a half-finished relocation reported as a
        traceback.
        """
        bin_dir = home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "swarm").write_text("#!/bin/sh\n")
        (bin_dir / "swarm").chmod(0o755)
        (home / ".swarm").mkdir()
        # Production-shape install (no source checkout), which is the branch
        # that needs the `swarm` binary on PATH.
        monkeypatch.setattr("swarm.service._detect_source_dir", lambda: None)
        monkeypatch.setenv("PATH", "/nonexistent")

        result = rl.relocate(rl.plan(), start=False)  # must not raise

        assert result.unit_written is not None
        assert "swarm-legacy serve" in result.unit_written.read_text()

    def test_a_failure_after_the_move_says_what_to_do(self, home: Path, monkeypatch) -> None:
        """Never let a post-move failure read like data loss."""
        (home / ".swarm").mkdir()
        monkeypatch.setattr(rl, "_write_unit", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        with pytest.raises(rl.RelocationError, match="Re-run the command"):
            rl.relocate(rl.plan(), start=False)

        # The move itself stands; re-running finishes the job.
        assert (home / ".swarm-legacy").is_dir()


class TestLingeringProcesses:
    def test_waits_for_a_slow_daemon_before_moving(self, home: Path, monkeypatch) -> None:
        """SIGTERM without waiting let the old daemon undo the relocation.

        A daemon still shutting down keeps the log path it resolved at
        import — the old one — and recreates the directory the move just
        emptied. Observed: a relocation reporting success with `~/.swarm`
        back moments later holding a lone `swarm.log`.
        """
        state = home / ".swarm"
        state.mkdir()
        (state / "daemon.lock").write_text("4242")

        alive = {"n": 3}  # dies only after a few polls

        def _alive(pid: int) -> bool:
            if pid != 4242:
                return False
            alive["n"] -= 1
            return alive["n"] > 0

        killed: list[int] = []
        monkeypatch.setattr(rl, "_pid_alive", _alive)
        monkeypatch.setattr(rl.os, "kill", lambda pid, sig: killed.append(pid))

        rl._stop_live(rl.plan(), timeout=5.0)

        assert 4242 in killed  # asked it to stop
        assert alive["n"] <= 0  # and waited until it was actually gone

    def test_kills_a_daemon_that_ignores_sigterm(self, home: Path, monkeypatch) -> None:
        """It must not get to undo a relocation by refusing to exit."""
        state = home / ".swarm"
        state.mkdir()
        (state / "daemon.lock").write_text("4243")

        signals: list[int] = []
        monkeypatch.setattr(rl, "_pid_alive", lambda pid: pid == 4243)
        monkeypatch.setattr(rl.os, "kill", lambda pid, sig: signals.append(sig))

        rl._stop_live(rl.plan(), timeout=0.5)

        assert rl.signal.SIGTERM in signals
        assert rl.signal.SIGKILL in signals

    def test_reports_the_old_directory_coming_back(self, home: Path, monkeypatch) -> None:
        (home / ".swarm").mkdir()
        real_move = rl._move_state

        def _move_then_resurrect(src: Path, dst: Path) -> bool:
            moved = real_move(src, dst)
            src.mkdir(parents=True, exist_ok=True)  # a straggler recreates it
            return moved

        monkeypatch.setattr(rl, "_move_state", _move_then_resurrect)

        assert rl.relocate(rl.plan(), start=False).source_recreated is True


class TestEveryCopyOfTheOldNameGoes:
    def test_removes_the_inner_tool_script_too(self, home: Path, monkeypatch) -> None:
        """uv writes both the PATH shim and the script it points at.

        Stopping after the first left the inner copy behind, which kept
        `already_done` false forever — so an operator who had long since
        relocated was shown the destructive banner again on every check.
        """
        from swarm.update import _drop_reoccupied_entrypoint

        (home / ".swarm-legacy").mkdir()
        tools = home / ".local" / "share" / "uv" / "tools" / "swarm-ai" / "bin"
        tools.mkdir(parents=True)
        inner = tools / "swarm"
        inner.write_text("#!/bin/sh\n")
        bin_dir = home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        shim = bin_dir / "swarm"
        shim.symlink_to(inner)
        monkeypatch.setattr(rl, "_receipt_bin_dirs", lambda: [str(tools)])

        removed = _drop_reoccupied_entrypoint()

        assert shim in removed and inner in removed
        assert not shim.exists() and not inner.exists()

    def test_already_done_after_an_update_on_a_relocated_install(
        self, home: Path, monkeypatch
    ) -> None:
        """The end state an operator actually sees: nothing left to do."""
        from swarm.update import _drop_reoccupied_entrypoint

        (home / ".swarm-legacy").mkdir()
        tools = home / ".local" / "share" / "uv" / "tools" / "swarm-ai" / "bin"
        tools.mkdir(parents=True)
        (tools / "swarm").write_text("#!/bin/sh\n")
        monkeypatch.setattr(rl, "_receipt_bin_dirs", lambda: [str(tools)])

        _drop_reoccupied_entrypoint()

        assert rl.plan().already_done is True
