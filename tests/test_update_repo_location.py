"""The updater notices when the repo has been renamed underneath it.

Every build bakes in the repo URL that was current when it shipped. A
rename does not break the update — GitHub redirects, git follows it — so
the failure is quiet: the name compiled into the binary simply stops
being true, and nothing says so.

This session watched the quiet half of that bite. A 2026.8.2.6 install
pointed at ``miopea/swarm``; after the rename to ``miopea/swarm-legacy``,
``api.github.com/repos/miopea/swarm/commits`` answered ``301`` and the
probe ran curl WITHOUT ``-L``. ``json.loads`` then succeeded on the
redirect body — a dict, not the expected list — so the commit metadata
came back empty and the update banner rendered with a stale sha and no
message, while the version probe (which raw.githubusercontent served
through the rename) kept working. Nothing errored. The banner just
quietly described the wrong commit.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from swarm.update import (
    _REPO_FULL_NAME,
    _fetch_latest_commit,
    _fetch_remote_version,
    fetch_repo_location,
    perform_update,
    repo_has_moved,
)


def _proc(stdout: bytes, returncode: int = 0) -> AsyncMock:
    mock = AsyncMock()
    mock.returncode = returncode
    mock.communicate.return_value = (stdout, b"")
    return mock


class TestRedirectsAreFollowed:
    """``-L`` is the whole fix for a rename. Guard it explicitly."""

    @pytest.mark.asyncio()
    async def test_version_probe_follows_redirects(self) -> None:
        with patch(
            "asyncio.create_subprocess_exec", return_value=_proc(b'__version__ = "9.9.9"\n')
        ) as spawn:
            await _fetch_remote_version()
        assert "-sSL" in spawn.call_args.args, (
            "without -L a renamed repo's 301 body is parsed as the file"
        )

    @pytest.mark.asyncio()
    async def test_commit_probe_follows_redirects(self) -> None:
        """The exact probe that silently lost its commit metadata."""
        with patch("asyncio.create_subprocess_exec", return_value=_proc(b"[]")) as spawn:
            await _fetch_latest_commit()
        assert "-sSL" in spawn.call_args.args

    @pytest.mark.asyncio()
    async def test_redirect_body_no_longer_masquerades_as_commit_data(self) -> None:
        """Regression: GitHub's 301 body parses as valid JSON, just wrong.

        It is a dict where a list is expected, so the old code returned
        {} and the banner lost its commit line without any error.
        """
        moved = json.dumps({"message": "Moved Permanently", "url": "https://x"}).encode()
        with patch("asyncio.create_subprocess_exec", return_value=_proc(moved)):
            info = await _fetch_latest_commit()
        assert info == {}, "a redirect body must never be mistaken for commit data"


class TestRepoLocationProbe:
    @pytest.mark.asyncio()
    async def test_reads_the_name_github_actually_served(self) -> None:
        body = json.dumps({"full_name": "miopea/swarm-legacy", "id": 1}).encode()
        with patch("asyncio.create_subprocess_exec", return_value=_proc(body)):
            assert await fetch_repo_location() == "miopea/swarm-legacy"

    @pytest.mark.asyncio()
    async def test_failure_reports_unknown_not_unmoved(self) -> None:
        with patch("asyncio.create_subprocess_exec", return_value=_proc(b"", returncode=1)):
            assert await fetch_repo_location() == ""

    @pytest.mark.asyncio()
    async def test_unexpected_shape_is_not_guessed_at(self) -> None:
        with patch("asyncio.create_subprocess_exec", return_value=_proc(b"[1, 2, 3]")):
            assert await fetch_repo_location() == ""


class TestMoveDetection:
    def test_unanswered_probe_is_not_a_move(self) -> None:
        """'' means "could not tell", which must never render as a rename."""
        assert repo_has_moved("") is False

    def test_same_repo_is_not_a_move(self) -> None:
        assert repo_has_moved(_REPO_FULL_NAME) is False

    def test_case_difference_is_not_a_move(self) -> None:
        """GitHub owners/repos are case-insensitive; a case flip is not news."""
        assert repo_has_moved(_REPO_FULL_NAME.upper()) is False

    def test_a_genuine_rename_is_reported(self) -> None:
        assert repo_has_moved("miopea/swarm") is True


class TestPostUpdateCheck:
    @pytest.mark.asyncio()
    async def test_successful_update_reports_a_rename(self, monkeypatch) -> None:
        """The user's ask: check the location AFTER the update lands.

        Uses a name from our OWN history. A rename between names this
        project has answered to is the normal supported path — it is
        reported, not refused. Refusing it would break every install
        whose baked-in URL predates the current name.
        """
        monkeypatch.setattr("swarm.update._preserve_foreign_entrypoints", list)
        monkeypatch.setattr("swarm.update._restore_foreign_entrypoints", lambda saved: None)
        monkeypatch.setattr("swarm.update._drop_reoccupied_entrypoint", list)

        async def _installed() -> str:
            return "miopea/swarm"

        monkeypatch.setattr("swarm.update.fetch_repo_location", _installed)

        install = AsyncMock()
        install.returncode = 0
        install.stdout.__aiter__.return_value = iter([b"Installed 2 executables\n"])

        lines: list[str] = []
        with patch("asyncio.create_subprocess_exec", return_value=install):
            ok, output = await perform_update(on_output=lines.append)

        assert ok is True, "a rename within our own history must not block the update"
        assert "miopea/swarm" in output
        assert any("serves it as" in line for line in lines), (
            "the rename must reach the dashboard, not just the log"
        )

    @pytest.mark.asyncio()
    async def test_a_reused_name_is_refused_before_anything_installs(self, monkeypatch) -> None:
        """The hazard behind freeing the `swarm` name.

        GitHub's rename redirect is the only reason builds carrying the
        old URL still update. It dies the moment a NEW repo claims the
        freed name — and then the same baked-in URL resolves to someone
        else's project, which would be installed straight over this hive
        with no error at any layer. Refuse, and refuse BEFORE running uv:
        refusing afterwards means the wrong package is already on disk.
        """
        monkeypatch.setattr("swarm.update._preserve_foreign_entrypoints", list)
        monkeypatch.setattr("swarm.update._restore_foreign_entrypoints", lambda saved: None)
        monkeypatch.setattr("swarm.update._drop_reoccupied_entrypoint", list)

        async def _hijacked() -> str:
            return "someone-else/swarm"

        monkeypatch.setattr("swarm.update.fetch_repo_location", _hijacked)

        with patch("asyncio.create_subprocess_exec") as spawn:
            ok, output = await perform_update()

        assert ok is False
        assert "someone-else/swarm" in output
        assert spawn.call_count == 0, "nothing may be installed from a repo that is not ours"

    @pytest.mark.asyncio()
    async def test_an_unreachable_probe_never_blocks_an_update(self, monkeypatch) -> None:
        """A network failure is not evidence of a hijacked name.

        Blocking on it would break the very migration the gate protects.
        """
        monkeypatch.setattr("swarm.update._preserve_foreign_entrypoints", list)
        monkeypatch.setattr("swarm.update._restore_foreign_entrypoints", lambda saved: None)
        monkeypatch.setattr("swarm.update._drop_reoccupied_entrypoint", list)

        async def _unreachable() -> str:
            return ""

        monkeypatch.setattr("swarm.update.fetch_repo_location", _unreachable)

        install = AsyncMock()
        install.returncode = 0
        install.stdout.__aiter__.return_value = iter([b"Installed 2 executables\n"])

        with patch("asyncio.create_subprocess_exec", return_value=install):
            ok, _ = await perform_update()

        assert ok is True

    @pytest.mark.asyncio()
    async def test_unmoved_repo_stays_quiet(self, monkeypatch) -> None:
        monkeypatch.setattr("swarm.update._preserve_foreign_entrypoints", list)
        monkeypatch.setattr("swarm.update._restore_foreign_entrypoints", lambda saved: None)
        monkeypatch.setattr("swarm.update._drop_reoccupied_entrypoint", list)

        async def _installed() -> str:
            return _REPO_FULL_NAME

        monkeypatch.setattr("swarm.update.fetch_repo_location", _installed)

        install = AsyncMock()
        install.returncode = 0
        install.stdout.__aiter__.return_value = iter([b"Installed 2 executables\n"])

        with patch("asyncio.create_subprocess_exec", return_value=install):
            ok, output = await perform_update()

        assert ok is True
        assert "serves it as" not in output, "no rename, no note"
