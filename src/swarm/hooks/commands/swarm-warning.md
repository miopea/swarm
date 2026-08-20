---
description: Send a warning to a specific worker — API change, breakage, dependency they need to know about. Pull-only by design: this sends to other workers or dispatches work, so it carries no "use when" trigger and fires only when invoked deliberately.
argument-hint: <to-worker> <warning text>
---

Send a warning to a specific peer worker.

Args: $ARGUMENTS

1. **Validate.** If $ARGUMENTS contains fewer than 2 whitespace-separated tokens, REFUSE with:

   ```text
   Usage: /swarm-warning <to-worker> <warning text>
   ```

2. Parse: first token = `<to-worker>`; remainder = `<warning>`.

3. Call `mcp__swarm__swarm_send_message` with `to=<to-worker>`, `msg_type="warning"`, `content=<warning>`.

4. Report a one-line confirmation.
