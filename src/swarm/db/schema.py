"""Database schema definitions for swarm.db.

All table creation SQL lives here.  The schema version is tracked in
the ``schema_version`` table so future migrations can be applied
incrementally.
"""

from __future__ import annotations

CURRENT_VERSION = 23

PRAGMAS = """\
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
"""

SCHEMA_V1 = """\
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
  version     INTEGER PRIMARY KEY,
  applied_at  REAL    NOT NULL
);

-- ============================================================
-- CONFIG
-- ============================================================

CREATE TABLE IF NOT EXISTS config (
  key         TEXT PRIMARY KEY,
  value       TEXT,
  updated_at  REAL
);

CREATE TABLE IF NOT EXISTS workers (
  id          TEXT PRIMARY KEY,
  name        TEXT UNIQUE NOT NULL,
  path        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  provider    TEXT NOT NULL DEFAULT '',
  isolation   TEXT NOT NULL DEFAULT '',
  identity    TEXT NOT NULL DEFAULT '',
  sort_order  INTEGER NOT NULL DEFAULT 0,
  created_at  REAL
);

CREATE TABLE IF NOT EXISTS groups (
  id    TEXT PRIMARY KEY,
  name  TEXT UNIQUE NOT NULL,
  label TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS group_workers (
  group_id    TEXT    NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  worker_id   TEXT    NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
  sort_order  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (group_id, worker_id)
);

CREATE TABLE IF NOT EXISTS config_overrides (
  id          INTEGER PRIMARY KEY,
  owner_type  TEXT NOT NULL,
  owner_id    TEXT,
  key         TEXT NOT NULL,
  value       TEXT,
  UNIQUE(owner_type, owner_id, key)
);

CREATE TABLE IF NOT EXISTS approval_rules (
  id          INTEGER PRIMARY KEY,
  owner_type  TEXT NOT NULL DEFAULT 'global',
  owner_id    TEXT,
  pattern     TEXT NOT NULL,
  action      TEXT NOT NULL,
  sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_approval_rules_owner ON approval_rules(owner_type, owner_id);

-- ============================================================
-- TASKS
-- ============================================================

CREATE TABLE IF NOT EXISTS tasks (
  id                TEXT PRIMARY KEY,
  number            INTEGER UNIQUE,
  title             TEXT NOT NULL,
  description       TEXT NOT NULL DEFAULT '',
  status            TEXT NOT NULL DEFAULT 'unassigned',
  priority          TEXT NOT NULL DEFAULT 'normal',
  task_type         TEXT NOT NULL DEFAULT 'chore',
  assigned_worker   TEXT,
  created_at        REAL,
  updated_at        REAL,
  completed_at      REAL,
  started_at        REAL,
  dispatch_requested_at REAL,
  resolution        TEXT NOT NULL DEFAULT '',
  block_reason      TEXT NOT NULL DEFAULT '',
  external_blocker_ref TEXT NOT NULL DEFAULT '',
  tags              TEXT NOT NULL DEFAULT '[]',
  attachments       TEXT NOT NULL DEFAULT '[]',
  depends_on        TEXT NOT NULL DEFAULT '[]',
  source_email_id   TEXT,
  jira_key          TEXT,
  is_cross_project  INTEGER NOT NULL DEFAULT 0,
  source_worker     TEXT,
  target_worker     TEXT,
  dependency_type   TEXT,
  acceptance_criteria TEXT NOT NULL DEFAULT '[]',
  context_refs      TEXT NOT NULL DEFAULT '[]',
  cost_budget       REAL,
  cost_spent        REAL NOT NULL DEFAULT 0,
  learnings         TEXT NOT NULL DEFAULT '',
  verification_status        TEXT    NOT NULL DEFAULT 'not_run',
  verification_reason        TEXT    NOT NULL DEFAULT '',
  verification_reopen_count  INTEGER NOT NULL DEFAULT 0,
  effort_tier                TEXT    NOT NULL DEFAULT '',
  -- #1274: an ADDITIVE annotation on a closed task's resolution. The
  -- resolution text itself stays immutable (TaskBoard.update refuses a
  -- `resolution` kwarg at all) because rewriting it would destroy the record
  -- of what was believed at the time. kind is '' | 'stale' | 'wrong'.
  resolution_note            TEXT    NOT NULL DEFAULT '',
  resolution_note_kind       TEXT    NOT NULL DEFAULT '',
  -- #1578: the title before a correction, '' if never corrected. A title is a
  -- pointer rather than a requirement, so it IS correctable on a closed task —
  -- the corrected text replaces `title` (so every render site picks it up) and
  -- the original is preserved here plus in task_history.
  title_original             TEXT    NOT NULL DEFAULT '',
  -- #1298: soft delete. NULL = live; a timestamp = archived and hidden from the
  -- board. A HARD delete would cascade this task's task_history rows away
  -- (task_history.task_id REFERENCES tasks(id) ON DELETE CASCADE), silently
  -- destroying the audit trail that learnings and playbook synthesis read. The
  -- row survives so the cascade never fires.
  archived_at                REAL,
  -- #jira-blocker-2: the status we last SUCCESSFULLY told Jira about. The desired
  -- state is `status`; this is the last acknowledged state. When they differ, the
  -- export never landed and the reconciler retries. Without it a failed export is
  -- invisible: Jira shows a ticket open while the swarm has it done, which is exactly
  -- what the operator hit.
  jira_exported_status       TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_assigned ON tasks(assigned_worker);
CREATE INDEX IF NOT EXISTS idx_tasks_jira ON tasks(jira_key);
CREATE INDEX IF NOT EXISTS idx_tasks_assigned_status ON tasks(assigned_worker, status);

CREATE TABLE IF NOT EXISTS task_history (
  id          INTEGER PRIMARY KEY,
  task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  action      TEXT NOT NULL,
  actor       TEXT,
  detail      TEXT NOT NULL DEFAULT '',
  created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_history_task ON task_history(task_id);
CREATE INDEX IF NOT EXISTS idx_task_history_time ON task_history(created_at);

-- Swarm Next migration finalization is deliberately separate from import. The
-- receipt proves Next accepted the exact source records; these tables retain
-- the reversible Legacy-side handoff without pretending the work completed.
CREATE TABLE IF NOT EXISTS next_migration_batches (
  batch_id                   TEXT PRIMARY KEY,
  bundle_digest              TEXT UNIQUE NOT NULL,
  source_installation_id     TEXT NOT NULL,
  source_snapshot_digest     TEXT NOT NULL,
  receipt_json               TEXT NOT NULL,
  backup_path                TEXT NOT NULL,
  applied_at                 REAL NOT NULL,
  reversed_at                REAL
);

CREATE TABLE IF NOT EXISTS next_migration_tasks (
  batch_id                   TEXT NOT NULL REFERENCES next_migration_batches(batch_id),
  task_id                    TEXT NOT NULL REFERENCES tasks(id),
  original_status            TEXT NOT NULL,
  original_assigned_worker   TEXT,
  migration_task_digest      TEXT NOT NULL,
  reversed_at                REAL,
  PRIMARY KEY (batch_id, task_id)
);

CREATE INDEX IF NOT EXISTS idx_next_migration_tasks_task
  ON next_migration_tasks(task_id);

-- ============================================================
-- PROPOSALS
-- ============================================================

CREATE TABLE IF NOT EXISTS proposals (
  id                TEXT PRIMARY KEY,
  worker_name       TEXT,
  task_id           TEXT,
  task_title         TEXT,
  proposal_type     TEXT,
  status            TEXT NOT NULL DEFAULT 'pending',
  confidence        REAL,
  assessment        TEXT,
  message           TEXT,
  reasoning         TEXT,
  queen_action      TEXT,
  prompt_snippet    TEXT,
  rule_pattern      TEXT,
  is_plan           INTEGER NOT NULL DEFAULT 0,
  rejection_reason  TEXT,
  created_at        REAL,
  resolved_at       REAL,
  thread_id         TEXT
);

CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_proposals_worker ON proposals(worker_name);
CREATE INDEX IF NOT EXISTS idx_proposals_task ON proposals(task_id);
CREATE INDEX IF NOT EXISTS idx_proposals_status_time ON proposals(status, created_at);

-- ============================================================
-- BUZZ LOG
-- ============================================================

CREATE TABLE IF NOT EXISTS buzz_log (
  id              INTEGER PRIMARY KEY,
  timestamp       REAL NOT NULL,
  action          TEXT NOT NULL,
  worker_name     TEXT,
  detail          TEXT NOT NULL DEFAULT '',
  category        TEXT NOT NULL DEFAULT 'drone',
  is_notification INTEGER NOT NULL DEFAULT 0,
  metadata        TEXT NOT NULL DEFAULT '{}',
  repeat_count    INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_buzz_timestamp ON buzz_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_buzz_worker ON buzz_log(worker_name);
CREATE INDEX IF NOT EXISTS idx_buzz_action ON buzz_log(action);
CREATE INDEX IF NOT EXISTS idx_buzz_category ON buzz_log(category);
CREATE INDEX IF NOT EXISTS idx_buzz_worker_time ON buzz_log(worker_name, timestamp);
CREATE INDEX IF NOT EXISTS idx_buzz_category_time ON buzz_log(category, timestamp);

-- ============================================================
-- MESSAGES
-- ============================================================

CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY,
  sender      TEXT NOT NULL,
  recipient   TEXT NOT NULL,
  msg_type    TEXT NOT NULL,
  content     TEXT NOT NULL,
  created_at  REAL NOT NULL,
  read_at     REAL
);

CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient);
CREATE INDEX IF NOT EXISTS idx_messages_unread ON messages(recipient, read_at);
-- v12: matches MessageStore.send()'s dedup probe — every inter-worker
-- send runs ``WHERE sender=? AND recipient=? AND msg_type=? AND
-- created_at > ?``.  Without this index the dedup check was a full
-- table scan on every message.
CREATE INDEX IF NOT EXISTS idx_messages_dedup
  ON messages(sender, recipient, msg_type, created_at);
-- v13: the queen_view_messages / message_stream triage queries scan
-- ``WHERE created_at >= ? ORDER BY created_at DESC`` — the dedup index
-- above starts with ``sender`` so it can't serve a bare created_at range.
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);

-- ============================================================
-- PIPELINES
-- ============================================================

CREATE TABLE IF NOT EXISTS pipelines (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  enabled     INTEGER NOT NULL DEFAULT 1,
  schedule    TEXT,
  config      TEXT NOT NULL DEFAULT '{}',
  created_at  REAL,
  updated_at  REAL
);

CREATE TABLE IF NOT EXISTS pipeline_stages (
  id            INTEGER PRIMARY KEY,
  pipeline_id   TEXT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
  stage_order   INTEGER NOT NULL,
  name          TEXT NOT NULL DEFAULT '',
  action        TEXT NOT NULL,
  config        TEXT NOT NULL DEFAULT '{}',
  UNIQUE(pipeline_id, stage_order)
);

-- ============================================================
-- SECRETS
-- ============================================================

CREATE TABLE IF NOT EXISTS secrets (
  key         TEXT PRIMARY KEY,
  value       TEXT NOT NULL,
  updated_at  REAL
);

-- ============================================================
-- QUEEN SESSIONS
-- ============================================================

CREATE TABLE IF NOT EXISTS queen_sessions (
  name        TEXT PRIMARY KEY,
  session_id  TEXT NOT NULL,
  created_at  REAL
);

-- ============================================================
-- SKILLS REGISTRY
-- Named slash-commands (e.g. /fix-and-ship) that workers invoke
-- in response to a matching task type. A registry (rather than a
-- hardcoded dict) lets operators inspect and adjust mappings at
-- runtime, and records usage so rarely-used skills can be retired.
-- ============================================================

CREATE TABLE IF NOT EXISTS skills (
  name           TEXT PRIMARY KEY,
  description    TEXT NOT NULL DEFAULT '',
  task_types     TEXT NOT NULL DEFAULT '[]',  -- JSON array of TaskType values
  usage_count    INTEGER NOT NULL DEFAULT 0,
  last_used_at   REAL,
  created_at     REAL NOT NULL
);

-- ============================================================
-- QUEEN CHAT — threads, messages, learnings
-- Interactive Queen central-command surface.  Threads are UI
-- grouping metadata over a single persistent Queen session; the
-- Claude conversation stream is unified, threads partition it.
-- ============================================================

CREATE TABLE IF NOT EXISTS queen_threads (
  id                 TEXT PRIMARY KEY,
  title              TEXT NOT NULL DEFAULT '',
  -- kind: operator|oversight|proposal|escalation|anomaly
  kind               TEXT NOT NULL DEFAULT 'operator',
  status             TEXT NOT NULL DEFAULT 'active',    -- active|resolved|archived
  worker_name        TEXT,                              -- optional subject worker
  task_id            TEXT,                              -- optional subject task
  created_at         REAL NOT NULL,
  updated_at         REAL NOT NULL,
  resolved_at        REAL,
  resolved_by        TEXT,                              -- operator|queen
  resolution_reason  TEXT
);

CREATE INDEX IF NOT EXISTS idx_queen_threads_status ON queen_threads(status);
CREATE INDEX IF NOT EXISTS idx_queen_threads_kind ON queen_threads(kind);
CREATE INDEX IF NOT EXISTS idx_queen_threads_worker ON queen_threads(worker_name);
CREATE INDEX IF NOT EXISTS idx_queen_threads_updated ON queen_threads(updated_at);

CREATE TABLE IF NOT EXISTS queen_messages (
  id          INTEGER PRIMARY KEY,
  thread_id   TEXT NOT NULL REFERENCES queen_threads(id) ON DELETE CASCADE,
  role        TEXT NOT NULL,              -- queen|operator|system
  content     TEXT NOT NULL,
  widgets     TEXT NOT NULL DEFAULT '[]', -- JSON array of widget descriptors
  ts          REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_queen_messages_thread ON queen_messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_queen_messages_ts ON queen_messages(ts);

CREATE TABLE IF NOT EXISTS queen_learnings (
  id          INTEGER PRIMARY KEY,
  context     TEXT NOT NULL,              -- what the decision was
  correction  TEXT NOT NULL,              -- operator's correction
  applied_to  TEXT NOT NULL DEFAULT '',   -- decision type tag
  thread_id   TEXT,                       -- optional originating thread
  created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_queen_learnings_applied ON queen_learnings(applied_to);

-- ============================================================
-- WORKER BLOCKERS (task #250)
-- One row per (worker, task_number) declaring the task is blocked
-- until ``blocked_by_task`` completes or a new inbox message arrives.
-- Read by the IdleWatcher drone before issuing a nudge.
-- ============================================================

CREATE TABLE IF NOT EXISTS worker_blockers (
  worker           TEXT    NOT NULL,
  task_number      INTEGER NOT NULL,
  blocked_by_task  INTEGER NOT NULL,
  reason           TEXT    NOT NULL DEFAULT '',
  created_at       REAL    NOT NULL,
  PRIMARY KEY (worker, task_number)
);

CREATE INDEX IF NOT EXISTS idx_worker_blockers_worker ON worker_blockers(worker);

-- ============================================================
-- PLAYBOOKS (playbook-synthesis-loop spec, Phase 1)
-- Self-improving procedural memory: generalizable procedures the
-- headless Queen synthesizes from SUCCESSFUL completed tasks.
-- DISTINCT from the `skills` table (slash-command registry) and
-- from Claude Code .claude/skills/ artifacts — see the spec's
-- normative "Naming" section. FTS is layered on by PlaybookStore
-- at runtime (optional fts5; LIKE fallback) so a missing-fts5
-- build never breaks fresh-DB creation here.
-- ============================================================

CREATE TABLE IF NOT EXISTS playbooks (
  id                   TEXT PRIMARY KEY,
  name                 TEXT NOT NULL UNIQUE,
  title                TEXT NOT NULL DEFAULT '',
  scope                TEXT NOT NULL DEFAULT 'global',
  trigger              TEXT NOT NULL DEFAULT '',
  body                 TEXT NOT NULL DEFAULT '',
  provenance_task_ids  TEXT NOT NULL DEFAULT '[]',
  source_worker        TEXT NOT NULL DEFAULT '',
  confidence           REAL NOT NULL DEFAULT 0.0,
  uses                 INTEGER NOT NULL DEFAULT 0,
  wins                 INTEGER NOT NULL DEFAULT 0,
  losses               INTEGER NOT NULL DEFAULT 0,
  status               TEXT NOT NULL DEFAULT 'candidate',
  version              INTEGER NOT NULL DEFAULT 1,
  content_hash         TEXT NOT NULL DEFAULT '',
  created_at           REAL NOT NULL,
  updated_at           REAL NOT NULL,
  last_used_at         REAL,
  retired_reason       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_playbooks_scope_status ON playbooks(scope, status);
CREATE INDEX IF NOT EXISTS idx_playbooks_content_hash ON playbooks(content_hash);

CREATE TABLE IF NOT EXISTS playbook_events (
  id           INTEGER PRIMARY KEY,
  playbook_id  TEXT NOT NULL,
  task_id      TEXT NOT NULL DEFAULT '',
  worker       TEXT NOT NULL DEFAULT '',
  event        TEXT NOT NULL,
  ts           REAL NOT NULL,
  detail       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_playbook_events_pb ON playbook_events(playbook_id, ts);

-- v23 (#1840): the obvious right target for an ad-hoc query over tasks.
--
-- ARCHIVING SETS ``archived_at`` AND DELIBERATELY LEAVES ``status`` ALONE — the status
-- records what the task WAS when it was archived, and ``archived_at`` is the authority
-- for whether it still counts. The board loader already filters on it, so every UI path
-- is correct; raw SQL was the exposed surface and had no obvious correct form.
--
-- THE INCIDENT THIS EXISTS FOR: a direct ``status='assigned'`` query returned 22 rows and
-- was reported as a congested board. 15 of them were archived; the board was showing 7.
-- The failure mode is a CONFIDENT WRONG COUNT, not an error, which is why an affordance
-- beats a convention here.
--
-- NOT EVERY UNFILTERED QUERY IS A BUG — see ``TaskStore.existing_jira_keys``, which must
-- see archived rows and must NOT be swapped onto this view.
CREATE VIEW IF NOT EXISTS live_tasks AS
  SELECT * FROM tasks WHERE archived_at IS NULL;
"""
