#!/usr/bin/env bash
# PreToolUse hook: query Swarm daemon for approval decision.
# Claude Code sends tool_name + tool_input on stdin as JSON.
# We forward it to the daemon and return the approval decision as JSON on stdout.
# If the daemon is unreachable or returns an error, we pass through (no decision).
#
# Active when SWARM_MANAGED=1 is set (the PTY holder exports it for every
# worker session it spawns — both autonomous workers and operator-attached
# ones). An operator who is driving a worker interactively can opt out by
# also exporting SWARM_OPERATOR=1 in that session; the hook then exits
# early and no drone rule gates their tool calls. See
# docs/hooks-operator-bypass.md for the full boundary.

# Skip if not a Swarm-managed worker
[ "$SWARM_MANAGED" != "1" ] && exit 0

# Operator escape hatch: interactive operator sessions opt out of drone rules.
[ "$SWARM_OPERATOR" = "1" ] && exit 0

SWARM_URL="${SWARM_URL:-http://localhost:9090}"

INPUT=$(cat)

# Extract tool name for quick bail-out on safe tools handled by Claude Code itself
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)
[ -z "$TOOL_NAME" ] && exit 0

# #1698: FORWARD THE WORKER'S TRUE IDENTITY, which is independent of where it is
# standing. The daemon otherwise identifies a worker by matching cwd to a configured
# path, so a worker operating inside ANOTHER worker's repo is recorded as the worker who
# OWNS that repo — the cross-repo event is invisible by construction, not merely
# unlogged. SWARM_WORKER_NAME is injected by the PTY holder at spawn.
#
# Falls back to the untouched payload if jq fails: identity is a measurement, and a
# measurement must never be able to break an approval decision.
if [ -n "$SWARM_WORKER_NAME" ]; then
  ENRICHED=$(echo "$INPUT" | jq -c --arg w "$SWARM_WORKER_NAME"     '. + {swarm_worker_name: $w}' 2>/dev/null)
  [ -n "$ENRICHED" ] && INPUT="$ENRICHED"
fi

AUTH_HEADER=""
[ -n "$SWARM_API_PASSWORD" ] && AUTH_HEADER="Authorization: Bearer $SWARM_API_PASSWORD"

# Query the daemon for an approval decision
RESPONSE=$(curl -s --max-time 4 -X POST "$SWARM_URL/api/hooks/approval" \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  ${AUTH_HEADER:+-H "$AUTH_HEADER"} \
  -d "$INPUT" 2>/dev/null)

# If curl failed or returned empty, pass through (let Claude Code handle it normally)
[ -z "$RESPONSE" ] && exit 0

# Extract the decision from the daemon response
DECISION=$(echo "$RESPONSE" | jq -r '.decision // empty' 2>/dev/null)

# BOTH PreToolUse schema forms are emitted, deliberately (#1588).
#
# `decision` is the LEGACY form. #1528 established by reading the shipped Claude Code
# binary (2.1.231) that it still works: the handler runs an unconditional
# `if (e.decision) switch(...)` that assigns the same `permissionBehavior` as the modern
# `hookSpecificOutput.permissionDecision` branch a few lines later. Nothing is broken.
#
# But that binary's own reference calls the field "deprecated for PreToolUse". The day a
# release drops the branch, every approval here becomes a silent no-op fleet-wide — this
# script still exits 0, the daemon still logs "approve", the buzz log still says the drone
# approved it, and nothing takes effect. Emitting both costs nothing and removes that
# failure mode: the handler processes `decision` first and `permissionDecision` second,
# both writing the same variable, so the new form wins wherever it is understood and the
# legacy form carries any version that is not. No version detection needed.
#
# The two must never disagree — a mismatch would make the outcome depend on which branch a
# given release happens to run, which is worse than being broken because it is intermittent
# and version-dependent. Tests pin approve->allow and block->deny together.
case "$DECISION" in
  approve)
    echo '{"decision":"approve","hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
    ;;
  block)
    REASON=$(echo "$RESPONSE" | jq -r '.reason // "Blocked by Swarm drone rules"' 2>/dev/null)
    # printf, not echo: `echo` appends a newline that `jq -Rs` would slurp into the string,
    # so the reason arrived with a trailing "\n". It now feeds TWO fields, and a reason that
    # differed between them by an invisible character is the kind of mismatch nobody finds.
    ESCAPED=$(printf '%s' "$REASON" | jq -Rs .)
    echo "{\"decision\":\"block\",\"reason\":$ESCAPED,\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"deny\",\"permissionDecisionReason\":$ESCAPED}}"
    ;;
  *)
    # No decision or unknown — pass through
    exit 0
    ;;
esac

exit 0
