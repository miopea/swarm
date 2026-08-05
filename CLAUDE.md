# Swarm — Project Guide

> See `~/.claude/CLAUDE.md` for universal rules (design principles, code quality, TDD workflow, quality gates).

## 1. Quick Reference

### Essential Rules
| Rule | Action |
|------|--------|
| Before commit | Use `/commit` slash command |
| Pre-commit validation | Use `/check` slash command |
| Bug fix | Use `/fix-and-ship` or `/diagnose` first |
| Test failures | STOP — fix before continuing |
| Warnings | STOP — warnings = failures |
| `type: ignore` | FORBIDDEN — fix the type error |
| Creating a file | SEARCH existing code first |
| Installed tool stale? | `uv tool uninstall swarm-ai && uv cache clean swarm-ai && uv tool install --no-cache .` |

### Key Files
| File | When to Check |
|------|---------------|
| `swarm.yaml` | Configuring workers, drones, queen, groups |
| `src/swarm/drones/state_tracker.py` | Debugging state detection issues (provider patterns in `src/swarm/providers/`) |
| `src/swarm/drones/pilot.py` | Understanding the poll loop and drone actions |
| `src/swarm/server/daemon.py` | Core daemon lifecycle, events, WebSocket broadcasts |
| `src/swarm/server/api.py` | All HTTP/WebSocket endpoints |
| `src/swarm/web/templates/dashboard.html` | Dashboard UI and JS |

---

## 3. Design Principles

### Architecture Guidelines
- **Event-driven decoupling** — Pilot emits events, daemon subscribes; never tight-couple components
- **Feature-based modules** — Organize by domain (worker/, drones/, queen/, tasks/), not by layer
- **Async everywhere** — All PTY/holder calls are async; all I/O is async. Never block the event loop.
- **Explicit types** — Use dataclasses and type hints; help AI and humans understand intent
- **Thin API handlers** — Validation in handlers, business logic in daemon/pilot/managers

---

## 5. Critical Rules

After making code edits, always run `uv run ruff format` before validation checks. Never commit unformatted code.

### Post-Change Validation (MANDATORY)
After making code changes, run `/check` and show the output. Do NOT report the task as complete until all checks pass with zero errors and zero warnings. If anything fails, fix it and re-run.

### Key Triggers
```yaml
IF test_fails        → STOP: Fix test before continuing
IF creating_file     → STOP: Search existing code first
IF iteration>2 && no_progress → RESET: Verify assumptions with tools
IF process_error     → CHECK: Holder running? Worker alive? ProcessError details?
IF state_not_updating → CHECK: Pilot loop alive? get_content() output? classify_worker_output?
IF code_change_not_working → CHECK: Using dev version (uv run) or installed tool?
IF command_fails     → FIX: Read error, fix syntax, retry (3x). Don't give up.
IF asked_to_verify   → ACTUALLY_CHECK: Run the command. Never assume.
```

### Command Failures — Be Persistent!
```
Command fails? → Read error, fix syntax, retry. Don't give up.
Need to verify? → Actually run the query/curl/command. Never assume.
Pattern: Try → Fix → Retry (3x) → Then ask user with details of attempts.
TDD Bug Fix: Write test (red) → Fix → Run test → Iterate (5x) → Ask if stuck.
```

---

## 6. Workflow

### Bug Fix Sequence
1. Reproduce the bug (or understand the report)
2. Use `/diagnose` to trace the full data flow
3. Write failing regression test — confirm it **fails** (red). If it passes, re-diagnose.
4. TDD loop — implement fix, run specific test (`uv run pytest tests/test_foo.py::test_name -q`), iterate until green (max 5 iterations, ask if 3x same error)
5. Run `/check` (format + lint + full test suite)
6. Document root cause in commit message

### Feature Sequence
1. Search existing code first
2. Design types/dataclasses
3. Write tests
4. Implement (tests should fail initially)
5. Iterate until all tests pass
6. Run `/check`

---

## 7. Slash Commands

**IMPORTANT**: Use these instead of running commands manually. They handle error cases and ensure consistency.

| Command | Purpose | When to Use |
|---------|---------|-------------|
| `/check` | Run pre-commit validation (ruff format + lint + pytest) | Before committing, during development |
| `/commit` | Create a git commit following conventions | When ready to commit changes |
| `/diagnose` | Trace full data flow before fixing a bug | Before any bug fix — prevents partial fixes |
| `/fix-and-ship` | Autonomous bug fix pipeline (diagnose → TDD → validate → commit) | End-to-end bug fix with one approval gate |
| `/get-latest` | Pull latest from origin/main and merge | Before starting work, after conflicts |
| `/interview` | Deep-dive requirements interview for a feature | Before building complex features |

### Command Details
- **`/check`**: Runs ruff format, ruff check, pytest. Must pass with zero warnings.
- **`/commit`**: Formats, lints, tests, drafts message, commits, optionally pushes. Run `/check` first.
- **`/diagnose`**: Maps complete architecture path before fixing. Prevents whack-a-mole debugging.
- **`/fix-and-ship`**: Full pipeline: diagnose → regression test (TDD) → fix → validate → commit + push.

```yaml
# ALWAYS use slash commands for these operations:
PRE_COMMIT: /check (not manual uv run ruff/pytest)
COMMITTING: /commit (not manual git commit)
BUG_FIXING: /fix-and-ship or /diagnose first
```

---

## Secrets

1Password is authoritative. Vault: `BFG`. Run `eval "$(op-login)"` at the start of any shell that touches a secret, and never print a value.
Full standard: `rcg-architecture/docs/standards/secrets.md`.

## Project notes

`docs/project-notes.md` — moved out of this file so it is read when
relevant rather than every session. **Check it before deriving a repo fact by
hand** (an `az` call, a directory walk, reading routes): if it is in here, the
answer is already written down.

Covers: What This Is; Autonomous task momentum; Plan-mode gate for user-request tasks; Queen message-surface elevation; Two Queens: division of labor; Verifying out-of-band task assignments; Worker identity: where it comes from, and when a fix reaches a session; Live MCP tool-surface propagation; Why three earlier attempts missed; Architecture; Key Modules; Conventions; State Machine; Dynamic workflows coexistence; Native `/loop` coexistence (task #761); Per-task token-budget governor (task #762); Standing background-improvement loops (task #765); Harness-improvement digest (operator-gated hill-climbing); ….
