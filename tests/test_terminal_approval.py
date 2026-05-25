"""Tests for operator terminal approval detection and /action/add-approval-rule."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from swarm.config import DroneConfig, HiveConfig, QueenConfig
from swarm.drones.log import DroneLog
from swarm.drones.pilot import DronePilot
from swarm.queen.queen import Queen
from swarm.queen.queue import QueenCallQueue
from swarm.server.analyzer import QueenAnalyzer
from swarm.server.config_manager import ConfigManager
from swarm.server.daemon import SwarmDaemon
from swarm.server.email_service import EmailService
from swarm.server.proposals import ProposalManager
from swarm.server.task_manager import TaskManager
from swarm.server.worker_service import WorkerService
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskHistory
from swarm.tasks.proposal import ProposalStore
from swarm.web.app import handle_action_add_approval_rule
from swarm.worker.worker import Worker
from tests.conftest import make_worker as _make_worker
from tests.fakes.process import FakeWorkerProcess

_HEADERS = {"X-Requested-With": "Dashboard"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _set_content(workers: list[Worker], content: str, command: str = "claude") -> None:
    for w in workers:
        if w.process:
            w.process.set_content(content)
            w.process._child_foreground_command = command


@pytest.fixture
def pilot_setup(monkeypatch):
    """DronePilot with two workers for terminal-approval tests."""
    workers = [_make_worker("alpha"), _make_worker("beta")]
    log = DroneLog()
    pilot = DronePilot(workers, log, interval=1.0, pool=None, drone_config=DroneConfig())
    monkeypatch.setattr("swarm.drones.pilot.revive_worker", AsyncMock())
    return pilot, workers, log


# Claude-style choice prompt: numbered options with cursor arrow
_CHOICE_CONTENT = """\
  Allow `npm test` to run?

  > 1. Yes
    2. No
> """

# Accept-edits prompt content (matches _RE_ACCEPT_EDITS, no numbered choices)
_ACCEPT_EDITS_CONTENT = """\
  >> accept edits on src/main.py
  (Y)es / (N)o
> """

# Plan prompt content (matches _RE_PLAN_MARKERS — should be skipped)
_PLAN_CONTENT = """\
  Proceed with this plan?

  > 1. Yes
    2. No
> """

# User question content (matches is_user_question — should be skipped)
_USER_QUESTION_CONTENT = """\
  Which database should I use? Chat about this or type something.

  > 1. PostgreSQL
    2. MySQL
> """


async def _transition_waiting_to_buzzing(pilot, workers, content, command="claude"):
    """Drive workers through WAITING then back to BUZZING.

    Drones are disabled so no auto-continue fires — the transition is
    "unexplained", simulating an operator pressing Enter in the terminal.
    """
    pilot.enabled = False  # prevent drone auto-continue

    # WAITING only needs 1 confirmation from BUZZING (strong signal)
    _set_content(workers, content, command)
    await pilot.poll_once()

    # Now set BUZZING content to trigger WAITING→BUZZING
    _set_content(workers, "esc to interrupt", command)
    await pilot.poll_once()


# ---------------------------------------------------------------------------
# Detection tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drone_continue_not_flagged(pilot_setup):
    """WAITING→BUZZING after drone CONTINUE should NOT emit terminal approval."""
    pilot, workers, log = pilot_setup
    events: list[tuple] = []
    pilot.on("operator_terminal_approval", lambda *a: events.append(a))
    pilot.enabled = True

    # Make workers WAITING (1 confirmation needed for WAITING)
    _set_content(workers, _CHOICE_CONTENT)
    await pilot.poll_once()

    # Mark both as drone-continued (simulating what _execute_deferred_actions does)
    for w in workers:
        pilot._drone_continued.add(w.name)

    # Transition to BUZZING
    _set_content(workers, "esc to interrupt")
    await pilot.poll_once()

    assert len(events) == 0, "Drone-continued workers should not trigger terminal approval"


@pytest.mark.asyncio
async def test_button_continue_also_offers_rule(pilot_setup):
    """mark_operator_continue() + WAITING→BUZZING should also emit terminal approval."""
    pilot, workers, log = pilot_setup
    events: list[tuple] = []
    pilot.on("operator_terminal_approval", lambda *a: events.append(a))
    pilot.enabled = False  # prevent drone auto-continue

    # Make workers WAITING
    _set_content(workers, _CHOICE_CONTENT)
    await pilot.poll_once()

    # Mark both as button-continued
    for w in workers:
        pilot.mark_operator_continue(w.name)

    # Transition to BUZZING
    _set_content(workers, "esc to interrupt")
    await pilot.poll_once()

    assert len(events) > 0, "Button-continued workers should also trigger approval banner"


@pytest.mark.asyncio
async def test_terminal_approval_detected(pilot_setup):
    """WAITING→BUZZING with no markers should emit operator_terminal_approval."""
    pilot, workers, log = pilot_setup
    events: list[tuple] = []
    pilot.on("operator_terminal_approval", lambda *a: events.append(a))

    await _transition_waiting_to_buzzing(pilot, workers, _CHOICE_CONTENT)

    assert len(events) > 0, "Terminal approval should be detected"
    # Each event is (worker, summary, prompt_type, pattern, prompt_snippet)
    for worker, summary, prompt_type, pattern, prompt_snippet in events:
        assert prompt_type == "choice"
        assert summary  # non-empty
        assert prompt_snippet  # should contain terminal context


@pytest.mark.asyncio
async def test_plan_prompt_skipped(pilot_setup):
    """Plan prompts should NOT trigger terminal approval."""
    pilot, workers, log = pilot_setup
    events: list[tuple] = []
    pilot.on("operator_terminal_approval", lambda *a: events.append(a))

    await _transition_waiting_to_buzzing(pilot, workers, _PLAN_CONTENT)

    assert len(events) == 0, "Plan prompts should not trigger terminal approval"


@pytest.mark.asyncio
async def test_user_question_skipped(pilot_setup):
    """User questions should NOT trigger terminal approval."""
    pilot, workers, log = pilot_setup
    events: list[tuple] = []
    pilot.on("operator_terminal_approval", lambda *a: events.append(a))

    await _transition_waiting_to_buzzing(pilot, workers, _USER_QUESTION_CONTENT)

    assert len(events) == 0, "User questions should not trigger terminal approval"


@pytest.mark.asyncio
async def test_accept_edits_detected(pilot_setup):
    """Accept-edits prompts should trigger terminal approval with prompt_type='accept_edits'."""
    pilot, workers, log = pilot_setup
    events: list[tuple] = []
    pilot.on("operator_terminal_approval", lambda *a: events.append(a))

    await _transition_waiting_to_buzzing(pilot, workers, _ACCEPT_EDITS_CONTENT)

    assert len(events) > 0, "Accept-edits should be detected"
    for worker, summary, prompt_type, pattern, prompt_snippet in events:
        assert prompt_type == "accept_edits"
        assert summary == "accept edits"
        assert prompt_snippet  # should contain terminal context


@pytest.mark.asyncio
async def test_terminal_approval_prompt_snippet_in_log(pilot_setup):
    """Operator terminal approval should include prompt_snippet in log metadata."""
    pilot, workers, log = pilot_setup

    await _transition_waiting_to_buzzing(pilot, workers, _CHOICE_CONTENT)

    operator_entries = [e for e in log.entries if e.action.value == "OPERATOR"]
    assert len(operator_entries) > 0, "Should have operator log entries"
    for entry in operator_entries:
        assert "prompt_snippet" in entry.metadata
        assert entry.metadata["prompt_snippet"]  # non-empty


def test_extract_prompt_snippet_basic():
    """extract_prompt_snippet returns last N lines of content."""
    from swarm.drones.pilot import extract_prompt_snippet

    content = "\n".join(f"line {i}" for i in range(30))
    snippet = extract_prompt_snippet(content, max_lines=10)
    lines = snippet.splitlines()
    assert len(lines) == 10
    assert lines[-1] == "line 29"
    assert lines[0] == "line 20"


def test_extract_prompt_snippet_short_content():
    """extract_prompt_snippet returns all lines when content is shorter than max."""
    from swarm.drones.pilot import extract_prompt_snippet

    content = "line 1\nline 2\nline 3"
    snippet = extract_prompt_snippet(content, max_lines=15)
    assert snippet == content


def test_extract_prompt_snippet_empty():
    """extract_prompt_snippet handles empty content gracefully."""
    from swarm.drones.pilot import extract_prompt_snippet

    assert extract_prompt_snippet("") == ""
    assert extract_prompt_snippet("   ") == ""


def test_pattern_suggestion_old_format():
    """_suggest_approval_pattern extracts multi-word pattern from old Bash(cmd) format."""
    from swarm.providers import get_provider

    provider = get_provider("claude")
    content = "Bash(npm test --coverage)\n  > 1. Yes, allow once\n    2. No\n"
    pattern = DronePilot._suggest_approval_pattern(content, provider)
    assert r"\bnpm\ test\b" == pattern


def test_pattern_suggestion_new_format():
    """_suggest_approval_pattern extracts multi-word pattern from new 'Bash command' format."""
    from swarm.providers import get_provider

    provider = get_provider("claude")
    content = "Bash command\n  az webapp restart --name foo\n  > 1. Yes\n    2. No\n"
    pattern = DronePilot._suggest_approval_pattern(content, provider)
    assert r"\baz\ webapp\b" == pattern


def test_pattern_suggestion_accept_edits():
    """_suggest_approval_pattern returns 'accept edits' for accept-edits prompts."""
    from swarm.providers import get_provider

    provider = get_provider("claude")
    content = ">> accept edits on 3 files\n"
    pattern = DronePilot._suggest_approval_pattern(content, provider)
    assert pattern == "accept edits"


def test_pattern_suggestion_no_match():
    """_suggest_approval_pattern returns empty string for unrecognised prompts."""
    from swarm.providers import get_provider

    provider = get_provider("claude")
    content = "Some random output\n> "
    pattern = DronePilot._suggest_approval_pattern(content, provider)
    assert pattern == ""


def test_cleanup_on_dead_worker(pilot_setup):
    """Tracking data should be cleaned up when a worker is reaped."""
    pilot, workers, log = pilot_setup

    # Populate tracking data
    pilot._waiting_content["alpha"] = "cached"
    pilot._drone_continued.add("alpha")
    pilot._operator_continued.add("alpha")

    # Directly call _cleanup_dead_workers (simulates reap after STUNG timeout)
    pilot._cleanup_dead_workers([workers[0]])

    assert "alpha" not in pilot._waiting_content
    assert "alpha" not in pilot._drone_continued
    assert "alpha" not in pilot._operator_continued


# ---------------------------------------------------------------------------
# Endpoint tests: /action/add-approval-rule
# ---------------------------------------------------------------------------

_TEST_PASSWORD = "test-secret"


@pytest.fixture
def daemon(monkeypatch):
    monkeypatch.setattr("swarm.queen.queen.load_session", lambda _: None)
    monkeypatch.setattr("swarm.queen.queen.save_session", lambda *a: None)

    cfg = HiveConfig(session_name="test", api_password=_TEST_PASSWORD)
    cfg.source_path = str(Path(tempfile.mktemp(suffix=".yaml")))
    d = SwarmDaemon.__new__(SwarmDaemon)
    d.config = cfg
    d.workers = [
        Worker(name="w1", path="/tmp/w1", process=FakeWorkerProcess(name="w1")),
    ]
    d._worker_lock = asyncio.Lock()
    d.drone_log = DroneLog()
    d.task_board = TaskBoard()
    d.task_history = TaskHistory(log_file=Path(tempfile.mktemp(suffix=".jsonl")))
    d.queen = Queen(config=QueenConfig(cooldown=0.0), session_name="test")
    d.queen_queue = QueenCallQueue(max_concurrent=2)
    d.proposal_store = ProposalStore()
    d.notification_bus = MagicMock()
    d.pilot = MagicMock(spec=DronePilot)
    d.pilot.enabled = True
    d._bg_tasks: set[asyncio.Task[object]] = set()
    d.broadcast_ws = MagicMock()

    from swarm.server.broadcast import BroadcastHub

    d.hub = BroadcastHub(track_task=lambda t: d._bg_tasks.add(t))
    d.hub.ws_clients = set()
    d.hub.terminal_ws_clients = set()
    d.pool = None
    d.start_time = 0.0
    d.proposals = ProposalManager(
        store=d.proposal_store,
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        notification_bus=d.notification_bus,
        task_board=d.task_board,
        get_worker=lambda name: d.get_worker(name),
        get_workers=lambda: d.workers,
        get_pilot=lambda: d.pilot,
        assign_task=lambda *a, **kw: d.assign_and_start_task(*a, **kw),
        complete_task=lambda *a, **kw: d.complete_task(*a, **kw),
        execute_escalation=lambda p: d.analyzer.execute_escalation(p),
    )
    d.analyzer = QueenAnalyzer(
        queen=d.queen,
        queue=d.queen_queue,
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        emit_event=d.emit,
        proposal_store=d.proposal_store,
        queue_proposal=d.queue_proposal,
        task_board=d.task_board,
        get_worker=lambda name: d.get_worker(name),
        require_worker=lambda name: d._require_worker(name),
        get_workers=lambda: d.workers,
        get_pool=lambda: d.pool,
        get_config=lambda: d.config,
        get_worker_descriptions=lambda: d._worker_descriptions(),
        clear_escalation=lambda name: d.pilot.clear_escalation(name) if d.pilot else None,
    )
    d.graph_mgr = None
    d.email = EmailService(
        drone_log=d.drone_log,
        queen=d.queen,
        graph_mgr=d.graph_mgr,
        broadcast_ws=d.broadcast_ws,
    )
    d.tasks = TaskManager(
        task_board=d.task_board,
        task_history=d.task_history,
        drone_log=d.drone_log,
        pilot=d.pilot,
    )
    d.send_to_worker = AsyncMock()
    d._heartbeat_task = None
    d._usage_task = None
    d._heartbeat_snapshot = {}
    d._bg_tasks: set[asyncio.Task[object]] = set()

    from swarm.server.escalation_handler import EscalationHandler

    d.escalation = EscalationHandler(
        broadcast_ws=d.broadcast_ws,
        notification_bus=d.notification_bus,
        proposal_store=d.proposal_store,
        get_analyzer=lambda: d.analyzer,
        get_queen=lambda: d.queen,
        emit=d.emit,
    )

    from swarm.server.proposal_coordinator import ProposalCoordinator

    d.proposal_coord = ProposalCoordinator(
        proposals=d.proposals,
        proposal_store=d.proposal_store,
        get_analyzer=lambda: d.analyzer,
        get_queen=lambda: d.queen,
        broadcast_ws=d.broadcast_ws,
        notification_bus=d.notification_bus,
        get_pilot=lambda: d.pilot,
        assign_task=lambda *a, **kw: d.assign_and_start_task(*a, **kw),
        track_task=lambda t: d._bg_tasks.add(t),
        emit=d.emit,
    )
    d.config_mgr = ConfigManager(
        config=cfg,
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        apply_config=d.apply_config,
        get_pilot=lambda: d.pilot,
        rebuild_graph=lambda: None,
    )
    d.worker_svc = WorkerService(
        broadcast_ws=d.broadcast_ws,
        drone_log=d.drone_log,
        task_board=d.task_board,
        get_pilot=lambda: d.pilot,
        get_pool=lambda: d.pool,
        get_config=lambda: d.config,
        get_workers=lambda: d.workers,
        set_workers=lambda ws: setattr(d, "workers", ws),
        worker_lock=d._worker_lock,
        init_pilot=lambda enabled: d.init_pilot(enabled=enabled),
    )
    return d


@pytest.fixture
async def client(daemon):
    app = web.Application()
    app["daemon"] = daemon
    app.router.add_post("/action/add-approval-rule", handle_action_add_approval_rule)
    async with TestClient(TestServer(app)) as c:
        yield c


@pytest.mark.asyncio
async def test_add_rule_valid(client, daemon):
    """Valid pattern → rule added, 200."""
    resp = await client.post(
        "/action/add-approval-rule",
        data={"pattern": r"\bnpm\b"},
        headers=_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert data["rule_added"] == r"\bnpm\b"
    rules = daemon.config.drones.approval_rules
    assert len(rules) == 1
    assert rules[0].pattern == r"\bnpm\b"
    assert rules[0].action == "approve"


@pytest.mark.asyncio
async def test_add_rule_invalid_regex(client, daemon):
    """Invalid regex → 400."""
    resp = await client.post(
        "/action/add-approval-rule",
        data={"pattern": "[invalid"},
        headers=_HEADERS,
    )
    assert resp.status == 400
    data = await resp.json()
    assert "invalid regex" in data["error"]


@pytest.mark.asyncio
async def test_add_rule_empty_pattern(client, daemon):
    """Empty pattern → 400."""
    resp = await client.post(
        "/action/add-approval-rule",
        data={"pattern": ""},
        headers=_HEADERS,
    )
    assert resp.status == 400
    data = await resp.json()
    assert "pattern" in data["error"]


# ---------------------------------------------------------------------------
# Pattern suggestion safety tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf /tmp/cache",
        "rmdir old_dir",
        "kill -9 1234",
        "killall node",
        "pkill python",
        "dd if=/dev/zero of=/dev/sda",
        "mkfs.ext4 /dev/sda1",
        "sudo apt install foo",
        "mv important.txt /dev/null",
        "chmod 777 /etc/passwd",
        "chown root:root /tmp",
        "reboot",
        "shutdown -h now",
        "halt",
    ],
)
def test_pattern_suggestion_rejects_dangerous_cmds(cmd):
    """Dangerous commands should return empty string so the modal opens empty."""
    from swarm.providers import get_provider

    provider = get_provider("claude")

    # Test old format
    content_old = f"Bash({cmd})\n  > 1. Yes\n    2. No\n"
    assert DronePilot._suggest_approval_pattern(content_old, provider) == ""

    # Test new format
    content_new = f"Bash command\n  {cmd}\n  > 1. Yes\n    2. No\n"
    assert DronePilot._suggest_approval_pattern(content_new, provider) == ""


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        ("npm test --coverage", r"\bnpm\ test\b"),
        ("az webapp restart --name foo", r"\baz\ webapp\b"),
        ("git status", r"\bgit\ status\b"),
        ("uv run pytest tests/", r"\buv\ run\ pytest\b"),
        ("npx run vitest --watch", r"\bnpx\ run\ vitest\b"),
        ("ls -la", r"\bls\ \-la\b"),
        ("python", r"\bpython\b"),  # single word → single word pattern
    ],
)
def test_pattern_suggestion_specific_patterns(cmd, expected):
    """Patterns should include multiple words for specificity."""
    from swarm.providers import get_provider

    provider = get_provider("claude")
    content = f"Bash({cmd})\n  > 1. Yes\n    2. No\n"
    assert DronePilot._suggest_approval_pattern(content, provider) == expected
