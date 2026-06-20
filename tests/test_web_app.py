"""Tests for web/app.py — utility functions and action handlers."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from swarm.drones.log import DroneAction, DroneLog, LogCategory, SystemAction
from swarm.server.daemon import SwarmOperationError, WorkerNotFoundError
from swarm.server.helpers import handle_errors, json_error
from swarm.tasks.board import TaskBoard
from swarm.web.app import (
    _format_age,
    _require_queen,
    _system_log_dicts,
    _task_dicts,
    _worker_dicts,
)
from swarm.worker.worker import Worker, WorkerState
from tests.fakes.process import FakeWorkerProcess

# --- _format_age ---


def test_format_age_just_now():
    assert _format_age(time.time() - 30) == "just now"


def test_format_age_minutes():
    assert _format_age(time.time() - 300) == "5m ago"


def test_format_age_hours():
    assert _format_age(time.time() - 7200) == "2h ago"


def test_format_age_days():
    assert _format_age(time.time() - 172800) == "2d ago"


# --- _json_error ---


def test_json_error_default_status():
    resp = json_error("oops")
    assert resp.status == 400


def test_json_error_custom_status():
    resp = json_error("not found", 404)
    assert resp.status == 404


# --- _require_queen ---


def test_require_queen_present():
    d = MagicMock()
    d.queen = MagicMock()
    assert _require_queen(d) is d.queen


def test_require_queen_missing():
    d = MagicMock()
    d.queen = None
    with pytest.raises(SwarmOperationError, match="Queen not configured"):
        _require_queen(d)


# --- _worker_dicts ---


def test_worker_dicts():
    w = Worker(name="api", path="/tmp/api", process=FakeWorkerProcess(name="api"))
    w.state = WorkerState.BUZZING
    w.state_since = time.time() - 60
    daemon = MagicMock()
    daemon.workers = [w]
    result = _worker_dicts(daemon)
    assert len(result) == 1
    assert result[0]["name"] == "api"
    assert "state_duration" in result[0]
    # Should be human-readable, not raw seconds
    assert isinstance(result[0]["state_duration"], str)


# --- _task_dicts ---


def test_task_dicts_empty():
    daemon = MagicMock()
    daemon.task_board = TaskBoard()
    result = _task_dicts(daemon)
    assert result == []


def test_task_dicts_with_tasks():
    board = TaskBoard()
    board.create(title="Fix bug", description="desc")
    daemon = MagicMock()
    daemon.task_board = board
    result = _task_dicts(daemon)
    assert len(result) == 1
    assert result[0]["title"] == "Fix bug"
    assert result[0]["status"] == "unassigned"
    assert result[0]["priority"] == "normal"
    assert "created_age" in result[0]
    assert "blocked" in result[0]


def test_task_dicts_blocked():
    """Task with unmet dependency should be marked blocked."""
    board = TaskBoard()
    t1 = board.create(title="First")
    t2 = board.create(title="Second", depends_on=[t1.id])
    daemon = MagicMock()
    daemon.task_board = board
    result = _task_dicts(daemon)
    task_map = {t["id"]: t for t in result}
    assert task_map[t2.id]["blocked"] is True


def test_task_dicts_unblocked_when_dep_completed():
    """Task with completed dependency should not be blocked."""
    board = TaskBoard()
    t1 = board.create(title="First")
    t2 = board.create(title="Second", depends_on=[t1.id])
    # Assign first so it can be completed
    board.assign(t1.id, "worker1")
    board.complete(t1.id)
    daemon = MagicMock()
    daemon.task_board = board
    result = _task_dicts(daemon)
    task_map = {t["id"]: t for t in result}
    assert task_map[t2.id]["blocked"] is False


# --- _system_log_dicts ---


def test_system_log_dicts_empty():
    daemon = MagicMock()
    daemon.drone_log = DroneLog()
    result = _system_log_dicts(daemon)
    assert result == []


def test_system_log_dicts_excludes_system_by_default():
    """SYSTEM entries should be excluded when no category filter is set."""
    log = DroneLog()
    log.add(
        SystemAction.CONFIG_CHANGED,
        worker_name="api",
        detail="started",
        category=LogCategory.SYSTEM,
    )
    log.add(
        DroneAction.CONTINUED,
        worker_name="api",
        detail="continued",
        category=LogCategory.DRONE,
    )
    daemon = MagicMock()
    daemon.drone_log = log
    result = _system_log_dicts(daemon)
    # Only drone entry should appear
    assert len(result) == 1
    assert result[0]["category"] == "drone"


def test_system_log_dicts_category_filter():
    log = DroneLog()
    log.add(
        SystemAction.CONFIG_CHANGED,
        worker_name="api",
        detail="started",
        category=LogCategory.SYSTEM,
    )
    log.add(
        DroneAction.CONTINUED,
        worker_name="api",
        detail="continued",
        category=LogCategory.DRONE,
    )
    daemon = MagicMock()
    daemon.drone_log = log
    result = _system_log_dicts(daemon, category="system")
    assert len(result) == 1
    assert result[0]["category"] == "system"


def test_system_log_dicts_text_search():
    log = DroneLog()
    log.add(
        DroneAction.CONTINUED,
        worker_name="api",
        detail="resumed work",
        category=LogCategory.DRONE,
    )
    log.add(
        DroneAction.CONTINUED,
        worker_name="web",
        detail="kept going",
        category=LogCategory.DRONE,
    )
    daemon = MagicMock()
    daemon.drone_log = log
    result = _system_log_dicts(daemon, query="api")
    assert len(result) == 1
    assert result[0]["worker"] == "api"


def test_system_log_dicts_limit():
    log = DroneLog()
    for i in range(10):
        log.add(
            DroneAction.CONTINUED,
            worker_name=f"w{i}",
            detail=f"entry {i}",
            category=LogCategory.DRONE,
        )
    daemon = MagicMock()
    daemon.drone_log = log
    result = _system_log_dicts(daemon, limit=3)
    assert len(result) == 3


def test_system_log_dicts_multi_category_filter():
    """Comma-separated category filter should match any of the values."""
    log = DroneLog()
    log.add(DroneAction.CONTINUED, "api", "c", category=LogCategory.DRONE)
    log.add(SystemAction.TASK_CREATED, "api", "t", category=LogCategory.TASK)
    log.add(SystemAction.QUEEN_PROPOSAL, "api", "q", category=LogCategory.QUEEN)
    daemon = MagicMock()
    daemon.drone_log = log
    result = _system_log_dicts(daemon, category="drone,task")
    assert len(result) == 2
    cats = {r["category"] for r in result}
    assert cats == {"drone", "task"}


def test_system_log_dicts_operator_category():
    """OPERATOR entries should use operator category when explicitly set."""
    log = DroneLog()
    log.add(DroneAction.OPERATOR, "api", "continued", category=LogCategory.OPERATOR)
    daemon = MagicMock()
    daemon.drone_log = log
    result = _system_log_dicts(daemon)
    assert len(result) == 1
    assert result[0]["category"] == "operator"


def test_system_log_dicts_operator_filter():
    """Operator category should be filterable."""
    log = DroneLog()
    log.add(DroneAction.CONTINUED, "api", "drone", category=LogCategory.DRONE)
    log.add(DroneAction.OPERATOR, "api", "manual", category=LogCategory.OPERATOR)
    daemon = MagicMock()
    daemon.drone_log = log
    result = _system_log_dicts(daemon, category="operator")
    assert len(result) == 1
    assert result[0]["category"] == "operator"


def test_system_log_dicts_newest_first():
    """Entries should be returned newest-first."""
    log = DroneLog()
    log.add(DroneAction.CONTINUED, "api", "first", category=LogCategory.DRONE)
    log.add(DroneAction.CONTINUED, "api", "second", category=LogCategory.DRONE)
    log.add(DroneAction.CONTINUED, "api", "third", category=LogCategory.DRONE)
    daemon = MagicMock()
    daemon.drone_log = log
    result = _system_log_dicts(daemon)
    assert result[0]["detail"] == "third"
    assert result[-1]["detail"] == "first"


def test_system_log_dicts_invalid_category_ignored():
    """Invalid category values in comma-separated filter should be silently ignored."""
    log = DroneLog()
    log.add(DroneAction.CONTINUED, "api", "c", category=LogCategory.DRONE)
    daemon = MagicMock()
    daemon.drone_log = log
    result = _system_log_dicts(daemon, category="bogus,drone")
    assert len(result) == 1
    assert result[0]["category"] == "drone"


# --- handle_errors (formerly handle_swarm_errors, unified Phase C) ---


@pytest.mark.asyncio
async def test_handle_errors_success():
    @handle_errors
    async def handler(request):
        return web.Response(text="ok")

    resp = await handler(MagicMock())
    assert resp.status == 200


@pytest.mark.asyncio
async def test_handle_errors_worker_not_found():
    @handle_errors
    async def handler(request):
        raise WorkerNotFoundError("api")

    request = MagicMock()
    request.get = MagicMock(return_value="")
    resp = await handler(request)
    assert resp.status == 404


@pytest.mark.asyncio
async def test_handle_errors_operation_error():
    @handle_errors
    async def handler(request):
        raise SwarmOperationError("bad state")

    request = MagicMock()
    request.get = MagicMock(return_value="")
    resp = await handler(request)
    assert resp.status == 409


@pytest.mark.asyncio
async def test_handle_errors_generic_error():
    @handle_errors
    async def handler(request):
        raise RuntimeError("boom")

    request = MagicMock()
    request.get = MagicMock(return_value="")
    resp = await handler(request)
    assert resp.status == 500


# ---------------------------------------------------------------------------
# Action handler tests
# ---------------------------------------------------------------------------


def _make_request(daemon, match_info=None, post_data=None, app_extras=None):
    """Build a mock request wired to the given daemon mock."""
    from unittest.mock import AsyncMock

    req = MagicMock()
    req.app = {"daemon": daemon}
    if app_extras:
        req.app.update(app_extras)
    req.match_info = match_info or {}
    req.post = AsyncMock(return_value=post_data or {})
    req.query = {}
    return req


# --- handle_action_send ---


@pytest.mark.asyncio
async def test_action_send_calls_daemon(monkeypatch):
    from swarm.web.app import handle_action_send

    d = MagicMock()
    d.send_to_worker = AsyncMock()
    req = _make_request(d, match_info={"name": "api"}, post_data={"message": "hello"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_send(req)
    assert resp.status == 204
    d.send_to_worker.assert_called_once_with("api", "hello")


@pytest.mark.asyncio
async def test_action_send_empty_message(monkeypatch):
    from swarm.web.app import handle_action_send

    d = MagicMock()
    d.send_to_worker = AsyncMock()
    req = _make_request(d, match_info={"name": "api"}, post_data={"message": ""})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_send(req)
    assert resp.status == 204
    d.send_to_worker.assert_not_called()


# --- handle_action_continue ---


@pytest.mark.asyncio
async def test_action_continue_calls_daemon():
    from swarm.web.app import handle_action_continue

    d = MagicMock()
    d.continue_worker = AsyncMock()
    req = _make_request(d, match_info={"name": "web"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_continue(req)
    assert resp.status == 204
    d.continue_worker.assert_called_once_with("web")


# --- handle_action_interrupt ---


@pytest.mark.asyncio
async def test_action_interrupt_calls_daemon():
    from swarm.web.app import handle_action_interrupt

    d = MagicMock()
    d.interrupt_worker = AsyncMock()
    req = _make_request(d, match_info={"name": "api"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_interrupt(req)
    assert resp.status == 200
    d.interrupt_worker.assert_called_once_with("api")


# --- handle_action_kill ---


@pytest.mark.asyncio
async def test_action_kill_calls_daemon():
    from swarm.web.app import handle_action_kill

    d = MagicMock()
    d.kill_worker = AsyncMock()
    req = _make_request(d, match_info={"name": "api"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_kill(req)
    assert resp.status == 200
    d.kill_worker.assert_called_once_with("api")


# --- handle_action_revive ---


@pytest.mark.asyncio
async def test_action_revive_calls_daemon():
    from swarm.web.app import handle_action_revive

    d = MagicMock()
    d.revive_worker = AsyncMock()
    req = _make_request(d, match_info={"name": "api"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_revive(req)
    assert resp.status == 200
    d.revive_worker.assert_called_once_with("api")


# --- handle_action_escape ---


@pytest.mark.asyncio
async def test_action_escape_calls_daemon():
    from swarm.web.app import handle_action_escape

    d = MagicMock()
    d.escape_worker = AsyncMock()
    req = _make_request(d, match_info={"name": "api"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_escape(req)
    assert resp.status == 200
    d.escape_worker.assert_called_once_with("api")


# --- handle_action_redraw ---


@pytest.mark.asyncio
async def test_action_redraw_calls_daemon():
    from swarm.web.app import handle_action_redraw

    d = MagicMock()
    d.redraw_worker = AsyncMock()
    req = _make_request(d, match_info={"name": "api"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_redraw(req)
    assert resp.status == 200
    d.redraw_worker.assert_called_once_with("api")


# --- handle_action_toggle_drones ---


@pytest.mark.asyncio
async def test_action_toggle_drones_on():
    from swarm.web.app import handle_action_toggle_drones

    d = MagicMock()
    d.pilot = MagicMock()
    d.toggle_drones = MagicMock(return_value=True)
    req = _make_request(d)
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_toggle_drones(req)
    assert resp.status == 200
    import json

    body = json.loads(resp.body)
    assert body["enabled"] is True


@pytest.mark.asyncio
async def test_action_toggle_drones_no_pilot():
    from swarm.web.app import handle_action_toggle_drones

    d = MagicMock()
    d.pilot = None
    req = _make_request(d)
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_toggle_drones(req)
    assert resp.status == 200
    import json

    body = json.loads(resp.body)
    assert body["enabled"] is False
    assert "error" in body


# --- handle_action_continue_all ---


@pytest.mark.asyncio
async def test_action_continue_all():
    from swarm.web.app import handle_action_continue_all

    d = MagicMock()
    d.continue_all = AsyncMock(return_value=3)
    req = _make_request(d)
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_continue_all(req)
    import json

    body = json.loads(resp.body)
    assert body["count"] == 3


# --- handle_action_send_all ---


@pytest.mark.asyncio
async def test_action_send_all_with_message():
    from swarm.web.app import handle_action_send_all

    d = MagicMock()
    d.send_all = AsyncMock(return_value=2)
    req = _make_request(d, post_data={"message": "do it"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_send_all(req)
    import json

    body = json.loads(resp.body)
    assert body["count"] == 2


@pytest.mark.asyncio
async def test_action_send_all_empty_message():
    from swarm.web.app import handle_action_send_all

    d = MagicMock()
    req = _make_request(d, post_data={"message": ""})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_send_all(req)
    assert resp.status == 400


# --- handle_action_create_task ---


@pytest.mark.asyncio
async def test_action_create_task():
    from swarm.web.app import handle_action_create_task

    d = MagicMock()
    task = MagicMock()
    task.id = "t1"
    task.title = "Fix bug"
    task.priority = MagicMock(value="high")
    task.task_type = MagicMock(value="bug")
    d.create_task_smart = AsyncMock(return_value=task)
    req = _make_request(
        d,
        post_data={
            "title": "Fix bug",
            "description": "desc",
            "priority": "high",
            "task_type": "",
            "depends_on": "",
            "attachments": "",
            "source_email_id": "",
        },
    )
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_create_task(req)
    assert resp.status == 201
    d.create_task_smart.assert_called_once()


# --- handle_action_assign_task ---


@pytest.mark.asyncio
async def test_action_assign_task():
    from swarm.web.app import handle_action_assign_task

    d = MagicMock()
    d.assign_task = AsyncMock()
    req = _make_request(d, post_data={"task_id": "t1", "worker": "api"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_assign_task(req)
    assert resp.status == 200
    d.assign_task.assert_called_once_with("t1", "api")


@pytest.mark.asyncio
async def test_action_assign_task_missing_fields():
    from swarm.web.app import handle_action_assign_task

    d = MagicMock()
    req = _make_request(d, post_data={"task_id": "t1"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_assign_task(req)
    assert resp.status == 400


# --- handle_action_complete_task ---


@pytest.mark.asyncio
async def test_action_complete_task():
    from swarm.web.app import handle_action_complete_task

    d = MagicMock()
    d.task_board.get.return_value = MagicMock(source_email_id="")
    req = _make_request(d, post_data={"task_id": "t1", "resolution": "done"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_complete_task(req)
    assert resp.status == 200
    d.complete_task.assert_called_once_with("t1", resolution="done")


@pytest.mark.asyncio
async def test_action_complete_task_email_draft():
    from swarm.web.app import handle_action_complete_task

    d = MagicMock()
    d.task_board.get.return_value = MagicMock(source_email_id="msg-123")
    req = _make_request(d, post_data={"task_id": "t1", "resolution": "fixed"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_complete_task(req)
    assert resp.status == 200
    d.complete_task.assert_called_once_with("t1", resolution="fixed")


@pytest.mark.asyncio
async def test_action_complete_task_missing_id():
    from swarm.web.app import handle_action_complete_task

    d = MagicMock()
    req = _make_request(d, post_data={})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_complete_task(req)
    assert resp.status == 400


# --- handle_action_remove_task ---


@pytest.mark.asyncio
async def test_action_remove_task():
    from swarm.web.app import handle_action_remove_task

    d = MagicMock()
    req = _make_request(d, post_data={"task_id": "t1"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_remove_task(req)
    assert resp.status == 200
    d.remove_task.assert_called_once_with("t1")


@pytest.mark.asyncio
async def test_action_remove_task_missing_id():
    from swarm.web.app import handle_action_remove_task

    d = MagicMock()
    req = _make_request(d, post_data={})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_remove_task(req)
    assert resp.status == 400


# --- handle_action_fail_task ---


@pytest.mark.asyncio
async def test_action_fail_task():
    from swarm.web.app import handle_action_fail_task

    d = MagicMock()
    req = _make_request(d, post_data={"task_id": "t1"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_fail_task(req)
    assert resp.status == 200
    d.fail_task.assert_called_once_with("t1")


# --- handle_action_reopen_task ---


@pytest.mark.asyncio
async def test_action_reopen_task():
    from swarm.web.app import handle_action_reopen_task

    d = MagicMock()
    req = _make_request(d, post_data={"task_id": "t1"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_reopen_task(req)
    assert resp.status == 200
    d.reopen_task.assert_called_once_with("t1")


# --- handle_action_unassign_task ---


@pytest.mark.asyncio
async def test_action_unassign_task():
    from swarm.web.app import handle_action_unassign_task

    d = MagicMock()
    req = _make_request(d, post_data={"task_id": "t1"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_unassign_task(req)
    assert resp.status == 200
    d.unassign_task.assert_called_once_with("t1")


# --- handle_action_reject_proposal ---


@pytest.mark.asyncio
async def test_action_reject_proposal():
    from swarm.web.app import handle_action_reject_proposal

    d = MagicMock()
    req = _make_request(d, post_data={"proposal_id": "p1"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_reject_proposal(req)
    assert resp.status == 200
    d.reject_proposal.assert_called_once_with("p1")


@pytest.mark.asyncio
async def test_action_reject_proposal_missing_id():
    from swarm.web.app import handle_action_reject_proposal

    d = MagicMock()
    req = _make_request(d, post_data={})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_reject_proposal(req)
    assert resp.status == 400


# --- handle_action_reject_all_proposals ---


@pytest.mark.asyncio
async def test_action_reject_all_proposals():
    from swarm.web.app import handle_action_reject_all_proposals

    d = MagicMock()
    d.reject_all_proposals = MagicMock(return_value=5)
    req = _make_request(d)
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_reject_all_proposals(req)
    import json

    body = json.loads(resp.body)
    assert body["count"] == 5


# --- handle_action_spawn ---


@pytest.mark.asyncio
async def test_action_spawn():
    from swarm.web.app import handle_action_spawn

    d = MagicMock()
    spawned = MagicMock()
    spawned.name = "new-worker"
    d.spawn_worker = AsyncMock(return_value=spawned)
    req = _make_request(d, post_data={"name": "new-worker", "path": "/tmp/nw"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_spawn(req)
    assert resp.status == 200
    d.spawn_worker.assert_called_once()


@pytest.mark.asyncio
async def test_action_spawn_missing_fields():
    from swarm.web.app import handle_action_spawn

    d = MagicMock()
    req = _make_request(d, post_data={"name": "x"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_spawn(req)
    assert resp.status == 400


# --- handle_action_kill_session ---


@pytest.mark.asyncio
async def test_action_kill_session():
    from swarm.web.app import handle_action_kill_session

    d = MagicMock()
    d.kill_session = AsyncMock()
    req = _make_request(d, post_data={"all": "0"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_kill_session(req)
    assert resp.status == 200
    d.kill_session.assert_called_once_with(all_sessions=False)


@pytest.mark.asyncio
async def test_action_kill_session_all():
    from swarm.web.app import handle_action_kill_session

    d = MagicMock()
    d.kill_session = AsyncMock()
    req = _make_request(d, post_data={"all": "1"})
    with patch("swarm.server.daemon.console_log"):
        await handle_action_kill_session(req)
    d.kill_session.assert_called_once_with(all_sessions=True)


# --- handle_action_stop_server ---


@pytest.mark.asyncio
async def test_action_stop_server_with_event():
    import asyncio

    from swarm.web.app import handle_action_stop_server

    event = asyncio.Event()
    d = MagicMock()
    req = _make_request(d, app_extras={"shutdown_event": event})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_stop_server(req)
    assert resp.status == 200
    assert event.is_set()


@pytest.mark.asyncio
async def test_action_stop_server_no_event():
    from swarm.web.app import handle_action_stop_server

    d = MagicMock()
    req = _make_request(d)
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_stop_server(req)
    assert resp.status == 500


# --- handle_partial_launch_config ---


@pytest.mark.asyncio
async def test_partial_launch_config():
    from swarm.config import WorkerConfig
    from swarm.web.app import handle_partial_launch_config

    d = MagicMock()
    d.workers = [Worker(name="api", path="/tmp/api")]
    d.config.workers = [
        WorkerConfig(name="api", path="/tmp/api"),
        WorkerConfig(name="web", path="/tmp/web"),
    ]
    d.config.groups = []
    req = _make_request(d)
    resp = await handle_partial_launch_config(req)
    import json

    body = json.loads(resp.body)
    assert len(body["workers"]) == 2
    api_entry = next(w for w in body["workers"] if w["name"] == "api")
    web_entry = next(w for w in body["workers"] if w["name"] == "web")
    assert api_entry["running"] is True
    assert web_entry["running"] is False


# --- handle_action_approve_proposal ---


@pytest.mark.asyncio
async def test_action_approve_proposal():
    from swarm.web.app import handle_action_approve_proposal

    d = MagicMock()
    d.approve_proposal = AsyncMock()
    req = _make_request(d, post_data={"proposal_id": "p1"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_approve_proposal(req)
    assert resp.status == 200
    d.approve_proposal.assert_called_once_with("p1")


@pytest.mark.asyncio
async def test_action_approve_proposal_missing_id():
    from swarm.web.app import handle_action_approve_proposal

    d = MagicMock()
    req = _make_request(d, post_data={})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_approve_proposal(req)
    assert resp.status == 400


# --- handle_action_add_approval_rule ---


@pytest.mark.asyncio
async def test_action_add_approval_rule():
    from swarm.web.app import handle_action_add_approval_rule

    d = MagicMock()
    d.config.drones.approval_rules = []
    d.drone_log = DroneLog()
    req = _make_request(d, post_data={"pattern": "npm test"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_add_approval_rule(req)
    assert resp.status == 200
    assert len(d.config.drones.approval_rules) == 1


@pytest.mark.asyncio
async def test_action_add_approval_rule_invalid_regex():
    from swarm.web.app import handle_action_add_approval_rule

    d = MagicMock()
    req = _make_request(d, post_data={"pattern": "[invalid"})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_add_approval_rule(req)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_action_add_approval_rule_empty_pattern():
    from swarm.web.app import handle_action_add_approval_rule

    d = MagicMock()
    req = _make_request(d, post_data={"pattern": ""})
    with patch("swarm.server.daemon.console_log"):
        resp = await handle_action_add_approval_rule(req)
    assert resp.status == 400


def test_dashboard_template_renders_queen_history_tab():
    """B4: the dashboard renders with the Queen history tab wired in.

    Renders dashboard.html through Jinja (ChainableUndefined for the
    many context vars helper-tested elsewhere) and asserts the Queen
    tab button, panel, filters, list, and detail modal are present —
    a regression guard so the tab can't silently disappear.
    """
    import jinja2

    class _Tunnel:
        url = ""

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader("src/swarm/web/templates"),
        undefined=jinja2.ChainableUndefined,
        autoescape=jinja2.select_autoescape(["html"]),
    )
    ctx = dict(
        groups=[],
        ws_token="t",
        is_dev=False,
        build_sha="abc",
        tunnel=_Tunnel(),
        worker_count=0,
        providers=[],
        csp_nonce="n",
        workers=[],
        queen=None,
        selected_worker=None,
        worker_output="",
        tasks=[],
        task_summary="",
        proposals=[],
        proposal_count=0,
        worker_tasks={},
        task_buttons=[],
        action_buttons=[],
        tool_buttons=[],
        pipelines=[],
        version="x",
    )
    html = env.get_template("dashboard.html").render(**ctx)
    for marker in (
        'id="tab-queen-btn"',
        'id="tab-queen"',
        'data-tab="queen"',
        'id="queen-history-list"',
        'id="qh-filter-kind"',
        'id="qh-search"',
        'id="qh-load-more-wrap"',
        'id="qh-detail-modal"',
        # B10 Messages tab
        'id="tab-messages-btn"',
        'id="tab-messages"',
        'data-tab="messages"',
        'id="messages-list"',
        'id="msg-filter-unread"',
        'id="msg-search"',
        'id="msg-load-more-wrap"',
        # B10 phase 3 — compose + bulk delete
        'id="msg-compose"',
        'data-action="msgSendCompose"',
        'id="msg-bulk-actions"',
        'data-action="msgBulkDelete"',
        'data-action="msgToggleSelect"',
    ):
        assert marker in html, f"missing dashboard tab marker: {marker}"
