"""#1659 — the drone log said "choice menu — selected 'X'" when nothing was selected.

THIS STRING COST A FALSE HIGH-PRIORITY SECURITY ESCALATION on 2026-08-15, and it nearly
reached the operator as a live defect.

`_decide_choice` builds its label from `provider.get_choice_summary(content)`, which scans
the SCREEN for the line matching the cursor regex — the option currently HIGHLIGHTED. The
drone that logs this line is deciding to ESCALATE; it is explicitly declining to act. The
rendered row nevertheless read:

    ESCALATED  user question: choice menu — selected '… → 1. HIGHLIGHTED-CANARY-A'

The Queen read three consecutive rows during a #1648 verification, concluded a drone had
escalated and then selected the highlighted option anyway — a defect shaped exactly like
#1645 — and relayed it to the operator before it was checked against an unfiltered window.
There was no action row; the drone escalated and stopped. The reasoning was sound and the
log lied about tense.

SAME FAMILY AS `Prompt sent` (#1608), `Interrupt sent` (#1608/#1633) and
`queen_dismiss_prompt`'s unobserved claim (#1623) — an intention or a screen state written
in the past tense of a completed action. This is that pattern surviving in the drone log,
where humans read it and no tool consumes it.

THE GENERAL RULE: a log line describing a SCREEN STATE must not use the past tense of an
ACTION. The reader cannot tell them apart, and here the reader was the Queen with the
source available to her.
"""

from __future__ import annotations

import re

from swarm.drones.rules import _decide_choice

REAL_PICKER = """\
 #1648 CHECK 5 — do not answer this by hand.

❯ 1. HIGHLIGHTED-CANARY-A
     First option, highlighted on open.
   2. RESCUE-OPTION-B
     Deliberate rescue only.
"""


def _label(content: str) -> str:
    """The reason string the drone would log for this screen."""
    from unittest.mock import MagicMock

    from swarm.config.models import DroneConfig

    worker = MagicMock()
    worker.name = "swarm"
    decision = _decide_choice(
        worker,
        content,
        content.splitlines(),
        DroneConfig(approval_rules=[], allowed_read_paths=[]),
        {},
    )
    return decision.reason


def test_the_label_does_not_claim_a_selection_was_made():
    """THE DEFECT. Nothing was selected — the drone read the screen and escalated."""
    label = _label(REAL_PICKER)

    assert "selected" not in label.lower(), (
        f"the label still reads as a completed action: {label!r}"
    )


def test_the_label_says_the_option_is_highlighted_and_unanswered():
    """The replacement has to carry BOTH halves: which option the cursor is on, and that
    no choice has been recorded. Dropping the option text would lose the diagnostic value
    the original had."""
    label = _label(REAL_PICKER)

    assert "highlighted" in label.lower()
    assert "no selection" in label.lower()
    assert "HIGHLIGHTED-CANARY-A" in label


def test_no_past_tense_action_verb_survives_in_the_label():
    """AC2, as a general guard rather than a spelling check — the next author adding a
    verb here should trip this, not rediscover the incident."""
    label = _label(REAL_PICKER).lower()

    forbidden = re.compile(
        r"\b(selected|chose|answered|approved|confirmed|accepted|dismissed|continued)\b"
    )
    match = forbidden.search(label)
    assert match is None, f"past-tense action verb {match.group(0)!r} in a screen-state label"


def test_a_screen_with_no_cursor_option_still_gets_a_plain_label():
    """POSITIVE CONTROL. The empty-summary branch must keep working — a change that made
    every label mention a highlighted option would be wrong for a menu with no cursor."""
    label = _label("some output with no numbered options at all\n> ")

    assert "highlighted" not in label.lower()
