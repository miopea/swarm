# Swarm (legacy) — Project Guide

> See `~/.claude/CLAUDE.md` for universal rules (code quality, verification, shipping vocabulary, secrets, swarm) and its routing map to `rcg-architecture/docs/standards/`.

## Orientation

- **What this is.** `swarm-ai` — **Swarm (legacy)**, a hive-mind orchestrator for
  Claude Code agents. Python ≥3.12, aiohttp daemon plus PTY workers, and two
  console scripts (`swarm` and `swarm-legacy`) that are the same entry point. Superseded by Swarm Next; this repo is maintenance-only. The
  GitHub repo is `miopea/swarm-legacy`, and this checkout now lives at
  `~/projects/personal/swarm-legacy` — `~/projects/personal/swarm` was freed
  for Swarm Next on 2026-08-21.
  **Display strings say "Swarm (legacy)"; the package name does not** — it is
  still `swarm-ai` on PyPI. The *installed* names do move, but only when the
  operator runs `swarm relocate`: `swarm` → `swarm-legacy`, `swarm.service` →
  `swarm-legacy.service`, `~/.swarm` → `~/.swarm-legacy`. Both entrypoints ship
  together so an un-relocated install keeps working.
- **How it runs here.** This box is **relocated** and now a *production*
  install: systemd **user** unit `swarm-legacy.service`,
  `ExecStart=~/.local/bin/swarm-legacy serve`, `WorkingDirectory=$HOME`,
  daemon API on **:9090**, state in `~/.swarm-legacy`. It no longer runs from
  this checkout — the tool is installed from git, so the source tree is for
  editing only and `uv run` is not in the startup path. Not a system unit —
  `systemctl --user`, not `sudo systemctl`.
- **`swarm-next` is a DIFFERENT system on the same box.**
  `swarm-next-api.service` and `swarm-next-terminal-host.service` run alongside
  this one from `~/projects/personal/swarm-next`. Restarting the wrong unit looks
  like a fix that changed nothing.
- **All state is one SQLite file, in the state dir** — `~/.swarm-legacy/swarm.db`
  after `swarm relocate`, `~/.swarm/swarm.db` before it (~99 MB). **Never
  hardcode either**: call `swarm.paths.state_dir()`, which prefers the relocated
  directory. Beside the DB, `invariant-log.jsonl` (Tier 0 hook decisions) and
  `pty-writes.jsonl`. Nothing authoritative lives in this repo's tree.
- **No deploy.** There is no production target and no `deploy.md`. Push is the
  terminal state — say `pushed`, never `shipped`.
- **CI gates on two Python versions**, `test (3.12)` and `test (3.13)`, and they
  are required checks on `main`. Both must be green before merge.

### Two `swarm.db` traps that have each cost a real investigation

Both look like a correct query returning a true zero.

- **`status` is `done`, NOT `completed`.** Querying `status='completed'` returns
  `0` from a `0` denominator, which reads exactly like "nothing has happened yet".
  Live values: `active`, `assigned`, `backlog`, `blocked`, `done`, `unassigned`.
- **`completed_at` is a unix FLOAT, not an ISO string.** A string comparison
  silently matches nothing and reports a clean zero.

Query it read-only — `sqlite3.connect(f"file:{db}?mode=ro", uri=True)` — and
print the denominator beside any count. Resolve `db` with
`swarm.paths.state_dir() / "swarm.db"`, never a hardcoded `~/.swarm`: after
`swarm relocate` that path is gone and the query reads nothing.

## Quick Reference

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
| Installed tool stale? | Follow *Dev vs Installed Version* in [`docs/project-notes.md`](docs/project-notes.md) — that is the canonical cache-busting reinstall |

### Key Files
| File | When to Check |
|------|---------------|
| `swarm.yaml` | Configuring workers, drones, queen, groups |
| `src/swarm/drones/state_tracker.py` | Debugging state detection issues (provider patterns in `src/swarm/providers/`) |
| `src/swarm/drones/pilot.py` | Understanding the poll loop and drone actions |
| `src/swarm/server/daemon.py` | Core daemon lifecycle, events, WebSocket broadcasts |
| `src/swarm/server/routes/` | HTTP/WebSocket endpoint handlers (tasks, workers, queen, jira, config, websocket, …) |
| `src/swarm/web/routes/` | Page + HTMX partial routes, login/passkeys, PWA manifest & share target |
| `src/swarm/server/api.py` | The aiohttp app factory — registers no routes itself; owns session auth, CSRF, security headers, and rate limiting |
| `src/swarm/web/templates/dashboard.html` | Dashboard markup (modals, panels, partial mount points) |
| `src/swarm/web/static/dashboard.js` | Dashboard behaviour — WebSocket wiring, terminal, task board, keyboard shortcuts |

---

## Design Principles

### Architecture Guidelines
- **Event-driven decoupling** — Pilot emits events, daemon subscribes; never tight-couple components
- **Feature-based modules** — Organize by domain (worker/, drones/, queen/, tasks/), not by layer
- **Async everywhere** — All PTY/holder calls are async; all I/O is async. Never block the event loop.
- **Thin API handlers** — Validation in handlers, business logic in daemon/pilot/managers

---

## Critical Rules

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

## Workflow

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

## Slash Commands

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

### Project-local

Checked into this repo, so they exist here whether or not the global set is installed.

`.claude/commands/`:

| Command | Purpose |
|---------|---------|
| `/test` | Run the swarm orchestration test on a dedicated port with auto-shutdown |
| `/swarm-status` | Current task, queue, peer worker state, unread messages |
| `/swarm-handoff` | Hand off the current task and dispatch follow-on work to another worker |
| `/swarm-finding` | Broadcast a finding to the swarm |
| `/swarm-warning` | Warn a specific worker (API change, breakage, dependency) |
| `/swarm-blocker` | Declare a task-dependency blocker — pauses idle-watcher nudges |
| `/swarm-progress` | Report structured phase / percent progress |

`.claude/skills/`:

| Skill | Purpose |
|-------|---------|
| `swarm-checkpoint` | Run `/check`, then commit on green or report a blocker and note the Queen on red |
| `swarm-coordinate` | Advisory peer/task survey producing a delegation suggestion — never creates or dispatches tasks |

---

## Secrets

1Password is authoritative. Vault: `BFG`. Run `eval "$(op-login)"` at the start of any shell that touches a secret, and never print a value.
Full standard: `rcg-architecture/docs/standards/secrets.md`.

## Project notes

`docs/project-notes.md` — moved out of this file so it is read when
relevant rather than every session. **Check it before deriving a repo fact by
hand** (an `az` call, a directory walk, reading routes): if it is in here, the
answer is already written down.

Covers: What This Is; Debugging the dashboard (what source-scan tests cannot see); Two running instances (editable venv vs copied tool install); Install layout (Legacy and Next coexist; nothing collides); If Next should later take the `swarm` name; The systemd unit self-heals on daemon start; Autonomous task momentum; Plan-mode gate for user-request tasks; Queen message-surface elevation; Two Queens: division of labor; Verifying out-of-band task assignments; Worker identity: where it comes from, and when a fix reaches a session; Live MCP tool-surface propagation; Why three earlier attempts missed; Architecture; Key Modules; Conventions; State Machine; Dynamic workflows coexistence; Native `/loop` coexistence (task #761); Per-task token-budget governor (task #762); Standing background-improvement loops (task #765); Harness-improvement digest (operator-gated hill-climbing); ….
