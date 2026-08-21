"""A killed update must not leave a package that looks installed but is not.

The chain this pins, observed end to end on a live box:

``_INSTALL_TIMEOUT`` was 120s. A real update on a real connection was
still downloading cryptography (4.5 MiB) and building red-black-tree-mod
from source when the timeout fired and ``proc.kill()`` ran. But
``uv tool install --force`` uninstalls BEFORE it installs, so the kill
landed after the old version was gone and before the data files were
written. The .py modules were there, so the daemon started and served
happily — until the first page load, which died with "Template
'dashboard.html' not found", a symptom four steps removed from its cause.

Meanwhile the dashboard reported the failure as the FIRST 200 characters
of output, which is download progress. The one line that explained it —
the timeout message — is appended last, and was never shown.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from swarm import update as up


class TestArtifactDetection:
    def test_a_complete_install_reports_nothing_missing(self) -> None:
        """Positive control: this very checkout is complete."""
        assert up.missing_install_artifacts() == []

    def test_a_missing_template_is_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The exact shape of the broken install: modules yes, data files no."""
        pkg = tmp_path / "swarm"
        (pkg / "web" / "static").mkdir(parents=True)
        (pkg / "web" / "static" / "dashboard.js").write_text("// present")
        (pkg / "web" / "templates").mkdir(parents=True)
        # dashboard.html deliberately absent
        fake = type("M", (), {"__file__": str(pkg / "__init__.py")})
        monkeypatch.setitem(__import__("sys").modules, "swarm", fake)

        missing = up.missing_install_artifacts()

        assert missing == ["web/templates/dashboard.html"]

    def test_an_unlocatable_package_invents_no_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not knowing must not be reported as broken."""
        broken = type("M", (), {})  # no __file__
        monkeypatch.setitem(__import__("sys").modules, "swarm", broken)
        assert up.missing_install_artifacts() == []


class TestTimeoutIsNotSilent:
    @pytest.mark.asyncio()
    async def test_timeout_names_itself_and_the_risk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(up, "_INSTALL_TIMEOUT", 0.01)
        monkeypatch.setattr(up, "missing_install_artifacts", lambda: ["web/templates/x.html"])

        class _Hanging:
            returncode = None

            def __init__(self) -> None:
                self.stdout = self

            def __aiter__(self):
                return self

            async def __anext__(self) -> bytes:
                import asyncio as _a

                await _a.sleep(10)  # never yields before the timeout
                raise StopAsyncIteration

            def kill(self) -> None:
                self.returncode = -9

            async def wait(self) -> int:
                return -9

        lines: list[str] = []
        with patch("asyncio.create_subprocess_exec", return_value=_Hanging()):
            ok = await up._stream_install(["uv"], lines, lines.append)

        assert ok is False
        joined = "\n".join(lines)
        assert "timed out" in joined and "killed" in joined
        assert "partly written" in joined, "the destructive consequence must be stated"
        assert "uv tool install --force" in joined, "and the recovery command given"


class TestSuccessIsVerified:
    @pytest.mark.asyncio()
    async def test_uv_exit_zero_is_not_trusted_on_its_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """uv exited 0 but the data files are missing — that is a failure.

        Trusting the exit code is what let a broken install be reported
        four steps later by the web layer instead of by the updater.
        """
        monkeypatch.setattr(up, "_preserve_foreign_entrypoints", list)
        monkeypatch.setattr(up, "_restore_foreign_entrypoints", lambda saved: None)
        monkeypatch.setattr(up, "_drop_reoccupied_entrypoint", list)

        async def _location() -> str:
            return up._REPO_FULL_NAME

        monkeypatch.setattr(up, "fetch_repo_location", _location)
        monkeypatch.setattr(up, "missing_install_artifacts", lambda: ["web/templates/x.html"])

        install = AsyncMock()
        install.returncode = 0
        install.stdout.__aiter__.return_value = iter([b"Installed 44 packages\n"])

        with patch("asyncio.create_subprocess_exec", return_value=install):
            ok, output = await up.perform_update()

        assert ok is False, "a package missing its templates is not a successful update"
        assert "did not finish" in output


class TestFailureIsRecoverable:
    """A failed update must leave the full output somewhere on disk.

    Both UI callers truncated to the FIRST 200 characters — which is uv's
    download progress, while the line explaining the failure is appended
    last. An operator was shown progress and told it was an error, on two
    separate pages, and the real cause could not be recovered from the
    browser at all.
    """

    @pytest.mark.asyncio()
    async def test_a_failed_install_writes_the_full_output(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))

        async def _location() -> str:
            return up._REPO_FULL_NAME

        monkeypatch.setattr(up, "fetch_repo_location", _location)
        monkeypatch.setattr(up, "_preserve_foreign_entrypoints", list)

        async def _fail(cmd, output_lines, emit):
            output_lines.append("Resolved 44 packages in 848ms")
            output_lines.append("Downloading cryptography (4.5MiB)")
            output_lines.append("error: the actual cause, appended last")
            return False

        monkeypatch.setattr(up, "_stream_install", _fail)

        ok, output = await up.perform_update()

        assert ok is False
        log = tmp_path / up.UPDATE_LOG
        assert log.is_file(), "the full output must survive the UI's truncation"
        assert "the actual cause, appended last" in log.read_text()
        assert str(log) in output, "and the operator must be told where it is"

    @pytest.mark.asyncio()
    async def test_the_log_is_rewritten_not_appended(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A stale log reads as evidence about the run you just did."""
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
        (tmp_path / up.UPDATE_LOG).write_text("OUTPUT FROM A PREVIOUS ATTEMPT")

        up._write_update_log("the new attempt")

        assert (tmp_path / up.UPDATE_LOG).read_text() == "the new attempt"


class TestGitFailureExplainsItself:
    """uv wraps git and swallows git's stderr.

    All the operator sees is "Git operation failed / process didn't exit
    successfully" — no cause, nothing to act on. Observed on a real box
    where an `insteadOf` rule sent a PUBLIC repository down an
    authenticated SSH path and the systemd daemon had no ssh-agent.
    """

    UV_OUTPUT = (
        "Updating https://github.com/miopea/swarm.git (HEAD)\n"
        "error: Git operation failed\n"
        "  Caused by: failed to fetch into: /tmp/.tmpX/git-v0/db/cf0ffb2a\n"
        "  Caused by: process didn't exit successfully"
    )

    def test_a_rewrite_is_named_when_the_fetch_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            up,
            "github_url_rewrites",
            lambda: ["url.git@github.com:.insteadof https://github.com/"],
        )

        hint = up.git_auth_hint(self.UV_OUTPUT)

        assert hint is not None
        assert "insteadof" in hint.lower(), "the actual rule must be quoted back"
        assert "uv tool install --force" in hint, "and a way forward given"

    def test_no_rewrite_still_gives_a_reproduction_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a rewrite we cannot name the cause — so hand over the
        command that reveals it, rather than guessing."""
        monkeypatch.setattr(up, "github_url_rewrites", list)

        hint = up.git_auth_hint(self.UV_OUTPUT)

        assert hint is not None
        assert "git ls-remote" in hint

    def test_a_successful_run_is_not_annotated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(up, "github_url_rewrites", list)
        assert up.git_auth_hint("Installed 44 packages in 121ms") is None

    def test_the_rewrite_probe_ignores_non_github_rules(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rewrite for some other host is not evidence about this fetch."""
        import subprocess as sp

        def _fake_run(*a: object, **kw: object) -> sp.CompletedProcess[str]:
            return sp.CompletedProcess(
                [], 0, stdout="url.git@gitlab.com:.insteadof https://gitlab.com/\n", stderr=""
            )

        monkeypatch.setattr(up.subprocess, "run", _fake_run)
        assert up.github_url_rewrites() == []

    def test_the_rewrite_probe_survives_a_missing_git(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*a: object, **kw: object) -> None:
            raise FileNotFoundError("git")

        monkeypatch.setattr(up.subprocess, "run", _boom)
        assert up.github_url_rewrites() == []
