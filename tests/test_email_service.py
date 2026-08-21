"""Tests for server/email_service.py — EmailService methods."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swarm.drones.log import DroneLog, LogCategory, SystemAction
from swarm.paths import state_dir
from swarm.server.email_service import EmailService
from swarm.tasks.task import TaskType

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def drone_log():
    return DroneLog()


@pytest.fixture
def queen():
    q = AsyncMock()
    q.draft_email_reply = AsyncMock(return_value="Thank you, this has been resolved.")
    return q


@pytest.fixture
def graph_mgr():
    mgr = AsyncMock()
    mgr.resolve_message_id = AsyncMock(return_value="GRAPH-MSG-ID-123")
    mgr.create_reply_draft = AsyncMock(return_value=True)
    return mgr


@pytest.fixture
def broadcast_ws():
    return MagicMock()


@pytest.fixture
def svc(tmp_path, drone_log, queen, graph_mgr, broadcast_ws):
    return EmailService(
        drone_log=drone_log,
        queen=queen,
        graph_mgr=graph_mgr,
        broadcast_ws=broadcast_ws,
        uploads_dir=tmp_path / "uploads",
    )


# ---------------------------------------------------------------------------
# save_attachment
# ---------------------------------------------------------------------------


class TestSaveAttachment:
    def test_saves_file_and_returns_path(self, svc, tmp_path):
        data = b"hello world"
        path = svc.save_attachment("test.txt", data)
        assert Path(path).exists()
        assert Path(path).read_bytes() == data

    def test_filename_includes_digest(self, svc):
        path = svc.save_attachment("photo.png", b"\x89PNG\r\n")
        name = Path(path).name
        # digest_filename pattern
        assert "_photo.png" in name
        assert len(name.split("_")[0]) == 12  # sha256[:12]

    def test_strips_directory_components(self, svc):
        path = svc.save_attachment("../../etc/passwd", b"sneaky")
        name = Path(path).name
        assert "passwd" in name
        assert ".." not in name

    def test_sanitizes_special_characters(self, svc):
        path = svc.save_attachment("mal\x00icious <script>.txt", b"xss")
        name = Path(path).name
        # Only alphanumeric, dots, dashes, underscores allowed
        assert "<" not in name
        assert ">" not in name
        assert "\x00" not in name

    def test_empty_name_after_sanitize_falls_back(self, svc):
        path = svc.save_attachment("///", b"empty")
        name = Path(path).name
        assert "attachment" in name

    def test_creates_uploads_dir(self, svc, tmp_path):
        uploads = tmp_path / "uploads"
        assert not uploads.exists()
        svc.save_attachment("f.txt", b"data")
        assert uploads.is_dir()

    def test_symlink_escape_blocked(self, svc, tmp_path):
        """Upload rejected when dest resolves outside uploads dir.

        Simulates a race where a symlink is planted at the expected
        dest path, causing resolve() to land outside the uploads dir.
        """
        import hashlib
        import os

        uploads = tmp_path / "uploads"
        uploads.mkdir(exist_ok=True)
        outside = tmp_path / "outside"
        outside.mkdir()

        data = b"pwned"
        digest = hashlib.sha256(data).hexdigest()[:12]
        # Pre-create a symlink at the exact dest path pointing outside
        dest_name = f"{digest}_evil.txt"
        symlink_path = uploads / dest_name
        os.symlink(outside / dest_name, symlink_path)

        with pytest.raises(ValueError, match="escapes"):
            svc.save_attachment("evil.txt", data)

    def test_uploads_dir_resolved_at_init(
        self, tmp_path, drone_log, queen, graph_mgr, broadcast_ws
    ):
        """_uploads_dir should be resolved (no symlink components) at init."""
        import os

        real_dir = tmp_path / "real_uploads"
        real_dir.mkdir()
        link = tmp_path / "link_uploads"
        os.symlink(real_dir, link)
        svc = EmailService(
            drone_log=drone_log,
            queen=queen,
            graph_mgr=graph_mgr,
            broadcast_ws=broadcast_ws,
            uploads_dir=link,
        )
        # Should be resolved to the real path, not the symlink
        assert svc._uploads_dir == real_dir.resolve()
        assert "link_uploads" not in str(svc._uploads_dir)


# ---------------------------------------------------------------------------
# fetch_and_save_image
# ---------------------------------------------------------------------------


class TestFetchAndSaveImage:
    @pytest.mark.asyncio
    async def test_blob_url_raises(self, svc):
        with pytest.raises(ValueError, match="blob:"):
            await svc.fetch_and_save_image("blob:http://example.com/abc")

    @pytest.mark.asyncio
    async def test_unsupported_scheme_raises(self, svc):
        with pytest.raises(ValueError, match="unsupported URL scheme"):
            await svc.fetch_and_save_image("ftp://example.com/file.png")

    @pytest.mark.asyncio
    async def test_data_url_raises(self, svc):
        with pytest.raises(ValueError, match="unsupported URL scheme"):
            await svc.fetch_and_save_image("data:image/png;base64,abc")

    @pytest.mark.asyncio
    async def test_file_url_success(self, svc, tmp_path):
        uploads = tmp_path / "uploads"
        uploads.mkdir(exist_ok=True)
        img = uploads / "pic.png"
        img.write_bytes(b"\x89PNG fake image data")
        file_url = f"file:///{img}"
        path = await svc.fetch_and_save_image(file_url)
        assert Path(path).exists()
        assert Path(path).read_bytes() == b"\x89PNG fake image data"

    @pytest.mark.asyncio
    async def test_file_url_not_found(self, svc, tmp_path):
        uploads = tmp_path / "uploads"
        uploads.mkdir(exist_ok=True)
        file_url = f"file:///{uploads}/nonexistent.png"
        with pytest.raises(FileNotFoundError, match="file not found"):
            await svc.fetch_and_save_image(file_url)

    @pytest.mark.asyncio
    async def test_file_url_path_traversal(self, svc, tmp_path):
        """file:/// URLs outside uploads dir are rejected."""
        outside = tmp_path / "secret.txt"
        outside.write_bytes(b"secret")
        with pytest.raises(ValueError, match="access denied"):
            await svc.fetch_and_save_image(f"file:///{outside}")

    @pytest.mark.asyncio
    async def test_file_url_windows_path_conversion(self, svc):
        """Windows-style file:/// URLs should be converted to WSL /mnt/ paths."""
        # file:///C:\Users\test\photo.png → /mnt/c/Users/test/photo.png
        # The converted path won't exist and is outside uploads dir,
        # so we expect ValueError (path traversal check).
        with pytest.raises(ValueError, match="access denied"):
            await svc.fetch_and_save_image("file:///C:\\Users\\test\\photo.png")

    @pytest.mark.asyncio
    async def test_http_url_success(self, svc):
        import aiohttp

        mock_resp_obj = AsyncMock()
        mock_resp_obj.status = 200
        mock_resp_obj.read = AsyncMock(return_value=b"image-bytes-123")
        mock_resp_obj.__aenter__ = AsyncMock(return_value=mock_resp_obj)
        mock_resp_obj.__aexit__ = AsyncMock(return_value=False)

        mock_session_obj = AsyncMock()
        mock_session_obj.get = MagicMock(return_value=mock_resp_obj)
        mock_session_obj.__aenter__ = AsyncMock(return_value=mock_session_obj)
        mock_session_obj.__aexit__ = AsyncMock(return_value=False)

        with patch.object(aiohttp, "ClientSession", return_value=mock_session_obj):
            path = await svc.fetch_and_save_image("https://example.com/photo.jpg")

        assert Path(path).exists()
        assert Path(path).read_bytes() == b"image-bytes-123"
        assert "photo.jpg" in Path(path).name

    @pytest.mark.asyncio
    async def test_http_url_non_200_raises(self, svc):
        import aiohttp

        mock_resp_obj = AsyncMock()
        mock_resp_obj.status = 404
        mock_resp_obj.__aenter__ = AsyncMock(return_value=mock_resp_obj)
        mock_resp_obj.__aexit__ = AsyncMock(return_value=False)

        mock_session_obj = AsyncMock()
        mock_session_obj.get = MagicMock(return_value=mock_resp_obj)
        mock_session_obj.__aenter__ = AsyncMock(return_value=mock_session_obj)
        mock_session_obj.__aexit__ = AsyncMock(return_value=False)

        with patch.object(aiohttp, "ClientSession", return_value=mock_session_obj):
            from swarm.server.daemon import SwarmOperationError

            with pytest.raises(SwarmOperationError, match="HTTP 404"):
                await svc.fetch_and_save_image("https://example.com/missing.png")

    @pytest.mark.asyncio
    async def test_http_url_empty_response_raises(self, svc):
        import aiohttp

        mock_resp_obj = AsyncMock()
        mock_resp_obj.status = 200
        mock_resp_obj.read = AsyncMock(return_value=b"")
        mock_resp_obj.__aenter__ = AsyncMock(return_value=mock_resp_obj)
        mock_resp_obj.__aexit__ = AsyncMock(return_value=False)

        mock_session_obj = AsyncMock()
        mock_session_obj.get = MagicMock(return_value=mock_resp_obj)
        mock_session_obj.__aenter__ = AsyncMock(return_value=mock_session_obj)
        mock_session_obj.__aexit__ = AsyncMock(return_value=False)

        with patch.object(aiohttp, "ClientSession", return_value=mock_session_obj):
            from swarm.server.daemon import SwarmOperationError

            with pytest.raises(SwarmOperationError, match="empty response"):
                await svc.fetch_and_save_image("https://example.com/empty.png")

    @pytest.mark.asyncio
    async def test_http_url_fallback_filename(self, svc):
        """When URL path ends with / or ?, filename defaults to image.png."""
        import aiohttp

        mock_resp_obj = AsyncMock()
        mock_resp_obj.status = 200
        mock_resp_obj.read = AsyncMock(return_value=b"img-data")
        mock_resp_obj.__aenter__ = AsyncMock(return_value=mock_resp_obj)
        mock_resp_obj.__aexit__ = AsyncMock(return_value=False)

        mock_session_obj = AsyncMock()
        mock_session_obj.get = MagicMock(return_value=mock_resp_obj)
        mock_session_obj.__aenter__ = AsyncMock(return_value=mock_session_obj)
        mock_session_obj.__aexit__ = AsyncMock(return_value=False)

        with patch.object(aiohttp, "ClientSession", return_value=mock_session_obj):
            path = await svc.fetch_and_save_image("https://example.com/")

        assert "image.png" in Path(path).name

    @pytest.mark.asyncio
    async def test_file_url_empty_content_raises(self, svc, tmp_path):
        """A file:// URL pointing to an empty file should raise."""
        uploads = tmp_path / "uploads"
        uploads.mkdir(exist_ok=True)
        empty_file = uploads / "empty.png"
        empty_file.write_bytes(b"")
        file_url = f"file:///{empty_file}"

        from swarm.server.daemon import SwarmOperationError

        with pytest.raises(SwarmOperationError, match="empty response"):
            await svc.fetch_and_save_image(file_url)


# ---------------------------------------------------------------------------
# process_email_data
# ---------------------------------------------------------------------------


class TestProcessEmailData:
    @pytest.mark.asyncio
    async def test_html_body_converted(self, svc):
        with patch("swarm.server.email_service.smart_title", new_callable=AsyncMock) as mock_title:
            mock_title.return_value = "Login Bug Report"
            result = await svc.process_email_data(
                subject="Bug: login broken",
                body_content="<p>The login page is <b>broken</b></p>",
                body_type="html",
                attachment_dicts=[],
                effective_id="msg-001",
            )

        assert result["title"] == "Login Bug Report"
        # Body now arrives as markdown — `<b>broken</b>` becomes `**broken**`.
        assert "The login page is **broken**" in result["description"]
        assert result["message_id"] == "msg-001"
        assert isinstance(result["attachments"], list)

    @pytest.mark.asyncio
    async def test_plain_body_preserved(self, svc):
        with patch("swarm.server.email_service.smart_title", new_callable=AsyncMock) as mock_title:
            mock_title.return_value = "Plain Email"
            result = await svc.process_email_data(
                subject="Hello",
                body_content="  Just plain text.  ",
                body_type="text",
                attachment_dicts=[],
                effective_id="msg-002",
            )

        assert "Just plain text." in result["description"]
        # Subject included in description
        assert "Subject: Hello" in result["description"]

    @pytest.mark.asyncio
    async def test_no_subject(self, svc):
        with patch("swarm.server.email_service.smart_title", new_callable=AsyncMock) as mock_title:
            mock_title.return_value = "Auto Title"
            result = await svc.process_email_data(
                subject="",
                body_content="Body only",
                body_type="text",
                attachment_dicts=[],
                effective_id="msg-003",
            )

        # When subject is empty, description should just be body_text
        assert result["description"] == "Body only"

    @pytest.mark.asyncio
    async def test_smart_title_fallback_to_subject(self, svc):
        with patch("swarm.server.email_service.smart_title", new_callable=AsyncMock) as mock_title:
            mock_title.return_value = ""  # smart_title returns empty
            result = await svc.process_email_data(
                subject="Fallback Subject",
                body_content="Some body",
                body_type="text",
                attachment_dicts=[],
                effective_id="msg-004",
            )

        # Title falls back to subject when smart_title returns empty/falsy
        assert result["title"] == "Fallback Subject"

    @pytest.mark.asyncio
    async def test_attachments_saved(self, svc, tmp_path):
        raw_data = b"file contents here"
        encoded = base64.b64encode(raw_data).decode()

        with patch("swarm.server.email_service.smart_title", new_callable=AsyncMock) as mock_title:
            mock_title.return_value = "With Attachment"
            result = await svc.process_email_data(
                subject="Test",
                body_content="Email body",
                body_type="text",
                attachment_dicts=[
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": "report.pdf",
                        "contentBytes": encoded,
                    }
                ],
                effective_id="msg-005",
            )

        assert len(result["attachments"]) == 1
        saved_path = Path(result["attachments"][0])
        assert saved_path.exists()
        assert saved_path.read_bytes() == raw_data

    @pytest.mark.asyncio
    async def test_non_file_attachments_skipped(self, svc):
        with patch("swarm.server.email_service.smart_title", new_callable=AsyncMock) as mock_title:
            mock_title.return_value = "No Attachments"
            result = await svc.process_email_data(
                subject="Test",
                body_content="Body",
                body_type="text",
                attachment_dicts=[
                    {
                        "@odata.type": "#microsoft.graph.referenceAttachment",
                        "name": "link.url",
                    }
                ],
                effective_id="msg-006",
            )

        assert result["attachments"] == []

    @pytest.mark.asyncio
    async def test_empty_content_bytes_skipped(self, svc):
        with patch("swarm.server.email_service.smart_title", new_callable=AsyncMock) as mock_title:
            mock_title.return_value = "Empty Attach"
            result = await svc.process_email_data(
                subject="Test",
                body_content="Body",
                body_type="text",
                attachment_dicts=[
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": "empty.txt",
                        "contentBytes": "",
                    }
                ],
                effective_id="msg-007",
            )

        assert result["attachments"] == []

    @pytest.mark.asyncio
    async def test_inline_cid_refs_rewritten_to_uploads(self, svc):
        """Outlook inline images come in as ``<img src="cid:abc">`` in the body
        and as separate attachment objects with ``contentId="abc"`` and
        ``isInline=true``. The body's ``cid:`` ref must end up rewritten to
        the saved ``/uploads/<basename>`` path so the rendered preview shows
        the image inline instead of a broken reference."""
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\0" * 100
        b64 = base64.b64encode(png_bytes).decode()
        with patch("swarm.server.email_service.smart_title", new_callable=AsyncMock) as mt:
            mt.return_value = "Email with inline image"
            result = await svc.process_email_data(
                subject="Quote",
                body_content=('<p>See logo:</p><p><img alt="logo" src="cid:abc-123-def"></p>'),
                body_type="html",
                attachment_dicts=[
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": "logo.png",
                        "contentBytes": b64,
                        "contentId": "abc-123-def",
                        "isInline": True,
                    }
                ],
                effective_id="msg-cid-1",
            )

        assert "cid:abc-123-def" not in result["description"], (
            "raw cid: ref leaked into description"
        )
        # The rewritten ref should point at /uploads/<hash>_logo.png.
        assert "/uploads/" in result["description"]
        assert "logo.png" in result["description"]
        # And the file should still be in the attachments list.
        assert result["attachments"]
        assert result["attachments"][0].endswith("logo.png")

    @pytest.mark.asyncio
    async def test_task_type_classified(self, svc):
        with patch("swarm.server.email_service.smart_title", new_callable=AsyncMock) as mock_title:
            mock_title.return_value = "Fix login bug"
            with patch(
                "swarm.server.email_service.auto_classify_type",
                return_value=TaskType.BUG,
            ):
                result = await svc.process_email_data(
                    subject="Bug report",
                    body_content="Login is broken",
                    body_type="text",
                    attachment_dicts=[],
                    effective_id="msg-008",
                )

        assert result["task_type"] == "bug"

    @pytest.mark.asyncio
    async def test_html_body_type_case_insensitive(self, svc):
        with patch("swarm.server.email_service.smart_title", new_callable=AsyncMock) as mock_title:
            mock_title.return_value = "Title"
            result = await svc.process_email_data(
                subject="Test",
                body_content="<p>Hello</p>",
                body_type="HTML",
                attachment_dicts=[],
                effective_id="msg-009",
            )

        # HTML tags should be stripped
        assert "<p>" not in result["description"]
        assert "Hello" in result["description"]


# ---------------------------------------------------------------------------
# _format_reply_html — Aptos 12pt wrapper for Outlook reply bodies
# ---------------------------------------------------------------------------


class TestFormatReplyHtml:
    """The reply body that goes into the Outlook draft must render as
    Aptos 12pt so it matches the user's normal Outlook composition
    (Aptos is the Office 365 default body font, with Calibri as the
    legacy fallback). The wrapper uses an inline style because Outlook's
    Word-based renderer drops ``<style>`` blocks in mail bodies.
    """

    def test_wraps_with_aptos_and_12pt(self):
        from swarm.server.email_service import _format_reply_html

        out = _format_reply_html("Hello there")
        assert "Aptos" in out
        assert "12pt" in out
        assert "Hello there" in out
        # Style is INLINE on a block element so Outlook honours it.
        assert out.startswith('<div style="')
        assert out.endswith("</div>")

    def test_falls_back_through_calibri_then_sans_serif(self):
        """Older mail clients without Aptos should still hit a sans-serif."""
        from swarm.server.email_service import _format_reply_html

        out = _format_reply_html("body")
        assert "Calibri" in out
        assert "sans-serif" in out

    def test_html_escapes_dangerous_characters(self):
        """Plain-text input must be escaped so users can't smuggle markup."""
        from swarm.server.email_service import _format_reply_html

        out = _format_reply_html("<script>alert(1)</script> & co.")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out
        assert "&amp;" in out

    def test_newlines_become_br_tags(self):
        """Multi-paragraph drafts keep their shape via <br> in HTML."""
        from swarm.server.email_service import _format_reply_html

        out = _format_reply_html("Line one\n\nLine two")
        assert out.count("<br>") == 2

    def test_empty_input_returns_empty_string(self):
        """No wrapper for empty input — no stray empty <div> in the draft."""
        from swarm.server.email_service import _format_reply_html

        assert _format_reply_html("") == ""
        assert _format_reply_html("   ") == ""


# ---------------------------------------------------------------------------
# send_completion_reply
# ---------------------------------------------------------------------------


class TestSendCompletionReply:
    @pytest.mark.asyncio
    async def test_success_with_graph_id(self, svc, queen, graph_mgr, broadcast_ws, drone_log):
        """Direct Graph ID (no RFC 822 resolution needed)."""
        await svc.send_completion_reply(
            message_id="AAMkAGI2...",
            task_title="Fix login",
            task_type="bug",
            resolution="Patched the auth module.",
            task_id="t-001",
        )

        queen.draft_email_reply.assert_awaited_once_with(
            "Fix login", "bug", "Patched the auth module."
        )
        graph_mgr.create_reply_draft.assert_awaited_once()
        sent_id, sent_html = graph_mgr.create_reply_draft.call_args[0]
        assert sent_id == "AAMkAGI2..."
        # Reply body wrapped in Aptos 12pt HTML for Outlook.
        assert "Thank you, this has been resolved." in sent_html
        assert "Aptos" in sent_html
        assert "12pt" in sent_html
        assert sent_html.startswith("<div style=") and sent_html.endswith("</div>")
        broadcast_ws.assert_called_once()
        msg = broadcast_ws.call_args[0][0]
        assert msg["type"] == "draft_reply_ok"
        assert msg["task_title"] == "Fix login"
        # DroneLog should have DRAFT_OK entry
        assert any(e.action == SystemAction.DRAFT_OK for e in drone_log.entries)

    @pytest.mark.asyncio
    async def test_rfc822_id_resolved(self, svc, queen, graph_mgr, broadcast_ws):
        """RFC 822 Message-ID (<...@...>) should be resolved via graph_mgr."""
        await svc.send_completion_reply(
            message_id="<abc123@mail.example.com>",
            task_title="Deploy fix",
            task_type="chore",
            resolution="Deployed v2.1.",
        )

        graph_mgr.resolve_message_id.assert_awaited_once_with("<abc123@mail.example.com>")
        # Should use the resolved Graph ID and the Aptos-12pt wrapper.
        graph_mgr.create_reply_draft.assert_awaited_once()
        sent_id, sent_html = graph_mgr.create_reply_draft.call_args[0]
        assert sent_id == "GRAPH-MSG-ID-123"
        assert "Thank you, this has been resolved." in sent_html
        assert "Aptos" in sent_html and "12pt" in sent_html

    @pytest.mark.asyncio
    async def test_rfc822_id_unresolved(self, svc, queen, graph_mgr, broadcast_ws, drone_log):
        """When RFC 822 ID cannot be resolved, should notify failure and return."""
        graph_mgr.resolve_message_id = AsyncMock(return_value=None)

        await svc.send_completion_reply(
            message_id="<unknown@nowhere.com>",
            task_title="Unknown task",
            task_type="chore",
            resolution="Done.",
            task_id="t-002",
        )

        # Should NOT attempt to create a draft
        graph_mgr.create_reply_draft.assert_not_awaited()
        # Should notify failure via broadcast
        broadcast_ws.assert_called_once()
        msg = broadcast_ws.call_args[0][0]
        assert msg["type"] == "draft_reply_failed"
        assert "Unknown task" in msg["task_title"]
        # DroneLog should have DRAFT_FAILED entry
        assert any(e.action == SystemAction.DRAFT_FAILED for e in drone_log.entries)

    @pytest.mark.asyncio
    async def test_graph_api_returns_false(self, svc, queen, graph_mgr, broadcast_ws, drone_log):
        """When create_reply_draft returns False, should notify failure."""
        graph_mgr.create_reply_draft = AsyncMock(return_value=False)

        await svc.send_completion_reply(
            message_id="GRAPH-ID-456",
            task_title="Failed draft",
            task_type="feature",
            resolution="Implemented feature X.",
            task_id="t-003",
        )

        broadcast_ws.assert_called_once()
        msg = broadcast_ws.call_args[0][0]
        assert msg["type"] == "draft_reply_failed"
        assert "Graph API returned failure" in msg["error"]
        assert any(e.action == SystemAction.DRAFT_FAILED for e in drone_log.entries)

    @pytest.mark.asyncio
    async def test_exception_caught_and_notified(
        self, svc, queen, graph_mgr, broadcast_ws, drone_log
    ):
        """Unexpected exceptions should be caught and reported, not raised."""
        queen.draft_email_reply = AsyncMock(side_effect=RuntimeError("Claude down"))

        # Should NOT raise
        await svc.send_completion_reply(
            message_id="GRAPH-ID-789",
            task_title="Error task",
            task_type="bug",
            resolution="Fixed.",
            task_id="t-004",
        )

        broadcast_ws.assert_called_once()
        msg = broadcast_ws.call_args[0][0]
        assert msg["type"] == "draft_reply_failed"
        assert "Claude down" in msg["error"]
        assert any(e.action == SystemAction.DRAFT_FAILED for e in drone_log.entries)

    @pytest.mark.asyncio
    async def test_exception_error_truncated(self, svc, queen, broadcast_ws):
        """Long exception messages should be truncated to 200 chars."""
        long_msg = "X" * 500
        queen.draft_email_reply = AsyncMock(side_effect=RuntimeError(long_msg))

        await svc.send_completion_reply(
            message_id="GRAPH-ID-000",
            task_title="Truncation test",
            task_type="chore",
            resolution="Done.",
            task_id="t-005",
        )

        msg = broadcast_ws.call_args[0][0]
        assert len(msg["error"]) <= 200


# ---------------------------------------------------------------------------
# _notify_draft_failed
# ---------------------------------------------------------------------------


class TestNotifyDraftFailed:
    def test_broadcasts_and_logs(self, svc, broadcast_ws, drone_log):
        svc._notify_draft_failed("Task A", "t-100", "some error")

        broadcast_ws.assert_called_once()
        msg = broadcast_ws.call_args[0][0]
        assert msg["type"] == "draft_reply_failed"
        assert msg["task_title"] == "Task A"
        assert msg["task_id"] == "t-100"
        assert msg["error"] == "some error"

        assert len(drone_log.entries) == 1
        entry = drone_log.entries[0]
        assert entry.action == SystemAction.DRAFT_FAILED
        assert entry.is_notification is True
        assert entry.category == LogCategory.SYSTEM

    def test_empty_error(self, svc, broadcast_ws, drone_log):
        svc._notify_draft_failed("Long Title " * 10, "t-101", "")

        entry = drone_log.entries[0]
        # detail should be truncated title (no error appended)
        assert len(entry.detail) <= 80

    def test_detail_truncation(self, svc, broadcast_ws, drone_log):
        long_title = "A" * 100
        long_error = "B" * 200
        svc._notify_draft_failed(long_title, "t-102", long_error)

        entry = drone_log.entries[0]
        # title[:60] + ": " + error[:80]
        assert len(entry.detail) <= 60 + 2 + 80


# ---------------------------------------------------------------------------
# EmailService __init__ defaults
# ---------------------------------------------------------------------------


class TestEmailServiceInit:
    def test_default_uploads_dir(self, drone_log, queen, graph_mgr, broadcast_ws):
        svc = EmailService(
            drone_log=drone_log,
            queen=queen,
            graph_mgr=graph_mgr,
            broadcast_ws=broadcast_ws,
        )
        # Resolved through state_dir(), so this holds before and after
        # `swarm relocate` instead of only on an un-relocated box.
        assert svc._uploads_dir == (state_dir() / "uploads").resolve()

    def test_custom_uploads_dir(self, tmp_path, drone_log, queen, graph_mgr, broadcast_ws):
        custom = tmp_path / "custom_uploads"
        svc = EmailService(
            drone_log=drone_log,
            queen=queen,
            graph_mgr=graph_mgr,
            broadcast_ws=broadcast_ws,
            uploads_dir=custom,
        )
        assert svc._uploads_dir == custom.resolve()
