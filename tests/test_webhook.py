"""Tests for webhook notification backend."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from swarm.config.models import WebhookConfig
from swarm.notify import webhook as webhook_mod
from swarm.notify.bus import EventType, NotificationBus, NotifyEvent, Severity
from swarm.notify.webhook import _sanitize_url, make_webhook_backend


@pytest.fixture(autouse=True)
def _run_detached_inline(monkeypatch):
    """#notify-audit A: the webhook POST now runs on a daemon thread so it can't
    block the event loop. Run it inline in tests so assertions on the mocked
    urlopen are deterministic (no thread race)."""
    monkeypatch.setattr(webhook_mod, "run_detached", lambda fn, **kw: fn())


class TestSanitizeUrl:
    def test_drops_path_query_and_userinfo(self) -> None:
        # Slack-style token in the PATH must not survive.
        assert (
            _sanitize_url("https://hooks.slack.com/services/T0/B0/SECRETxyz")
            == "https://hooks.slack.com"
        )
        # ntfy-style token in the QUERY must not survive.
        out = _sanitize_url("https://ntfy.sh/mytopic?auth=tk_secret")
        assert "tk_secret" not in out and out == "https://ntfy.sh"

    def test_failure_logs_sanitized_url_not_token(self, caplog) -> None:
        import logging

        with patch(
            "swarm.notify.webhook.urllib.request.urlopen",
            side_effect=ConnectionError("refused"),
        ):
            backend = make_webhook_backend(
                WebhookConfig(url="https://hooks.slack.com/services/T/B/SECRETxyz")
            )
            with caplog.at_level(logging.WARNING):
                backend(NotifyEvent(event_type=EventType.WORKER_STUNG, title="x", message="y"))
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "SECRETxyz" not in joined  # token not leaked
        assert "hooks.slack.com" in joined  # host still identifiable


class TestMakeWebhookBackend:
    def test_creates_callable(self) -> None:
        config = WebhookConfig(url="https://example.com/hook")
        backend = make_webhook_backend(config)
        assert callable(backend)

    @patch("swarm.notify.webhook.urllib.request.urlopen")
    def test_posts_json_payload(self, mock_urlopen: MagicMock) -> None:
        config = WebhookConfig(url="https://example.com/hook")
        backend = make_webhook_backend(config)

        event = NotifyEvent(
            event_type=EventType.WORKER_STUNG,
            title="api exited",
            message="Worker api has exited",
            severity=Severity.WARNING,
            worker_name="api",
        )
        backend(event)

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://example.com/hook"
        assert req.get_header("Content-type") == "application/json"
        body = json.loads(req.data)
        assert body["event"] == "worker_stung"
        assert body["title"] == "api exited"
        assert body["worker"] == "api"
        assert body["severity"] == "warning"

    @patch("swarm.notify.webhook.urllib.request.urlopen")
    def test_filters_by_event_type(self, mock_urlopen: MagicMock) -> None:
        config = WebhookConfig(
            url="https://example.com/hook",
            events=["worker_stung", "task_completed"],
        )
        backend = make_webhook_backend(config)

        # This event type is not in the filter list — should be skipped
        event = NotifyEvent(
            event_type=EventType.WORKER_IDLE,
            title="idle",
            message="idle",
            worker_name="api",
        )
        backend(event)
        mock_urlopen.assert_not_called()

        # This one is in the filter list — should be sent
        event2 = NotifyEvent(
            event_type=EventType.WORKER_STUNG,
            title="stung",
            message="stung",
            worker_name="api",
        )
        backend(event2)
        mock_urlopen.assert_called_once()

    @patch("swarm.notify.webhook.urllib.request.urlopen")
    def test_empty_events_sends_all(self, mock_urlopen: MagicMock) -> None:
        config = WebhookConfig(url="https://example.com/hook", events=[])
        backend = make_webhook_backend(config)

        event = NotifyEvent(
            event_type=EventType.DRONE_ACTION,
            title="action",
            message="msg",
        )
        backend(event)
        mock_urlopen.assert_called_once()

    @patch("swarm.notify.webhook.urllib.request.urlopen")
    def test_network_error_does_not_raise(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = ConnectionError("refused")
        config = WebhookConfig(url="https://example.com/hook")
        backend = make_webhook_backend(config)

        event = NotifyEvent(
            event_type=EventType.WORKER_STUNG,
            title="stung",
            message="msg",
            worker_name="api",
        )
        # Should not raise
        backend(event)


class TestWebhookIntegration:
    @patch("swarm.notify.webhook.urllib.request.urlopen")
    def test_bus_with_webhook_backend(self, mock_urlopen: MagicMock) -> None:
        config = WebhookConfig(url="https://hooks.slack.com/test")
        backend = make_webhook_backend(config)

        bus = NotificationBus(debounce_seconds=0)
        bus.add_backend(backend)

        bus.emit_worker_stung("api")
        mock_urlopen.assert_called_once()

        body = json.loads(mock_urlopen.call_args[0][0].data)
        assert body["event"] == "worker_stung"
        assert body["worker"] == "api"
