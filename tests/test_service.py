"""Tests for service.py — systemd/launchd service install/uninstall."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from swarm.service import generate_unit, install_service, uninstall_service


class TestGenerateUnit:
    """Test unit file generation."""

    def test_generates_valid_unit_file(self, tmp_path: Path) -> None:
        config = tmp_path / "swarm.yaml"
        config.write_text("workers: []")

        with (
            patch("swarm.service._detect_source_dir", return_value=None),
            patch("swarm.service.shutil.which", return_value="/usr/local/bin/swarm"),
        ):
            unit = generate_unit(str(config))

        assert "[Unit]" in unit
        assert "[Service]" in unit
        assert "[Install]" in unit
        assert "ExecStart=/usr/local/bin/swarm serve\n" in unit
        # ``-c <yaml>`` must NOT be in ExecStart — DB is the source of
        # truth; the YAML override silently overwrote dashboard state
        # on every restart pre-fix (Amanda 2026-05-05).
        assert "-c " not in unit
        assert "--config" not in unit
        assert "Restart=always" in unit
        assert "WantedBy=default.target" in unit

    def test_includes_swarm_bin_dir_in_path(self, tmp_path: Path) -> None:
        config = tmp_path / "swarm.yaml"
        config.write_text("workers: []")

        with (
            patch("swarm.service._detect_source_dir", return_value=None),
            patch("swarm.service.shutil.which", return_value="/home/user/.local/bin/swarm"),
        ):
            unit = generate_unit(str(config))

        assert "/home/user/.local/bin" in unit

    def test_no_config_path_omits_flag(self) -> None:
        with (
            patch("swarm.service._detect_source_dir", return_value=None),
            patch("swarm.service.shutil.which", return_value="/usr/local/bin/swarm"),
            patch("swarm.service._resolve_config_path", return_value=None),
        ):
            unit = generate_unit(None)

        assert "ExecStart=/usr/local/bin/swarm serve\n" in unit
        assert "-c " not in unit

    def test_raises_if_swarm_not_found(self) -> None:
        with (
            patch("swarm.service._detect_source_dir", return_value=None),
            patch("swarm.service.shutil.which", return_value=None),
            pytest.raises(FileNotFoundError, match="swarm binary not found"),
        ):
            generate_unit(None)

    def test_workdir_is_home(self, tmp_path: Path) -> None:
        """Production unit's WorkingDirectory is ~ — the YAML's parent
        was load-bearing only when ``-c <yaml>`` was passed to ExecStart;
        with that flag gone (DB-first), there's no reason to chdir
        anywhere config-specific.
        """
        config = tmp_path / "mydir" / "swarm.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("workers: []")

        with (
            patch("swarm.service._detect_source_dir", return_value=None),
            patch("swarm.service.shutil.which", return_value="/usr/local/bin/swarm"),
        ):
            unit = generate_unit(str(config))

        assert f"WorkingDirectory={Path.home()}" in unit

    def test_dev_install_uses_uv_run(self, tmp_path: Path) -> None:
        config = tmp_path / "swarm.yaml"
        config.write_text("workers: []")
        source = str(tmp_path / "projects" / "swarm")

        with (
            patch("swarm.service._detect_source_dir", return_value=source),
            patch(
                "swarm.service.shutil.which",
                side_effect=lambda n: {
                    "uv": "/home/user/.local/bin/uv",
                    "swarm": "/home/user/.local/bin/swarm",
                }.get(n),
            ),
        ):
            unit = generate_unit(str(config))

        assert "ExecStart=/home/user/.local/bin/uv run swarm serve" in unit
        assert f"WorkingDirectory={source}" in unit
        assert "SWARM_DEV=1" in unit

    def test_dev_install_raises_without_uv(self, tmp_path: Path) -> None:
        with (
            patch("swarm.service._detect_source_dir", return_value="/some/source"),
            patch("swarm.service.shutil.which", return_value=None),
            pytest.raises(FileNotFoundError, match="uv not found"),
        ):
            generate_unit(None)


class TestInstallService:
    """Test install_service function."""

    def test_creates_service_file(self, tmp_path: Path) -> None:
        service_dir = tmp_path / "systemd" / "user"
        service_path = service_dir / "swarm.service"

        with (
            patch("swarm.service._check_systemd", return_value=None),
            patch(
                "swarm.service.generate_unit",
                return_value="[Unit]\nDescription=Test\n",
            ),
            patch("swarm.service._SERVICE_DIR", service_dir),
            patch("swarm.service._SERVICE_PATH", service_path),
            patch("swarm.service._systemctl") as mock_ctl,
        ):
            result = install_service()

        assert result == service_path
        assert service_path.exists()
        assert "[Unit]" in service_path.read_text()

        # Verify systemctl calls
        calls = [c.args for c in mock_ctl.call_args_list]
        assert ("daemon-reload",) in calls
        assert ("enable", "swarm.service") in calls
        assert ("start", "swarm.service") in calls

    def test_raises_if_systemd_unavailable(self) -> None:
        with (
            patch("swarm.service._check_systemd", return_value="systemctl not found"),
            pytest.raises(RuntimeError, match="systemctl not found"),
        ):
            install_service()


class TestUninstallService:
    """Test uninstall_service function."""

    def test_removes_existing_service_file(self, tmp_path: Path) -> None:
        service_dir = tmp_path / "systemd" / "user"
        service_dir.mkdir(parents=True)
        service_path = service_dir / "swarm.service"
        service_path.write_text("[Unit]\nDescription=Test\n")

        with (
            patch("swarm.service._SERVICE_PATH", service_path),
            patch("swarm.service._systemctl") as mock_ctl,
        ):
            result = uninstall_service()

        assert result is True
        assert not service_path.exists()

        calls = [c.args for c in mock_ctl.call_args_list]
        assert ("stop", "swarm.service") in calls
        assert ("disable", "swarm.service") in calls
        assert ("daemon-reload",) in calls

    def test_returns_false_if_no_service_file(self, tmp_path: Path) -> None:
        service_path = tmp_path / "swarm.service"

        with (
            patch("swarm.service._SERVICE_PATH", service_path),
            patch("swarm.service._systemctl"),
        ):
            result = uninstall_service()

        assert result is False


class TestEnsureKillmodeProcess:
    """Test the auto-patcher that runs on every daemon start."""

    @pytest.fixture(autouse=True)
    def _isolate_unit_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Redirect _SERVICE_PATH to a tmp file and stub systemctl."""
        from swarm import service as svc

        monkeypatch.setattr(svc, "_SERVICE_PATH", tmp_path / "swarm.service")
        monkeypatch.setattr(svc, "_systemctl", lambda *_a, **_kw: None)

    def test_no_patch_when_unit_missing(self) -> None:
        from swarm.service import ensure_killmode_process

        assert ensure_killmode_process() is False

    def test_strips_legacy_config_flag_dash_c(self, tmp_path: Path) -> None:
        """Regression: ``ExecStart=swarm serve -c <yaml>`` must lose
        the ``-c <yaml>`` portion on next daemon start.

        Pre-fix the legacy systemd unit kept passing ``-c yaml`` through
        every reload, silently overriding the DB-canonical state.
        Reported by Amanda 2026-05-05.
        """
        from swarm import service as svc
        from swarm.service import ensure_killmode_process

        unit = (
            "[Unit]\nDescription=Swarm\n\n"
            "[Service]\nKillMode=process\n"
            "ExecStart=/usr/local/bin/swarm serve -c /home/u/.config/swarm/config.yaml\n\n"
            "[Install]\nWantedBy=default.target\n"
        )
        svc._SERVICE_PATH.write_text(unit)

        assert ensure_killmode_process() is True
        rewritten = svc._SERVICE_PATH.read_text()
        assert "ExecStart=/usr/local/bin/swarm serve\n" in rewritten
        assert "-c " not in rewritten
        assert "--config" not in rewritten

    def test_strips_legacy_config_flag_long_form(self) -> None:
        from swarm import service as svc
        from swarm.service import ensure_killmode_process

        for flag in ("--config /etc/swarm.yaml", "--config=/etc/swarm.yaml"):
            unit = (
                "[Unit]\nDescription=Swarm\n\n"
                "[Service]\nKillMode=process\n"
                f"ExecStart=/usr/local/bin/swarm serve {flag}\n\n"
                "[Install]\nWantedBy=default.target\n"
            )
            svc._SERVICE_PATH.write_text(unit)
            assert ensure_killmode_process() is True
            rewritten = svc._SERVICE_PATH.read_text()
            assert "ExecStart=/usr/local/bin/swarm serve\n" in rewritten
            assert "--config" not in rewritten

    def test_idempotent_when_clean(self) -> None:
        from swarm import service as svc
        from swarm.service import ensure_killmode_process

        unit = (
            "[Unit]\nDescription=Swarm\n\n"
            "[Service]\nKillMode=process\n"
            "ExecStart=/usr/local/bin/swarm serve\n\n"
            "[Install]\nWantedBy=default.target\n"
        )
        svc._SERVICE_PATH.write_text(unit)

        # Already clean — must return False without rewriting.
        assert ensure_killmode_process() is False
        # File untouched (modulo no patching).
        assert svc._SERVICE_PATH.read_text() == unit

    def test_rebrands_legacy_description(self) -> None:
        """Regression: an install that predates the "Swarm (legacy)" rename
        must have its unit Description corrected on the next daemon start.

        ``perform_update()`` only reinstalls the package — it never rewrites
        the unit — so without this transform an updated install would keep
        advertising itself as plain "Swarm" in ``systemctl`` output forever.
        """
        from swarm import service as svc
        from swarm.service import ensure_killmode_process

        unit = (
            "[Unit]\nDescription=Swarm Web Dashboard\n\n"
            "[Service]\nKillMode=process\n"
            "ExecStart=/usr/local/bin/swarm serve\n\n"
            "[Install]\nWantedBy=default.target\n"
        )
        svc._SERVICE_PATH.write_text(unit)

        assert ensure_killmode_process() is True
        rewritten = svc._SERVICE_PATH.read_text()
        assert "Description=Swarm (legacy) Web Dashboard\n" in rewritten
        assert "Description=Swarm Web Dashboard\n" not in rewritten
        # Nothing else may move.
        assert "ExecStart=/usr/local/bin/swarm serve\n" in rewritten
        assert "KillMode=process\n" in rewritten

    def test_rebrand_is_idempotent(self) -> None:
        from swarm import service as svc
        from swarm.service import ensure_killmode_process

        unit = (
            "[Unit]\nDescription=Swarm (legacy) Web Dashboard\n\n"
            "[Service]\nKillMode=process\n"
            "ExecStart=/usr/local/bin/swarm serve\n\n"
            "[Install]\nWantedBy=default.target\n"
        )
        svc._SERVICE_PATH.write_text(unit)

        assert ensure_killmode_process() is False
        assert svc._SERVICE_PATH.read_text() == unit

    def test_rebrand_leaves_custom_description_alone(self) -> None:
        """An operator who renamed the unit themselves keeps their name."""
        from swarm import service as svc
        from swarm.service import ensure_killmode_process

        unit = (
            "[Unit]\nDescription=Brad's hive\n\n"
            "[Service]\nKillMode=process\n"
            "ExecStart=/usr/local/bin/swarm serve\n\n"
            "[Install]\nWantedBy=default.target\n"
        )
        svc._SERVICE_PATH.write_text(unit)

        assert ensure_killmode_process() is False
        assert svc._SERVICE_PATH.read_text() == unit

    def test_generated_unit_matches_rebrand_target(self) -> None:
        """A freshly generated unit and the in-place rebrand must agree.

        If these two drift, a fresh install and an updated install end up
        advertising different names for the same service.
        """
        from swarm import service as svc

        assert svc._CURRENT_DESCRIPTION in svc._UNIT_TEMPLATE

    def test_downgrades_killmode_mixed(self) -> None:
        from swarm import service as svc
        from swarm.service import ensure_killmode_process

        unit = (
            "[Unit]\nDescription=Swarm\n\n"
            "[Service]\nKillMode=mixed\n"
            "ExecStart=/usr/local/bin/swarm serve\n\n"
            "[Install]\nWantedBy=default.target\n"
        )
        svc._SERVICE_PATH.write_text(unit)

        assert ensure_killmode_process() is True
        assert "KillMode=process" in svc._SERVICE_PATH.read_text()
        assert "KillMode=mixed" not in svc._SERVICE_PATH.read_text()


class TestResolveConfigPath:
    """Test config path resolution."""

    def test_explicit_path_takes_priority(self, tmp_path: Path) -> None:
        from swarm.service import _resolve_config_path

        config = tmp_path / "custom.yaml"
        config.write_text("workers: []")

        result = _resolve_config_path(str(config))
        assert result == config.resolve()

    def test_finds_cwd_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from swarm.service import _resolve_config_path

        monkeypatch.chdir(tmp_path)
        config = tmp_path / "swarm.yaml"
        config.write_text("workers: []")

        result = _resolve_config_path(None)
        assert result == config.resolve()

    def test_returns_none_if_no_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from swarm.service import _resolve_config_path

        monkeypatch.chdir(tmp_path)

        with patch("swarm.service.Path.home", return_value=tmp_path):
            result = _resolve_config_path(None)

        assert result is None


class TestWslSystemdEnabled:
    """Test _wsl_systemd_enabled() detection."""

    def test_returns_true_when_enabled(self, tmp_path: Path) -> None:
        from swarm.service import _wsl_systemd_enabled

        conf = tmp_path / "wsl.conf"
        conf.write_text("[boot]\nsystemd=true\n")
        with patch("swarm.service._WSL_CONF", conf):
            assert _wsl_systemd_enabled() is True

    def test_returns_false_when_missing(self, tmp_path: Path) -> None:
        from swarm.service import _wsl_systemd_enabled

        conf = tmp_path / "wsl.conf"  # does not exist
        with patch("swarm.service._WSL_CONF", conf):
            assert _wsl_systemd_enabled() is False

    def test_returns_false_when_no_boot_section(self, tmp_path: Path) -> None:
        from swarm.service import _wsl_systemd_enabled

        conf = tmp_path / "wsl.conf"
        conf.write_text("[automount]\nenabled=true\n")
        with patch("swarm.service._WSL_CONF", conf):
            assert _wsl_systemd_enabled() is False

    def test_returns_false_when_systemd_false(self, tmp_path: Path) -> None:
        from swarm.service import _wsl_systemd_enabled

        conf = tmp_path / "wsl.conf"
        conf.write_text("[boot]\nsystemd=false\n")
        with patch("swarm.service._WSL_CONF", conf):
            assert _wsl_systemd_enabled() is False


class TestEnableWslSystemd:
    """Test enable_wsl_systemd() function."""

    def test_creates_wsl_conf_when_missing(self, tmp_path: Path) -> None:
        import subprocess as sp

        from swarm.service import enable_wsl_systemd

        conf = tmp_path / "wsl.conf"  # does not exist
        with (
            patch("swarm.service._WSL_CONF", conf),
            patch("swarm.service.subprocess.run") as mock_run,
        ):
            mock_run.return_value = sp.CompletedProcess([], 0, "", "")
            result = enable_wsl_systemd()

        assert result is True
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["sudo", "tee", str(conf)]
        written = call_args[1]["input"]
        assert "[boot]" in written
        assert "systemd = true" in written

    def test_appends_boot_section_to_existing(self, tmp_path: Path) -> None:
        import subprocess as sp

        from swarm.service import enable_wsl_systemd

        conf = tmp_path / "wsl.conf"
        conf.write_text("[automount]\nenabled = true\n")
        with (
            patch("swarm.service._WSL_CONF", conf),
            patch("swarm.service.subprocess.run") as mock_run,
        ):
            mock_run.return_value = sp.CompletedProcess([], 0, "", "")
            result = enable_wsl_systemd()

        assert result is True
        written = mock_run.call_args[1]["input"]
        assert "[automount]" in written
        assert "[boot]" in written
        assert "systemd = true" in written

    def test_inserts_systemd_into_existing_boot(self, tmp_path: Path) -> None:
        import subprocess as sp

        from swarm.service import enable_wsl_systemd

        conf = tmp_path / "wsl.conf"
        conf.write_text("[boot]\ncommand = /usr/bin/foo\n")
        with (
            patch("swarm.service._WSL_CONF", conf),
            patch("swarm.service.subprocess.run") as mock_run,
        ):
            mock_run.return_value = sp.CompletedProcess([], 0, "", "")
            result = enable_wsl_systemd()

        assert result is True
        written = mock_run.call_args[1]["input"]
        assert "systemd = true" in written
        assert "command = /usr/bin/foo" in written

    def test_noop_if_already_enabled(self, tmp_path: Path) -> None:
        from swarm.service import enable_wsl_systemd

        conf = tmp_path / "wsl.conf"
        conf.write_text("[boot]\nsystemd=true\n")
        with (
            patch("swarm.service._WSL_CONF", conf),
            patch("swarm.service.subprocess.run") as mock_run,
        ):
            result = enable_wsl_systemd()

        assert result is True
        mock_run.assert_not_called()

    def test_raises_on_sudo_failure(self, tmp_path: Path) -> None:
        import subprocess as sp

        from swarm.service import enable_wsl_systemd

        conf = tmp_path / "wsl.conf"
        with (
            patch("swarm.service._WSL_CONF", conf),
            patch("swarm.service.subprocess.run") as mock_run,
        ):
            mock_run.return_value = sp.CompletedProcess([], 1, "", "permission denied")
            with pytest.raises(RuntimeError, match="Failed to write"):
                enable_wsl_systemd()


# --- macOS launchd tests ---


class TestIsMacos:
    """Test is_macos() detection."""

    def test_returns_true_on_darwin(self) -> None:
        from swarm.service import is_macos

        with patch("sys.platform", "darwin"):
            assert is_macos() is True

    def test_returns_false_on_linux(self) -> None:
        from swarm.service import is_macos

        with patch("sys.platform", "linux"):
            assert is_macos() is False


class TestGeneratePlist:
    """Test plist file generation."""

    def test_generates_valid_plist_xml(self, tmp_path: Path) -> None:
        from swarm.service import generate_plist

        config = tmp_path / "swarm.yaml"
        config.write_text("workers: []")

        with (
            patch("swarm.service._detect_source_dir", return_value=None),
            patch("swarm.service.shutil.which", return_value="/usr/local/bin/swarm"),
            patch("swarm.service._SWARM_LOG_DIR", tmp_path / ".swarm"),
        ):
            plist = generate_plist(str(config))

        assert '<?xml version="1.0"' in plist
        assert "<plist version" in plist
        assert "<key>Label</key>" in plist
        assert "<string>com.swarm.dashboard</string>" in plist
        assert "<key>RunAtLoad</key>" in plist
        assert "<true/>" in plist
        assert "<key>KeepAlive</key>" in plist

    def test_includes_swarm_binary_path(self, tmp_path: Path) -> None:
        from swarm.service import generate_plist

        config = tmp_path / "swarm.yaml"
        config.write_text("workers: []")

        with (
            patch("swarm.service._detect_source_dir", return_value=None),
            patch("swarm.service.shutil.which", return_value="/opt/bin/swarm"),
            patch("swarm.service._SWARM_LOG_DIR", tmp_path / ".swarm"),
        ):
            plist = generate_plist(str(config))

        assert "<string>/opt/bin/swarm</string>" in plist
        assert "<string>serve</string>" in plist

    def test_includes_config_path(self, tmp_path: Path) -> None:
        from swarm.service import generate_plist

        config = tmp_path / "swarm.yaml"
        config.write_text("workers: []")

        with (
            patch("swarm.service._detect_source_dir", return_value=None),
            patch("swarm.service.shutil.which", return_value="/usr/local/bin/swarm"),
            patch("swarm.service._SWARM_LOG_DIR", tmp_path / ".swarm"),
        ):
            plist = generate_plist(str(config))

        assert "<string>-c</string>" in plist
        assert f"<string>{config.resolve()}</string>" in plist

    def test_no_config_omits_flag(self) -> None:
        from swarm.service import generate_plist

        with (
            patch("swarm.service._detect_source_dir", return_value=None),
            patch("swarm.service.shutil.which", return_value="/usr/local/bin/swarm"),
            patch("swarm.service._resolve_config_path", return_value=None),
            patch("swarm.service._SWARM_LOG_DIR", Path("/tmp/.swarm")),
        ):
            plist = generate_plist(None)

        assert "<string>-c</string>" not in plist

    def test_raises_if_swarm_not_found(self) -> None:
        from swarm.service import generate_plist

        with (
            patch("swarm.service._detect_source_dir", return_value=None),
            patch("swarm.service.shutil.which", return_value=None),
            pytest.raises(FileNotFoundError, match="swarm binary not found"),
        ):
            generate_plist(None)

    def test_log_paths_in_plist(self, tmp_path: Path) -> None:
        from swarm.service import generate_plist

        log_dir = tmp_path / ".swarm"

        with (
            patch("swarm.service.shutil.which", return_value="/usr/local/bin/swarm"),
            patch("swarm.service._resolve_config_path", return_value=None),
            patch("swarm.service._SWARM_LOG_DIR", log_dir),
        ):
            plist = generate_plist(None)

        assert str(log_dir / "launchd-stdout.log") in plist
        assert str(log_dir / "launchd-stderr.log") in plist


class TestInstallLaunchd:
    """Test install_launchd function."""

    def test_creates_plist_file(self, tmp_path: Path) -> None:
        from swarm.service import install_launchd

        plist_dir = tmp_path / "LaunchAgents"
        plist_path = plist_dir / "com.swarm.dashboard.plist"

        with (
            patch("swarm.service._check_launchd", return_value=None),
            patch(
                "swarm.service.generate_plist",
                return_value='<?xml version="1.0"?>\n<plist><dict/></plist>\n',
            ),
            patch("swarm.service._PLIST_DIR", plist_dir),
            patch("swarm.service._PLIST_PATH", plist_path),
            patch("swarm.service._launchctl") as mock_ctl,
        ):
            result = install_launchd()

        assert result == plist_path
        assert plist_path.exists()
        assert "<?xml" in plist_path.read_text()

        calls = [c.args for c in mock_ctl.call_args_list]
        assert ("load", str(plist_path)) in calls

    def test_raises_if_not_macos(self) -> None:
        from swarm.service import install_launchd

        with (
            patch(
                "swarm.service._check_launchd",
                return_value="launchd is only available on macOS.",
            ),
            pytest.raises(RuntimeError, match="launchd is only available on macOS"),
        ):
            install_launchd()


class TestUninstallLaunchd:
    """Test uninstall_launchd function."""

    def test_removes_existing_plist(self, tmp_path: Path) -> None:
        from swarm.service import uninstall_launchd

        plist_dir = tmp_path / "LaunchAgents"
        plist_dir.mkdir(parents=True)
        plist_path = plist_dir / "com.swarm.dashboard.plist"
        plist_path.write_text("<plist/>")

        with (
            patch("swarm.service._PLIST_PATH", plist_path),
            patch("swarm.service._launchctl") as mock_ctl,
        ):
            result = uninstall_launchd()

        assert result is True
        assert not plist_path.exists()

        calls = [c.args for c in mock_ctl.call_args_list]
        assert ("unload", str(plist_path)) in calls

    def test_returns_false_if_no_plist(self, tmp_path: Path) -> None:
        from swarm.service import uninstall_launchd

        plist_path = tmp_path / "com.swarm.dashboard.plist"

        with patch("swarm.service._PLIST_PATH", plist_path):
            result = uninstall_launchd()

        assert result is False
