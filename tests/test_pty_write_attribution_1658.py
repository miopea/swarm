"""#1658 — every write to a worker PTY is now attributable.

WHY THIS EXISTS. A selection prompt on worker `swarm` was answered six times with no
record of who did it, while an identical picker on sculpt-studio produced an
`OPERATOR — terminal approval` row in the same minute. The audit path existed and did not
cover whichever path answered. Seven investigation attempts could not close it, because
nothing recorded the actor at all — so the question "who wrote to this PTY" had no answer
anywhere in the system.

`holder.write_to_worker` is the single choke point every byte passes through on its way to
a worker's PTY master, which is why the record is taken there rather than at the ~13 call
sites upstream: recording at the choke point makes the audit complete by construction, and
a caller that forgets to label itself shows up AS "unknown" rather than as nobody.

RECORDS SHAPE, NEVER CONTENT — writes carry whatever a human or a worker types, which can
include a credential. Byte count and a coarse kind (enter/escape/arrow/interrupt/text)
answer "who answered the picker, and was it a bare Enter"; "what did they type" is
deliberately unanswerable from this record.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def audit(tmp_path, monkeypatch):
    """Point the audit at a temp file and hand back a reader for it.

    Sets the ENV VAR rather than patching `_WRITE_AUDIT_PATH`, because the session-scoped
    conftest isolation already sets that env var and it takes precedence — patching the
    constant here would be silently overridden and these tests would read an empty file
    while the writes went somewhere else.
    """
    path = tmp_path / "pty-writes.jsonl"
    monkeypatch.setenv("SWARM_PTY_WRITE_AUDIT", str(path))

    def rows() -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    return rows


def _holder_with_worker(alive: bool = True):
    """A holder whose worker has a REAL writable fd (/dev/null).

    Not a fake fd: `write_to_worker` calls fcntl on it, so a sentinel like -1 raises
    before the write completes and the test would be exercising an error path rather
    than the audit.
    """
    import os

    from swarm.pty.holder import PtyHolder

    h = PtyHolder.__new__(PtyHolder)
    worker = MagicMock()
    worker.alive = alive
    worker.master_fd = os.open(os.devnull, os.O_WRONLY)
    h.workers = {"swarm": worker}
    return h


def test_every_write_records_actor_worker_bytes_and_time(audit):
    """THE STANDING RECORD the ticket asks for: actor, target, size, timestamp — enough
    that the NEXT unexplained input is a log lookup rather than an investigation."""
    h = _holder_with_worker()

    h.write_to_worker("swarm", b"hello", "queen-answer")

    rows = audit()
    assert len(rows) == 1
    row = rows[0]
    assert row["worker"] == "swarm"
    assert row["actor"] == "queen-answer"
    assert row["bytes"] == 5
    assert row["ts"] > 0


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (b"\r", "enter"),
        (b"\x1b", "escape"),
        (b"\x1b[A", "arrow-or-ansi"),
        (b"\x03", "interrupt"),
        (b"some text", "text"),
    ],
)
def test_the_kind_distinguishes_a_bare_enter_from_a_message(audit, data, kind):
    """The distinction the whole investigation needed. A picker answered by a stray Enter
    and one answered by a typed message are different events, and the byte count alone
    cannot tell them apart."""
    h = _holder_with_worker()

    h.write_to_worker("swarm", data, "someone")

    assert audit()[0]["kind"] == kind


def test_content_is_never_recorded(audit):
    """Writes carry whatever is typed, including credentials. The audit must answer WHO
    and WHAT SHAPE, never WHAT — a security record that itself leaks secrets is worse than
    none."""
    h = _holder_with_worker()

    h.write_to_worker("swarm", b"export TOKEN=sk-secret-value", "operator")

    blob = json.dumps(audit()[0])
    assert "sk-secret-value" not in blob
    assert "TOKEN" not in blob


def test_an_unlabelled_write_is_recorded_as_unknown_not_dropped(audit):
    """A caller that forgets to label itself must show up AS unknown. Attributing it to
    whoever happens to be nearby would be worse than the gap this fixes, and silently
    dropping the row would recreate it exactly."""
    h = _holder_with_worker()

    h.write_to_worker("swarm", b"x")

    assert audit()[0]["actor"] == "unknown"


def test_a_write_to_a_dead_worker_records_nothing(audit):
    """No PTY, no write, no row — the record must not claim bytes reached a worker that
    was not there to receive them."""
    h = _holder_with_worker(alive=False)

    assert h.write_to_worker("swarm", b"x", "queen") is False
    assert audit() == []


def test_an_audit_failure_never_blocks_the_write(tmp_path, monkeypatch):
    """Telemetry hanging off the write path must never stop a worker receiving input.
    An unwritable audit file is a degraded record, not a wedged fleet."""
    monkeypatch.setenv("SWARM_PTY_WRITE_AUDIT", str(tmp_path / "nope" / "deep" / "x.jsonl"))
    h = _holder_with_worker()

    # Must not raise even though the audit path is unwritable.
    h.write_to_worker("swarm", b"x", "queen")


def test_the_write_command_carries_the_actor_across_the_socket():
    """The actor has to survive the daemon→holder hop, or the choke point records
    'unknown' for everything and the whole record is worthless."""
    from swarm.pty.command_handler import PtyCommandHandler

    handler = PtyCommandHandler.__new__(PtyCommandHandler)
    handler.holder = MagicMock()
    handler.holder.write_to_worker.return_value = True

    import base64

    handler._cmd_write(
        {"name": "swarm", "data": base64.b64encode(b"hi").decode(), "actor": "queen-dismiss"}
    )

    handler.holder.write_to_worker.assert_called_once()
    assert handler.holder.write_to_worker.call_args.args[2] == "queen-dismiss"


def test_a_write_command_with_no_actor_defaults_to_unknown():
    """POSITIVE CONTROL for the socket hop: an older daemon sending no actor field must
    produce 'unknown', not a crash and not a wrong name."""
    from swarm.pty.command_handler import PtyCommandHandler

    handler = PtyCommandHandler.__new__(PtyCommandHandler)
    handler.holder = MagicMock()
    handler.holder.write_to_worker.return_value = True

    import base64

    handler._cmd_write({"name": "swarm", "data": base64.b64encode(b"hi").decode()})

    assert handler.holder.write_to_worker.call_args.args[2] == "unknown"


# ---------------------------------------------------------------------------
# AC3 — each write path carries its own actor, asserted per verb
# ---------------------------------------------------------------------------


def _proc_capturing_cmds():
    """A real WorkerProcess whose holder command sender is captured.

    Exercises the ACTUAL public verbs rather than `_write` directly, because the actor is
    chosen by the verb — testing `_write` alone would pass while every caller above it
    still sent "unknown".
    """
    from swarm.pty.process import WorkerProcess

    proc = WorkerProcess(name="swarm", cwd="/tmp")
    sent: list[dict] = []

    async def _send(cmd: dict) -> dict:
        sent.append(cmd)
        return {"ok": True}

    proc.bind_send_cmd(_send)
    return proc, sent


def _actors(sent: list[dict]) -> list[str]:
    return [c.get("actor") for c in sent if c.get("cmd") == "write"]


@pytest.mark.asyncio
async def test_the_queen_answer_keystrokes_are_attributed(monkeypatch):
    """THE PATH THE POSITIVE CONTROL EXERCISED LIVE. Four `queen_answer_prompt` calls on
    2026-08-15 produced exactly four `queen-answer`/`enter` rows, one per worker, matching
    the buzz log to the second. This pins that mapping."""
    proc, sent = _proc_capturing_cmds()

    await proc.send_arrow_down(actor="queen-answer")
    await proc.send_enter(actor="queen-answer")

    assert _actors(sent) == ["queen-answer", "queen-answer"]


@pytest.mark.asyncio
async def test_the_dismiss_path_is_attributed():
    """`queen_dismiss_prompt` writes Escape. Distinguishing a dismiss from an answer in
    the log is the difference between 'the Queen declined it' and 'the Queen chose an
    option' — #1623 spent a night on exactly that distinction."""
    proc, sent = _proc_capturing_cmds()

    await proc.send_escape(actor="queen-dismiss")

    assert _actors(sent) == ["queen-dismiss"]


@pytest.mark.asyncio
async def test_an_automated_dispatch_and_an_operator_keystroke_are_told_apart():
    """AC2's core, at the verb level. `send_keys` serves BOTH the automated dispatch path
    and the operator's own keystrokes via the web bridge, separated only by the
    `automated` flag — so if that flag did not reach the actor, the single most important
    distinction in the record would collapse."""
    proc, sent = _proc_capturing_cmds()

    await proc.send_keys("a queen message", automated=True)
    await proc.send_keys("typed by a human", automated=False)

    # text + enter for each send, so two rows per call.
    assert _actors(sent) == ["automated", "automated", "operator", "operator"]


@pytest.mark.asyncio
async def test_an_unlabelled_verb_reports_unknown_rather_than_borrowing_a_name():
    """Observed in the wild within minutes of this shipping: one `unknown`/`enter` row on
    platform-data at 20:39:53. That is the record WORKING — an unlabelled path is visible
    as unlabelled instead of being silently attributed to whoever wrote last."""
    proc, sent = _proc_capturing_cmds()

    await proc.send_enter()

    assert _actors(sent) == ["unknown"]
