# Multi-LLM Provider Support: Research & Architecture Reference

> [!IMPORTANT]
> **Swarm (legacy) is maintenance-only.** Active development moved to
> [Swarm Next](https://github.com/miopea/swarm-next). Everything proposed below
> is a record of what *was* planned here, not a commitment — treat unshipped
> items as candidate input for Swarm Next rather than upcoming work in this repo.

> **Purpose**: Standalone reference document for adding Gemini CLI and Codex CLI as
> alternative worker backends alongside Claude Code. Not an immediate implementation
> plan — a well-researched guide to return to when ready.
>
> **Status update (2026-04-22)**: **Phase 1 (extraction refactor) has SHIPPED.**
> Provider abstraction lives at `src/swarm/providers/` with `claude.py`, `gemini.py`,
> `codex.py`, `opencode.py`, `generic.py`, `styled.py`, `tuned.py`. Worker startup
> now goes through `provider.worker_command()` (see `src/swarm/providers/base.py`).
> Only Claude is fully production-ready; the other provider modules are stubs.
> Sections 2.1 – 2.5 below describe the PRE-refactor coupling; section 3+ is still
> forward-looking for the Gemini / Codex implementation work.

---

## 1. Executive Summary

Swarm's PTY transport layer is **already CLI-agnostic** — it spawns a command, reads bytes
from a ring buffer, and sends bytes back. The coupling to Claude Code lives entirely in the
**interpretation layer**: state detection regex patterns, drone approval logic, headless
invocations (`claude -p`), and auxiliary features (session tracking, hooks, slash commands).

Supporting Gemini CLI or Codex CLI requires:
1. **Extracting** Claude-specific logic into a provider abstraction (mechanical refactor)
2. **Implementing** per-provider state detection patterns (the hard part — each CLI has
   completely different terminal output for idle/busy/approval states)
3. **Adapting** headless invocation and approval response strategies per provider

The extraction refactor (Phase 1) delivers standalone value by cleaning up the codebase,
even if no other providers are added.

---

## 2. Current Claude Coupling: Complete Inventory

### 2.1 Worker Startup Command — ~~4 locations — LOW effort~~ SHIPPED

*Post-refactor:* all worker startup now goes through `provider.worker_command()` on
the provider instance. Implementations:

| Provider | `worker_command()` location |
|----------|------------------------------|
| Claude | `src/swarm/providers/claude.py` |
| Gemini / Codex / OpenCode / generic / tuned | `src/swarm/providers/{name}.py` (stubs) |

The old hardcoded `["claude", "--continue"]` strings have been removed from
`worker/manager.py`, `pty/holder.py`, and `pty/pool.py`.

### 2.2 State Detection Patterns (22 references — HIGH effort)

*Post-refactor:* per-provider state detection now lives in the provider modules
(`src/swarm/providers/claude.py` for the Claude patterns: `_RE_PROMPT`,
`_RE_CURSOR_OPTION`, `_RE_HINTS`, `_RE_ACCEPT_EDITS`, `"esc to interrupt"`,
`"? for shortcuts"`, etc.). The drone classifier in
`src/swarm/drones/state_tracker.py` consults the active provider's patterns.
The old `src/swarm/worker/state.py` monolith is gone.

### 2.3 Drone Auto-Approval Rules (1 key pattern — MEDIUM effort)

Two distinct mechanisms, and they are easy to confuse:

- `src/swarm/drones/rules.py` — `_SAFE_TOOL_NAMES`, a `frozenset` of
  `{"Glob", "Grep", "Read", "WebSearch", "WebFetch"}`. Used by
  `_is_safe_tool_event()` against a structured `tool_call` event. This is a
  **set-membership test, not a regex**, and it is provider-neutral because the
  event carries a parsed `tool_name`.
- `src/swarm/providers/claude.py` — `_BUILTIN_SAFE_PATTERNS`, the regex fallback
  for scraped PTY text (`Bash(...)` / `Bash command` / `Glob(` / `Read file` …),
  returned by `ClaudeProvider.safe_tool_patterns()`. Claude-specific by
  construction; other providers supply their own.

### 2.4 Queen / Headless Invocations (30 references — HARD)

`src/swarm/queen/queen.py` — `["claude", "-p", ...]`, `--output-format json`,
`--resume`, session management, JSON envelope parsing.

### 2.5 Auxiliary Features (21 references — MEDIUM)

- `src/swarm/tasks/task.py` — `claude -p` for smart title generation
- `src/swarm/testing/report.py` — `claude -p` for AI-powered analysis
- `src/swarm/worker/usage.py` — `~/.claude/projects/` session JSONL
- `src/swarm/hooks/install.py` — `~/.claude/settings.json`
- `src/swarm/tasks/workflows.py` — Claude Code slash commands

---

## 3. Provider Comparison: CLI Terminal Behavior

### 3.1 Claude Code

| Aspect | Details |
|--------|---------|
| **Launch (interactive)** | `claude` or `claude --continue` |
| **Launch (headless)** | `claude -p "prompt" --output-format json\|text [--resume ID] [--max-turns N]` |
| **Busy indicator** | `"esc to interrupt"` |
| **Idle prompt** | `> ` or `❯ ` cursor, `"? for shortcuts"` |
| **Approval prompts** | Numbered choice menu: `> 1. Always allow` / `  2. Yes` / `  3. No` |
| **User questions** | `"Type something"` / `"Chat about this"` |
| **Accept edits** | `">> accept edits on (shift+tab to cycle)"` |
| **Interrupt** | Esc or Ctrl+C |
| **Session storage** | `~/.claude/projects/<encoded-path>/` JSONL |
| **JSON response** | `{"type": "result", "result": "...", "session_id": "..."}` |
| **Approval response** | Enter (selects highlighted option) |

### 3.2 Gemini CLI

| Aspect | Details |
|--------|---------|
| **Launch (interactive)** | `gemini` |
| **Launch (headless)** | `gemini -p "prompt" [--output-format json\|text]` |
| **Resume session** | `gemini --resume` or `gemini --resume <UUID>` |
| **Busy indicator** | `💬 ` emoji, `⠏` braille spinner, `"(esc to cancel, Xs)"` |
| **Idle prompt** | `gemini>` |
| **Approval prompts** | `"Approve? (y/n/always)"` |
| **Approval modes** | `--approval-mode default\|auto_edit\|yolo` or `--yolo` |
| **Approval response** | `y\r` (yes), `n\r` (no), `always\r` |
| **Tool names** | `run_shell_command`, `EditFile`, `FindFiles`, `ReadFile`, `WriteFile`, `SearchText`, `GoogleSearch`, `WebFetch` |
| **Known issues** | Orphaned processes at 100% CPU after terminal close |

### 3.3 Codex CLI (OpenAI)

| Aspect | Details |
|--------|---------|
| **Launch (interactive)** | `codex` (full-screen Ratatui TUI) |
| **Launch (headless)** | `codex exec "prompt" [--json]` (JSONL event stream) |
| **Busy indicator** | `▶` (Run), `▷` (Think) — Ratatui rendered |
| **Idle indicators** | `◇` (Idle), `□` (Free) |
| **Alternate screen** | **YES by default** — `--no-alt-screen` to disable |
| **Approval modes** | `--full-auto` / `-a on-request\|never\|untrusted` |
| **JSONL events** | `thread.started`, `turn.completed`, `item.completed` |
| **Key risk** | Alternate screen buffer makes PTY text detection unreliable |

---

## 4. Architecture

### 4.1 Provider Module Structure

```
src/swarm/providers/
├── __init__.py          # ProviderType enum, get_provider() factory
├── base.py              # Abstract base class (LLMProvider)
├── claude.py            # Claude Code provider (production)
├── gemini.py            # Gemini CLI provider (experimental)
├── codex.py             # Codex CLI provider (experimental)
├── opencode.py          # OpenCode provider (community, experimental)
├── generic.py           # Fallback provider used for custom CLIs defined via `custom_llms` in swarm.yaml
├── styled.py            # Wrapping provider that applies display/styling overrides (identity, color, label) on top of a base provider
├── tuned.py             # Wrapping provider that applies per-provider pattern overrides from `provider_overrides` in swarm.yaml
└── events.py            # Shared state-detection event types used by all providers
```

**Provider stack at a glance:**

- `base.py` defines the abstract contract every provider implements (launch command, state regexes, approval keys).
- `claude.py` / `gemini.py` / `codex.py` / `opencode.py` are the concrete CLI adapters.
- `generic.py` is used when a worker specifies a command that isn't one of the built-ins — driven by the `custom_llms` block in `swarm.yaml`.
- `styled.py` and `tuned.py` are *decorators*: they wrap a base provider with display and tuning overrides without duplicating core logic. See `provider_overrides` in the README for the YAML surface.
- `events.py` holds the event enum emitted by state detection (busy, idle, waiting, approval, etc.) so higher layers can stay provider-agnostic.

### 4.2 Config

```yaml
provider: claude  # Global default: "claude" | "gemini" | "codex"

workers:
  - name: worker-1
    path: ~/projects/foo
    # provider: claude  (inherits global default)
  - name: experiment
    path: ~/projects/bar
    provider: gemini   # Per-worker override
```

### 4.3 Claude-only config surfaces

A few config blocks are Claude-specific and ignored by other providers
because only the Claude Code CLI surfaces the corresponding mechanisms:

```yaml
# Opt-in Claude Code native sandbox. No-op for Gemini / Codex workers.
sandbox:
  enabled: true
  min_claude_version: "2.0"
  settings_overrides:
    allow_network: false
    denied_tools: ["Bash"]
```

`test.pin_model` belongs in **neither** of the blocks above and is **not a
`swarm.yaml` key**. It is a field on `TestConfig`
(`src/swarm/testing/config.py`), and `config/loader.py` does not read it out of
the YAML `test:` section — it is absent from `_KNOWN_TEST_KEYS`, so writing it
there earns an unknown-key warning and no effect. Set it per run:

```bash
swarm test --pin-model=claude-opus-4-7
```

It pins the model identifier recorded in every `swarm test` run's
`InfraSnapshot`. Not a runtime override — it only affects reporting. (The config
API can also set it, via the `test` applier's generic dataclass dispatch.)

---

## 5. Key Risks

1. **Codex Alternate Screen Buffer (HIGH)** — Ratatui renders to alternate buffer,
   making PTY text detection unreliable. May need `--no-alt-screen` or JSONL monitoring.
2. **Gemini Orphaned Processes (MEDIUM)** — Known bug with no SIGHUP handler.
3. **Codex Interactive Deadlocks (MEDIUM)** — Hangs on terminal prompts.
4. **Approval Semantics Mismatch** — Each provider has different approval UX.

---

## 6. Implementation Phases

- **Phase 1**: Extract Claude provider (refactor — no new functionality)
- **Phase 2**: Gemini CLI provider (requires empirical PTY capture)
- **Phase 3**: Codex CLI provider (requires alternate screen investigation)
- **Phase 4**: Provider-aware features (queen, tasks, usage tracking)
