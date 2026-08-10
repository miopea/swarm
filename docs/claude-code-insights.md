# Claude Code Improvements Catalog — Derived from Source Analysis

> Catalog of improvements derived from reverse-engineering Claude Code v2.1.88 source (512K lines, 1,900 files). Each item is rated by **impact** (how much it improves Swarm) and **effort** (implementation cost). Rating scale: S (game-changing), A (high-value), B (solid win), C (nice-to-have).
>
> Many items have since shipped — see the "Already Implemented" table at the bottom and per-item status in [`claude-code-roadmap.md`](claude-code-roadmap.md) before treating any entry as actionable.
>
> **This is a snapshot, not current.** The analysis is pinned to Claude Code **v2.1.88**. Swarm's runtime detection has since been verified against **v2.1.186** (see `project-notes.md`, "Native `/loop` coexistence"), and the harness has changed underneath several observations here — footer strings, tool surface, and compaction behaviour in particular. Re-verify against the installed binary before relying on any specific string or constant below.

---

## S-TIER: Game-Changing Improvements

### S1. Speculative Execution for Idle Workers
**Source**: `src/services/PromptSuggestion/speculation.ts`

Claude Code forks a speculative subprocess that runs safe read-only operations (Glob, Grep, Read, WebSearch) while the user reads output. If the user accepts, results merge instantly. If they type something different, the spec is aborted cleanly.

**Swarm application**: When a worker finishes a task and transitions to RESTING, speculatively start the next queued task's read-only preparation (fetch files, search codebase, read context). If the drone assigns that task, the worker already has context loaded. If a different task is assigned, abort the speculation.

**Win**: Eliminates 30-60s of "reading the codebase" time at the start of every task assignment. For a 5-worker swarm doing 20 tasks/day, this could save 10-20 minutes of billable API time daily.

**Effort**: Medium — needs a "speculative prep" mode in the drone that queues read-only operations.

---

### S2. Microcompaction — Surgical Context Trimming
**Source**: `src/services/compact/microCompact.ts`

Instead of full conversation compaction (expensive, loses detail), Claude Code surgically deletes old tool results (bash output, web fetches, file reads) while keeping the conversation structure intact. Uses Anthropic's `cache_edits` API to delete specific blocks without invalidating prompt cache.

**Swarm application**: Workers on long tasks accumulate stale tool results that bloat context. Before hitting the compaction threshold, the drone could inject a `/compact` command — but better, we could use the hook system to monitor context fill (`PreCompact` hook) and proactively suggest the worker trim old results.

**Win**: Extends effective worker session length by 2-3x before needing full compaction. Preserves important reasoning while dropping bulky tool output.

**Effort**: Low-Medium — we already monitor `context_pct`. Add a threshold (e.g., 70%) that triggers a "trim old results" instruction to the worker.

---

### S3. Inter-Worker Message Queues (Teammate Pattern)
**Source**: `src/tasks/InProcessTeammateTask/`, `SendMessageTool`

Claude Code's in-process teammates communicate via mailbox queues with `onIdleCallbacks`. Workers don't poll — they register a callback that fires when a message arrives. Messages are typed (`shutdown_request`, `plan_approval_response`, etc.) and routed by agentId.

**Swarm application**: Currently workers can only communicate indirectly (via task board, or the queen relaying messages). A direct inter-worker message system would let workers coordinate without queen overhead — e.g., "I found X in the codebase, you'll need it for your task" or "I'm about to modify file Y, hold off."

**Win**: Enables collaborative multi-worker tasks without queen mediation. Reduces queen cognitive load. Prevents file conflicts between workers editing the same codebase.

**Effort**: Medium-High — needs a message bus (could use the existing WebSocket infrastructure), worker-side message handling, and conflict detection.

---

## A-TIER: High-Value Improvements

### A1. Token Budget with Diminishing Returns Detection
**Source**: `src/query/tokenBudget.ts`

Claude Code doesn't just track token % — it detects **diminishing returns**: if a worker has auto-continued 3+ times and each continuation adds <500 tokens, it's spinning its wheels. Stop and surface the issue.

**Swarm application**: When a worker's `context_pct` is rising but output quality/quantity is dropping (measurable via token delta per turn), the drone should escalate rather than letting the worker burn tokens. Currently Swarm only monitors absolute context fill.

**Win**: Prevents token waste on stuck workers. A worker spinning in circles at 80% context costs ~$0.50/turn in API fees. Detecting this after 3 turns instead of 10 saves real money.

**Effort**: Low — we already track `last_turn_input_tokens`. Add delta tracking and a threshold check in the state tracker.

**Implementation**:
- Track `prev_input_tokens` on Worker
- In state_tracker poll: `delta = current - prev`
- If `delta < 500` for 3 consecutive polls while BUZZING → escalate "diminishing returns"

---

### A2. Tiered Context Recovery (Error Withholding)
**Source**: `src/query.ts` lines 788-822

Claude Code stacks recovery strategies from cheapest to most expensive when hitting context limits:
1. Context collapse (free — just reorganize)
2. Microcompact (cheap — delete old tool results)
3. Reactive compact (moderate — summarize + trim)
4. Full compaction (expensive — rewrite context)

Errors are **withheld** during recovery — the user doesn't see "prompt too long" unless all recovery layers fail.

**Swarm application**: When a worker hits context limits, instead of immediately showing the error or forcing a restart, try automated recovery in sequence. The drone could: (1) inject a compact command, (2) if that fails, restart the worker with a context summary, (3) only escalate if both fail.

**Win**: Reduces worker restarts by ~50%. Each restart loses context and costs 1-2 minutes of re-orientation time.

**Effort**: Medium — needs a recovery state machine in the pilot/state_tracker.

---

### A3. Post-Compaction Context Restoration
**Source**: `src/services/compact/compact.ts`

After compaction, Claude Code re-injects up to 5 critical files (5K tokens each) + skills (25K budget) so the model has immediate working context without needing to re-fetch everything.

**Swarm application**: After a worker is revived or compacts, inject a "context restoration" message listing the task description, key files already read, and current progress. Currently revived workers start cold.

**Win**: Eliminates the 2-5 turns a revived worker spends re-reading files it already knew about.

**Effort**: Low — store a "last known context" summary per worker (key files, task state). Inject it as the first message after revive.

**Implementation**:
- On worker BUZZING, track files mentioned in output (already partially done via PTY parsing)
- On revive, prepend: "You were working on [task]. Key files: [list]. Progress: [summary]."

---

### A4. Permission Bubbling for Nested Operations
**Source**: `src/utils/permissions/`, bubble mode

Claude Code's `bubble` permission mode surfaces permission prompts from child agents to the parent, so the child doesn't block waiting for user input — the parent decides.

**Swarm application**: When the queen sends a directive to a worker, and that worker needs approval for a dangerous operation, instead of the worker blocking (showing a permission prompt in its PTY), the drone should detect this and route the decision to the queen or the operator — without needing PTY interaction.

**Win**: Combined with the PreToolUse hook (already implemented), this creates a complete approval pipeline: hook catches the permission → routes to daemon → queen or operator decides → hook responds. Zero PTY injection needed.

**Effort**: Low — we already have the PreToolUse hook infrastructure. Add queen-level decision routing for escalated approvals.

---

### A5. Cost Budgeting Per Task
**Source**: `src/cost-tracker.ts`, session cost persistence

Claude Code tracks costs per model with cache efficiency metrics and persists across sessions. It shows utilization % with early warnings at 70%.

**Swarm application**: Assign a cost budget to each task in the task board. When a worker's cumulative cost on a task exceeds the budget, escalate or pause. Show cost warnings at 70% of budget.

**Win**: Prevents runaway tasks from consuming disproportionate API budget. A task stuck in a loop can burn $5-10 before anyone notices. Budget enforcement catches this at $2.

**Effort**: Low-Medium — we already track per-worker cost. Add per-task accumulation and threshold checks.

**Implementation**:
- Add `cost_budget` field to Task model
- In usage refresh loop, accumulate cost against assigned task
- At 70%: warn in buzz log. At 100%: pause worker and escalate.

---

### A6. Denial Tracking — Adaptive Failure Response
**Source**: `src/utils/permissions/denialTracking.ts`

Claude Code tracks consecutive denials (max 3) and total denials (max 20) per session. After threshold, falls back from auto-mode to interactive prompting to prevent classifier death spirals.

**Swarm application**: Track consecutive escalations per worker. If a worker triggers 3+ consecutive escalations (the drone keeps escalating the same type of issue), something is systematically wrong — switch the worker to a more conservative mode or reassign the task.

**Win**: Prevents the "escalation spam" problem where a misconfigured worker floods the operator with identical escalation notifications.

**Effort**: Low — add a counter to the escalation tracking in `decision_executor.py`.

---

## B-TIER: Solid Wins

### B1. Batch State Updates (Coalescing)
**Source**: `src/services/mcp/useManageMCPConnections.ts` (16ms batching window)

Claude Code batches multiple state updates into a single AppState mutation using a 16ms time window. This prevents UI thrashing from rapid async callbacks.

**Swarm application**: The daemon broadcasts worker state changes individually. During a busy poll cycle (5+ workers), this can produce 10+ WebSocket messages in <100ms. Batch these into a single "state update" message per poll cycle.

**Win**: Reduces WebSocket traffic by 5-10x during busy periods. Dashboard renders once per cycle instead of per-worker.

**Effort**: Low — collect state changes during poll cycle, broadcast once at the end.

---

### B2. Error Deduplication
**Source**: `src/services/mcp/useManageMCPConnections.ts`

Claude Code deduplicates errors by `${error.type}:${error.source}:${plugin}` key. Same error shown once, not spammed.

**Swarm application**: The buzz log can fill with identical "escalated: choice requires approval" entries when a worker is stuck on the same prompt. Deduplicate consecutive identical entries with a count badge.

**Win**: Cleaner buzz log, easier to spot actual novel issues. Currently 20 identical escalation entries can bury important state changes.

**Effort**: Low — group consecutive entries with matching (action, worker, detail) tuples, show count.

---

### B3. Dream Task — Background Memory Consolidation
**Source**: `src/tasks/DreamTask/`

After N turns, Claude Code auto-triggers a background agent that reads session history and consolidates learnings into persistent memory. The agent is invisible to the user but enriches future sessions.

**Swarm application**: After a worker completes a task, trigger a background "consolidation" pass that extracts: key files discovered, patterns learned, gotchas encountered. Store in the task's metadata for future workers on similar tasks.

**Win**: Knowledge transfer between task completions. Worker B doesn't re-discover what Worker A already learned about the codebase area.

**Effort**: Medium — needs a post-completion hook and a structured knowledge extraction prompt.

---

### B4. Prompt Cache Optimization (CacheSafeParams)
**Source**: `src/tools/AgentTool/forkSubagent.ts`, CacheSafeParams

Claude Code carefully constructs fork contexts so the cache key (system_prompt + tools + model + message_prefix) is byte-identical across forks. This means forked agents hit the prompt cache, saving ~90% on input token costs.

**Swarm application**: When spawning multiple workers on similar tasks (same repo, same CLAUDE.md), ensure they share identical system prompts and tool configurations. Currently each worker gets its own context independently.

**Win**: With 5 workers in the same repo, prompt cache sharing could reduce input token costs by 60-80% (~$2-5/hour savings at scale).

**Effort**: Medium — need to standardize system prompt construction across workers and track cache hit rates.

---

### B5. MCP Server as Swarm Interface
**Source**: `src/entrypoints/mcp.ts`, `src/services/mcp/`

Claude Code can expose itself as an MCP server AND connect to external MCP servers. MCP servers provide tools, resources, and prompts via a standard protocol.

**Swarm application**: Register Swarm daemon as an MCP server that workers connect to. Workers could call Swarm tools directly: `swarm_task_status`, `swarm_send_message`, `swarm_claim_file`, without file-based hooks. This replaces the Write-file-then-curl pattern.

**Win**: Real-time bidirectional communication between workers and daemon. Eliminates file I/O latency and the "write JSON, wait for hook" pattern.

**Effort**: High — needs MCP server implementation in Python (the protocol is JSON-RPC over stdio).

---

### B6. Settings Layering (Global → Project → Local → Policy)
**Source**: `src/utils/settings/constants.ts`

Claude Code has 5-layer settings precedence: user → project → local → flag → managed. Later sources override earlier ones.

**Swarm application**: Worker configuration currently comes from `swarm.yaml` only. Add layering: global defaults → per-group settings → per-worker overrides → runtime flags. This enables templates ("all workers in group X get these approval rules") without per-worker boilerplate.

**Win**: Reduces config duplication. Currently adding a new approval rule to 5 workers requires editing 5 config sections.

**Effort**: Low-Medium — refactor config loader to support merge-based inheritance.

---

### B7. File Conflict Prevention
**Source**: InProcessTeammateTask message queue, coordination primitives

Claude Code's teammates can coordinate file access via typed messages. No two teammates edit the same file simultaneously.

**Swarm application**: When multiple workers operate in the same repo, track which files each worker is actively editing (via PostToolUse hooks on Edit/Write). If Worker B tries to edit a file Worker A is currently modifying, the drone should block or queue the edit.

**Win**: Prevents merge conflicts and lost work. Currently two workers can silently overwrite each other's changes.

**Effort**: Medium — needs a file lock registry in the daemon, consulted via PreToolUse hook.

---

## C-TIER: Nice-to-Have

### C1. Vim-Style State Machine for Worker Interaction
**Source**: `src/vim/transitions.ts`

Claude Code's vim mode is a pure-function state machine with exhaustive transitions. Each state defines exactly which inputs are valid and what the next state is.

**Swarm application**: Formalize the worker state machine (BUZZING/WAITING/RESTING/SLEEPING/STUNG) as a proper state transition table with explicit allowed transitions and side effects. Currently transitions are spread across multiple files.

**Win**: Easier debugging, impossible invalid states, cleaner code.

**Effort**: Low — refactor existing state logic into a transition table.

---

### C2. Graceful Rate Limit Messaging
**Source**: `src/utils/rateLimitMessages.ts`

Claude Code detects 5 rate limit types with utilization %, reset times, and actionable suggestions.

**Swarm application**: When workers hit API rate limits, show structured info in the buzz log: which limit, when it resets, which workers to pause. Currently rate limit errors are opaque.

**Win**: Operator can make informed decisions about which workers to pause/deprioritize during rate limiting.

**Effort**: Low — parse rate limit headers from worker output, display in buzz log.

---

### C3. Filesystem Error Classification
**Source**: `src/utils/errors.ts`

Claude Code classifies filesystem errors (ENOENT, EACCES, EPERM, ENOTDIR, ELOOP) and treats them as "nothing there" vs "genuine failure." Different recovery paths for each.

**Swarm application**: When workers encounter file errors, the drone could classify them and suggest remediation (missing file → wrong branch? permission error → wrong user? loop → symlink issue?).

**Win**: Slightly faster debugging of worker file access issues.

**Effort**: Low.

---

### C4. Agent Progress Tracking (ToolActivity)
**Source**: `src/tasks/LocalAgentTask/LocalAgentTask.tsx`

Claude Code tracks per-agent: toolUseCount, tokenCount, lastActivity, recentActivities (with tool name + description).

**Swarm application**: Show richer worker activity in the dashboard — not just state, but "last 5 tool calls" with tool names and brief descriptions. Currently we show state + PTY output.

**Win**: Better observability. Operator can see at a glance what each worker is doing without opening the terminal.

**Effort**: Medium — needs hook-based activity collection (PostToolUse already fires).

---

### C5. Serial Write Queue with Backpressure
**Source**: `src/cli/transports/HybridTransport.ts`

Claude Code's write queue allows only one inflight operation at a time, with exponential backoff on failure and a 100K-item memory cap.

**Swarm application**: The daemon's WebSocket broadcast is fire-and-forget. Add a bounded queue with backpressure for critical operations (task assignments, approval responses) to prevent message loss during high load.

**Win**: Prevents lost drone actions during busy periods.

**Effort**: Low-Medium.

---

## Already Implemented (No Action Needed)

| Claude Code Feature | Swarm Status |
|---|---|
| Circuit breaker | ✅ `pilot.py` — per-worker failure tracking |
| Adaptive backoff | ✅ `backoff.py` — state-aware + pressure-aware |
| Token/cost tracking | ✅ `worker.py` — TokenUsage dataclass |
| Context % monitoring | ✅ `usage.py` — estimate_context_usage() |
| Resource pressure response | ✅ `pressure.py` — PressureManager |
| Worker state hysteresis | ✅ `state_tracker.py` — configurable confirmations |
| PreToolUse approval hooks | ✅ Shipped — hooks/approval_hook.sh |
| SessionEnd hooks | ✅ Shipped — hooks/session_end_hook.sh |
| Lifecycle event hooks | ✅ Shipped — hooks/event_hook.sh |
| Compact event telemetry | ✅ Shipped — `LogCategory.COMPACT` records tokens before/after/ratio/trigger each `/compact` cycle |
| Tool-usage analytics | ✅ Shipped — `swarm analyze-tools` CLI mines `mcp:*` buzz-log entries for per-tool stats |
| Approval-rate gauge | ✅ Shipped — `SystemLog.approval_rate()` + `/api/drones/approval-rate` + dashboard badge |
| Batch MCP calls | ✅ Shipped — `swarm_batch` MCP tool runs op sequences in one round-trip |
| Test run reproducibility | ✅ Shipped — `InfraSnapshot` captured at every `swarm test` start; `--pin-model` flag |
| Opt-in native sandbox | ✅ Shipped — `SandboxConfig` + CC version detection + `~/.claude/settings.json["sandbox"]` merge |
| Skills registry | ✅ Shipped — SQLite-backed (schema v5) with usage counters; `GET /api/skills` endpoint |
| Claude Code Security scans | ✅ Shipped — `claude_code_security` service handler + dedup state file |

---

## Implementation Priority (Recommended Order) — HISTORICAL

> **This ordering is spent, not a plan.** 10 of the 12 items below have shipped;
> only **B1** (batch state updates, satisfied in substance rather than as scoped)
> and the optimization half of **B4** remain open. Do not pick work off this list
> — [`claude-code-roadmap.md`](claude-code-roadmap.md) carries the per-item status,
> and [`../CHANGELOG.md`](../CHANGELOG.md) is the authoritative shipped record.
> Retained for the reasoning behind the original sequencing.

1. **A1** — Diminishing returns detection (low effort, immediate $ savings)
2. **S2** — Microcompaction threshold (low-medium effort, big session length win)
3. **A5** — Cost budgeting per task (low-medium effort, prevents runaway costs)
4. **A3** — Post-compaction context restoration (low effort, faster revives)
5. **A6** — Denial/escalation tracking (low effort, cleaner operations)
6. **B1** — Batch state updates (low effort, cleaner WebSocket)
7. **B2** — Error deduplication in buzz log (low effort, better UX)
8. **S1** — Speculative execution (medium effort, significant time savings)
9. **A2** — Tiered context recovery (medium effort, fewer restarts)
10. **B7** — File conflict prevention (medium effort, prevents data loss)
11. **S3** — Inter-worker messages (high effort, enables collaboration)
12. **B5** — MCP server interface (high effort, replaces file-based IPC)
