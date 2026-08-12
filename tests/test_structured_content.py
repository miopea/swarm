"""Phase 3 of the Apr–May 2026 Anthropic-features bundle.

Adds MCP ``structuredContent`` sidecars to the read-side view tools so
Claude Code 2.1.x clients (which already prefer ``structuredContent`` in
``transformMCPResult`` — verified in the leaked source at
``services/mcp/client.ts:2662``) can reason against typed JSON instead
of re-parsing the markdown summary.

This is the conservative, do-it-properly take on the original "MCP Apps
spike" plan: instead of betting on speculative SEP-1865 UI widgets that
no shipped Claude Code version handles, deliver structured JSON
sidecars *alongside* the existing text content. The text stays for
operator-facing rendering; the JSON gives the model a queryable shape.

Behavioural contract:

* Handlers may now optionally return a ``dict`` with ``content`` and
  ``structuredContent`` (and optional ``_meta``) keys instead of the
  bare ``list[dict]`` content array.
* ``handle_tool_call`` normalizes either shape so callers don't have
  to branch.
* The ``tools/call`` JSON-RPC response carries ``structuredContent``
  when present, omits it otherwise — so older Claude Code that
  doesn't read the field still gets the standard content array.
* Every view tool (Queen + worker side) returns a structured shape
  whose top level is a JSON object — never a bare array — so the
  protocol's "structuredContent SHOULD be a JSON object" hint holds.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Tool-call dispatch carries structuredContent through
# ---------------------------------------------------------------------------


def _fake_daemon() -> MagicMock:
    d = MagicMock()
    d.drone_log = MagicMock()
    return d


def test_handle_tool_call_passes_structured_content_through() -> None:
    """Handlers returning dict shape preserve both content and structuredContent."""
    from swarm.mcp.tools import _HANDLERS, handle_tool_call

    def fake_handler(daemon: Any, worker: str, args: dict) -> dict:
        return {
            "content": [{"type": "text", "text": "hello"}],
            "structuredContent": {"hello": "world", "n": 1},
        }

    _HANDLERS["__test_structured__"] = fake_handler
    try:
        result = handle_tool_call(_fake_daemon(), "alpha", "__test_structured__", {})
    finally:
        del _HANDLERS["__test_structured__"]

    # New shape: dict wrapper with both keys
    assert isinstance(result, dict)
    assert result["content"] == [{"type": "text", "text": "hello"}]
    assert result["structuredContent"] == {"hello": "world", "n": 1}


def test_handle_tool_call_legacy_list_shape_still_works() -> None:
    """Backwards compat: handlers returning bare list[dict] still produce a usable result."""
    from swarm.mcp.tools import _HANDLERS, handle_tool_call

    def legacy_handler(daemon: Any, worker: str, args: dict) -> list[dict]:
        return [{"type": "text", "text": "plain"}]

    _HANDLERS["__test_legacy__"] = legacy_handler
    try:
        result = handle_tool_call(_fake_daemon(), "alpha", "__test_legacy__", {})
    finally:
        del _HANDLERS["__test_legacy__"]

    # Legacy shape: bare list[dict] — same as before
    assert isinstance(result, list)
    assert result == [{"type": "text", "text": "plain"}]


def test_tools_call_response_includes_structured_content() -> None:
    """The JSON-RPC ``tools/call`` envelope surfaces structuredContent when present."""
    from swarm.mcp.tools import _HANDLERS

    def fake(daemon: Any, worker: str, args: dict) -> dict:
        return {
            "content": [{"type": "text", "text": "ok"}],
            "structuredContent": {"ok": True},
        }

    _HANDLERS["__test_envelope__"] = fake
    try:
        from swarm.mcp.server import _handle_tools_call

        envelope = _handle_tools_call(
            _fake_daemon(), "alpha", {"name": "__test_envelope__", "arguments": {}}
        )
    finally:
        del _HANDLERS["__test_envelope__"]

    assert envelope["content"] == [{"type": "text", "text": "ok"}]
    assert envelope["structuredContent"] == {"ok": True}


def test_tools_call_response_omits_structured_content_for_legacy() -> None:
    """Legacy handlers don't sprout an empty/null structuredContent key."""
    from swarm.mcp.tools import _HANDLERS

    def legacy(daemon: Any, worker: str, args: dict) -> list[dict]:
        return [{"type": "text", "text": "ok"}]

    _HANDLERS["__test_legacy_envelope__"] = legacy
    try:
        from swarm.mcp.server import _handle_tools_call

        envelope = _handle_tools_call(
            _fake_daemon(),
            "alpha",
            {"name": "__test_legacy_envelope__", "arguments": {}},
        )
    finally:
        del _HANDLERS["__test_legacy_envelope__"]

    # Standard MCP shape — content only, no extra keys
    assert envelope["content"] == [{"type": "text", "text": "ok"}]
    assert "structuredContent" not in envelope


# ---------------------------------------------------------------------------
# Queen view tools return structured sidecars
# ---------------------------------------------------------------------------


def _make_queen_daemon(
    *,
    workers: list[Any] | None = None,
    tasks: list[Any] | None = None,
) -> MagicMock:
    """Build a daemon stub the queen view handlers can read from.

    The Queen-side handlers all start with ``_assert_queen(worker_name)``
    which checks the caller is the Queen — tests pass ``"queen"`` as the
    worker name to bypass that gate.
    """
    daemon = MagicMock()
    daemon.workers = workers or []
    if tasks is not None:
        board = MagicMock()
        board.all_tasks = tasks
        board.assigned_or_active_tasks_for_worker = MagicMock(side_effect=lambda name: [])
        daemon.task_board = board
    else:
        daemon.task_board = None
    daemon.drone_log = MagicMock()
    daemon.drone_log.entries = []
    return daemon


def _fake_worker(
    name: str,
    *,
    state: str = "RESTING",
    is_queen: bool = False,
    duration: float = 5.0,
) -> MagicMock:
    w = MagicMock()
    w.name = name
    w.is_queen = is_queen
    w.kind = "queen" if is_queen else "claude"
    w.state_duration = duration
    w.context_pct = 0.42
    w.process = None
    state_obj = MagicMock()
    state_obj.value = state.lower()
    w.display_state = state_obj
    usage = MagicMock()
    usage.cost_usd = 0.0
    usage.to_dict = MagicMock(return_value={"input_tokens": 100, "output_tokens": 50})
    w.usage = usage
    return w


def test_view_worker_state_summary_returns_structured() -> None:
    """No-target summary returns structured worker list alongside text."""
    from swarm.mcp.queen_tools import _handle_view_worker_state

    daemon = _make_queen_daemon(
        workers=[
            _fake_worker("alpha", state="RESTING"),
            _fake_worker("beta", state="BUZZING"),
        ],
        tasks=[],
    )

    result = _handle_view_worker_state(daemon, "queen", {})

    assert isinstance(result, dict)
    assert "content" in result
    assert "structuredContent" in result
    sc = result["structuredContent"]
    # Top-level is an object — keep schema friendly to MCP clients
    assert isinstance(sc, dict)
    assert "workers" in sc
    workers = sc["workers"]
    names = {w["name"] for w in workers}
    assert names == {"alpha", "beta"}
    # Per-worker fields should be the same data the text summary expresses
    for w in workers:
        assert "state" in w
        assert "context_pct" in w
        assert "task" in w  # null when idle


def test_view_worker_state_single_worker_returns_structured() -> None:
    """Targeted view returns one structured worker entry."""
    from swarm.mcp.queen_tools import _handle_view_worker_state

    daemon = _make_queen_daemon(
        workers=[_fake_worker("alpha", state="RESTING")],
        tasks=[],
    )

    result = _handle_view_worker_state(daemon, "queen", {"worker": "alpha"})

    assert isinstance(result, dict)
    sc = result["structuredContent"]
    assert sc["worker"]["name"] == "alpha"
    assert sc["worker"]["state"] == "resting"
    assert sc["worker"]["kind"] == "claude"


def test_view_worker_state_unknown_worker_carries_structured() -> None:
    """#1432. REVERSES THIS FILE'S EARLIER ASSERTION, DELIBERATELY.

    The previous version of this test asserted the opposite — that the not-found
    path returns the bare list so "older clients don't see misleading empty
    structures". That reasoning was backwards. With no ``structuredContent`` key
    at all, the natural way to consume this tool
    (``result["structuredContent"]["worker"]``) RAISES on a mistyped worker name
    instead of reading an error, and the Queen is the only caller.

    The contract now: a client branches on a FIELD, never on the response's type.
    """
    from swarm.mcp.queen_tools import _handle_view_worker_state

    daemon = _make_queen_daemon(workers=[], tasks=[])
    result = _handle_view_worker_state(daemon, "queen", {"worker": "ghost"})

    assert isinstance(result, dict), "not-found must use the dict shape, not the legacy list"
    sc = result["structuredContent"]
    # THE POINT OF THE TICKET: reading the sidecar does not raise.
    assert sc["worker"] is None
    assert sc["error"] == "not_found"
    assert sc["requested"] == "ghost"


def test_view_worker_state_not_found_is_distinguishable_from_success() -> None:
    """AC2: the two outcomes must not be confusable by a client reading the sidecar.

    Asserts both directions from one daemon, so a change that made success look
    like an error (or vice versa) fails here rather than in production.
    """
    from swarm.mcp.queen_tools import _handle_view_worker_state

    daemon = _make_queen_daemon(workers=[_fake_worker("alpha", state="RESTING")], tasks=[])

    found = _handle_view_worker_state(daemon, "queen", {"worker": "alpha"})["structuredContent"]
    missing = _handle_view_worker_state(daemon, "queen", {"worker": "ghost"})["structuredContent"]

    # Same discriminator read on both, opposite answers — no type sniffing.
    assert found.get("error") is None
    assert missing.get("error") == "not_found"
    assert found["worker"] is not None
    assert missing["worker"] is None
    assert found["worker"]["name"] == "alpha"


def test_view_worker_state_not_found_text_block_is_unchanged() -> None:
    """The human-readable half of the contract did NOT change in #1432.

    Only the sidecar was added. A text-only client (or a thread log) must read
    exactly what it read before, or this was a breaking change and not a fix.
    """
    from swarm.mcp.queen_tools import _handle_view_worker_state

    daemon = _make_queen_daemon(workers=[], tasks=[])
    result = _handle_view_worker_state(daemon, "queen", {"worker": "ghost"})

    assert result["content"] == [{"type": "text", "text": "Worker 'ghost' not found."}]


def test_view_worker_state_not_found_envelope_carries_structured() -> None:
    """Proves it at the DISPATCHER, not just the handler.

    ``_handle_tools_call`` only surfaces ``structuredContent`` when it is not
    None (server.py), so a handler can return a sidecar that never reaches the
    wire. This drives the same function that builds the JSON-RPC envelope —
    the seam where the contract actually lives.
    """
    from swarm.mcp.server import _handle_tools_call

    daemon = _make_queen_daemon(workers=[], tasks=[])
    envelope = _handle_tools_call(
        daemon,
        "queen",
        {"name": "queen_view_worker_state", "arguments": {"worker": "ghost"}},
    )

    assert "structuredContent" in envelope, "sidecar was dropped between handler and wire"
    assert envelope["structuredContent"]["error"] == "not_found"
    assert envelope["content"] == [{"type": "text", "text": "Worker 'ghost' not found."}]
