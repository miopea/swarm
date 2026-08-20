---
description: Hand off your current task and dispatch follow-on work to another worker. Both arguments required. Pull-only by design: this sends to other workers or dispatches work, so it carries no "use when" trigger and fires only when invoked deliberately.
argument-hint: <target-worker> <task description>
---

Hand off the current task and dispatch follow-on work to another worker.

Args: $ARGUMENTS

Workflow:

1. **Validate args.** If $ARGUMENTS is empty OR contains fewer than 2 whitespace-separated tokens, REFUSE with this exact line and stop:

   ```text
   Usage: /swarm-handoff <target-worker> <task description>
   ```

   Do NOT pick a target automatically. Explicit only.

2. Parse: first whitespace-separated token = `<target-worker>`; remainder = `<description>`.

3. Call `mcp__swarm__swarm_task_status` with `filter="mine"` to find my current IN_PROGRESS task. If I have no IN_PROGRESS task, skip step 4.

4. Call `mcp__swarm__swarm_complete_task` for my current task with a brief resolution describing where I left off and why I'm handing off.

5. Call `mcp__swarm__swarm_create_task` with:
   - `title` = first sentence (or first 60 chars) of `<description>`
   - `description` = full `<description>`
   - `target_worker` = `<target-worker>`

6. Report a one-line summary, e.g. `Completed task #N, dispatched task #M to <target-worker>.`
