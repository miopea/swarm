---
description: Reports that one of your tasks is blocked by another task, which pauses idle-watcher nudges until the blocker clears. Use when work cannot proceed because it depends on another task, or when the user says something is blocked or waiting on someone else.
argument-hint: <task#> <blocked-by-task#> <reason>
---

Report a task blocker.

Args: $ARGUMENTS

1. **Validate.** Args must have at least 3 whitespace-separated tokens, where the first two are integers. If not, REFUSE with:

   ```text
   Usage: /swarm-blocker <task#> <blocked-by-task#> <reason>
   ```

2. Parse:
   - `<task_number>` = first int
   - `<blocked_by_task>` = second int
   - `<reason>` = remainder of the args

3. Call `mcp__swarm__swarm_report_blocker` with `task_number=<task_number>`, `blocked_by_task=<blocked_by_task>`, `reason=<reason>`.

4. Report a one-line confirmation, e.g. `Reported task #N blocked by #M.`
