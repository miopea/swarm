---
description: Reports structured progress on your current task — phase and percent complete. Use when the user asks for a progress update, or when a long task reaches a phase boundary worth recording on the board.
argument-hint: <phase> <percent>
---

Report progress on your current task.

Args: $ARGUMENTS

Examples accepted (positional flexible):

- `/swarm-progress 50% testing`
- `/swarm-progress testing 50%`
- `/swarm-progress 50 implementing`

1. **Parse** $ARGUMENTS:
   - Find the numeric token (with optional trailing `%`); strip the `%` if present. This is `<percent>` as an integer (0–100).
   - Remaining text (joined with single spaces) is `<phase>`.

2. **Validate.** If no numeric token OR no phase text remaining, REFUSE with:

   ```text
   Usage: /swarm-progress <phase> <percent>
   ```

3. Call `mcp__swarm__swarm_report_progress` with `phase=<phase>`, `pct=<percent>`.

4. Report a one-line confirmation, e.g. `Progress reported: <phase> <percent>%.`
