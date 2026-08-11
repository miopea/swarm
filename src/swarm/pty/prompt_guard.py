"""Structural detection of an OPEN selection prompt (#1451).

Lives here, below both ``providers`` and ``server``, so the PTY layer and the
provider layer share ONE definition instead of drifting apart. ``providers.base``
delegates to it; ``WorkerProcess.send_keys`` calls it directly.

WHY THIS EXISTS. ``PtyProcess.send_keys(text, enter=True)`` sends Enter BY
DEFAULT, and 15 of 17 call sites relied on that default. When a worker is showing
an AskUserQuestion, an automated write does one of two things, both bad:

* with a highlighted option, Enter COMMITS it — the swarm answers a question the
  operator was asked, and the answer is recorded as the operator's;
* with a text field focused, the message body is typed in AS FREE TEXT — which is
  what the operator actually observed, and which no option-selection bug can
  produce.

WHY NOT ``is_user_question``. It matches inside ``TAIL_MEDIUM`` (15 lines) and
``has_choice_prompt`` inside ``TAIL_WIDE`` (30). A three-question set with four
options each renders taller than both, so the marker scrolls out of the window
while the prompt is still open and still answerable. **A guard blind to the
tallest prompts reports "safe" exactly when the stakes are highest**, which is
worse than no guard, because it is trusted.

WHY STRUCTURAL, NOT LEXICAL. Keying on the shape a selection prompt has — a
cursored option line with at least one sibling — survives the CLI rewording its
UI. Matching "chat about this" does not.
"""

from __future__ import annotations

import re

# A cursored option (``> 1.`` / ``❯ 2)``) is what makes a stray Enter dangerous:
# it means a choice is highlighted and Enter commits THAT choice.
_RE_CURSOR_OPTION = re.compile(r"^\s*[>❯]\s*\d+[.)]", re.MULTILINE)
# At least one sibling option, so a shell prompt like ``> 1.5`` cannot masquerade
# as a menu. Requiring both halves is what keeps this from firing on ordinary output.
_RE_SIBLING_OPTION = re.compile(r"^\s+\d+[.)]", re.MULTILINE)


def has_open_selection_prompt(content: str) -> bool:
    """True when ``content`` shows a prompt that a stray Enter would answer.

    Scans the WHOLE string deliberately — callers pass a generous window
    (``_PROMPT_SCAN_LINES``) rather than a tail slice, because prompt height is
    the thing that defeated the previous detectors.
    """
    if not content:
        return False
    return bool(_RE_CURSOR_OPTION.search(content)) and bool(_RE_SIBLING_OPTION.search(content))
