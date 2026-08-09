"""Jira integration — two-way sync between Jira and Swarm task board."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp

from swarm.config import JiraConfig
from swarm.integrations.retry import is_transient_status, retry_transient
from swarm.logging import get_logger
from swarm.tasks.task import (
    JIRA_SYNC_MARKER,
    SwarmTask,
    TaskPriority,
    TaskStatus,
    TaskType,
)

if TYPE_CHECKING:
    from pathlib import Path

    from swarm.auth.jira import JiraTokenManager

_log = get_logger("integrations.jira")

# Marker that delimits the auto-synced tail of a Jira-imported description.
# Anything after this line is regenerated on every refresh.
_JIRA_SYNC_MARKER = JIRA_SYNC_MARKER

# Field list requested from the Jira REST search/get APIs. Includes comment
# and attachment so we can mirror them into the Swarm task on import.
# Swarm statuses that mean "finished". Only for these does an already-terminal Jira
# ticket count as agreement — a done-category ticket while Swarm says ACTIVE is a real
# divergence, not a match.
_TERMINAL_STATUSES = (TaskStatus.DONE, TaskStatus.FAILED)

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

    async def search_issues(
        self, jql: str, max_results: int = 50, fields: str = ""
    ) -> list[dict[str, Any]]:
        """Search Jira issues using JQL.

        Returns a list of issue dicts with key, summary, description, etc.

        ``fields`` overrides the import field set. It exists because the default does
        NOT include ``assignee``: a caller that needs it would silently receive issues
        without it and read the absence as "no result" rather than as "I did not ask".
        """
        session = await self._ensure_session()
        url = f"{self._base_url}/rest/api/3/search/jql"
        params: dict[str, Any] = {
            "jql": jql,
            "maxResults": max_results,
            "fields": fields or _JIRA_ISSUE_FIELDS,
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

    async def get_project_statuses(self, project_key: str) -> list[dict[str, Any]]:
        """Every status the project's workflows use, grouped by issue type.

        Discovery for the setup flow (v2 phase 2). Asked at the PROJECT level rather
        than per issue, because ``get_transitions`` only reports transitions available
        from one issue's CURRENT state — which is how a status map can look complete
        while being wrong for every ticket that is not in that state. On 2026-08-07 a
        hardcoded map targeting "Done" was refused by 11 real tickets whose workflow
        offered only "Waiting for support"; nothing could see that until it failed.
        """
        session = await self._ensure_session()
        url = f"{self._base_url}/rest/api/3/project/{project_key}/statuses"
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return data if isinstance(data, list) else []

    async def transition_issue(self, issue_key: str, transition_id: str) -> bool:
        """Transition an issue to a new status. Retries transient failures —
        a lost transition leaves Swarm and Jira permanently out of sync."""
        session = await self._ensure_session()
        url = f"{self._base_url}/rest/api/3/issue/{issue_key}/transitions"
        payload = {"transition": {"id": transition_id}}

        async def _do() -> bool:
            async with session.post(url, json=payload) as resp:
                if resp.status == 204:
                    return True
                if is_transient_status(resp.status):
                    resp.raise_for_status()
                _log.warning(
                    "transition %s to %s failed: %d",
                    issue_key,
                    transition_id,
                    resp.status,
                )
                return False

        try:
            return await retry_transient(_do, what=f"jira transition {issue_key}")
        except (aiohttp.ClientError, TimeoutError):
            _log.warning("transition %s failed after retries", issue_key, exc_info=True)
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

        async def _do() -> bool:
            async with session.post(url, json=payload) as resp:
                if resp.status in (200, 201):
                    return True
                if is_transient_status(resp.status):
                    resp.raise_for_status()
                _log.warning(
                    "comment on %s failed: %d",
                    issue_key,
                    resp.status,
                )
                return False

        try:
            return await retry_transient(_do, what=f"jira comment {issue_key}")
        except (aiohttp.ClientError, TimeoutError):
            _log.warning("comment on %s failed after retries", issue_key, exc_info=True)
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

        async def _do() -> bool:
            async with session.put(url, json={"accountId": account_id}) as resp:
                if resp.status == 204:
                    return True
                if is_transient_status(resp.status):
                    resp.raise_for_status()
                _log.warning(
                    "assign %s to %s failed: %d",
                    issue_key,
                    account_id,
                    resp.status,
                )
                return False

        try:
            return await retry_transient(_do, what=f"jira assign {issue_key}")
        except (aiohttp.ClientError, TimeoutError):
            _log.warning("assign %s failed after retries", issue_key, exc_info=True)
            return False

    async def create_issue(
        self,
        project: str,
        summary: str,
        description: str,
        issue_type: str = "Task",
        priority: str = "Medium",
        labels: list[str] | None = None,
        assignee_account_id: str = "",
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
        if labels:
            payload["fields"]["labels"] = list(labels)
        if assignee_account_id:
            payload["fields"]["assignee"] = {"accountId": assignee_account_id}
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

        async def _do() -> dict[str, Any]:
            async with session.post(url, json=payload) as resp:
                resp.raise_for_status()
                return await resp.json()

        return await retry_transient(_do, what=f"jira create issue in {project}")


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
        # (project, swarm status) pairs already warned about — see export_status.
        self._unmapped_warned: set[tuple[str, str]] = set()
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
        """JQL for importing this dev's work. Routed by ASSIGNEE, scoped to projects.

        v2 (docs/specs/jira-integration-v2.md). The previous query routed by
        ``labels = "swarm"``, which does not survive the integration being enabled for
        every dev: each swarm would import the SAME tickets, create duplicate tasks for
        one issue, and race to transition it. ``assignee = currentUser()`` gives one
        answer to "who owns this" in both systems, uses semantics Jira already has, and
        needs no per-dev labelling ritual anyone can forget.

        ``statusCategory != Done`` is deliberately the terminal test: statusCategory is
        a universal three-value field (To Do / In Progress / Done) valid in ANY
        workflow, so "not finished" needs no per-project discovery. Discovery is still
        required for the EXPORT transition map — that is where a hardcoded "Done" was
        refused by 11 real tickets whose workflow only offered "Waiting for support".

        Returns "" when no project is configured, which imports NOTHING. Importing
        everything by default would put a whole Jira site on one dev's board.
        """
        projects = self._config.active_projects()
        if not projects:
            return ""

        # No legacy-filter warning here any more: import_filter and import_label were
        # DELETED rather than left disabled (2026.8.8.7). A setting that exists and does
        # nothing is worse than one that is gone — the operator can still see it, so it
        # reads as configuration when it is decoration. Older configs carrying those keys
        # are reported by the loader's stale-key check instead.

        def _quote(value: str) -> str:
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

        clauses = [
            f"project IN ({', '.join(_quote(p) for p in projects)})",
            "assignee = currentUser()",
            "statusCategory != Done",
        ]
        types = [t for t in self._config.issue_types if t]
        if types:
            clauses.append(f"issuetype IN ({', '.join(_quote(t) for t in types)})")
        return " AND ".join(clauses) + " ORDER BY created DESC"

    async def discover_workflow(self, project_key: str) -> dict[str, Any]:
        """Read a project's real workflow and PROPOSE a status map for confirmation.

        The setup flow's core call (v2 phase 2). Returns the project's status
        vocabulary, a proposed ``swarm status -> jira status`` map, the terminal names,
        and any Swarm status the project offers no plausible target for.

        WRITES NOTHING and CONFIRMS NOTHING. Done / Resolved / Closed are rarely
        interchangeable, and a wrong automatic choice transitions someone's ticket to
        the wrong state while reporting success. The operator confirms; this only shows
        them what is actually there — which is precisely what nobody could see when a
        hardcoded "Done" was refused by 11 real tickets.
        """
        from swarm.integrations.jira_workflow import (
            flatten_statuses,
            propose_status_map,
            terminal_status_names,
        )

        raw = await self.client.get_project_statuses(project_key)
        statuses = flatten_statuses(raw)
        proposed = propose_status_map(statuses)
        return {
            "project": project_key,
            "statuses": statuses,
            "proposed_status_map": proposed,
            "terminal_statuses": terminal_status_names(statuses),
            # Named explicitly rather than left as an absence: a Swarm status with no
            # target is exactly the case that failed silently before, and the operator
            # needs to see it on the confirmation screen rather than discover it when an
            # export is refused.
            "unmapped": sorted(
                s
                for s in (
                    "backlog",
                    "unassigned",
                    "assigned",
                    "active",
                    "blocked",
                    "done",
                    "failed",
                )
                if s not in proposed
            ),
        }

    async def import_issues(
        self,
        existing_tasks: dict[str, SwarmTask],
        extra_known_keys: set[str] | None = None,
    ) -> list[SwarmTask]:
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
        # ``extra_known_keys`` carries keys the caller can see but the board cannot —
        # archived rows keep their jira_key and are excluded when the store loads, so
        # without them re-importing an archived issue creates a DUPLICATE task.
        known_keys = {t.jira_key for t in existing_tasks.values() if t.jira_key}
        if extra_known_keys:
            known_keys |= set(extra_known_keys)

        # NO CLIENT-SIDE LABEL FILTER (v2). It used to drop any issue lacking
        # import_label as a "safety net" for the label-routed JQL. Under
        # assignee-routing that net catches everything: the query returns this dev's
        # assigned work, almost none of which carries the label, so the filter would
        # discard nearly every ticket and the integration would import nothing while
        # appearing to run. `swarm` is now reserved PROVENANCE, applied to tickets
        # Swarm created — it must never decide what comes in.

        new_tasks: list[SwarmTask] = []
        for issue in issues:
            key = issue.get("key", "")
            if not key or key in known_keys:
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

    @staticmethod
    def _sync_tail(description: str) -> str:
        return description.partition(_JIRA_SYNC_MARKER)[2]

    async def refresh_synced_content(self, task: SwarmTask) -> str:
        """Re-mirror a linked ticket's comments and attachments. ADDITIVE ONLY.

        Returns the newest comment's text when the synced content CHANGED, else "".

        WHY THIS IS NOT ``refresh_task``. That one re-derives the description from the
        Jira body and REPLACES ``task.attachments`` wholesale. It is correct for a button
        a person just pressed — they asked for a full re-sync and can see the result.
        Running it on a timer would silently delete, every five minutes:

        * everything a worker wrote into the description (the #1289 truncation, but
          automated and repeating), and
        * every attachment Swarm added itself, such as a debugging screenshot.

        So this keeps whatever sits ABOVE the sync marker exactly as it is — the marker
        already separates the regenerated tail from the user-authored portion — and
        MERGES attachments instead of replacing them. It can add information; it has no
        path by which it can remove any.

        CHANGE IS DETECTED BY COMPARING THE DERIVED TAIL, not by counting comments. The
        tail is truncated at a size cap, so counting what is rendered undercounts; and
        an edited comment changes no count at all. Comparing what we would write against
        what is there answers "did anything change" for every case at once.
        """
        if not self.enabled or not task.jira_key:
            return ""
        try:
            issue = await self.client.get_issue(task.jira_key)
        except Exception:
            # A refresh that cannot read is a no-op, never a truncation.
            _log.debug("could not refresh %s", task.jira_key, exc_info=True)
            return ""

        fields = issue.get("fields", {}) or {}
        # NOT re-derived from the Jira body: that would drop worker-authored text.
        base_desc = _strip_sync_tail(task.description).rstrip()
        existing = list(task.attachments or [])
        before = self._sync_tail(task.description)

        new_paths = await self._download_attachments(task, fields)
        merged = existing + [p for p in new_paths if p not in existing]
        rebuilt = _build_synced_description(base_desc, fields, merged)
        if self._sync_tail(rebuilt) == before:
            return ""

        task.attachments = merged
        task.description = rebuilt
        return _latest_comment(fields.get("comment"))

    async def _download_attachments(self, task: SwarmTask, fields: dict[str, Any]) -> list[str]:
        """Download a ticket's attachments to the uploads dir. Returns local paths."""
        downloaded: list[str] = []
        attachments_field = fields.get("attachment")
        if not isinstance(attachments_field, list):
            return downloaded
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
                downloaded.append(_save_attachment_bytes(filename, data, self._uploads_dir))
            except OSError as e:
                _log.warning("failed to save attachment %s for %s: %s", filename, task.jira_key, e)
        return downloaded

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

        # PER-PROJECT map, keyed off the ticket's own project. A single global map is
        # what made 11 IS tickets fail while WWD succeeded: workflows differ per
        # project, so "what does done look like here" has no global answer.
        project_key = task.jira_key.split("-")[0] if "-" in task.jira_key else ""
        target_name = self._config.status_map_for(project_key).get(new_status.value, "")
        if not target_name:
            # WARNING, not DEBUG. This is a state change that silently does not happen:
            # the task moves in Swarm and the ticket does not move in Jira, with nothing
            # in the log an operator running at default level would ever see. Suppressed
            # to ONCE per (project, status) because a discovered map legitimately omits
            # states it could not justify, and a warning per transition would flood.
            if (project_key, new_status.value) not in self._unmapped_warned:
                self._unmapped_warned.add((project_key, new_status.value))
                _log.warning(
                    "jira: no confirmed mapping for %s status '%s' — %s was NOT "
                    "transitioned. Discover and confirm this project's workflow in "
                    "Settings > Integrations. (Further occurrences for this pair are "
                    "logged at debug.)",
                    project_key or "(unknown project)",
                    new_status.value,
                    task.jira_key,
                )
            else:
                _log.debug(
                    "no Jira status mapping for %s/%s",
                    project_key,
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
            # BEFORE reporting failure, ASK WHETHER JIRA IS ALREADY THERE.
            #
            # Measured on the operator's real board 2026-08-08: all 10 unacknowledged
            # IS tickets were already `Resolved` (statusCategory=done) and offered only
            # a `Waiting for support` transition — reopening. The reconciler had been
            # retrying an impossible transition every sync interval, forever, to put
            # them into a state they were already in.
            #
            # "Jira refused the transition" is not evidence Jira is in the desired
            # state — but "Jira reports statusCategory=done and Swarm says done" IS.
            # Name equality was the wrong test: this project calls it Resolved, ours
            # maps to Done, and the INTENT ("this work is finished") is satisfied by
            # either. So this records agreement instead of writing, which is the same
            # comparison-over-push move the task board and this file's reconciler
            # already make.
            if new_status in _TERMINAL_STATUSES and await self._already_terminal(task.jira_key):
                _log.info(
                    "%s is already terminal in Jira (Swarm: %s); recording agreement "
                    "without a write — no transition to '%s' exists from there",
                    task.jira_key,
                    new_status.value,
                    target_name,
                )
                return True
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

    # Provenance, reserved (v2 phase 5). Applied to tickets Swarm CREATED and to
    # nothing else. It means exactly one thing: an agent raised this.
    #
    # The trap this avoids: the old import filter was `labels = "swarm"`. Had created
    # tickets carried this label while it still drove routing, Swarm would re-import its
    # own output as a new task — an echo loop. Separating "came from Swarm" from "route
    # to Swarm" (the assignee) makes the loop impossible rather than merely deduped
    # against. Nothing reads this label; that is the point.
    PROVENANCE_LABEL = "swarm"

    def default_create_project(self) -> str:
        """Which project a newly created ticket belongs in.

        Was ``self._config.project`` — the LEGACY single-project field. On a v2 config
        that only sets ``projects`` it is empty, which Jira rejects, and on a multi-
        project config it silently pinned creation to whichever project happened to be
        in the old field. First configured project, legacy field as the fallback.
        """
        for candidate in self._config.projects or []:
            if str(candidate).strip():
                return str(candidate).strip()
        return str(self._config.project or "").strip()

    async def _my_account_id(self) -> str:
        """The authenticated dev's Jira accountId, or "" if it cannot be read.

        A ticket a worker raised is assigned to the dev whose swarm raised it, so the
        outbound rule and the assignee-routing rule agree and it round-trips home to the
        same board. Failing to resolve it is NOT fatal: an unassigned ticket that exists
        is recoverable, a promotion that failed because a lookup 500'd is just lost work.
        """
        try:
            me = await self.client.get_myself()
            account = str(me.get("accountId", "") or "")
            if account:
                return account
        except Exception:
            # Expected on installs authorized before read:jira-user was requested:
            # /myself returns 401 "scope does not match" while create and search keep
            # working on the same token. Fall through rather than give up.
            _log.debug("jira: /myself unavailable; deriving account from assigned work")

        account = await self._account_id_from_assigned_work()
        if account:
            return account

        _log.warning(
            "jira: could not resolve the authenticated account, so the created ticket "
            "will be UNASSIGNED and will not route back to this swarm. Reconnect Jira "
            "in Settings > Integrations to grant the read:jira-user permission.",
        )
        return ""

    async def _account_id_from_assigned_work(self) -> str:
        """The accountId, derived WITHOUT the read:jira-user scope.

        `assignee = currentUser()` is already how imports are routed, so this needs only
        read:jira-work — which every existing install already granted. One issue the
        current user is assigned carries their own accountId in its assignee field.

        This exists so adding a scope does not silently break every dev who authorized
        before it: their tokens keep the old scopes until they reconnect, and until then
        this keeps promoted tickets routing home. Returns "" when they have no assigned
        issue to read it from, which is the one case that genuinely needs a reconnect.
        """
        try:
            issues = await self.client.search_issues(
                "assignee = currentUser()", max_results=1, fields="assignee"
            )
        except Exception:
            _log.debug("jira: could not derive account from assigned work", exc_info=True)
            return ""
        for issue in issues or []:
            assignee = (issue.get("fields") or {}).get("assignee") or {}
            account = str(assignee.get("accountId", "") or "")
            if account:
                return account
        return ""

    async def find_reassigned(self, tasks: list[SwarmTask]) -> list[tuple[SwarmTask, str]]:
        """Tasks whose Jira ticket is NO LONGER assigned to this dev.

        Returns ``(task, new_owner_display_name)``; the new owner is "" when the ticket
        is now unassigned in Jira.

        WHY THIS IS A POSITIVE CHECK AND NOT "IT FELL OUT OF THE IMPORT QUERY".
        The import runs ``assignee = currentUser() AND statusCategory != Done``. A ticket
        disappears from those results for at least four different reasons: it was
        reassigned, it was closed, it was moved or deleted or permissions changed, or the
        call simply failed and returned fewer rows. Treating absence as reassignment
        would unassign EVERY linked task the first time Jira returned an error — an empty
        result is not a finding. So this asks Jira what the assignee actually is, and
        acts only on a definite mismatch.

        A key missing from the response is reported as NOTHING rather than as
        "unassigned": we could not see it, which is different from seeing that it changed.
        """
        import re as _re

        my_account = await self._my_account_id()
        if not my_account:
            # Cannot establish who "I" am -> cannot judge whose ticket this is. Every
            # task would look foreign and the whole board would be released. Refusing to
            # act is the only safe answer.
            _log.warning(
                "jira: cannot resolve this account, so ticket ownership cannot be "
                "checked; no tasks were released"
            )
            return []

        # Keys are validated, not escaped: anything that is not PROJ-123 never reaches
        # the query, so a malformed jira_key cannot alter its meaning.
        keys = [
            t.jira_key
            for t in tasks
            if t.jira_key and _re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", t.jira_key)
        ]
        if not keys:
            return []

        try:
            issues = await self.client.search_issues(
                f"key IN ({', '.join(keys)})", max_results=len(keys), fields="assignee"
            )
        except Exception:
            _log.warning("jira: ownership check failed; no tasks were released", exc_info=True)
            return []

        seen: dict[str, dict[str, Any]] = {}
        for issue in issues or []:
            key = str(issue.get("key", "") or "")
            if key:
                seen[key] = (issue.get("fields") or {}).get("assignee") or {}

        moved: list[tuple[SwarmTask, str]] = []
        for task in tasks:
            if task.jira_key not in seen:
                # Not visible in the response. Could be permissions, a move, a delete —
                # all of which are "I do not know", not "it is not yours".
                continue
            assignee = seen[task.jira_key]
            if str(assignee.get("accountId", "") or "") == my_account:
                continue
            moved.append((task, str(assignee.get("displayName", "") or "")))
        return moved

    async def agrees_already(self, task: SwarmTask, status: TaskStatus) -> bool:
        """True when Jira is ALREADY in the state *status* wants — a pure read.

        Public because the reconciler needs it for projects whose workflow is NOT
        confirmed. That gate exists to stop an unattended sweep from BULK WRITING to a
        shared tracker; it does not need to stop a COMPARISON. Blocking one behind the
        other left MTR-11806 — done in Swarm, already `Done` in Jira — warning on every
        sync interval, permanently, about a divergence that did not exist.
        """
        if not task.jira_key or status not in _TERMINAL_STATUSES:
            return False
        return await self._already_terminal(task.jira_key)

    async def _already_terminal(self, jira_key: str) -> bool:
        """True when the ticket is already in Jira's DONE status category.

        statusCategory is universal across every workflow (new / indeterminate / done),
        so this needs no per-project discovery — the same property that makes it the
        right test for excluding finished work from imports.

        Deliberately only consulted when a transition could NOT be found: the happy path
        costs no extra API call, and a ticket that CAN be transitioned should be, rather
        than have its current state accepted.
        """
        try:
            issue = await self.client.get_issue(jira_key)
        except Exception:
            # Cannot tell -> do not claim agreement. Silence here would convert an
            # unreachable API into a false "Jira is up to date".
            _log.debug("could not read %s to check terminal state", jira_key, exc_info=True)
            return False
        status = (issue.get("fields") or {}).get("status") or {}
        category = str((status.get("statusCategory") or {}).get("key", "")).lower()
        return category == "done"

    async def create_jira_issue(self, task: SwarmTask, *, project: str = "") -> str:
        """Create a Jira issue from a Swarm task. Returns the Jira key.

        Raises RuntimeError if Jira is not enabled or no project is configured.
        """
        if not self.enabled:
            raise RuntimeError("Jira integration is not enabled")

        target = (project or "").strip() or self.default_create_project()
        if not target:
            # Refuse rather than post an empty project key and surface Jira's own error.
            raise RuntimeError(
                "no Jira project configured — set one in Settings > Integrations "
                "before promoting a task"
            )

        issue_type = _SWARM_TYPE_TO_JIRA.get(task.task_type, "Task")
        priority = _SWARM_PRIORITY_TO_JIRA.get(task.priority, "Medium")

        result = await self.client.create_issue(
            project=target,
            summary=task.title,
            description=task.description,
            issue_type=issue_type,
            priority=priority,
            labels=[self.PROVENANCE_LABEL],
            assignee_account_id=await self._my_account_id(),
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


def _latest_comment(comment_field: object) -> str:
    """The most recent comment rendered as one line, or "".

    Used to tell a worker WHAT changed rather than merely that something did. "The
    ticket was updated" sends them to read it; "Larissa: do X instead" is the thing
    they actually needed.
    """
    if not isinstance(comment_field, dict):
        return ""
    comments = comment_field.get("comments")
    if not isinstance(comments, list) or not comments:
        return ""
    for c in reversed(comments):
        if not isinstance(c, dict):
            continue
        body = _extract_text(c.get("body", "")).strip()
        if not body:
            continue
        author = _format_comment_author(c.get("author"))
        return f"{author}: {body}"
    return ""


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
