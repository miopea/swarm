"""#1910b — a stranding is an ONGOING CONDITION, not a point event.

THE DEFECT, AND IT WAS MINE. #1858's detector debounced on (worker, TEXT) and suppressed
forever while the text was unchanged. That reasoning was RIGHT ABOUT NOISE — re-reporting
the same line every sweep really would mute a real signal — and WRONG ABOUT STATE: it made
the condition unobservable between edges.

A DEBOUNCE CONVERTS AN ONGOING CONDITION INTO A POINT EVENT, and a reader samples WINDOWS,
not edges. platform-data sat stranded 12.9 hours holding "open a PR"; it was detected at
10:09:44 and 11:10:13 and then silent. Anyone checking the last two hours saw ZERO and
concluded the detector was broken. That happened to the Queen TWICE, and both times she
was reading the log correctly — "already reported, still stranded" and "nothing to report"
produced identical output.

TWO HALVES, per the ruling:
 (a) `stranded_now` — a LIVE read of who is stranded at this moment, deliberately NOT a
     summary of what has been reported. That distinction is the whole ticket.
 (b) an hourly re-emit carrying HOW LONG, so a recent-window check stops lying to every
     future reader who does what the Queen did.
The correct half of the original mechanism is kept: NEW text on the same worker is a new
finding and fires immediately.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from swarm.drones.idle_watcher import _UNSENT_REEMIT_SECONDS, IdleWatcher, _format_duration
from swarm.drones.log import DroneAction


def _cfg():
    c = MagicMock()
    c.idle_nudge_interval_seconds = 0
    c.idle_nudge_debounce_seconds = 0
    c.idle_nudge_max_repeats = 0
    return c


async def _never_send(*_a, **_k):
    raise AssertionError("the stranded-input path must NEVER write to a worker")


def _watcher(check, log):
    return IdleWatcher(
        drone_config=_cfg(),
        task_board=None,
        drone_log=log,
        send_to_worker=_never_send,
        unsent_input_check=check,
    )


def _worker(name="platform-data"):
    return SimpleNamespace(name=name)


def _details(log):
    return [c.args[2] for c in log.add.call_args_list]


# ---------------------------------------------------------------------------
# (b) The heartbeat
# ---------------------------------------------------------------------------


def test_an_unchanged_stranding_re_reports_after_the_interval():
    """THE FIX. Without this a 12.9-hour stranding leaves one row and silence."""
    log = MagicMock()
    w = _watcher(lambda _w: "open a PR", log)

    w._check_unsent_input(_worker())
    assert log.add.call_count == 1

    # Rewind the bookkeeping rather than sleeping an hour.
    text, first, _last = w._last_unsent_seen["platform-data"]
    w._last_unsent_seen["platform-data"] = (
        text,
        first - 46800,
        time.time() - _UNSENT_REEMIT_SECONDS - 1,
    )

    w._check_unsent_input(_worker())

    assert log.add.call_count == 2


def test_the_re_emit_says_HOW_LONG():
    """The Queen's addition, and it is the field that would have told her at a glance that
    the finding was old rather than new."""
    log = MagicMock()
    w = _watcher(lambda _w: "open a PR", log)
    w._check_unsent_input(_worker())
    text, _f, _l = w._last_unsent_seen["platform-data"]
    w._last_unsent_seen["platform-data"] = (text, time.time() - 46440, 0.0)  # 12.9h

    w._check_unsent_input(_worker())

    assert "12.9h" in _details(log)[-1]
    assert "STILL" in _details(log)[-1]


def test_it_stays_QUIET_between_heartbeats():
    """POSITIVE CONTROL the other way. Re-reporting every sweep is what the original
    debounce correctly prevented, and losing that would mute the signal with noise."""
    log = MagicMock()
    w = _watcher(lambda _w: "open a PR", log)

    for _ in range(20):
        w._check_unsent_input(_worker())

    assert log.add.call_count == 1


def test_NEW_text_still_fires_immediately():
    """The correct half of the old mechanism, kept. A different instruction stranded on
    the same worker is a new finding, not a repeat."""
    log = MagicMock()
    texts = iter(["open a PR", "open a PR", "ship it"])
    w = _watcher(lambda _w: next(texts), log)

    for _ in range(3):
        w._check_unsent_input(_worker())

    assert log.add.call_count == 2
    assert "ship it" in _details(log)[-1]
    assert "STILL" not in _details(log)[-1], "new text was reported as a continuation"


def test_clearing_resets_the_duration():
    """A stranding that clears and returns is genuinely new — its clock restarts."""
    log = MagicMock()
    texts = iter(["open a PR", "", "open a PR"])
    w = _watcher(lambda _w: next(texts), log)

    for _ in range(3):
        w._check_unsent_input(_worker())

    assert log.add.call_count == 2
    assert "STILL" not in _details(log)[-1]


@pytest.mark.parametrize(
    "seconds,expected", [(5, "5s"), (600, "10m"), (46440, "12.9h"), (3600 * 24, "24.0h")]
)
def test_duration_formatting(seconds, expected):
    assert _format_duration(seconds) == expected


# ---------------------------------------------------------------------------
# (a) The live read
# ---------------------------------------------------------------------------


def test_stranded_now_reports_who_is_stranded_AT_THIS_MOMENT():
    log = MagicMock()
    holding = {"a": "open a PR", "b": "", "c": "ship it"}
    w = _watcher(lambda wk: holding[wk.name], log)

    rows = w.stranded_now([_worker("a"), _worker("b"), _worker("c")])

    assert {r[0] for r in rows} == {"a", "c"}, "a clear worker was reported as stranded"


def test_it_is_a_LIVE_READ_not_a_replay_of_what_was_reported():
    """THE DISTINCTION THE WHOLE TICKET IS ABOUT. A worker reported an hour ago whose text
    has since CLEARED must not appear; a worker never reported but stranded NOW must."""
    log = MagicMock()
    w = _watcher(lambda _wk: "open a PR", log)
    w._check_unsent_input(_worker("reported-then-cleared"))
    assert "reported-then-cleared" in w._last_unsent_seen

    holding = {"reported-then-cleared": "", "never-reported": "ship it"}
    w._unsent_input_check = lambda wk: holding[wk.name]

    rows = w.stranded_now([_worker("reported-then-cleared"), _worker("never-reported")])

    assert [r[0] for r in rows] == ["never-reported"]


def test_longest_held_comes_first():
    log = MagicMock()
    w = _watcher(lambda _wk: "x", log)
    w._last_unsent_seen["old"] = ("x", time.time() - 40000, 0.0)
    w._last_unsent_seen["recent"] = ("x", time.time() - 60, 0.0)

    rows = w.stranded_now([_worker("recent"), _worker("old")])

    assert [r[0] for r in rows] == ["old", "recent"]


def test_a_first_sighting_reports_zero_not_missing():
    """0.0 means "just found", never "no data" — the same reason [] and
    outside-known-repos are distinct values in #1698."""
    w = _watcher(lambda _wk: "x", MagicMock())

    assert w.stranded_now([_worker("fresh")])[0][2] == 0.0


def test_a_raising_check_skips_that_worker_and_keeps_going():
    def _check(wk):
        if wk.name == "bad":
            raise RuntimeError("pty gone")
        return "x"

    rows = _watcher(_check, MagicMock()).stranded_now([_worker("bad"), _worker("good")])

    assert [r[0] for r in rows] == ["good"]


def test_no_checker_reports_nothing_rather_than_all_clear():
    assert _watcher(None, MagicMock()).stranded_now([_worker("a")]) == []


# ---------------------------------------------------------------------------
# Still no auto-submit, on either path
# ---------------------------------------------------------------------------


def test_neither_path_can_submit():
    """`_never_send` raises if touched. Two of the original three strandings were
    production deploy approvals."""
    log = MagicMock()
    w = _watcher(lambda _wk: "merge it, deploys straight to production", log)

    w._check_unsent_input(_worker())
    w.stranded_now([_worker()])

    assert log.add.call_args.args[0] is DroneAction.UNSENT_INPUT_DETECTED


def test_no_submit_verb_appears_in_either_source():
    import inspect

    for fn in (IdleWatcher._check_unsent_input, IdleWatcher.stranded_now):
        body = inspect.getsource(fn)
        code = body.split('"""')[2] if body.count('"""') >= 2 else body
        for forbidden in ("send_keys", "send_enter", "send_to_worker"):
            assert forbidden not in code, f"{fn.__name__} can submit buffered text"
