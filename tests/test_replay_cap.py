"""The terminal replay is bounded, and the heartbeat can see the memory it costs.

WHAT THE CRASH DUMP SAID. Edge exception **0xE0000008** — Chromium's out-of-memory code
— on a **2MB allocation**, while the dashboard heartbeat had reported the JS heap flat at
17MB of a 4192MB limit for five minutes beforehand.

Both facts are true, and the contradiction is the lesson: ``performance.memory
.usedJSHeapSize`` counts ONLY the JS heap. ArrayBuffer backing stores are excluded — and
the terminal replay arrives as BINARY WebSocket frames, which is precisely that. The
instrument was blind to the memory that ran out, and "not memory" was concluded from it
twice.

TWO FIXES, TESTED HERE:
  1. The replay is capped (1MB -> 256KB). Every attach used to ship the whole ring
     buffer, at 18-99 attaches/hour.
  2. The heartbeat reports cumulative WebSocket bytes, so the same blind spot cannot
     hide the next one.
"""

from __future__ import annotations

from pathlib import Path

from swarm.pty.bridge import _MAX_REPLAY_BYTES, _trim_replay


def test_a_large_snapshot_is_capped():
    """THE FIX. 1MB per attach was the suspected allocation."""
    out = _trim_replay(b"x" * (1024 * 1024))
    assert len(out) <= _MAX_REPLAY_BYTES, f"{len(out)} bytes still sent"


def test_a_small_snapshot_is_untouched():
    """Most attaches are well under the cap; they must not be altered at all."""
    small = b"hello\nworld\n"
    assert _trim_replay(small) == small


def test_the_kept_slice_is_the_MOST_RECENT_output():
    """Keeping the head would restore ancient scrollback and drop what the operator is
    actually looking at — the opposite of useful."""
    body = b"".join(b"line-%d\n" % i for i in range(200000))
    out = _trim_replay(body)
    assert out.endswith(b"line-199999\n"), "the tail of the buffer was not preserved"
    assert b"line-0\n" not in out, "old scrollback survived instead of recent output"


def test_the_cut_lands_on_a_line_boundary():
    """Cutting mid-line hands xterm a partial ANSI escape, which it renders as garbage or
    swallows along with the text after it. A shorter first screen beats a corrupted one.
    """
    body = b"".join(b"\x1b[32mline-%d\x1b[0m\n" % i for i in range(100000))
    out = _trim_replay(body)
    assert out.startswith(b"\x1b["), f"replay starts mid-sequence: {out[:20]!r}"


def test_one_enormous_line_is_still_bounded():
    """No newline anywhere is the degenerate case. Trimming to nothing would be worse
    than sending a bounded tail, so it must stay bounded rather than empty."""
    out = _trim_replay(b"z" * (2 * 1024 * 1024))
    assert 0 < len(out) <= _MAX_REPLAY_BYTES


def test_the_cap_is_a_real_reduction():
    """A POSITIVE CONTROL on the premise. If the ring buffer capacity ever drops below
    the cap, every test above passes while the cap does nothing at all."""
    src = Path("src/swarm/pty/buffer.py").read_text(encoding="utf-8")
    assert "capacity: int = 1048576" in src, (
        "the ring buffer size changed — re-check whether a 256KB cap is still a cap"
    )
    assert _MAX_REPLAY_BYTES < 1048576


def test_the_heartbeat_reports_websocket_bytes():
    """THE BLIND SPOT. Without this the next buffer-memory problem looks exactly like
    the last one: a flat JS heap and a dead tab."""
    js = Path("src/swarm/web/static/dashboard.js").read_text(encoding="utf-8")
    assert "__swarmWsBytes" in js, "nothing counts the bytes arriving over the sockets"
    assert "wsMB:" in js, "the byte count is not reported in the heartbeat"
    server = Path("src/swarm/server/routes/system.py").read_text(encoding="utf-8")
    assert "wsMB=" in server, (
        "the server drops the field — a payload nobody logs is a payload nobody has, "
        "which already happened once with plat/webgl"
    )


def test_the_trim_is_actually_wired_into_the_attach_path():
    """CAUGHT BY THE NEGATIVE CONTROL, not by writing it first.

    Every test above exercises ``_trim_replay`` in isolation, so deleting the CALL SITE
    left the whole suite green while the cap did nothing whatsoever. A correct helper
    nobody calls is the same shape as the plat/webgl fields nobody logged, and as the
    WebGL guard that never fired — three instances in one investigation of something
    computed correctly and then not connected.

    Pins the ordering too: trimming after ``send_bytes`` would be a very tidy no-op.
    """
    src = Path("src/swarm/pty/bridge.py").read_text(encoding="utf-8")
    assert "snapshot = _trim_replay(snapshot)" in src, (
        "the replay is never trimmed on the attach path — the cap is dead code"
    )
    assert src.index("_trim_replay(snapshot)") < src.index("await ws.send_bytes(snapshot)"), (
        "the snapshot is sent before it is trimmed"
    )
