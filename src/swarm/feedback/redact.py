"""Redaction engine for feedback payloads.

Scrubs paths, secret-shaped tokens, env var values, emails, and auth URLs
before anything leaves the user's machine.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Secret-shaped regex patterns. Each is a (pattern, replacement) tuple.
# Order matters: more-specific patterns must come before more-general ones.
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # GitHub tokens
    (re.compile(r"ghp_[A-Za-z0-9]{36}"), "<github-token>"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{80,}"), "<github-token>"),
    (re.compile(r"gho_[A-Za-z0-9]{36}"), "<github-token>"),
    (re.compile(r"ghs_[A-Za-z0-9]{36}"), "<github-token>"),
    # Anthropic / OpenAI
    (re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"), "<api-key>"),
    (re.compile(r"sk-[A-Za-z0-9\-_]{20,}"), "<api-key>"),
    # AWS
    (re.compile(r"AKIA[0-9A-Z]{16}"), "<aws-key>"),
    (
        re.compile(r"aws_secret_access_key\s*=\s*\S+", re.IGNORECASE),
        "aws_secret_access_key=<redacted>",
    ),
    # JWTs
    (
        re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
        "<jwt>",
    ),
    # Bearer tokens
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}"), "Bearer <redacted>"),
    # Webhook URLs whose secret lives in the PATH (the host alone is safe).
    (re.compile(r"https://hooks\.slack\.com/services/\S+"), "<slack-webhook>"),
    (
        re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\S+"),
        "<discord-webhook>",
    ),
    # Secret-bearing query params (ntfy ?auth=, generic ?token=/?key=/?secret=).
    # Keep the param NAME, blank the value, stop at the next & or whitespace.
    (
        re.compile(
            r"([?&](?:auth|token|access_token|api[_-]?key|key|secret|password)=)[^&\s]+",
            re.IGNORECASE,
        ),
        r"\1<redacted>",
    ),
    # Generic long hex (session IDs, SHA-256 digests, etc.)
    (re.compile(r"\b[a-fA-F0-9]{32,}\b"), "<hex-secret>"),
]

# Auth URL with inline credentials: https://user:pass@host
_AUTH_URL_RE = re.compile(r"(https?://)[^\s/:@]+:[^\s/@]+@")

# Email addresses
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# Minimum length an env var value must have before we bother scrubbing it.
# (Prevents replacing every instance of "1" or "true" if PATH or DEBUG=1 etc.)
_ENV_SCRUB_MIN_LEN = 6


def _home_path_pattern() -> tuple[re.Pattern[str], str] | None:
    """Build a regex that replaces the user's absolute home path with ``~``."""
    try:
        home = str(Path.home())
    except (RuntimeError, OSError):
        return None
    if not home or home == "/":
        return None
    return (re.compile(re.escape(home)), "~")


def _collect_env_values_to_scrub(
    env_refs: list[str] | None,
) -> list[str]:
    """Return the current values of env vars that should be scrubbed.

    ``env_refs`` is a list of environment-variable names that swarm.yaml
    references via ``$VAR_NAME``. We pull their current values from
    ``os.environ`` and return any that are long enough to be worth
    scrubbing (short values create too many false positives).
    """
    if not env_refs:
        return []
    values: list[str] = []
    for name in env_refs:
        val = os.environ.get(name, "")
        if val and len(val) >= _ENV_SCRUB_MIN_LEN:
            values.append(val)
    # Longest first so we replace containing values before substrings
    values.sort(key=len, reverse=True)
    return values


def redact_text(
    text: str,
    *,
    env_refs: list[str] | None = None,
) -> tuple[str, int]:
    """Apply all redaction rules to *text*.

    Returns ``(redacted_text, replacement_count)``. The count is the total
    number of substitutions made across all rules — useful for the UI's
    "N items redacted" badge.
    """
    if not text:
        return text, 0

    total = 0

    # 1. Home path → ~
    home_pat = _home_path_pattern()
    if home_pat is not None:
        pat, repl = home_pat
        text, n = pat.subn(repl, text)
        total += n

    # 2. Env var values referenced by swarm.yaml
    for value in _collect_env_values_to_scrub(env_refs):
        # Use literal string replace (faster than regex, no metachar issues)
        occurrences = text.count(value)
        if occurrences:
            text = text.replace(value, "<env-secret>")
            total += occurrences

    # 3. Known secret shapes
    for pat, repl in _SECRET_PATTERNS:
        text, n = pat.subn(repl, text)
        total += n

    # 4. Auth URLs with inline credentials
    text, n = _AUTH_URL_RE.subn(r"\1<redacted>@", text)
    total += n

    # 5. Emails
    text, n = _EMAIL_RE.subn("<email>", text)
    total += n

    return text, total


_SENSITIVE_KEY_RE = re.compile(r"(?i)(token|secret|password|api[_-]?key|client_secret)")


def redact_config_dict(data: object) -> tuple[object, int]:
    """Walk a nested dict/list structure and blank values under sensitive keys.

    Returns ``(redacted_structure, replacement_count)``. The caller should
    then stringify the result and run :func:`redact_text` over it to catch
    any remaining secret-shaped values.
    """
    count = 0

    def _walk(node: object) -> object:
        nonlocal count
        if isinstance(node, dict):
            out: dict[str, object] = {}
            for k, v in node.items():
                if isinstance(k, str) and _SENSITIVE_KEY_RE.search(k):
                    if v not in (None, "", [], {}):
                        count += 1
                        out[k] = "<redacted>"
                    else:
                        out[k] = v
                else:
                    out[k] = _walk(v)
            return out
        if isinstance(node, list):
            return [_walk(x) for x in node]
        return node

    return _walk(data), count
