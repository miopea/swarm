"""queen_view_worker_state must put the PTY tail in the STRUCTURED payload (2026-08-10).

REPORTED by another operator's Queen, and verified before fixing: the handler reads the
screen contents into `pty_tail`, includes it in the human-readable text block, and then
builds `structuredContent` without it. A client that consumes the structured payload —
which is what an MCP client does — got the worker's state and no output at all.

WHAT MADE IT WORSE THAN AN ABSENT KEY: the structured payload DID carry
`pty_tail_lines: 40`. A field naming a line count reads as a promise that the lines are
present, so the failure looked like an empty terminal rather than a missing field.

SCOPE, verified rather than assumed: the dashboard is unaffected. It reads PTY output
over the /ws/terminal WebSocket, an entirely separate path. This only ever hit MCP
clients.

The other five handlers returning structuredContent (_messages, _logs, _peers, _tasks,
_task_format) were checked for the same computed-then-dropped shape and all carry their
payloads correctly.
"""

from __future__ import annotations

import re
from pathlib import Path

_VIEWS = Path("src/swarm/mcp/queen_handlers/_views.py").read_text(encoding="utf-8")


def _worker_state_handler() -> str:
    i = _VIEWS.index("def _handle_view_worker_state")
    nxt = _VIEWS.find("\ndef ", i + 1)
    return _VIEWS[i : nxt if nxt != -1 else len(_VIEWS)]


def test_the_tail_reaches_the_structured_payload():
    """THE FIX. Without this the Queen sees a worker's state and none of its output."""
    body = _worker_state_handler()
    i = body.index('"structuredContent"')
    assert '"pty_tail": pty_tail' in body[i:], (
        "pty_tail is computed and then dropped from structuredContent again — the Queen "
        "gets worker state with no terminal output"
    )


def test_it_is_the_same_value_shown_in_the_text_block():
    """Two sources for one fact drift. The text block and the structured payload must
    both render the SAME variable, not re-read the PTY."""
    body = _worker_state_handler()
    assert body.count("worker.process.get_content(") == 1, (
        "the tail is read more than once; the text and structured views can now disagree"
    )


def test_the_line_count_field_still_describes_something_real():
    """`pty_tail_lines` without `pty_tail` is worse than no field at all — it promises
    content that is not there. Keeping it is fine ONLY alongside the content."""
    body = _worker_state_handler()
    if '"pty_tail_lines"' in body:
        assert '"pty_tail"' in body, (
            "pty_tail_lines advertises a line count while the lines themselves are absent"
        )


def test_the_handler_still_returns_a_text_block():
    """The fix must not trade one consumer for another — clients reading the text block
    (and humans reading a transcript) still need it."""
    body = _worker_state_handler()
    assert '"content": [{"type": "text"' in body
    assert "--- pty tail" in body


def test_no_other_queen_view_drops_its_payload():
    """A POSITIVE CONTROL on the audit of the sibling handlers: each structured payload
    must carry a collection or object, not merely counts and echoed filters. Catches the
    same class of bug appearing elsewhere later."""
    for name, path in (
        ("_messages", "src/swarm/mcp/queen_handlers/_messages.py"),
        ("_logs", "src/swarm/mcp/queen_handlers/_logs.py"),
    ):
        src = Path(path).read_text(encoding="utf-8")
        for m in re.finditer(r'"structuredContent":\s*\{', src):
            block = src[m.start() : m.start() + 1500]
            assert re.search(r'"(messages|entries|actions|peers|tasks)":', block), (
                f"{name} has a structuredContent block carrying no payload collection — "
                "counts and filters only, which is the pty_tail bug in a new place"
            )
