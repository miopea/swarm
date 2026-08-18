"""#1858 — an instruction reached the input line and was never submitted.

THREE WORKERS IN ONE NIGHT, each idle with a fully-formed instruction one keystroke from
executing: platform-data 8.6h ("add the same hook to nexus's package.json"),
public-website 2h ("#1814 plan approved — ship it"), budgetbug ~48min ("set the azure
settings and merge it", with an open picker behind it). The only way any of them was
found was the Queen reading the raw PTY tail by hand, three times.

THE INFORMATION WAS NEVER MISSING, AND THAT IS THE FINDING. `has_idle_prompt` matches
``^ *[>❯]`` — true whether the line reads ``❯`` or ``❯ ship it and merge``. So a worker
holding an unsent instruction is classified IDLE by the same regex that classifies an
empty prompt as idle. `has_empty_prompt`, which CAN tell them apart, already existed in
the same file and was never consulted on that path. Nothing was unobservable; two regexes
in one module were never introduced to each other.

WHY IT CAN HAPPEN AT ALL, cited: `PtyProcess.send_keys` (pty/process.py:445) writes the
payload and the newline as TWO separate `_write` calls with `await asyncio.sleep(0.05)`
between them, and `_write` RAISES on failure. A failure on the second call leaves the
text in the buffer with no terminator. Separately, `pty/bridge.py:57,61` and two drone
approval paths call `send_keys(..., enter=False)` by design — writes that never submit.

NO AUTO-SUBMIT. Two of the three held production deploy approvals ("ship it", "merge
it"). Firing a buffered instruction automatically would execute a deploy nobody
confirmed, and "typed it" cannot be distinguished from "typed it and thought better of
it". Detection only.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from swarm.providers.claude import ClaudeProvider
from tests.test_holder import _send_cmd, holder, socket_path  # noqa: F401

# ---------------------------------------------------------------------------
# THE POSITIVE CONTROL, FIRST — required before any path may be called clean
# ---------------------------------------------------------------------------


async def _spawn_shell(sock: str, name: str) -> None:
    resp = await _send_cmd(
        sock,
        {
            "cmd": "spawn",
            "name": name,
            "cwd": "/tmp",
            "command": ["/bin/sh", "-c", "PS1='❯ '; export PS1; exec /bin/sh -i"],
            "cols": 80,
            "rows": 24,
        },
    )
    assert resp["ok"] is True, f"could not spawn a real PTY: {resp}"
    await asyncio.sleep(0.6)


async def _snapshot(sock: str, name: str) -> str:
    """Read the buffer back as TEXT.

    The holder returns base64. My first version of this helper forgot to decode it, and
    the negative test PASSED anyway — base64 never matches the prompt regex, so "nothing
    stranded" was reported by an instrument that could not have seen anything. Caught by
    the positive control failing, which is exactly what it is for.
    """
    resp = await _send_cmd(sock, {"cmd": "snapshot", "name": name, "lines": 35})
    assert resp.get("ok") is not False, resp
    raw = resp.get("content") or resp.get("data") or ""
    if not raw:
        return ""
    try:
        return base64.b64decode(raw).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return raw


@pytest.mark.asyncio
async def test_POSITIVE_CONTROL_the_detector_sees_deliberately_planted_unsent_text(
    holder,  # noqa: F811
    socket_path,  # noqa: F811
):
    """PLANT IT, THEN LOOK. The ticket's standing requirement, and this file's reason to
    exist: a survey that finds nothing with an instrument that could not have found
    anything is the shape this fleet has caught repeatedly.

    Text is written to a REAL pty with NO newline — exactly what `send_keys` leaves
    behind when its second `_write` fails — and read back through the real buffer.
    """
    provider = ClaudeProvider()
    await _spawn_shell(socket_path, "control")

    clean = await _snapshot(socket_path, "control")
    assert provider.unsent_input(clean) == "", (
        f"the buffer was dirty before planting, so this control proves nothing: {clean[-120:]!r}"
    )

    await _send_cmd(
        socket_path,
        {"cmd": "write", "name": "control", "data": _b64("set the azure settings and merge it")},
    )
    await asyncio.sleep(0.5)

    planted = await _snapshot(socket_path, "control")
    found = provider.unsent_input(planted)

    assert found == "set the azure settings and merge it", (
        f"the detector CANNOT see unsent buffered text on a real PTY — nothing else in "
        f"this file means anything. Buffer tail was: {planted[-200:]!r}"
    )


@pytest.mark.asyncio
async def test_the_same_text_submitted_leaves_nothing_behind(holder, socket_path):  # noqa: F811
    """THE OTHER HALF OF THE CONTROL. A detector that returned the text unconditionally
    would pass the test above. Send the identical payload WITH the newline and the input
    line must come back empty — that is the difference the whole ticket turns on."""
    provider = ClaudeProvider()
    await _spawn_shell(socket_path, "submitted")

    await _send_cmd(
        socket_path, {"cmd": "write", "name": "submitted", "data": _b64("echo hello\r")}
    )
    await asyncio.sleep(0.6)

    after = await _snapshot(socket_path, "submitted")

    assert provider.unsent_input(after) == "", (
        f"a SUBMITTED command was reported as stranded input — this detector would fire "
        f"on every worker in the fleet. Tail: {after[-200:]!r}"
    )


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


# ---------------------------------------------------------------------------
# AC4 — the detection surfaces; AC5 — it never submits
# ---------------------------------------------------------------------------


def _worker(name="platform-data"):
    from unittest.mock import MagicMock

    w = MagicMock()
    w.name = name
    return w


def _watcher(check, log):
    from swarm.drones.idle_watcher import IdleWatcher

    return IdleWatcher(
        drone_config=_cfg(),
        task_board=None,
        drone_log=log,
        send_to_worker=_never_send,
        unsent_input_check=check,
    )


def _cfg():
    from unittest.mock import MagicMock

    c = MagicMock()
    c.idle_nudge_interval_seconds = 0
    c.idle_nudge_debounce_seconds = 0
    c.idle_nudge_max_repeats = 0
    return c


async def _never_send(*_a, **_k):
    raise AssertionError("the stranded-input check must NEVER write to a worker")


def test_a_stranded_worker_reaches_the_buzz_log():
    """AC4. platform-data's actual held text, 8.6 hours of it."""
    from unittest.mock import MagicMock

    from swarm.drones.log import DroneAction

    log = MagicMock()
    w = _watcher(lambda _w: "add the same hook to nexus's package.json", log)

    w._check_unsent_input(_worker())

    action, name, detail = log.add.call_args.args
    assert action is DroneAction.UNSENT_INPUT_DETECTED
    assert name == "platform-data"
    assert "nexus's package.json" in detail


def test_a_clean_worker_produces_nothing():
    """POSITIVE CONTROL for the test above. A check that logged unconditionally would
    pass it and put a line in the buzz log for every idle worker in the fleet."""
    from unittest.mock import MagicMock

    log = MagicMock()

    _watcher(lambda _w: "", log)._check_unsent_input(_worker())

    log.add.assert_not_called()


def test_the_same_stranded_text_is_reported_once_not_every_sweep():
    from unittest.mock import MagicMock

    log = MagicMock()
    w = _watcher(lambda _w: "ship it", log)

    for _ in range(5):
        w._check_unsent_input(_worker())

    assert log.add.call_count == 1


def test_a_SECOND_different_stranding_is_a_new_finding():
    """Debounced on the TEXT, not the worker: a worker that strands a different
    instruction later is a new incident, not the same one still open."""
    from unittest.mock import MagicMock

    log = MagicMock()
    texts = iter(["ship it", "ship it", "merge it"])
    w = _watcher(lambda _w: next(texts), log)

    for _ in range(3):
        w._check_unsent_input(_worker())

    assert log.add.call_count == 2


def test_clearing_then_stranding_the_same_text_reports_again():
    from unittest.mock import MagicMock

    log = MagicMock()
    texts = iter(["ship it", "", "ship it"])
    w = _watcher(lambda _w: next(texts), log)

    for _ in range(3):
        w._check_unsent_input(_worker())

    assert log.add.call_count == 2


def test_NO_AUTO_SUBMIT_the_check_never_writes_to_the_worker():
    """AC5, THE ONE THAT MATTERS MOST. Two of three observed instances were production
    deploy approvals — "ship it" and "merge it, deploys straight to production". The
    send callback raises if touched, so any future edit that tries to submit buffered
    text fails here rather than in production."""
    from unittest.mock import MagicMock

    _watcher(
        lambda _w: "merge it, deploys straight to production", MagicMock()
    )._check_unsent_input(_worker("budgetbug"))


def test_no_submit_call_exists_anywhere_in_the_detection_path():
    """The behavioural test above only covers the path it walks. This one catches the
    future edit — a `send_keys`/`send_enter` added to the check — which no test of
    current behaviour would ever see."""
    import inspect

    from swarm.drones.idle_watcher import IdleWatcher

    body = inspect.getsource(IdleWatcher._check_unsent_input)
    code = body.split('"""')[2] if body.count('"""') >= 2 else body

    for forbidden in ("send_keys", "send_enter", "send_to_worker", "\\r"):
        assert forbidden not in code, (
            f"the stranded-input check can now submit buffered text ({forbidden!r}) — "
            f"that is a production deploy nobody confirmed"
        )


def test_a_raising_check_never_breaks_the_sweep():
    from unittest.mock import MagicMock

    def _boom(_w):
        raise RuntimeError("pty gone")

    _watcher(_boom, MagicMock())._check_unsent_input(_worker())  # must not raise


def test_no_checker_reports_nothing_rather_than_all_clear():
    """A deployment without the check wired must contribute no findings — not a clean
    bill of health it never measured."""
    from unittest.mock import MagicMock

    log = MagicMock()
    _watcher(None, log)._check_unsent_input(_worker())

    log.add.assert_not_called()
