"""Tests for notify/bus.py and notify/desktop.py."""

from pathlib import Path
from unittest.mock import patch

import pytest

import swarm.notify.desktop as _desktop_mod
from swarm.notify.bus import EventType, NotificationBus, NotifyEvent, Severity
from swarm.notify.desktop import (
    _get_icon_path,
    _ps_escape,
    _send_notify_send,
    _send_wsl_toast,
    desktop_backend,
)


class TestNotificationBus:
    def test_emit_calls_backend(self):
        bus = NotificationBus(debounce_seconds=0)
        received = []
        bus.add_backend(lambda e: received.append(e))

        bus.emit(
            NotifyEvent(
                event_type=EventType.WORKER_IDLE,
                title="test",
                message="hello",
            )
        )
        assert len(received) == 1
        assert received[0].title == "test"

    def test_debounce(self):
        bus = NotificationBus(debounce_seconds=10.0)
        received = []
        bus.add_backend(lambda e: received.append(e))

        bus.emit(
            NotifyEvent(
                event_type=EventType.WORKER_IDLE,
                title="first",
                message="",
                worker_name="api",
            )
        )
        bus.emit(
            NotifyEvent(
                event_type=EventType.WORKER_IDLE,
                title="second",
                message="",
                worker_name="api",
            )
        )
        # Second should be debounced
        assert len(received) == 1

    def test_different_events_not_debounced(self):
        bus = NotificationBus(debounce_seconds=10.0)
        received = []
        bus.add_backend(lambda e: received.append(e))

        bus.emit(
            NotifyEvent(
                event_type=EventType.WORKER_IDLE,
                title="idle",
                message="",
                worker_name="api",
            )
        )
        bus.emit(
            NotifyEvent(
                event_type=EventType.WORKER_STUNG,
                title="stung",
                message="",
                worker_name="api",
            )
        )
        assert len(received) == 2

    def test_helper_methods(self):
        bus = NotificationBus(debounce_seconds=0)
        received = []
        bus.add_backend(lambda e: received.append(e))

        bus.emit_worker_idle("api")
        bus.emit_worker_stung("web")
        bus.emit_escalation("tests", "stuck")
        bus.emit_task_assigned("api", "Fix bug")
        bus.emit_task_completed("api", "Fix bug")

        assert len(received) == 5
        assert received[0].event_type == EventType.WORKER_IDLE
        assert received[1].event_type == EventType.WORKER_STUNG
        assert received[1].severity == Severity.WARNING
        assert received[2].event_type == EventType.WORKER_ESCALATED
        assert received[2].severity == Severity.URGENT
        assert received[3].event_type == EventType.TASK_ASSIGNED
        assert received[4].event_type == EventType.TASK_COMPLETED

    def test_backend_error_doesnt_crash(self):
        bus = NotificationBus(debounce_seconds=0)
        bus.add_backend(lambda e: 1 / 0)  # Will raise ZeroDivisionError
        received = []
        bus.add_backend(lambda e: received.append(e))

        # Should not raise
        bus.emit_worker_idle("api")
        # Second backend should still receive
        assert len(received) == 1

    def test_multiple_backends(self):
        bus = NotificationBus(debounce_seconds=0)
        r1, r2 = [], []
        bus.add_backend(lambda e: r1.append(e))
        bus.add_backend(lambda e: r2.append(e))

        bus.emit_worker_idle("api")
        assert len(r1) == 1
        assert len(r2) == 1


# --- Desktop notification backend tests ---


class TestPsEscape:
    def test_no_quotes(self):
        assert _ps_escape("hello world") == "hello world"

    def test_single_quotes_doubled(self):
        assert _ps_escape("it's a test") == "it''s a test"

    def test_multiple_quotes(self):
        assert _ps_escape("a'b'c") == "a''b''c"


class TestGetIconPath:
    def test_resolves_icon(self):
        """Icon file exists in the package — should return a Path."""
        # Reset cache
        _desktop_mod._icon_path = None
        path = _get_icon_path()
        assert path is not None
        assert path.name == "icon-192.png"
        assert path.exists()
        # Reset cache for other tests
        _desktop_mod._icon_path = None


class TestSendWslToast:
    @patch("swarm.notify.desktop.shutil.which", return_value="/usr/bin/powershell.exe")
    @patch("swarm.notify.desktop._get_win_icon_path", return_value=None)
    @patch("swarm.notify.desktop.subprocess.Popen")
    def test_toast_without_icon(self, mock_popen, _mock_icon, _mock_which):
        _send_wsl_toast("test title", "test body")
        mock_popen.assert_called_once()
        script = mock_popen.call_args[0][0][4]  # -Command script arg
        assert "ToastGeneric" in script
        assert "test title" in script
        assert "test body" in script
        assert "appLogoOverride" not in script

    @patch("swarm.notify.desktop.shutil.which", return_value="/usr/bin/powershell.exe")
    @patch(
        "swarm.notify.desktop._get_win_icon_path",
        return_value=r"\\wsl.localhost\Ubuntu\icon.png",
    )
    @patch("swarm.notify.desktop.subprocess.Popen")
    def test_toast_with_icon(self, mock_popen, _mock_icon, _mock_which):
        _send_wsl_toast("test title", "test body")
        mock_popen.assert_called_once()
        script = mock_popen.call_args[0][0][4]
        assert "appLogoOverride" in script
        assert "wsl.localhost" in script

    @patch("swarm.notify.desktop.shutil.which", return_value="/usr/bin/powershell.exe")
    @patch("swarm.notify.desktop._get_win_icon_path", return_value=None)
    @patch("swarm.notify.desktop.subprocess.Popen")
    def test_toast_escapes_quotes(self, mock_popen, _mock_icon, _mock_which):
        _send_wsl_toast("it's broken", "can't fix")
        script = mock_popen.call_args[0][0][4]
        assert "it''s broken" in script
        assert "can''t fix" in script

    @patch("swarm.notify.desktop.shutil.which", return_value=None)
    @patch("swarm.notify.desktop.subprocess.Popen")
    def test_no_powershell_noop(self, mock_popen, _mock_which):
        _send_wsl_toast("title", "body")
        mock_popen.assert_not_called()


class TestSendNotifySend:
    @patch("swarm.notify.desktop.shutil.which", return_value="/usr/bin/notify-send")
    @patch("swarm.notify.desktop._get_icon_path", return_value=None)
    @patch("swarm.notify.desktop.subprocess.Popen")
    def test_without_icon(self, mock_popen, _mock_icon, _mock_which):
        _send_notify_send("title", "body")
        cmd = mock_popen.call_args[0][0]
        assert "--icon" not in " ".join(cmd)
        assert "title" in cmd
        assert "body" in cmd

    @patch("swarm.notify.desktop.shutil.which", return_value="/usr/bin/notify-send")
    @patch("swarm.notify.desktop._get_icon_path")
    @patch("swarm.notify.desktop.subprocess.Popen")
    def test_with_icon(self, mock_popen, mock_icon, _mock_which):
        mock_icon.return_value = Path("/fake/icon-192.png")
        _send_notify_send("title", "body")
        cmd = mock_popen.call_args[0][0]
        assert "--icon=/fake/icon-192.png" in cmd

    @patch("swarm.notify.desktop.shutil.which", return_value=None)
    @patch("swarm.notify.desktop.subprocess.Popen")
    def test_no_notifysend_noop(self, mock_popen, _mock_which):
        _send_notify_send("title", "body")
        mock_popen.assert_not_called()


class TestDesktopBackend:
    def test_info_severity_skipped(self):
        """INFO events should not trigger any desktop notification."""
        event = NotifyEvent(
            event_type=EventType.WORKER_IDLE,
            title="idle",
            message="worker is idle",
            severity=Severity.INFO,
        )
        with patch("swarm.notify.desktop._is_wsl", return_value=True):
            with patch("swarm.notify.desktop._send_wsl_toast") as mock_toast:
                desktop_backend(event)
                mock_toast.assert_not_called()

    @patch("swarm.notify.desktop._is_wsl", return_value=True)
    @patch("swarm.notify.desktop._send_wsl_toast")
    def test_warning_sends_wsl_toast(self, mock_toast, _mock_wsl):
        event = NotifyEvent(
            event_type=EventType.WORKER_STUNG,
            title="api exited",
            message="Worker api has exited",
            severity=Severity.WARNING,
        )
        desktop_backend(event)
        mock_toast.assert_called_once_with("api exited", "Worker api has exited")

    @patch("swarm.notify.desktop._is_wsl", return_value=False)
    @patch("swarm.notify.desktop.platform")
    @patch("swarm.notify.desktop._send_notify_send")
    def test_urgent_sends_critical_urgency(self, mock_ns, mock_platform, _mock_wsl):
        mock_platform.system.return_value = "Linux"
        event = NotifyEvent(
            event_type=EventType.WORKER_ESCALATED,
            title="swarm escalated",
            message="Drones escalated swarm: stuck",
            severity=Severity.URGENT,
        )
        desktop_backend(event)
        mock_ns.assert_called_once_with(
            "swarm escalated", "Drones escalated swarm: stuck", "critical"
        )


# ---------------------------------------------------------------------------
# Email backend
# ---------------------------------------------------------------------------


class TestEmailBackend:
    @pytest.fixture(autouse=True)
    def _run_detached_inline(self, monkeypatch):
        """#notify-audit A: the SMTP send now runs on a daemon thread (so a slow
        server can't block the event loop). Run it inline here so the mocked
        smtplib assertions are deterministic."""
        import swarm.notify.email as email_mod

        monkeypatch.setattr(email_mod, "run_detached", lambda fn, **kw: fn())

    def test_sends_email(self):
        from swarm.config.models import EmailConfig
        from swarm.notify.email import make_email_backend

        config = EmailConfig(
            enabled=True,
            smtp_host="localhost",
            smtp_port=25,
            from_address="swarm@test.com",
            to_addresses=["admin@test.com"],
            use_tls=False,
        )
        backend = make_email_backend(config)

        with patch("swarm.notify.email.smtplib.SMTP") as mock_smtp:
            instance = mock_smtp.return_value.__enter__.return_value
            backend(
                NotifyEvent(
                    event_type=EventType.WORKER_STUNG,
                    title="Worker crashed",
                    message="api is stung",
                )
            )
            instance.send_message.assert_called_once()
            msg = instance.send_message.call_args[0][0]
            assert "[Swarm]" in msg["Subject"]
            assert "admin@test.com" in msg["To"]

    def test_filters_by_event_type(self):
        from swarm.config.models import EmailConfig
        from swarm.notify.email import make_email_backend

        config = EmailConfig(
            enabled=True,
            from_address="swarm@test.com",
            to_addresses=["admin@test.com"],
            events=["worker_stung"],
            use_tls=False,
        )
        backend = make_email_backend(config)

        with patch("swarm.notify.email.smtplib.SMTP") as mock_smtp:
            instance = mock_smtp.return_value.__enter__.return_value
            backend(
                NotifyEvent(
                    event_type=EventType.WORKER_IDLE,
                    title="idle",
                    message="idle",
                )
            )
            instance.send_message.assert_not_called()

    def test_handles_smtp_error(self):
        from swarm.config.models import EmailConfig
        from swarm.notify.email import make_email_backend

        config = EmailConfig(
            enabled=True,
            from_address="swarm@test.com",
            to_addresses=["admin@test.com"],
            use_tls=False,
        )
        backend = make_email_backend(config)

        with patch("swarm.notify.email.smtplib.SMTP", side_effect=OSError("refused")):
            # Should not raise
            backend(
                NotifyEvent(
                    event_type=EventType.WORKER_STUNG,
                    title="crash",
                    message="crash",
                )
            )


class TestRunDetached:
    def test_runs_fn_on_a_thread(self):
        """#notify-audit A: run_detached executes fn off the calling thread so a
        blocking backend can't freeze the event loop."""
        import threading

        from swarm.notify._util import run_detached

        done = threading.Event()
        seen = {}

        def fn():
            seen["thread"] = threading.current_thread().name
            done.set()

        run_detached(fn, name="unit-test-notify")
        assert done.wait(timeout=2), "detached fn did not run"
        assert seen["thread"] == "unit-test-notify"  # ran on the named daemon thread

    def test_thread_start_failure_is_swallowed(self, monkeypatch):
        from swarm.notify import _util

        monkeypatch.setattr(
            _util.threading, "Thread", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        # Must not raise into the emit path.
        _util.run_detached(lambda: None, name="x")


class TestNewEventHelpers:
    """Emit helpers added for the task/pipeline lifecycle + digest events."""

    def _bus(self):
        bus = NotificationBus(debounce_seconds=0)
        received = []
        bus.add_backend(lambda e: received.append(e))
        return bus, received

    def test_emit_task_failed(self):
        bus, received = self._bus()
        bus.emit_task_failed("alice", "fix the build")
        assert received[0].event_type == EventType.TASK_FAILED
        assert received[0].severity == Severity.WARNING
        assert "fix the build" in received[0].title

    def test_emit_task_reopened(self):
        bus, received = self._bus()
        bus.emit_task_reopened("alice", "fix the build")
        assert received[0].event_type == EventType.TASK_REOPENED

    def test_emit_pipeline_started(self):
        bus, received = self._bus()
        bus.emit_pipeline_started("nightly deploy")
        assert received[0].event_type == EventType.PIPELINE_STARTED
        assert "nightly deploy" in received[0].title

    def test_emit_pipeline_finished_success_vs_failed(self):
        bus, received = self._bus()
        bus.emit_pipeline_finished("nightly deploy", failed=False)
        bus.emit_pipeline_finished("other run", failed=True)
        assert received[0].severity == Severity.INFO
        assert received[1].severity == Severity.URGENT
        assert "FAILED" in received[1].title

    def test_emit_daily_digest(self):
        bus, received = self._bus()
        bus.emit_daily_digest("digest title", "digest body")
        assert received[0].event_type == EventType.DAILY_DIGEST
        assert received[0].message == "digest body"
