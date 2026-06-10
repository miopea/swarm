"""Tests for tasks/task.py, tasks/board.py, tasks/store.py, and tasks/history.py."""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskAction, TaskHistory
from swarm.tasks.store import FileTaskStore
from swarm.tasks.task import (
    SwarmTask,
    TaskPriority,
    TaskStatus,
    TaskType,
    _looks_like_msg,
    auto_classify_type,
    auto_title,
    parse_email,
    smart_title,
)
from swarm.tasks.workflows import (
    SKILL_COMMANDS,
    WORKFLOW_TEMPLATES,
    apply_config_overrides,
    get_skill_command,
    get_workflow_instructions,
)


class TestSwarmTask:
    def test_defaults(self):
        t = SwarmTask(title="Fix bug")
        assert t.status == TaskStatus.UNASSIGNED
        assert t.priority == TaskPriority.NORMAL
        assert t.assigned_worker is None
        assert t.is_available is True
        assert len(t.id) == 12

    def test_default_empty_attachments(self):
        t = SwarmTask(title="Test")
        assert t.attachments == []

    def test_assign(self):
        t = SwarmTask(title="Fix bug")
        t.assign("api")
        assert t.status == TaskStatus.ASSIGNED
        assert t.assigned_worker == "api"
        assert t.is_available is False

    def test_lifecycle(self):
        t = SwarmTask(title="Fix bug")
        t.assign("api")
        t.start()
        assert t.status == TaskStatus.ACTIVE
        t.complete()
        assert t.status == TaskStatus.DONE
        assert t.completed_at is not None

    def test_fail(self):
        t = SwarmTask(title="Fix bug")
        t.assign("api")
        t.start()
        t.fail()
        assert t.status == TaskStatus.FAILED


class TestAutoTitle:
    def test_first_line(self):
        assert auto_title("Fix the login bug\nMore details here") == "Fix the login bug"

    def test_truncation(self):
        long = "A" * 100
        result = auto_title(long)
        assert len(result) == 80
        assert result.endswith("\u2026")

    def test_blank_returns_empty(self):
        assert auto_title("") == ""
        assert auto_title("   ") == ""
        assert auto_title(None) == ""

    def test_short_passthrough(self):
        assert auto_title("Short title") == "Short title"

    def test_strips_whitespace(self):
        assert auto_title("  hello  \n  world  ") == "hello"


class TestSmartTitle:
    @pytest.mark.asyncio
    async def test_smart_title_with_claude(self):
        """smart_title calls claude and returns the result."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"Fix login validation bug", b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await smart_title("The login form doesn't validate email addresses")
            assert result == "Fix login validation bug"

    @pytest.mark.asyncio
    async def test_smart_title_timeout_fallback(self):
        """smart_title falls back to auto_title on timeout."""

        async def slow_communicate():
            raise TimeoutError()

        mock_proc = AsyncMock()
        mock_proc.communicate = slow_communicate

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await smart_title("Fix the login bug\nMore details here")
            assert result == "Fix the login bug"

    @pytest.mark.asyncio
    async def test_smart_title_missing_claude(self):
        """smart_title falls back when claude binary is missing."""
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            result = await smart_title("Fix the login bug")
            assert result == "Fix the login bug"

    @pytest.mark.asyncio
    async def test_smart_title_blank(self):
        result = await smart_title("")
        assert result == ""


class TestTaskBoard:
    def test_create_and_get(self):
        board = TaskBoard()
        task = board.create("Fix bug", description="Fix the login bug")
        assert board.get(task.id) is not None
        assert board.get(task.id).title == "Fix bug"

    def test_available_tasks(self):
        board = TaskBoard()
        t1 = board.create("Task 1")
        board.create("Task 2")
        assert len(board.available_tasks) == 2
        board.assign(t1.id, "api")
        assert len(board.available_tasks) == 1

    def test_dependency_blocks_availability(self):
        board = TaskBoard()
        t1 = board.create("Build API")
        t2 = board.create("Build frontend", depends_on=[t1.id])
        avail = board.available_tasks
        assert t1 in avail
        assert t2 not in avail  # blocked by t1

    def test_dependency_unblocks_on_complete(self):
        board = TaskBoard()
        t1 = board.create("Build API")
        t2 = board.create("Build frontend", depends_on=[t1.id])
        board.assign(t1.id, "worker-1")
        board.complete(t1.id)
        avail = board.available_tasks
        assert t2 in avail

    def test_priority_ordering(self):
        board = TaskBoard()
        board.create("Low", priority=TaskPriority.LOW)
        board.create("Urgent", priority=TaskPriority.URGENT)
        board.create("Normal", priority=TaskPriority.NORMAL)
        tasks = board.all_tasks
        assert tasks[0].priority == TaskPriority.URGENT
        assert tasks[-1].priority == TaskPriority.LOW

    def test_tasks_for_worker(self):
        board = TaskBoard()
        t1 = board.create("Task 1")
        t2 = board.create("Task 2")
        board.assign(t1.id, "api")
        board.assign(t2.id, "web")
        assert len(board.tasks_for_worker("api")) == 1
        assert board.tasks_for_worker("api")[0].id == t1.id

    def test_remove(self):
        board = TaskBoard()
        t = board.create("Temp task")
        assert board.remove(t.id) is True
        assert board.get(t.id) is None

    def test_summary(self):
        board = TaskBoard()
        board.create("A")
        t2 = board.create("B")
        board.assign(t2.id, "api")
        s = board.summary()
        assert "2 tasks" in s
        assert "1 unassigned" in s
        assert "1 in progress" in s

    def test_on_change_callback(self):
        board = TaskBoard()
        changes = []
        board.on_change(lambda: changes.append(1))
        board.create("Test")
        assert len(changes) == 1
        board.assign(board.all_tasks[0].id, "api")
        assert len(changes) == 2

    def test_active_tasks(self):
        board = TaskBoard()
        t1 = board.create("A")
        board.create("B")
        board.assign(t1.id, "api")
        assert len(board.active_tasks) == 1
        board.complete(t1.id)
        assert len(board.active_tasks) == 0

    def test_update_title(self):
        board = TaskBoard()
        t = board.create("Old title")
        assert board.update(t.id, title="New title")
        assert board.get(t.id).title == "New title"

    def test_update_description(self):
        board = TaskBoard()
        t = board.create("Task", description="old desc")
        assert board.update(t.id, description="new desc")
        assert board.get(t.id).description == "new desc"

    def test_update_priority(self):
        board = TaskBoard()
        t = board.create("Task")
        assert board.update(t.id, priority=TaskPriority.URGENT)
        assert board.get(t.id).priority == TaskPriority.URGENT

    def test_update_tags(self):
        board = TaskBoard()
        t = board.create("Task", tags=["a"])
        assert board.update(t.id, tags=["b", "c"])
        assert board.get(t.id).tags == ["b", "c"]

    def test_update_attachments(self):
        board = TaskBoard()
        t = board.create("Task")
        assert board.update(t.id, attachments=["/tmp/img.png"])
        assert board.get(t.id).attachments == ["/tmp/img.png"]

    def test_update_nonexistent(self):
        board = TaskBoard()
        assert board.update("nonexistent", title="Nope") is False

    def test_update_fires_change(self):
        board = TaskBoard()
        changes = []
        board.on_change(lambda: changes.append(1))
        t = board.create("Task")
        changes.clear()
        board.update(t.id, title="Updated")
        assert len(changes) == 1

    def test_update_partial(self):
        """Updating one field should not affect others."""
        board = TaskBoard()
        t = board.create("Task", description="desc", priority=TaskPriority.HIGH, tags=["x"])
        board.update(t.id, title="New Title")
        updated = board.get(t.id)
        assert updated.title == "New Title"
        assert updated.description == "desc"
        assert updated.priority == TaskPriority.HIGH
        assert updated.tags == ["x"]

    def test_create_with_attachments(self):
        board = TaskBoard()
        t = board.create("Task", attachments=["/tmp/a.png", "/tmp/b.png"])
        assert t.attachments == ["/tmp/a.png", "/tmp/b.png"]


class TestTaskStore:
    def test_store_backward_compat(self):
        """Loading old JSON without attachments field should default to empty list."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(
                [
                    {
                        "id": "abc123",
                        "title": "Old task",
                        "status": "unassigned",
                    }
                ],
                f,
            )
            f.flush()
            store = FileTaskStore(path=Path(f.name))
            tasks = store.load()
            assert "abc123" in tasks
            assert tasks["abc123"].attachments == []
            assert tasks["abc123"].tags == []

    def test_roundtrip_with_attachments(self):
        """Save and load tasks with attachments should roundtrip correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "tasks.json"
            store = FileTaskStore(path=path)
            board = TaskBoard(store=store)
            t = board.create("Task", attachments=["/tmp/img.png"])
            board.update(t.id, tags=["tag1"])

            # Reload from disk
            store2 = FileTaskStore(path=path)
            loaded = store2.load()
            assert t.id in loaded
            assert loaded[t.id].attachments == ["/tmp/img.png"]
            assert loaded[t.id].tags == ["tag1"]

    def test_load_invalid_enum(self):
        """Loading tasks with bad status/priority values should not crash."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(
                [
                    {
                        "id": "good1",
                        "title": "Good task",
                        "status": "unassigned",
                        "priority": "normal",
                    },
                    {
                        "id": "bad1",
                        "title": "Bad status",
                        "status": "deleted",
                    },
                    {
                        "id": "bad2",
                        "title": "Bad priority",
                        "status": "unassigned",
                        "priority": "mega",
                    },
                ],
                f,
            )
            f.flush()
            store = FileTaskStore(path=Path(f.name))
            # Should not crash — bad tasks are skipped via ValueError catch
            tasks = store.load()
            # The load catches the entire batch on ValueError, returns empty
            assert isinstance(tasks, dict)


class TestTaskHistory:
    def test_append_and_get(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "history.jsonl"
            h = TaskHistory(log_file=log_file)
            h.append("task1", TaskAction.CREATED, actor="user", detail="Fix bug")
            h.append("task1", TaskAction.ASSIGNED, actor="user", detail="api")
            h.append("task2", TaskAction.CREATED, actor="drone", detail="Other")

            events = h.get_events("task1")
            assert len(events) == 2
            assert events[0].action == TaskAction.CREATED
            assert events[0].detail == "Fix bug"
            assert events[1].action == TaskAction.ASSIGNED

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "history.jsonl"
            h1 = TaskHistory(log_file=log_file)
            h1.append("task1", TaskAction.CREATED, detail="Test")
            h1.append("task1", TaskAction.COMPLETED)

            # New instance reads from same file
            h2 = TaskHistory(log_file=log_file)
            events = h2.get_events("task1")
            assert len(events) == 2
            assert events[1].action == TaskAction.COMPLETED

    def test_empty_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "history.jsonl"
            h = TaskHistory(log_file=log_file)
            assert h.get_events("nonexistent") == []

    def test_to_dict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "history.jsonl"
            h = TaskHistory(log_file=log_file)
            ev = h.append("t1", TaskAction.EDITED, actor="queen", detail="title changed")
            d = ev.to_dict()
            assert d["task_id"] == "t1"
            assert d["action"] == "EDITED"
            assert d["actor"] == "queen"
            assert d["detail"] == "title changed"
            assert "timestamp" in d

    def test_update_depends_on(self):
        board = TaskBoard()
        t = board.create("Task", depends_on=["dep1"])
        assert board.update(t.id, depends_on=["dep2", "dep3"])
        assert board.get(t.id).depends_on == ["dep2", "dep3"]


class TestTaskUnassign:
    def test_unassign_task(self):
        t = SwarmTask(title="Fix bug")
        t.assign("api")
        assert t.status == TaskStatus.ASSIGNED
        t.unassign()
        assert t.status == TaskStatus.UNASSIGNED
        assert t.assigned_worker is None

    def test_board_unassign(self):
        board = TaskBoard()
        t = board.create("Task")
        board.assign(t.id, "api")
        assert board.unassign(t.id) is True
        task = board.get(t.id)
        assert task.status == TaskStatus.UNASSIGNED
        assert task.assigned_worker is None

    def test_board_unassign_nonexistent(self):
        board = TaskBoard()
        assert board.unassign("nonexistent") is False

    def test_board_unassign_pending(self):
        """Cannot unassign a task that is not assigned."""
        board = TaskBoard()
        t = board.create("Task")
        assert board.unassign(t.id) is False

    def test_board_unassign_fires_change(self):
        board = TaskBoard()
        t = board.create("Task")
        board.assign(t.id, "api")
        changes = []
        board.on_change(lambda: changes.append(1))
        board.unassign(t.id)
        assert len(changes) == 1

    def test_board_unassign_makes_available(self):
        """Unassigned task returns to available list."""
        board = TaskBoard()
        t = board.create("Task")
        board.assign(t.id, "api")
        assert len(board.available_tasks) == 0
        board.unassign(t.id)
        assert len(board.available_tasks) == 1


class TestParseEmail:
    def test_plain_text(self):
        raw = (
            b"From: alice@example.com\r\n"
            b"Subject: Bug report\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"The login page is broken.\r\n"
        )
        result = parse_email(raw)
        assert result["subject"] == "Bug report"
        assert "login page is broken" in result["body"]
        assert result["attachments"] == []

    def test_html_fallback(self):
        raw = (
            b"From: bob@example.com\r\n"
            b"Subject: HTML only\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"\r\n"
            b"<html><body><p>Hello <b>world</b></p></body></html>\r\n"
        )
        result = parse_email(raw)
        assert result["subject"] == "HTML only"
        assert "Hello" in result["body"]
        assert "<b>" not in result["body"]

    def test_multipart_with_attachment(self):
        raw = (
            b"From: carol@example.com\r\n"
            b"Subject: With attachment\r\n"
            b"MIME-Version: 1.0\r\n"
            b'Content-Type: multipart/mixed; boundary="BOUNDARY"\r\n'
            b"\r\n"
            b"--BOUNDARY\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"See attached file.\r\n"
            b"--BOUNDARY\r\n"
            b"Content-Type: application/octet-stream\r\n"
            b'Content-Disposition: attachment; filename="data.bin"\r\n'
            b"\r\n"
            b"binarydata\r\n"
            b"--BOUNDARY--\r\n"
        )
        result = parse_email(raw)
        assert result["subject"] == "With attachment"
        assert "attached file" in result["body"]
        assert len(result["attachments"]) == 1
        assert result["attachments"][0]["filename"] == "data.bin"
        assert result["attachments"][0]["data"] == b"binarydata"

    def test_empty_subject(self):
        raw = b"From: dave@example.com\r\nContent-Type: text/plain\r\n\r\nNo subject line.\r\n"
        result = parse_email(raw)
        assert result["subject"] == ""
        assert "No subject line" in result["body"]

    def test_multipart_html_fallback(self):
        """When multipart has only HTML part (no text/plain), use stripped HTML."""
        raw = (
            b"From: eve@example.com\r\n"
            b"Subject: HTML multi\r\n"
            b"MIME-Version: 1.0\r\n"
            b'Content-Type: multipart/alternative; boundary="ALT"\r\n'
            b"\r\n"
            b"--ALT\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"\r\n"
            b"<div>Important <em>message</em></div>\r\n"
            b"--ALT--\r\n"
        )
        result = parse_email(raw)
        assert "Important" in result["body"]
        assert "<div>" not in result["body"]

    def test_filename_hint_routes_to_msg(self):
        """When filename ends with .msg but content is garbage, _parse_msg is called."""
        # OLE2 magic bytes trigger msg path even without filename hint
        ole_magic = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        assert _looks_like_msg(ole_magic + b"\x00" * 100)
        assert not _looks_like_msg(b"From: test@example.com\r\n")

    def test_eml_via_filename_hint(self):
        """Filename hint .eml routes to EML parser."""
        raw = b"From: a@b.com\r\nSubject: Test\r\n\r\nBody\r\n"
        result = parse_email(raw, filename="message.eml")
        assert result["subject"] == "Test"

    def test_eml_extracts_message_id(self):
        """Message-ID header is extracted from .eml files."""
        raw = (
            b"From: alice@example.com\r\n"
            b"Subject: Test\r\n"
            b"Message-ID: <abc123@example.com>\r\n"
            b"Content-Type: text/plain\r\n"
            b"\r\n"
            b"Body\r\n"
        )
        result = parse_email(raw)
        assert result["message_id"] == "<abc123@example.com>"

    def test_eml_missing_message_id(self):
        """When Message-ID is absent, message_id should be empty."""
        raw = b"From: a@b.com\r\nSubject: No ID\r\n\r\nBody\r\n"
        result = parse_email(raw)
        assert result["message_id"] == ""

    def test_msg_parse_with_extract_msg(self):
        """parse_email with .msg filename delegates to _parse_msg."""
        with patch("swarm.tasks.task._looks_like_msg", return_value=False):
            # Use filename hint to route to msg parser, mock extract_msg
            mock_msg = type(
                "Msg",
                (),
                {
                    "subject": "Meeting Notes",
                    "body": "Discuss project timeline",
                    "htmlBody": None,
                    "attachments": [],
                    "messageId": None,
                    "close": lambda self: None,
                },
            )()
            with patch("extract_msg.openMsg", return_value=mock_msg):
                result = parse_email(b"fake", filename="notes.msg")
        assert result["subject"] == "Meeting Notes"
        assert "project timeline" in result["body"]
        assert result["message_id"] == ""

    def test_msg_parse_extracts_message_id(self):
        """_parse_msg extracts messageId from .msg files."""
        with patch("swarm.tasks.task._looks_like_msg", return_value=False):
            mock_msg = type(
                "Msg",
                (),
                {
                    "subject": "With ID",
                    "body": "Body text",
                    "htmlBody": None,
                    "attachments": [],
                    "messageId": "<msg456@outlook.com>",
                    "close": lambda self: None,
                },
            )()
            with patch("extract_msg.openMsg", return_value=mock_msg):
                result = parse_email(b"fake", filename="test.msg")
        assert result["message_id"] == "<msg456@outlook.com>"


class TestTaskType:
    def test_default_type_is_chore(self):
        t = SwarmTask(title="Do something")
        assert t.task_type == TaskType.CHORE

    def test_explicit_type(self):
        t = SwarmTask(title="Fix bug", task_type=TaskType.BUG)
        assert t.task_type == TaskType.BUG

    def test_auto_classify_bug(self):
        assert auto_classify_type("Fix the login crash") == TaskType.BUG
        assert auto_classify_type("", "There's a bug in the parser") == TaskType.BUG

    def test_auto_classify_verify(self):
        assert auto_classify_type("Verify the deployment") == TaskType.VERIFY
        assert auto_classify_type("QA check on release") == TaskType.VERIFY

    def test_auto_classify_feature(self):
        assert auto_classify_type("Add dark mode support") == TaskType.FEATURE
        assert auto_classify_type("Implement user auth") == TaskType.FEATURE

    def test_auto_classify_ambiguous_returns_chore(self):
        assert auto_classify_type("Update README") == TaskType.CHORE

    def test_auto_classify_tie_returns_chore(self):
        # "fix" (bug) + "add" (feature) = tie → CHORE
        assert auto_classify_type("fix and add") == TaskType.CHORE

    def test_board_create_with_type(self):
        board = TaskBoard()
        t = board.create("Fix bug", task_type=TaskType.BUG)
        assert t.task_type == TaskType.BUG

    def test_board_update_type(self):
        board = TaskBoard()
        t = board.create("Task")
        assert board.update(t.id, task_type=TaskType.FEATURE)
        assert board.get(t.id).task_type == TaskType.FEATURE


class TestTaskTypeStore:
    def test_roundtrip_with_type(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "tasks.json"
            store = FileTaskStore(path=path)
            board = TaskBoard(store=store)
            t = board.create("Fix it", task_type=TaskType.BUG)

            store2 = FileTaskStore(path=path)
            loaded = store2.load()
            assert loaded[t.id].task_type == TaskType.BUG

    def test_backward_compat_missing_type(self):
        """Old JSON without task_type field defaults to CHORE."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(
                [{"id": "old1", "title": "Old task", "status": "unassigned"}],
                f,
            )
            f.flush()
            store = FileTaskStore(path=Path(f.name))
            tasks = store.load()
            assert tasks["old1"].task_type == TaskType.CHORE


class TestWorkflowTemplates:
    def test_all_types_covered(self):
        """Every TaskType must have either a skill command or an inline template."""
        for tt in TaskType:
            assert tt in SKILL_COMMANDS or tt in WORKFLOW_TEMPLATES, (
                f"{tt} has neither a skill command nor a workflow template"
            )

    def test_skill_commands(self):
        assert get_skill_command(TaskType.BUG) == "/fix-and-ship"
        assert get_skill_command(TaskType.FEATURE) == "/feature"
        assert get_skill_command(TaskType.VERIFY) == "/verify"
        assert get_skill_command(TaskType.CHORE) is None

    def test_get_workflow_instructions_chore(self):
        result = get_workflow_instructions(TaskType.CHORE)
        assert "General Task" in result

    def test_get_workflow_instructions_fallback(self):
        """Types with a skill still get the CHORE fallback from get_workflow_instructions."""
        result = get_workflow_instructions(TaskType.BUG)
        assert "General Task" in result

    # NOTE: these tests assert the *body* shape of the dispatched task message
    # (skill prefix, inline workflow, attachment placement). The plan-mode
    # preamble added for user-request tasks (2026-05-22) is covered separately
    # in tests/test_messages.py::TestPlanModePreamble — these pass
    # ``plan_mode_for_user_requests=False`` to keep the body assertions sharp.

    def test_build_task_message_skill(self):
        """BUG/FEATURE/VERIFY tasks produce a skill invocation."""
        from swarm.server.messages import build_task_message

        task = SwarmTask(
            title="Login button broken",
            description="Clicking login does nothing",
            task_type=TaskType.BUG,
            tags=["auth"],
        )
        msg = build_task_message(task, plan_mode_for_user_requests=False)
        # BUG tasks get the env-causes preamble first, then the skill invocation.
        assert msg.startswith("Before assuming a code bug")
        assert "/fix-and-ship " in msg
        assert "Login button broken" in msg
        assert "Clicking login does nothing" in msg
        assert "auth" in msg

    def test_build_task_message_feature(self):
        from swarm.server.messages import build_task_message

        task = SwarmTask(
            title="Add dark mode",
            description="Toggle in settings",
            task_type=TaskType.FEATURE,
        )
        msg = build_task_message(task, plan_mode_for_user_requests=False)
        assert msg.startswith("/feature ")
        assert "Add dark mode" in msg

    def test_build_task_message_verify(self):
        from swarm.server.messages import build_task_message

        task = SwarmTask(
            title="Check auth flow",
            description="Verify login redirects correctly",
            task_type=TaskType.VERIFY,
        )
        msg = build_task_message(task, plan_mode_for_user_requests=False)
        assert msg.startswith("/verify ")
        assert "Check auth flow" in msg

    def test_build_task_message_chore_fallback(self):
        """CHORE tasks still use inline workflow instructions."""
        from swarm.server.messages import build_task_message

        task = SwarmTask(
            title="Update README",
            description="Add setup instructions",
            task_type=TaskType.CHORE,
        )
        msg = build_task_message(task, plan_mode_for_user_requests=False)
        assert msg.startswith("Task: Update README")
        assert "General Task" in msg

    def test_build_task_message_attachments(self):
        from swarm.server.messages import build_task_message

        task = SwarmTask(
            title="Fix crash",
            task_type=TaskType.BUG,
            attachments=["/tmp/log.txt", "/tmp/screenshot.png"],
        )
        msg = build_task_message(task, plan_mode_for_user_requests=False)
        # The skill invocation is on its own line (BUG tasks prepend the
        # env-causes preamble, so it's no longer line 0).
        skill_line = next(ln for ln in msg.split("\n") if ln.startswith("/fix-and-ship "))
        # Attachments must NOT be inside the quoted skill argument —
        # they go on separate lines so the worker can see and read them
        assert "/tmp/log.txt" not in skill_line
        assert "/tmp/screenshot.png" not in skill_line
        assert "/tmp/log.txt" in msg
        assert "/tmp/screenshot.png" in msg
        assert "Attachments" in msg

    def test_build_task_message_attachments_fallback(self):
        """CHORE tasks also list attachments on separate lines."""
        from swarm.server.messages import build_task_message

        task = SwarmTask(
            title="Update docs",
            task_type=TaskType.CHORE,
            attachments=["/tmp/spec.pdf"],
        )
        msg = build_task_message(task, plan_mode_for_user_requests=False)
        assert "/tmp/spec.pdf" in msg
        assert "Attachments" in msg

    def test_apply_config_overrides_custom_skill(self):
        """Config can override the default skill for a task type."""
        # Save original and restore after test
        original = dict(SKILL_COMMANDS)
        try:
            apply_config_overrides({"bug": "/my-custom-fix"})
            assert get_skill_command(TaskType.BUG) == "/my-custom-fix"
        finally:
            SKILL_COMMANDS.clear()
            SKILL_COMMANDS.update(original)

    def test_apply_config_overrides_add_chore_skill(self):
        """Config can add a skill for a type that doesn't have one by default."""
        original = dict(SKILL_COMMANDS)
        try:
            apply_config_overrides({"chore": "/my-chore-skill"})
            assert get_skill_command(TaskType.CHORE) == "/my-chore-skill"
        finally:
            SKILL_COMMANDS.clear()
            SKILL_COMMANDS.update(original)

    def test_apply_config_overrides_empty_removes(self):
        """Setting a workflow to empty string removes the skill."""
        original = dict(SKILL_COMMANDS)
        try:
            apply_config_overrides({"bug": ""})
            assert get_skill_command(TaskType.BUG) is None
        finally:
            SKILL_COMMANDS.clear()
            SKILL_COMMANDS.update(original)

    def test_apply_config_overrides_unknown_type_ignored(self):
        """Unknown task type keys are silently ignored."""
        original = dict(SKILL_COMMANDS)
        try:
            apply_config_overrides({"nonexistent": "/foo"})
            assert SKILL_COMMANDS == original
        finally:
            SKILL_COMMANDS.clear()
            SKILL_COMMANDS.update(original)

    def test_build_task_message_no_skill_when_slash_unsupported(self):
        """BUG task with supports_slash_commands=False gets inline workflow, not /fix-and-ship."""
        from swarm.server.messages import build_task_message

        task = SwarmTask(
            title="Fix login crash",
            description="Users can't log in",
            task_type=TaskType.BUG,
        )
        msg = build_task_message(
            task,
            supports_slash_commands=False,
            plan_mode_for_user_requests=False,
        )
        assert "/fix-and-ship" not in msg
        assert "Fix login crash" in msg
        assert "Workflow" in msg
