"""Email notification backend for the swarm notification bus."""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from typing import TYPE_CHECKING

from swarm.logging import get_logger
from swarm.notify._util import run_detached

if TYPE_CHECKING:
    from swarm.config.models import EmailConfig
    from swarm.notify.bus import NotifyEvent

_log = get_logger("notify.email")
_TIMEOUT = 10  # seconds


def make_email_backend(config: EmailConfig) -> Callable[[NotifyEvent], None]:
    """Create an email notification backend.

    Returns a callable that sends email notifications for matching events.
    Filters by event type if ``config.events`` is non-empty.
    """
    from swarm.notify.bus import EventType

    # Match filtered_backend: tolerate unknown event names by skipping
    # them with a debug log, instead of raising during config apply.
    allowed: set[EventType] | None
    if config.events:
        allowed = set()
        for e in config.events:
            try:
                allowed.add(EventType(e))
            except ValueError:
                _log.debug("email backend: ignoring unknown event type %r", e)
    else:
        allowed = None

    def _send(event: NotifyEvent) -> None:
        if allowed and event.event_type not in allowed:
            return
        if not config.to_addresses or not config.from_address:
            return

        msg = EmailMessage()
        msg["Subject"] = f"[Swarm] {event.title}"
        msg["From"] = config.from_address
        msg["To"] = ", ".join(config.to_addresses)
        msg.set_content(
            f"{event.message}\n\n"
            f"Severity: {event.severity.value}\n"
            f"Worker: {event.worker_name or 'N/A'}\n"
        )

        def _deliver() -> None:
            try:
                with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=_TIMEOUT) as server:
                    if config.use_tls:
                        server.starttls()
                    if config.smtp_user:
                        server.login(config.smtp_user, config.smtp_password)
                    server.send_message(msg)
            except Exception:
                _log.warning("failed to send email notification", exc_info=True)

        # smtplib is blocking (connect/STARTTLS/login can take seconds); run it
        # off the event loop so a slow SMTP server can't freeze the daemon.
        run_detached(_deliver, name="email-notify")

    return _send
