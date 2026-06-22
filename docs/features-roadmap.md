# Swarm Feature Roadmap

> Originally compiled from a deep-dive interview on 2026-02-26.
> **Status (2026-04-16): Phases 1–6 are all shipped, plus a post-roadmap batch of Anthropic-engineering-inspired features (`swarm_batch` MCP tool, cron pipeline schedules, compact telemetry, approval-rate gauge, test infra pinning, skills registry, `claude_code_security` service handler, `swarm analyze-tools` CLI, opt-in CC sandbox).** This doc is retained as a historical reference for the original design. Current roadmap work lives in [`claude-code-roadmap.md`](claude-code-roadmap.md).
>
> **This is a historical design doc — [`../CHANGELOG.md`](../CHANGELOG.md) is the authoritative record of what has shipped.** Everything in Phases 1–6 and the follow-on batches below has landed; the project ships continuously (current version `2026.6.21`). Recent additions not in the original phases include the verifier drone, worker-reported blockers, the playbook-synthesis loop, daemon health-sweep alerting + lifecycle notifications + daily digest, DB auto-backup/restore, the `swarm_query_peers` peer-visibility tool, and the dashboard's Queen-history and Messages tabs.
> Build order was foundation-first; each layer unlocked the next.
> Architecture: direct integration into existing modules (not plugins).

---

## Phase 1: Drone Log Persistence (SQLite) — **SHIPPED**

> Delivered as part of the unified SQLite migration (`src/swarm/db/`). Drone decisions now land in `swarm.db` (`buzz_log` table) and are queryable from the dashboard. See `docs/specs/sqlite-unified-storage.md` for the migration details.

**Goal**: Persist drone decision logs so they survive daemon restarts and enable analytics.

### Storage
- SQLite database at `~/.swarm/drone_log.db`
- Single file, zero-config, queryable
- Schema: `decision_log` table with columns:
  - `id` INTEGER PRIMARY KEY
  - `timestamp` TEXT (ISO 8601)
  - `worker_name` TEXT
  - `action_type` TEXT (approve | reject | escalate | skip)
  - `rule_name` TEXT (which drone rule fired)
  - `context` TEXT (JSON blob: worker state, trigger, confidence)
  - `outcome` TEXT (what happened after the decision)
  - `overridden` BOOLEAN DEFAULT FALSE
  - `override_action` TEXT (what the user did instead, if overridden)
- Rotation: configurable max age (default 30 days), auto-prune on startup
- Migrate existing in-memory `DroneActionLog` to write-through: in-memory for fast reads, SQLite for persistence

### Integration Points
- `drones/log.py` — extend `DroneActionLog` with SQLite backend
- `server/daemon.py` — initialize DB on startup, prune old entries
- `server/api.py` — new endpoint: `GET /api/drone-log?worker=X&days=7`
- Dashboard — new "Drone Log" panel showing recent decisions with filters

---

## Phase 2: Override Tracking + Auto-Tuning Suggestions — **SHIPPED**

> Live as the "Tuning Suggestions" card in the dashboard and the `/api/drones/tuning` + `/api/drones/rules/analytics` endpoints.

**Goal**: Learn from user corrections to suggest drone config improvements.

### Override Detection
An "override" is when the user's action contradicts what the drone decided:

| Override Type | Signal | Meaning |
|---|---|---|
| Rejecting a drone approval | User sends Ctrl+C or manually intervenes after drone auto-approved an action | Rule too permissive |
| Approving after drone skip | User manually approves/answers when drone decided not to act (worker WAITING but below threshold) | Threshold too high |
| Redirecting a worker | User manually sends new instructions that drone/Queen didn't flag as needed | Oversight signals missed |

### Data Collection
- Extend `decision_log` schema with `overridden` and `override_action` columns
- Track user actions via existing API endpoints (`POST /api/send`, manual approvals)
- Correlate user actions with most recent drone decision for that worker within a time window (e.g., 5 minutes)

### Auto-Tuning Engine
- Periodic analysis (on-demand or daily): query override patterns from SQLite
- Pattern detection rules:
  - "You override >60% of escalations for worker X" → suggest raising `escalation_threshold`
  - "You manually approve >50% of WAITING states" → suggest lowering `auto_approve_confidence`
  - "You redirect worker X >3 times in 24h" → suggest enabling oversight for that worker
- Output: suggested `swarm.yaml` diff with explanation
- Presentation: "Tuning Suggestions" panel on dashboard with approve/dismiss buttons
- Approved suggestions update `swarm.yaml` directly (with backup)

### Integration Points
- New module: `drones/tuning.py` — analysis engine
- `server/api.py` — `GET /api/tuning/suggestions`, `POST /api/tuning/apply`
- Dashboard — "Tuning Suggestions" card with diff preview

---

## Phase 3: Dashboard Push Notifications — **SHIPPED**

> Browser push + desktop + terminal bell notifications are live. Notification history is persisted in the `buzz_log` table (see `src/swarm/notify/`).

**Goal**: Alert the user about important events without requiring them to watch the dashboard.

### Notification Events
| Event | Priority | Description |
|---|---|---|
| Approval needed | High | Worker is WAITING and needs human input |
| Queen intervention | High | Queen detected an issue and took action (redirected, flagged drift) |
| Task completed | Medium | Worker finished its assigned task |

### Implementation
- Use existing WebSocket infrastructure (`ws_broadcast`)
- New message type: `{"type": "notification", "event": "approval_needed", "worker": "api", "message": "...", "priority": "high", "timestamp": "..."}`
- Dashboard JS: toast/banner notification system
  - High priority: persistent banner until dismissed, optional browser `Notification` API (with permission prompt)
  - Medium priority: auto-dismissing toast (10s)
- Notification preferences stored in `swarm.yaml`:
  ```yaml
  notifications:
    approval_needed: true
    queen_intervention: true
    task_completed: true
    browser_notifications: false  # Notification API
  ```
- Notification history: last 50 notifications stored in-memory, viewable in dashboard panel

### Integration Points
- `server/daemon.py` — emit notification events
- `drones/pilot.py` — fire notifications on state transitions
- `queen/queen.py` — fire notifications on interventions
- Dashboard — toast component + notification history panel

---

## Phase 4: Queen Oversight Signals — **SHIPPED**

> `queen.oversight` is configurable in `swarm.yaml` and exposed via `GET /api/queen/oversight`. Prolonged-buzzing and task-drift signals both fire `oversight_alert` events.

**Goal**: Queen proactively monitors workers and intervenes when problems are detected.

### Trigger Model: Signal-Triggered with Cheap First-Pass Filter

**Signal 1: Prolonged Buzzing**
- Heuristic (cheap): worker has been in BUZZING state > N minutes (configurable, default 15min) without a git commit
- Detection: track last commit timestamp per worker, compare with buzzing duration
- If heuristic fires → Queen LLM call: "Worker X has been buzzing for 20 minutes without committing. Here's their recent output: [last 200 lines]. Are they making progress or stuck?"

**Signal 2: Task Drift**
- Hybrid detection (two-tier):
  1. **Cheap diff check**: run `git diff --name-only` in worker's directory. Compare modified files against task description's expected scope. If >50% of modified files are outside expected scope → escalate to tier 2
  2. **Semantic output analysis** (LLM call): Queen reads worker's last N lines of terminal output and asks "Is this worker still working on task '{task_title}'? If not, what are they doing instead?"

### Intervention Model: Configurable Per-Severity

| Severity | Action | Example |
|---|---|---|
| Minor | Queen sends corrective note directly to worker PTY | "Note: you're modifying files outside your task scope. Please focus on {task}" |
| Major | Queen pauses worker (Ctrl+C) then sends redirect instructions | Worker went completely off-track, needs fresh direction |
| Critical | Queen flags for human on dashboard (fires notification) | Security concern, data loss risk, or Queen is uncertain |

Severity classification by Queen LLM based on:
- How far off-track the worker is
- Risk of the worker's current actions
- Whether the issue is recoverable without human input

### Configuration
```yaml
queen:
  oversight:
    enabled: true
    buzzing_threshold_minutes: 15
    drift_check_interval_minutes: 10
    max_oversight_calls_per_hour: 6  # token budget control
```

### Integration Points
- New module: `queen/oversight.py` — signal detection + intervention logic
- `drones/pilot.py` — feed worker state data to oversight module during poll cycle
- `server/daemon.py` — oversight lifecycle management
- `queen/queen.py` — new `review_worker()` and `intervene()` methods

---

## Phase 5: File Ownership + Single-Branch Coordination — **SHIPPED**

> Implemented in `src/swarm/coordination/` (`ownership.py`, `sync.py`). Surfaced in the dashboard and via `GET /api/coordination/ownership` and `GET /api/coordination/sync`. The `coordination.file_ownership` config switches between `off`, `warning`, and `hard-block` modes.

**Goal**: Eliminate worktrees as the default isolation model. Workers share a single branch with Queen-managed file ownership.

### Single-Branch Model
- **Default**: all workers operate on the same branch (typically `main` or a shared feature branch)
- **Escape hatch**: when Queen detects unavoidable file overlap between two tasks, it spins up a temporary worktree for one worker
- **Auto-pull**: when any worker commits, other workers automatically pull latest (Queen sends `git pull --rebase` to their PTY)

### File Ownership Map
- Queen assigns file ownership when distributing tasks
- Ownership tracked in daemon state: `{file_path: worker_name}` mapping
- Ownership derived from:
  1. Task description analysis (Queen identifies likely files)
  2. Runtime tracking (`git diff --name-only` during poll cycle)
- Dashboard: visual file ownership map showing which worker owns which files/directories

### Conflict Handling
- **Warning + Queen review**: when a worker touches a file owned by another worker:
  1. Dashboard shows warning
  2. Queen is notified and reviews the overlap
  3. Queen decides: allow (update ownership), redirect worker, or escalate to worktree
- Existing `detect_conflicts()` infrastructure (runs every 30s) feeds the ownership system

### Shared Decisions Log
- When Queen or a worker makes an architectural/design decision, log it to SQLite
- Schema: `decisions` table: `id`, `timestamp`, `worker_name`, `decision`, `context`, `affected_files`
- Queen injects relevant decisions into worker context when assigning tasks
- Example: "Note: worker-api chose JWT for auth. Use the same approach."

### Worktree Escape Hatch
- Queen identifies overlapping file scopes during task assignment
- If overlap is unavoidable (e.g., two features modifying same API route file):
  1. Queen creates a temporary worktree for the second worker
  2. First worker completes and commits on shared branch
  3. Second worker's worktree is merged back
  4. Normal single-branch resumes

### Configuration
```yaml
coordination:
  mode: single-branch  # or "worktree" for legacy behavior
  auto_pull: true
  file_ownership: warning  # warning | hard-block | off
  decisions_log: true
```

### Integration Points
- New module: `coordination/ownership.py` — file ownership tracking and enforcement
- New module: `coordination/sync.py` — auto-pull orchestration
- Extend `queen/queen.py` — file scope analysis during task assignment
- Extend `git/conflicts.py` — feed ownership system
- `server/daemon.py` — coordination lifecycle
- Dashboard — file ownership map visualization

---

## Phase 6: Jira Integration — **SHIPPED**

> Jira Cloud sync went live using Atlassian OAuth 2.0 (3LO); the token-auth path described below was superseded. See the Jira section of the README for the current OAuth setup.

**Goal**: Two-way sync between Jira and Swarm's task board.

### Sync Scope
| Direction | What | How |
|---|---|---|
| Jira → Swarm | Pull tickets into Swarm task board | Periodic poll or webhook listener |
| Swarm → Jira (status) | Update Jira ticket status when worker starts/completes task | API call on task state change |
| Swarm → Jira (output) | Post worker output summaries, commit links, PR refs as Jira comments | API call on task completion |

### Task Mapping
- **Per-task**: each Swarm task links to a Jira ticket via `jira_key` field
- Worker assignment is independent — if task moves to different worker, Jira ticket stays linked
- Jira ticket status mapping (configurable):
  ```yaml
  jira:
    status_map:
      pending: "To Do"
      in_progress: "In Progress"
      done: "Done"
      failed: "To Do"  # return to backlog
  ```

### Authentication
- Config in `swarm.yaml`:
  ```yaml
  jira:
    url: "https://company.atlassian.net"
    email: "user@company.com"
    token: "$JIRA_API_TOKEN"  # env var reference
    project: "PROJ"
    sync_interval_minutes: 5
    import_filter: "status = 'To Do' AND labels = 'swarm'"
  ```
- Env var references (`$VAR`) resolved at startup
- Token stored securely via env var, never in plain text in config

### Import Flow
1. Daemon polls Jira every N minutes (or on-demand via API)
2. Fetches tickets matching `import_filter` JQL
3. Creates Swarm tasks with:
   - Title from Jira summary
   - Description from Jira description (markdown)
   - `jira_key` link back to ticket
   - Type mapping: Jira "Bug" → Swarm "bug", "Story" → "feature", etc.
4. Dedup: skip if task with same `jira_key` already exists

### Export Flow
1. On task state change → update Jira status via API
2. On task completion → post Jira comment with:
   - Worker name and duration
   - Commit hashes and messages
   - Link to PR (if created)
   - Summary of changes (from Queen or worker output)

### Integration Points
- New module: `integrations/jira.py` — Jira API client and sync logic
- Extend `tasks/board.py` — `jira_key` field on Task dataclass
- `server/daemon.py` — Jira sync lifecycle (periodic poll task)
- `server/api.py` — `POST /api/jira/sync` (manual trigger), `GET /api/jira/status`
- Dashboard — Jira link badges on tasks, "Sync Now" button

---

## Cross-Cutting Concerns

### Token Cost Management
- Queen oversight calls are gated by `max_oversight_calls_per_hour`
- Cheap heuristic filters reduce unnecessary LLM calls by ~90%
- Override tracking is pure data collection (no LLM cost)
- Jira sync is pure API calls (no LLM cost)

### Configuration
All features configured in `swarm.yaml` with sensible defaults:
```yaml
# Full example
drones:
  log_persistence: true  # Phase 1
  auto_tuning: true      # Phase 2

notifications:
  approval_needed: true   # Phase 3
  queen_intervention: true
  task_completed: true

queen:
  oversight:              # Phase 4
    enabled: true
    buzzing_threshold_minutes: 15
    drift_check_interval_minutes: 10

coordination:             # Phase 5
  mode: single-branch
  auto_pull: true
  file_ownership: warning

jira:                     # Phase 6
  url: "https://company.atlassian.net"
  email: "user@company.com"
  token: "$JIRA_API_TOKEN"
  project: "PROJ"
```

### Database
- Single SQLite database at `~/.swarm/swarm.db` (or `drone_log.db` if keeping it separate)
- Tables: `decision_log`, `overrides`, `decisions`, `notifications`
- Auto-migration on startup
- Configurable retention (default 30 days)

### Dashboard Updates
Each phase adds to the dashboard:
1. Drone Log panel (filterable decision history)
2. Tuning Suggestions card (approve/dismiss config changes)
3. Toast notifications + notification history
4. Oversight status indicators on worker cards
5. File ownership map + coordination status
6. Jira badges + sync controls
