"""GitHub-based update detection for Swarm.

Compares the installed version against the latest ``__version__`` on GitHub
main.  Results are cached to ``~/.swarm/update_cache.json`` with a 24-hour
TTL so that startup stays fast (the CLI banner reads cache only).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from swarm.paths import state_dir

_log = logging.getLogger(__name__)

_CACHE_DIR = state_dir()
_CACHE_FILE = _CACHE_DIR / "update_cache.json"
_CACHE_TTL = 86400  # 24 hours

_GITHUB_RAW_URL = "https://raw.githubusercontent.com/miopea/swarm-legacy/main/src/swarm/__init__.py"
_GITHUB_RAW_AT_SHA = (
    "https://raw.githubusercontent.com/miopea/swarm-legacy/{sha}/src/swarm/__init__.py"
)
_GITHUB_API_COMMITS_URL = "https://api.github.com/repos/miopea/swarm-legacy/commits?per_page=1"
_GITHUB_API_REPO_URL = "https://api.github.com/repos/miopea/swarm-legacy"
_REPO_FULL_NAME = "miopea/swarm-legacy"
# Every name this project has answered to.  GitHub redirects a renamed repo,
# which is why builds with the old URL still update — but a redirect dies the
# moment a NEW repo claims the freed name, and then the same URL silently
# resolves to a different product.  Landing anywhere outside this set means
# the name was reused, not renamed.
_KNOWN_REPO_NAMES = frozenset({"miopea/swarm", "miopea/swarm-legacy"})
_VERSION_RE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')

_CURL_TIMEOUT = "10"  # seconds (string for CLI arg)
# A COLD install: `--no-cache` re-fetches all 44 packages (cryptography is
# 4.5 MiB alone) and builds red-black-tree-mod from source.  120s fit a warm
# cache and nothing else — a real update on a real connection was killed
# mid-download and reported as a failure, which blocks the one thing that
# has to work for a repo migration: users being able to update at all.
# The restart paths bound themselves separately (`_best_effort_reinstall`
# passes its own 30s), so this only governs the deliberate update.
_INSTALL_TIMEOUT = 600  # seconds

_INSTALL_SOURCE = "git+https://github.com/miopea/swarm-legacy.git"


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
    # Where GitHub says this repo actually lives now.  Empty when the
    # probe could not answer — which is NOT the same as "unmoved", so the
    # two are kept distinguishable rather than defaulting to the baked-in
    # name and reporting a move that was never checked for.
    repo_full_name: str = ""
    repo_moved: bool = False


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
            "-sSL",  # -L: a repo rename answers 301; without it we parse the redirect
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
            "-sSL",  # -L: see _fetch_remote_version — a rename 301s here too
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


async def fetch_repo_location() -> str:
    """Where GitHub currently serves this repo, as ``owner/name``.

    Every build bakes in the repo URL that was current when it shipped, so
    a rename leaves existing installs pointing at the old name forever.
    GitHub redirects, which is why the install keeps working and why the
    breakage is quiet: ``git`` follows the redirect, and ``curl -L`` now
    does too, so nothing fails — the name in the binary simply stops being
    the truth.

    Asking the API which repo it actually served turns that into a fact we
    can report.  Returns ``""`` when the probe fails; the caller must not
    read that as "unmoved".
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl",
            "-sSL",
            "--max-time",
            _CURL_TIMEOUT,
            "-H",
            "Accept: application/vnd.github+json",
            _GITHUB_API_REPO_URL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return ""
        data = json.loads(stdout.decode(errors="replace"))
        if not isinstance(data, dict):
            return ""
        name = data.get("full_name")
        return str(name) if isinstance(name, str) else ""
    except Exception:
        _log.debug("fetch_repo_location failed", exc_info=True)
        return ""


def repo_has_moved(resolved: str) -> bool:
    """True only when the probe answered AND named a different repo."""
    return bool(resolved) and resolved.casefold() != _REPO_FULL_NAME.casefold()


def foreign_repo_refusal(resolved: str) -> str | None:
    """Refuse to install from a repo that is not one of ours.

    The hazard this exists for: freeing the ``swarm`` name means some
    other project eventually claims it.  GitHub's rename redirect —
    which is the only reason builds carrying the old URL still update —
    disappears the instant that happens, and the baked-in URL stops
    being a redirect and starts being a real repo belonging to someone
    else.  An install would then quietly replace Legacy with whatever
    now lives there, with no error at any layer.

    A rename within our OWN history is not a refusal; that is the
    normal, supported path.  Only a name outside ``_KNOWN_REPO_NAMES``
    is.

    An unanswered probe returns ``""`` and must never refuse — a network
    failure is not evidence of a hijacked name, and blocking updates on
    it would break the very migration this protects.
    """
    if not resolved or resolved.casefold() in {n.casefold() for n in _KNOWN_REPO_NAMES}:
        return None
    return (
        f"Refusing to update: {_REPO_FULL_NAME} now resolves to {resolved}, which is "
        f"not a Swarm (legacy) repository. The name was most likely reused by another "
        f"project. Reinstall explicitly from the correct URL rather than letting this "
        f"install follow the old name."
    )


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

    resolved_repo = await fetch_repo_location()
    moved = repo_has_moved(resolved_repo)
    if moved:
        _log.warning(
            "[repo-moved] this build installs from %s but GitHub now serves it as %s. "
            "Updates still work (git and curl follow the redirect); the compiled-in "
            "name is stale and should be refreshed in a release.",
            _REPO_FULL_NAME,
            resolved_repo,
        )

    result = UpdateResult(
        available=available,
        current_version=current,
        remote_version=remote,
        commit_sha=commit_info.get("sha", ""),
        commit_message=commit_info.get("message", ""),
        commit_date=commit_info.get("date", ""),
        is_dev=dev,
        repo_full_name=resolved_repo,
        repo_moved=moved,
    )
    _write_cache(result)
    return result


def check_for_update_sync() -> UpdateResult | None:
    """Synchronous cache-only read for the CLI banner.

    Returns ``None`` if no cache exists or it is expired.
    """
    return _read_cache()


_FOREIGN_BACKUP_SUFFIX = ".pre-swarm-update"


def _is_our_shim(path: Path) -> bool:
    """True when *path* is the console script uv installs for this package."""
    try:
        return "swarm-ai" in str(path.resolve())
    except OSError:
        return False


def _preserve_foreign_entrypoints() -> list[tuple[Path, Path]]:
    """Move a ``swarm`` we do not own out of ``uv``'s way before installing.

    ``uv tool install --force`` overwrites whatever sits at a declared
    script's name — verified, not assumed: a foreign ``swarm`` on PATH is
    silently replaced by ours.  On a relocated install that name has been
    deliberately handed to something else, so an update would destroy the
    binary now standing there.

    The file is moved aside and put back afterwards, which also preserves a
    symlink as a symlink.  Only relocated installs do this; before
    relocation ``swarm`` is legitimately ours.
    """
    from swarm.paths import is_relocated
    from swarm.relocate import _shim_directories

    if not is_relocated():
        return []
    saved: list[tuple[Path, Path]] = []
    for directory in _shim_directories():
        shim = directory / "swarm"
        if not (shim.exists() or shim.is_symlink()) or _is_our_shim(shim):
            continue
        backup = shim.with_name(shim.name + _FOREIGN_BACKUP_SUFFIX)
        try:
            if backup.exists() or backup.is_symlink():
                backup.unlink()
            shutil.move(str(shim), str(backup))
            saved.append((shim, backup))
            _log.warning("Moved %s aside for the update; it is not ours", shim)
        except OSError:
            _log.warning("could not preserve %s across the update", shim, exc_info=True)
    return saved


def _restore_foreign_entrypoints(saved: list[tuple[Path, Path]]) -> None:
    """Put back what ``uv`` overwrote, discarding the copy it installed."""
    for shim, backup in saved:
        try:
            if shim.exists() or shim.is_symlink():
                shim.unlink()
            shutil.move(str(backup), str(shim))
            _log.warning("Restored %s after the update", shim)
        except OSError:
            _log.warning("could not restore %s — the copy is at %s", shim, backup, exc_info=True)


def _drop_reoccupied_entrypoint() -> list[Path]:
    """Remove the ``swarm`` shim a reinstall recreates on a relocated install.

    ``uv tool install`` writes every console script the package declares, so
    every update hands ``swarm`` back to Legacy even though ``swarm relocate``
    deliberately gave that name up.  Left alone, each update silently
    re-occupies a name the operator freed — and once something else owns that
    name, re-occupying it is a collision, not a cosmetic wart.

    Only a shim resolving into this package's own tool directory is removed, so
    a ``swarm`` belonging to something else is never deleted by us.

    Removes **every** copy it finds, not just the first.  ``uv`` writes both the
    PATH shim and the script it points at inside the tool directory; stopping
    after one left the inner copy behind, which kept
    :attr:`RelocationPlan.already_done` false forever — so an operator who had
    long since relocated was shown the destructive banner again on every check.
    """
    from swarm.paths import is_relocated
    from swarm.relocate import _shim_directories

    if not is_relocated():
        return []
    removed: list[Path] = []
    # Shared with the relocation itself so the two cannot disagree about
    # where a shim lives.
    for directory in _shim_directories():
        shim = directory / "swarm"
        if not (shim.exists() or shim.is_symlink()):
            continue
        try:
            owned = "swarm-ai" in str(shim.resolve())
        except OSError:
            owned = False
        if not owned:
            _log.warning(
                "%s exists but is not ours — leaving it alone. This install is "
                "relocated and answers to 'swarm-legacy'.",
                shim,
            )
            continue
        try:
            shim.unlink()
            _log.warning(
                "Removed %s recreated by the update; this install is relocated "
                "and answers to 'swarm-legacy'.",
                shim,
            )
            removed.append(shim)
        except OSError:
            _log.warning("could not remove recreated entrypoint %s", shim, exc_info=True)
    return removed


# Data files, not modules.  ``uv tool install --force`` uninstalls before it
# installs, so a process killed partway through leaves the package importable
# — the .py files land early — while templates and static assets are still
# missing.  The daemon then starts, serves, and dies on the first request with
# "Template 'dashboard.html' not found", which names a symptom four steps
# removed from the cause.  Observed exactly that after a 120s timeout kill.
_REQUIRED_ARTIFACTS = (
    ("web/templates", "dashboard.html"),
    ("web/static", "dashboard.js"),
)


def missing_install_artifacts() -> list[str]:
    """Files the installed package must have, and does not.

    Checked on disk rather than through the import system: the running
    daemon holds the OLD code in memory but the tool directory it was
    loaded from is the one just rewritten, so the filesystem is the only
    thing that reflects what an update actually produced.
    """
    try:
        import swarm

        root = Path(str(swarm.__file__)).resolve().parent
    except Exception:
        return []  # cannot locate the package — do not invent a failure
    missing: list[str] = []
    for subdir, name in _REQUIRED_ARTIFACTS:
        if not (root / subdir / name).is_file():
            missing.append(f"{subdir}/{name}")
    return missing


def _incomplete_install_message(missing: list[str]) -> str:
    return (
        "Update did not finish: the install is missing "
        + ", ".join(missing)
        + ". The package was partly written, so the dashboard will fail to render. "
        "Re-run the update, or from a terminal: "
        "uv tool install --force --no-cache " + _INSTALL_SOURCE
    )


def _report_partial_install(output_lines: list[str], emit: Callable[[str], None]) -> bool:
    """Append and emit a partial-install report. True when one was found."""
    missing = missing_install_artifacts()
    if not missing:
        return False
    broken = _incomplete_install_message(missing)
    output_lines.append(broken)
    emit(broken)
    _log.error("[update] %s", broken)
    return True


_GIT_AUTH_MARKERS = (
    "enter passphrase",
    "permission denied (publickey)",
    "could not read from remote repository",
    "authentication failed",
    "host key verification failed",
)


def _noninteractive_git_env() -> dict[str, str]:
    """Environment that makes git FAIL rather than wait for a human.

    The install runs with ``stdin=DEVNULL``.  This repository is public,
    so a plain HTTPS clone needs no credentials at all — but an operator
    whose git rewrites ``https://github.com/`` to SSH (an ``insteadOf``
    rule, common on machines set up for other tooling) sends the update
    down an authenticated path anyway.  A passphrase-protected key then
    prompts, and a prompt against a closed stdin blocks until the install
    timeout kills it.

    That is the worst available shape: the longest possible wait produces
    the least possible information, and the kill lands mid-install and
    leaves a package that imports but has no templates.  Observed exactly
    that, twice, on a real box.

    ``GIT_TERMINAL_PROMPT=0`` plus ``BatchMode=yes`` turn the hang into an
    immediate, readable authentication error.  ``SSH_ASKPASS`` is cleared
    for the same reason — a graphical prompt on a headless daemon is a
    hang wearing a different hat.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    existing = env.get("GIT_SSH_COMMAND", "ssh")
    if "BatchMode" not in existing:
        env["GIT_SSH_COMMAND"] = f"{existing} -oBatchMode=yes"
    env.pop("SSH_ASKPASS", None)
    env.pop("SSH_ASKPASS_REQUIRE", None)
    return env


def git_auth_hint(output: str) -> str | None:
    """Explain an auth failure that should never have been reachable.

    Without this the operator sees a raw ssh error for a PUBLIC repo,
    which reads as "the update is broken" rather than "your git is
    rewriting this URL to SSH and the daemon has no key".
    """
    lowered = output.lower()
    if not any(marker in lowered for marker in _GIT_AUTH_MARKERS):
        return None
    return (
        "The update needed git credentials, but this repository is public and "
        "an HTTPS clone needs none. Your git is almost certainly rewriting "
        "https://github.com/ to SSH (an 'insteadOf' rule in ~/.gitconfig), and "
        "the daemon has no way to unlock a passphrase-protected key. Remove or "
        "scope that rewrite, or load the key into an ssh-agent the daemon can "
        "reach. Check with: git config --get-regexp 'url\\..*insteadOf'"
    )


async def _stream_install(
    cmd: list[str],
    output_lines: list[str],
    emit: Callable[[str], None],
) -> bool:
    """Run the install, streaming output. False on any failure.

    Split out of :func:`perform_update` so the timeout handling — the
    part that can leave a half-written package behind — reads as one
    thing rather than as nesting inside the wider update flow.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_noninteractive_git_env(),
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
            # Killing uv mid-install is destructive, not merely a failure:
            # --force has already removed the previous version by this point.
            msg = (
                f"Install timed out after {_INSTALL_TIMEOUT}s and was killed. "
                "This can leave the package partly written."
            )
            output_lines.append(msg)
            emit(msg)
            _report_partial_install(output_lines, emit)
            return False
        if proc.returncode != 0:
            hint = git_auth_hint("\n".join(output_lines))
            if hint:
                output_lines.append(hint)
                emit(hint)
                _log.error("[update] %s", hint)
            return False
        return True
    except Exception as exc:
        output_lines.append(str(exc))
        return False


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

    # Checked BEFORE anything is installed: refusing afterwards would mean
    # the wrong package is already on disk.
    refusal = foreign_repo_refusal(await fetch_repo_location())
    if refusal:
        _log.error("[repo-identity] %s", refusal)
        _emit(refusal)
        return False, refusal

    preserved = _preserve_foreign_entrypoints()

    _emit("Installing from GitHub...")
    print("  → Installing from GitHub...", flush=True)

    output_lines: list[str] = []
    if not await _stream_install(cmd, output_lines, _emit):
        return False, "\n".join(output_lines)

    # uv exited 0 — but verify what it produced rather than trusting the
    # exit code, since a broken install is reported by the WEB layer four
    # steps later, long after the update claimed to have worked.
    if _report_partial_install(output_lines, _emit):
        return False, "\n".join(output_lines)

    # Clear cache so next check reflects the new version
    try:
        _CACHE_FILE.unlink(missing_ok=True)
    except Exception:
        _log.debug("Failed to clear update cache", exc_info=True)

    _restore_foreign_entrypoints(preserved)
    for dropped in _drop_reoccupied_entrypoint():
        msg = f"Removed {dropped} (this install is relocated; use 'swarm-legacy')"
        output_lines.append(msg)
        _emit(msg)

    # Check where the repo actually lives now, AFTER installing.  The
    # build just written carries whatever URL was current when it was
    # committed, so this is the first moment the freshly-installed name
    # can be compared against what GitHub serves.  Reported, never acted
    # on: retargeting an install to a repo the operator did not name is
    # not a decision an updater gets to make silently.
    resolved_repo = await fetch_repo_location()
    if repo_has_moved(resolved_repo):
        msg = (
            f"Note: this build installs from {_REPO_FULL_NAME}, but GitHub now "
            f"serves it as {resolved_repo}. Updates still work through the "
            f"redirect; the URL should be refreshed in a release."
        )
        output_lines.append(msg)
        _emit(msg)
        _log.warning("[repo-moved] %s -> %s (after update)", _REPO_FULL_NAME, resolved_repo)

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


def _find_source_repo_root() -> Path | None:
    """The git checkout the RUNNING swarm code was imported from, or None.

    Walks up from ``swarm.__file__`` (works for editable installs) and falls
    back to ``get_local_source_path()`` (works for local-path installs).

    Deliberately anchored on the imported package rather than on a configured
    path: the question this answers is "what code is this process running",
    and the only honest source for that is where the modules came from.
    """
    import swarm

    candidate = Path(swarm.__file__).resolve().parent
    while candidate != candidate.parent:
        if (candidate / ".git").exists():
            return candidate
        candidate = candidate.parent
    source = get_local_source_path()
    return Path(source) if source else None


def get_source_git_sha() -> str:
    """Return 8-char git HEAD SHA of the source tree (synchronous).

    Finds the repo by walking up from ``swarm.__file__`` (works for editable
    installs) or falling back to ``get_local_source_path()`` (works for
    local-path installs).  Returns ``""`` if git is unavailable or we're
    not in a git repo.
    """
    import subprocess

    root = _find_source_repo_root()
    if root is None:
        return ""
    candidate = root

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


@dataclass
class SourceTreeState:
    """What the running daemon's source checkout looked like when it started.

    #1203. ``build_sha()`` already fingerprints the tree as
    ``<git sha>+<source hash>``, but a fingerprint is not a diagnosis: it
    cannot tell you whether the hash is what the SHA checks out to, or the
    result of uncommitted edits. "Running the last release" and "running code
    that exists in no commit" look identical from inside, which is exactly the
    shape of check this fleet's standard exists to catch.

    ``checked=False`` means the probe could not answer — no git checkout, git
    missing, or the call failed. That is deliberately distinct from
    ``is_dirty=False``: "we could not tell" and "it is clean" are different
    answers and must not collapse into one.
    """

    repo_root: str = ""
    head: str = ""
    dirty_files: list[str] = field(default_factory=list)
    checked: bool = False

    @property
    def is_dirty(self) -> bool:
        return bool(self.dirty_files)

    def summary(self) -> str:
        """One line naming HEAD, the count, and the files themselves.

        Names files rather than just counting them: the incident that motivated
        this cost a round trip precisely because "something is uncommitted" does
        not tell an investigator which subsystem to suspect.
        """
        shown = self.dirty_files[:_DIRTY_FILES_IN_SUMMARY]
        more = len(self.dirty_files) - len(shown)
        tail = f" (+{more} more)" if more > 0 else ""
        return (
            f"HEAD {self.head or '?'} with {len(self.dirty_files)} uncommitted "
            f"file(s): {', '.join(shown)}{tail}"
        )


# Cap the buzz-log detail so one messy tree can't flood the operator's feed;
# the count is always exact even when the list is elided.
_DIRTY_FILES_IN_SUMMARY = 12


async def _git_porcelain(root: Path) -> str:
    """``git status --porcelain`` for *root*. Async so startup never blocks."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(root),
        "status",
        "--porcelain",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    return stdout_b.decode(errors="replace")


async def get_source_tree_state() -> SourceTreeState:
    """Determine whether the code this process is RUNNING was committed.

    Never raises — a startup probe that can break startup is worse than the
    ambiguity it removes.
    """
    root = _find_source_repo_root()
    if root is None:
        return SourceTreeState()
    try:
        porcelain = await _git_porcelain(root)
    except Exception:
        _log.debug("source tree probe failed for %s", root, exc_info=True)
        return SourceTreeState(repo_root=str(root))
    files = [line[3:].strip() for line in porcelain.splitlines() if len(line) > 3]
    return SourceTreeState(
        repo_root=str(root),
        head=get_source_git_sha(),
        dirty_files=files,
        checked=True,
    )


# Extensions that make up a build fingerprint. `.py` alone was the original set and it
# had a hole big enough to break a shipped feature: `build_sha()` is the cache-buster on
# `/static/dashboard.js?v=...`, so a change to dashboard.js, base.html or the CSS produced
# a BYTE-IDENTICAL `?v=` and the browser kept serving its cached copy. A cache-buster
# keyed on a different asset class than the asset it busts.
#
# HOW IT PRESENTED (2026-08-18): a new Queen composer rendered correctly — templates are
# read per request — while neither Enter nor its Send button did anything, because the JS
# that wires them was never re-fetched. Nothing was broken in the feature; the browser was
# running last build's script against this build's markup. A daemon restart does NOT fix
# it either: the hash is identical, so the URL is identical.
_BUILD_HASH_SUFFIXES = (".py", ".js", ".css", ".html")


def _hash_source_tree() -> str:
    """Hash the file contents that define a build, under the swarm package dir. 8-char hex.

    Covers Python AND the web assets — see ``_BUILD_HASH_SUFFIXES`` for why the latter is
    not optional.

    COSTS 62ms AGAINST THE OLD 16ms, AND THAT IS AFFORDABLE ONLY BECAUSE ``build_sha()``
    CACHES. Every caller goes through it, and it memoises into ``_BUILD_SHA``, so this
    runs once per process — two page routes call ``build_sha()`` per REQUEST and hit the
    cache. Drop that memo and this becomes a 62ms filesystem walk on every dashboard load.

    CONSEQUENCE OF THE SAME CACHE, worth knowing rather than rediscovering: a front-end
    edit in a dev/editable install still does not change the served ``?v=`` until the
    daemon restarts. That is an improvement on what it replaced — before, a restart did
    not change it either, because no ``.py`` had moved — but it does mean "restart", not
    "refresh", is what publishes a CSS or JS change.
    """
    import hashlib

    import swarm

    src_root = Path(swarm.__file__).resolve().parent
    h = hashlib.sha256()
    for path in sorted(src_root.rglob("*")):
        if path.suffix in _BUILD_HASH_SUFFIXES and path.is_file():
            # The NAME goes in too: a rename with identical contents changes what the
            # browser must fetch, and a content-only hash would call that the same build.
            h.update(str(path.relative_to(src_root)).encode())
            h.update(path.read_bytes())
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


# --- Shared team-config sync --------------------------------------------
#
# Distributes the shared agent configuration repos (claude-team-config and
# codex-team-config) onto this box by running each repo's own installer.
#
# THREE PROPERTIES THIS CODE EXISTS TO PRESERVE:
#
# 1. INVOKE THE INSTALLER THROUGH ``bash``, NEVER DIRECTLY. Both repos commit
#    ``install.sh`` as mode 100644 — not executable, in git, in every clone.
#    Running it directly exits 126 ("found, not executable"), which is what it
#    did on this box 9 times over ~31h without a single success. ``.rcg.yaml``'s
#    post_clone hook works only because it says ``bash install.sh --yes``. The
#    caller is fixed here rather than depending on every clone carrying the
#    right mode bit; a follow-up chmod in those repos is complementary, not a
#    substitute.
#
# 2. NEVER CHANGE WHAT IS CHECKED OUT. A prompt-ablation experiment runs on
#    branch ``ablation`` in claude-team-config while ``main`` keeps the full
#    CLAUDE.md (claude-team-config/docs/specs/prompt-ablation.md). This module
#    therefore runs ONLY ``rev-parse`` and ``fetch`` — both read-only with
#    respect to HEAD, the working tree and branch checkout. No checkout, no
#    reset, no pull of our own. The installer's internal
#    ``git pull --ff-only origin main`` is left exactly as it is, because it
#    was verified not to switch branches. A sync that forced the box back to
#    main would destroy the experiment AND look like a successful sync, which
#    is precisely the failure class this module is being fixed for.
#
# 3. SUCCESS AND FAILURE MUST BE EQUALLY OBSERVABLE. The old code logged
#    failure at WARNING and success at DEBUG. With the daemon's configured
#    ``log_level = WARNING`` a success could not appear in swarm.log at all —
#    so "synced fine" and "never ran" were indistinguishable from the log, and
#    a grep for the success line returned 0 whether or not it had ever worked.
#    Outcomes are now logged symmetrically at INFO (installed / already
#    current / skipped) with failures still at WARNING.

_SHARED_CONFIG_TIMEOUT = 60  # seconds
_SHARED_CONFIG_STATE_FILE = _CACHE_DIR / "shared-config-state.json"


@dataclass(frozen=True)
class SharedConfigRepo:
    """A shared agent-config repo and where to look for it."""

    name: str
    candidates: tuple[Path, ...]
    installer: str = "install.sh"


_SHARED_CONFIG_REPOS: tuple[SharedConfigRepo, ...] = (
    SharedConfigRepo(
        name="claude-team-config",
        candidates=(
            Path.home() / "projects" / "rcg" / "claude-team-config",
            Path.home() / "projects" / "claude-team-config",
        ),
    ),
    # codex-team-config is DOWNSTREAM of claude-team-config by construction:
    # its scripts/sync-from-claude.sh regenerates AGENTS.md as
    # ``cat AGENTS.header.md <claude-team-config>/CLAUDE.md > AGENTS.md``.
    #
    # DECISION: this updater INSTALLS WHAT IS COMMITTED and does NOT run that
    # regeneration. Distributing config and authoring it are different jobs.
    # Regeneration reads whatever branch claude-team-config happens to be on,
    # so on this box today it would rewrite AGENTS.md from the cut ablation
    # CLAUDE.md and quietly push the experiment into the codex config — a
    # successful-looking sync that corrupts what it distributes. Regeneration
    # is a deliberate authoring step and belongs with a human on a known
    # branch, not in a daemon's background startup task.
    SharedConfigRepo(
        name="codex-team-config",
        candidates=(
            Path.home() / "projects" / "rcg" / "codex-team-config",
            Path.home() / "projects" / "codex-team-config",
        ),
    ),
)


def dev_mode_active() -> bool:
    """True when this process is running from a development checkout.

    Checks the ``SWARM_DEV`` env var **and** :func:`_is_dev_install`. The env
    var alone was the old gate and is not sufficient: neither daemon running on
    this box sets it, including the one started from the project ``.venv``, so
    the shared-config sync fired from a dev checkout every time.
    """
    import os

    return bool(os.environ.get("SWARM_DEV")) or _is_dev_install()


def _read_shared_config_state() -> dict[str, str]:
    """Last-installed fingerprint per repo. Missing/corrupt reads as empty."""
    try:
        with open(_SHARED_CONFIG_STATE_FILE) as fh:
            data = json.load(fh)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_shared_config_state(state: dict[str, str]) -> None:
    try:
        _SHARED_CONFIG_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_SHARED_CONFIG_STATE_FILE, "w") as fh:
            json.dump(state, fh, indent=2)
    except OSError:
        # Losing the cache costs one redundant install, never correctness.
        _log.debug("could not persist shared-config state", exc_info=True)


async def _git_output(repo_dir: Path, *args: str) -> str:
    """Run a READ-ONLY git command in *repo_dir*; "" on any failure.

    Only ``rev-parse`` and ``fetch`` are ever passed here. Nothing in this
    module may run a command that changes HEAD, the branch or the working
    tree — see property 2 in the module comment above.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(repo_dir),
        )
        async with asyncio.timeout(_SHARED_CONFIG_TIMEOUT):
            out, _ = await proc.communicate()
        return out.decode(errors="replace").strip() if proc.returncode == 0 else ""
    except (OSError, TimeoutError, ValueError):
        return ""


async def _repo_fingerprint(repo_dir: Path) -> str:
    """Identify the repo's current state as ``branch:HEAD:origin/main``.

    ``fetch`` updates remote-tracking refs only — it cannot move HEAD, switch
    branches or touch the working tree, which is why it is the safe way to
    notice upstream commits without running the installer.

    The BRANCH is part of the fingerprint on purpose: checking out ``ablation``
    must invalidate a fingerprint recorded on ``main`` (and vice versa), or the
    updater would skip the install that makes the switch take effect.

    Returns "" when anything is unreadable, which callers treat as "unknown" —
    and unknown must never compare equal to a stored fingerprint, or a broken
    git would silently look like "already current" forever.
    """
    head = await _git_output(repo_dir, "rev-parse", "HEAD")
    if not head:
        return ""
    branch = await _git_output(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
    await _git_output(repo_dir, "fetch", "--quiet", "origin")
    remote = await _git_output(repo_dir, "rev-parse", "origin/main")
    return f"{branch}:{head}:{remote}"


def _find_repo(repo: SharedConfigRepo) -> Path | None:
    for candidate in repo.candidates:
        if (candidate / repo.installer).is_file():
            return candidate
    return None


async def _sync_one_shared_config(repo: SharedConfigRepo, state: dict[str, str]) -> str:
    """Install *repo* if it is not already current.

    Returns a short OUTCOME token — ``installed`` / ``already-current`` /
    ``absent`` / ``failed`` / ``timeout`` — rather than a bool, so the caller can
    name what happened per repo in one summary line. A bool could only say
    "something changed", which cannot distinguish "already current" from "never
    ran" — the exact distinction #1263 was filed about.

    Never raises — a shared-config problem must not take down the update check.
    """
    repo_dir = _find_repo(repo)
    if repo_dir is None:
        _log.debug("%s not found locally; skipping", repo.name)
        return "absent"

    fingerprint = await _repo_fingerprint(repo_dir)
    # "" means we could not read git. Fall through and install rather than
    # guessing; an unreadable repo is exactly when a stale skip is worst.
    if fingerprint and state.get(repo.name) == fingerprint:
        _log.info("%s already current at %s — install skipped", repo.name, fingerprint)
        return "already-current"

    installer = repo_dir / repo.installer
    _log.info("syncing %s from %s", repo.name, repo_dir)

    try:
        # ``bash <installer>`` — NOT the installer directly. See property 1.
        # ``yes |`` auto-accepts the installer's prompts; the shared config is
        # authoritative, so there is nothing to decide interactively.
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            f"yes | bash {shlex.quote(str(installer))}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(repo_dir),
        )
        assert proc.stdout is not None
        try:
            async with asyncio.timeout(_SHARED_CONFIG_TIMEOUT):
                output = await proc.stdout.read()
                await proc.wait()
        except TimeoutError:
            proc.kill()
            _log.warning("%s install timed out after %ds", repo.name, _SHARED_CONFIG_TIMEOUT)
            return "timeout"

        text = output.decode(errors="replace").strip()
        if proc.returncode != 0:
            _log.warning("%s install.sh exited %d:\n%s", repo.name, proc.returncode, text)
            return "failed"

        # Re-read AFTER the install: the installer pulls, so HEAD may have
        # moved. Recording the pre-install fingerprint would re-run the whole
        # install on the next start for a change we already have.
        installed = await _repo_fingerprint(repo_dir) or fingerprint
        if installed:
            state[repo.name] = installed
        _log.info("team config sync complete for %s at %s", repo.name, installed or "unknown")
        return "installed"
    except Exception:
        _log.warning("%s sync failed", repo.name, exc_info=True)
        return "failed"


async def sync_team_config() -> None:
    """Sync every shared agent-config repo found on this box.

    Skipped entirely in development mode: a dev checkout is where shared
    config gets *authored*, and overwriting the author's working copy from
    origin mid-edit is how you lose work.

    Never raises — this runs from a background startup task.
    """
    # ONE WARNING-LEVEL LINE PER INVOCATION, and the level is deliberate (#1263).
    #
    # Every per-repo outcome below is INFO, and the daemon runs at
    # log_level=WARNING — so before this line existed, a healthy sync, an
    # already-current skip, a dev-mode skip and a sync that was never invoked at
    # all were ALL silence in swarm.log. Failures were visible (WARNING), so a
    # broken installer still surfaced; what could not be seen was whether the
    # distribution path was alive.
    #
    # WARNING for a healthy outcome is a deliberate abuse of the level. The
    # alternative is unobservability at the level this fleet actually deploys
    # at, and the governing question decides it: what would this look like if it
    # were measuring nothing? Silence. Would that differ from success? Not
    # without this line. Once per daemon start is not spam.
    if dev_mode_active():
        _log.warning("shared-config sync: SKIPPED — development mode")
        return

    state = _read_shared_config_state()
    outcomes: list[str] = []
    changed = False
    for repo in _SHARED_CONFIG_REPOS:
        outcome = await _sync_one_shared_config(repo, state)
        outcomes.append(f"{repo.name}={outcome}")
        changed |= outcome == "installed"
    if changed:
        _write_shared_config_state(state)
    _log.warning("shared-config sync: %s", "; ".join(outcomes) or "no repos configured")
