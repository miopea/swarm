"""A reconnect must re-fetch the panels, and a dropped frame must not be silent.

Both halves are the same failure shape, the one this project keeps re-finding: the
state change is real and durable and NOTHING CAN SEE IT. The dashboard shows a green
connection dot over a stale board, or the daemon drops a frame that connected clients
were owed, and in neither case is there a log line, a toast, or a test failure.

THE CLIENT HALF. ``ws.onopen`` re-fetches every panel when the socket comes back,
because events that arrived while it was down are gone for good. That resync was gated
on ``reconnectDelay > 1000`` — a proxy for "we retried at least once", which holds only
when the reconnect went through ``onclose``'s backoff doubling. Two paths do not:

  * ``forceReconnectMainWs()`` sets ``reconnectDelay = 1000`` itself before connecting.
    Masked in practice, because its only caller (``onAppFocus``) re-fetches separately.
  * the restart watchdog: ``_restarting`` makes ``onclose`` return BEFORE the doubling,
    then the watchdog calls ``ensureMainWsConnected()``.

Both arrive in ``onopen`` with the delay still 1000, so ``1000 > 1000`` was false and no
panel was re-fetched. Gating on "have we connected before" asks the actual question.

THE SERVER HALF. ``broadcast()`` and ``_send_ws_now`` both bail with a bare ``return``
when there is no running event loop. That is correct in CLI and test contexts — nobody
is listening — but when ``ws_clients`` is non-empty a real frame is being thrown away,
and it produced no evidence whatsoever. Operators run at default WARNING, so that is the
level that reaches them.

NOT CLAIMED: neither of these is confirmed to be the cause of the operator's
2026-08-06 report (a task completed over MCP stayed visible until he clicked a filter
chip). That instance had a healthy socket as far as anyone knows, and the report is
still open. These are two defects found while tracing it, each provable by reading, and
each in the reported bug's class.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from swarm.server.broadcast import BroadcastHub

_JS = (
    Path(__file__).parent.parent / "src" / "swarm" / "web" / "static" / "dashboard.js"
).read_text()


def _onopen_body() -> str:
    """The text of ``ws.onopen``, up to its sibling ``ws.onclose``.

    Bounded by the next handler rather than a fixed character count: a fixed window
    runs past the end and makes the assertions depend on whatever happens to sit
    below (the classifier bug in tests/test_select_worker_ordering.py).
    """
    start = _JS.index("ws.onopen = function()")
    end = _JS.index("ws.onclose = function()", start)
    return _JS[start:end]


def _strip_comments(src: str) -> str:
    """Blank comment-only lines.

    Scans in this repo have repeatedly matched the PROSE EXPLAINING A BUG instead of
    the bug — and the comment added with this fix names ``reconnectDelay > 1000`` while
    describing what was removed, so an un-stripped scan would read the fix as the
    defect. Line-based on purpose: a regex over the whole file pairs any ``/*`` inside a
    string literal with the next ``*/`` and deletes real code.
    """
    return "\n".join(
        "" if line.lstrip().startswith(("//", "/*", "*/", "* ")) else line
        for line in src.split("\n")
    )


# --- client: the reconnect resync -----------------------------------------------


def test_the_scan_finds_the_onopen_handler():
    """Positive control. Every assertion below reads this one region; if the slice were
    empty or ran to the wrong place they would all pass over nothing."""
    body = _strip_comments(_onopen_body())
    assert "ws-dot" in body, "onopen slice does not look like the real handler"
    assert "refreshTasks()" in body, "onopen no longer re-fetches the task panel at all"
    assert len(body) < 4000, f"onopen slice ran long ({len(body)} chars); bound is wrong"


def test_the_resync_is_not_gated_on_the_backoff_delay():
    """The bug itself. ``reconnectDelay`` is a backoff value, not a record of whether we
    have connected before, and the two paths that reconnect without doubling it were
    silently excluded from the resync."""
    body = _strip_comments(_onopen_body())
    guards = [ln.strip() for ln in body.split("\n") if "reconnectDelay" in ln and "if" in ln]
    assert not guards, (
        f"onopen still gates behavior on reconnectDelay: {guards}. forceReconnectMainWs "
        f"and the restart watchdog both arrive here with it at 1000, so anything behind "
        f"that gate does not run on the paths that need it most."
    )


def test_the_resync_refetches_every_panel_on_reconnect():
    """A reconnect must re-fetch all four panels: any event type that arrived while the
    socket was down is unrecoverable, so a partial resync leaves a pane stale with no
    indication."""
    body = _strip_comments(_onopen_body())
    guard = re.search(r"if\s*\(\s*wasDisconnected\s*&&\s*(\w+)\s*\)", body)
    assert guard, "the resync guard is no longer `wasDisconnected && <flag>`"
    assert guard.group(1) == "hasConnectedBefore", (
        f"resync gated on {guard.group(1)!r}; expected hasConnectedBefore"
    )
    for fn in ("refreshWorkers()", "refreshStatus()", "refreshTasks()", "refreshBuzzLog()"):
        assert fn in body, f"reconnect resync no longer calls {fn}"


def test_the_flag_is_set_after_the_resync_so_first_load_does_not_refetch():
    """Ordering is load-bearing. Set before the guard, the flag is true on the very first
    connect and the page re-fetches four panels the server just rendered."""
    body = _strip_comments(_onopen_body())
    guard_at = body.index("wasDisconnected && hasConnectedBefore")
    set_at = body.index("hasConnectedBefore = true")
    assert set_at > guard_at, (
        "hasConnectedBefore is set BEFORE the resync guard reads it, so the first "
        "connect on a fresh page would re-fetch every panel unnecessarily"
    )


def test_force_reconnect_no_longer_defeats_the_resync():
    """``forceReconnectMainWs`` may keep resetting the backoff — that is its job — but
    resetting it must no longer decide whether panels are re-fetched."""
    start = _JS.index("function forceReconnectMainWs()")
    body = _strip_comments(_JS[start : _JS.index("\n    }", start)])
    assert "reconnectDelay = 1000" in body, (
        "forceReconnectMainWs no longer resets the backoff; if deliberate, update this test"
    )
    assert "hasConnectedBefore" not in body, (
        "forceReconnectMainWs now touches hasConnectedBefore, which would re-create the "
        "bug by another name — a forced reconnect IS a reconnect and must resync"
    )


# --- server: a dropped frame must be loud ---------------------------------------


class _FakeWs:
    closed = False


def test_debounced_frame_dropped_without_a_loop_warns_when_clients_are_waiting(caplog):
    """``tasks_changed`` is debounced, so it takes the ``call_later`` path — the one that
    needs a running loop. Off-loop with a client attached, the frame is unrecoverable."""
    hub = BroadcastHub(track_task=lambda t: None)
    hub.ws_clients.add(_FakeWs())  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING):
        hub.broadcast({"type": "tasks_changed"})
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("tasks_changed" in r.getMessage() for r in warnings), (
        f"a dropped tasks_changed frame logged nothing naming it at WARNING: "
        f"{[r.getMessage() for r in warnings]}"
    )


def test_undebounced_frame_dropped_without_a_loop_also_warns(caplog):
    """The non-debounced path has its own bail-out; both must be loud."""
    hub = BroadcastHub(track_task=lambda t: None)
    hub.ws_clients.add(_FakeWs())  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING):
        hub.broadcast({"type": "some_unusual_event"})
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "a dropped non-debounced frame logged nothing at WARNING"
    )


def test_no_clients_stays_silent(caplog):
    """The negative half, and the reason this is not just `log everything`. CLI and test
    contexts broadcast with nobody attached constantly; warning there would bury the real
    signal in noise and train everyone to ignore it."""
    hub = BroadcastHub(track_task=lambda t: None)
    with caplog.at_level(logging.WARNING):
        hub.broadcast({"type": "tasks_changed"})
        hub.broadcast({"type": "some_unusual_event"})
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        f"warned with no clients attached: {[r.message for r in caplog.records]}"
    )


def test_on_a_running_loop_nothing_warns_and_the_frame_is_scheduled(caplog):
    """Positive control for the whole server half: with a loop present the frame takes
    the normal path, so the warnings above are about the drop and not about broadcasting
    generally."""

    async def _go() -> None:
        hub = BroadcastHub(track_task=lambda t: None)
        hub.ws_clients.add(_FakeWs())  # type: ignore[arg-type]
        with caplog.at_level(logging.WARNING):
            hub.broadcast({"type": "tasks_changed"})
        assert "tasks_changed" in hub._broadcast_pending, "frame was not scheduled"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
            "warned about a frame that was scheduled normally"
        )
        for h in hub._broadcast_pending.values():
            h.cancel()

    asyncio.run(_go())
