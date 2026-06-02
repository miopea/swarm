"""Jira integration — two-way sync between Jira and Swarm task board."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp

from swarm.config import JiraConfig
from swarm.logging import get_logger
from swarm.tasks.task import SwarmTask, TaskPriority, TaskStatus, TaskType

if TYPE_CHECKING:
    from pathlib import Path

    from swarm.auth.jira import JiraTokenManager

_log = get_logger("integrations.jira")

# Marker that delimits the auto-synced tail of a Jira-imported description.
# Anything after this line is regenerated on every refresh.
_JIRA_SYNC_MARKER = "\n\n--- Jira sync ---\n"

# Field list requested from the Jira REST search/get APIs. Includes comment
# and attachment so we can mirror them into the Swarm task on import.
_JIRA_ISSUE_FIELDS = "summary,description,status,issuetype,priority,labels,comment,attachment"

# Cap how much of the synced text we append to a task description so we don't
# blow past the task description size limit (10000 chars enforced in routes).
_DESC_BUDGET = 9000

# Filename safety regex used when downloading Jira attachments to disk.
_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._-]")
_DIGEST_LEN = 12

# Compiled once: a trailing ORDER BY clause in a JQL filter, and the two
# whitespace-normalizing passes the ADF→markdown extractor runs per issue.
_ORDER_BY_RE = re.compile(r"\s+ORDER\s+BY\s+.+$", re.IGNORECASE)
_TRAILING_WS_RE = re.compile(r"[ \t]+\n")
_BLANK_RUN_RE = re.compile(r"\n{3,}")

# Jira issue type → Swarm TaskType
_JIRA_TYPE_MAP: dict[str, TaskType] = {
    "bug": TaskType.BUG,
    "story": TaskType.FEATURE,
    "task": TaskType.CHORE,
    "sub-task": TaskType.CHORE,
    "epic": TaskType.FEATURE,
}

# Swarm TaskType → Jira issue type (reverse)
_SWARM_TYPE_TO_JIRA: dict[TaskType, str] = {
    TaskType.BUG: "Bug",
    TaskType.FEATURE: "Story",
    TaskType.CHORE: "Task",
    TaskType.VERIFY: "Task",
    TaskType.CONTENT: "Task",
    TaskType.REVIEW: "Task",
    TaskType.PUBLISH: "Task",
    TaskType.INGEST: "Task",
    TaskType.OPERATOR: "Task",
}

# Jira priority → Swarm TaskPriority
_JIRA_PRIORITY_MAP: dict[str, TaskPriority] = {
    "highest": TaskPriority.URGENT,
    "high": TaskPriority.HIGH,
    "medium": TaskPriority.NORMAL,
    "low": TaskPriority.LOW,
    "lowest": TaskPriority.LOW,
}

# Swarm TaskPriority → Jira priority (reverse)
_SWARM_PRIORITY_TO_JIRA: dict[TaskPriority, str] = {
    TaskPriority.URGENT: "Highest",
    TaskPriority.HIGH: "High",
    TaskPriority.NORMAL: "Medium",
    TaskPriority.LOW: "Low",
}


class JiraAuthError(RuntimeError):
    """Jira OAuth is unusable — token expired/revoked or not configured.

    An *expected* operational state (refresh tokens expire), not a bug.
    Subclasses RuntimeError so any existing ``except RuntimeError``
    callers still catch it; ``handle_errors`` maps it to a clean 400
    with the actionable message instead of an opaque 500 + error_id.
    """


@dataclass
class JiraSyncStats:
    """Track sync operation results."""

    last_sync: float = 0.0
    total_syncs: int = 0
    total_imported: int = 0
    total_exported: int = 0
    last_error: str = ""
    errors: int = 0


class JiraClient:
    """Async HTTP client for Jira REST API v3 (OAuth 2.0 only)."""

    def __init__(self, config: JiraConfig, token_manager: JiraTokenManager | None = None) -> None:
        self._config = config
        self._token_manager = token_manager
        self._base_url = self._resolve_base_url()
        self._session: aiohttp.ClientSession | None = None
        self._current_token: str | None = None  # track OAuth token for session reuse

    def _resolve_base_url(self) -> str:
        if self._token_manager and self._token_manager.api_base_url:
            return self._token_manager.api_base_url
        return ""

    def update_base_url(self) -> None:
        """Refresh base URL (call after cloud_id discovery)."""
        self._base_url = self._resolve_base_url()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Create or reuse an OAuth session with Bearer token."""
        if self._token_manager is None:
            raise JiraAuthError("No Jira OAuth token manager configured")
        token = await self._token_manager.get_token()
        if not token:
            raise JiraAuthError(
                "Jira authorization expired or revoked — reconnect Jira on the Config page"
            )
        # Recreate session when token changes
        if self._session and not self._session.closed and self._current_token == token:
            return self._session
        if self._session and not self._session.closed:
            await self._session.close()
        self._current_token = token
        self._base_url = self._resolve_base_url()
        _log.debug("Jira session base_url: %s", self._base_url)
        self._session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=30),
        )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def search_issues(self, jql: str, max_results: int = 50) -> list[dict[str, Any]]:
        """Search Jira issues using JQL.

        Returns a list of issue dicts with key, summary, description, etc.
        """
        session = await self._ensure_session()
        url = f"{self._base_url}/rest/api/3/search/jql"
        params: dict[str, Any] = {
            "jql": jql,
            "maxResults": max_results,
            "fields": _JIRA_ISSUE_FIELDS,
        }
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                body = await resp.text()
                _log.warning(
                    "Jira search failed: %d %s — %s (url=%s, jql=%s)",
                    resp.status,
                    resp.reason,
                    body[:500],
                    url,
                    jql,
                )
                if resp.status == 410 and self._token_manager:
                    _log.warning("410 Gone — cloud_id may be stale, re-discovering")
                    await self._token_manager._discover_cloud_id()
                    self.update_base_url()
                resp.raise_for_status()
            data = await resp.json()
        return data.get("issues", [])

    async def get_issue(self, issue_key: str) -> dict[str, Any]:
        """Fetch a single issue with the standard sync field set.

        Used to refresh an existing Swarm task from its linked Jira issue —
        returns the same shape as ``search_issues`` entries.
        """
        session = await self._ensure_session()
        url = f"{self._base_url}/rest/api/3/issue/{issue_key}"
        params = {"fields": _JIRA_ISSUE_FIELDS}
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def download_attachment(self, attachment_id: str) -> bytes:
        """Download attachment content via the OAuth-aware REST endpoint.

        We deliberately reconstruct the URL from ``base_url`` + attachment ID
        instead of trusting the ``content`` URL Jira returns, because that URL
        may point at a site host that doesn't accept our cloud OAuth bearer.
        """
        session = await self._ensure_session()
        url = f"{self._base_url}/rest/api/3/attachment/content/{attachment_id}"
        # Disable redirects so we can follow them with the same auth header.
        async with session.get(url, allow_redirects=True) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def get_transitions(self, issue_key: str) -> list[dict[str, Any]]:
        """Get available transitions for an issue."""
        session = await self._ensure_session()
        url = f"{self._base_url}/rest/api/3/issue/{issue_key}/transitions"
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return data.get("transitions", [])

    async def transition_issue(self, issue_key: str, transition_id: str) -> bool:
        """Transition an issue to a new status."""
        session = await self._ensure_session()
        url = f"{self._base_url}/rest/api/3/issue/{issue_key}/transitions"
        payload = {"transition": {"id": transition_id}}
        async with session.post(url, json=payload) as resp:
            if resp.status == 204:
                return True
            _log.warning(
                "transition %s to %s failed: %d",
                issue_key,
                transition_id,
                resp.status,
            )
            return False

    async def add_comment(self, issue_key: str, body: str) -> bool:
        """Add a comment to an issue using ADF (Atlassian Document Format)."""
        session = await self._ensure_session()
        url = f"{self._base_url}/rest/api/3/issue/{issue_key}/comment"
        payload = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": body}],
                    }
                ],
            }
        }
        async with session.post(url, json=payload) as resp:
            if resp.status in (200, 201):
                return True
            _log.warning(
                "comment on %s failed: %d",
                issue_key,
                resp.status,
            )
            return False

    async def get_myself(self) -> dict[str, Any]:
        """Fetch the authenticated user's profile (accountId, displayName, etc.)."""
        session = await self._ensure_session()
        url = f"{self._base_url}/rest/api/3/myself"
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def assign_issue(self, issue_key: str, account_id: str) -> bool:
        """Assign a Jira issue to a user by accountId."""
        session = await self._ensure_session()
        url = f"{self._base_url}/rest/api/3/issue/{issue_key}/assignee"
        async with session.put(url, json={"accountId": account_id}) as resp:
            if resp.status == 204:
                return True
            _log.warning(
                "assign %s to %s failed: %d",
                issue_key,
                account_id,
                resp.status,
            )
            return False

    async def create_issue(
        self,
        project: str,
        summary: str,
        description: str,
        issue_type: str = "Task",
        priority: str = "Medium",
    ) -> dict[str, Any]:
        """Create a Jira issue. Returns the created issue dict with 'key' and 'id'."""
        session = await self._ensure_session()
        url = f"{self._base_url}/rest/api/3/issue"
        payload: dict[str, Any] = {
            "fields": {
                "project": {"key": project},
                "summary": summary,
                "issuetype": {"name": issue_type},
                "priority": {"name": priority},
            }
        }
        if description:
            payload["fields"]["description"] = {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": description}],
                    }
                ],
            }
        async with session.post(url, json=payload) as resp:
            resp.raise_for_status()
            return await resp.json()


class JiraSyncService:
    """Two-way sync between Jira and Swarm's task board."""

    def __init__(
        self,
        config: JiraConfig,
        token_manager: JiraTokenManager | None = None,
        uploads_dir: str | Path | None = None,
    ) -> None:
        from pathlib import Path as _Path

        self._config = config
        self._token_manager = token_manager
        self.client = JiraClient(config, token_manager)
        self.stats = JiraSyncStats()
        self._running = False
        self._uploads_dir = (
            _Path(str(uploads_dir)) if uploads_dir else _Path.home() / ".swarm" / "uploads"
        )

    @property
    def enabled(self) -> bool:
        return (
            self._config.enabled
            and self._token_manager is not None
            and self._token_manager.is_connected()
        )

    async def close(self) -> None:
        self._running = False
        await self.client.close()

    def _record_error(self, context: str, exc: Exception) -> None:
        """Stamp a failed Jira API call onto the sync stats and log a warning.

        Consolidates the identical ``last_error`` / ``errors`` / log triple
        that every API wrapper repeats in its ``except`` handler.
        """
        self.stats.last_error = str(exc)
        self.stats.errors += 1
        _log.warning("Jira %s failed: %s", context, exc)

    # --- Import: Jira → Swarm ---

    def build_jql(self) -> str:
        """Build the JQL query string for importing issues."""
        jql = self._config.import_filter
        if not jql and self._config.project:
            jql = f"project = {self._config.project}"
        # Apply lookback window when there's no explicit filter
        lookback = self._config.lookback_days
        if not jql and not self._config.import_label:
            if lookback > 0:
                jql = f"created >= -{lookback}d"
        # Strip any ORDER BY from the filter so we can safely append
        # clauses and re-add it at the very end.
        order_by = ""
        if jql:
            m = _ORDER_BY_RE.search(jql)
            if m:
                order_by = m.group(0)
                jql = jql[: m.start()]
        # Include label in JQL for server-side filtering; client-side
        # filter remains as a case-insensitive safety net.
        if self._config.import_label and "labels" not in (jql or "").lower():
            # Escape so a label containing a backslash or double-quote can't
            # break out of the JQL string literal.
            escaped_label = self._config.import_label.replace("\\", "\\\\").replace('"', '\\"')
            label_clause = f'labels = "{escaped_label}"'
            jql = f"{label_clause} AND {jql}" if jql else label_clause
        # Always exclude completed issues unless the user's custom filter
        # already handles statusCategory.
        if "statuscategory" not in (jql or "").lower():
            done_clause = "statusCategory != Done"
            jql = f"{jql} AND {done_clause}" if jql else done_clause
        if not order_by:
            order_by = " ORDER BY created DESC"
        jql += order_by
        return jql

    async def import_issues(self, existing_tasks: dict[str, SwarmTask]) -> list[SwarmTask]:
        """Fetch issues from Jira and return new SwarmTasks to create.

        Deduplicates by checking ``jira_key`` against existing tasks.
        """
        if not self.enabled:
            return []

        jql = self.build_jql()

        try:
            issues = await self.client.search_issues(jql)
        except (aiohttp.ClientError, TimeoutError) as e:
            self._record_error("import", e)
            return []

        # Build set of existing jira_keys for dedup
        known_keys = {t.jira_key for t in existing_tasks.values() if t.jira_key}

        # Optional case-insensitive label filter (client-side)
        label_filter = self._config.import_label.lower() if self._config.import_label else ""

        new_tasks: list[SwarmTask] = []
        for issue in issues:
            key = issue.get("key", "")
            if not key or key in known_keys:
                continue

            # Apply label filter case-insensitively
            if label_filter:
                issue_labels = [lbl.lower() for lbl in issue.get("fields", {}).get("labels", [])]
                if label_filter not in issue_labels:
                    continue

            fields = issue.get("fields", {})
            task = _jira_issue_to_task(key, fields)
            await self._enrich_task_from_fields(task, fields)
            new_tasks.append(task)
            known_keys.add(key)

        self.stats.total_imported += len(new_tasks)
        self.stats.last_sync = time.time()
        self.stats.total_syncs += 1

        if new_tasks:
            _log.info("imported %d new tasks from Jira", len(new_tasks))
        return new_tasks

    async def import_one(
        self,
        issue_key: str,
        existing_keys: set[str] | None = None,
    ) -> SwarmTask | None:
        """Fetch a single Jira issue and return it as a SwarmTask.

        Used by the drag-and-drop import path so the operator can pull a
        specific ticket without waiting for the periodic sync. Skips if a task
        with this ``jira_key`` already exists (caller handles user feedback).
        """
        if not self.enabled:
            return None
        existing_keys = existing_keys or set()
        if issue_key in existing_keys:
            return None

        try:
            issue = await self.client.get_issue(issue_key)
        except (aiohttp.ClientError, TimeoutError) as e:
            self._record_error(f"import_one({issue_key})", e)
            return None

        fields = issue.get("fields", {}) or {}
        task = _jira_issue_to_task(issue_key, fields)
        await self._enrich_task_from_fields(task, fields)
        self.stats.total_imported += 1
        self.stats.last_sync = time.time()
        return task

    async def _enrich_task_from_fields(self, task: SwarmTask, fields: dict[str, Any]) -> None:
        """Mirror Jira attachments + comments onto a SwarmTask in-place.

        Downloads each attachment to the uploads dir, appends paths to
        ``task.attachments``, and rewrites ``task.description`` so the synced
        block (comments + attachment list + local paths) sits below the
        original Jira description body.
        """
        # task.description currently holds only the extracted body text from
        # the issue's description ADF — strip any prior sync tail just in
        # case (defensive; new tasks won't have one).
        base_desc = _strip_sync_tail(task.description).rstrip()

        downloaded: list[str] = []
        attachments_field = fields.get("attachment")
        if isinstance(attachments_field, list):
            for att in attachments_field:
                if not isinstance(att, dict):
                    continue
                att_id = str(att.get("id", "")).strip()
                filename = str(att.get("filename", "")).strip() or f"attachment-{att_id}"
                if not att_id:
                    continue
                try:
                    data = await self.client.download_attachment(att_id)
                except (aiohttp.ClientError, TimeoutError) as e:
                    _log.warning(
                        "failed to download attachment %s (%s) for %s: %s",
                        att_id,
                        filename,
                        task.jira_key,
                        e,
                    )
                    continue
                if not data:
                    continue
                try:
                    path = _save_attachment_bytes(filename, data, self._uploads_dir)
                except OSError as e:
                    _log.warning(
                        "failed to save attachment %s for %s: %s",
                        filename,
                        task.jira_key,
                        e,
                    )
                    continue
                downloaded.append(path)

        task.attachments = downloaded
        task.description = _build_synced_description(base_desc, fields, downloaded)

    async def refresh_task(self, task: SwarmTask) -> bool:
        """Re-fetch a Jira issue and rewrite the task's description + attachments.

        Returns ``True`` if the task was updated. Used by the manual refresh
        endpoint so users can pull comments/attachments into tasks that were
        imported before this sync was added.
        """
        if not self.enabled or not task.jira_key:
            return False
        try:
            issue = await self.client.get_issue(task.jira_key)
        except (aiohttp.ClientError, TimeoutError) as e:
            self._record_error(f"refresh {task.jira_key}", e)
            return False

        fields = issue.get("fields", {}) or {}

        # Re-derive base description from the freshly-fetched issue body
        # (the user may have edited the Jira description since import).
        raw_desc = fields.get("description")
        task.description = _extract_text(raw_desc) if raw_desc else ""

        await self._enrich_task_from_fields(task, fields)
        return True

    # --- Export: Swarm → Jira ---

    async def export_status(self, task: SwarmTask, new_status: TaskStatus) -> bool:
        """Update a Jira ticket's status to match the Swarm task status."""
        if not self.enabled or not task.jira_key:
            return False

        target_name = self._config.status_map.get(new_status.value, "")
        if not target_name:
            _log.debug(
                "no Jira status mapping for %s",
                new_status.value,
            )
            return False

        try:
            transitions = await self.client.get_transitions(task.jira_key)
        except (aiohttp.ClientError, TimeoutError) as e:
            self._record_error(f"get transitions for {task.jira_key}", e)
            return False

        # Find transition matching target status name
        transition_id = _find_transition(transitions, target_name)
        if not transition_id:
            _log.warning(
                "no transition to '%s' found for %s (available: %s)",
                target_name,
                task.jira_key,
                [t.get("name", "") for t in transitions],
            )
            return False

        try:
            ok = await self.client.transition_issue(
                task.jira_key,
                transition_id,
            )
        except (aiohttp.ClientError, TimeoutError) as e:
            self._record_error(f"transition {task.jira_key}", e)
            return False

        if ok:
            self.stats.total_exported += 1
            _log.info(
                "transitioned %s to '%s'",
                task.jira_key,
                target_name,
            )
        return ok

    async def post_completion_comment(self, task: SwarmTask) -> bool:
        """Post a completion summary as a Jira comment.

        The comment includes a non-technical summary (task title) for end
        users and the full technical resolution for developers.
        """
        if not self.enabled or not task.jira_key:
            return False

        parts = ["*Task completed in Swarm.*"]
        if task.title:
            parts.append(f"*Summary:* {task.title} — done.")
        if task.assigned_worker:
            parts.append(f"*Worker:* {task.assigned_worker}")
        if task.resolution:
            parts.append(f"\n----\n*Technical Resolution:*\n{task.resolution}")

        body = "\n".join(parts)

        try:
            ok = await self.client.add_comment(task.jira_key, body)
            if ok:
                _log.info("posted completion comment on %s", task.jira_key)
            return ok
        except (aiohttp.ClientError, TimeoutError) as e:
            self._record_error(f"comment on {task.jira_key}", e)
            return False

    async def assign_to_me(self, task: SwarmTask) -> bool:
        """Assign a Jira issue to the authenticated user."""
        if not self.enabled or not task.jira_key:
            return False

        account_id = self._token_manager.account_id if self._token_manager else ""
        if not account_id:
            _log.warning("cannot assign %s — no account_id available", task.jira_key)
            return False

        try:
            ok = await self.client.assign_issue(task.jira_key, account_id)
            if ok:
                _log.info("assigned %s to current user", task.jira_key)
            return ok
        except (aiohttp.ClientError, TimeoutError) as e:
            self._record_error(f"assign {task.jira_key}", e)
            return False

    async def create_jira_issue(self, task: SwarmTask) -> str:
        """Create a Jira issue from a Swarm task. Returns the Jira key.

        Raises RuntimeError if Jira is not enabled.
        """
        if not self.enabled:
            raise RuntimeError("Jira integration is not enabled")

        issue_type = _SWARM_TYPE_TO_JIRA.get(task.task_type, "Task")
        priority = _SWARM_PRIORITY_TO_JIRA.get(task.priority, "Medium")

        result = await self.client.create_issue(
            project=self._config.project,
            summary=task.title,
            description=task.description,
            issue_type=issue_type,
            priority=priority,
        )
        key = result.get("key", "")
        if key:
            self.stats.total_exported += 1
            _log.info("created Jira issue %s from task %s", key, task.id[:8])
        return key

    def get_status(self) -> dict[str, Any]:
        """Return sync status for API/WS."""
        return {
            "enabled": self.enabled,
            "project": self._config.project,
            "last_sync": self.stats.last_sync,
            "total_syncs": self.stats.total_syncs,
            "total_imported": self.stats.total_imported,
            "total_exported": self.stats.total_exported,
            "errors": self.stats.errors,
            "last_error": self.stats.last_error,
        }


# --- Helpers ---


def _format_comment_author(author: dict[str, Any] | None) -> str:
    """Pull a display name out of a Jira comment ``author`` block."""
    if not isinstance(author, dict):
        return "Unknown"
    return author.get("displayName") or author.get("emailAddress") or "Unknown"


def _format_comment_timestamp(raw: str) -> str:
    """Best-effort pretty timestamp for a Jira comment.

    Jira returns ISO-8601 with milliseconds + offset (e.g. ``2026-03-30T12:02:11.123-0400``).
    Falls back to the raw string if parsing fails.
    """
    if not raw:
        return ""
    from datetime import datetime

    # datetime.fromisoformat doesn't handle Jira's tz format reliably across
    # versions, so try a few common shapes before giving up.
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return raw


def _format_comments(comment_field: object) -> str:
    """Render Jira comments as a plain-text block.

    ``comment_field`` is the value of issue ``fields.comment`` from the REST
    API — a dict with a ``comments`` list. Returns an empty string when no
    comments are present.
    """
    if not isinstance(comment_field, dict):
        return ""
    comments = comment_field.get("comments")
    if not isinstance(comments, list) or not comments:
        return ""
    lines: list[str] = []
    for c in comments:
        if not isinstance(c, dict):
            continue
        author = _format_comment_author(c.get("author"))
        when = _format_comment_timestamp(str(c.get("created", "")))
        body = _extract_text(c.get("body", "")).strip()
        if not body:
            continue
        header = f"[{when}] {author}:" if when else f"{author}:"
        lines.append(header)
        lines.append(body)
        lines.append("")  # blank line between comments
    return "\n".join(lines).rstrip()


def _format_attachment_list(attachment_field: object) -> str:
    """Render Jira attachment metadata as a bullet list (filenames only)."""
    if not isinstance(attachment_field, list) or not attachment_field:
        return ""
    names: list[str] = []
    for att in attachment_field:
        if isinstance(att, dict):
            name = att.get("filename") or att.get("id") or ""
            if name:
                names.append(f"- {name}")
    return "\n".join(names)


def _truncate(text: str, limit: int) -> str:
    """Trim *text* to *limit* characters, marking the cut with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


def _strip_sync_tail(description: str) -> str:
    """Return the user-authored portion of a description, dropping any prior sync tail."""
    idx = description.find(_JIRA_SYNC_MARKER)
    if idx == -1:
        return description
    return description[:idx]


def _build_synced_description(
    base_description: str,
    fields: dict[str, Any],
    attachment_paths: list[str],
) -> str:
    """Compose a description block: original body + comments + attachment list."""
    parts: list[str] = []
    if base_description:
        parts.append(base_description.rstrip())

    sync_sections: list[str] = []
    comments_text = _format_comments(fields.get("comment"))
    if comments_text:
        sync_sections.append("Comments:\n" + comments_text)

    attachment_text = _format_attachment_list(fields.get("attachment"))
    if attachment_text:
        sync_sections.append("Attachments:\n" + attachment_text)

    if attachment_paths:
        local_lines = "\n".join(f"- {p}" for p in attachment_paths)
        sync_sections.append("Local attachment paths:\n" + local_lines)

    if sync_sections:
        # Strip the marker prefix newlines for the join, then re-add as prefix.
        synced = _JIRA_SYNC_MARKER.lstrip("\n") + "\n\n".join(sync_sections)
        parts.append("\n\n" + synced)

    full = "".join(parts)
    return _truncate(full, _DESC_BUDGET)


def _save_attachment_bytes(filename: str, data: bytes, uploads_dir: str | Path) -> str:
    """Persist *data* to *uploads_dir* using a content-addressed filename.

    Mirrors :meth:`swarm.server.email_service.EmailService.save_attachment`
    so attachments downloaded from Jira live alongside email/manual uploads.
    """
    import hashlib as _hashlib
    from pathlib import Path as _Path

    base_dir = _Path(str(uploads_dir)).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    digest = _hashlib.sha256(data).hexdigest()[:_DIGEST_LEN]
    base = _Path(filename).name
    safe_name = _SAFE_FILENAME_RE.sub("_", base).strip("_") or "attachment"
    dest = (base_dir / f"{digest}_{safe_name}").resolve()
    if not dest.is_relative_to(base_dir):
        raise ValueError(f"Upload path escapes uploads directory: {dest}")
    dest.write_bytes(data)
    return str(dest)


def _jira_issue_to_task(key: str, fields: dict[str, Any]) -> SwarmTask:
    """Convert a Jira issue's fields to a SwarmTask."""
    summary = fields.get("summary", key)

    # Extract plain-text description from ADF or string
    raw_desc = fields.get("description")
    description = _extract_text(raw_desc) if raw_desc else ""

    # Map issue type
    issue_type_name = ""
    issue_type = fields.get("issuetype")
    if isinstance(issue_type, dict):
        issue_type_name = issue_type.get("name", "").lower()
    task_type = _JIRA_TYPE_MAP.get(issue_type_name, TaskType.CHORE)

    # Map priority
    priority_name = ""
    priority = fields.get("priority")
    if isinstance(priority, dict):
        priority_name = priority.get("name", "").lower()
    task_priority = _JIRA_PRIORITY_MAP.get(
        priority_name,
        TaskPriority.NORMAL,
    )

    return SwarmTask(
        title=summary,
        description=description,
        jira_key=key,
        task_type=task_type,
        priority=task_priority,
    )


def _extract_text(adf: str | dict[str, object]) -> str:
    """Convert an ADF document (or plain string) to Markdown.

    Preserves paragraph breaks, headings, lists, blockquotes, code blocks,
    rules, and inline marks (bold, italic, code, strike, links) so the
    swarm task description reads like the source Jira issue instead of one
    space-joined run-on paragraph.
    """
    if isinstance(adf, str):
        return adf
    if not isinstance(adf, dict):
        return ""
    state: _AdfState = {"out": [""], "list_stack": []}
    _walk_adf(adf, state)
    text = "\n".join(state["out"])
    text = _TRAILING_WS_RE.sub("\n", text)
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip()


_AdfState = dict[str, Any]  # {out: list[str], list_stack: list[dict]}


def _adf_cur(state: _AdfState) -> str:
    return state["out"][-1]


def _adf_set(state: _AdfState, line: str) -> None:
    state["out"][-1] = line


def _adf_push(state: _AdfState) -> None:
    state["out"].append("")


def _adf_blank_before(state: _AdfState) -> None:
    if len(state["out"]) == 1 and state["out"][0] == "":
        return
    if _adf_cur(state) != "":
        _adf_push(state)
    if len(state["out"]) >= 2 and state["out"][-2] != "":
        _adf_push(state)


def _adf_apply_marks(text: str, marks: list[dict[str, Any]]) -> str:
    """Wrap *text* with the inline marks present on an ADF text node."""
    for m in marks:
        mtype = m.get("type")
        if mtype in ("strong", "bold"):
            text = f"**{text}**"
        elif mtype in ("em", "italic"):
            text = f"*{text}*"
        elif mtype == "code":
            text = f"`{text}`"
        elif mtype in ("strike", "strikethrough"):
            text = f"~~{text}~~"
        elif mtype == "link":
            href = (m.get("attrs") or {}).get("href") or ""
            if href:
                text = f"[{text}]({href})"
    return text


def _adf_emit_text(node: dict[str, Any], state: _AdfState) -> None:
    text = str(node.get("text", ""))
    marks = node.get("marks") or []
    _adf_set(state, _adf_cur(state) + _adf_apply_marks(text, marks))


def _adf_emit_rule(state: _AdfState) -> None:
    _adf_blank_before(state)
    _adf_push(state)
    _adf_set(state, "---")
    _adf_push(state)


def _adf_emit_paragraph(node: dict[str, Any], state: _AdfState) -> None:
    _adf_blank_before(state)
    _adf_push(state)
    _walk_adf(node.get("content", []) or [], state)
    if _adf_cur(state) != "":
        _adf_push(state)


def _adf_emit_heading(node: dict[str, Any], state: _AdfState) -> None:
    level = int((node.get("attrs") or {}).get("level", 1))
    level = max(1, min(level, 6))
    _adf_blank_before(state)
    _adf_push(state)
    _adf_set(state, "#" * level + " ")
    _walk_adf(node.get("content", []) or [], state)
    _adf_push(state)


def _adf_emit_list(node: dict[str, Any], state: _AdfState) -> None:
    ntype = node.get("type", "")
    _adf_blank_before(state)
    _adf_push(state)
    state["list_stack"].append({"type": ntype, "idx": 0})
    for child in node.get("content", []) or []:
        if not (isinstance(child, dict) and child.get("type") == "listItem"):
            continue
        _adf_emit_list_item(child, state)
    state["list_stack"].pop()
    if not state["list_stack"]:
        _adf_push(state)


def _adf_emit_list_item(item: dict[str, Any], state: _AdfState) -> None:
    """Emit one list item. The first paragraph (the most common shape for a
    list item's content) is unwrapped so its inline text lands on the same
    line as the bullet marker. Any subsequent block-level children render
    normally (lifted under the bullet)."""
    top = state["list_stack"][-1]
    indent = "  " * (len(state["list_stack"]) - 1)
    if top["type"] == "orderedList":
        top["idx"] += 1
        marker = f"{top['idx']}. "
    else:
        marker = "- "
    if _adf_cur(state) != "":
        _adf_push(state)
    _adf_set(state, indent + marker)

    children = item.get("content", []) or []
    if children and isinstance(children[0], dict) and children[0].get("type") == "paragraph":
        _walk_adf(children[0].get("content", []) or [], state)
        rest = children[1:]
    else:
        rest = list(children)
    _walk_adf(rest, state)
    if _adf_cur(state) != "":
        _adf_push(state)


def _adf_emit_blockquote(node: dict[str, Any], state: _AdfState) -> None:
    _adf_blank_before(state)
    _adf_push(state)
    start = len(state["out"]) - 1
    _walk_adf(node.get("content", []) or [], state)
    if _adf_cur(state) != "":
        _adf_push(state)
    for i in range(start, len(state["out"])):
        if state["out"][i] != "":
            state["out"][i] = "> " + state["out"][i]
    _adf_push(state)


def _adf_emit_code_block(node: dict[str, Any], state: _AdfState) -> None:
    _adf_blank_before(state)
    _adf_push(state)
    lang = (node.get("attrs") or {}).get("language", "")
    _adf_set(state, "```" + (lang or ""))
    _adf_push(state)
    code_text = "".join(
        str(c.get("text", ""))
        for c in (node.get("content", []) or [])
        if isinstance(c, dict) and c.get("type") == "text"
    )
    for line in code_text.split("\n"):
        _adf_set(state, line)
        _adf_push(state)
    _adf_set(state, "```")
    _adf_push(state)


def _adf_emit_mention(node: dict[str, Any], state: _AdfState) -> None:
    attrs = node.get("attrs") or {}
    text = attrs.get("text") or attrs.get("displayName") or attrs.get("id", "")
    if text:
        _adf_set(state, _adf_cur(state) + f"@{text}")


def _adf_emit_emoji(node: dict[str, Any], state: _AdfState) -> None:
    attrs = node.get("attrs") or {}
    shortname = attrs.get("shortName") or attrs.get("text") or ""
    if shortname:
        _adf_set(state, _adf_cur(state) + shortname)


def _adf_emit_inline_card(node: dict[str, Any], state: _AdfState) -> None:
    href = (node.get("attrs") or {}).get("url", "")
    if href:
        _adf_set(state, _adf_cur(state) + f"<{href}>")


def _adf_emit_status(node: dict[str, Any], state: _AdfState) -> None:
    """Emit an inline status badge's label.

    The label lives in ``attrs.text`` with no content children, so the
    generic fallback (descend into ``content``) would drop it silently.
    """
    text = str((node.get("attrs") or {}).get("text") or "")
    if text:
        _adf_set(state, _adf_cur(state) + text)


def _adf_emit_date(node: dict[str, Any], state: _AdfState) -> None:
    """Emit an inline date node as an ISO ``YYYY-MM-DD`` (UTC).

    A date node carries only an epoch-millis ``attrs.timestamp`` and no text,
    so without this the value is lost. Falls back to the raw value if the
    timestamp can't be parsed.
    """
    raw = str((node.get("attrs") or {}).get("timestamp") or "").strip()
    if not raw:
        return
    from datetime import UTC, datetime

    try:
        rendered = datetime.fromtimestamp(int(raw) / 1000, tz=UTC).strftime("%Y-%m-%d")
    except (ValueError, OverflowError, OSError):
        rendered = raw
    _adf_set(state, _adf_cur(state) + rendered)


_ADF_HANDLERS: dict[str, Any] = {
    "text": lambda node, state: _adf_emit_text(node, state),
    "hardBreak": lambda node, state: _adf_push(state),
    "rule": lambda node, state: _adf_emit_rule(state),
    "paragraph": _adf_emit_paragraph,
    "heading": _adf_emit_heading,
    "bulletList": _adf_emit_list,
    "orderedList": _adf_emit_list,
    "blockquote": _adf_emit_blockquote,
    "codeBlock": _adf_emit_code_block,
    "mention": _adf_emit_mention,
    "emoji": _adf_emit_emoji,
    "inlineCard": _adf_emit_inline_card,
    "status": _adf_emit_status,
    "date": _adf_emit_date,
}


def _walk_adf(node: Any, state: _AdfState) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_adf(item, state)
        return
    if not isinstance(node, dict):
        return
    handler = _ADF_HANDLERS.get(node.get("type", ""))
    if handler is not None:
        handler(node, state)
        return
    # Unknown / pass-through container — descend into children.
    _walk_adf(node.get("content", []) or [], state)


def _find_transition(transitions: list[dict[str, Any]], target_name: str) -> str | None:
    """Find a transition ID whose target status matches the name."""
    target_lower = target_name.lower()
    for t in transitions:
        name = t.get("name", "").lower()
        if name == target_lower:
            return t.get("id", "")
        # Also check the "to" status name
        to_status = t.get("to", {})
        if isinstance(to_status, dict):
            to_name = to_status.get("name", "").lower()
            if to_name == target_lower:
                return t.get("id", "")
    return None
