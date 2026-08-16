"""#1675 — every PTY keystroke call names its actor, and the next one that forgets fails CI.

The #1658 audit shipped and within minutes recorded an `unknown` actor row on
platform-data. A bare Enter is the write that ANSWERS AN OPEN PICKER by selecting the
highlighted option — #1443's whole failure mode — so an unlabelled `enter` is the single
most important row type to be able to trace, and it was the one we could not.

FOUND BY READING THE LIVE AUDIT, not the code. 823 rows over ~55 minutes, 14 of them
`unknown`, in exactly two shapes:

  A. An extra Enter after every automated dispatch, always the same triple:
         automated  text   Nb
         automated  enter  1b
         unknown    enter  1b   <- ~0.3s later, every time
     That is `task_coordinator`'s SUBMIT NUDGE — after a multi-line paste the CLI needs a
     second Enter. The byte counts pin each of its three sites: `/goal clear` is exactly
     11 characters, and `automated text 11b` precedes an unknown Enter at 21:20:11 and
     21:23:12.

  B. Unlabelled 3-byte arrows on queen — `send_arrow_right` / `send_arrow_left`, which
     never got the actor their up/down siblings received in #1658.

THE STATIC SWEEP BELOW IS THE POINT OF THIS FILE. Labelling the eight sites fixes today;
the sweep is what stops the ninth. It gives the "impossible to forget" property the ticket
wanted from making `actor` required at `_write`, without that change's churn — and without
breaking the deliberate `unknown` fallback, which must stay reachable so an unlabelled
caller is recorded honestly rather than guessed at.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src" / "swarm"

# The keystroke verbs. `send_keys` is deliberately absent: it derives its actor from the
# `automated` flag rather than taking one, which is #1451's design.
_VERBS = (
    "send_enter",
    "send_escape",
    "send_arrow_up",
    "send_arrow_down",
    "send_arrow_left",
    "send_arrow_right",
    "send_shift_tab",
)
_CALL = re.compile(r"\.(" + "|".join(_VERBS) + r")\s*\(([^)]*)\)")


def _unlabelled_calls() -> list[str]:
    """Every keystroke call in src/ that passes no actor, as `file:line  text`."""
    hits: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            for match in _CALL.finditer(line):
                if "actor" not in match.group(2):
                    rel = path.relative_to(_SRC.parent.parent)
                    hits.append(f"{rel}:{i}  {line.strip()[:88]}")
    return hits


def test_no_pty_keystroke_call_omits_its_actor():
    """THE GUARD. A write whose actor is unknown cannot be traced, and a bare Enter is
    exactly the write that answers an open picker.

    This is a SOURCE SWEEP rather than a runtime assertion on purpose: the failure it
    prevents is someone adding a ninth call site, which no runtime test of the existing
    eight would ever catch."""
    unlabelled = _unlabelled_calls()

    assert not unlabelled, (
        "PTY keystroke calls with no `actor=` — each will record as `unknown` and be "
        "untraceable in ~/.swarm/pty-writes.jsonl:\n  " + "\n  ".join(unlabelled)
    )


def test_the_sweep_can_actually_find_an_unlabelled_call(tmp_path, monkeypatch):
    """POSITIVE CONTROL for the guard itself. A sweep with a broken regex would report a
    clean tree forever and read exactly like success — which is the defect class this
    codebase has hit repeatedly today. Point it at a file that definitely offends and
    confirm it complains."""
    offender = tmp_path / "swarm" / "bad.py"
    offender.parent.mkdir(parents=True)
    offender.write_text("async def f(proc):\n    await proc.send_enter()\n")
    monkeypatch.setattr("tests.test_pty_write_actor_sweep_1675._SRC", offender.parent)

    found = _unlabelled_calls()

    assert any("bad.py" in f for f in found), f"the sweep missed an obvious offender: {found}"


def test_the_sweep_accepts_a_labelled_call(tmp_path, monkeypatch):
    """POSITIVE CONTROL, other direction: a guard that flagged everything would be as
    useless as one that flagged nothing, and would be switched off just as fast."""
    good = tmp_path / "swarm" / "good.py"
    good.parent.mkdir(parents=True)
    good.write_text('async def f(proc):\n    await proc.send_enter(actor="dispatch-submit")\n')
    monkeypatch.setattr("tests.test_pty_write_actor_sweep_1675._SRC", good.parent)

    assert _unlabelled_calls() == []


# ---------------------------------------------------------------------------
# Per-path assertions through the REAL public verbs
# ---------------------------------------------------------------------------


def _proc_capturing():
    from swarm.pty.process import WorkerProcess

    proc = WorkerProcess(name="platform-data", cwd="/tmp")
    sent: list[dict] = []

    async def _send(cmd: dict) -> dict:
        sent.append(cmd)
        return {"ok": True}

    proc.bind_send_cmd(_send)
    return proc, sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actor",
    [
        "dispatch-submit",
        "goal-arm-submit",
        "goal-clear-submit",
        "analyzer-submit",
        "proposal-submit",
    ],
)
async def test_a_submit_nudge_reaches_the_holder_with_its_actor(actor):
    """The shape that produced every Shape-A row. Asserted through `send_enter`, the real
    verb, rather than through `_write`: the actor is chosen by the CALLER, so a test of
    `_write` would pass while every caller above it still sent nothing."""
    proc, sent = _proc_capturing()

    await proc.send_enter(actor=actor)

    writes = [c for c in sent if c.get("cmd") == "write"]
    assert [c["actor"] for c in writes] == [actor]


@pytest.mark.asyncio
async def test_the_left_and_right_arrows_are_attributed_like_their_siblings():
    """Shape B. Up/down got actors in #1658 and left/right did not, which is why queen
    showed labelled and unlabelled 3-byte arrows interleaved in the same second."""
    proc, sent = _proc_capturing()

    await proc.send_arrow_left(actor="operator-arrow")
    await proc.send_arrow_right(actor="operator-arrow")

    writes = [c for c in sent if c.get("cmd") == "write"]
    assert [c["actor"] for c in writes] == ["operator-arrow", "operator-arrow"]


@pytest.mark.asyncio
async def test_an_unlabelled_call_still_records_unknown():
    """AC3 — `unknown` MUST STAY REACHABLE. The ticket is explicit: do not remove the
    default and do not substitute a plausible-looking name. A confident wrong name in a
    forensic record is worse than an honest gap, because it cannot be questioned."""
    proc, sent = _proc_capturing()

    await proc.send_enter()

    writes = [c for c in sent if c.get("cmd") == "write"]
    assert [c["actor"] for c in writes] == ["unknown"]


def test_the_socket_boundary_still_defaults_to_unknown():
    """The other half of AC3. An older daemon talking to a newer holder sends no `actor`
    field at all; that must record "could not tell", never a guess."""
    import base64
    from unittest.mock import MagicMock

    from swarm.pty.command_handler import PtyCommandHandler

    handler = PtyCommandHandler.__new__(PtyCommandHandler)
    handler.holder = MagicMock()
    handler.holder.write_to_worker.return_value = True

    handler._cmd_write({"name": "x", "data": base64.b64encode(b"\r").decode()})

    assert handler.holder.write_to_worker.call_args.args[2] == "unknown"
