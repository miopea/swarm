"""Read an open selection prompt: its options, its cursor, and a stable identity (#1608).

COMPANION TO ``prompt_guard``, NOT A REPLACEMENT. That module answers "is a prompt open?"
and deliberately stays structural and cheap, because it gates every automated write. This
one answers "WHAT is being asked, and which option is highlighted?" — needed only when
somebody intends to answer, and allowed to be more detailed.

WHY THIS PARSES RENDERED TEXT. There is no machine-readable option list anywhere in the
stack. The Queen reads `queen_view_worker_state`'s ``pty_tail`` and infers the cursored
line from the ❯ glyph; so does this. Measured against two REAL pickers captured
2026-08-14 before they cleared:

    nexus, permission picker  — ~18 rendered lines, options "❯ 1. Yes" / "   2. No",
                                footer "Esc to cancel · Tab to amend · ctrl+e to explain"
    platform-api, plan picker — ~12 rendered lines, "❯ 1. Yes, and use auto mode" /
                                "   2. Yes, manually approve edits" /
                                "   3. Tell Claude what to change"

Both fit inside the default 50-line tail with room to spare, which is why the READ half of
#1608 needed no code at all — only this parse, for the ANSWER half.

THE FINGERPRINT EXCLUDES THE CURSOR, AND THAT IS THE WHOLE POINT. Moving the highlight
with arrow keys does not make it a different question, so a fingerprint that changed on
cursor movement would refuse valid answers and train callers to retry until it passed —
which is worse than no check. It covers the option TEXTS and their order: if those change,
a different question is being asked and an answer aimed at the old one must not land.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# An option line: optional cursor glyph, a number, a separator, then the label.
# Anchored to line start so a "1." inside prose cannot masquerade as an option.
_RE_OPTION = re.compile(r"^\s*(?P<cursor>[>❯])?\s*(?P<num>\d+)[.)]\s+(?P<label>\S.*?)\s*$")

# Continuation hints the CLI renders under an option ("shift+tab to approve with this
# feedback"). They are not options and must not be numbered as ones.
_RE_HINT = re.compile(r"^\s+(shift\+tab|ctrl\+|esc |tab )", re.IGNORECASE)


@dataclass(frozen=True)
class PromptOption:
    number: int
    label: str
    cursored: bool


@dataclass(frozen=True)
class OpenPrompt:
    """A parsed selection prompt. ``fingerprint`` identifies the QUESTION, not the cursor."""

    options: tuple[PromptOption, ...]
    fingerprint: str

    @property
    def cursored(self) -> PromptOption | None:
        return next((o for o in self.options if o.cursored), None)

    def option(self, number: int) -> PromptOption | None:
        return next((o for o in self.options if o.number == number), None)


def _fingerprint(labels: list[str]) -> str:
    """Stable identity for a question: its option labels, in order, cursor excluded.

    Short on purpose — this travels through an MCP argument and gets pasted into
    resolutions; a 64-char hash would be copied wrongly. 12 hex chars over a
    normalised join is ample to distinguish the prompts a worker actually shows.
    """
    joined = "\x1f".join(" ".join(label.split()) for label in labels)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def parse_open_prompt(content: str) -> OpenPrompt | None:
    """Parse the selection prompt in ``content``, or None when there is not one.

    Returns None rather than an empty prompt when nothing parses, so a caller cannot
    accidentally treat "no prompt" as "a prompt with no options" and answer into it.
    """
    if not content:
        return None
    options: list[PromptOption] = []
    for line in content.splitlines():
        if _RE_HINT.match(line):
            continue
        m = _RE_OPTION.match(line)
        if not m:
            continue
        options.append(
            PromptOption(
                number=int(m.group("num")),
                label=m.group("label").strip(),
                cursored=bool(m.group("cursor")),
            )
        )
    # A single numbered line is not a menu — the same two-part requirement prompt_guard
    # uses, so the two modules cannot disagree about whether a prompt is open.
    if len(options) < 2:
        return None
    # Keep the LAST contiguous run: a scrollback can hold an older, already-answered
    # menu above the live one, and answering the stale one is the exact race this
    # ticket exists to prevent.
    run: list[PromptOption] = [options[-1]]
    for opt in reversed(options[:-1]):
        if opt.number == run[0].number - 1:
            run.insert(0, opt)
        else:
            break
    if len(run) < 2:
        return None
    return OpenPrompt(options=tuple(run), fingerprint=_fingerprint([o.label for o in run]))
