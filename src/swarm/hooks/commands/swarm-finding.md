---
description: Share a finding with the swarm — broadcast info you discovered that peers should know.
argument-hint: <freeform finding text>
---

Send a finding to the swarm.

Args: $ARGUMENTS

If $ARGUMENTS is empty, REFUSE with this exact line and stop:

```text
Usage: /swarm-finding <freeform finding text>
```

Otherwise, call `mcp__swarm__swarm_send_message` with `to="*"`, `msg_type="finding"`, and `content=<args>`. Then report a one-line confirmation.
