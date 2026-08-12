---
description: Share a finding — to named peers, or to the Queen when it affects everyone.
argument-hint: <freeform finding text> [| to: worker1, worker2]
---

Send a finding.

Args: $ARGUMENTS

If $ARGUMENTS is empty, REFUSE with this exact line and stop:

```text
Usage: /swarm-finding <freeform finding text> [| to: worker1, worker2]
```

**Broadcast to `*` is Queen-only** (operator ruling 2026-08-12) — a worker calling it is
refused before anything is sent, so this command must not attempt it.

Decide which of these applies and do exactly one:

1. **The finding names specific peers** — either because the args end with
   `| to: worker1, worker2`, or because the content is plainly about particular repos
   (a changed API those consumers call, a broken build, a file you are about to move).
   Call `mcp__swarm__swarm_send_message` once per named recipient with
   `type="finding"` and `content=<the finding text, without the `| to:` suffix>`.
   Naming them costs a sentence and spares everyone else an interrupt.

2. **You believe it genuinely affects the whole fleet.** Call
   `mcp__swarm__swarm_send_message` with `to="queen"`, `type="finding"`, and content that
   is the finding text plus one closing line saying you think it warrants a fleet-wide
   broadcast and why. The Queen holds the fan-out authority and the cross-repo context to
   judge whether it does.

Prefer (1) when you can name the affected workers. Reach for (2) when you genuinely cannot.

Then report a one-line confirmation naming who it went to.
