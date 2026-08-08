"""Every element id the config page reads must exist in its markup.

THE BUG THIS CLASS PRODUCES, hit while building the Jira v2 setup UI. Renaming an
input from ``cfg-jira-project`` to ``cfg-jira-projects`` left the save handler calling
``document.getElementById('cfg-jira-project').value`` — which is ``null.value``, a
TypeError that aborts ``saveSettings`` before ANY section is written. Renaming one Jira
field would have silently broken saving the entire configuration: workers, LLMs,
approval rules, everything.

It is the same shape as ``.text-center`` being used in eight places and defined nowhere
(#1291), and as ``.type-cross`` having no background (2026.8.7.10): markup and the code
that references it drift apart, nothing checks, and the failure surfaces somewhere
unrelated to the change.

A scan cannot prove the page WORKS — only the browser tests do that. It can prove that
every id the JavaScript reaches for is actually rendered, which is the specific
drift that costs a whole save.
"""

from __future__ import annotations

import re
from pathlib import Path

_CONFIG = Path(__file__).parent.parent / "src" / "swarm" / "web" / "templates" / "config.html"
_SRC = _CONFIG.read_text()


def _strip_comment_lines(src: str) -> str:
    """Drop whole-line JS/HTML/Jinja comments before scanning.

    Scans in this repo have repeatedly matched the PROSE EXPLAINING A BUG rather than
    the bug — the comments added with this fix name several element ids while
    describing what changed.
    """
    out = []
    for line in src.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith(("//", "/*", "*/", "* ", "{#", "<!--")):
            continue
        out.append(line)
    return "\n".join(out)


_CODE = _strip_comment_lines(_SRC)

# ids rendered by the template. THREE sources, and missing any of them turns this
# sweep into a false-positive machine:
#   1. literal id="x" in config.html
#   2. ids passed to Jinja MACROS — config_number('sync_interval_minutes', 'help',
#      'cfg-jira-sync_interval', ...) renders that id with no literal id= anywhere.
#      Whatever string the macro is handed BECOMES a rendered id, so collecting the
#      quoted arguments is correct rather than lenient.
#   3. ids in base.html, which config.html extends (e.g. the toast container).
_BASE = (
    Path(__file__).parent.parent / "src" / "swarm" / "web" / "templates" / "base.html"
).read_text()
_RENDERED = set(re.findall(r"""id=["']([\w-]+)["']""", _CODE))
_RENDERED |= set(re.findall(r"""id=["']([\w-]+)["']""", _BASE))
for _call in re.findall(r"\{\{\s*config_\w+\((.*?)\)\s*\}\}", _CODE, re.S):
    _RENDERED |= set(re.findall(r"""["']([\w-]+)["']""", _call))
# ids the JavaScript reads AND IMMEDIATELY DEREFERENCES — getElementById('x').value.
#
# Deliberately not every reference. `var el = getElementById('x'); if (el) ...` is a
# null-GUARDED lookup: a missing id makes it a harmless no-op. config.html has one such
# reference (tool-buttons-list, a drag-reorder list that no longer exists) which is
# dead code rather than a defect, and flagging it would have meant either a false
# failure or deleting an unrelated line to make a test pass.
#
# The dangerous shape is the unguarded dereference, because that is what threw and took
# the whole save with it: getElementById('cfg-jira-project').value on a renamed input.
_REFERENCED = set(re.findall(r"""getElementById\(\s*["']([\w-]+)["']\s*\)\s*\.""", _CODE))


def test_the_scan_finds_both_sides():
    """Positive control. If either set were empty the sweep below would pass over
    nothing, which is exactly the failure mode it exists to catch."""
    assert len(_RENDERED) > 50, f"only {len(_RENDERED)} rendered ids found; scan is broken"
    assert len(_REFERENCED) > 30, f"only {len(_REFERENCED)} referenced ids found; scan is broken"
    assert "cfg-jira-projects" in _RENDERED, "the Jira projects input is not rendered"


def test_every_id_the_page_reads_is_rendered():
    """THE SWEEP. A missing id is not a cosmetic bug: getElementById returns null, and
    the first `.value` on it throws — aborting saveSettings before anything is written,
    however unrelated the section."""
    missing = sorted(_REFERENCED - _RENDERED)
    assert not missing, (
        f"config.html dereferences element id(s) it never renders: {missing}. "
        f"getElementById returns null for these, so the first property access throws "
        f"and takes the whole save with it."
    )


def test_the_jira_setup_ids_line_up_specifically():
    """The instance that produced this file, pinned so the rename cannot regress."""
    for element_id in (
        "cfg-jira-projects",
        "jira-discover-project",
        "jira-discover-result",
        "jira-plan-result",
    ):
        assert element_id in _RENDERED, f"{element_id} is not rendered"
    assert "cfg-jira-project'" not in _CODE.replace("cfg-jira-projects", ""), (
        "the save handler still reads the pre-rename single-project id"
    )
