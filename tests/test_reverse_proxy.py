"""Tests for ``swarm.reverse_proxy`` — Caddy install + Caddyfile setup.

These functions wrap ``subprocess.run`` and ``shutil.which`` to install
and configure Caddy as the operator's reverse proxy. The module had no
test coverage; this file mocks the system boundary so we can verify the
decision logic (install when missing, abort on first failing step, write
the right Caddyfile template) without touching the real apt/systemd
on the host.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

from swarm import reverse_proxy


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr=stderr)


def _fail(stderr: str = "boom") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


# ---------------------------------------------------------------------------
# caddy_installed — ``shutil.which`` boundary
# ---------------------------------------------------------------------------


def test_caddy_installed_true_when_on_path() -> None:
    with patch("swarm.reverse_proxy.shutil.which", return_value="/usr/bin/caddy"):
        assert reverse_proxy.caddy_installed() is True


def test_caddy_installed_false_when_missing() -> None:
    with patch("swarm.reverse_proxy.shutil.which", return_value=None):
        assert reverse_proxy.caddy_installed() is False


# ---------------------------------------------------------------------------
# install_caddy — 5-step apt pipeline
# ---------------------------------------------------------------------------


def test_install_caddy_succeeds_when_all_steps_succeed() -> None:
    run = MagicMock(return_value=_ok())
    with patch("swarm.reverse_proxy.subprocess.run", run):
        assert reverse_proxy.install_caddy() is True
    # 5 install steps; each calls subprocess.run once.
    assert run.call_count == 5


def test_install_caddy_aborts_on_first_failing_step() -> None:
    """Mid-pipeline failure must stop and return False — never continue
    once a step has failed (apt repo unconfigured would silently install
    nothing if we ignored the failure)."""
    outcomes = [_ok(), _ok(), _fail("repo unreachable"), _ok(), _ok()]
    run = MagicMock(side_effect=outcomes)
    with patch("swarm.reverse_proxy.subprocess.run", run):
        assert reverse_proxy.install_caddy() is False
    # Should not have invoked steps 4 or 5 after step 3 failed.
    assert run.call_count == 3


# ---------------------------------------------------------------------------
# write_caddyfile — Caddyfile template + sudo-tee piping
# ---------------------------------------------------------------------------


def test_write_caddyfile_pipes_expected_template() -> None:
    """The template substitution must include both ``{domain}`` and
    ``localhost:{port}`` reverse-proxy directives literally — that's what
    Caddy actually parses."""
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input", "")
        return _ok()

    with patch("swarm.reverse_proxy.subprocess.run", side_effect=fake_run):
        assert reverse_proxy.write_caddyfile("swarm.example.com", port=9090) is True

    assert captured["cmd"][:2] == ["sudo", "tee"]
    content = captured["input"]
    assert "swarm.example.com {" in content
    assert "reverse_proxy localhost:9090" in content


def test_write_caddyfile_returns_false_on_tee_failure() -> None:
    with patch("swarm.reverse_proxy.subprocess.run", return_value=_fail("perm denied")):
        assert reverse_proxy.write_caddyfile("swarm.example.com") is False


def test_write_caddyfile_honors_custom_port() -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["input"] = kwargs.get("input", "")
        return _ok()

    with patch("swarm.reverse_proxy.subprocess.run", side_effect=fake_run):
        reverse_proxy.write_caddyfile("swarm.example.com", port=8443)

    assert "localhost:8443" in captured["input"]


# ---------------------------------------------------------------------------
# reload_caddy — restart then enable
# ---------------------------------------------------------------------------


def test_reload_caddy_succeeds_when_restart_and_enable_succeed() -> None:
    run = MagicMock(return_value=_ok())
    with patch("swarm.reverse_proxy.subprocess.run", run):
        assert reverse_proxy.reload_caddy() is True
    # restart + enable
    assert run.call_count == 2


def test_reload_caddy_returns_false_when_restart_fails() -> None:
    """If restart fails we don't bother trying enable — there's no service
    to enable."""
    run = MagicMock(side_effect=[_fail("unit not found")])
    with patch("swarm.reverse_proxy.subprocess.run", run):
        assert reverse_proxy.reload_caddy() is False
    # Should NOT have attempted the enable step.
    assert run.call_count == 1


def test_reload_caddy_returns_false_when_enable_fails() -> None:
    run = MagicMock(side_effect=[_ok(), _fail("enable failed")])
    with patch("swarm.reverse_proxy.subprocess.run", run):
        assert reverse_proxy.reload_caddy() is False


# ---------------------------------------------------------------------------
# setup_caddy — full pipeline (install + write + reload)
# ---------------------------------------------------------------------------


def test_setup_caddy_skips_install_when_already_present() -> None:
    """If caddy is already on PATH, ``install_caddy`` is never called.
    Avoids re-running a 5-step apt pipeline on a healthy host."""
    with (
        patch("swarm.reverse_proxy.caddy_installed", return_value=True),
        patch("swarm.reverse_proxy.install_caddy") as mock_install,
        patch("swarm.reverse_proxy.write_caddyfile", return_value=True),
        patch("swarm.reverse_proxy.reload_caddy", return_value=True),
    ):
        assert reverse_proxy.setup_caddy("swarm.example.com") is True
        mock_install.assert_not_called()


def test_setup_caddy_aborts_when_install_fails() -> None:
    with (
        patch("swarm.reverse_proxy.caddy_installed", return_value=False),
        patch("swarm.reverse_proxy.install_caddy", return_value=False),
        patch("swarm.reverse_proxy.write_caddyfile") as mock_write,
        patch("swarm.reverse_proxy.reload_caddy") as mock_reload,
    ):
        assert reverse_proxy.setup_caddy("swarm.example.com") is False
        # Downstream steps must NOT run after install failure.
        mock_write.assert_not_called()
        mock_reload.assert_not_called()


def test_setup_caddy_aborts_when_write_caddyfile_fails() -> None:
    with (
        patch("swarm.reverse_proxy.caddy_installed", return_value=True),
        patch("swarm.reverse_proxy.write_caddyfile", return_value=False),
        patch("swarm.reverse_proxy.reload_caddy") as mock_reload,
    ):
        assert reverse_proxy.setup_caddy("swarm.example.com") is False
        # Reload must NOT run if Caddyfile didn't write.
        mock_reload.assert_not_called()


def test_setup_caddy_returns_reload_result() -> None:
    with (
        patch("swarm.reverse_proxy.caddy_installed", return_value=True),
        patch("swarm.reverse_proxy.write_caddyfile", return_value=True),
        patch("swarm.reverse_proxy.reload_caddy", return_value=False),
    ):
        # Even though install + write succeeded, reload failure surfaces.
        assert reverse_proxy.setup_caddy("swarm.example.com") is False


# ---------------------------------------------------------------------------
# caddy_status — None when caddy not installed
# ---------------------------------------------------------------------------


def test_caddy_status_returns_none_when_not_installed() -> None:
    with patch("swarm.reverse_proxy.caddy_installed", return_value=False):
        assert reverse_proxy.caddy_status() is None


def test_caddy_status_returns_systemctl_output_when_installed() -> None:
    with (
        patch("swarm.reverse_proxy.caddy_installed", return_value=True),
        patch(
            "swarm.reverse_proxy.subprocess.run",
            return_value=_ok(stdout="● caddy.service - Caddy"),
        ),
    ):
        assert reverse_proxy.caddy_status() == "● caddy.service - Caddy"


def test_caddy_status_falls_back_to_stderr_when_stdout_empty() -> None:
    """systemctl status sometimes routes the report to stderr (e.g. when
    the service has never been activated). We surface that instead of
    None so the dashboard shows the operator what's wrong."""
    with (
        patch("swarm.reverse_proxy.caddy_installed", return_value=True),
        patch(
            "swarm.reverse_proxy.subprocess.run",
            return_value=_ok(stdout="", stderr="Unit caddy.service could not be found."),
        ),
    ):
        assert reverse_proxy.caddy_status() == "Unit caddy.service could not be found."
