"""#1608 — parsing a real selection prompt, and identifying it stably.

THE FIXTURES BELOW ARE REAL. Both were captured from live pickers on 2026-08-14 before
they cleared — nexus's permission prompt and platform-api's plan prompt, the two that cost
8.16h and 6.64h respectively. They are not my idea of what a prompt looks like, which is
the distinction this ticket's acceptance criteria were written around: a test that drives
the parser with a synthetic prompt is testing the parser against my assumptions.

The rendered heights were ~18 and ~12 lines, both inside the default 50-line
`queen_view_worker_state` tail — which is why the READ half of #1608 needed no code.
"""

from __future__ import annotations

from swarm.pty.prompt_guard import has_open_selection_prompt
from swarm.pty.prompt_options import parse_open_prompt

# --- REAL CAPTURES -----------------------------------------------------------------

NEXUS_PERMISSION = """\
  Bash command
    psql -h localhost -U app -c "CREATE TABLE probe_least_privilege (id int);"

  3 consecutive actions were blocked
  stage-2 classifier error: could not determine intent

  Do you want to proceed?
❯ 1. Yes
   2. No

  Esc to cancel · Tab to amend · ctrl+e to explain
"""

PLATFORM_API_PLAN = """\
  Ready to code?

  Here is the plan for #1599:
  /home/bschleifer/.claude/plans/some-plan.md

  Would you like to proceed?
❯ 1. Yes, and use auto mode
   2. Yes, manually approve edits
   3. Tell Claude what to change
      shift+tab to approve with this feedback
"""


# ---------------------------------------------------------------------------
# The two real prompts parse
# ---------------------------------------------------------------------------


def test_the_nexus_permission_prompt_parses():
    p = parse_open_prompt(NEXUS_PERMISSION)

    assert p is not None
    assert [(o.number, o.label) for o in p.options] == [(1, "Yes"), (2, "No")]
    assert p.cursored is not None and p.cursored.number == 1


def test_the_platform_api_plan_prompt_parses():
    """Three options, and the `shift+tab` hint under option 3 must NOT become a fourth."""
    p = parse_open_prompt(PLATFORM_API_PLAN)

    assert p is not None
    assert [o.number for o in p.options] == [1, 2, 3]
    assert p.option(1).label == "Yes, and use auto mode"
    assert p.option(3).label == "Tell Claude what to change"
    assert p.cursored.number == 1


def test_option_1_on_the_plan_prompt_is_the_approval_the_queen_wanted():
    """The concrete thing that cost 6.64 hours: approving #1599's plan is selecting 1.
    Interrupt cannot produce this — Ctrl-C cancels the plan — which is why the answer
    capability is genuinely missing rather than covered by the existing tool."""
    p = parse_open_prompt(PLATFORM_API_PLAN)

    assert p.option(1).label.startswith("Yes")


def test_this_module_agrees_with_the_guard_about_both_real_prompts():
    """`prompt_guard` decides whether writes are held; this decides what is being asked.
    If they disagreed, a prompt could be un-answerable and un-writable at once — or worse,
    answerable while the guard thought nothing was open."""
    for content in (NEXUS_PERMISSION, PLATFORM_API_PLAN):
        assert has_open_selection_prompt(content)
        assert parse_open_prompt(content) is not None


# ---------------------------------------------------------------------------
# The fingerprint — identity of the QUESTION, not of the cursor
# ---------------------------------------------------------------------------


def test_moving_the_cursor_does_not_change_the_fingerprint():
    """THE DESIGN DECISION, pinned. Arrowing down does not make it a different question.
    A fingerprint that changed on cursor movement would refuse valid answers and train
    callers to retry until one passed — worse than no check at all."""
    moved = PLATFORM_API_PLAN.replace("❯ 1. Yes, and use", "   1. Yes, and use").replace(
        "   2. Yes, manually", "❯ 2. Yes, manually"
    )

    before = parse_open_prompt(PLATFORM_API_PLAN)
    after = parse_open_prompt(moved)

    assert after.cursored.number == 2, "precondition: the cursor really moved"
    assert before.fingerprint == after.fingerprint


def test_a_different_question_gets_a_different_fingerprint():
    """The half that makes it a guard rather than a formality — this is the stale-answer
    race the ticket exists to prevent."""
    assert parse_open_prompt(NEXUS_PERMISSION).fingerprint != (
        parse_open_prompt(PLATFORM_API_PLAN).fingerprint
    )


def test_changing_one_option_label_changes_the_fingerprint():
    """A prompt that swapped an option between read and answer must not accept an answer
    aimed at the old one, even though the option COUNT is unchanged."""
    swapped = PLATFORM_API_PLAN.replace("Tell Claude what to change", "Reject and stop")

    assert (
        parse_open_prompt(swapped).fingerprint != parse_open_prompt(PLATFORM_API_PLAN).fingerprint
    )


def test_the_fingerprint_is_short_enough_to_paste():
    """It travels through an MCP argument and into resolutions; a 64-char hash gets
    copied wrongly."""
    assert len(parse_open_prompt(NEXUS_PERMISSION).fingerprint) == 12


# ---------------------------------------------------------------------------
# Refusing to parse things that are not prompts
# ---------------------------------------------------------------------------


def test_ordinary_output_with_a_numbered_line_is_not_a_prompt():
    """A single numbered line is not a menu — the same two-part requirement
    `prompt_guard` uses, so the two cannot disagree about whether a prompt is open."""
    assert parse_open_prompt("Running step 1. Done\n  1. only one thing here\n") is None


def test_empty_and_missing_content_return_none_not_an_empty_prompt():
    """None, not an empty OpenPrompt: a caller must not be able to treat "no prompt" as
    "a prompt with no options" and answer into nothing."""
    assert parse_open_prompt("") is None
    assert parse_open_prompt("just some log output\nnothing to choose\n") is None


def test_a_stale_menu_higher_in_the_scrollback_does_not_win():
    """Scrollback can hold an already-answered menu above the live one. Answering the
    stale one is precisely the race #1608 exists to prevent, so the LAST contiguous run
    is the live prompt."""
    content = (
        "  1. Old A\n  2. Old B\n\n…answered…\n\n"
        "  Do you want to proceed?\n❯ 1. New A\n   2. New B\n"
    )

    p = parse_open_prompt(content)

    assert [o.label for o in p.options] == ["New A", "New B"]
