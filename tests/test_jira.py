"""Tests for Jira integration (OAuth 2.0 only)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.config import HiveConfig, JiraConfig
from swarm.integrations.jira import (
    _SWARM_PRIORITY_TO_JIRA,
    _SWARM_TYPE_TO_JIRA,
    JiraSyncService,
    JiraSyncStats,
    _extract_text,
    _find_transition,
    _jira_issue_to_task,
)
from swarm.tasks.task import SwarmTask, TaskPriority, TaskStatus, TaskType


def _mock_mgr(connected: bool = True) -> MagicMock:
    """Create a mock JiraTokenManager that reports the given connected state."""
    mgr = MagicMock()
    mgr.is_connected.return_value = connected
    mgr.api_base_url = "https://api.atlassian.com/ex/jira/test-cloud"
    return mgr


# --- JiraConfig ---


class TestJiraConfig:
    def test_defaults(self) -> None:
        cfg = JiraConfig()
        assert cfg.enabled is False
        assert cfg.sync_interval_minutes == 5.0
        assert "unassigned" in cfg.status_map

    def test_resolved_client_secret_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_SECRET", "s3cret")
        cfg = JiraConfig(client_secret="$MY_SECRET")
        assert cfg.resolved_client_secret() == "s3cret"

    def test_resolved_client_secret_plain(self) -> None:
        cfg = JiraConfig(client_secret="plain")
        assert cfg.resolved_client_secret() == "plain"


# --- Config Integration ---


class TestJiraConfigIntegration:
    def test_hive_config_has_jira(self) -> None:
        cfg = HiveConfig()
        assert isinstance(cfg.jira, JiraConfig)
        assert cfg.jira.enabled is False

    def test_validation_disabled_no_errors(self) -> None:
        cfg = HiveConfig()
        errors = cfg.validate()
        jira_errors = [e for e in errors if "jira" in e]
        assert jira_errors == []

    def test_validation_enabled_missing_fields(self) -> None:
        cfg = HiveConfig(jira=JiraConfig(enabled=True))
        errors = cfg.validate()
        jira_errors = [e for e in errors if "jira" in e]
        assert len(jira_errors) == 3  # client_id, client_secret, project

    def test_validation_enabled_complete(self) -> None:
        cfg = HiveConfig(
            jira=JiraConfig(
                enabled=True,
                client_id="cid",
                client_secret="csecret",
                project="PROJ",
            )
        )
        errors = cfg.validate()
        jira_errors = [e for e in errors if "jira" in e]
        assert jira_errors == []

    def test_validation_bad_interval(self) -> None:
        cfg = HiveConfig(jira=JiraConfig(sync_interval_minutes=0))
        errors = cfg.validate()
        assert any("sync_interval_minutes" in e for e in errors)

    def test_serialization(self) -> None:
        from swarm.config import serialize_config

        cfg = HiveConfig(
            jira=JiraConfig(
                enabled=True,
                client_id="cid",
                client_secret="csecret",
                cloud_id="cloud-1",
                project="PROJ",
            )
        )
        data = serialize_config(cfg)
        assert "jira" in data
        assert data["jira"]["enabled"] is True
        assert data["jira"]["client_id"] == "cid"
        assert data["jira"]["client_secret"] == "csecret"
        assert data["jira"]["cloud_id"] == "cloud-1"
        assert data["jira"]["project"] == "PROJ"

    def test_serialization_disabled_omitted(self) -> None:
        from swarm.config import serialize_config

        cfg = HiveConfig()
        data = serialize_config(cfg)
        assert "jira" not in data


# --- Helpers ---


class TestExtractText:
    def test_plain_string(self) -> None:
        assert _extract_text("hello world") == "hello world"

    def test_adf_document(self) -> None:
        # Adjacent text nodes are concatenated as-is — ADF has no implicit
        # whitespace between siblings; whitespace lives inside the text
        # content. Real Jira issues use a space-bearing text node ("Hello "
        # then "world") or a separator mark.
        adf = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Hello "},
                        {"type": "text", "text": "world"},
                    ],
                }
            ],
        }
        assert _extract_text(adf) == "Hello world"

    def test_none(self) -> None:
        assert _extract_text(None) == ""

    def test_empty_dict(self) -> None:
        assert _extract_text({}) == ""

    def test_paragraphs_become_separated(self) -> None:
        """Two ADF paragraphs should be separated by a blank line so the
        worker doesn't see one giant run-on paragraph."""
        adf = {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "First."}]},
                {"type": "paragraph", "content": [{"type": "text", "text": "Second."}]},
            ],
        }
        assert _extract_text(adf) == "First.\n\nSecond."

    def test_heading_renders_as_markdown(self) -> None:
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 2},
                    "content": [{"type": "text", "text": "Description"}],
                },
                {"type": "paragraph", "content": [{"type": "text", "text": "Body."}]},
            ],
        }
        out = _extract_text(adf)
        assert "## Description" in out
        assert "Body." in out

    def test_bullet_list_renders_as_markdown(self) -> None:
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "alpha"}],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "beta"}],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
        out = _extract_text(adf)
        assert "- alpha" in out
        assert "- beta" in out

    def test_marks_become_inline_markdown(self) -> None:
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "bold",
                            "marks": [{"type": "strong"}],
                        },
                        {"type": "text", "text": " then "},
                        {
                            "type": "text",
                            "text": "italic",
                            "marks": [{"type": "em"}],
                        },
                    ],
                }
            ],
        }
        out = _extract_text(adf)
        assert "**bold**" in out
        assert "*italic*" in out

    def test_link_mark_renders(self) -> None:
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "click here",
                            "marks": [{"type": "link", "attrs": {"href": "https://example.com"}}],
                        }
                    ],
                }
            ],
        }
        assert "[click here](https://example.com)" in _extract_text(adf)


class TestFindTransition:
    def test_match_by_name(self) -> None:
        transitions = [
            {"id": "1", "name": "To Do"},
            {"id": "2", "name": "In Progress"},
            {"id": "3", "name": "Done"},
        ]
        assert _find_transition(transitions, "Done") == "3"

    def test_match_case_insensitive(self) -> None:
        transitions = [{"id": "1", "name": "In Progress"}]
        assert _find_transition(transitions, "in progress") == "1"

    def test_match_by_to_status(self) -> None:
        transitions = [
            {"id": "5", "name": "Move to Done", "to": {"name": "Done"}},
        ]
        assert _find_transition(transitions, "Done") == "5"

    def test_no_match(self) -> None:
        transitions = [{"id": "1", "name": "In Progress"}]
        assert _find_transition(transitions, "Done") is None

    def test_empty(self) -> None:
        assert _find_transition([], "Done") is None


class TestJiraIssueToTask:
    def test_basic_conversion(self) -> None:
        fields = {
            "summary": "Fix login bug",
            "description": "Users can't login",
            "issuetype": {"name": "Bug"},
            "priority": {"name": "High"},
        }
        task = _jira_issue_to_task("PROJ-123", fields)
        assert task.title == "Fix login bug"
        assert task.description == "Users can't login"
        assert task.jira_key == "PROJ-123"
        assert task.task_type == TaskType.BUG
        assert task.priority == TaskPriority.HIGH

    def test_story_maps_to_feature(self) -> None:
        fields = {
            "summary": "Add dashboard",
            "issuetype": {"name": "Story"},
            "priority": {"name": "Medium"},
        }
        task = _jira_issue_to_task("PROJ-1", fields)
        assert task.task_type == TaskType.FEATURE
        assert task.priority == TaskPriority.NORMAL

    def test_unknown_type_defaults_to_chore(self) -> None:
        fields = {"summary": "Deploy", "issuetype": {"name": "Unknown"}}
        task = _jira_issue_to_task("PROJ-1", fields)
        assert task.task_type == TaskType.CHORE

    def test_adf_description(self) -> None:
        fields = {
            "summary": "Test",
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "ADF body"}],
                    }
                ],
            },
        }
        task = _jira_issue_to_task("PROJ-1", fields)
        assert task.description == "ADF body"

    def test_missing_fields(self) -> None:
        task = _jira_issue_to_task("PROJ-1", {})
        assert task.title == "PROJ-1"
        assert task.description == ""
        assert task.task_type == TaskType.CHORE
        assert task.priority == TaskPriority.NORMAL


# --- JiraSyncService ---


class TestJiraSyncService:
    def _make_service(self, **kwargs: object) -> JiraSyncService:
        mgr = kwargs.pop("_mgr", None) or _mock_mgr()
        defaults: dict[str, object] = {
            "enabled": True,
            "project": "PROJ",
        }
        defaults.update(kwargs)
        cfg = JiraConfig(**defaults)  # type: ignore[arg-type]
        return JiraSyncService(cfg, token_manager=mgr)

    def test_enabled(self) -> None:
        svc = self._make_service()
        assert svc.enabled is True

    def test_disabled_no_manager(self) -> None:
        cfg = JiraConfig(enabled=True, project="PROJ")
        svc = JiraSyncService(cfg, token_manager=None)
        assert svc.enabled is False

    def test_disabled_disconnected(self) -> None:
        svc = self._make_service(_mgr=_mock_mgr(connected=False))
        assert svc.enabled is False

    def test_disabled_flag(self) -> None:
        svc = self._make_service(enabled=False)
        assert svc.enabled is False

    def test_get_status(self) -> None:
        svc = self._make_service()
        status = svc.get_status()
        assert status["enabled"] is True
        assert status["total_syncs"] == 0

    @pytest.mark.asyncio
    async def test_import_disabled(self) -> None:
        svc = self._make_service(enabled=False)
        result = await svc.import_issues({})
        assert result == []

    @pytest.mark.asyncio
    async def test_import_deduplicates(self) -> None:
        svc = self._make_service()
        svc.client.search_issues = AsyncMock(
            return_value=[
                {
                    "key": "PROJ-1",
                    "fields": {"summary": "Existing", "issuetype": {"name": "Task"}},
                },
                {
                    "key": "PROJ-2",
                    "fields": {"summary": "New", "issuetype": {"name": "Bug"}},
                },
            ]
        )
        existing = {
            "abc": SwarmTask(title="Existing", jira_key="PROJ-1"),
        }
        new_tasks = await svc.import_issues(existing)
        assert len(new_tasks) == 1
        assert new_tasks[0].jira_key == "PROJ-2"
        assert svc.stats.total_imported == 1

    @pytest.mark.asyncio
    async def test_import_handles_error(self) -> None:
        import aiohttp

        svc = self._make_service()
        svc.client.search_issues = AsyncMock(side_effect=aiohttp.ClientError("connection failed"))
        result = await svc.import_issues({})
        assert result == []
        assert svc.stats.errors == 1

    @pytest.mark.asyncio
    async def test_export_status(self) -> None:
        svc = self._make_service()
        svc.client.get_transitions = AsyncMock(
            return_value=[
                {"id": "31", "name": "In Progress"},
            ]
        )
        svc.client.transition_issue = AsyncMock(return_value=True)
        task = SwarmTask(title="Test", jira_key="PROJ-1")
        ok = await svc.export_status(task, TaskStatus.ACTIVE)
        assert ok is True
        svc.client.transition_issue.assert_called_once_with("PROJ-1", "31")
        assert svc.stats.total_exported == 1

    @pytest.mark.asyncio
    async def test_export_status_no_jira_key(self) -> None:
        svc = self._make_service()
        task = SwarmTask(title="Test")
        ok = await svc.export_status(task, TaskStatus.ACTIVE)
        assert ok is False

    @pytest.mark.asyncio
    async def test_export_no_matching_transition(self) -> None:
        svc = self._make_service()
        svc.client.get_transitions = AsyncMock(return_value=[{"id": "1", "name": "To Do"}])
        task = SwarmTask(title="Test", jira_key="PROJ-1")
        ok = await svc.export_status(task, TaskStatus.ACTIVE)
        assert ok is False

    @pytest.mark.asyncio
    async def test_post_completion_comment(self) -> None:
        svc = self._make_service()
        svc.client.add_comment = AsyncMock(return_value=True)
        task = SwarmTask(
            title="Fix login page",
            jira_key="PROJ-1",
            assigned_worker="w1",
            resolution="Fixed the issue",
        )
        ok = await svc.post_completion_comment(task)
        assert ok is True
        call_body = svc.client.add_comment.call_args[0][1]
        assert "w1" in call_body
        assert "Fixed the issue" in call_body
        assert "Fix login page" in call_body
        assert "Summary" in call_body
        assert "Technical Resolution" in call_body

    @pytest.mark.asyncio
    async def test_post_completion_comment_logs_success(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        svc = self._make_service()
        svc.client.add_comment = AsyncMock(return_value=True)
        task = SwarmTask(
            title="Test",
            jira_key="PROJ-1",
            resolution="Resolved via pipeline test",
        )
        with caplog.at_level("INFO", logger="swarm.integrations.jira"):
            ok = await svc.post_completion_comment(task)
        assert ok is True
        assert any("PROJ-1" in r.message and "comment" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_post_completion_no_jira_key(self) -> None:
        svc = self._make_service()
        task = SwarmTask(title="Test")
        ok = await svc.post_completion_comment(task)
        assert ok is False


# --- JiraSyncStats ---


class TestJiraSyncStats:
    def test_defaults(self) -> None:
        stats = JiraSyncStats()
        assert stats.total_syncs == 0
        assert stats.total_imported == 0
        assert stats.total_exported == 0
        assert stats.errors == 0
        assert stats.last_error == ""


# --- Label Filtering ---


class TestLabelFiltering:
    def _make_service(self, **kwargs: object) -> JiraSyncService:
        defaults: dict[str, object] = {
            "enabled": True,
            "project": "PROJ",
        }
        defaults.update(kwargs)
        cfg = JiraConfig(**defaults)  # type: ignore[arg-type]
        return JiraSyncService(cfg, token_manager=_mock_mgr())

    @pytest.mark.asyncio
    async def test_import_label_in_jql_and_client_side(self) -> None:
        """Label is included in JQL for server-side filtering, plus client-side safety net."""
        svc = self._make_service(import_label="swarm")
        svc.client.search_issues = AsyncMock(
            return_value=[
                {
                    "key": "PROJ-1",
                    "fields": {
                        "summary": "Match",
                        "issuetype": {"name": "Task"},
                        "labels": ["swarm"],
                    },
                },
                {
                    "key": "PROJ-2",
                    "fields": {
                        "summary": "No label",
                        "issuetype": {"name": "Task"},
                        "labels": [],
                    },
                },
            ]
        )
        tasks = await svc.import_issues({})
        assert len(tasks) == 1
        assert tasks[0].jira_key == "PROJ-1"
        # JQL should contain label filter
        jql = svc.client.search_issues.call_args[0][0]
        assert 'labels = "swarm"' in jql

    @pytest.mark.asyncio
    async def test_import_label_case_insensitive(self) -> None:
        """Label filter matches regardless of case (Swarm, swarm, SWARM)."""
        svc = self._make_service(import_label="swarm")
        svc.client.search_issues = AsyncMock(
            return_value=[
                {
                    "key": "PROJ-1",
                    "fields": {
                        "summary": "Uppercase",
                        "issuetype": {"name": "Task"},
                        "labels": ["Swarm"],
                    },
                },
                {
                    "key": "PROJ-2",
                    "fields": {
                        "summary": "Mixed",
                        "issuetype": {"name": "Task"},
                        "labels": ["SWARM"],
                    },
                },
                {
                    "key": "PROJ-3",
                    "fields": {
                        "summary": "Wrong",
                        "issuetype": {"name": "Task"},
                        "labels": ["other"],
                    },
                },
            ]
        )
        tasks = await svc.import_issues({})
        assert len(tasks) == 2
        assert {t.jira_key for t in tasks} == {"PROJ-1", "PROJ-2"}

    @pytest.mark.asyncio
    async def test_import_label_empty_no_filter(self) -> None:
        """When import_label is empty, all issues are returned."""
        svc = self._make_service(import_label="")
        svc.client.search_issues = AsyncMock(
            return_value=[
                {
                    "key": "PROJ-1",
                    "fields": {
                        "summary": "Any",
                        "issuetype": {"name": "Task"},
                        "labels": [],
                    },
                },
            ]
        )
        tasks = await svc.import_issues({})
        assert len(tasks) == 1


# --- build_jql ---


class TestBuildJql:
    def _make_service(self, **kwargs: object) -> JiraSyncService:
        defaults: dict[str, object] = {"enabled": True}
        defaults.update(kwargs)
        cfg = JiraConfig(**defaults)  # type: ignore[arg-type]
        return JiraSyncService(cfg, token_manager=_mock_mgr())

    def test_excludes_done_status_category(self) -> None:
        """JQL must always exclude completed/done issues."""
        svc = self._make_service(project="PROJ")
        jql = svc.build_jql()
        assert "statusCategory != Done" in jql

    def test_excludes_done_with_label_no_project(self) -> None:
        """Label-only config (no project) should still exclude done issues."""
        svc = self._make_service(import_label="swarm")
        jql = svc.build_jql()
        assert "statusCategory != Done" in jql
        assert 'labels = "swarm"' in jql

    def test_label_only_no_30d_fallback(self) -> None:
        """When a label is set, the 30-day fallback should not apply."""
        svc = self._make_service(import_label="swarm")
        jql = svc.build_jql()
        assert "-30d" not in jql

    def test_no_filters_at_all_has_30d_fallback(self) -> None:
        """With no project, no label, no filter — 30d fallback is a safety net."""
        svc = self._make_service()
        jql = svc.build_jql()
        assert "-30d" in jql

    def test_custom_filter_not_overridden(self) -> None:
        """Custom import_filter should be preserved; done exclusion still added."""
        svc = self._make_service(import_filter="assignee = currentUser()")
        jql = svc.build_jql()
        assert "assignee = currentUser()" in jql
        assert "statusCategory != Done" in jql

    def test_custom_filter_with_status_not_doubled(self) -> None:
        """If custom filter already mentions statusCategory, don't add it again."""
        svc = self._make_service(import_filter="statusCategory = 'In Progress'")
        jql = svc.build_jql()
        assert jql.lower().count("statuscategory") == 1

    def test_lookback_days_custom(self) -> None:
        """Custom lookback_days should be used in the fallback JQL."""
        svc = self._make_service(lookback_days=90)
        jql = svc.build_jql()
        assert "-90d" in jql
        assert "-30d" not in jql

    def test_lookback_days_zero_no_date_filter(self) -> None:
        """lookback_days=0 means no date restriction in the fallback."""
        svc = self._make_service(lookback_days=0)
        jql = svc.build_jql()
        assert "created >=" not in jql

    def test_order_by_in_filter_stays_last(self) -> None:
        """ORDER BY embedded in import_filter must remain at the end of the JQL."""
        svc = self._make_service(
            import_filter="status NOT IN (Closed, Done) ORDER BY created DESC",
            import_label="swarm",
        )
        jql = svc.build_jql()
        order_idx = jql.lower().index("order by")
        # Nothing except the ORDER BY clause should follow it
        after_order = jql[order_idx:]
        assert "AND" not in after_order


# --- OAuth ---


class TestOAuth:
    def test_enabled_no_manager(self) -> None:
        cfg = JiraConfig(enabled=True)
        svc = JiraSyncService(cfg, token_manager=None)
        assert svc.enabled is False

    def test_enabled_connected(self) -> None:
        cfg = JiraConfig(enabled=True)
        svc = JiraSyncService(cfg, token_manager=_mock_mgr(connected=True))
        assert svc.enabled is True

    def test_enabled_disconnected(self) -> None:
        cfg = JiraConfig(enabled=True)
        svc = JiraSyncService(cfg, token_manager=_mock_mgr(connected=False))
        assert svc.enabled is False

    def test_validation_missing_fields(self) -> None:
        cfg = HiveConfig(jira=JiraConfig(enabled=True, project="P"))
        errors = cfg.validate()
        jira_errors = [e for e in errors if "jira" in e]
        assert any("client_id" in e for e in jira_errors)
        assert any("client_secret" in e for e in jira_errors)

    def test_validation_complete(self) -> None:
        cfg = HiveConfig(
            jira=JiraConfig(
                enabled=True,
                client_id="cid",
                client_secret="csecret",
                project="PROJ",
            )
        )
        errors = cfg.validate()
        jira_errors = [e for e in errors if "jira" in e]
        assert jira_errors == []


# --- Issue Creation ---


class TestIssueCreation:
    def _make_service(self, **kwargs: object) -> JiraSyncService:
        defaults: dict[str, object] = {
            "enabled": True,
            "project": "PROJ",
        }
        defaults.update(kwargs)
        cfg = JiraConfig(**defaults)  # type: ignore[arg-type]
        return JiraSyncService(cfg, token_manager=_mock_mgr())

    @pytest.mark.asyncio
    async def test_create_issue_basic(self) -> None:
        svc = self._make_service()
        svc.client.create_issue = AsyncMock(return_value={"key": "PROJ-99", "id": "10001"})
        task = SwarmTask(
            title="Fix bug",
            description="Something broken",
            task_type=TaskType.BUG,
            priority=TaskPriority.HIGH,
        )
        key = await svc.create_jira_issue(task)
        assert key == "PROJ-99"
        call_kw = svc.client.create_issue.call_args
        assert call_kw[1]["issue_type"] == "Bug"
        assert call_kw[1]["priority"] == "High"
        assert svc.stats.total_exported == 1

    @pytest.mark.asyncio
    async def test_create_jira_issue_maps_types(self) -> None:
        svc = self._make_service()
        svc.client.create_issue = AsyncMock(return_value={"key": "PROJ-1", "id": "1"})
        task = SwarmTask(
            title="New feature",
            task_type=TaskType.FEATURE,
            priority=TaskPriority.URGENT,
        )
        await svc.create_jira_issue(task)
        call_kw = svc.client.create_issue.call_args
        assert call_kw[1]["issue_type"] == "Story"
        assert call_kw[1]["priority"] == "Highest"

    @pytest.mark.asyncio
    async def test_create_jira_issue_when_disabled(self) -> None:
        svc = self._make_service(enabled=False)
        task = SwarmTask(title="Test")
        with pytest.raises(RuntimeError, match="not enabled"):
            await svc.create_jira_issue(task)

    def test_reverse_map_completeness(self) -> None:
        """All TaskType and TaskPriority enum values are covered."""
        for tt in TaskType:
            assert tt in _SWARM_TYPE_TO_JIRA, f"Missing {tt}"
        for tp in TaskPriority:
            assert tp in _SWARM_PRIORITY_TO_JIRA, f"Missing {tp}"


# --- Assignment ---


class TestAssignment:
    def _make_service(self, **kwargs: object) -> JiraSyncService:
        defaults: dict[str, object] = {
            "enabled": True,
            "project": "PROJ",
        }
        defaults.update(kwargs)
        cfg = JiraConfig(**defaults)  # type: ignore[arg-type]
        mgr = _mock_mgr()
        mgr.account_id = "abc123"
        return JiraSyncService(cfg, token_manager=mgr)

    @pytest.mark.asyncio
    async def test_assign_to_me(self) -> None:
        """assign_to_me should call client.assign_issue with the token manager's account_id."""
        svc = self._make_service()
        svc.client.assign_issue = AsyncMock(return_value=True)
        task = SwarmTask(title="Test", jira_key="PROJ-1")
        ok = await svc.assign_to_me(task)
        assert ok is True
        svc.client.assign_issue.assert_called_once_with("PROJ-1", "abc123")

    @pytest.mark.asyncio
    async def test_assign_to_me_no_jira_key(self) -> None:
        """assign_to_me returns False when task has no jira_key."""
        svc = self._make_service()
        task = SwarmTask(title="Test")
        ok = await svc.assign_to_me(task)
        assert ok is False

    @pytest.mark.asyncio
    async def test_assign_to_me_no_account_id(self) -> None:
        """assign_to_me returns False when token manager has no account_id."""
        svc = self._make_service()
        svc._token_manager.account_id = ""  # type: ignore[union-attr]
        svc.client.assign_issue = AsyncMock(return_value=True)
        task = SwarmTask(title="Test", jira_key="PROJ-1")
        ok = await svc.assign_to_me(task)
        assert ok is False
        svc.client.assign_issue.assert_not_called()

    @pytest.mark.asyncio
    async def test_assign_to_me_disabled(self) -> None:
        """assign_to_me returns False when Jira is disabled."""
        svc = self._make_service(enabled=False)
        task = SwarmTask(title="Test", jira_key="PROJ-1")
        ok = await svc.assign_to_me(task)
        assert ok is False

    @pytest.mark.asyncio
    async def test_assign_to_me_handles_error(self) -> None:
        """assign_to_me logs and returns False on network error."""
        import aiohttp

        svc = self._make_service()
        svc.client.assign_issue = AsyncMock(side_effect=aiohttp.ClientError("fail"))
        task = SwarmTask(title="Test", jira_key="PROJ-1")
        ok = await svc.assign_to_me(task)
        assert ok is False
        assert svc.stats.errors == 1

    @pytest.mark.asyncio
    async def test_client_assign_issue(self) -> None:
        """JiraClient.assign_issue PUTs the assignee."""
        from swarm.integrations.jira import JiraClient

        cfg = JiraConfig(enabled=True, project="PROJ")
        mgr = _mock_mgr()
        mgr.get_token = AsyncMock(return_value="tok")
        client = JiraClient(cfg, token_manager=mgr)

        mock_resp = AsyncMock()
        mock_resp.status = 204
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = AsyncMock()
        session.put = MagicMock(return_value=mock_resp)
        session.closed = False
        client._session = session
        client._current_token = "tok"
        client._base_url = "https://api.atlassian.com/ex/jira/test-cloud"

        ok = await client.assign_issue("PROJ-1", "abc123")
        assert ok is True
        session.put.assert_called_once()
        call_args = session.put.call_args
        assert "/rest/api/3/issue/PROJ-1/assignee" in call_args[0][0]
        assert call_args[1]["json"] == {"accountId": "abc123"}

    @pytest.mark.asyncio
    async def test_client_get_myself(self) -> None:
        """JiraClient.get_myself returns the current user's account info."""
        from swarm.integrations.jira import JiraClient

        cfg = JiraConfig(enabled=True, project="PROJ")
        mgr = _mock_mgr()
        mgr.get_token = AsyncMock(return_value="tok")
        client = JiraClient(cfg, token_manager=mgr)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"accountId": "abc123", "displayName": "Me"})
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        session = AsyncMock()
        session.get = MagicMock(return_value=mock_resp)
        session.closed = False
        client._session = session
        client._current_token = "tok"
        client._base_url = "https://api.atlassian.com/ex/jira/test-cloud"

        result = await client.get_myself()
        assert result["accountId"] == "abc123"
        session.get.assert_called_once()
        assert "/rest/api/3/myself" in session.get.call_args[0][0]


# --- JiraService (server-level) ---


class TestJiraServiceRunImport:
    """Regression tests for JiraService.run_import (server/jira_service.py)."""

    @pytest.mark.asyncio
    async def test_run_import_multiple_tasks(self) -> None:
        """All tasks should be added when Jira returns multiple issues."""
        from swarm.drones.log import SystemLog
        from swarm.server.jira_service import JiraService
        from swarm.tasks.board import TaskBoard

        board = TaskBoard()
        drone_log = SystemLog()
        tasks_to_import = [
            SwarmTask(title="Task A", jira_key="PROJ-1"),
            SwarmTask(title="Task B", jira_key="PROJ-2"),
            SwarmTask(title="Task C", jira_key="PROJ-3"),
        ]

        mock_jira = MagicMock()
        mock_jira.import_issues = AsyncMock(return_value=tasks_to_import)

        ws_messages: list[dict[str, object]] = []

        svc = JiraService(
            get_jira=lambda: mock_jira,
            task_board=board,
            broadcast_ws=ws_messages.append,
            drone_log=drone_log,
            track_task=lambda t: None,
            get_sync_interval=lambda: 300,
        )

        count = await svc.run_import()

        assert count == 3
        assert len(board.all_tasks) == 3
        assert {t.jira_key for t in board.all_tasks} == {"PROJ-1", "PROJ-2", "PROJ-3"}
        assert len(ws_messages) == 1
        assert ws_messages[0]["count"] == 3


# --- Comments + attachment sync ---


class TestCommentFormatting:
    def test_format_comments_renders_text_block(self) -> None:
        from swarm.integrations.jira import _format_comments

        comment_field = {
            "comments": [
                {
                    "author": {"displayName": "Sharon E."},
                    "created": "2026-03-30T12:02:11.123-0400",
                    "body": "I still can't merge these three.",
                },
                {
                    "author": {"displayName": "Edward W."},
                    "created": "2026-02-17T10:09:00.000-0500",
                    "body": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Still not working."}],
                            }
                        ],
                    },
                },
            ]
        }
        text = _format_comments(comment_field)
        assert "Sharon E." in text
        assert "I still can't merge" in text
        assert "Edward W." in text
        assert "Still not working." in text

    def test_format_comments_empty(self) -> None:
        from swarm.integrations.jira import _format_comments

        assert _format_comments(None) == ""
        assert _format_comments({}) == ""
        assert _format_comments({"comments": []}) == ""

    def test_format_comments_skips_empty_bodies(self) -> None:
        from swarm.integrations.jira import _format_comments

        text = _format_comments({"comments": [{"author": {"displayName": "X"}, "body": "  "}]})
        assert text == ""


class TestAttachmentList:
    def test_format_attachment_list(self) -> None:
        from swarm.integrations.jira import _format_attachment_list

        text = _format_attachment_list(
            [
                {"filename": "image001.png", "id": "10001"},
                {"filename": "image002.png", "id": "10002"},
            ]
        )
        assert "image001.png" in text
        assert "image002.png" in text

    def test_format_attachment_list_empty(self) -> None:
        from swarm.integrations.jira import _format_attachment_list

        assert _format_attachment_list(None) == ""
        assert _format_attachment_list([]) == ""


class TestEnrichTaskFromFields:
    def _make_service(self, uploads_dir: object) -> JiraSyncService:
        cfg = JiraConfig(enabled=True, project="PROJ")
        return JiraSyncService(cfg, token_manager=_mock_mgr(), uploads_dir=uploads_dir)

    @pytest.mark.asyncio
    async def test_attachments_downloaded_and_paths_recorded(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        svc = self._make_service(uploads_dir=tmp_path)
        svc.client.download_attachment = AsyncMock(side_effect=[b"png-bytes-1", b"png-bytes-2"])

        task = SwarmTask(title="Merge", jira_key="MTR-11806", description="")
        fields = {
            "attachment": [
                {"id": "10001", "filename": "image001.png"},
                {"id": "10002", "filename": "image002.png"},
            ],
            "comment": {
                "comments": [
                    {
                        "author": {"displayName": "Sharon E."},
                        "created": "2026-03-30T12:02:11.123-0400",
                        "body": "I still can't merge these three.",
                    }
                ]
            },
        }

        await svc._enrich_task_from_fields(task, fields)

        # Both attachments downloaded and persisted to uploads dir
        assert len(task.attachments) == 2
        for path in task.attachments:
            assert path.startswith(str(tmp_path))
        # Comment text appears in description
        assert "Sharon E." in task.description
        assert "I still can't merge" in task.description
        # Attachment filenames listed in description
        assert "image001.png" in task.description
        assert "image002.png" in task.description
        # Sync marker present
        assert "Jira sync" in task.description

    @pytest.mark.asyncio
    async def test_download_failure_skips_attachment(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        import aiohttp

        svc = self._make_service(uploads_dir=tmp_path)
        svc.client.download_attachment = AsyncMock(side_effect=[aiohttp.ClientError("boom"), b"ok"])

        task = SwarmTask(title="t", jira_key="PROJ-1")
        fields = {
            "attachment": [
                {"id": "1", "filename": "broken.png"},
                {"id": "2", "filename": "ok.png"},
            ],
        }
        await svc._enrich_task_from_fields(task, fields)

        # Only the second attachment was saved
        assert len(task.attachments) == 1
        assert task.attachments[0].endswith("ok.png")

    @pytest.mark.asyncio
    async def test_import_issues_enriches_each_task(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        svc = self._make_service(uploads_dir=tmp_path)
        svc.client.search_issues = AsyncMock(
            return_value=[
                {
                    "key": "MTR-11806",
                    "fields": {
                        "summary": "Merge",
                        "issuetype": {"name": "Task"},
                        "attachment": [{"id": "10001", "filename": "img.png"}],
                        "comment": {
                            "comments": [
                                {"author": {"displayName": "U"}, "body": "hi", "created": ""}
                            ]
                        },
                    },
                },
            ]
        )
        svc.client.download_attachment = AsyncMock(return_value=b"data")

        new_tasks = await svc.import_issues({})
        assert len(new_tasks) == 1
        t = new_tasks[0]
        assert t.attachments and t.attachments[0].endswith("img.png")
        assert "U:" in t.description and "hi" in t.description


class TestRefreshTask:
    def _make_service(self, uploads_dir: object) -> JiraSyncService:
        cfg = JiraConfig(enabled=True, project="PROJ")
        return JiraSyncService(cfg, token_manager=_mock_mgr(), uploads_dir=uploads_dir)

    @pytest.mark.asyncio
    async def test_refresh_pulls_comments_and_attachments(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        svc = self._make_service(uploads_dir=tmp_path)
        svc.client.get_issue = AsyncMock(
            return_value={
                "key": "MTR-11806",
                "fields": {
                    "summary": "Merge",
                    "description": "Body text",
                    "attachment": [{"id": "1", "filename": "a.png"}],
                    "comment": {
                        "comments": [
                            {"author": {"displayName": "X"}, "body": "note", "created": ""}
                        ]
                    },
                },
            }
        )
        svc.client.download_attachment = AsyncMock(return_value=b"png")

        task = SwarmTask(title="Merge", jira_key="MTR-11806", description="")
        ok = await svc.refresh_task(task)
        assert ok is True
        assert task.description.startswith("Body text")
        assert "note" in task.description
        assert task.attachments and task.attachments[0].endswith("a.png")

    @pytest.mark.asyncio
    async def test_refresh_no_jira_key(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        svc = self._make_service(uploads_dir=tmp_path)
        task = SwarmTask(title="t")
        assert await svc.refresh_task(task) is False

    @pytest.mark.asyncio
    async def test_refresh_strips_old_sync_tail(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        svc = self._make_service(uploads_dir=tmp_path)
        svc.client.get_issue = AsyncMock(
            return_value={
                "key": "PROJ-1",
                "fields": {
                    "summary": "x",
                    "description": "fresh body",
                    "attachment": [],
                    "comment": {"comments": []},
                },
            }
        )

        from swarm.integrations.jira import _JIRA_SYNC_MARKER

        task = SwarmTask(
            title="t",
            jira_key="PROJ-1",
            description=f"old body{_JIRA_SYNC_MARKER}stale comments here",
        )
        ok = await svc.refresh_task(task)
        assert ok is True
        # Stale tail dropped, fresh body is the only content
        assert "stale comments here" not in task.description
        assert "fresh body" in task.description


class TestFormatCommentAuthor:
    def test_display_name_preferred(self) -> None:
        from swarm.integrations.jira import _format_comment_author

        assert _format_comment_author({"displayName": "Ada", "emailAddress": "a@x.io"}) == "Ada"

    def test_email_fallback(self) -> None:
        from swarm.integrations.jira import _format_comment_author

        assert _format_comment_author({"emailAddress": "a@x.io"}) == "a@x.io"

    def test_non_dict_is_unknown(self) -> None:
        from swarm.integrations.jira import _format_comment_author

        assert _format_comment_author(None) == "Unknown"
        assert _format_comment_author({}) == "Unknown"


class TestFormatCommentTimestamp:
    def test_iso_with_milliseconds_and_offset(self) -> None:
        from swarm.integrations.jira import _format_comment_timestamp

        assert _format_comment_timestamp("2026-03-30T12:02:11.123-0400") == "2026-03-30 12:02"

    def test_iso_without_milliseconds(self) -> None:
        from swarm.integrations.jira import _format_comment_timestamp

        assert _format_comment_timestamp("2026-03-30T12:02:11-0400") == "2026-03-30 12:02"

    def test_unparseable_falls_back_to_raw(self) -> None:
        from swarm.integrations.jira import _format_comment_timestamp

        assert _format_comment_timestamp("not a date") == "not a date"

    def test_empty_string(self) -> None:
        from swarm.integrations.jira import _format_comment_timestamp

        assert _format_comment_timestamp("") == ""


class TestTruncate:
    def test_under_limit_unchanged(self) -> None:
        from swarm.integrations.jira import _truncate

        assert _truncate("hello", 10) == "hello"

    def test_exactly_at_limit_unchanged(self) -> None:
        from swarm.integrations.jira import _truncate

        assert _truncate("hello", 5) == "hello"

    def test_over_limit_truncates_with_ellipsis(self) -> None:
        from swarm.integrations.jira import _truncate

        out = _truncate("hello world", 6)
        assert len(out) == 6
        assert out.endswith("…")
        assert out == "hello…"


class TestBuildSyncedDescription:
    def test_base_only_has_no_sync_marker(self) -> None:
        from swarm.integrations.jira import _JIRA_SYNC_MARKER, _build_synced_description

        out = _build_synced_description("just the body", {}, [])
        assert out == "just the body"
        assert _JIRA_SYNC_MARKER.strip() not in out

    def test_comments_appended_under_marker(self) -> None:
        from swarm.integrations.jira import _build_synced_description

        fields = {
            "comment": {"comments": [{"author": {"displayName": "Ada"}, "body": "looks good"}]}
        }
        out = _build_synced_description("body", fields, [])
        assert "--- Jira sync ---" in out
        assert "Comments:" in out
        assert "looks good" in out

    def test_local_attachment_paths_listed(self) -> None:
        from swarm.integrations.jira import _build_synced_description

        out = _build_synced_description("body", {}, ["/uploads/a.png", "/uploads/b.pdf"])
        assert "Local attachment paths:" in out
        assert "- /uploads/a.png" in out
        assert "- /uploads/b.pdf" in out

    def test_truncated_to_budget(self) -> None:
        from swarm.integrations.jira import _DESC_BUDGET, _build_synced_description

        out = _build_synced_description("x" * (_DESC_BUDGET + 500), {}, [])
        assert len(out) == _DESC_BUDGET
        assert out.endswith("…")
