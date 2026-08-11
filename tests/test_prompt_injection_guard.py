"""#1451 — automated PTY writes must not answer an open prompt.

The bug: ``WorkerProcess.send_keys(text, enter=True)`` sends Enter BY DEFAULT,
and 15 of 17 call sites relied on that default. With an AskUserQuestion open, an
automated write either commits the highlighted option or types the message body
in as free text — and the answer is then attributed to the operator.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from swarm.pty.process import WorkerProcess
from swarm.pty.prompt_guard import has_open_selection_prompt

SRC = Path(__file__).resolve().parents[1] / "src" / "swarm"


def _tall_prompt(questions: int = 3, options: int = 4) -> str:
    """An AskUserQuestion set taller than every TAIL_* window."""
    rows: list[str] = []
    for q in range(questions):
        rows.append(f"Question {q + 1} of {questions}: which approach do you want?")
        rows.append("")
        for o in range(options):
            cursor = "❯ " if (q, o) == (0, 0) else "  "
            rows.append(f"{cursor}{o + 1}. a reasonably long option label number {o}")
        rows.append("")
        rows.append("   " + "explanatory detail " * 3)
        rows.append("")
    return "\n".join(rows)


# --------------------------------------------------------------------------
# Detection is STRUCTURAL and has no tail window
# --------------------------------------------------------------------------


def test_detects_three_question_four_option_set() -> None:
    """The prompt shape the old detectors could not see."""
    screen = _tall_prompt()
    assert len(screen.splitlines()) > 15, "fixture must exceed TAIL_MEDIUM to be meaningful"
    assert has_open_selection_prompt(screen) is True


def test_tail_medium_window_would_have_missed_it() -> None:
    """Why is_user_question was the wrong basis — the marker scrolls out.

    This is the negative control for the whole ticket: if this assertion ever
    fails, the fixture stopped being tall enough and the test above proves less
    than it claims.
    """
    from swarm.providers.base import TAIL_MEDIUM

    tail = "\n".join(_tall_prompt().splitlines()[-TAIL_MEDIUM:])
    assert "❯" not in tail
    assert has_open_selection_prompt(tail) is False


@pytest.mark.parametrize(
    "content",
    [
        "",
        "just some ordinary output\nwith no options at all",
        "a line mentioning 1. something inline but no cursor",
        "❯ ",  # bare shell prompt, no numbered options
    ],
)
def test_does_not_fire_without_a_real_menu(content: str) -> None:
    """Positive controls above are worthless unless this discriminates."""
    assert has_open_selection_prompt(content) is False


# --------------------------------------------------------------------------
# Behaviour: held while open, delivered once clear
# --------------------------------------------------------------------------


class _Proc(WorkerProcess):
    """WorkerProcess with the socket write captured instead of performed."""

    def __init__(self) -> None:
        super().__init__(name="t", cwd="/tmp")
        self.writes: list[bytes] = []

    async def _write(self, data: bytes) -> None:  # type: ignore[override]
        self.writes.append(data)


def _show(proc: _Proc, screen: str) -> None:
    """Put ``screen`` on the worker's terminal as the PTY would."""
    proc.buffer.write(screen.encode())


@pytest.mark.asyncio
async def test_automated_write_is_held_while_a_prompt_is_open() -> None:
    proc = _Proc()
    _show(proc, _tall_prompt())
    await proc.send_keys("broadcast body", automated=True)
    assert proc.writes == [], "an automated write reached the PTY with a prompt open"


@pytest.mark.asyncio
async def test_held_message_still_arrives_once_the_prompt_clears() -> None:
    """Held, not dropped. The message must not be lost — only delayed."""
    proc = _Proc()
    _show(proc, _tall_prompt())
    await proc.send_keys("broadcast body", automated=True)
    assert proc.writes == []

    # The operator answers: the menu leaves the screen. Nothing notifies this
    # object, which is why the flush is opportunistic rather than event-driven.
    _answer_the_prompt(proc)
    await proc.send_keys("later message", automated=True)

    sent = b"".join(proc.writes)
    assert b"broadcast body" in sent, "the held message was dropped, not deferred"
    assert sent.index(b"broadcast body") < sent.index(b"later message"), "ordering not preserved"


def _answer_the_prompt(proc: _Proc) -> None:
    """Simulate the operator answering: the menu is replaced by normal output."""
    from swarm.pty.buffer import RingBuffer

    proc.buffer = RingBuffer()
    _show(proc, "the operator answered\nwork continues\n")


@pytest.mark.asyncio
async def test_operator_write_is_never_held() -> None:
    """The operator is the human the prompt is waiting for.

    A guard that blocked them would make an open question unanswerable — the
    failure mode that matters most, because it is silent.
    """
    proc = _Proc()
    _show(proc, _tall_prompt())
    await proc.send_keys("2", automated=False)
    assert proc.writes, "the operator's own answer was swallowed by the guard"


@pytest.mark.asyncio
async def test_automated_write_passes_through_when_no_prompt() -> None:
    proc = _Proc()
    _show(proc, "ordinary worker output\n")
    await proc.send_keys("dispatch", automated=True)
    assert b"dispatch" in b"".join(proc.writes)


# --------------------------------------------------------------------------
# The class cannot regrow — a new unguarded call site fails this test
# --------------------------------------------------------------------------

# Sites that are legitimately un-guarded, each with the reason it is exempt.
_EXEMPT: dict[str, str] = {
    "pty/bridge.py": "the operator's own keystrokes from the terminal UI",
    "pty/process.py": "the guard's own implementation and its deferred flush",
    "server/worker_service.py": (
        "operator dashboard send (forwards `automated`) and the kill path, "
        "which must not be deferrable — see the comments at both sites"
    ),
}


def _call_sites() -> list[tuple[str, int, str]]:
    """Every send_keys(...) call in the package, as (relpath, lineno, source)."""
    found: list[tuple[str, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        lines = text.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "send_keys"):
                continue
            rel = str(path.relative_to(SRC))
            src = "\n".join(lines[node.lineno - 1 : node.end_lineno])
            found.append((rel, node.lineno, src))
    return found


def test_every_send_keys_call_site_is_guarded_or_explicitly_exempt() -> None:
    """A NEW automated send_keys site must not be able to appear unguarded.

    This is the regression test for the SHAPE of the bug rather than one
    instance of it. #1443 fixed one of three approve paths and looked done for a
    month; the same thing happens here the moment someone adds a call site and
    inherits ``enter=True`` without thinking about it.
    """
    unguarded: list[str] = []
    for rel, lineno, src in _call_sites():
        if rel in _EXEMPT:
            continue
        if "automated=" not in src:
            unguarded.append(f"{rel}:{lineno}\n{src.strip()}")
    assert not unguarded, (
        "send_keys call site(s) with no `automated=` argument.\n\n"
        "Enter is the DEFAULT for send_keys, so an unmarked site can answer an\n"
        "open AskUserQuestion under the operator's name (#1451). Pass\n"
        "automated=True if no human chose to send it now, or add the file to\n"
        "_EXEMPT with the reason it is a human path.\n\n" + "\n\n".join(unguarded)
    )


def test_the_scan_can_actually_find_call_sites() -> None:
    """Positive control: an empty `unguarded` list above must mean something."""
    sites = _call_sites()
    assert len(sites) > 10, f"AST scan found only {len(sites)} send_keys sites — scan is broken"
    assert any(rel == "pty/bridge.py" for rel, _, _ in sites)


def test_bridge_still_delivers_operator_keystrokes_unchanged() -> None:
    """AC: pty/bridge.py:57 and :61 must not have acquired a guard."""
    src = (SRC / "pty" / "bridge.py").read_text(encoding="utf-8")
    calls = re.findall(r"send_keys\([^)]*\)", src)
    assert calls, "bridge no longer sends keystrokes at all"
    for call in calls:
        assert "automated=True" not in call, (
            "the terminal bridge carries the operator's OWN keystrokes; "
            "guarding it would make an open prompt unanswerable"
        )


@pytest.mark.asyncio
async def test_operator_answering_does_not_flush_held_writes_into_the_prompt() -> None:
    """The operator's keystroke must not release held writes into the open menu.

    This is the regression that makes deferring worth anything. The operator's
    answer travels through ``send_keys(automated=False)``, which flushes before
    writing. If that flush is unconditional, typing "y" into an open question
    delivers every held broadcast into that question FIRST, with Enter — the
    swarm answers the operator's question a beat before the operator does, which
    is precisely the reported bug, merely delayed by the length of the pause.
    """
    proc = _Proc()
    _show(proc, _tall_prompt())
    await proc.send_keys("worker reply body", automated=True)
    assert proc.writes == []

    # The operator types their answer WHILE the menu is still on screen.
    await proc.send_keys("2", automated=False)

    sent = b"".join(proc.writes)
    assert b"worker reply body" not in sent, (
        "a held write was flushed into a prompt that was still open — "
        "the operator's own keystroke released it"
    )
    assert b"2" in sent, "the operator's answer did not reach the PTY"
