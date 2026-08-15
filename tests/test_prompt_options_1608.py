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

import pytest

from swarm.pty.process import WorkerProcess
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


# ---------------------------------------------------------------------------
# AC4 (second half) — #1451's hold still fires, against the REAL prompts
# ---------------------------------------------------------------------------
#
# `test_prompt_injection_guard.py` already covers the hold, and covers it well —
# but its fixture is GENERATED (`cursor = "❯ " if (q, o) == (0, 0) else "  "`).
# These two prompts are the ones that actually cost 14.8 worker-hours, so the
# guard is now exercised against the text a real picker renders rather than the
# text a test author imagined. That distinction is the whole reason this ticket
# refuses a synthetic end-to-end proof.


class _CapturingProc(WorkerProcess):
    """WorkerProcess with the socket write captured instead of performed."""

    def __init__(self) -> None:
        super().__init__(name="t", cwd="/tmp")
        self.writes: list[bytes] = []

    async def _write(  # type: ignore[override]
        self, data: bytes, *, actor: str = "unknown"
    ) -> None:
        # #1658 added `actor` at the choke point; doubles must accept it or every
        # write path through them raises TypeError instead of exercising the guard.
        _ = actor
        self.writes.append(data)


@pytest.mark.parametrize(
    ("label", "screen"),
    [("nexus permission", NEXUS_PERMISSION), ("platform-api plan", PLATFORM_API_PLAN)],
)
@pytest.mark.asyncio
async def test_an_automated_write_is_still_held_by_these_real_prompts(label: str, screen: str):
    """The guard must not be weakened by anything #1608 adds. If an ordinary dispatch,
    nudge or broadcast reached the PTY here, the swarm would answer — under the
    operator's name — the question the operator was asked."""
    proc = _CapturingProc()
    proc.buffer.write(screen.encode())

    await proc.send_keys("routine dispatch body", automated=True)

    assert proc.writes == [], f"an automated write reached the PTY during the {label} prompt"


@pytest.mark.asyncio
async def test_the_held_write_is_delayed_not_dropped():
    """Held, not dropped — the property that makes the stall silent rather than loud,
    and the reason nexus's 8.16h looked like nothing was happening."""
    proc = _CapturingProc()
    proc.buffer.write(NEXUS_PERMISSION.encode())
    await proc.send_keys("first", automated=True)
    assert proc.writes == []

    from swarm.pty.buffer import RingBuffer

    proc.buffer = RingBuffer()
    proc.buffer.write(b"the prompt was answered\nwork continues\n")
    await proc.send_keys("second", automated=True)

    sent = b"".join(proc.writes)
    assert b"first" in sent, "the held write was dropped rather than deferred"
    assert sent.index(b"first") < sent.index(b"second"), "ordering was not preserved"


@pytest.mark.asyncio
async def test_an_operator_write_is_never_held_by_a_real_prompt():
    """POSITIVE CONTROL. The operator is exactly the human the prompt waits for; a guard
    that blocked them would make an open question unanswerable — and would look identical
    to the guard working."""
    proc = _CapturingProc()
    proc.buffer.write(PLATFORM_API_PLAN.encode())

    await proc.send_keys("1", automated=False)

    assert proc.writes, "the operator's own answer was held — the prompt is unanswerable"


# ---------------------------------------------------------------------------
# AC2 — the stale-prompt REFUSAL, which is the whole point of the fingerprint
# ---------------------------------------------------------------------------


def _svc_with_screen(screen: str, name: str = "platform-api"):
    """A WorkerService whose single worker's PTY shows `screen`."""
    from unittest.mock import MagicMock

    from swarm.server.worker_service import WorkerService

    svc = WorkerService.__new__(WorkerService)
    worker = MagicMock()
    worker.name = name
    worker.process.get_content.return_value = screen
    svc._get_workers = lambda: [worker]  # type: ignore[method-assign]
    svc._drone_log = MagicMock()
    svc._get_pilot = lambda: None  # type: ignore[method-assign]
    return svc, worker


def _fingerprint_of(screen: str) -> str:
    p = parse_open_prompt(screen)
    assert p is not None
    return p.fingerprint


def test_a_matching_fingerprint_is_accepted():
    """POSITIVE CONTROL FIRST. Without it, a check that refused everything would pass
    every refusal test below while making the feature useless — and would look
    identical to a working guard."""
    svc, _ = _svc_with_screen(PLATFORM_API_PLAN)

    ok, message = svc.check_prompt_answer("platform-api", 1, _fingerprint_of(PLATFORM_API_PLAN))

    assert ok is True
    assert "Yes, and use auto mode" in message


def test_a_prompt_that_changed_in_between_is_REFUSED():
    """AC2. The Queen reads platform-api's plan prompt, and by the time she answers the
    worker is showing nexus's permission prompt instead. Answering '1' there would grant
    a permission she never read — which is the failure #1451's guard exists to prevent
    and which this path must not reintroduce."""
    stale = _fingerprint_of(PLATFORM_API_PLAN)
    svc, _ = _svc_with_screen(NEXUS_PERMISSION)  # the screen moved on

    ok, message = svc.check_prompt_answer("platform-api", 1, stale)

    assert ok is False
    assert "changed since you read it" in message
    assert stale in message, "the refusal must name the fingerprint that was sent"


def test_answering_when_no_prompt_is_open_is_REFUSED():
    """Someone else already answered it. Sending '1' into a live session types a stray
    character into whatever is on screen now."""
    svc, _ = _svc_with_screen("the prompt was answered\nwork continues\n")

    ok, message = svc.check_prompt_answer("platform-api", 1, "abc123abc123")

    assert ok is False
    assert "no selection prompt is open" in message


def test_an_option_number_not_on_the_prompt_is_REFUSED_and_lists_the_real_ones():
    """nexus's prompt has two options. Answering '3' must not silently do nothing, and
    the refusal has to say what WAS available or the caller just guesses again."""
    svc, _ = _svc_with_screen(NEXUS_PERMISSION, name="nexus")

    ok, message = svc.check_prompt_answer("nexus", 3, _fingerprint_of(NEXUS_PERMISSION))

    assert ok is False
    assert "not on this prompt" in message
    assert "1, 2" in message


def test_the_check_never_sends_anything():
    """It is a CHECK. If it wrote to the PTY, a refusal would still have answered the
    prompt — the exact bug it exists to prevent, hidden inside the guard."""
    svc, worker = _svc_with_screen(PLATFORM_API_PLAN)

    svc.check_prompt_answer("platform-api", 1, _fingerprint_of(PLATFORM_API_PLAN))
    svc.check_prompt_answer("platform-api", 1, "wrongfingerprint")

    worker.process.send_keys.assert_not_called()
