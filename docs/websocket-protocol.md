# Swarm WebSocket Protocol

## Endpoints

| Path | Purpose |
|------|---------|
| `/ws` | Main event channel (state updates, proposals, notifications) |
| `/ws/terminal` | Terminal I/O (PTY read/write for a specific worker) |

## Authentication

Both endpoints require first-message auth within 5 seconds of connection:

```json
{"type": "auth", "token": "your-api-password"}
```

**Failure responses:**
- Timeout (5s): Close code `4001`, message "Auth timeout"
- Invalid JSON: Close code `4001`, message "Invalid auth message"
- Wrong token: Close code `4001`, message "Unauthorized"

**Lockout:** 5 failed auth attempts from the same IP triggers a 5-minute lockout.

**Per-IP limit:** Maximum 10 concurrent WebSocket connections per IP.

**Deprecated:** `?token=` query parameter still accepted but logs a warning.

## Main WebSocket (`/ws`)

### Init Message (server → client)

Sent immediately after successful auth:

```json
{
  "type": "init",
  "workers": [
    {"name": "api", "state": "BUZZING", "duration": 12.5}
  ],
  "drones_enabled": true,
  "proposals": [...],
  "proposal_count": 2,
  "queen_queue": {"running": 0, "pending": 0},
  "test_mode": false
}
```

### Event Types (server → client)

Grouped by area. Most refresh-style events carry no payload — they signal the client to re-fetch the relevant resource.

**Worker / task / pipeline state**

| Event Type | Trigger | Key Fields |
|------------|---------|------------|
| `workers_changed` | Any worker state change (also acts as the ~20s heartbeat) | (triggers client refresh) |
| `state` | Per-worker state snapshot (initial push or targeted update) | `worker`, `state`, `duration` |
| `tasks_changed` | Task created / updated / deleted | (triggers client refresh) |
| `task_updated` | Single-task update (e.g. Jira sync touched one row) — clients can patch in place instead of re-fetching the list | `task_id` |
| `task_assigned` | Queen (or operator) assigned a task to a worker | `task_id`, `worker` |
| `task_send_failed` | Failure delivering a task payload to a worker | `task_id`, `worker`, `error` |
| `pipelines_changed` | Pipeline created / state change / removed | (triggers client refresh) |

**Proposals & Queen**

| Event Type | Trigger | Key Fields |
|------------|---------|------------|
| `proposal_created` | Queen proposes an action | `proposal` object |
| `proposals_changed` | Proposal list changed (approved, rejected, expired, bulk resolve) — clients re-fetch the list | (empty) |
| `queen_auto_acted` | Queen auto-executed an action (confidence ≥ threshold) | `proposal_id`, `action` |
| `queen_escalation` | Queen escalated a worker to the operator | `worker`, `reason` |
| `queen_completion` | Queen detected a task completion | `task_id`, `worker`, `summary` |
| `queen_queue` | Queen call queue depth snapshot | `running`, `pending` |
| `queen.health` | Interactive Queen heartbeat / health snapshot (CLAUDE.md drift, queue depth, last-tick timing) | varies — full payload merged into envelope |
| `queen.thread` | Queen thread created / updated / closed (operator-facing thread object) | `event` (`created` / `updated` / `closed`), `thread` |
| `queen.message` | New message appended to a Queen thread | `thread_id`, `message` |
| `oversight_alert` | Queen oversight signal (prolonged buzzing, task drift) | `worker`, `signal`, `severity` |
| `escalation` | Drone escalated a worker to the Queen / operator | `worker`, `reason` |
| `operator_terminal_approval` | Operator is asked to approve a terminal-level action | `worker`, `request` |

**Messaging & hooks**

| Event Type | Trigger | Key Fields |
|------------|---------|------------|
| `message` | Inter-worker or operator message delivered | `from`, `to`, `msg_type`, `content` |
| `hook_event` | Claude Code lifecycle hook fired (SubagentStart/Stop, PreCompact/PostCompact, etc.) | `worker`, `event` |
| `hook_session_end` | SessionEnd hook — worker's Claude process exited | `worker` |
| `system_log` | New entry appended to the buzz log (drones, Queen, operator actions) | `action`, `worker`, `detail`, `category` |

> **`category` values** include `drone`, `task`, `queen`, `worker`, `system`, `operator`, `message`, and `compact`. A `compact` entry records `{tokens_before, tokens_after, ratio, trigger}` in its metadata whenever a worker finishes a `/compact` cycle.
| `notification` | Push notification event | `event`, `title`, `message`, `severity` |

**Coordination, resources & integrations**

| Event Type | Trigger | Key Fields |
|------------|---------|------------|
| `resources` | Resource monitor tick (~30s) | `mem_percent`, `swap_percent`, `load_1m`, `pressure_level` |
| `dstate_alert` | D-state (wedged) process detected | `pids` map |
| `usage_updated` | Token / cost usage refreshed | `workers`, `queen`, `total` |
| `conflict_detected` | File conflict detected (ownership / git) | `worker`, `files` |
| `conflicts_cleared` | Previously reported conflicts cleared | (empty) |
| `ownership_overlap` | File ownership overlap between workers | `overlaps` array |
| `jira_import` | Jira sync imported issues (`server/jira_service.py`) | `count` (number of tasks created; `1` for a single import-by-key) |
| `draft_reply_ok` | Queen-drafted email reply saved to Drafts | `task_id`, `message_id` |
| `draft_reply_failed` | Draft generation failed | `task_id`, `error` |

**Server / tunnel / config / test**

| Event Type | Trigger | Key Fields |
|------------|---------|------------|
| `config_changed` | Config was mutated via the API | (triggers client refresh) |
| `config_file_changed` | YAML file on disk changed (watcher) | `path` |
| `drones_toggled` | Drones enabled / disabled | `enabled` |
| `tunnel_started` | Cloudflare Tunnel goes up | `url` |
| `tunnel_stopped` | Tunnel goes down | (empty) |
| `tunnel_error` | Tunnel failure | `error` message |
| `update_available` | New Swarm version detected | `version`, `url` |
| `test_mode` | Test harness mode toggled | `enabled` |
| `test_report_ready` | `swarm test` finished and wrote a report | `path` |
| `http` | Internal HTTP log envelope (advanced) | `method`, `path`, `status` |
| `error` | Server-side error envelope | `message`, `code` |

> **Proposal resolution:** clients should listen for `proposals_changed` and re-fetch `GET /api/proposals`. There is no per-proposal "resolved" event — resolution is coalesced into the list-level broadcast.

### Client Commands (client → server)

| Command | Purpose |
|---------|---------|
| `{"command": "focus", "worker": "name"}` | Tell server which worker the client is viewing |

### Heartbeat

Server sends heartbeat data every ~20 seconds via `workers_changed` events. No explicit ping/pong frames — aiohttp handles WebSocket-level pings automatically.

## Terminal WebSocket (`/ws/terminal`)

### Auth + Worker Selection

```json
{"type": "auth", "token": "...", "worker": "api"}
```

Or connect with query param: `/ws/terminal?worker=api`

### Data Flow

- **Server → Client:** Raw terminal output (binary frames, UTF-8 text)
- **Client → Server:** Keystrokes (text frames)
- **Resize:** `{"type": "resize", "cols": 120, "rows": 40}`

### Connection Lifecycle

1. Client connects and authenticates
2. Server replays the recent terminal buffer, capped at **256 KB** (`_MAX_REPLAY_BYTES` in `pty/bridge.py`) and cut at a line boundary so xterm never receives a partial ANSI escape sequence
3. Bidirectional streaming begins
4. On disconnect, server keeps the PTY running (reconnect resumes)

## Error Codes

| Code | Meaning |
|------|---------|
| 4001 | Authentication failed |
| 4002 | Worker not found |
| 4003 | Rate limited (too many connections) |
| 1000 | Normal close |
