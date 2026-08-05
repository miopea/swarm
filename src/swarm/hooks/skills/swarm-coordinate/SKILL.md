---
name: swarm-coordinate
description: Survey peer worker states and pending tasks, then output a delegation suggestion. Advisory only — does not create or dispatch tasks. Use when the user asks who's idle, what's pending, or whether to hand work off.
user-invocable: true
allowed-tools:
  - mcp__swarm__swarm_task_status
  - mcp__swarm__swarm_check_messages
---

# /swarm-coordinate — Suggest a delegation

Survey the swarm and recommend where pending work could go. **Advisory only** — this skill never creates or dispatches tasks. Cross-worker task creation is reserved for the operator and the Queen.

## Workflow

1. Call `mcp__swarm__swarm_task_status` with `filter="all"` to see the full board state. Note each worker's current task (if any).
2. Call `mcp__swarm__swarm_task_status` with `filter="unassigned"` to see the queue of queen-eligible work.
3. Optionally call `mcp__swarm__swarm_check_messages` if peer warnings or findings would change a delegation choice.
4. Identify:
   - **Idle workers** — peers in `RESTING`/`SLEEPING` with no `ACTIVE` task.
   - **Open tasks** — tasks in `UNASSIGNED`/`ASSIGNED` waiting for a worker.
5. Output a tight delegation suggestion as plain text. Use this shape:

   ```text
   Idle workers: <list, or "none">
   Pending tasks: <list with task numbers + short titles, or "none">
   Suggested delegations:
   - Send task #<N> "<title>" to <worker> — <one-line reason>
   - Send task #<M> "<title>" to <worker> — <one-line reason>
   ```

   If there are no idle workers OR no pending tasks, say so plainly and stop.

## Constraints

- **Never call `mcp__swarm__swarm_create_task`** from this skill. Cross-worker dispatch crosses the Queen-only authority boundary.
- Suggestions must reference real task numbers and real worker names — never invent.
- If the operator wants to act on a suggestion, they invoke `swarm_create_task` themselves, or the suggesting worker uses `/swarm-handoff <worker> <description>`.
- Output is text only; no side effects, no PTY injections, no messages sent.
