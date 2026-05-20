"""Regression tests for HTML template attribute correctness.

Duplicate class= attributes on HTML elements cause the browser to ignore
all but the first, breaking styles (e.g. width: 120px instead of 100%).
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "src" / "swarm" / "web" / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "src" / "swarm" / "web" / "static"

# Matches opening HTML tags (possibly spanning multiple lines)
_TAG_RE = re.compile(r"<[a-zA-Z][^>]*>", re.DOTALL)
# Matches individual class="..." attributes within a tag
_CLASS_ATTR_RE = re.compile(r'\bclass\s*=\s*"[^"]*"')


def test_no_duplicate_class_attributes():
    """Every HTML tag should have at most one class= attribute."""
    errors: list[str] = []
    for template in sorted(TEMPLATES_DIR.glob("*.html")):
        content = template.read_text()
        lines = content.split("\n")
        # Track character offset → line number
        offset_to_line: list[int] = []
        for i, line in enumerate(lines, 1):
            offset_to_line.extend([i] * (len(line) + 1))  # +1 for \n
        for m in _TAG_RE.finditer(content):
            tag_text = m.group()
            class_matches = _CLASS_ATTR_RE.findall(tag_text)
            if len(class_matches) > 1:
                line_no = offset_to_line[m.start()] if m.start() < len(offset_to_line) else "?"
                errors.append(
                    f"{template.name}:{line_no} — tag has {len(class_matches)} "
                    f"class attributes: {class_matches}"
                )
    assert not errors, "Duplicate class= attributes found:\n" + "\n".join(errors)


def test_proposal_buttons_say_dismiss_not_reject():
    """Proposal reject buttons should be labelled 'Dismiss', not 'Reject'."""
    template = (TEMPLATES_DIR / "dashboard.html").read_text()
    js = (STATIC_DIR / "dashboard.js").read_text()
    # No reject-proposal button should have ">Reject<" label
    assert ">Reject<" not in template, "dashboard.html still has a >Reject< button label"
    assert ">Reject<" not in js, "dashboard.js still has a >Reject< button label"


def test_dashboard_has_paste_interception():
    """Ctrl-V paste must be intercepted so raw 0x16 doesn't reach Claude Code.

    The inline xterm.js terminal needs:
    1. attachCustomKeyEventHandler blocking Ctrl+V
    2. Capture-phase paste handler on the textarea
    Without these, Claude Code shows "No images found in clipboard" on paste.
    """
    content = (STATIC_DIR / "dashboard.js").read_text()
    # attachCustomKeyEventHandler must appear at least once (inline terminal)
    assert content.count("attachCustomKeyEventHandler") >= 1, (
        "dashboard.js must block Ctrl+V via attachCustomKeyEventHandler on the inline terminal"
    )
    # Capture-phase paste handlers (addEventListener('paste', ..., true))
    assert content.count("addEventListener('paste'") >= 2, (
        "dashboard.js must have capture-phase paste handlers"
    )


def test_question_mark_shortcut_skips_contenteditable():
    """The global ? help-shortcut handler must skip when the user is
    typing in a contenteditable element — the task editor's description
    field is a contenteditable div, and a missing isContentEditable
    guard swallows the ? keystroke and pops the shortcuts modal instead
    of letting the operator type a question mark.
    """
    js = (STATIC_DIR / "dashboard.js").read_text()
    marker = "? key opens keyboard shortcut help"
    i = js.find(marker)
    assert i >= 0, "expected the ? shortcut handler block in dashboard.js"
    # Inspect only the handler region (next ~900 chars after the comment).
    block = js[i : i + 900]
    assert "isContentEditable" in block, (
        "the ? shortcut handler must guard on isContentEditable so the "
        "task editor (contenteditable description) accepts a literal '?'"
    )
