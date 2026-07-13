"""Submit a feedback report directly via the ``gh`` CLI.

When ``gh`` is installed and authenticated on the user's machine, we can
create GitHub issues as the user without any OAuth flow, client IDs, or
URL-length limits. The user's existing ``gh auth`` credentials are used
and never touched by swarm.

Issue body is piped via stdin (``--body-file -``) so there is no
command-line length limit — bodies up to GitHub's ~65KB issue limit
work end-to-end.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass

from swarm.feedback.builder import Category

_log = logging.getLogger("swarm.feedback.gh_submit")

_DEFAULT_REPO = "miopea/swarm"
_CATEGORY_LABELS: dict[Category, str] = {
    "bug": "bug",
    "feature": "enhancement",
    "question": "question",
}
_GH_TIMEOUT_SECONDS = 30


@dataclass
class GhStatus:
    """Result of the ``gh`` availability check."""

    installed: bool
    authenticated: bool
    account: str = ""
    error: str = ""


@dataclass
class GhSubmitResult:
    """Result of a successful ``gh issue create`` call."""

    url: str


class GhSubmitError(RuntimeError):
    """Raised when submission via ``gh`` fails."""


def _find_gh() -> str | None:
    """Return the absolute path to ``gh`` on PATH, or None if missing."""
    return shutil.which("gh")


async def check_gh_status() -> GhStatus:
    """Check whether ``gh`` is installed and authenticated.

    Runs ``gh auth status`` with a short timeout. Returns a
    :class:`GhStatus` the UI can use to decide whether to offer the
    "Submit via gh" button.
    """
    path = _find_gh()
    if path is None:
        return GhStatus(installed=False, authenticated=False, error="gh not installed")

    try:
        proc = await asyncio.create_subprocess_exec(
            path,
            "auth",
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (TimeoutError, OSError) as e:
        return GhStatus(installed=True, authenticated=False, error=str(e))

    output = stdout.decode("utf-8", errors="replace") if stdout else ""
    if proc.returncode != 0:
        return GhStatus(
            installed=True,
            authenticated=False,
            error=output.strip() or "gh auth status returned non-zero",
        )

    # Parse the "✓ Logged in to github.com account <name>" line. Output
    # may be prefixed with a checkmark glyph, so use substring match
    # rather than startswith.
    account = ""
    for line in output.splitlines():
        if "Logged in to" in line and "account" in line:
            parts = line.split("account", 1)[1].strip().split()
            if parts:
                account = parts[0]
            break
    return GhStatus(installed=True, authenticated=True, account=account)


async def submit_via_gh(
    *,
    title: str,
    body: str,
    category: Category,
    repo: str = _DEFAULT_REPO,
) -> GhSubmitResult:
    """Create a GitHub issue via the ``gh`` CLI.

    Body is piped on stdin so there's no arg-length limit. On success,
    returns the created issue URL (which ``gh`` prints to stdout).
    """
    path = _find_gh()
    if path is None:
        raise GhSubmitError("gh is not installed on PATH")

    label = _CATEGORY_LABELS.get(category, "bug")
    args = [
        path,
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        title,
        "--label",
        label,
        "--body-file",
        "-",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=body.encode("utf-8")),
            timeout=_GH_TIMEOUT_SECONDS,
        )
    except TimeoutError as e:
        raise GhSubmitError("gh issue create timed out") from e
    except OSError as e:
        raise GhSubmitError(f"Failed to invoke gh: {e}") from e

    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", errors="replace").strip()
        out = (stdout or b"").decode("utf-8", errors="replace").strip()
        msg = err or out or f"gh exited with status {proc.returncode}"
        # If the label doesn't exist on the repo, gh fails. Retry once
        # without the label so the report still gets through.
        lower = msg.lower()
        label_missing = "could not add label" in lower or (
            "label" in lower and "not found" in lower
        )
        if label_missing:
            _log.info("retrying gh issue create without label (%s)", label)
            return await _submit_without_label(path=path, title=title, body=body, repo=repo)
        raise GhSubmitError(msg)

    url = (stdout or b"").decode("utf-8", errors="replace").strip()
    if not url.startswith("https://"):
        raise GhSubmitError(f"gh returned unexpected output: {url[:200]}")
    return GhSubmitResult(url=url)


async def _submit_without_label(*, path: str, title: str, body: str, repo: str) -> GhSubmitResult:
    """Fallback path: create the issue with no label if the label is missing."""
    args = [
        path,
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        title,
        "--body-file",
        "-",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=body.encode("utf-8")),
            timeout=_GH_TIMEOUT_SECONDS,
        )
    except TimeoutError as e:
        raise GhSubmitError("gh issue create (no label) timed out") from e
    except OSError as e:
        raise GhSubmitError(f"Failed to invoke gh (no label): {e}") from e
    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", errors="replace").strip()
        raise GhSubmitError(err or "gh issue create failed without label")
    url = (stdout or b"").decode("utf-8", errors="replace").strip()
    return GhSubmitResult(url=url)
