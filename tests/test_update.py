"""Tests for swarm.update — GitHub-based update detection and dev-mode re-exec."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from swarm.update import (
    _VERSION_RE,
    UpdateResult,
    _fetch_latest_commit,
    _fetch_remote_version,
    _get_installed_version,
    _hash_source_tree,
    _is_dev_install,
    _local_head_sha,
    _read_cache,
    _version_tuple,
    _write_cache,
    check_for_update,
    check_for_update_sync,
    get_local_source_path,
    perform_update,
    reinstall_from_local_source,
    sync_team_config,
    update_result_to_dict,
)


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    """Redirect cache file to a temp directory."""
    cache_file = tmp_path / "update_cache.json"
    monkeypatch.setattr("swarm.update._CACHE_DIR", tmp_path)
    monkeypatch.setattr("swarm.update._CACHE_FILE", cache_file)
    return cache_file


# --- _get_installed_version ---


def test_get_installed_version():
    """Returns the importlib metadata version."""
    with patch("importlib.metadata.version", return_value="1.2.3"):
        assert _get_installed_version() == "1.2.3"


def test_get_installed_version_fallback():
    """Falls back to __version__ on PackageNotFoundError."""
    import importlib.metadata

    with (
        patch("importlib.metadata.version", side_effect=importlib.metadata.PackageNotFoundError),
        patch("swarm.__version__", "0.9.0"),
    ):
        assert _get_installed_version() == "0.9.0"


# --- _is_dev_install ---


def test_is_dev_install_editable():
    """Editable install detected via direct_url.json."""
    import json as _json

    url_data = _json.dumps(
        {"url": "file:///home/user/projects/swarm", "dir_info": {"editable": True}},
    )
    mock_dist = patch(
        "importlib.metadata.distribution",
        return_value=type("D", (), {"read_text": lambda self, name: url_data})(),
    )
    with mock_dist:
        assert _is_dev_install() is True


def test_is_dev_install_file_url():
    """Local file:// install (non-editable) detected as dev."""
    import json as _json

    url_data = _json.dumps({"url": "file:///home/user/projects/swarm", "dir_info": {}})
    mock_dist = patch(
        "importlib.metadata.distribution",
        return_value=type("D", (), {"read_text": lambda self, name: url_data})(),
    )
    with mock_dist:
        assert _is_dev_install() is True


def test_is_dev_install_not_dev():
    """Regular PyPI install → not dev."""
    mock_dist = patch(
        "importlib.metadata.distribution",
        return_value=type("D", (), {"read_text": lambda self, name: None})(),
    )
    with mock_dist:
        assert _is_dev_install() is False


def test_is_dev_install_not_found():
    """Package not found → not dev."""
    import importlib.metadata

    exc = importlib.metadata.PackageNotFoundError
    with patch("importlib.metadata.distribution", side_effect=exc):
        assert _is_dev_install() is False


# --- _VERSION_RE ---


def test_version_regex_double_quotes():
    text = '__version__ = "1.2.3"'
    m = _VERSION_RE.search(text)
    assert m and m.group(1) == "1.2.3"


def test_version_regex_single_quotes():
    text = "__version__ = '2.0.0-beta'"
    m = _VERSION_RE.search(text)
    assert m and m.group(1) == "2.0.0-beta"


def test_version_regex_no_match():
    text = "version = 123"
    assert _VERSION_RE.search(text) is None


# --- _version_tuple ---


def test_version_tuple_standard():
    assert _version_tuple("2026.2.21.3") == (2026, 2, 21, 3)


def test_version_tuple_short():
    assert _version_tuple("1.0.0") == (1, 0, 0)


def test_version_tuple_comparison():
    assert _version_tuple("2026.2.21.3") > _version_tuple("2026.2.21.2")
    assert not _version_tuple("2026.2.21.2") > _version_tuple("2026.2.21.3")
    assert _version_tuple("2.0.0") > _version_tuple("1.0.0")


def test_version_tuple_non_numeric_suffix():
    """Non-numeric segments cause early stop (3-beta is not parseable)."""
    assert _version_tuple("1.2.3-beta") == (1, 2)


# --- _fetch_remote_version ---


@pytest.mark.asyncio()
async def test_fetch_remote_version_success():
    """Mocked curl returning a valid __init__.py."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b'__version__ = "2.0.0"\n', b"")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        version, error = await _fetch_remote_version()
    assert version == "2.0.0"
    assert error == ""


@pytest.mark.asyncio()
async def test_fetch_remote_version_network_error():
    """Mocked curl failure → error string."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = (b"", b"Connection refused")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        version, error = await _fetch_remote_version()
    assert version == ""
    assert "Connection refused" in error


@pytest.mark.asyncio()
async def test_fetch_remote_version_parse_error():
    """Curl succeeds but content has no __version__."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"# empty file\n", b"")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        version, error = await _fetch_remote_version()
    assert version == ""
    assert "could not parse" in error


# --- _fetch_latest_commit ---


@pytest.mark.asyncio()
async def test_fetch_latest_commit_success():
    """Mocked GitHub API returning commit info including parent SHA."""
    commit_json = json.dumps(
        [
            {
                "sha": "abcdef1234567890",
                "parents": [{"sha": "parent1234567890"}],
                "commit": {
                    "message": "fix: something important\n\ndetails",
                    "committer": {"date": "2025-01-15T10:30:00Z"},
                },
            }
        ]
    ).encode()
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (commit_json, b"")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        info = await _fetch_latest_commit()
    assert info["sha"] == "abcdef12"
    assert info["full_sha"] == "abcdef1234567890"
    assert info["parent_sha"] == "parent12"
    assert info["message"] == "fix: something important"
    assert info["date"] == "2025-01-15T10:30:00Z"


@pytest.mark.asyncio()
async def test_fetch_latest_commit_failure():
    """Mocked failure → empty dict."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = (b"", b"error")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        info = await _fetch_latest_commit()
    assert info == {}


# --- Cache ---


def test_cache_round_trip(cache_dir):
    """Write + read preserves data."""
    result = UpdateResult(
        available=True,
        current_version="1.0.0",
        remote_version="2.0.0",
        commit_sha="abc123",
        checked_at=time.time(),
    )
    _write_cache(result)
    loaded = _read_cache()
    assert loaded is not None
    assert loaded.available is True
    assert loaded.current_version == "1.0.0"
    assert loaded.remote_version == "2.0.0"


def test_cache_expired(cache_dir):
    """Old checked_at → returns None."""
    result = UpdateResult(
        available=True,
        current_version="1.0.0",
        remote_version="2.0.0",
        checked_at=time.time() - 100000,  # way past TTL
    )
    _write_cache(result)
    assert _read_cache() is None


def test_cache_missing(cache_dir):
    """No cache file → returns None."""
    assert _read_cache() is None


# --- check_for_update ---


@pytest.mark.asyncio()
async def test_check_uses_cache(cache_dir):
    """Fresh cache → no network call."""
    result = UpdateResult(
        available=True,
        current_version="1.0.0",
        remote_version="2.0.0",
        checked_at=time.time(),
    )
    _write_cache(result)

    with patch("swarm.update._fetch_remote_version") as mock_fetch:
        got = await check_for_update()
    mock_fetch.assert_not_called()
    assert got.available is True


@pytest.mark.asyncio()
async def test_check_force_bypasses_cache(cache_dir):
    """force=True → network call made even with fresh cache."""
    result = UpdateResult(
        available=False,
        current_version="1.0.0",
        remote_version="1.0.0",
        checked_at=time.time(),
    )
    _write_cache(result)

    with (
        patch("swarm.update._fetch_remote_version", return_value=("2.0.0", "")),
        patch("swarm.update._fetch_latest_commit", return_value={}),
        patch("swarm.update._get_installed_version", return_value="1.0.0"),
    ):
        got = await check_for_update(force=True)
    assert got.available is True
    assert got.remote_version == "2.0.0"


@pytest.mark.asyncio()
async def test_check_available(cache_dir):
    """Remote newer than installed → available=True."""
    with (
        patch("swarm.update._fetch_remote_version", return_value=("2.0.0", "")),
        patch("swarm.update._fetch_latest_commit", return_value={"sha": "abc"}),
        patch("swarm.update._get_installed_version", return_value="1.0.0"),
    ):
        got = await check_for_update(force=True)
    assert got.available is True
    assert got.remote_version == "2.0.0"
    assert got.current_version == "1.0.0"


@pytest.mark.asyncio()
async def test_check_up_to_date(cache_dir):
    """Same versions → available=False."""
    with (
        patch("swarm.update._fetch_remote_version", return_value=("1.0.0", "")),
        patch("swarm.update._fetch_latest_commit", return_value={}),
        patch("swarm.update._get_installed_version", return_value="1.0.0"),
    ):
        got = await check_for_update(force=True)
    assert got.available is False


@pytest.mark.asyncio()
async def test_check_remote_older_not_available(cache_dir):
    """Remote older than installed → available=False (regression)."""
    with (
        patch("swarm.update._fetch_remote_version", return_value=("2026.2.21.2", "")),
        patch("swarm.update._fetch_latest_commit", return_value={}),
        patch("swarm.update._get_installed_version", return_value="2026.2.21.3"),
    ):
        got = await check_for_update(force=True)
    assert got.available is False


@pytest.mark.asyncio()
async def test_check_pins_remote_fetch_to_commit_sha(cache_dir):
    """Regression: check_for_update must pass the commit SHA to
    _fetch_remote_version so that raw.githubusercontent.com's ~5-minute
    /main/ CDN cache can't serve a stale version string after a fresh
    version-bump commit.
    """
    from unittest.mock import AsyncMock as _AsyncMock

    fetch_mock = _AsyncMock(return_value=("2.0.0", ""))
    with (
        patch("swarm.update._fetch_remote_version", fetch_mock),
        patch(
            "swarm.update._fetch_latest_commit",
            return_value={
                "sha": "1fd13ac0",
                "full_sha": "1fd13ac0abcdef0123456789abcdef0123456789",
                "parent_sha": "07a0c4d0",
                "message": "chore(version): bump to 2.0.0",
                "date": "2026-04-05T11:00:00Z",
            },
        ),
        patch("swarm.update._get_installed_version", return_value="1.0.0"),
    ):
        got = await check_for_update(force=True)
    assert got.available is True
    # The raw-file fetch must be pinned to the exact SHA returned by the
    # commits API — not the mutable /main/ URL.
    fetch_mock.assert_called_once_with("1fd13ac0abcdef0123456789abcdef0123456789")


@pytest.mark.asyncio()
async def test_check_falls_back_when_commit_api_unreachable(cache_dir):
    """If the GitHub commits API fails, we still fall back to /main/ fetch."""
    from unittest.mock import AsyncMock as _AsyncMock

    fetch_mock = _AsyncMock(return_value=("2.0.0", ""))
    with (
        patch("swarm.update._fetch_remote_version", fetch_mock),
        patch("swarm.update._fetch_latest_commit", return_value={}),
        patch("swarm.update._get_installed_version", return_value="1.0.0"),
    ):
        got = await check_for_update(force=True)
    assert got.available is True
    # Empty SHA → falls back to the mutable /main/ URL.
    fetch_mock.assert_called_once_with("")


@pytest.mark.asyncio()
async def test_fetch_remote_version_uses_sha_pinned_url():
    """_fetch_remote_version with a SHA must curl the SHA-pinned raw URL."""
    captured: dict[str, tuple[object, ...]] = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        mock = AsyncMock()
        mock.returncode = 0
        mock.communicate.return_value = (b'__version__ = "2.0.0"', b"")
        return mock

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        version, error = await _fetch_remote_version("deadbeefcafe")
    assert error == ""
    assert version == "2.0.0"
    assert any("/deadbeefcafe/src/swarm/__init__.py" in str(a) for a in captured["args"])


@pytest.mark.asyncio()
async def test_check_error(cache_dir):
    """Network error → error captured, available=False."""
    with (
        patch("swarm.update._fetch_remote_version", return_value=("", "timeout")),
        patch("swarm.update._get_installed_version", return_value="1.0.0"),
    ):
        got = await check_for_update(force=True)
    assert got.available is False
    assert got.error == "timeout"


@pytest.mark.asyncio()
async def test_check_includes_is_dev(cache_dir):
    """check_for_update populates is_dev from _is_dev_install."""
    with (
        patch("swarm.update._fetch_remote_version", return_value=("2.0.0", "")),
        patch("swarm.update._fetch_latest_commit", return_value={}),
        patch("swarm.update._get_installed_version", return_value="1.0.0"),
        patch("swarm.update._is_dev_install", return_value=True),
        patch("swarm.update._local_head_sha", return_value=""),
    ):
        got = await check_for_update(force=True)
    assert got.is_dev is True


# --- _local_head_sha ---


@pytest.mark.asyncio()
async def test_local_head_sha_success():
    """Returns short SHA when git succeeds."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"abcd1234\n", b"")

    with (
        patch("swarm.update.get_local_source_path", return_value="/home/user/projects/swarm"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        sha = await _local_head_sha()
    assert sha == "abcd1234"


@pytest.mark.asyncio()
async def test_local_head_sha_no_source():
    """No local source path → empty string."""
    with patch("swarm.update.get_local_source_path", return_value=None):
        sha = await _local_head_sha()
    assert sha == ""


@pytest.mark.asyncio()
async def test_local_head_sha_git_fails():
    """Git failure → empty string."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 128
    mock_proc.communicate.return_value = (b"", b"not a git repo")

    with (
        patch("swarm.update.get_local_source_path", return_value="/tmp/nope"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        sha = await _local_head_sha()
    assert sha == ""


# --- Dev-mode SHA suppression in check_for_update ---


@pytest.mark.asyncio()
async def test_check_dev_sha_matches_remote(cache_dir):
    """Dev mode + local SHA matches remote SHA → available=False."""
    with (
        patch("swarm.update._fetch_remote_version", return_value=("2.0.0", "")),
        patch(
            "swarm.update._fetch_latest_commit",
            return_value={"sha": "abcd1234", "parent_sha": "parent12", "message": "", "date": ""},
        ),
        patch("swarm.update._get_installed_version", return_value="1.0.0"),
        patch("swarm.update._is_dev_install", return_value=True),
        patch("swarm.update._local_head_sha", return_value="abcd1234"),
    ):
        got = await check_for_update(force=True)
    assert got.available is False
    assert got.is_dev is True


@pytest.mark.asyncio()
async def test_check_dev_sha_matches_parent(cache_dir):
    """Dev mode + local SHA matches parent (version-bump-only) → available=False."""
    with (
        patch("swarm.update._fetch_remote_version", return_value=("2.0.0", "")),
        patch(
            "swarm.update._fetch_latest_commit",
            return_value={"sha": "bumped12", "parent_sha": "abcd1234", "message": "", "date": ""},
        ),
        patch("swarm.update._get_installed_version", return_value="1.0.0"),
        patch("swarm.update._is_dev_install", return_value=True),
        patch("swarm.update._local_head_sha", return_value="abcd1234"),
    ):
        got = await check_for_update(force=True)
    assert got.available is False
    assert got.is_dev is True


@pytest.mark.asyncio()
async def test_check_dev_sha_differs(cache_dir):
    """Dev mode + local SHA differs from remote → normal version comparison."""
    with (
        patch("swarm.update._fetch_remote_version", return_value=("2.0.0", "")),
        patch(
            "swarm.update._fetch_latest_commit",
            return_value={"sha": "remote12", "parent_sha": "parent12", "message": "", "date": ""},
        ),
        patch("swarm.update._get_installed_version", return_value="1.0.0"),
        patch("swarm.update._is_dev_install", return_value=True),
        patch("swarm.update._local_head_sha", return_value="localxyz"),
    ):
        got = await check_for_update(force=True)
    assert got.available is True  # 2.0.0 > 1.0.0
    assert got.is_dev is True


# --- check_for_update_sync ---


def test_sync_returns_cache(cache_dir):
    """Cache exists and fresh → returns it."""
    result = UpdateResult(
        available=True,
        current_version="1.0.0",
        remote_version="2.0.0",
        checked_at=time.time(),
    )
    _write_cache(result)
    got = check_for_update_sync()
    assert got is not None
    assert got.available is True


def test_sync_returns_none(cache_dir):
    """No cache → returns None."""
    assert check_for_update_sync() is None


# --- perform_update ---


async def _async_lines_gen(lines: list[bytes]):
    for line in lines:
        yield line


def _async_lines_iter(lines: list[bytes]):
    """Return an async iterator over *lines*, mimicking proc.stdout."""
    return _async_lines_gen(lines)


@pytest.mark.asyncio()
async def test_perform_update_success(cache_dir):
    """Single uv command succeeds → (True, output)."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.stdout = _async_lines_iter([b"Resolved 5 packages\n", b"Installed swarm\n"])
    mock_proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        success, output = await perform_update()
    assert success is True
    assert "Installed swarm" in output


@pytest.mark.asyncio()
async def test_perform_update_failure(cache_dir):
    """uv command returns non-zero → (False, output)."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.stdout = _async_lines_iter([b"error: not found\n"])
    mock_proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        success, output = await perform_update()
    assert success is False
    assert "error" in output


@pytest.mark.asyncio()
async def test_perform_update_on_output_callback(cache_dir):
    """on_output callback receives each line of output."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.stdout = _async_lines_iter([b"line one\n", b"line two\n"])
    mock_proc.wait = AsyncMock()

    collected: list[str] = []
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        success, _ = await perform_update(on_output=collected.append)
    assert success is True
    # Should have the emit labels plus the two output lines plus "Update complete!"
    assert any("line one" in c for c in collected)
    assert any("line two" in c for c in collected)
    assert any("Update complete" in c for c in collected)


# --- update_result_to_dict ---


def test_update_result_to_dict():
    result = UpdateResult(
        available=True,
        current_version="1.0.0",
        remote_version="2.0.0",
        commit_sha="abc",
        checked_at=1000.0,
        is_dev=True,
    )
    d = update_result_to_dict(result)
    assert d["available"] is True
    assert d["current_version"] == "1.0.0"
    assert d["remote_version"] == "2.0.0"
    assert d["commit_sha"] == "abc"
    assert d["checked_at"] == 1000.0
    assert d["is_dev"] is True


# --- /action/update-and-restart endpoint ---


@pytest.fixture
def _web_app():
    """Create a minimal aiohttp app with the update-and-restart route."""
    import asyncio
    from unittest.mock import MagicMock

    from aiohttp import web

    from swarm.web.app import handle_action_update_and_restart

    app = web.Application()
    app.router.add_post("/action/update-and-restart", handle_action_update_and_restart)

    daemon = MagicMock()
    daemon.broadcast_ws = MagicMock()
    app["daemon"] = daemon
    app["shutdown_event"] = asyncio.Event()
    app["restart_flag"] = {"requested": False}

    return app


@pytest.mark.asyncio()
async def test_update_and_restart_success(_web_app):
    """Successful update sets restart_requested and shutdown_event."""
    from aiohttp.test_utils import TestClient, TestServer

    app = _web_app
    with patch("swarm.update.perform_update", return_value=(True, "ok")):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/action/update-and-restart",
                headers={"X-Requested-With": "SwarmWeb"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is True
            assert data["restarting"] is True
            assert app["restart_flag"]["requested"] is True
            assert app["shutdown_event"].is_set()
            app["daemon"].broadcast_ws.assert_called_with({"type": "update_restarting"})


@pytest.mark.asyncio()
async def test_update_and_restart_failure(_web_app):
    """Failed update returns error and does NOT set restart flag."""
    from aiohttp.test_utils import TestClient, TestServer

    app = _web_app
    with patch("swarm.update.perform_update", return_value=(False, "install error")):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/action/update-and-restart",
                headers={"X-Requested-With": "SwarmWeb"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is False
            assert "install error" in data["output"]
            assert app["restart_flag"]["requested"] is False
            assert not app["shutdown_event"].is_set()


# --- get_local_source_path ---


def test_get_local_source_path_file_url():
    """Local file:// install returns the filesystem path."""
    import json as _json

    url_data = _json.dumps({"url": "file:///home/user/projects/swarm", "dir_info": {}})
    mock_dist = patch(
        "importlib.metadata.distribution",
        return_value=type("D", (), {"read_text": lambda self, name: url_data})(),
    )
    with mock_dist:
        assert get_local_source_path() == "/home/user/projects/swarm"


def test_get_local_source_path_editable():
    """Editable install returns None (changes already live)."""
    import json as _json

    url_data = _json.dumps(
        {"url": "file:///home/user/projects/swarm", "dir_info": {"editable": True}},
    )
    mock_dist = patch(
        "importlib.metadata.distribution",
        return_value=type("D", (), {"read_text": lambda self, name: url_data})(),
    )
    with mock_dist:
        assert get_local_source_path() is None


def test_get_local_source_path_git_url():
    """Git install returns None."""
    import json as _json

    url_data = _json.dumps({"url": "https://github.com/user/swarm.git", "vcs_info": {}})
    mock_dist = patch(
        "importlib.metadata.distribution",
        return_value=type("D", (), {"read_text": lambda self, name: url_data})(),
    )
    with mock_dist:
        assert get_local_source_path() is None


def test_get_local_source_path_no_direct_url():
    """PyPI install (no direct_url.json) returns None."""
    mock_dist = patch(
        "importlib.metadata.distribution",
        return_value=type("D", (), {"read_text": lambda self, name: None})(),
    )
    with mock_dist:
        assert get_local_source_path() is None


def test_get_local_source_path_not_found():
    """Package not found returns None."""
    import importlib.metadata

    with patch(
        "importlib.metadata.distribution",
        side_effect=importlib.metadata.PackageNotFoundError,
    ):
        assert get_local_source_path() is None


# --- reinstall_from_local_source ---


@pytest.mark.asyncio()
async def test_reinstall_noop_when_not_local():
    """No local source path → no-op (True, "")."""
    with patch("swarm.update.get_local_source_path", return_value=None):
        success, output = await reinstall_from_local_source()
    assert success is True
    assert output == ""


@pytest.mark.asyncio()
async def test_reinstall_success():
    """Local source path → runs 3-step uninstall/clean/install and succeeds."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.stdout = _async_lines_iter([b"Resolved 5 packages\n", b"Installed swarm\n"])
    mock_proc.wait = AsyncMock()

    with (
        patch("swarm.update.get_local_source_path", return_value="/home/user/projects/swarm"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
    ):
        success, output = await reinstall_from_local_source()

    assert success is True
    assert "Installed swarm" in output
    # Three steps: uninstall, cache clean, install
    assert mock_exec.call_count == 3
    install_args = mock_exec.call_args_list[2][0]
    assert "/home/user/projects/swarm" in install_args
    assert "--no-cache" in install_args


@pytest.mark.asyncio()
async def test_reinstall_failure():
    """Install step fails → (False, output). Cleanup steps failing is tolerated."""
    # All steps return rc=1; only the install step (required=True) causes failure
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.stdout = _async_lines_iter([b"error: build failed\n"])
    mock_proc.wait = AsyncMock()

    with (
        patch("swarm.update.get_local_source_path", return_value="/home/user/projects/swarm"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        success, output = await reinstall_from_local_source()

    assert success is False
    assert "error" in output


@pytest.mark.asyncio()
async def test_reinstall_on_output_callback():
    """on_output callback receives each line."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.stdout = _async_lines_iter([b"building\n"])
    mock_proc.wait = AsyncMock()

    collected: list[str] = []
    with (
        patch("swarm.update.get_local_source_path", return_value="/tmp/swarm"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        success, _ = await reinstall_from_local_source(on_output=collected.append)

    assert success is True
    assert any("Reinstalling from local source" in c for c in collected)
    assert any("building" in c for c in collected)
    assert any("Local reinstall complete" in c for c in collected)


# --- Dev mode: SWARM_DEV re-exec and update skip ---


@pytest.mark.asyncio()
async def test_dev_mode_skips_update_check(monkeypatch):
    """SWARM_DEV=1 → _check_for_updates returns immediately without calling check_for_update."""
    monkeypatch.setenv("SWARM_DEV", "1")

    from swarm.server.daemon import SwarmDaemon

    with patch("swarm.server.daemon.SwarmDaemon.__init__", return_value=None):
        daemon = SwarmDaemon.__new__(SwarmDaemon)

    # Invoke _check_for_updates directly
    with patch("swarm.update.check_for_update") as mock_check:
        await daemon._check_for_updates()
    mock_check.assert_not_called()


def test_dev_reexec_when_installed(monkeypatch):
    """SWARM_DEV=1 + installed tool (get_local_source_path returns path) → os.execvp called."""
    monkeypatch.setenv("SWARM_DEV", "1")
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr("sys.argv", ["swarm", "serve"])

    from click.testing import CliRunner

    from swarm.cli import main

    with (
        patch("swarm.cli.load_config") as mock_cfg,
        patch("swarm.update.get_local_source_path", return_value="/home/user/projects/swarm"),
        patch("os.chdir") as mock_chdir,
        patch("os.execvp") as mock_execvp,
    ):
        mock_cfg.return_value.port = 9090
        runner = CliRunner()
        runner.invoke(main, ["serve"])

    mock_chdir.assert_called_once_with("/home/user/projects/swarm")
    mock_execvp.assert_called_once_with("uv", ["uv", "run", "swarm", "serve"])


def test_no_reexec_when_already_dev(monkeypatch):
    """SWARM_DEV=1 + editable install (get_local_source_path returns None) → no re-exec."""
    monkeypatch.setenv("SWARM_DEV", "1")

    from click.testing import CliRunner

    from swarm.cli import main

    with (
        patch("swarm.cli.load_config") as mock_cfg,
        patch("swarm.update.get_local_source_path", return_value=None),
        patch("os.execvp") as mock_execvp,
        patch("swarm.cli.setup_logging"),
        # Close the coroutine instead of dropping it: `serve` builds
        # ``run_daemon(...)`` before handing it to ``asyncio.run``, and a bare
        # mock never consumes it — Python then reports "coroutine 'run_daemon'
        # was never awaited" against whatever test triggers the collection.
        patch("asyncio.run", side_effect=lambda coro, *a, **kw: coro.close()),
    ):
        mock_cfg.return_value.port = 9090
        mock_cfg.return_value.log_level = "WARNING"
        mock_cfg.return_value.log_file = None
        runner = CliRunner()
        runner.invoke(main, ["serve"])

    mock_execvp.assert_not_called()


# --- get_source_git_sha / build_sha ---


def test_get_source_git_sha_returns_8_char_hex():
    """In a real git repo (this project), returns an 8-char hex string."""
    from swarm.update import get_source_git_sha

    sha = get_source_git_sha()
    assert len(sha) == 8
    assert all(c in "0123456789abcdef" for c in sha)


def test_get_source_git_sha_no_git():
    """When git fails, returns empty string."""
    from swarm.update import get_source_git_sha

    with patch("subprocess.run", side_effect=FileNotFoundError("no git")):
        # Also patch the .git walk to fail
        with patch("pathlib.Path.exists", return_value=False):
            sha = get_source_git_sha()
    assert sha == ""


def test_build_sha_includes_source_hash(monkeypatch):
    """build_sha() includes git SHA + source content hash."""
    from swarm.update import build_sha

    monkeypatch.setattr("swarm.update._BUILD_SHA", "")
    with (
        patch("swarm.update.get_source_git_sha", return_value="deadbeef") as mock_git,
        patch("swarm.update._hash_source_tree", return_value="a1b2c3d4") as mock_hash,
    ):
        result = build_sha()
        assert result == "deadbeef+a1b2c3d4"
        mock_git.assert_called_once()
        mock_hash.assert_called_once()

    # Cleanup
    monkeypatch.setattr("swarm.update._BUILD_SHA", "")


def test_build_sha_cached(monkeypatch):
    """build_sha() caches the result in _BUILD_SHA."""
    from swarm.update import build_sha

    # Reset the cache
    monkeypatch.setattr("swarm.update._BUILD_SHA", "")
    with (
        patch("swarm.update.get_source_git_sha", return_value="deadbeef"),
        patch("swarm.update._hash_source_tree", return_value="a1b2c3d4"),
    ):
        result1 = build_sha()
        assert result1 == "deadbeef+a1b2c3d4"

    # Second call uses cached value — no functions called
    monkeypatch.setattr("swarm.update._BUILD_SHA", "deadbeef+a1b2c3d4")
    with (
        patch("swarm.update.get_source_git_sha") as mock_git,
        patch("swarm.update._hash_source_tree") as mock_hash,
    ):
        result2 = build_sha()
        assert result2 == "deadbeef+a1b2c3d4"
        mock_git.assert_not_called()
        mock_hash.assert_not_called()

    # Cleanup
    monkeypatch.setattr("swarm.update._BUILD_SHA", "")


def test_build_sha_no_git(monkeypatch):
    """build_sha() returns just source hash when git is unavailable."""
    from swarm.update import build_sha

    monkeypatch.setattr("swarm.update._BUILD_SHA", "")
    with (
        patch("swarm.update.get_source_git_sha", return_value=""),
        patch("swarm.update._hash_source_tree", return_value="a1b2c3d4"),
    ):
        result = build_sha()
        assert result == "a1b2c3d4"

    # Cleanup
    monkeypatch.setattr("swarm.update._BUILD_SHA", "")


def test_hash_source_tree_changes_with_content(tmp_path, monkeypatch):
    """_hash_source_tree returns different hashes when file contents change."""
    # Create a fake package directory
    pkg_dir = tmp_path / "swarm"
    pkg_dir.mkdir()
    init_file = pkg_dir / "__init__.py"
    init_file.write_text("# version 1")

    mock_swarm = type("M", (), {"__file__": str(init_file)})()
    with patch.dict("sys.modules", {"swarm": mock_swarm}):
        hash1 = _hash_source_tree()

    # Change file contents
    init_file.write_text("# version 2")
    with patch.dict("sys.modules", {"swarm": mock_swarm}):
        hash2 = _hash_source_tree()

    assert hash1 != hash2
    assert len(hash1) == 8
    assert len(hash2) == 8


# --- sync_team_config ---


@pytest.mark.asyncio()
async def test_sync_team_config_repo_not_found(tmp_path, monkeypatch):
    """No repo on disk → silent skip, no subprocess."""
    monkeypatch.setattr(
        "swarm.update._TEAM_CONFIG_CANDIDATES",
        (tmp_path / "nonexistent",),
    )
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        await sync_team_config()
    mock_exec.assert_not_called()


@pytest.mark.asyncio()
async def test_sync_team_config_runs_install_sh(tmp_path, monkeypatch):
    """Repo found → runs 'yes | install.sh' as async subprocess."""
    repo = tmp_path / "claude-team-config"
    repo.mkdir()
    install_sh = repo / "install.sh"
    install_sh.write_text("#!/bin/bash\necho done")
    install_sh.chmod(0o755)

    monkeypatch.setattr("swarm.update._TEAM_CONFIG_CANDIDATES", (repo,))

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.stdout = AsyncMock()
    mock_proc.stdout.read = AsyncMock(return_value=b"done\n")
    mock_proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        await sync_team_config()

    mock_exec.assert_called_once()
    call_args = mock_exec.call_args
    assert call_args[0][0] == "bash"
    assert call_args[0][1] == "-c"
    assert "yes" in call_args[0][2]
    assert str(install_sh) in call_args[0][2]
    assert call_args[1]["cwd"] == str(repo)


@pytest.mark.asyncio()
async def test_sync_team_config_nonzero_exit(tmp_path, monkeypatch):
    """install.sh exits non-zero → warning logged, no exception raised."""
    repo = tmp_path / "claude-team-config"
    repo.mkdir()
    (repo / "install.sh").write_text("#!/bin/bash\nexit 1")
    (repo / "install.sh").chmod(0o755)

    monkeypatch.setattr("swarm.update._TEAM_CONFIG_CANDIDATES", (repo,))

    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.stdout = AsyncMock()
    mock_proc.stdout.read = AsyncMock(return_value=b"error\n")
    mock_proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await sync_team_config()  # should not raise


@pytest.mark.asyncio()
async def test_sync_team_config_subprocess_exception(tmp_path, monkeypatch):
    """Subprocess creation fails → warning logged, no exception raised."""
    repo = tmp_path / "claude-team-config"
    repo.mkdir()
    (repo / "install.sh").write_text("#!/bin/bash\necho ok")
    (repo / "install.sh").chmod(0o755)

    monkeypatch.setattr("swarm.update._TEAM_CONFIG_CANDIDATES", (repo,))

    with patch("asyncio.create_subprocess_exec", side_effect=OSError("no bash")):
        await sync_team_config()  # should not raise
