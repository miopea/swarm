"""Regression tests for `src/swarm/hooks/approval_hook.sh`.

The shell script is the PreToolUse hook Claude Code invokes for every
tool call in a Swarm-managed worker session. Its guards decide whether
the daemon is queried at all — if they break, every worker stalls on
prompts the daemon can't see, or conversely the operator's interactive
session gets gated by drone rules it shouldn't be.

Task #211 added the ``SWARM_OPERATOR=1`` escape hatch — operator-driven
interactive workers carry ``SWARM_MANAGED=1`` from the PTY holder, so
without a second marker there was no way to honor the invariant
documented at the top of the script ("operator's own Claude Code session
is never gated by drone rules"). These tests lock the guard order in.
"""

from __future__ import annotations

import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import ClassVar

import pytest

_HOOK = Path(__file__).parent.parent / "src" / "swarm" / "hooks" / "approval_hook.sh"


class _CountingHandler(BaseHTTPRequestHandler):
    """Records every POST to /api/hooks/approval and replies with block."""

    calls: ClassVar[list[dict]] = []
    # #1588: the reply is now configurable so the approve path can be driven too.
    # Defaults to the original block reply so every pre-existing test is unchanged.
    reply: ClassVar[dict] = {"decision": "block", "reason": "from-test"}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body)
        except Exception:
            payload = {"_raw": body.decode("utf-8", "replace")}
        type(self).calls.append({"path": self.path, "body": payload})
        resp = json.dumps(type(self).reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *args, **kwargs):
        return


@pytest.fixture
def daemon_stub():
    """Start a local HTTP server that records every approval call."""
    _CountingHandler.calls = []
    _CountingHandler.reply = {"decision": "block", "reason": "from-test"}
    server = HTTPServer(("127.0.0.1", 0), _CountingHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", _CountingHandler.calls
    finally:
        server.shutdown()
        server.server_close()


def _run_hook(env: dict[str, str], payload: dict) -> subprocess.CompletedProcess:
    """Invoke approval_hook.sh with the given env and stdin payload."""
    merged = {**os.environ, **env}
    return subprocess.run(
        ["bash", str(_HOOK)],
        input=json.dumps(payload).encode(),
        env=merged,
        capture_output=True,
        timeout=10,
        check=False,
    )


def test_not_swarm_managed_exits_without_hitting_daemon(daemon_stub):
    """Sessions where SWARM_MANAGED != 1 must never query the daemon.

    This is the operator-outside-swarm path — a regular terminal that
    inherits nothing from the holder. The hook must be a transparent
    no-op there.
    """
    url, calls = daemon_stub
    result = _run_hook(
        env={"SWARM_URL": url, "SWARM_MANAGED": "", "SWARM_OPERATOR": ""},
        payload={"tool_name": "Bash", "tool_input": {"command": "git push origin main"}},
    )
    assert result.returncode == 0
    assert result.stdout == b""
    assert calls == []


def test_swarm_operator_bypass_skips_daemon(daemon_stub):
    """SWARM_OPERATOR=1 opts the operator's attached session out of drone rules.

    Task #211: the operator interacts with a Swarm-managed worker to do
    their own dev work. That worker carries SWARM_MANAGED=1 from the
    holder, so without a second marker the approval hook ran for every
    operator-driven tool call. SWARM_OPERATOR=1 is that second marker —
    when set, the hook must exit early without contacting the daemon.
    """
    url, calls = daemon_stub
    result = _run_hook(
        env={"SWARM_URL": url, "SWARM_MANAGED": "1", "SWARM_OPERATOR": "1"},
        payload={"tool_name": "Bash", "tool_input": {"command": "git push origin main"}},
    )
    assert result.returncode == 0
    assert result.stdout == b""
    assert calls == []


def test_managed_non_operator_queries_daemon(daemon_stub):
    """Autonomous workers (SWARM_MANAGED=1, no SWARM_OPERATOR) must query the daemon.

    Locks in the positive path — without this we could silently disable
    the whole approval flow via a typo in the guard order.
    """
    url, calls = daemon_stub
    result = _run_hook(
        env={"SWARM_URL": url, "SWARM_MANAGED": "1", "SWARM_OPERATOR": ""},
        payload={"tool_name": "Bash", "tool_input": {"command": "ls"}},
    )
    assert result.returncode == 0
    assert b'"decision":"block"' in result.stdout
    assert len(calls) == 1
    assert calls[0]["path"] == "/api/hooks/approval"
    assert calls[0]["body"]["tool_name"] == "Bash"


# ---------------------------------------------------------------------------
# #1588 — both PreToolUse schema forms, so a deprecation cannot silently disarm us
# ---------------------------------------------------------------------------
#
# #1528 established by reading the shipped binary (2.1.231) that the legacy
# `{"decision":"approve"}` STILL WORKS — the handler has an unconditional
# `if (e.decision) switch(...)` branch assigning the same `permissionBehavior` as the
# modern `hookSpecificOutput.permissionDecision` branch. Nothing is broken today.
#
# The risk is scheduled: the binary's own reference calls the field "deprecated for
# PreToolUse". When a release drops that branch, approvals become no-ops fleet-wide and
# THE FAILURE IS INVISIBLE — the hook still exits 0, the daemon still logs "approve",
# the buzz log still says the drone approved it. Emitting both is safe because the
# handler processes them in order and both write the same variable.


def _emit(daemon_stub, reply: dict) -> dict:
    """Drive the real script with a given daemon reply; return the parsed JSON it printed."""
    url, _ = daemon_stub
    _CountingHandler.reply = reply
    result = _run_hook(
        env={"SWARM_URL": url, "SWARM_MANAGED": "1", "SWARM_OPERATOR": ""},
        payload={"tool_name": "Bash", "tool_input": {"command": "ls"}},
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.strip(), "the script emitted nothing"
    return json.loads(result.stdout.decode())


def test_approve_carries_both_schema_forms(daemon_stub):
    """AC1/AC2, driven through the script rather than by reading it."""
    out = _emit(daemon_stub, {"decision": "approve"})

    assert out["decision"] == "approve"
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "allow"


def test_block_carries_both_schema_forms_and_both_reasons(daemon_stub):
    """The reason must reach BOTH fields — a version reading only the new form would
    otherwise deny with an empty explanation, which is how a block becomes unattributable."""
    out = _emit(daemon_stub, {"decision": "block", "reason": "rule #3 says no"})

    assert out["decision"] == "block"
    assert out["reason"] == "rule #3 says no"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == "rule #3 says no"


@pytest.mark.parametrize(
    ("daemon_decision", "expected_new"), [("approve", "allow"), ("block", "deny")]
)
def test_the_two_forms_never_disagree(daemon_stub, daemon_decision: str, expected_new: str):
    """THE PROPERTY, stated once. Both fields feed the same `permissionBehavior`, so a
    mismatch would make the outcome depend on which branch a given release happens to
    run — the worst possible failure here, because it would be version-dependent and
    intermittent rather than simply broken."""
    out = _emit(daemon_stub, {"decision": daemon_decision, "reason": "r"})

    assert out["decision"] == daemon_decision
    assert out["hookSpecificOutput"]["permissionDecision"] == expected_new


def test_a_reason_with_quotes_and_newlines_still_produces_valid_json(daemon_stub):
    """The escaping is now used TWICE. If one site loses it the object still parses at a
    glance in the other field, so this drives a reason that breaks naive quoting."""
    nasty = 'he said "no"\nand meant it \\ really'
    out = _emit(daemon_stub, {"decision": "block", "reason": nasty})

    assert out["reason"] == nasty
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == nasty


# --- The controls that matter more than the change itself -------------------


def test_no_decision_from_the_daemon_emits_nothing(daemon_stub):
    """POSITIVE CONTROL. A hook that starts emitting a decision where it previously
    stayed silent would begin OVERRIDING Claude Code's own permission logic on every
    tool call — strictly worse than the deprecation this ticket fixes, and invisible in
    exactly the same way."""
    url, calls = daemon_stub
    _CountingHandler.reply = {"reason": "no decision field at all"}

    result = _run_hook(
        env={"SWARM_URL": url, "SWARM_MANAGED": "1", "SWARM_OPERATOR": ""},
        payload={"tool_name": "Bash", "tool_input": {"command": "ls"}},
    )

    assert result.returncode == 0
    assert result.stdout == b""
    assert calls, "the daemon was never queried — this control proved nothing"


def test_an_unreachable_daemon_emits_nothing(daemon_stub):
    """The fail-open path. If the daemon is down the worker must fall back to Claude
    Code's normal prompting, not be silently allowed or silently blocked."""
    result = _run_hook(
        env={"SWARM_URL": "http://127.0.0.1:9", "SWARM_MANAGED": "1", "SWARM_OPERATOR": ""},
        payload={"tool_name": "Bash", "tool_input": {"command": "ls"}},
    )

    assert result.returncode == 0
    assert result.stdout == b""
