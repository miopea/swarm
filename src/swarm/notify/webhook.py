"""Webhook notification backend — POST JSON to a configurable URL."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import TYPE_CHECKING

from swarm.logging import get_logger
from swarm.notify._util import run_detached
from swarm.notify.bus import NotifyEvent

if TYPE_CHECKING:
    from swarm.config.models import WebhookConfig

_log = get_logger("notify.webhook")

_TIMEOUT = 5  # seconds


def _sanitize_url(url: str) -> str:
    """Return ``scheme://host`` only — drop path, query, and userinfo.

    Webhook tokens can live in the query (ntfy ``?auth=``) AND in the path
    (Slack ``/services/T/B/secret``, Discord ``/api/webhooks/id/token``), so the
    only safe-to-log part is scheme+host. Keeps enough to identify which webhook
    failed without leaking the secret into logs.
    """
    try:
        p = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((p.scheme, p.hostname or "", "", "", "")) or "<webhook>"
    except ValueError:
        return "<webhook>"


def make_webhook_backend(config: WebhookConfig) -> Callable[[NotifyEvent], None]:
    """Create a webhook backend callable from a WebhookConfig.

    Returns a function compatible with NotificationBus.add_backend().
    The backend POSTs a JSON payload to the configured URL.
    If ``config.events`` is non-empty, only matching event types are sent.
    """
    url = config.url
    allowed_events = set(config.events) if config.events else None

    def webhook_backend(event: NotifyEvent) -> None:
        if allowed_events and event.event_type.value not in allowed_events:
            return

        payload = json.dumps(
            {
                "event": event.event_type.value,
                "title": event.title,
                "message": event.message,
                "severity": event.severity.value,
                "worker": event.worker_name,
                "timestamp": event.timestamp,
            }
        ).encode()

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        def _deliver() -> None:
            try:
                with urllib.request.urlopen(req, timeout=_TIMEOUT):
                    pass
            except Exception:
                # Log a sanitized URL — the configured webhook URL can embed a
                # token (ntfy/Slack) that must not land in the logs.
                _log.warning("webhook POST to %s failed", _sanitize_url(url), exc_info=True)

        # urllib is blocking; run the POST off the event loop so a slow/hung
        # webhook endpoint can't freeze the daemon (bounded by _TIMEOUT anyway).
        run_detached(_deliver, name="webhook-notify")

    return webhook_backend
