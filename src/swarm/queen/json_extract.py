"""Shared JSON extraction for headless-LLM output.

Both the headless Queen (``queen.py``) and the verifier (``verifier.py``)
parse a ``claude -p`` stdout blob that may be plain JSON, JSON inside a
markdown code fence (often with trailing prose), or a JSON object embedded
in surrounding text. This is the single implementation both import — keeping
the brace-matching / fence-parsing logic in one place.
"""

from __future__ import annotations

import json
import re
from typing import Any

from swarm.logging import get_logger

_log = get_logger("queen.json_extract")

# Matches a fenced JSON code block — models often add markdown text after the
# closing fence, which broke the old startswith/endswith parser.
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any] | None:
    """Extract and parse a JSON object from model output.

    Tries, in order: plain JSON, a markdown ``json`` fence, then the first
    balanced ``{...}`` block found via bracket matching. Returns ``None`` when
    none of those yield a dict.
    """
    stripped = text.strip()
    return _try_plain(stripped) or _try_fenced(stripped) or _try_balanced(stripped)


def _try_plain(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _try_fenced(text: str) -> dict[str, Any] | None:
    m = _JSON_FENCE_RE.search(text)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(1))
    except json.JSONDecodeError:
        _log.debug("JSON fence found but parse failed")
        return None
    return parsed if isinstance(parsed, dict) else None


def _try_balanced(text: str) -> dict[str, Any] | None:
    """Bracket-match the first balanced ``{...}`` block and parse it."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return _try_plain(text[start : i + 1])
    return None
