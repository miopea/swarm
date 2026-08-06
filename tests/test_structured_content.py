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


def test_view_worker_state_unknown_worker_omits_structured() -> None:
    """Error path falls back to plain text — no half-built sidecar."""
    from swarm.mcp.queen_tools import _handle_view_worker_state

    daemon = _make_queen_daemon(workers=[], tasks=[])
    result = _handle_view_worker_state(daemon, "queen", {"worker": "ghost"})

    # On the not-found error path the function returns the legacy
    # list[dict] so older clients don't see misleading empty structures.
    if isinstance(result, dict):
        assert "structuredContent" not in result or result["structuredContent"] is None
    else:
        assert result[0]["text"].startswith("Worker 'ghost' not found")
