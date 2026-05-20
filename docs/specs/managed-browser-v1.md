# Managed Browser Capability — v1

Status: **specified, not yet implemented**
Date: 2026-05-20
Interview: 4 rounds, decisions captured below
Audience: implementer (almost certainly the same model that wrote this spec)
Predecessor memories: `project_browser_idea`, `project_no_ai_slop_content_system`

---

## Problem

Workers today have two ways to look at the web — `WebFetch` and `WebSearch`
— both text-only. They can't:

- **Verify a deploy** — open the URL the change went to, confirm the new
  content actually renders.
- **Read a JS-rendered docs site** — most modern docs pages need a real
  browser to hydrate before the text appears.
- **Drive a logged-in flow** — needed for the future content system's
  YouTube analytics / social posting steps (see
  `project_no_ai_slop_content_system` memory).

This spec adds a managed browser via Playwright, exposed as a new MCP
worker tool `swarm_browse`. v1 covers verify-deploy + read-docs;
v2 will cover the content-system use cases.

## Interview decisions (2026-05-20)

| Question | Decision |
|---|---|
| Trigger mechanism | **MCP tool** — `swarm_browse(url, action, ...)` in the existing MCP server. No service handler or task type for v1. |
| Browser engine | **Playwright Python in-process.** Already installed via the mobile-QA dev dep. Sync API from the tool handler; chromium engine. |
| Session model | **Named persistent profiles + ephemeral default.** Profiles configured in `swarm.yaml`; default is a fresh context per call with no stored auth. |
| Headed vs headless | **Headless by default; `--headed` flag on the login CLI.** Daemon-side calls are always headless; profile-login command spawns headed Chromium on the operator's machine. |
| Action surface | **Single named action per call.** v1 ships 5 actions: `navigate`/`get_text`, `screenshot`, `extract_links`, `fill_form`, `click`. |
| Profile setup | **`swarm browser login <profile>` CLI command.** Spawns headed Chromium pointed at the profile's login URL; on close, saves `storage_state.json` for headless reuse. |
| Return shape | **Structured JSON** — `{url, title, text, screenshot_path, links, status}`. Screenshots saved to `~/.swarm/browser-cache/<task-id>/<n>.png`, path returned (not blob). |
| Per-call timeout | **30 s default, configurable, capped at 5 min.** |
| Domain allowlist | **Per-profile** declared in `swarm.yaml`. Default ephemeral profile has no allowlist (anything goes). |
| Confirm-before-submit | **Yes** — `click` on `[type=submit]` and `fill_form` on inputs matching `password|payment|credit_card` route through the Queen escalation surface. |
| Audit log | **Include** — every call writes a `buzz_log` entry under `LogCategory.TOOL`. |
| Worker access | **All workers**, bounded per-profile by the allowlist. No worker-level enable/disable in `swarm.yaml`. |
| Lifecycle | **One-shot** — browser spawned per call, closed after. ~1 s startup overhead; rock-solid cleanup; no memory leak. |
| youtube_scraper coexistence | **Keep + eventually upgrade.** The handler stays; when the content system phase fires it gets browser-augmented for logged-in YouTube analytics. |
| Discoverability | **Just exist** — register as an MCP tool, workers find it like any other. No CLAUDE.md hint added. |
| Spec scope | **Full v1 surface in one commit.** Spec + all 5 actions + profile system + login command + safeguards + tests + release. |

---

## Architecture

```
┌─────────────────┐    swarm_browse(url, action, profile?, ...)
│ Worker PTY      │──────────────────────┐
└─────────────────┘                      ▼
                                  ┌──────────────┐
                                  │ MCP server   │
                                  │ tools.py     │
                                  └──────┬───────┘
                                         │
                            ┌────────────▼────────────┐
                            │ BrowserService (new)    │
                            │ src/swarm/browser/      │
                            │  ├─ service.py          │
                            │  ├─ profiles.py         │
                            │  ├─ actions.py          │
                            │  └─ guardrails.py       │
                            └────────────┬────────────┘
                                         │ (sync_playwright())
                                         ▼
                            ┌─────────────────────────┐
                            │ Playwright Chromium     │
                            │ headless context        │
                            │  + storage_state[prof]  │
                            └─────────────────────────┘
```

## Module layout

```
src/swarm/browser/
  __init__.py
  service.py        # BrowserService: spawn/teardown, dispatch action
  profiles.py       # ProfileConfig dataclass, load_profiles, storage paths
  actions.py        # _action_navigate, _action_screenshot, ..._click
  guardrails.py     # timeout enforcement, allowlist check, sensitive-form detector
  cli.py            # `swarm browser login <profile>` headed-spawn command

src/swarm/mcp/
  tools.py          # + swarm_browse tool definition + dispatch into BrowserService

src/swarm/config/
  models.py         # + BrowserConfig + BrowserProfile dataclasses on HiveConfig

tests/
  test_browser_service.py
  test_browser_actions.py
  test_browser_guardrails.py
  test_browser_profiles.py
```

## Data model

```python
@dataclass
class BrowserProfile:
    name: str
    storage_path: str = ""        # filled at runtime: ~/.swarm/profiles/<name>/
    allowed_domains: list[str] = field(default_factory=list)  # glob patterns
    login_url: str = ""           # consumed by `swarm browser login`
    description: str = ""

@dataclass
class BrowserConfig:
    enabled: bool = True
    default_timeout_seconds: float = 30.0
    max_timeout_seconds: float = 300.0
    cache_dir: str = "~/.swarm/browser-cache"
    profiles: list[BrowserProfile] = field(default_factory=list)
    # Patterns that trigger the confirm-before-submit Queen escalation.
    sensitive_input_pattern: str = r"password|payment|credit_card|ssn"

# Wired onto HiveConfig as a top-level field:
@dataclass
class HiveConfig:
    ...
    browser: BrowserConfig = field(default_factory=BrowserConfig)
```

`BrowserConfig` rides the existing six-layer config-save chain — register
in `_JSON_KEYS` + `_DATACLASS_BLOBS` + the apply dispatcher just like
`PlaybookConfig` did in P4b.

## MCP tool surface

```python
swarm_browse(
    url: str,
    action: Literal["navigate", "screenshot", "extract_links", "fill_form", "click"] = "navigate",
    profile: str = "",                 # "" = ephemeral
    selector: str = "",                # CSS selector for click/fill
    fields: dict[str, str] = None,     # field-name → value for fill_form
    timeout_seconds: float = 30.0,
    wait_for_selector: str = "",       # optional: wait until visible before action
) -> dict
```

Return shape:

```json
{
  "ok": true,
  "url": "https://...",
  "final_url": "https://...",            // post-redirect
  "title": "...",
  "status": 200,
  "text": "...",                          // populated for navigate/get_text
  "screenshot_path": "/.swarm/browser-cache/abc123/1.png",  // for screenshot or any action with capture=true
  "links": [{"href": "...", "text": "..."}],  // populated for extract_links
  "elapsed_ms": 1234,
  "profile": "ephemeral"
}
```

Errors return `{"ok": false, "error": "...", "category": "timeout|allowlist|navigation|action_failed|escalation_required"}`.

## Action semantics

| Action | What it does |
|---|---|
| `navigate` | `page.goto(url)` → wait for `domcontentloaded` → return `{text, title, status}`. Default action when none specified. |
| `screenshot` | `navigate` semantics + `page.screenshot(full_page=True)` → save under cache_dir, return `screenshot_path`. |
| `extract_links` | `navigate` + `page.query_selector_all("a[href]")` → return list of `{href, text}`. |
| `fill_form` | `navigate` (or stay on current page if same URL) + for each `(selector, value)` in `fields`: detect sensitive pattern → escalate to Queen if matched → else `page.fill(selector, value)`. No submit. |
| `click` | `navigate` + `page.click(selector)`. If selector matches `[type=submit]` or `button[type=submit]` → escalate to Queen. Returns post-click `{text, url, title}`. |

## Profile system

**Configuration** (in `swarm.yaml`):

```yaml
browser:
  enabled: true
  default_timeout_seconds: 30
  max_timeout_seconds: 300
  cache_dir: ~/.swarm/browser-cache
  profiles:
    - name: youtube                    # future-content-system profile
      login_url: https://accounts.google.com/...
      allowed_domains:
        - youtube.com
        - "*.youtube.com"
        - googleusercontent.com
      description: "Logged-in YouTube account for content-system analytics scrape"
    - name: internal-docs
      login_url: https://docs.company.com/login
      allowed_domains:
        - docs.company.com
      description: "Gated company docs portal"
```

**Storage** — Playwright `storage_state.json` per profile at
`~/.swarm/profiles/<name>/storage_state.json`. Saved by the login CLI,
loaded by `BrowserContext(storage_state=...)` on every call that names
the profile.

**Default (ephemeral)** — when `profile=""` or omitted, no storage_state
loaded, no allowlist check (any URL OK). Browser context discarded after
the call.

**Login flow**:

```bash
$ uv run swarm browser login youtube
# Spawns headed Chromium pointed at login_url.
# Operator signs in, navigates as needed.
# On window close, storage_state captured to
# ~/.swarm/profiles/youtube/storage_state.json.
# Profile is now usable from headless calls.
```

## Safeguards

1. **Timeout** — every action gets `timeout_seconds` (default 30,
   capped 300). Implemented as `page.set_default_timeout(timeout * 1000)`
   plus an outer `asyncio.wait_for` on the whole dispatch. Timeouts
   return `{ok: false, error: "..."` with `category="timeout"`.

2. **Allowlist** — when `profile` is set, the URL's hostname must match
   at least one entry in `profile.allowed_domains` (glob via fnmatch).
   Non-match → return `{ok: false, error: "...", category: "allowlist"}`.
   Default ephemeral profile has no allowlist.

3. **Confirm-before-submit** — actions that match the sensitive pattern
   route through the existing Queen escalation surface:
   - `click` on a selector matching `[type=submit]` or `button[type=submit]`
   - `fill_form` on any field name matching `password|payment|credit_card|ssn`
   The escalation proposal carries the worker name, URL, selector, and
   the action's parameters. Until the operator approves, the action
   returns `{ok: false, error: "...", category: "escalation_required"}`.
   Operator approves via the existing decisions surface; tool retries
   on the worker's behalf.

4. **Audit log** — every `swarm_browse` call writes a `buzz_log` entry:
   ```
   category = LogCategory.TOOL
   action = SystemAction.BROWSER_ACTION  (new enum value)
   detail = {worker, url, action, profile, status, elapsed_ms}
   ```
   Visible in the Activity tab.

5. **Cache GC** — `browser-cache/<task-id>/` dirs older than 7 days are
   pruned on daemon startup. Screenshots take up real disk space.

## Tests

| File | What it covers |
|---|---|
| `test_browser_service.py` | Service lifecycle: spawn, dispatch, teardown. Idempotent close. |
| `test_browser_actions.py` | Each of 5 actions against a local file:// fixture page. Asserts return shape. |
| `test_browser_guardrails.py` | Timeout fires; allowlist denies cross-domain; sensitive-form escalates. |
| `test_browser_profiles.py` | Profile loads storage_state correctly; ephemeral resets every call. |

Note: live-network tests are out — fixtures use `file:///` URLs served
from `tests/fixtures/browser_pages/`. No flaky network deps.

---

## Implementation order

Single feature commit, broken into TDD increments:

1. **Module skeleton + config wiring.** `swarm/browser/` package, dataclasses, six-layer config-save-chain wiring (mirrors P4b's PlaybookConfig).
2. **Ephemeral navigate path.** `BrowserService.dispatch(action="navigate", profile="")` end-to-end with no auth, no allowlist. Tests: file:// fixture loads, text returned.
3. **MCP tool registration.** Add `swarm_browse` to `src/swarm/mcp/tools.py`. Smoke test that worker can call it.
4. **Remaining actions.** screenshot → extract_links → fill_form → click. Each gets a test against the fixture.
5. **Guardrails.** Timeout, allowlist, sensitive-pattern detection. Tests for each.
6. **Profile system.** Storage state save/load. Test with a pre-seeded storage_state.json fixture.
7. **Login CLI.** `swarm browser login <profile>` headed-spawn. Manual test only (headed requires display).
8. **Audit log + Queen escalation.** Wire `LogCategory.TOOL` write + escalation proposal creation. Existing patterns from approval rules.
9. **Cache GC on daemon startup.** Small background sweep.

## Out of scope (v2 or later)

- **Content system integration** — the YouTube logged-in analytics scrape, social-media posting, weekly content planning step. v2 builds on top of v1's profile + click/fill primitives.
- **Multi-tab / multi-window flows.** v1 is single-page.
- **Service handler version** — driving the browser from a pipeline automated step. Add when content-system pipelines materialise.
- **Cross-worker browser sharing** — every call is its own browser context. If two workers want to share a session, they use the same profile.
- **CDP devtools** — no perf metrics, network capture, etc. in v1.
- **Mobile viewport emulation** — already covered by the mobile-QA harness; not exposed as a tool action.

## Risks

- **Playwright memory footprint** — Chromium spawn per call is ~150-200 MB peak. Concurrent calls could OOM a small daemon host. **Mitigation:** semaphore in `BrowserService` to cap concurrent browsers (default 2). Document as a config knob.
- **Headed login on a daemon-only host** — operator running swarm via systemd on a remote box can't launch a headed window. **Mitigation:** the login command runs **on the operator's local dev machine**, writes the storage_state to the same path swarm.yaml references, then operator syncs the file to the daemon host. Document explicitly in the login command's help text.
- **Sensitive-pattern detector is regex-based** — it'll miss creatively-named password fields and false-positive on innocent ones. **Mitigation:** the operator can override the pattern via `browser.sensitive_input_pattern` in config. Document examples.
- **Subscription-cost surprise** — Playwright doesn't use the API, but a worker that calls `swarm_browse` in a loop could exhaust the daemon's RAM. **Mitigation:** per-worker rate limit (already exists for tool calls in the MCP server; verify it covers `swarm_browse` too).
- **Profile drift** — if a logged-in session expires, the next call returns a logged-out page silently. **Mitigation:** the operator re-runs `swarm browser login <profile>`; we don't try to auto-refresh OAuth tokens.

## Open questions

- **Should the MCP tool surface accept arbitrary Playwright code as a power-user escape hatch?** Not in v1. Single named actions are easier to safeguard. Revisit if operators ask.
- **Should screenshots be PNG or WebP?** PNG for now — universal viewer support. WebP saves ~30% disk; revisit if cache size becomes a problem.
- **Per-action cost budgeting** (similar to playbook synthesis hourly cap)? Not in v1. Add if abuse appears.
