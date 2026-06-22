# Claude Code Intelligence — Implementation Roadmap

> Derived from reverse-engineering Claude Code v2.1.88 (512K lines, 1,900 files).
> Source analysis: `docs/claude-code-insights.md`
> Private source archive: `github.com/bschleifer/claude-code-source` (private)
>
> **Status (2026-04-16):** Phase 0 is fully shipped. Most of Phase 1 and the MCP work from Phase 4 have landed. A follow-on batch of Anthropic-engineering-inspired features (tracked in CHANGELOG Unreleased) has also shipped: `swarm_batch` MCP tool, cron pipeline schedules, compact event telemetry, approval-rate gauge, `InfraSnapshot` in test runs, SQLite skills registry (schema v5), `claude_code_security` service handler, `swarm analyze-tools` CLI, and opt-in Claude Code sandbox. Grep for the named files/fields to verify.
>
> **Last reviewed 2026-04-16.** Heavy post-roadmap shipping (releases 2026.4.22.2 – 2026.4.22.8) added: `swarm_report_blocker` (task #250, schema v7), `swarm_note_to_queen` (#248), interactive Queen CLAUDE.md + drift reconcile (#251/#254), `HEADLESS_DECISION_PROMPT` seeding (#253), pressure threshold tuning (#254 / version .6), and the two-Queens architecture decision (`docs/specs/headless-queen-architecture.md`).
>
> **This is a planning doc; [`../CHANGELOG.md`](../CHANGELOG.md) is the authoritative record of what has shipped** (the project releases continuously — current version `2026.6.21`). Since this roadmap was last revised, shipped work includes the verifier drone, the playbook-synthesis loop, daemon health-sweep alerting, task/pipeline lifecycle notifications + a daily digest, DB auto-backup + `swarm db restore`, retry/backoff for the Jira/Graph integrations, the `swarm_query_peers` peer-visibility tool, and the dashboard's searchable Queen-history and inter-worker Messages tabs.

---

## Phase 0: Foundation (SHIPPED 2026-04-01)

Hook-based integration layer — replaces fragile PTY-injection with Claude Code's native hook protocol.

| ID | Item | Status | Files |
|----|------|--------|-------|
| P0-1 | PreToolUse approval hook | **SHIPPED** | `hooks/approval_hook.sh`, `server/routes/hooks.py` |
| P0-2 | SessionEnd hook (STUNG detection) | **SHIPPED** | `hooks/session_end_hook.sh`, `server/routes/hooks.py` |
| P0-3 | Lifecycle event hooks (SubagentStart/Stop, PreCompact/PostCompact) | **SHIPPED** | `hooks/event_hook.sh`, `hooks/install.py` |
| P0-4 | Hook route registration + auth exemptions | **SHIPPED** | `server/routes/__init__.py`, `server/api.py` |
| P0-5 | Buzz log local time display | **SHIPPED** | `web/app.py`, `web/static/dashboard.js`, `web/templates/partials/system_log.html` |

---

## Phase 1: Quick Wins (Low Effort, High Value)

Items that build on existing infrastructure with minimal new code. Target: 1-2 days each.

### 1.1 — Diminishing Returns Detection [A1] — **SHIPPED**

> Live: `prev_input_tokens` / `low_delta_streak` fields on `Worker`, delta check in `drones/state_tracker.py`.

**What**: Detect when a BUZZING worker is spinning its wheels — context growing but output productivity dropping. Escalate after 3 consecutive low-delta polls.

**Why** (from source): Claude Code's `tokenBudget.ts` uses `DIMINISHING_THRESHOLD = 500` tokens and `continuationCount >= 3` to detect unproductive continuation loops.

**Files to modify**:
- `src/swarm/worker/worker.py` — add `prev_input_tokens: int` and `low_delta_streak: int` fields
- `src/swarm/drones/state_tracker.py` — compute delta in poll, check threshold
- `src/swarm/drones/pilot.py` — emit escalation on diminishing returns

**Effort**: ~2-4 hours
**Dependencies**: None
**Acceptance criteria**:
- [ ] Worker tracks input token delta between polls
- [ ] After 3 consecutive polls with delta < 500 tokens while BUZZING, escalate
- [ ] Escalation appears in buzz log with "diminishing returns" reason
- [ ] Counter resets on state change or task reassignment

---

### 1.2 — Proactive Compact Threshold [S2] — **SHIPPED (partial)**

> `context_warning_threshold` and `context_critical_threshold` are live in `DroneConfig` and enforced in `state_tracker.py`. The actual `/compact` injection path runs through the state tracker / decision executor — verify against `drones/state_tracker.py` before treating this as fully done.

**What**: When a worker's `context_pct` exceeds 70%, the drone proactively injects a `/compact` message. At 85%, inject unconditionally. Uses the PreCompact hook event to track when compaction is already in progress.

**Why** (from source): Claude Code has a 4-layer compaction stack (collapse → microcompact → reactive → full) with buffer thresholds at 13K, 20K, and 3K tokens. We can't control which layer fires, but we can trigger compaction before the worker hits the wall.

**Files to modify**:
- `src/swarm/drones/state_tracker.py` — check `context_pct` during poll
- `src/swarm/drones/decision_executor.py` — add `_execute_deferred_compact()` for threshold injection
- `src/swarm/worker/worker.py` — add `compacting: bool` flag (set via PreCompact hook)
- `src/swarm/config/models.py` — add `context_warning_threshold` and `context_critical_threshold` to DroneConfig (may already exist)

**Effort**: ~4-6 hours
**Dependencies**: P0-3 (PreCompact hook already shipped)
**Acceptance criteria**:
- [ ] At 70% context: warning in buzz log
- [ ] At 85% context: drone injects `/compact` command
- [ ] PreCompact/PostCompact hooks set/clear `compacting` flag to prevent double-injection
- [ ] Configurable thresholds in swarm.yaml

---

### 1.3 — Cost Budgeting Per Task [A5] — **SHIPPED**

> `cost_budget` and `cost_spent` fields live on `Task` (`tasks/task.py`) and the unified schema (`tasks` table). Dashboard cards show the ratio.

**What**: Add optional `cost_budget` field to tasks. Track cumulative cost while task is assigned. Warn at 70%, pause worker at 100%.

**Why** (from source): Claude Code's `cost-tracker.ts` persists costs per session with model-level granularity and shows utilization warnings at 70%. A stuck task can burn $5-10 before anyone notices.

**Files to modify**:
- `src/swarm/tasks/task.py` — add `cost_budget: float | None` and `cost_spent: float` fields
- `src/swarm/server/daemon.py` — in `_usage_refresh_loop()`, accumulate cost against assigned task
- `src/swarm/drones/pilot.py` — check task budget in poll cycle, escalate/pause on threshold
- `src/swarm/web/templates/partials/task_list.html` — show cost progress bar
- `src/swarm/server/routes/tasks.py` — accept `cost_budget` in create/update

**Effort**: ~4-6 hours
**Dependencies**: None
**Acceptance criteria**:
- [ ] Tasks can have an optional cost budget (set via API or dashboard)
- [ ] Cost accumulates while worker is assigned to task
- [ ] At 70%: warning in buzz log
- [ ] At 100%: worker paused, escalation fired
- [ ] Dashboard shows cost/budget ratio on task cards

---

### 1.4 — Post-Revive Context Restoration [A3] — **SHIPPED**

> Live via `last_context_summary` / `recent_tools` fields on `Worker` (`worker/worker.py`) and the revive flow in `drones/pilot.py`.

**What**: When a worker is revived, inject a context restoration message containing: task description, key files previously read, and progress summary.

**Why** (from source): Claude Code's `compact.ts` restores up to 5 files (5K tokens each) + skills (25K budget) after compaction. Revived workers currently start cold and waste 2-5 turns re-reading.

**Files to modify**:
- `src/swarm/worker/worker.py` — add `last_context_summary: str` field, populated during BUZZING
- `src/swarm/drones/state_tracker.py` — extract file paths from output during polls
- `src/swarm/drones/pilot.py` — in revive flow, prepend context summary as first message

**Effort**: ~3-4 hours
**Dependencies**: None
**Acceptance criteria**:
- [ ] Worker tracks last N file paths seen in output (via regex)
- [ ] On revive, first message includes task + files + progress
- [ ] Context summary clears on task reassignment

---

### 1.5 — Escalation Spam Detection [A6] — **SHIPPED**

> Live: consecutive-escalation tracker in `drones/decision_executor.py` + dedup logic in `drones/log.py`.

**What**: Track consecutive escalations per worker. After 3 consecutive identical escalations, suppress further notifications and log a "systematic issue" alert.

**Why** (from source): Claude Code's `denialTracking.ts` uses `maxConsecutive: 3` and `totalDenials: 20` with adaptive fallback. Prevents classifier death spirals.

**Files to modify**:
- `src/swarm/drones/decision_executor.py` — add `_consecutive_escalations: dict[str, int]` tracker
- `src/swarm/drones/log.py` — add dedup logic for consecutive identical entries

**Effort**: ~2-3 hours
**Dependencies**: None
**Acceptance criteria**:
- [ ] After 3 consecutive escalations for same worker+reason, suppress further notifications
- [ ] Log single "systematic issue" alert with count
- [ ] Counter resets on state change, task change, or manual override

---

### 1.6 — Permission Bubbling to Queen [A4] — **SHIPPED**

> Queen-routed approval decisions live in `server/routes/hooks.py` via the PreToolUse hook path. Falls back to operator approval when Queen is offline.

**What**: When the PreToolUse hook receives a tool call that requires escalation, route the decision to the queen (if active) for autonomous resolution instead of blocking the worker.

**Why** (from source): Claude Code's `bubble` permission mode surfaces child permission prompts to the parent. Combined with our shipped PreToolUse hooks, this completes the approval pipeline.

**Files to modify**:
- `src/swarm/server/routes/hooks.py` — in `_evaluate_rules()`, when decision is "passthrough" (escalate), queue for queen
- `src/swarm/drones/directives.py` — add queen directive type for approval decisions
- `src/swarm/queen/session.py` — handle approval requests

**Effort**: ~4-6 hours
**Dependencies**: P0-1 (PreToolUse hook), queen must be active
**Acceptance criteria**:
- [ ] Escalated tool calls route to queen when queen is active
- [ ] Queen can approve or deny via directive
- [ ] Falls back to operator approval if queen is inactive or times out

---

## Phase 2: Operational Polish (Low-Medium Effort)

Dashboard and UX improvements that make Swarm easier to operate.

### 2.1 — Batch State Updates [B1]

**What**: Collect all worker state changes during a poll cycle and broadcast once at the end, instead of per-worker.

**Files to modify**:
- `src/swarm/drones/pilot.py` — collect state diffs in poll loop, broadcast batch
- `src/swarm/server/daemon.py` — add `broadcast_batch()` method
- `src/swarm/web/static/dashboard.js` — handle batch state message

**Effort**: ~3-4 hours
**Dependencies**: None
**Acceptance criteria**:
- [ ] Single WebSocket message per poll cycle for all worker state changes
- [ ] Dashboard handles batch update atomically (single re-render)

---

### 2.2 — Buzz Log Deduplication [B2]

**What**: Group consecutive identical log entries with a count badge instead of showing each one.

**Files to modify**:
- `src/swarm/drones/log.py` — add dedup logic in `add()` method
- `src/swarm/web/templates/partials/system_log.html` — show count badge
- `src/swarm/web/static/dashboard.js` — update rendering

**Effort**: ~2-3 hours
**Dependencies**: None
**Acceptance criteria**:
- [ ] Consecutive entries with same (action, worker, detail) are merged
- [ ] Badge shows "x3" etc.
- [ ] First and last timestamps shown

---

### 2.3 — Rate Limit Messaging [C2]

**What**: Parse rate limit indicators from worker output and display structured info in buzz log.

**Files to modify**:
- `src/swarm/providers/claude.py` — add rate limit detection patterns
- `src/swarm/drones/state_tracker.py` — detect and emit rate limit events
- `src/swarm/drones/pilot.py` — pause lowest-priority worker on rate limit

**Effort**: ~3-4 hours
**Dependencies**: None
**Acceptance criteria**:
- [ ] Rate limit detected from worker output
- [ ] Buzz log shows which limit, estimated reset time
- [ ] Suggestion to pause N workers shown

---

### 2.4 — Agent Progress Tracking [C4]

**What**: Track per-worker tool activity via PostToolUse hooks. Show "last 5 tool calls" in dashboard.

**Files to modify**:
- `src/swarm/server/routes/hooks.py` — in `handle_event()`, store tool activity
- `src/swarm/worker/worker.py` — add `recent_tools: list[dict]` field
- `src/swarm/web/templates/dashboard.html` — show tool activity in worker detail

**Effort**: ~4-6 hours
**Dependencies**: P0-3 (PostToolUse hooks)
**Acceptance criteria**:
- [ ] Last 5 tool calls per worker visible in dashboard
- [ ] Shows tool name + brief description
- [ ] Clears on task reassignment

---

## Phase 3: Architecture (Medium Effort)

Deeper improvements that change how Swarm operates.

### 3.1 — Speculative Task Preparation [S1] — **SHIPPED (experimental)**

> Gated behind `drones.speculation_enabled` (default `false`). Triggers live in `drones/poll_dispatcher.py` and `drones/state_tracker.py`; `speculating` flag lives on `Worker`.

**What**: When a worker becomes RESTING and the task queue has items, speculatively prepare the next task by having the worker read relevant files and search the codebase. If the task is assigned, the worker has context. If not, abort cleanly.

**Why** (from source): Claude Code's `speculation.ts` forks a subprocess for safe read-only operations with formal boundary detection. Stops at permission boundaries (edits). Uses `MAX_SPECULATION_TURNS = 20`.

**Files to modify**:
- `src/swarm/drones/pilot.py` — add speculation trigger on RESTING→task available
- `src/swarm/drones/decision_executor.py` — add `_execute_speculation()` method
- `src/swarm/worker/worker.py` — add `speculating: bool` and `speculation_task_id: str | None`
- `src/swarm/providers/claude.py` — add speculation prompt template (read-only prep)

**Effort**: ~8-12 hours
**Dependencies**: 1.4 (context restoration pattern)
**Acceptance criteria**:
- [ ] RESTING worker with queued task starts read-only prep
- [ ] Prep aborted cleanly if different task assigned or worker needed elsewhere
- [ ] Buzz log shows speculation start/end
- [ ] No file modifications during speculation (read-only tools only)
- [ ] Worker transitions to full task mode on assignment

---

### 3.2 — Tiered Context Recovery [A2] — **PARTIAL**

> Recovery logic (`recovery_attempts`, RECOVERING path) is live in `drones/state_tracker.py`. Verify the full tiered sequence (compact → restart with summary → escalate) against the current implementation before relying on it.

**What**: When a worker shows context limit errors, attempt automated recovery before escalating: (1) inject /compact, (2) if that fails, restart with context summary, (3) escalate only if both fail.

**Files to modify**:
- `src/swarm/drones/state_tracker.py` — detect context error patterns in output
- `src/swarm/drones/pilot.py` — add recovery state machine (RECOVERING state)
- `src/swarm/worker/worker.py` — add `recovery_attempts: int` field
- `src/swarm/drones/decision_executor.py` — recovery action handlers

**Effort**: ~6-8 hours
**Dependencies**: 1.2 (compact threshold), 1.4 (context restoration)
**Acceptance criteria**:
- [ ] Context errors trigger recovery, not immediate escalation
- [ ] Recovery tries: compact → restart with summary → escalate
- [ ] Max 2 recovery attempts per error
- [ ] Buzz log shows recovery attempts and outcomes

---

### 3.3 — File Conflict Prevention [B7] — **SHIPPED**

> File ownership tracking lives in `src/swarm/coordination/ownership.py`; approval-hook consults it via `server/routes/hooks.py`. Exposed through `GET /api/coordination/ownership` and the `coordination.file_ownership` config.

**What**: Track which files each worker is editing via PreToolUse hooks on Edit/Write. Block concurrent edits to the same file.

**Files to modify**:
- `src/swarm/server/routes/hooks.py` — in `handle_approval()`, check file lock registry
- `src/swarm/server/daemon.py` — add `file_locks: dict[str, str]` (path → worker_name)
- `src/swarm/hooks/install.py` — ensure Edit/Write tools trigger PreToolUse

**Effort**: ~6-8 hours
**Dependencies**: P0-1 (PreToolUse hook)
**Acceptance criteria**:
- [ ] Edit/Write to a file locked by another worker returns `{"decision": "block"}`
- [ ] Lock acquired on first Edit/Write, released after PostToolUse
- [ ] Dashboard shows active file locks per worker
- [ ] Locks auto-expire after 60s (stale lock protection)

---

### 3.4 — Settings Layering [B6] — **SHIPPED**

> `config_overrides` table in the unified DB supports `owner_type` = `defaults` | `group` | `worker`. Merge resolution lives in `config/loader.py`.

**What**: Refactor config to support inheritance: global defaults → group settings → per-worker overrides.

**Files to modify**:
- `src/swarm/config/loader.py` — add merge-based inheritance resolution
- `src/swarm/config/models.py` — add `defaults` and per-group config sections

**Effort**: ~6-8 hours
**Dependencies**: None
**Acceptance criteria**:
- [ ] `swarm.yaml` supports `defaults:` section applied to all workers
- [ ] Group-level settings override defaults
- [ ] Per-worker settings override group
- [ ] Existing configs work unchanged (backward compatible)

---

### 3.5 — Post-Task Knowledge Consolidation (Dream) [B3]

**What**: After task completion, trigger a background pass that extracts key learnings (files, patterns, gotchas) and stores them in task metadata.

**Files to modify**:
- `src/swarm/drones/pilot.py` — trigger consolidation on task completion
- `src/swarm/tasks/task.py` — add `learnings: dict` field
- `src/swarm/queen/session.py` — consolidation prompt template

**Effort**: ~6-8 hours
**Dependencies**: Queen must be active
**Acceptance criteria**:
- [ ] Completed tasks get a `learnings` field with extracted knowledge
- [ ] Future workers assigned similar tasks see prior learnings
- [ ] Consolidation runs in background, doesn't block worker

---

### 3.6 — Prompt Cache Optimization [B4]

**What**: Standardize system prompt construction across workers in the same repo so they share prompt cache prefixes.

**Files to modify**:
- `src/swarm/pty/holder.py` — standardize env vars and CLAUDE.md injection order
- `src/swarm/worker/usage.py` — track cache hit rate per worker
- `src/swarm/server/daemon.py` — report cache efficiency in usage broadcasts

**Effort**: ~4-6 hours
**Dependencies**: None
**Acceptance criteria**:
- [ ] Workers in same repo share identical system prompt prefix
- [ ] Cache read vs creation tokens tracked and displayed
- [ ] Dashboard shows cache efficiency % per worker

---

## Phase 4: Advanced Architecture (High Effort)

Major new capabilities that change Swarm's operational model.

### 4.1 — Inter-Worker Message Bus [S3] — **SHIPPED (via MCP, not hooks)**

> Implemented as MCP `swarm_send_message` / `swarm_check_messages` backed by the `messages` table in `swarm.db`, not the file-backed hook path originally scoped here. See `docs/specs/phase4-mcp-messaging.md`.

**What**: Direct worker-to-worker communication via typed message queues. Workers can send coordination messages (file claims, findings, warnings) without queen mediation.

**Why** (from source): Claude Code's `InProcessTeammateTask` uses mailbox queues with `onIdleCallbacks`, typed messages (`shutdown_request`, `plan_approval_response`), and file-backed persistence (`.claude/teammates/<agentId>/mailbox`).

**Files to modify**:
- `src/swarm/server/daemon.py` — add message bus (in-memory + WebSocket routing)
- `src/swarm/server/routes/hooks.py` — new endpoint `/api/hooks/message` for worker→daemon→worker
- `src/swarm/hooks/` — new `message_hook.sh` for bidirectional messaging
- `src/swarm/worker/worker.py` — add `inbox: list[dict]` field
- `src/swarm/web/static/dashboard.js` — show inter-worker messages in buzz log

**Effort**: ~12-16 hours
**Dependencies**: 3.3 (file conflict prevention uses similar coordination)
**Acceptance criteria**:
- [ ] Worker A can send typed message to Worker B via Swarm API
- [ ] Messages delivered via hook callback when worker is idle
- [ ] Message types: `file_claim`, `finding`, `warning`, `request`
- [ ] Messages visible in buzz log
- [ ] Undelivered messages persist across worker restarts

---

### 4.2 — MCP Server Interface [B5] — **SHIPPED**

> `src/swarm/mcp/server.py` + `tools.py` expose 8 coordination tools over Streamable HTTP (`/mcp`) and legacy SSE (`/mcp/sse`). Registered in `~/.claude/settings.json` by `hooks/install.py`.

**What**: Implement Swarm daemon as an MCP server. Workers connect via Claude Code's MCP client and call Swarm tools directly (task status, coordination, file claims) without file-based hooks.

**Files to modify**:
- `src/swarm/mcp/` — new module: MCP server implementation (JSON-RPC over stdio)
- `src/swarm/mcp/tools.py` — Swarm tools exposed via MCP
- `src/swarm/hooks/install.py` — register MCP server in Claude Code settings

**Effort**: ~16-20 hours
**Dependencies**: 4.1 (message bus provides the coordination layer MCP tools call into)
**Acceptance criteria**:
- [ ] Swarm registers as MCP server in Claude Code settings
- [ ] Workers can call `swarm_task_status`, `swarm_claim_file`, `swarm_send_message`
- [ ] MCP connection health-checked with reconnection
- [ ] Replaces file-based hook pattern for task completion and cross-task creation

---

## Phase 5: Polish (Low Effort, Low Priority)

### 5.1 — Formal State Machine [C1]
Refactor worker state transitions into a transition table. ~2-3 hours.

### 5.2 — Filesystem Error Classification [C3]
Classify worker file errors and suggest remediation. ~2-3 hours.

### 5.3 — Serial Write Queue [C5]
Bounded queue with backpressure for critical daemon operations. ~3-4 hours.

---

## Summary

| Phase | Items | Status | Key Win |
|-------|-------|--------|---------|
| **0** | 5 | SHIPPED | Hook-based integration layer |
| **1** | 6 | SHIPPED (1.2 partial) | Cost savings, fewer restarts, cleaner ops |
| **2** | 4 | In progress — grep to verify individual items | Better dashboard, less noise |
| **3** | 6 | Mixed — 3.1, 3.3, 3.4 shipped; 3.2 partial; 3.5, 3.6 pending | Speculation, recovery, conflict prevention |
| **4** | 2 | SHIPPED | Direct worker coordination, MCP interface |
| **5** | 3 | Pending | Code quality polish |
| **Total** | **26** | **majority shipped** | |

## Implementation Order (Recommended)

Start with Phase 1 items in parallel (they're independent). Phase 2 can overlap. Phase 3 items have some dependencies (noted above). Phase 4 is the big architectural push. Phase 5 whenever there's downtime.
