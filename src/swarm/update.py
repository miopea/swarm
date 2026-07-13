"""GitHub-based update detection for Swarm.

Compares the installed version against the latest ``__version__`` on GitHub
main.  Results are cached to ``~/.swarm/update_cache.json`` with a 24-hour
TTL so that startup stays fast (the CLI banner reads cache only).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

_CACHE_DIR = Path.home() / ".swarm"
_CACHE_FILE = _CACHE_DIR / "update_cache.json"
_CACHE_TTL = 86400  # 24 hours

_GITHUB_RAW_URL = "https://raw.githubusercontent.com/miopea/swarm/main/src/swarm/__init__.py"
_GITHUB_RAW_AT_SHA = "https://raw.githubusercontent.com/miopea/swarm/{sha}/src/swarm/__init__.py"
_GITHUB_API_COMMITS_URL = "https://api.github.com/repos/miopea/swarm/commits?per_page=1"
_VERSION_RE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')

_CURL_TIMEOUT = "10"  # seconds (string for CLI arg)
_INSTALL_TIMEOUT = 120  # seconds

_INSTALL_SOURCE = "git+https://github.com/miopea/swarm.git"


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse a dotted version string into a tuple of ints for comparison."""
    parts: list[int] = []
    for segment in v.split("."):
        try:
            parts.append(int(segment))
        except ValueError:
            break
    return tuple(parts)


@dataclass
class UpdateResult:
    """Result of an update check."""

    available: bool
    current_version: str
    remote_version: str
    commit_sha: str = ""
    commit_message: str = ""
    commit_date: str = ""
    checked_at: float = field(default_factory=time.time)
    error: str = ""
    is_dev: bool = False


def _is_dev_install() -> bool:
    """Return True if swarm is running from a local editable/dev install."""
    import importlib.metadata

    try:
        dist = importlib.metadata.distribution("swarm-ai")
        # PEP 610: editable installs have a direct_url.json with dir_info.editable
        if dist.read_text("direct_url.json"):
            import json as _json

            info = _json.loads(dist.read_text("direct_url.json"))
            if info.get("dir_info", {}).get("editable", False):
                return True
            # Also flag file:// installs (local path installs via uv)
            if info.get("url", "").startswith("file://"):
                return True
    except (importlib.metadata.PackageNotFoundError, Exception):
        pass
    return False


def _get_installed_version() -> str:
    """Return the installed version of swarm-ai."""
    import importlib.metadata

    try:
        return importlib.metadata.version("swarm-ai")
    except importlib.metadata.PackageNotFoundError:
        from swarm import __version__

        return __version__


async def _fetch_remote_version(sha: str = "") -> tuple[str, str]:
    """Fetch ``__version__`` from the raw GitHub ``__init__.py``.

    If *sha* is given, fetch the file at that specific commit — the
    raw URL is immutable per-SHA, which avoids GitHub's ~5 minute CDN
    cache on the mutable ``/main/`` URL.  Without pinning, a freshly
    pushed version bump can look stale for several minutes.

    Returns ``(version_string, error_string)``.
    """
    url = _GITHUB_RAW_AT_SHA.format(sha=sha) if sha else _GITHUB_RAW_URL
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl",
            "-sS",
            "--max-time",
            _CURL_TIMEOUT,
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return "", f"curl failed: {stderr.decode(errors='replace').strip()}"
        text = stdout.decode(errors="replace")
        m = _VERSION_RE.search(text)
        if not m:
            return "", "could not parse __version__ from remote"
        return m.group(1), ""
    except Exception as exc:
        return "", str(exc)


async def _fetch_latest_commit() -> dict[str, str]:
    """Fetch the latest commit sha/message/date from the GitHub API.

    Returns an empty dict on any failure.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl",
            "-sS",
            "--max-time",
            _CURL_TIMEOUT,
            "-H",
            "Accept: application/vnd.github+json",
            _GITHUB_API_COMMITS_URL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return {}
        data = json.loads(stdout.decode(errors="replace"))
        if not isinstance(data, list) or not data:
            return {}
        commit = data[0]
        parents = commit.get("parents", [])
        full_sha = commit.get("sha", "")
        parent_full_sha = parents[0]["sha"] if parents else ""
        return {
            "sha": full_sha[:8],
            "full_sha": full_sha,
            "parent_sha": parent_full_sha[:8],
            "message": commit.get("commit", {}).get("message", "").split("\n")[0],
            "date": commit.get("commit", {}).get("committer", {}).get("date", ""),
        }
    except Exception:
        # Update probe — never raise. Logged so operators diagnosing
        # an update-check that mysteriously returns empty have a trail.
        _log.debug("get_latest_commit failed", exc_info=True)
        return {}


def _read_cache() -> UpdateResult | None:
    """Read the cached update result if it exists and is fresh.

    Returns None for every "nothing to read" case — missing file,
    stale file, corrupt JSON, incompatible schema.  Missing file is
    the normal case on first run, so we explicitly short-circuit on
    it rather than swallowing a FileNotFoundError and noisily logging
    a traceback at DEBUG level (which the user then sees mixed into
    their startup output whenever they run ``--log-level DEBUG``).
    """
    if not _CACHE_FILE.exists():
        return None
    try:
        data = json.loads(_CACHE_FILE.read_text())
        result = UpdateResult(**data)
        if time.time() - result.checked_at < _CACHE_TTL:
            return result
    except (json.JSONDecodeError, TypeError, ValueError, OSError) as exc:
        # Real parse/schema issue — debug-log without a full traceback,
        # since these are all recoverable (we just re-fetch).
        _log.debug("update cache unreadable (%s); will re-fetch", exc)
    return None


def _write_cache(result: UpdateResult) -> None:
    """Persist an update result to the cache file."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(asdict(result)))
    except Exception:
        _log.debug("Failed to write update cache", exc_info=True)


async def check_for_update(*, force: bool = False) -> UpdateResult:
    """Check for updates, using the cache unless *force* or expired.

    Never raises — errors are captured in ``UpdateResult.error``.
    """
    if not force:
        cached = _read_cache()
        if cached is not None:
            return cached

    current = _get_installed_version()
    # Fetch commit metadata first so we can pin the raw-file request to its
    # SHA — GitHub's raw.githubusercontent.com caches /main/ URLs for ~5
    # minutes, so right after a version-bump commit the mutable URL can
    # still serve the prior version.  Per-SHA raw URLs are immutable and
    # bypass that cache entirely.  Fall back to /main/ if the API is
    # unreachable.
    commit_info = await _fetch_latest_commit()
    pin_sha = commit_info.get("full_sha", "")
    remote, error = await _fetch_remote_version(pin_sha)
    if error:
        return UpdateResult(
            available=False,
            current_version=current,
            remote_version="",
            error=error,
        )

    dev = _is_dev_install()

    if dev:
        local_sha = await _local_head_sha()
        remote_sha = commit_info.get("sha", "")
        parent_sha = commit_info.get("parent_sha", "")
        if local_sha and (local_sha == remote_sha or local_sha == parent_sha):
            available = False  # Only a version-bump commit ahead
        else:
            available = _version_tuple(remote) > _version_tuple(current)
    else:
        available = _version_tuple(remote) > _version_tuple(current)

    result = UpdateResult(
        available=available,
        current_version=current,
        remote_version=remote,
        commit_sha=commit_info.get("sha", ""),
        commit_message=commit_info.get("message", ""),
        commit_date=commit_info.get("date", ""),
        is_dev=dev,
    )
    _write_cache(result)
    return result


def check_for_update_sync() -> UpdateResult | None:
    """Synchronous cache-only read for the CLI banner.

    Returns ``None`` if no cache exists or it is expired.
    """
    return _read_cache()


async def perform_update(
    on_output: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """Install the latest version from GitHub via a single uv command.

    ``--force`` reinstalls even if present (no separate uninstall step).
    ``--no-cache`` bypasses the build cache (no separate cache-clean step).

    *on_output* is called with each line of stdout/stderr for live progress.

    Returns ``(success, combined_output)``.
    """
    cmd = ["uv", "tool", "install", "--force", "--no-cache", _INSTALL_SOURCE]

    def _emit(line: str) -> None:
        if on_output:
            on_output(line)

    _emit("Installing from GitHub...")
    print("  → Installing from GitHub...", flush=True)

    output_lines: list[str] = []
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        if proc.stdout is None:
            raise RuntimeError("subprocess stdout is None despite PIPE")
        try:
            async with asyncio.timeout(_INSTALL_TIMEOUT):
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").rstrip()
                    output_lines.append(line)
                    _emit(line)
                await proc.wait()
        except TimeoutError:
            proc.kill()
            msg = f"Command timed out after {_INSTALL_TIMEOUT}s"
            output_lines.append(msg)
            _emit(msg)
            return False, "\n".join(output_lines)

        if proc.returncode != 0:
            return False, "\n".join(output_lines)
    except Exception as exc:
        output_lines.append(str(exc))
        return False, "\n".join(output_lines)

    # Clear cache so next check reflects the new version
    try:
        _CACHE_FILE.unlink(missing_ok=True)
    except Exception:
        _log.debug("Failed to clear update cache", exc_info=True)

    _emit("Update complete!")
    return True, "\n".join(output_lines)


def get_local_source_path() -> str | None:
    """Return the local filesystem path if swarm was installed from a local directory.

    Returns ``None`` for editable installs (changes already live), git installs,
    or PyPI installs.
    """
    import importlib.metadata

    try:
        dist = importlib.metadata.distribution("swarm-ai")
        raw = dist.read_text("direct_url.json")
        if not raw:
            return None
        info = json.loads(raw)
        # Editable installs don't need reinstalling — changes are live via symlinks
        if info.get("dir_info", {}).get("editable", False):
            return None
        url = info.get("url", "")
        if url.startswith("file://"):
            # Strip the file:// prefix to get the filesystem path
            return url[len("file://") :]
        return None
    except Exception:
        _log.debug("get_local_source_path parse failed", exc_info=True)
        return None


async def _local_head_sha() -> str:
    """Return the short (8-char) git HEAD SHA of the local source repo.

    Returns an empty string if the source path is unavailable or git fails.
    """
    source = get_local_source_path()
    if not source:
        return ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            source,
            "rev-parse",
            "--short=8",
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return ""
        return stdout.decode(errors="replace").strip()
    except Exception:
        _log.debug("_local_head_sha failed (git missing or repo unreadable)", exc_info=True)
        return ""


async def _run_install_step(
    cmd: list[str],
    label: str,
    output_lines: list[str],
    emit: Callable[[str], None],
) -> bool:
    """Run a single subprocess step, streaming output. Returns True on success."""
    emit(label)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        if proc.stdout is None:
            raise RuntimeError("subprocess stdout is None despite PIPE")
        try:
            async with asyncio.timeout(_INSTALL_TIMEOUT):
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").rstrip()
                    output_lines.append(line)
                    emit(line)
                await proc.wait()
        except TimeoutError:
            proc.kill()
            msg = f"{label} timed out after {_INSTALL_TIMEOUT}s"
            output_lines.append(msg)
            emit(msg)
            return False
        return proc.returncode == 0
    except Exception as exc:
        output_lines.append(f"{label}: {exc}")
        return False


async def reinstall_from_local_source(
    on_output: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """Reinstall swarm from its local source path before a server restart.

    Uses a three-step sequence (uninstall → cache clean → install) to guarantee
    a fresh build.  ``uv tool install --force --no-cache`` alone does not
    reliably rebuild when the version number hasn't changed.

    No-op (returns ``(True, "")``) when the package was not installed from a
    local directory (e.g. git, PyPI, or editable installs).

    Returns ``(success, combined_output)``.
    """
    source_path = get_local_source_path()
    if source_path is None:
        return True, ""

    def _emit(line: str) -> None:
        if on_output:
            on_output(line)

    _emit(f"Reinstalling from local source: {source_path}")
    print(f"  → Reinstalling from local source: {source_path}", flush=True)

    steps: list[tuple[list[str], str, bool]] = [
        (["uv", "tool", "uninstall", "swarm-ai"], "Uninstalling old binary", False),
        (["uv", "cache", "clean", "swarm-ai"], "Cleaning build cache", False),
        (
            ["uv", "tool", "install", "--no-cache", source_path],
            "Installing from source",
            True,
        ),
    ]

    output_lines: list[str] = []
    for cmd, label, required in steps:
        ok = await _run_install_step(cmd, label, output_lines, _emit)
        if not ok and required:
            return False, "\n".join(output_lines)

    _emit("Local reinstall complete!")
    return True, "\n".join(output_lines)


def get_source_git_sha() -> str:
    """Return 8-char git HEAD SHA of the source tree (synchronous).

    Finds the repo by walking up from ``swarm.__file__`` (works for editable
    installs) or falling back to ``get_local_source_path()`` (works for
    local-path installs).  Returns ``""`` if git is unavailable or we're
    not in a git repo.
    """
    import subprocess

    import swarm

    # Walk up from the package directory to find the .git root
    pkg_dir = Path(swarm.__file__).resolve().parent
    candidate = pkg_dir
    while candidate != candidate.parent:
        if (candidate / ".git").exists():
            break
        candidate = candidate.parent
    else:
        # No .git found — try get_local_source_path() as fallback
        source = get_local_source_path()
        if not source:
            return ""
        candidate = Path(source)

    try:
        result = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--short=8", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except Exception:
        return ""


def _hash_source_tree() -> str:
    """Hash all .py file contents under the swarm package dir. 8-char hex."""
    import hashlib

    import swarm

    src_root = Path(swarm.__file__).resolve().parent
    h = hashlib.sha256()
    for py_file in sorted(src_root.rglob("*.py")):
        h.update(py_file.read_bytes())
    return h.hexdigest()[:8]


_BUILD_SHA: str = ""


def build_sha() -> str:
    """Cached build fingerprint: git_sha+source_hash (always includes source hash)."""
    global _BUILD_SHA
    if not _BUILD_SHA:
        git_sha = get_source_git_sha()
        source_hash = _hash_source_tree()
        _BUILD_SHA = f"{git_sha}+{source_hash}" if git_sha else source_hash
    return _BUILD_SHA


def update_result_to_dict(result: UpdateResult) -> dict[str, Any]:
    """Serialize an UpdateResult for JSON API/WebSocket responses."""
    return asdict(result)


# --- Team config sync ---------------------------------------------------

_TEAM_CONFIG_CANDIDATES = (
    Path.home() / "projects" / "rcg" / "claude-team-config",
    Path.home() / "projects" / "claude-team-config",
)

_TEAM_CONFIG_TIMEOUT = 60  # seconds


async def sync_team_config() -> None:
    """Run claude-team-config install.sh if the repo is found locally.

    Searches common checkout locations.  If found, runs ``yes | ./install.sh``
    so all interactive prompts are auto-accepted (team config is authoritative).
    install.sh handles its own ``git pull`` internally.

    Never raises — failures are logged at warning level.
    """
    repo_dir: Path | None = None
    for candidate in _TEAM_CONFIG_CANDIDATES:
        if (candidate / "install.sh").is_file():
            repo_dir = candidate
            break

    if repo_dir is None:
        _log.debug("claude-team-config repo not found; skipping team config sync")
        return

    install_sh = repo_dir / "install.sh"
    _log.debug("syncing team config from %s", repo_dir)

    try:
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            f"yes | {install_sh}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(repo_dir),
        )
        assert proc.stdout is not None
        try:
            async with asyncio.timeout(_TEAM_CONFIG_TIMEOUT):
                output = await proc.stdout.read()
                await proc.wait()
        except TimeoutError:
            proc.kill()
            _log.warning("team config install timed out after %ds", _TEAM_CONFIG_TIMEOUT)
            return

        text = output.decode(errors="replace").strip()
        if proc.returncode == 0:
            _log.debug("team config sync complete:\n%s", text)
        else:
            _log.warning("team config install.sh exited %d:\n%s", proc.returncode, text)
    except Exception:
        _log.warning("team config sync failed", exc_info=True)
