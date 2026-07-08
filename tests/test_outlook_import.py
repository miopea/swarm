"""Tests for the Import-from-Outlook feature (Microsoft Graph).

Covers:
- GraphTokenManager.list_inbox_messages — normalization + not-connected path.
- handle_list_outlook_messages — connected/not-configured/not-authenticated.
- handle_create_tasks_from_outlook — separate mode (N tasks), merge mode
  (1 task), empty selection, and per-message fetch failures.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from swarm.auth.graph import GraphTokenManager
from swarm.web.routes.tasks import (
    handle_create_tasks_from_outlook,
    handle_list_outlook_messages,
)
from tests.conftest import make_daemon

# --- fakes -----------------------------------------------------------------


class _FakeResp:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)


class _FakeSession:
    def __init__(self, resp: _FakeResp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, *a, **k):
        return self._resp


class _FakeGraph:
    def __init__(self, *, token="tok", messages=None):
        self._token = token
        self._messages = messages or []

    async def get_token(self):
        return self._token

    async def list_inbox_messages(self, limit):
        return self._messages[:limit]


class _Req:
    """Minimal aiohttp-request stand-in for the two handlers."""

    def __init__(self, daemon, *, json_body=None, query=None):
        self.app = {"daemon": daemon}
        self._json = json_body or {}
        self.query = query or {}

    async def json(self):
        return self._json


def _body(resp) -> dict:
    return json.loads(resp.text)


# --- GraphTokenManager.list_inbox_messages ---------------------------------


@pytest.mark.asyncio
async def test_list_inbox_messages_not_connected_returns_empty():
    mgr = GraphTokenManager("cid")  # no refresh token → get_token() is None
    assert await mgr.list_inbox_messages(10) == []


@pytest.mark.asyncio
async def test_list_inbox_messages_normalizes_graph_rows():
    mgr = GraphTokenManager("cid")
    # Prime a valid token so get_token() returns without a network refresh.
    mgr._access_token = "tok"
    mgr._refresh_token = "rt"
    mgr._expires_at = time.time() + 3600

    payload = {
        "value": [
            {
                "id": "AAA",
                "subject": "Server down",
                "from": {"emailAddress": {"name": "Ops", "address": "ops@x.com"}},
                "receivedDateTime": "2026-07-08T10:00:00Z",
                "bodyPreview": "  the server is down  ",
                "isRead": False,
            },
            {
                "id": "BBB",
                "subject": "",
                "from": {},
                "receivedDateTime": "2026-07-08T09:00:00Z",
                "isRead": True,
            },
        ]
    }
    with patch("aiohttp.ClientSession", return_value=_FakeSession(_FakeResp(200, payload))):
        rows = await mgr.list_inbox_messages(25)

    assert len(rows) == 2
    assert rows[0]["id"] == "AAA"
    assert rows[0]["from"] == "ops@x.com"
    assert rows[0]["from_name"] == "Ops"
    assert rows[0]["preview"] == "the server is down"  # trimmed
    assert rows[0]["is_read"] is False
    # Missing subject → placeholder; missing from → empty strings, no crash.
    assert rows[1]["subject"] == "(no subject)"
    assert rows[1]["from"] == ""
    assert rows[1]["is_read"] is True


@pytest.mark.asyncio
async def test_list_inbox_messages_non_200_returns_empty():
    mgr = GraphTokenManager("cid")
    mgr._access_token = "tok"
    mgr._refresh_token = "rt"
    mgr._expires_at = time.time() + 3600
    with patch("aiohttp.ClientSession", return_value=_FakeSession(_FakeResp(500, {}))):
        assert await mgr.list_inbox_messages(25) == []


# --- handle_list_outlook_messages ------------------------------------------


@pytest.mark.asyncio
async def test_list_handler_not_configured(monkeypatch):
    d = make_daemon(monkeypatch)
    d.graph_mgr = None
    resp = await handle_list_outlook_messages(_Req(d))
    body = _body(resp)
    assert body["connected"] is False
    assert body["messages"] == []


@pytest.mark.asyncio
async def test_list_handler_not_authenticated(monkeypatch):
    d = make_daemon(monkeypatch)
    d.graph_mgr = _FakeGraph(token=None)
    body = _body(await handle_list_outlook_messages(_Req(d)))
    assert body["connected"] is False


@pytest.mark.asyncio
async def test_list_handler_connected_passes_messages(monkeypatch):
    d = make_daemon(monkeypatch)
    d.graph_mgr = _FakeGraph(messages=[{"id": "AAA", "subject": "Hi"}])
    body = _body(await handle_list_outlook_messages(_Req(d, query={"limit": "5"})))
    assert body["connected"] is True
    assert body["messages"][0]["id"] == "AAA"


# --- handle_create_tasks_from_outlook --------------------------------------


def _fake_fields(monkeypatch, *, error_ids=()):
    async def fake(d, mid, token):
        if mid in error_ids:
            return {"error": f"Graph API 404 for {mid}"}
        return {
            "title": f"Email {mid}",
            "description": f"body of {mid}",
            "task_type": "chore",
            "attachments": [],
            "message_id": mid,
        }

    monkeypatch.setattr("swarm.web.routes.tasks._graph_email_fields", fake)


@pytest.mark.asyncio
async def test_create_separate_makes_one_task_per_email(monkeypatch):
    d = make_daemon(monkeypatch)
    d.graph_mgr = _FakeGraph()
    _fake_fields(monkeypatch)
    before = len(d.task_board.all_tasks)
    resp = await handle_create_tasks_from_outlook(
        _Req(d, json_body={"message_ids": ["A", "B", "C"], "mode": "separate"})
    )
    body = _body(resp)
    assert body["count"] == 3
    assert len(d.task_board.all_tasks) == before + 3


@pytest.mark.asyncio
async def test_create_merge_makes_single_combined_task(monkeypatch):
    d = make_daemon(monkeypatch)
    d.graph_mgr = _FakeGraph()
    _fake_fields(monkeypatch)
    before = len(d.task_board.all_tasks)
    resp = await handle_create_tasks_from_outlook(
        _Req(d, json_body={"message_ids": ["A", "B", "C"], "mode": "merge"})
    )
    body = _body(resp)
    assert body["count"] == 1
    assert len(d.task_board.all_tasks) == before + 1
    merged = next(t for t in d.task_board.all_tasks if t.title.startswith("3 emails:"))
    # All three bodies made it into the one task.
    assert "body of A" in merged.description
    assert "body of C" in merged.description


@pytest.mark.asyncio
async def test_create_empty_ids_is_rejected(monkeypatch):
    d = make_daemon(monkeypatch)
    d.graph_mgr = _FakeGraph()
    resp = await handle_create_tasks_from_outlook(_Req(d, json_body={"message_ids": []}))
    assert "message_ids required" in _body(resp)["error"]


@pytest.mark.asyncio
async def test_create_reports_per_message_failures(monkeypatch):
    d = make_daemon(monkeypatch)
    d.graph_mgr = _FakeGraph()
    _fake_fields(monkeypatch, error_ids=("B",))
    resp = await handle_create_tasks_from_outlook(
        _Req(d, json_body={"message_ids": ["A", "B", "C"], "mode": "separate"})
    )
    body = _body(resp)
    assert body["count"] == 2  # A + C created
    assert len(body["errors"]) == 1  # B failed, reported not fatal


@pytest.mark.asyncio
async def test_create_not_configured(monkeypatch):
    d = make_daemon(monkeypatch)
    d.graph_mgr = None
    resp = await handle_create_tasks_from_outlook(
        _Req(d, json_body={"message_ids": ["A"], "mode": "separate"})
    )
    assert "not configured" in _body(resp)["error"]
