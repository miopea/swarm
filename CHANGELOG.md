# Changelog

Swarm uses calendar versioning (`YYYY.M.D.patch`) — see `pyproject.toml` for the current version. Notable changes since the initial v1.0.0 release are grouped below.

## Unreleased

### Features

### Changes

### Fixes

## [2026.5.25.5] - 2026-05-25

### Features

### Changes

- **Remove 11 backward-compat `@property` shims from `SwarmDaemon`.**
  When subsystems were progressively extracted (`BroadcastHub`,
  `ResourceMonitor`, `EscalationHandler`, `StatePublisher`), each
  refactor left behind delegation properties on the daemon so external
  callers wouldn't break. Audited the actual usage and migrated every
  caller to the extracted service directly:
  - `daemon.ws_clients` / `daemon.terminal_ws_clients` → `daemon.hub.*`
    (callers: `pty/bridge.py`, `routes/websocket.py`, several tests).
  - `daemon._broadcast_hook` → `daemon.hub._broadcast_hook` (callers:
    `daemon._on_ws_broadcast` setup in `run_daemon`, tests).
  - `daemon._notification_history` → `daemon.escalation._notification_history`
    (callers: `routes/drones.py` notification history endpoint, tests).
  - `daemon._state_dirty` / `_state_debounce_handle` /
    `_state_debounce_delay` → `daemon.publisher.*` (callers: tests
    plus daemon's own `_mark_state_dirty` / `_flush_state_broadcast`
    methods, which were updated to thread `pub = self.publisher`
    once instead of bouncing through the shim per field).
  - `daemon._broadcast_pending` / `_broadcast_latest` /
    `_resource_snapshot` / `_prev_pressure_level` had **zero**
    external callers — pure dead shim. Deleted outright.
  Result: -85 LOC of pure indirection, no behaviour change, no public
  API surface change (the shims were on private attributes anyway).
  The 3 `BackgroundLoopRunner` shims (`_heartbeat_task`, `_usage_task`,
  `_mtime_task`) from 2026.5.25.2 stay — they're 24 hours old and
  the cost of churning tests off them outweighs the indirection.

### Fixes

## [2026.5.25.4] - 2026-05-25

### Features

### Changes

- **Remove dormant verifier wiring from `SwarmDaemon`.** Audit
  surfaced that `_init_verifier_drone` was defined in commit `4249a39`
  (`feat(verifier): tiered verifier drone — adversarial
  post-completion check`) but the activation call site was never
  added — `_init_verifier_drone` had zero callers, so
  `self.verifier_drone` was never set, and `_fire_verifier`'s
  `getattr(self, "verifier_drone", None)` always returned `None`. The
  verifier code path has been dormant in production since landing.
  Removed: `_init_verifier_drone`, `_verifier_diff`,
  `_verifier_check_evidence`, `_verifier_peer_warnings`,
  `_verifier_send_warning`, `_verifier_escalate`, and `_fire_verifier`
  (~115 LOC of dead code). The `complete_task` `verify` kwarg is
  preserved on the public API (queen_force_complete_task still passes
  `verify=False` to leave a SKIPPED stamp) — the `verify=True` branch
  is now a no-op. `_log_verifier_skip` stays (it's live on the
  force-complete path). The `VerifierDrone` class and its 70 unit
  tests in `tests/test_verifier_drone.py` are unchanged; if the
  verifier ever comes off the shelf, the wiring is documented in
  commit `4249a39`. `test_complete_task_default_verify_runs_verifier_when_wired`
  renamed and re-docstringed to reflect the no-op semantics.

### Fixes

## [2026.5.25.3] - 2026-05-25

### Features

### Changes

- **Remove dead Jira delegation shims from `SwarmDaemon`.** Six
  methods (`_fire_jira`, `_fire_jira_export`, `_fire_jira_assign`,
  `_fire_jira_completion`, `_run_jira_import`, `jira_export_status`)
  plus `_jira_sync_loop` were one-line forwarders to methods that
  already existed on `JiraService` since the service was extracted.
  Two of them (`_run_jira_import`, `jira_export_status`) had zero
  callers anywhere in the codebase — pure dead code. The four
  `_fire_jira_*` shims had seven internal callers inside daemon.py;
  those now call `self.jira_svc.fire_*` directly. `_jira_sync_loop`'s
  one caller (the `BackgroundLoopRunner` registration in `start()`)
  now points at `self.jira_svc.sync_loop` directly. Net: -30 LOC of
  pure indirection, zero behaviour change.

### Fixes

## [2026.5.25.2] - 2026-05-25

### Features

### Changes

- **SwarmDaemon background-loop lifecycle hoisted into
  `BackgroundLoopRunner`** (`src/swarm/server/loop_runner.py`). Before
  this commit each periodic loop was wired inline: a
  `self._foo_task = asyncio.create_task(self._foo_loop())` line in
  `start()` and a matching entry in the cancellation tuple in
  `_cancel_timers`. The two lists drifted whenever a loop was added
  (resource, backup, db_maintenance, playbook_consolidation each
  needed an edit in both sites), and a missed cancellation handle
  would leak the task across `os.execv` reloads. The runner
  centralises the lifecycle:
  `register(name, factory, *, enabled=True)` collects loops;
  `start_all()` materialises tasks (idempotent — already-live entries
  are skipped); `start(name)` covers the late-enable path used by
  `reload_config` for the resource monitor; `cancel_all()` cancels
  every registered task and awaits them under
  `gather(return_exceptions=True)` so shutdown never raises on a
  worker that already errored. Loop *bodies* still live on
  `SwarmDaemon` because they're tightly coupled to daemon state —
  moving them would require plumbing ~25 closures into the runner
  constructor and split one god class into two. The win that matters
  is separating lifecycle plumbing from business logic; that's what
  this module does. Backward-compat `@property` shims for
  `_heartbeat_task` / `_usage_task` / `_mtime_task` keep the daemon
  tests that directly assigned those attributes working without a
  parallel rename pass. 14 new unit tests in
  `tests/test_loop_runner.py` pin register / start / cancel semantics
  including the idempotent-restart case, single-loop start,
  done-task replacement, and exception-swallowing cancel.

### Fixes

## [2026.5.25] - 2026-05-25

### Features

- **New focused unit tests for three core modules.** Added
  `tests/test_oversight_handler.py` (14 tests) covering the
  signal-to-intervention dispatch in `swarm.drones.oversight_handler`
  — guard clauses, park-proposal emission, rate-limited evaluation,
  operator-engagement skip, redirect message sanitisation;
  `tests/test_state_tracker.py` (37 tests) covering
  `WorkerStateTracker` public surface plus the small private helpers
  the pilot loop depends on (`_build_safe_pattern`, content
  fingerprinting, idle counter, rate-limit debounce, diminishing
  returns, context-pressure thresholds, dead-worker cleanup); and
  `tests/test_queen_tools.py` (55 tests) covering every Queen MCP
  tool — permission gates for non-Queen callers, validation errors
  for missing required args, audit-reason gates, and happy-path
  side-effects on the daemon mock. The three modules were previously
  exercised only indirectly through integration tests; these add
  unit-level coverage that pins behaviour without spinning up a
  daemon.

### Changes

- **SSE keepalive poll loosened from 0.5 s to 5.0 s
  (`src/swarm/mcp/server.py`).** Each long-lived MCP client opened a
  `while True` loop that woke twice per second just to check whether
  the underlying transport had closed. Disconnect detection isn't
  user-visible — broadcast notifications fire on the broadcast call,
  not the poll tick — so the tighter cadence was pure idle CPU. With
  a typical operator running ~5–10 Claude Code sessions concurrently,
  this drops ~10–20 unnecessary wakeups per second per daemon.
- **Dropped a stale `# type: ignore[name-defined]` in
  `src/swarm/drones/pilot.py:53`.** The comment was attached to a
  function definition; the `Any` typevar it referenced is imported at
  module top, so the suppression silenced an error that couldn't
  occur. Removed.
- **De-duplicated repeated `asyncio.get_event_loop().time()` reads in
  `src/swarm/tunnel.py:121-123`.** Three calls in three lines became
  one local-variable assignment, both for clarity and to avoid the
  per-call hash-lookup into the loop registry.

### Fixes

## [2026.5.23.2] - 2026-05-23

### Features

- **Right/Left arrow custom-button actions.** The worker action-button
  picker (Config → Workers) already offered Arrow Up and Arrow Down for
  navigating Claude Code's plan-mode approval prompts and other arrow-
  driven TUIs. Right and Left are now in the same dropdown — useful for
  TUIs that put choices on a horizontal axis (`Y/n` style approvals,
  carousel pickers, file-tree navigation). End-to-end wiring follows
  the existing pattern: ANSI escape (`\x1b[C` / `\x1b[D`) sent through
  `PtyProcess.send_arrow_right` / `send_arrow_left` →
  `WorkerService.arrow_right_worker` / `arrow_left_worker` → daemon
  delegate → `POST /action/arrow-right/{name}` / `arrow-left/{name}` →
  `sendSpecialKey('arrow-right' / 'arrow-left')` from the
  `doAction(action, ...)` dispatcher. The config template (both
  server-rendered options and the JS `buildActionBtnRow` factory) lists
  both alongside the existing Arrow Up / Arrow Down options.

### Changes

### Fixes

- **Drag-reorder for custom buttons now works on mobile.** The
  drag-and-drop reorder on Config → Workers (Action Buttons, Task
  Buttons, Tool Buttons) only wired HTML5 `dragstart` / `dragover` /
  `drop`, which never fire on iOS Safari or most Android touch
  browsers — the rows looked draggable but couldn't actually be
  reordered from a phone. `initDragReorder` in `config.html` now
  carries a touch path that mirrors the desktop flow: a 250 ms
  long-press on a row enters drag mode (with haptic feedback if
  `navigator.vibrate` is available); `touchmove` blocks scroll, uses
  `document.elementFromPoint` to find the row under the finger, and
  paints the same `drag-over` indicator; `touchend` reuses the
  insert-before-vs-after midpoint logic to drop. Touches starting on
  an `input` / `select` / `textarea` / `button` / `label` never
  initiate a drag, so the row's editable fields stay tappable; a
  finger that wanders more than 8 px before the long-press timer
  fires cancels (treated as a scroll). Desktop drag behavior is
  unchanged.

## [2026.5.23] - 2026-05-23

### Features

- **Plan-mode gate for user-request tasks.** Tasks originating from a
  user channel (Jira sync, email import, or the operator dashboard —
  i.e. anything where `SwarmTask.source_worker` is empty) now ship
  with a plan-mode preamble prepended to the dispatch message. The
  worker is instructed to investigate read-only, present a concrete
  plan via Claude Code's `ExitPlanMode`, and park in `WAITING` until
  the operator approves from the dashboard. After approval the worker
  executes the agreed plan. Worker-to-worker handoffs (cross-project
  tasks, MCP `swarm_create_task` with a sender, and the inter-worker
  auto-handoff drone — now correctly tagged with `source_worker`) skip
  the gate entirely: the originating worker has already done the
  reasoning, so a second plan round would just slow the swarm. Wired
  through a single chokepoint in `build_task_message`
  (`src/swarm/server/messages.py`); the preamble explicitly warns
  workers not to fire `/feature` / `/fix-and-ship` skills or call
  `swarm_complete_task` before approval. The behavior is gated by
  `DroneConfig.user_request_plan_mode` (default `True`) — set to
  `False` in `swarm.yaml` to revert to legacy fire-and-forget dispatch.

### Changes

### Fixes

## [2026.5.21.8] - 2026-05-21

### Changes

- **Shared screenshots always route to the Queen.** The Web Share Target
  flow previously read `localStorage.swarm.lastActiveWorker` to decide
  which worker's PTY should receive the shared file — a guessing game
  that mis-routed often enough that the operator had to re-attach by
  hand. Now `checkShareIntent` in `dashboard.js` sends straight to
  `queen` whenever the share has files; the operator tells the Queen
  which worker should pick it up (she can forward via
  `queen_prompt_worker` / `swarm_send_message`). The fallback task
  modal also defaults to Queen, and the `ccMobileFocus` Queen-focus
  hack that wrote `lastActiveWorker = 'queen'` is gone — it was only
  there to paper over the now-removed heuristic.

## [2026.5.21.7] - 2026-05-21

### Fixes

- **CC Queen focus toggle now updates `lastActiveWorker`.** Operator
  follow-up: shared a screenshot while looking at the Queen panel
  (via the mobile Attention/Queen focus toggle) and it routed to
  the `swarm` worker, not the Queen. Pattern: the
  `localStorage.swarm.lastActiveWorker` value was only written by
  `selectWorker()` — the sidebar click handler. The mobile CC focus
  toggle was a separate mechanism that just flipped a body CSS
  class to show/hide panels; it never told the share-target flow
  "the Queen is your active terminal now."

  `ccMobileFocus()` now writes `'queen'` to `lastActiveWorker` when
  the operator picks Queen focus. The attention-focus path
  intentionally doesn't write — the Attention panel spans all
  workers (it's the inbound-escalations surface), so a share while
  attention is focused should fall back to whichever sidebar
  worker was last clicked.

### Features

### Changes

### Fixes

## [2026.5.21.6] - 2026-05-21

### Fixes

- **Web Share Target into worker: don't auto-press Enter.** Operator
  follow-up — the screenshot shared into a worker submitted before
  they could add context. Mobile typing is slow; auto-Enter shipped
  the path without prose.

  Threaded an `enter` kwarg through `daemon.send_to_worker` →
  `worker_service.send_to_worker` → `PTY.send_keys(enter=...)`.
  `POST /api/workers/<name>/send` now accepts an optional
  `"enter": false` in the JSON body (default True preserves the
  prior contract for every existing caller). The share-target JS
  passes `enter: false` so the bracketed `[/path/to/file]` lands in
  the PTY's input buffer, focus switches to the worker, and the
  operator can add prose before pressing Enter themselves. Toast
  updated: "Attached N to <worker> — add context + press Enter."

- **Dashboard URL no longer pasted alongside the path.** Same flow:
  when sharing FROM the PWA, the OS share sheet auto-attaches the
  current page URL as the `url` field — which is the dashboard's
  own host. That ended up in the worker's input buffer as noise.
  JS now drops `share.url` when it parses to the same host as
  `window.location.host`. Cross-app shares (e.g. sharing a tweet,
  a video URL, an article) still include the URL as expected.

### Features

### Changes

### Fixes

## [2026.5.21.5] - 2026-05-21

### Changes

- **Share-target default behavior: route into the active worker's PTY,
  not the New Task modal.** Operator follow-up after `.21.4`: "it
  shared into the app but opens as a task, not just an image into the
  open worker." The original `.21.3` design created a task; the
  operator wanted the screenshot to land directly in whatever worker
  was currently active. This release flips the default.

  New flow when the share lands:
  - Dashboard JS reads `localStorage.swarm.lastActiveWorker` (set by
    `selectWorker()` whenever the operator focuses a worker).
  - If that's set AND the share carries at least one file: build a
    message of `[/abs/path/to/file]` tokens (Claude Code parses
    those as image attachments) + any shared text/url, then POST to
    `/api/workers/<name>/send`. Toast: "Sent N attachment(s) to
    <worker>". Switches focus to the worker so the operator sees the
    result land in the PTY immediately.
  - If no last-active worker OR no file was shared: falls back to
    the New Task modal pre-filled with attachments — the original
    behavior, kept as a safety net.
  - Any send-to-worker failure (HTTP error, worker not found,
    transient state) also drops back to the task modal so the share
    isn't lost — toast surfaces the underlying error.

  Verification: `scripts/check_share_target.py` now exercises BOTH
  paths in one run. With `localStorage.swarm.lastActiveWorker` set,
  the task modal stays closed (`task-modal opened: False`); cleared,
  the modal opens (`task-modal opened: True`). Live screenshot
  capture confirms the toast "Sent 1 attachment(s) to
  public-website" + the dashboard's focus switch to that worker.

  Caught a test-script bug while verifying: the dashboard's boot
  code at `dashboard.js:9667` restores the previously-selected
  worker from sessionStorage via `selectWorker()`, which re-writes
  `localStorage.swarm.lastActiveWorker`. To simulate a
  never-selected state in the fallback test, both storages must
  be cleared.

## [2026.5.21.4] - 2026-05-21

### Fixes

- **Web Share Target was 403'ing on real iOS / Android shares.** Hot on
  the heels of `.21.3` — the operator tried it and got "Origin
  rejected." Root cause: iOS Safari and Android Chrome send Web
  Share Target POSTs with `Origin: null` (because the share is
  initiated by the OS share sheet, not by a page). Our CSRF
  middleware rejects any cross-origin mutating request, including
  the `null` origin.

  Fix: exempt `/share-receive` from the origin check. The session
  cookie still travels with the PWA — and the session-auth
  middleware still runs — so we trust the cookie as the auth
  signal. The X-Requested-With check (applied only to `/api/*` and
  `/action/*` paths) doesn't apply to `/share-receive` anyway, so
  no further bypass needed.

  Verified by replaying the iOS POST shape via curl with
  `Origin: null` — server now returns 303 → `/?share=<id>` instead
  of 403.

## [2026.5.21.3] - 2026-05-21

### Features

- **PWA Web Share Target — share screenshots from phone to Swarm.** When
  the dashboard PWA is installed on iOS (Safari ≥ 16.4) or Android
  (Chrome), "Swarm" now appears in the OS share sheet alongside Mail /
  Messages / Notes / etc. Take a screenshot, tap Share, pick Swarm →
  the screenshot lands as an attachment on a pre-filled New Task
  modal in the dashboard. Title and any shared text/URL pre-populate
  too. If the operator was last looking at a specific worker
  (tracked in `localStorage.swarm.lastActiveWorker`), that worker is
  pre-selected in the assignee dropdown so Submit → task auto-routes
  to "whatever terminal was active."

  Implementation:
  - PWA manifest declares `share_target` with `method: POST`,
    `enctype: multipart/form-data`, file accept covers `image/*`,
    `text/*`, `application/pdf`.
  - New `POST /share-receive` endpoint accepts the OS multipart POST,
    saves attachments via the existing `daemon.save_attachment` path
    (lands in `~/.swarm/uploads/`), stashes the payload + filenames
    in an in-process cache, 303-redirects to `/?share=<id>`.
  - `GET /share/<id>` is single-shot — first caller gets the payload,
    subsequent calls 404. 5-minute TTL keeps interrupted shares from
    lingering.
  - Dashboard JS detects `?share=<id>` on load, fetches the payload,
    opens the New Task modal pre-filled with title + description +
    attached file thumbnails via the existing `taskModalAttachmentPaths`
    + `addThumbnail` path (same flow the email-drop already uses).
    Query string cleaned via `history.replaceState` so refresh
    doesn't re-trigger.
  - `selectWorker()` now also writes `localStorage.swarm.lastActiveWorker`
    alongside the existing sessionStorage entry — sessionStorage
    doesn't survive the OS share-sheet → browser bounce; localStorage
    does. The share landing reads localStorage to pre-select the
    assignee.

  Verification: `scripts/check_share_target.py` simulates a Web Share
  Target POST via Playwright's request context, follows the redirect,
  asserts the modal opens with the title / description / thumbnail
  populated and the URL cleaned. Captures
  `docs/qa-share-target.png` for the record.

### Changes

### Fixes

## [2026.5.21.2] - 2026-05-21

### Fixes

- **Mobile dashboard tighter — three operator complaints addressed.**
  - **Worker search + state filter chips hidden** under 600 px. They
    were eating vertical space on a phone where the worker list is
    short anyway and the workers themselves are visible / tappable
    right below.
  - **Focus toggle buttons (Attention / Queen) sized to content** —
    they were `flex: 1` so each claimed half the row width, which
    the operator called "huge." Now `flex: 0 0 auto` + 0.4/0.8 rem
    padding. Still satisfies the 44 px touch min-height.
  - **Queen action buttons wrap inline instead of locking to a
    2-column grid.** Was `display: grid; grid-template-columns:
    1fr 1fr` so every button (Refresh / Continue / 1 / 2 /
    Get Latest / Clear / Kill / Revive) claimed 50% of the screen.
    Operator: "they should only be as wide as they need to be so
    more fit on one line." Now `display: flex; flex-wrap: wrap`
    + content-sized buttons. Six fit per row instead of two; same
    44 px touch target preserved on each.

  All three changes are pure CSS under `@media (max-width: 600px)`.
  Verified via Playwright at 390×844 — header is now ~50 px shorter
  (no worker search bar), focus toggle compact, action row holds
  5 buttons in one line where the old grid held 2.

### Features

### Changes

## [2026.5.21] - 2026-05-21

### Features

- **Playbook detail modal — body, trigger, provenance, actions, events all
  in one place.** Previous version showed events-only, which left
  operators with empty modals on every candidate (uses=0 → no events).
  Operator couldn't see what a playbook actually CONTAINS before
  deciding whether to promote.

  Modal now renders:
  - Title + status badge + scope / uses / winrate / version / last-used
  - **Promote to Active** (candidates) + **Retire** (anything non-retired)
    buttons inside the modal — no need to dismiss + find the row
  - **Trigger** — what conditions tell a worker this playbook applies
  - **Body** — the actual playbook content in a monospace `<pre>` block,
    scrollable at max-height 320px; this is what a worker would see
    if the playbook got injected at task dispatch
  - **Provenance** — task chips that link to the linked-task editor
    (uses the cleanup-batch's `openLinkedTask` flow)
  - **Source worker + timestamps** for context
  - **Events** timeline (was previously the only content) below; if
    empty, shows "(none yet — playbook hasn't been applied to a task)"
    so operators understand why it's blank instead of assuming the
    modal is broken

  Modal sized up from `modal-md` (550px) to `modal-lg` (650px) for the
  longer body content. Title de-duplicated when `pb.title` matches the
  slug. `GET /api/playbooks/{name}/events` enriched to return the
  playbook itself alongside the events array; clients get everything
  in one fetch.

- **Bulk select on the playbook list.** Operator follow-up: 23
  candidates one-at-a-time was painful. New **Select…** button in the
  filter bar flips bulk mode on; each row gets a checkbox; a bulk
  action bar appears showing the selected count plus **Promote
  selected** / **Retire selected** / **Cancel**. Promote-selected
  parallel-POSTs to `/api/playbooks/{name}/promote` for every checked
  row; retire-selected prompts once for a reason and applies it across
  the batch. Summary toast names success + failure counts.

### Changes

### Fixes

## [2026.5.20.15] - 2026-05-20

### Fixes

- **Playbooks tab: one-row-per-card layout.** Follow-up to `.14`'s
  compact analytics — the cards themselves still rendered 3 rows
  deep each (title row + trigger row + Promote/Retire button row),
  so 23 candidate playbooks looked like a wall of identical-looking
  buttons. Restructured the card to a single row: status icon +
  title (ellipsis-truncated, full text on hover) on the left;
  status badge + scope + win/uses/prov + Promote/Retire all
  right-aligned. Trigger snippet moved to the events-timeline
  modal (open by clicking the title). Roughly 2× the density —
  3 playbooks visible in the space that used to hold 1.5. Below
  900 px the row wraps to two rows (title above, meta + actions
  below) so phone widths stay readable. Dropped the per-mover
  panel scrollbar from `.14` since the top-3 entries always fit
  without it.

## [2026.5.20.14] - 2026-05-20

### Changes

- **Per-tab action buttons in the bottom panel header.** The
  tab-header utility buttons (Preview Jira / Sync Jira / + New
  Task) used to show on every tab regardless of which one was
  active, and "+ New Pipeline" lived in a separate row below the
  tab nav — visual inconsistency the operator flagged with a
  screenshot. P-fix: every button in `.tab-header-utils` now
  declares `data-show-on-tab="<tab>"`; `switchTab` toggles inline
  display so only the relevant actions for the current tab
  appear. "+ New Pipeline" moved up into the same row as
  "+ New Task" (Pipelines tab); the in-content `filter-bar`
  wrapper around it is gone. Tasks tab → Preview Jira + Sync
  Jira + New Task. Pipelines tab → New Pipeline. Playbooks /
  Decisions / Activity → nothing (no creation action; playbooks
  are auto-synthesized, decisions are inbound, activity is read-only).

### Fixes

- **Playbooks tab layout compacted.** Operator screenshot showed
  the P4a analytics summary band consuming ~60% of the visible
  bottom panel — stat tiles + 3-column movers each at 240+ px
  pushed the filter chips and the actual playbook cards below
  the fold. Tightened: `.pb-analytics` now uses a flex row that
  floats movers next to the stat tiles instead of stacking;
  stat tiles dropped from 70 px → 56 px min-width with smaller
  font; mover lists clamp at 5.5 em with internal scroll instead
  of growing unbounded; mover names ellipsis on overflow.
  Result: filter chips + the first few playbook cards visible
  on a typical bottom-panel height; no information lost, just
  packed.

## [2026.5.20.13] - 2026-05-20

### Features

- **Spec: No-AI-Slop content system v1.** New
  `docs/specs/content-system-v1.md` captures the 4-round, 16-decision
  interview for the content orchestration system the
  `project_no_ai_slop_content_system` memory anticipated. Single-creator
  with a `creator_id` hedge for future multi-tenancy. New
  `content_ideas` + `content_pieces` tables (v12 schema). Eight-stage
  enum (idea → planned → scripted → filming → edited → staged →
  published → analyzed). Six target platforms (YouTube as anchor +
  X / Instagram / TikTok / Pinterest / Facebook). API where available,
  browser v2 as fallback. Source idea → platform-specific children
  for repurposing. OneDrive integration via the existing Microsoft
  Graph OAuth. Voice corpus warms over time (no day-1 corpus). Idea
  capture nightly @ 2am from YouTube competitor scrape + new
  `swarm_capture_idea` MCP tool + email forwarding to an `ideas@`
  address. Weekly planning Queen brief Sunday @ 9am ingests captured
  ideas + analytics snapshots. Analytics daily @ 6am scrape feeds
  back into next week's planning. Dashboard "Content" tab (Ideas /
  Pieces / Analytics sub-views) + Queen escalations for every HITL
  gate. Ships as **4 phases** across ~12-15 weeks (A: idea capture
  ~2w, B: planning + scripting ~2-3w, C: filming/editing/posting
  ~6-8w, D: analytics feedback ~2w). v1 explicitly accepts the
  months-scale commitment. Force-added past `docs/specs/` gitignore.

### Changes

### Fixes

## [2026.5.20.12] - 2026-05-20

### Features

- **Spec: managed browser capability v1.** New
  `docs/specs/managed-browser-v1.md` capturing the 4-round interview
  decisions for the upcoming `swarm_browse` MCP tool. Scope covers
  Playwright Python in-process, named persistent profiles + ephemeral
  default, `swarm browser login <profile>` headed CLI for setup, five
  v1 actions (`navigate` / `screenshot` / `extract_links` / `fill_form`
  / `click`), per-call timeout, per-profile domain allowlist,
  confirm-before-submit Queen escalation on sensitive forms, audit
  log per call. Spec only — implementation downstream. Force-added
  past the `docs/specs/` gitignore, matching the pattern used for the
  P3 + post-overhaul-cleanup specs.

### Changes

### Fixes

## [2026.5.20.11] - 2026-05-20

### Fixes

- **Mobile visual fixes from the QA findings doc — P1 through P6.**
  Pure CSS, no JS / HTML / backend changes. Before/after screenshots
  via the QA harness confirm every item:
  - **P1 (BLOCKER) — Queen card compacted on phone.** Was ~140px tall
    eating 40% of mobile viewport; now ~50px with `padding: 0.4rem
    0.55rem`, 24×24 bee icon (down from 32×32), and `queen-name`
    forced to single-line ellipsis. Worker list now starts within
    a thumb's reach of the top instead of after a half-screen scroll.
  - **P2 (HIGH) — Status strip label/value separation.** Was
    "queue0/0 last hr14 today56 5h0%" (labels colliding with
    values); now "queue **0/0**  last hr **9**  today **59**" with
    a proper `0.35em` gap inside each `.cc-qs-item`. Reads at a glance.
  - **P3 (HIGH) — Digest strip horizontal scroll.** Was truncating
    "completed: Ext…" with no affordance; now `overflow-x: auto` on
    mobile so the operator can scroll the whole digest if it spills.
  - **P4 (MEDIUM) — Hide BUZ/RES/SLE pills in header on mobile.**
    Was wrapping to 3 vertical lines (60+ px header height); now
    hidden under 768px. The same counts are available via the
    worker-state filter chips directly below.
  - **P5 (MEDIUM) — Hide "operator command center" subtitle on
    phone.** Was wasting 3 lines inside the Queen card; now
    `display: none` on `.queen-meta` under 600px.
  - **P6 (LOW) — Attention empty-state word-wrap.** Was truncating
    mid-word ("the swarm i…"); now `white-space: normal` +
    `word-break: normal` on `.cc-empty` under 600px renders the full
    "Nothing needs you — the swarm is running clean" cleanly.
  Re-run of `scripts/mobile_qa.py` after the fix confirms zero
  pageerrors, all six visual issues resolved at 390px. Spec /
  findings at `docs/qa-mobile-findings-2026-05-20.md`.

## [2026.5.20.10] - 2026-05-20

### Features

- **Mobile QA Playwright harness + first run's findings.** New
  `scripts/mobile_qa.py` drives Chromium at iPhone-14 viewport
  (390×844) through nine touch points across the dashboard — Command
  Center default, Attention/Queen focus toggle, each bottom-panel
  tab (Tasks / Decisions / Pipelines / Playbooks / Activity),
  config General + Automation — and captures full-page screenshots
  plus a `FINDINGS.md` scaffold listing any console errors or
  pageerrors the page produced. Auth uses a session cookie minted
  from the API password (gitignored `.env`). One-shot QA tool, not
  a test suite — re-run with `uv run python scripts/mobile_qa.py`
  after fixes to compare. Screenshots land in
  `docs/qa-mobile-<timestamp>/` (gitignored — large PNG binaries,
  easy to regenerate); the curated findings doc is at
  `docs/qa-mobile-findings-2026-05-20.md`.

### Changes

### Fixes

- **Two pre-existing JS reference errors uncovered by the QA harness.**
  Both fired on every page load before; only surfaced now because
  Playwright's pageerror listener doesn't swallow them like the
  dashboard's defaults did.
  - `queenCooldownTimer` was referenced in the unload cleanup at
    `dashboard.js:9383` but never declared at module scope. Added
    the missing `var queenCooldownTimer = null` next to the other
    timer declarations in IIFE 1.
  - `updateQueenHealthIndicator` is defined in IIFE 2 (line ~10465)
    but called as a bare reference from the WS event dispatcher
    in IIFE 1 (line 624). The two IIFEs are separate scopes —
    fixed by exposing the function on `window` from IIFE 2 and
    guarding the call site with `typeof window.X === 'function'`
    so a future scope shuffle can't silently break it again.
  Both fixes verified by re-running the QA harness: the
  `pageerror` listener captured zero errors on the second run.

## [2026.5.20.9] - 2026-05-20

### Features

- **Cleanup batch — follow-up to the P1–P6 UX overhaul series.** Closes
  the four gaps named in earlier commits' "deferred" sections, plus
  the unrelated test_ws_auth flake that bit three full-suite runs.
  Spec at `docs/specs/post-overhaul-cleanup.md`. New tests + lint
  clean; full suite 4421 passing (up from 4406).
  - **Linked-task-by-ID.** New `GET /api/tasks/{task_id}` returning
    the rich task dict (every field the editor reads, not just the
    7-field list-view summary). New `showTaskEditorById(id)` helper
    in `dashboard.js` fetches that endpoint and feeds the existing
    `openTaskModal('edit', data)`. P3's pipeline-step task chip now
    actually opens the editor instead of falling back to
    scroll-and-flash. The scroll-and-flash code stays as a defensive
    fallback if the fetch 404s.
  - **PlaybookConfig range validation.** New `_validate_playbook_ranges`
    mirroring `_validate_drone_ranges`. Rejects winrate / similarity
    values outside `[0.0, 1.0]`, `auto_promote_uses` / `prune_min_uses`
    below 1, negative `min_resolution_chars` / `max_synth_per_hour`,
    and `consolidation_interval_seconds` below the engine's 300s
    floor. Dashboard sliders prevent the common case but the REST
    endpoint is publicly addressable so this is the only gate
    against a direct bad POST. Errors raise `ValueError` → 400 with
    explicit `playbooks.X must be …` messages.
  - **Retry-on-COMPLETED with confirmation modal.** Operator who
    really needs to re-run a COMPLETED step can now do so —
    `engine.retry_step` gains a `confirmed=False` kwarg; without
    it the engine still rejects non-FAILED (back-compat preserved).
    Route accepts `{"confirmed": true}` body and threads it through.
    A new modal in the P3 detail view gates the action behind a
    required checkbox + explicit warning about side effects
    (shell commands re-execute, webhooks re-fire, agent tasks
    re-create). FAILED retry still skips the modal. Cascade
    behaviour is unchanged: only FAILED downstream descendants
    reset, even on a confirmed COMPLETED retry — re-firing a
    whole completed subtree is a separate decision we deliberately
    deferred. The detail view's per-step button is `⚠ Retry`
    (amber) for COMPLETED to visually distinguish from FAILED retry.
  - **test_ws_auth flake fixed.** The 30s pytest-timeout was
    catching `selector.poll` during pytest-asyncio's event-loop
    teardown — but the timed body did nothing async itself. Root
    cause: imports were inside each test function (`from
    swarm.server.api import _RATE_LIMIT_WINDOW`), so the first
    test in the file paid the full `swarm.server.api` import cost
    while the timeout was already counting. Hoisting the imports
    to module level moves the work into collection. Was a
    pre-existing flake that hit three earlier full-suite runs.

### Changes

### Fixes

## [2026.5.20.8] - 2026-05-20

### Features

- **Playbook config tuning UI — P4b, the deferred half of P4.** Wires
  `PlaybookConfig` through all six layers of the config-save-chain so
  operators can edit the synthesis loop's tuning knobs from the
  dashboard instead of hand-editing `swarm.yaml`. Adds a new
  Playbooks pane to the Automation tab with three sections —
  Synthesis (enabled / eligible task types / min resolution chars /
  hourly cap), Promotion + Pruning (auto-promote uses + winrate
  slider, prune min uses + winrate slider), and Consolidation
  (interval seconds + dedupe similarity slider + Skills install
  toggle). Winrate / similarity fields render as 0–100% sliders
  backed by hidden float inputs so `buildPayload` reads clean
  0.0–1.0 values without re-doing the math.

  Per the config-save-chain audit (`docs/audits/config-save-chain-2026-05-04.md`):
  L1 dataclass (already existed); L2 form added to `config.html`;
  L3 dispatcher — `"playbooks"` added to `_KNOWN_BODY_KEYS` + a new
  `if "playbooks" in body` branch in `apply_update`; L4 handler —
  new `_apply_playbooks()` method routing through the generic
  `_apply_dataclass_dict` dispatcher so unknown keys land in the
  structured FieldOutcome (no silent drops); L5 persistence —
  `"playbooks"` added to `_JSON_KEYS` + new `_serialize_playbooks()`
  in `serialization.py`; L6 load — `"playbooks"` added to the
  `_DATACLASS_BLOBS` map so the generic `_parse_json_dataclass`
  loader picks it up automatically. `PlaybookConfig` is now
  re-exported from `swarm.config` for consistency with the other
  top-level dataclasses.

  3 new tests: round-trip through save/load (all 11 fields verified),
  `apply_update` happy path, and an unknown-key check that asserts
  bogus body fields surface in the FieldOutcome's `unknown` list
  rather than getting silently swallowed — that was exactly the
  failure mode the audit's silent-drop bug class produced before
  Phase 7 of #328 added the structured outcome shape.

### Changes

### Fixes

## [2026.5.20.7] - 2026-05-20

### Features

- **Mobile global polish — P6 of the editor UX series.** Wraps up the
  cross-cutting mobile pass that the audit punch-list flagged. All
  interactive elements inside `@media (pointer: coarse)` are now at
  the 44px iOS/Material tap-target minimum — `.btn`, `.tab-btn`,
  `.worker-item`, `.queen-banner-actions .btn`, `.mobile-overflow-btn`
  (the header hamburger), and `.resize-handle` (24px on touch — the
  prior 12px was unreliable). `.filter-chip` jumps from 32px to 40px
  with more padding so the chip rows are actually tappable. Under
  600px, the task editor's primary-metadata row (priority / type /
  status / worker / tags) wraps with each field at 100% width instead
  of fighting for space in the flex line, and the first filter-chip
  in every filter bar gets `position: sticky; left: 0` so the "All"
  reset stays grabbable while the row horizontal-scrolls. Worker
  names in the sidebar pill drop the 140px max-width truncation at
  ≤768px — names wrap to a second line rather than ellipsis-clipping
  on a phone where there's room for the full string. The Activity
  (Buzz Log) filter chips were previously hidden entirely on mobile;
  P6 brings them back as a `<select>` paired with the chips, kept in
  sync by `switchBuzzFilter` and a change listener — single-category
  filtering on phone, multi-chip on desktop.

### Changes

### Fixes

## [2026.5.20.6] - 2026-05-20

### Features

- **Mobile Queen dashboard rescue — P5 of the editor UX series.** The
  Command Center stacked at 900px and then never adapted further; on a
  phone (~390px) it was effectively unusable — the Queen action row
  crushed eight buttons into one cramped line, the status strip
  shrunk to 0.65rem to fit, and the Attention card body clipped
  worker messages at 4em. P5 adds a `<600px` breakpoint that:
  switches the layout into a one-panel-at-a-time mode controlled by a
  new tab strip above the grid (Attention / Queen) that lets the
  operator pick which surface gets the full screen height — Attention
  defaults if there's pending work, Queen otherwise, and the choice
  persists in localStorage so a re-render doesn't reset it; Queen
  action buttons render as a 2-column grid with 44px tap targets;
  status strip wraps to multiple lines at 0.75rem instead of
  shrinking to unreadable; Attention card body / detail lose the 4em
  max-height so escalation messages render in full; Queen terminal
  holds a `min-height: 280px` when focused so the PTY isn't tiny. The
  Attention focus button mirrors the pending-attention count in its
  label so the operator sees what's waiting before flipping panels.

### Changes

### Fixes

## [2026.5.20.5] - 2026-05-20

### Features

- **Playbooks analytics — P4a of the editor UX series.** The Playbooks
  tab gains a summary band at the top showing per-status totals
  (active / candidate / retired) and a rolling 24-hour event window
  (applied / wins / losses), plus a movers panel: top 5 by uses, top 5
  by winrate (gated on `uses >= 3` so a single lucky win can't dominate
  a 50-and-10 active), and a per-scope breakdown (global / project / worker
  with totals and derived winrate). The flat list below gains status
  chips (All / Active / Candidate / Retired) and a scope dropdown that
  filters client-side from a single fetch. Clicking a playbook title
  (or a row in the movers panel) opens a new event-timeline modal
  rendering the `playbook_events` rows newest-first, color-coded by
  event type (synthesized / applied / win / loss / promoted / retired /
  consolidated) with the task ID / worker / detail on each row. Two new
  PlaybookStore methods (`get_events_for_playbook` + `get_analytics`)
  power two new endpoints (`GET /api/playbooks/{name}/events`,
  `GET /api/playbooks/analytics?since_hours=N`). Pure aggregation — no
  schema changes, rides the existing `(playbook_id, ts)` index on
  `playbook_events`. Winrate is `-1.0` when no outcomes have been
  attributed yet so the UI can render "—" instead of misleading "0%".
  Config tuning UI for `PlaybookConfig` was split off as P4b — the
  config-save-chain wiring is risky enough (silent-drop bug class lives
  there) to deserve its own pass rather than getting bundled.

### Changes

### Fixes

## [2026.5.20.4] - 2026-05-20

### Features

- **Pipeline detail view + retry — P3 of the editor UX series.** Adds a
  read-only inspect modal that opens when an operator clicks anywhere on
  a pipeline card. The step list is grouped by execution wave (Kahn-style
  levelization client-side; same DAG the engine's `advance()` walks),
  each step shown with status / duration / linked task chip / error +
  pretty-printed result. For `shell_command` results, stdout / stderr /
  returncode are surfaced as labeled blocks above the raw JSON. A Copy
  button is on every result block. New `POST /api/pipelines/{id}/steps/
  {step_id}/retry` endpoint resets a FAILED step plus its FAILED
  downstream descendants (BFS forward through the DAG); SKIPPED and
  COMPLETED downstream are left alone — SKIPPED is sticky operator
  intent and re-running a COMPLETED side-effecting step would
  double-fire it. The retry resets `status`, `started_at`,
  `completed_at`, `error`, `result`, and `task_id` so the engine
  re-creates fresh tasks for agent steps. 404 for unknown pipeline/step,
  409 for non-FAILED targets. The detail view subscribes to the existing
  `pipelines_changed` WS event for live re-render — steps tick through
  pending→ready→in_progress→completed without refreshing the page.
  Detail modal has its own Edit button (only when status ∈
  {DRAFT, PAUSED}, matching the engine guard) that warps into the
  P1 editor pre-filled. Pipeline metadata header shows timezone /
  schedule / tags / template_name / created. Linked-task chips switch
  to the Tasks tab + scroll-and-flash the row (the existing task editor
  isn't ID-addressable so we don't open it directly — flagged as
  deferred in the spec). Spec lives at
  `docs/specs/pipeline-detail-view.md`. 11 new tests (7 engine, 4 route)
  covering the cascade-reset semantics, SKIPPED/COMPLETED preservation,
  and status-code mapping. P4 in the series adds the playbook
  analytics + config tuning surface.

### Changes

### Fixes

## [2026.5.20.3] - 2026-05-20

### Features

- **Pipeline schedule builder + per-pipeline timezone — P2 of the editor
  UX overhaul.** Replaces the free-form `HH:MM`/cron text input with a
  preset picker (On-demand / Daily / Weekly / Weekdays / Hourly / Custom
  cron) that emits the same cron string the engine reads, plus a live
  preview wired to a new `POST /api/pipelines/schedule/preview` endpoint
  — "Weekdays at 14:30" and the next five fire timestamps update as the
  operator edits. Per-step `schedule` inputs gain the same inline
  preview without the full builder so quick edits stay quick. Added a
  curated 30-zone IANA timezone select to the Basics section (custom
  values typed previously are preserved as a sticky option so saves
  don't drop them). `Pipeline.timezone` is a new optional string field
  — empty preserves legacy server-local evaluation; populated routes
  through `zoneinfo.ZoneInfo` so cron expressions fire in the
  operator's frame regardless of where the daemon happens to run.
  Timezone is the only field freely editable while a pipeline is
  RUNNING (steps still need DRAFT/PAUSED); fixing a misconfigured
  zone shouldn't require pausing the work. New
  `swarm.pipelines.schedule` module holds the normalize / humanize /
  preview helpers and is pure-stdlib + croniter so the same code runs
  the engine match path and the editor preview. Edit mode reverse-
  engineers a saved cron back into the matching preset for visual
  consistency; un-presettable expressions land in the Custom cron pane.
  Persistence rides the existing JSON-blob column on the `pipelines`
  table — no schema migration needed since `pipeline_from_dict`
  tolerates the absent field on old rows. P3 in the series adds the
  detail view + DAG visualization.

### Changes

### Fixes

## [2026.5.20.2] - 2026-05-20

### Features

- **Pipeline editor — P1 of the multi-phase UX overhaul.** Replaces the
  single-modal create flow that couldn't reach automated steps with a
  sectioned editor (Basics / Steps / Schedule). Each step gets a card
  layout; conditional fields appear by step type — Agent shows worker +
  task_type dropdowns plus description; Human shows description; Automated
  finally surfaces a Service dropdown (populated from the new
  `GET /api/pipelines/services` endpoint) and a JSON config field with a
  "Use example" button that pre-fills the registered handler's
  `example_config`. Dependencies became a chip picker over already-defined
  steps; cycles and duplicate IDs are rejected client-side before submit.
  Step rows on the list now surface `error` and `result` text (previously
  hidden in the model), and pipelines in DRAFT/PAUSED show an **Edit**
  button that re-opens the same modal pre-filled and submits via
  `PUT /api/pipelines/{id}` — `PipelineEngine.update()` was extended to
  accept a `steps=` list under the DRAFT/PAUSED guardrail, raising
  `ValueError` that the route handler maps to 409 once a pipeline is
  RUNNING. Built-in handlers (`shell_command`, `webhook_notify`,
  `headless_claude`, `file_uploader`, `youtube_scraper`,
  `claude_code_security`) now advertise `description` + `example_config`
  attrs that feed the dropdown via `ServiceRegistry.describe()`. P2 in the
  series replaces the still-text-only schedule input with a cron builder.

### Changes

### Fixes

## [2026.5.20] - 2026-05-20

### Features

### Changes

### Fixes

- **Task editor accepts a literal `?` again.** The global `?`
  keyboard-shortcut handler bailed on `INPUT`/`TEXTAREA`/`SELECT` but
  not on `contenteditable`, and the task editor's description field is
  a contenteditable div — so typing `?` opened the shortcuts modal and
  swallowed the keystroke. Added an `isContentEditable` guard.

## [2026.5.19.4] - 2026-05-19

### Features

- **Operator-blocked-stall guard — a task waiting on the operator no
  longer churns ACTIVE forever.** Incident: #443 sat `active` while the
  worker stood by for an operator hand-back; over 12h that drew ~259
  drone CONTINUED + ~63 oversight interventions + ~46 completion
  proposals (each a headless Queen call), zero progress. Now the
  oversight monitor tracks a per-(worker, task) no-progress streak
  (`task.updated_at` frozen while ACTIVE across N drift-cadence checks —
  deterministic and Queen-free so it survives a rate-limit storm); at
  `auto_park_no_progress_checks` (default 3, ~30 min) it raises **one**
  `ProposalType.PARK` proposal. Approve → `TaskBoard.block_for_operator`
  parks it to the existing #405 `BLOCKED` hold (idle-watcher, completion
  loop and reconciler already skip BLOCKED, so every churn loop stands
  down with no new guards); the worker resumes on the normal operator
  re-dispatch (`activate` → BLOCKED→ACTIVE, `block_reason` cleared).
  Dismiss → `auto_park_reject_backoff_seconds` (default 2h) before it
  can re-propose. Pending-park dedupe (`has_pending_park`) freezes the
  oversight/completion churn while the proposal awaits the operator. The
  proposal surfaces as a normal Approve/Dismiss decision card via the
  existing exception-queue path — deliberately *not* an extra
  modal/push (single-source-of-truth, no new interruptive notification).
  New `queen.oversight` knobs: `auto_park_enabled`,
  `auto_park_no_progress_checks`, `auto_park_reject_backoff_seconds`.

### Changes

### Fixes

## [2026.5.19.3] - 2026-05-19

### Features

### Changes

### Fixes

- **WAITING workers poll at the base cadence (fast resume detection).**
  `compute_backoff` applied the same idle exponential backoff to WAITING
  as to truly-idle RESTING workers, so a WAITING worker was polled as
  rarely as every `max_idle_interval` (30s default) and memory pressure
  doubled it again — WAITING→BUZZING took 30–60s to show after the
  worker actually resumed. A WAITING worker is the one *most* likely to
  resume imminently (it was just answered/unblocked), so it now polls at
  the flat base interval (`poll_interval_waiting or base`), exempt from
  both the idle-streak multiplier and the memory-pressure doubling.
  Focus still only speeds it up further; RESTING/BUZZING backoff
  unchanged. Resume is now observed in ~base seconds (5s default).

## [2026.5.19.2] - 2026-05-19

### Features

### Changes

### Fixes

- **Toast notifications can no longer wall the screen.** Toasts are
  one-line glances, but `showToast` had no text cap, no stack limit and
  no height clamp, and `WORKER_STUNG` broadcast its full 30-line
  terminal tail straight into a toast — a burst produced an unreadable
  paragraph wall while the Attention panel stayed empty (those events
  are Queen-handled / live in the Activity tab). Now: `toast.js`
  collapses whitespace and hard-caps to one ellipsised line
  (`TOAST_MAX_CHARS`), and keeps only the newest `TOAST_MAX_STACK` so a
  flurry can't fill the viewport; `.toast` CSS clamps height as
  defense-in-depth; `StatePublisher.on_drone_entry` broadcasts a terse
  first-line summary (`_terse_detail`) for the `system_log` WS event and
  the push notification while the **full** multi-line detail still lands
  in the buzz log for the Activity tab (no diagnostics lost). Per the
  operator decision, Queen-handled events stay as terse FYI toasts
  rather than being suppressed.

## [2026.5.19] - 2026-05-19

### Features

- **Answer a waiting worker's choice prompt from the Attention card.**
  When a `worker-waiting` exception is a numbered Claude choice menu,
  the card now renders the worker's *own* options as buttons; clicking
  one sends that selection straight to the worker's PTY (same path the
  Queen 1/2 strip uses) instead of forcing "Open terminal" + typing.
  `attention_model.extract_choice_options()` parses the captured WAITING
  tail with the same cursor/plain-option shape the Claude provider uses
  for detection (requires a focused `>`/`❯` option **and** another
  numbered line, so prose with stray "1." doesn't sprout fake buttons);
  options ride on `ExceptionItem.options`; the generic Open terminal /
  Force rest verbs stay as the fallback. Pure + unit-tested; no options
  parsed ⇒ unchanged behaviour.

### Changes

### Fixes

## [2026.5.18.3] - 2026-05-18

### Features

### Changes

### Fixes

- **Actionable cross-worker handoffs no longer fall through both drone
  nets (task #442).** A handoff carried only by a `dependency`/`warning`
  message to a recipient who is idle *and* task-less was silently lost:
  the IdleWatcher skipped it (no task to carry) and a one-shot
  InterWorkerMessageWatcher nudge dies on a missed turn or a daemon
  restart, leaving the published work unconsumed with nothing tracking
  it (the public-website #985 → realtruth incident; #441 was the manual
  backfill). The watcher now spawns a **tracked task** assigned to the
  recipient (`daemon._spawn_handoff_task` → `assign_and_start_task`), so
  the IdleWatcher durably carries it to completion. Scoped to
  action-bearing types only (informational `status`/`finding` still just
  nudges — no board flooding), idempotent per message id, logged as
  `AUTO_HANDOFF_TASK`, and a no-op when the spawn callback is unwired
  (graceful fallback to the prior nudge-only behaviour).

## [2026.5.18.2] - 2026-05-18

### Features

- **Attention panel → exception queue.** The dashboard Attention panel
  was the operator's old coordinator feed (every worker→Queen message,
  every worker idle >15s, recency-sorted, bare Reply/Dismiss). Now that
  the Queen coordinates the swarm, that feed is mostly already-handled
  noise. It is rebuilt as an exception queue that surfaces only what is
  genuinely escalated to a human or a hard failure the autonomous layers
  can't resolve.
  - New pure classifier `swarm.server.attention_model.classify()` —
    snapshot-in, `{critical, decision, handled}`-out, no I/O, fully
    unit-tested. `routes/attention.py` just gathers live snapshots
    (threads, pending proposals, worker state, buzz log, blockers,
    resource pressure) and delegates.
  - **Suppression filter:** worker-messages (Queen owns them via #235
    auto-relay), nudged/blocked waiting workers, reviving crashes, and
    proposals inside the autonomous-approval window drop into a
    collapsed "the swarm is handling" drawer instead of the queue.
  - **Severity model:** `Critical` / `Needs your decision` sections,
    oldest-first within each, plus age-escalation — a decision
    unresolved past 30m auto-promotes to Critical with a `STALE` marker
    (fixes "a stale proposal looks like a fresh crash").
  - **Action-first cards:** each carries a "what's been tried / why
    it's yours" detail line and type-correct verbs — proposals get
    inline Approve/Dismiss (reusing existing endpoints), crashes get
    Revive, waiting workers get Open terminal / Force rest.
  - **Layout:** the queue fills the top; the "swarm is handling" region
    is pinned to the bottom third with its own scroll and a sticky
    collapse toggle.

### Changes

### Fixes

- **Attention no longer claims the Queen is working on an idle thread.**
  A `worker-message` thread stays `active` until something explicitly
  resolves it, which the Queen rarely does — she just moves on. The
  classifier now keeps a worker-message in the drawer only if it is
  fresh (touched < 10m) **or** the Queen is actively BUZZING; a stale
  thread with an idle Queen is dropped entirely. Honest reasons
  ("with the Queen now" / "relayed — awaiting her next turn"), never a
  false "handling".
- **Interruptive notifications aligned to the exception queue (single
  source of truth).** Browser/OS notifications fired on event creation
  while the panel surfaces on escalation-to-a-human-decision, so a
  worker hitting a choice menu pinged the operator with an empty
  Attention panel. `escalation` and `proposal_created` are downgraded
  to FYI toasts; `escalation_handler.on_escalation` no longer emits a
  premature desktop notification (the Queen handles it; if she can't it
  returns as a `queen_escalation` proposal with its own banner +
  decision card); `proposals._notify_proposal` notifies only for
  ESCALATION proposals (assignment/completion sit silently in the
  autonomous window). The classifier-derived `maybeNotifyAttention`
  remains the one path that pings when something actually needs you.

## [2026.5.18] - 2026-05-18

### Features

- **Native `/goal` seeding from task acceptance criteria (v1).** When a
  task with `acceptance_criteria` is dispatched to a worker whose CLI
  has a native session-scoped `/goal` (Claude Code v2.1.139+, Codex),
  Swarm injects `/goal <condition>` after the task message. The
  provider's own small-fast-model evaluator then runs the keep-working
  loop — Swarm builds **no** evaluator, subprocess, or metered API call.
  This is the *proactive* complement to the existing *reactive*
  post-completion verifier (which stays as the backstop): it reduces
  premature `swarm_complete_task` calls rather than reopening after the
  fact. Inspired by Claude Code's native `/goal` (the "separate the
  agent that works from the one that decides it's done" pattern).
  - `LLMProvider.supports_native_goal` capability — `True` for Claude
    and Codex; Gemini/OpenCode/generic inherit `False` → a clean no-op
    there (the generic idle-watcher remains the only safety net;
    provider-neutral by capability detection, not assumption).
  - `render_goal_condition()` turns criteria into a one-line condition
    with a proof directive (the evaluator only judges the transcript,
    not files) and the docs-recommended `or stop after N turns` runaway
    bound; ≤ 4000 chars.
  - `DroneConfig.native_goal_enabled` (default on, operator-reversible)
    and `native_goal_max_turns` (default 25).
  - Seeded only from `start_task` (the dispatch boundary) so it is
    set-once-per-dispatch — idle-watcher nudges never re-arm it.
    Best-effort: a `/goal` send failure cannot unwind a started task.
    Logged as `SystemAction.GOAL_SET`.
  - Coordinator/orchestrator-level `/goal` (Queen / project-root holding
    a macro objective) is deliberately **out of scope for v1** — filed
    as a separate `/interview`-driven initiative.

## [2026.5.17.9] - 2026-05-17

### Fixes

- **Attention queue cards now word-wrap their titles instead of
  truncating.** `.cc-attention-card-title` forced single-line
  truncation (`overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap`), so a multi-line escalation title showed as
  `swarm: Status correction (oper…`. Now `white-space:normal;
  overflow-wrap:anywhere; word-break:break-word` — the full title wraps
  to as many lines as it needs (long unbroken tokens like a path/URL
  wrap too). `.cc-attention-card-meta` gained `white-space:nowrap;
  flex:0 0 auto` so the `worker · age` meta stays pinned on one line as
  the title grows downward (the card head is a baseline-aligned flex
  row). CSS-only, `base.html`.

## [2026.5.17.8] - 2026-05-17

### Fixes

- **`swarm_park_task` no longer silently parks the wrong task** (#407;
  follow-up to #406, off the 2026-05-17 public-website incident). #406
  shipped with no task argument — it parked "the" active task via
  `current_task_for_worker()`. When a worker legitimately owns >1 ACTIVE
  task (legal pre-#405-reload / un-reconciled board state), that
  iterated `_tasks` and set down an arbitrary one: public-website owned
  #393/#394/#398/#399, intended #399, the tool parked the
  genuinely-blocked #393 instead. A state-mutating worker tool that
  silently targets the wrong task corrupts board truth and can
  de-silence a correctly-blocked task's idle-watcher — the exact skew
  #405 was meant to end.
  - `swarm_park_task` now accepts an explicit `task_number`; parks
    exactly that task (rejected, no mutation, if not owned by the caller
    or not ACTIVE).
  - Omitted + caller owns exactly one ACTIVE task → parks it
    (back-compat with the common #406 case).
  - Omitted + caller owns >1 ACTIVE task → **REFUSES**, lists the
    candidate numbers, mutates nothing — never a silent guess.
  - New `TaskBoard.parkable_tasks_for_worker()` accessor (keeps the
    `TaskStatus` enum in the board layer; mirrors the existing
    `current_task_for_worker` / `active_tasks_for_worker` family).
  - Regression: explicit-id-among-several, omitted+multiple refusal,
    omitted+single back-compat, not-owned / not-active / invalid-arg
    rejections, and a faithful public-website-incident-shape test.

## [2026.5.17.7] - 2026-05-17

### Changes

- **Command Center: retired the "Now" (live activity) panel.** The
  per-worker live-activity feed and its row-resize handle added no
  signal over the worker tiles + Attention queue, so it's removed
  entirely — JS cluster (`loadLive`/`renderLive*`/poll loop/row-resize
  geometry/`CC_LIVE_*` storage keys) and the `cc-live-panel` markup +
  `.cc-live*`/`.cc-row-resize` CSS. `ccFocusLive` is kept (still used by
  the Attention card). The CC grid collapses to `auto 1fr` and the
  column-resize handle now sits on the single content row.
- **Command Center: Queen on the left, Attention on the right.** The
  Queen's live terminal is now the primary left pane and the Attention
  queue moved to the right of it (was reversed). The column-resize
  handle and stored `--cc-attention-pct` split track the new order.

### Fixes

- **Task/bottom panel now remembers its split position.** The persisted
  `swarm-split` ratio was restored once at page load but wiped on every
  return to the Command Center: `show()` cleared
  `gridTemplateRows` with no re-apply (every other panel survives via
  `applyCcLayoutFromStorage`; the bottom split had no equivalent).
  Extracted `applySavedSplit()` and `show()` now calls it — clears the
  stale per-visit inline state first (preserving the original intent),
  then re-applies the operator's persisted ratio.
- **Pasting an image into the Queen no longer lands in the last active
  worker.** The embedded Queen terminal is deliberately not
  `activeTermWorker`, but `uploadAndPaste()` hard-coded the
  `inlineTerm`/`inlineTermWs` globals (= last-focused worker), so Queen
  pastes/drops were routed to whatever worker was active. Refactored to
  `uploadAndPaste(file, targetTerm, targetWs)`; the per-terminal
  paste/drop handlers in `createTermEntry` now pass their own
  `term` + `entry.ws`, so an image pasted into the Queen reaches the
  Queen's PTY. The global drop-outside fallback still defaults to the
  active terminal.

## [2026.5.17.6] - 2026-05-17

### Features

- **`swarm_park_task` — workers can hand back their own task** (#406;
  followup flagged during #405). A worker MCP tool that transitions the
  caller's single ACTIVE task back to ASSIGNED with a required reason —
  an intentional set-down, **not** a blocker (no `swarm_report_blocker`
  binding created) and not completion. Closes the gap that bit during
  the #405 Playbooks→urgent preempt: a parked worker couldn't proactively
  un-stick its own task, so the board lied (`active` on an idle worker)
  and misled the Queen into a false STOP.
  - `TaskBoard.park(task_id, worker, reason)` — pure transition;
    rejects unless the task exists, is ACTIVE, and is owned by the
    caller (no cross-worker parking by construction).
  - `_handle_park_task` parks the caller's own active task (found via
    `current_task_for_worker`), reason required, records to task history
    + buzz (`SystemAction.TASK_PARKED`).
  - Composes with #405 INV-1/2/3 **immediately** — the worker has zero
    ACTIVE tasks right after, no daemon reload / reconciler needed; the
    board is truthful at once.
  - Distinct from `swarm_report_blocker` (waiting on upstream) and
    `swarm_complete_task` (done). Tool description satisfies the
    `test_every_tool_description_explains_when` meta-guard.
  10 new tests incl. the preempt scenario + not-a-blocker assertion;
  full suite green; ruff clean.

## [2026.5.17.5] - 2026-05-17

### Features

- **Playbook synthesis loop — Phase 4: operator surface** (spec:
  `docs/specs/playbook-synthesis-loop.md`, now **status: shipped**;
  swarm task #404). Final phase — the loop (synthesize → recall →
  outcome → propagate → consolidate → **operate**) is complete.
  - `src/swarm/server/routes/playbooks.py`: `GET /api/playbooks`
    (all statuses incl. candidates; optional `?status=`/`?scope=`),
    `POST /api/playbooks/{name}/promote`, `POST /.../retire` (body
    `reason`). Same global auth/CSRF middleware as every `/api` route;
    registered via `routes.register_all`.
  - Dashboard **Playbooks** bottom-tab: active-first list with a
    status badge (active / **candidate** / retired visually distinct),
    winrate / uses / provenance / scope / trigger, and operator
    Promote (candidates) / Retire controls wired to the routes.
  - Spec frontmatter flipped `proposed → shipped` (+ `shipped_date`,
    per-phase release map, Phase 4 closeout).
  - **Deferred by decision** (acceptance-criterion option B):
    operator-editability of `PlaybookConfig` via the dashboard /
    `config_store` DB round-trip is *not* implemented — the audited
    config-save chain is sensitive and `PlaybookConfig` already has
    sane `HiveConfig`/`swarm.yaml` defaults. Documented in the spec's
    Phase 4 closeout.
  Route tests in `tests/test_playbook_routes.py`; full suite 4285
  passed; ruff + JS syntax clean. Headless-only / no metered API; v5
  `skills` table / `SkillsStore` untouched.

## [2026.5.17.4] - 2026-05-17

### Features

- **Playbook synthesis loop — Phase 3: propagation + consolidation**
  (spec: `docs/specs/playbook-synthesis-loop.md`; swarm task #403).
  Release record for Phase 3, whose code shipped functionally in
  `d7b8fef` (a deliberate WIP park during the urgent #405 preempt) +
  ruff-normalized in `c107730`; this is the missing CHANGELOG/release
  marker — no new code.
  - **`.claude/skills/` installer** (`playbooks/installer.py`):
    `install_worker_playbooks` renders ACTIVE, in-scope playbooks to
    `pb-<name>/SKILL.md` so a Claude worker discovers them by
    description match. Idempotent with stale-cleanup; wired into
    `daemon._install_worker_artifacts` and **provider-gated** — native
    install for Claude workers only; other providers reach playbooks via
    the provider-neutral `swarm_get_playbooks` MCP tool.
  - **Consolidation sweep** (`playbooks/consolidator.py` +
    `daemon._playbook_consolidation_loop`): a low-frequency
    (`PlaybookConfig.consolidation_interval_seconds`, floored 300s,
    clean-shutdown) sweep that uses `PlaybookStore.find_near_duplicate`
    + the headless Queen (decision shape #8) to merge **same-scope**
    near-duplicate ACTIVE playbooks — `consolidate_into` bumps version,
    unions provenance, recomputes content-hash + FTS, retires the loser.
    Never cross-scope. `SystemAction.PLAYBOOK_CONSOLIDATED`.
  - Fixed a Phase-1 latent bug found here: `find_near_duplicate` used
    `search(limit=1)` so a body-vs-self query + self-exclude always
    returned `None`; now `limit=5`.
  Headless `claude -p` only (no metered API); v5 `skills` table /
  `SkillsStore` untouched. Phase 4 (dashboard + config editability)
  remains queued (#404).

### Changes

### Fixes

## [2026.5.17.3] - 2026-05-17

### Fixes

- **Task-lifecycle invariant bug (#405, operator-trust)** — the board
  was showing multiple in-progress tasks per worker and ACTIVE tasks on
  RESTING workers ("that shouldn't be possible"). Roots: activation-time
  demotion existed but reconciliation was startup-only and INV-1-only;
  nothing demoted an ACTIVE task when its worker went idle; operator-only
  tasks (e.g. GitHub org-admin) could occupy a worker-ACTIVE slot. Fix
  enforces three invariants with a one-shot + ongoing self-healing
  reconciler:
  - **INV-1** ≤1 ACTIVE/worker — `TaskBoard.activate()` demotes a
    worker's other ACTIVE tasks; reconciler collapses any drift.
  - **INV-2** ACTIVE ⇒ worker working or task blocked —
    `daemon._on_state_changed` demotes a worker's ACTIVE task when it
    leaves BUZZING/WAITING (→ ASSIGNED, or → the new **`BLOCKED`**
    status when a `swarm_report_blocker` binding exists).
  - **INV-3** a worker's current task IS its single ACTIVE task —
    `TaskBoard.current_task_for_worker()` (no separate desyncing pointer).
  - **Operator-action tasks**: new `TaskType.OPERATOR` (never ACTIVE;
    `is_operator_action`; non-executable workflow template).
  - **Reconciliation** (`TaskBoard.reconcile_invariants`) runs at daemon
    start and on every worker state transition, repairs INV-1/2/3 +
    operator-action drift deterministically + idempotently, and
    buzz-logs each auto-repair (`SystemAction.TASK_RECONCILED`) so the
    operator can audit what self-healed.
  - **Blocked status added inline** (spec implementer's-call): a
    distinct `TaskStatus.BLOCKED` (+ `block_reason`, schema v11
    migration, persisted) — INV-2 is incoherent without a real target
    state and the blocker binding already exists.
  Enum-ripple completed (STATUS_ICON/STATUS_LABEL, WORKFLOW_TEMPLATES,
  jira `_SWARM_TYPE_TO_JIRA`). New regression suites
  (`test_task_lifecycle_invariants`, `test_task_lifecycle_daemon`);
  full suite 4280 passed; ruff clean. The documented corrupt records
  (public-website/swarm/my-rcg/project-root) self-heal on the next
  daemon reload via the startup reconciler.

  *(Incidental: ruff-format normalization of Playbooks Phase-3 files
  committed earlier in d7b8fef — formatting only, no logic change.)*

## [2026.5.17.2] - 2026-05-17

### Features

- **Playbook synthesis loop — Phase 2: the outcome loop** (spec:
  `docs/specs/playbook-synthesis-loop.md`; swarm task #402; builds on
  Phase 1 / 2026.5.17). Playbooks now learn from real results:
  - **Recall-at-dispatch:** `daemon.start_task()` injects the top
    (`_PLAYBOOK_RECALL_LIMIT`) FTS-relevant **active**, in-scope
    playbooks into the worker's task message and records a
    `playbook_events 'applied'` row per injection (+ bumps `uses`,
    `PLAYBOOK_APPLIED` buzz). Candidates are never injected; gated by
    `PlaybookConfig.enabled`.
  - **Win/loss attribution:** a new decoupled `on_verdict` hook on
    `VerifierDrone` (invoked from `fire_and_forget` with the terminal
    status) wires to `daemon._attribute_playbook_outcome` — `VERIFIED`
    → win, `REOPENED`/`ESCALATED` → loss for every playbook applied to
    that task; `SKIPPED`/`NOT_RUN` → no signal. Off the
    verification-resolution path, not `complete_task` directly.
  - **Auto-promote / prune:** `PlaybookStore.evaluate_lifecycle` flips a
    candidate → active at `auto_promote_uses`/`auto_promote_winrate`,
    and retires at `prune_min_uses`/`prune_max_winrate` (never on a 0.0
    winrate that just means no decided outcomes yet).
    `PLAYBOOK_PROMOTED`/`PLAYBOOK_RETIRED` buzz.
  - New `PlaybookStore` methods (`mark_applied`,
    `playbooks_applied_to_task`, `record_outcome`, `promote`, `retire`,
    `evaluate_lifecycle`) — config-free (thresholds passed in). All
    best-effort: never block dispatch or the verification path.
  Subscription-safe (no metered API); the v5 `skills` table /
  `SkillsStore` remains untouched. Phase 3 (`.claude/skills/`
  propagation, consolidation) and Phase 4 (dashboard) remain out of
  scope (tasks #403/#404).

### Changes

### Fixes

## [2026.5.17] - 2026-05-17

### Features

- **Playbook synthesis loop — Phase 1** (spec:
  `docs/specs/playbook-synthesis-loop.md`). Self-improving procedural
  memory: when a task ships successfully, `daemon.complete_task()` fires
  a fire-and-forget, non-blocking `PlaybookSynthesizer` that asks the
  **headless** Queen (decision shape #7 — no metered API) whether the
  task encoded a generalizable procedure and, if so, persists a
  `candidate` playbook. New v10 schema (`playbooks` + `playbook_events`,
  optional fts5 with LIKE fallback) and `PlaybookStore` with exact-
  duplicate folding by `content_hash`. Synthesis is volume-gated
  (`PlaybookConfig`: eligible task types, min resolution length,
  per-(worker,task) memoization, `max_synth_per_hour`) and logged to the
  buzz log (`PLAYBOOK_SYNTHESIZED` / `PLAYBOOK_SKIPPED`, category DRONE).
  New `swarm_get_playbooks` MCP worker tool recalls scoped active
  playbooks via fts5. Distinct from the `skills` registry / `SkillsStore`
  (untouched) and Claude Code `.claude/skills/` artifacts. Later phases
  (recall-at-dispatch, win/loss attribution, auto-promote/prune,
  `.claude/skills/` propagation, dashboard) are deliberately out of
  scope. Borrowed from Hermes Agent's learning loop, re-scoped to
  Swarm's true-multi-agent + subscription model.

### Changes

### Fixes

## [2026.5.16.4] - 2026-05-16

### Changes

- **The Command Center now embeds the interactive Queen's real live PTY
  session, replacing the chat-relay UI.** The "Ask Queen" chat box was an
  indirect bridge (operator → HTTP → inject into her PTY → she calls
  `queen_reply` → WS → panel must swap a placeholder) with ~5 independent
  failure points; it kept leaving the panel stuck on "thinking" even though
  the Queen had answered (the reply was persisted in `queen_messages` but
  never rendered). It now mounts her actual `/ws/terminal?worker=queen`
  session in the right CC panel using the same proven, worker-agnostic
  terminal infrastructure every worker uses — one cached xterm, one
  connection, moved between the embed holder and `#detail-body` via
  `appendChild`. A "⛶ Full screen" button opens her exactly like a worker
  (the queen-card stays the Command Center nav — it is the only path back
  to the CC, so it was deliberately *not* repurposed). The fragile
  chat-relay JS, the `queen.message`/`queen.thread`/`queen.activity` WS
  handlers, the daemon `queen.activity` ticker loop, and
  `extract_queen_activity_line` are deleted. The backend thread machinery
  (`/api/queen/threads`, `_forward_to_queen`, `queen_reply`,
  `queen.message`/`queen.thread` broadcasts) is unchanged — it still
  serves the Attention queue, worker→queen messaging, and oversight
  threads. The Queen health dot (`queen.health`) is retained.

## [2026.5.16.3] - 2026-05-16

### Changes

- **Ask Queen now talks to the interactive Queen, not the headless
  subprocess.** The Command Center "Ask Queen" panel posted operator
  questions to `/api/queen/ask`, which fired the stateless, toolless
  headless `claude -p` Queen — she has no `queen_view_task_board` /
  `queen_view_buzz_log` / `queen_view_message_stream` /
  `queen_view_worker_state`, so coordination questions ("why did
  rcg-networks get a task?") timed out at 120s or got speculation. The
  panel now posts to the interactive-Queen thread path
  (`/api/queen/threads`), which forwards into her PTY; she answers with
  real tools and her reply renders live via the `queen.message` /
  `queen.thread` / `queen.health` WebSocket events (previously broadcast
  but never consumed by the dashboard). Matches the documented
  division of labor in `docs/specs/headless-queen-architecture.md`.
- The Ask Queen panel shows a live activity ticker while she works
  (what she's doing — tool calls, board reads — instead of a frozen
  spinner), driven by a new daemon `queen.activity` broadcast
  (2s cadence, BUZZING-gated, debounced) + `extract_queen_activity_line`
  (ANSI/terminal-chrome stripping).
- `_forward_to_queen` now reports delivery; create/post-message
  responses include `queen_delivered` so the panel surfaces "Queen
  offline — saved, she'll answer when back" instead of hanging.
- Removed the headless `/api/queen/ask` endpoint and the now-unused
  `operator-question` thread kind; the panel standardizes on the
  `operator` thread kind.

## [2026.5.16.2] - 2026-05-16

### Fixes

- Expired/revoked Jira OAuth tokens now surface a clear, actionable
  message instead of an opaque 500. `_ensure_session` raised a bare
  `RuntimeError` when the refresh token was invalid or no token
  manager was configured; uncaught, `handle_errors` turned it into
  "Internal server error" + error_id across `/api/jira/preview`,
  `/sync`, and `/import-by-key`. New `JiraAuthError(RuntimeError)` is
  raised instead and mapped by `handle_errors` to a 400 with
  "Jira authorization expired or revoked — reconnect Jira on the
  Config page", which the dashboard shows as a toast. Subclassing
  `RuntimeError` keeps existing catchers working.

## [2026.5.16] - 2026-05-16

### Fixes

- BACKLOG tasks can now be moved to Unassigned and assigned from the
  dashboard. Two stacked guards made a normal operator action fail
  nonsensically: (1) `_apply_status_change` had no `backlog → *` case,
  so changing a BACKLOG task's status to Unassigned via the edit modal
  silently no-op'd; (2) `handle_action_assign_task` tried to reach
  UNASSIGNED via `board.unassign()`, but that method only accepts
  ASSIGNED/ACTIVE and silently no-ops on BACKLOG, so `d.assign_task`'s
  `is_available` gate still 409'd. Both paths now use the same
  `task.approve()` BACKLOG → UNASSIGNED transition the "Hand to Queen"
  promote button uses, so the edit-modal status dropdown and the
  Assign action both work for backlogged tasks. Completes the
  2026.5.15.4 reassignment fix, which only covered ASSIGNED/ACTIVE.

## [2026.5.15.4] - 2026-05-15

### Fixes

- Task reassignment from the dashboard edit modal no longer silently
  lost. `/action/task/assign` 409s for any task not in `UNASSIGNED`
  (the `is_available` gate is meant to stop the auto-assign *drone*
  poaching in-flight work, not to block an explicit operator assign).
  The frontend chained the edit POST off the assign without checking
  its result, so the edit succeeded and the modal reported "Task
  updated" while the reassignment was dropped. Server now mirrors the
  proven Queen reassign path (unassign-then-assign) so operator
  (re)assignment of ASSIGNED/BACKLOG/ACTIVE tasks works; the frontend
  now surfaces a failed assign instead of a false success.

## [2026.5.15.3] - 2026-05-15

### Fixes

- Holder-bounce button now actually works. `bounceHolder()` used a bare
  `fetch()`, so the request carried no `X-Requested-With` header and the
  `_csrf_middleware` rejected it with `403 "Missing X-Requested-With
  header"` — every click since the button shipped (2026.5.14.2). The
  pre-2026.5.15.2 swallow-all error handling hid the 403 entirely
  (silent no-op); the 2026.5.15.2 honesty fix surfaced it as the visible
  "Not authorized (status 403)" toast that exposed the real cause. Now
  uses `actionFetch()` like every other state-changing dashboard POST
  (Reload, task actions, …), which sets the CSRF header.

## [2026.5.15.2] - 2026-05-15

### Fixes

- Holder bounce / server Reload no longer wedge the daemon. Both
  `handle_holder_bounce` and `handle_server_restart` did a bare
  unbounded `await reinstall_from_local_source()`; that runs up to
  three `uv` subprocess steps at 120 s each (~6 min worst case). For
  the bounce the holder is already SIGTERM'd before that await, so a
  stalled reinstall meant the daemon never restarted — a silent
  multi-minute no-op (reported on 2026.5.15). Extracted a shared
  `_best_effort_reinstall()` helper that wraps the reinstall in a 30 s
  `asyncio.wait_for` and swallows timeout/failure; the restart now
  always proceeds. Applied to both restart paths so the class can't
  reappear.
- Holder-bounce button now reports outcomes honestly. The frontend
  did `r.json().catch(()=>({}))`, which swallowed every non-JSON error
  (404/401/HTML) into silence behind an optimistic "Bouncing…" toast.
  It now branches on `r.ok`/status with distinct messages and states
  the connection-drop case (expected mid-restart) instead of implying
  success.

## [2026.5.15] - 2026-05-15

### Fixes

- Worker terminal no longer wraps Claude's output at ~6 columns after
  switching from the Queen Dashboard back to a worker. `showTermEntry`
  reconnects the WS before the flex layout settles, so
  `fitAddon.proposeDimensions()` measured a ~54px container and returned
  ~6 cols, which got sent in the `/ws/terminal` query string and
  SIGWINCH'd to the holder. Added a `MIN_TERM_COLS=20` / `MIN_TERM_ROWS=4`
  sanity floor enforced at all four resize paths (WS-open URL,
  `sendResizeIfChanged`, `forceFitAndResize`, ResizeObserver); sub-floor
  measurements are treated as not-ready and the resync retry ladder
  (rAF/80/220/600 ms) applies the correct size once layout settles.
  Self-healing for already-mis-wrapped sessions.

## [2026.5.14.2] - 2026-05-14

### Features

- "Bounce holder" button on the PTY holder drift banner. New endpoint
  `POST /api/holder/bounce` SIGTERMs the holder PID, removes the
  socket + PID files, reinstalls from local source, and triggers the
  same daemon-restart path as the Reload button. One-click upgrade
  flow for `holder.py` changes — no terminal paste required. Confirm
  modal warns that all workers will be killed (the daemon respawns
  them) and that a browser/PWA hard-refresh may be needed.

## [2026.5.14] - 2026-05-14

### Features

- Floating "Jump to bottom" pill on each worker terminal. Appears when the
  operator scrolls away from the bottom; one click re-arms auto-follow.
  Mobile-friendly (44 × 44 px tap target).

### Fixes

- Worker terminal viewport no longer snaps back to the bottom when the
  operator scrolls up during heavy worker output. Replaced the
  `_isAutoScrolling` / `_writesPending` guards in the scroll handler with
  a wheel-capture listener on the xterm root, a DOM scroll listener on
  `.xterm-viewport`, and an unguarded xterm `onScroll` — three
  independent signals converging on a single `stickyBottom` truth.
- Set `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1` on Claude worker PTY
  spawns so output flows into xterm.js's main buffer (5000-line
  scrollback) instead of the alternate buffer (no scrollback). Upstream
  context: anthropics/claude-code#42670.
- "Copy" button on the holder-drift banner now actually copies — added
  the missing entry to the `data-action` dispatch table.

## [2026.5.11] - 2026-05-11

### Features

### Changes

### Fixes

## [2026.5.8.6] - 2026-05-08

### Features

### Changes

### Fixes

- **Inline mini swap bar now colors from `pressure_level`, not raw
  `swap_percent`** (task #353). Followup to #352: the popover was
  reworked but the small inline bar in the dashboard top row still
  derived its color from `swap_percent`, so on a healthy long-uptime
  workstation with sticky cold pages the bar would go orange/red
  while the pressure badge sat at NOM. Bar *width* still reflects
  `swap_percent` (a fair "how full is the disk-backed pool"
  indicator); only the *color* switches to the pressure-driven
  palette so it tracks the badge instead of contradicting it. Single-
  file change in `src/swarm/web/static/dashboard.js:734–738`. No
  backend changes needed — `pressure_level` was already on the
  snapshot from #352.

## [2026.5.8.5] - 2026-05-08

### Features

- **Resource widget surfaces PSI + swap I/O instead of standing percentages**
  (task #352). The dashboard "Bee Hive" popover and the underlying
  `ResourceSnapshot` now expose three pressure signals that actually correlate
  with worker performance:
    * `psi_cpu_avg10`, `psi_mem_avg10`, `psi_io_avg10` — kernel PSI from
      `/proc/pressure/{cpu,memory,io}` (the `some avg10=` value, % of last 10 s
      processes stalled). `psi_available` flag tells the UI when CONFIG_PSI=n
      kernels should hide the row instead of showing zeros.
    * `swap_in_per_sec`, `swap_out_per_sec` — pages/sec derived from
      `pswpin`/`pswpout` deltas in `/proc/vmstat`. `ResourceMonitor` keeps the
      previous `(in, out, ts)` reading on the instance for stateful diffing
      (counter rollback or zero-dt → 0.0 instead of negative/divide errors).
    * `top_workers_by_rss` — top-N worker process trees by total RSS, populated
      only when pressure ≠ NOMINAL so the per-tick cost stays trivial under
      healthy load.
  `classify_pressure` now accepts `psi_mem_avg10` as a floor: ≥ 10 forces at
  least ELEVATED, ≥ 30 forces HIGH. The override never demotes — percentage-
  based escalations stay where they are. The dashboard popover reorders to PSI →
  Memory → Load (% utilized vs cpu_count) → Swap I/O (✓ when zero) → Top by RSS
  → pressure box → suspended / d-state → collapsible details (standing swap
  pool, demoted from headline). Backwards compatible: `to_dict()` keeps every
  legacy key (`mem_percent`, `swap_percent`, `pressure_level`, …); existing API
  consumers see a strict superset.

### Changes

### Fixes

## [2026.5.8.4] - 2026-05-08

### Features

- **MCP `structuredContent` sidecars on view tools.** Phase 3 of the
  Apr–May 2026 Anthropic-features bundle. The original spike plan
  targeted speculative SEP-1865 UI widgets, but a read of Claude Code
  2.1.x's source (verified at `services/mcp/client.ts:2662`,
  `transformMCPResult`) showed that `structuredContent` was already a
  shipped, supported feature — when a tool result includes it, Claude
  Code prefers it over the markdown content array, JSON-stringifies
  it, and pairs it with an inferred compact schema for the model.
  The six Queen view tools (`queen_view_worker_state`,
  `queen_view_task_board`, `queen_view_messages`,
  `queen_view_message_stream`, `queen_view_buzz_log`,
  `queen_view_drone_actions`) plus the worker-side `swarm_task_status`
  now return both the existing markdown text content AND a typed JSON
  sidecar with the same data. The Queen sees both — text for thread
  rendering, JSON for queryable reasoning. Handlers may opt into the
  new shape by returning `{"content": [...], "structuredContent":
  {...}}` instead of the bare list; `handle_tool_call` and
  `_handle_tools_call` thread either shape through. Empty-result
  paths still return the legacy list so older clients never see
  half-built sidecars. Fully backwards compatible — clients that
  ignore `structuredContent` see exactly the prior payload shape.

### Changes

### Fixes

## [2026.5.8.3] - 2026-05-08

### Features

- **`acceptance_criteria` is now wired into the verifier.** The field has
  lived on the `tasks` table and `SwarmTask` since v1 but was unread by the
  verifier — workers could declare success criteria and the verifier
  ignored them. Phase 2 of the Apr–May 2026 Anthropic-features bundle
  closes that loop: the Tier-2 verifier prompt now requests an optional
  per-criterion `criteria: [{"text", "passed"}]` array in its JSON output;
  the parser carries it through as `VerifierVerdict.criteria_results`;
  the drone formats failed criteria verbatim into `verification_reason`
  (e.g. `"diff missed criterion (failed criteria: 'returns 200',
  'logs event')"`). `swarm_create_task` accepts a new optional
  `acceptance_criteria: list[str]` argument that flows through `edit_task`
  to the task row at creation. Empty / whitespace-only entries are
  filtered. Backwards compatible: tasks without criteria see no behaviour
  change.

### Changes

### Fixes

## [2026.5.8.2] - 2026-05-08

### Features

- New **Dreamer drone** (`src/swarm/drones/dreamer.py`) periodically scans the
  buzz log for recurring failure / oversight signatures (verifier reopens, task
  failures, oversight interventions, worker-reported blockers) and auto-curates
  matching `queen_learnings` rows tagged `discovered_by_dreamer:{action}:{key}`.
  Workers and the Queen surface them through the existing
  `swarm_get_learnings` / `queen_query_learnings` tools — no new client surface
  needed. v1 is fully deterministic (regex-based signature normalization, no
  LLM call); promotion requires both `dreamer_min_pattern_count` and ≥2
  distinct workers so a single chatty worker can't manufacture patterns.
  Dedupe rewrites the same pattern only after a 7-day refresh window. New
  config knobs on `DroneConfig`: `dreamer_interval_seconds` (default 4h, 0
  disables), `dreamer_lookback_hours` (24h), `dreamer_min_pattern_count` (3).
  Sweeps emit a `PATTERN_DISCOVERED` buzz entry under `LogCategory.DRONE`.
  Inspired by Anthropic's "Dreaming" announcement (2026-05-06).

### Changes

### Fixes

## [2026.5.8] - 2026-05-08

### Features

### Changes

- Queen proposals are now suppressed for whichever worker the operator is
  currently viewing in the dashboard. Focus is signalled by the existing
  `focus` WS command (`pilot._focused_workers`); when the operator is
  hands-on with a worker, escalation/completion/assignment proposals get
  dropped at the `ProposalManager.on_proposal()` chokepoint with a
  `QUEEN_PROPOSAL_SKIPPED_FOCUSED` log entry under `LogCategory.QUEEN`.

### Fixes

- Only one task per worker can show as IN PROGRESS at a time. Previously,
  rapid `swarm_create_task(target_worker=X)` dispatches would each call
  `start_task` and flip every task to ACTIVE without demoting the prior
  one — the dashboard then showed multiple "IN PROGRESS" badges for a
  single worker. `start_task` now demotes any other ACTIVE task for the
  worker back to ASSIGNED before promoting the new one, and a startup
  reconcile (`TaskBoard.reconcile_active_per_worker`) cleans up state
  left behind by older daemon versions on first boot after upgrade.

## [2026.5.7.2] - 2026-05-07

### Features

### Changes

### Fixes

## [2026.5.7] - 2026-05-07

### Fixes
- **Workers stuck RESTING when background shells are running.** Claude Code 2.x's auto mode lets workers background async ``Bash`` commands ("shells") in addition to long-running monitors (dev servers, watchers). The two surface forms are identical except for the noun (``"N shells still running"`` / ``"auto mode on · N shells"`` vs the same with ``monitors``), but the state classifier's ``_RE_MONITOR_RUNNING`` regex only matched the monitor variant — so workers with active background shells were classified RESTING (and eventually SLEEPING) while real work continued, causing the pilot/idle-watcher to consider them free and the dashboard sidebar to mislead operators. Renamed the constant to ``_RE_BACKGROUND_RUNNING`` and broadened the pattern to ``(?:monitors?|shells?)``; updated all three call sites (``classify_output``, ``classify_styled_output``, ``state_tracker._has_active_turn_signal``) and added five regression tests including one that reproduces the original ``budgetbug`` screenshot exactly. Workers will now flip to BUZZING when shells are running, suppressing both auto-assignment and idle-watcher nudges until the background work clears.

## [2026.5.5.24] - 2026-05-05

### Docs
- **CLAUDE.md: ``Verifying out-of-band task assignments`` runbook subsection.** New section in ``CLAUDE.md`` (between Queen message-surface elevation and Live MCP tool-surface propagation) documenting the defensive ``sqlite3 ~/.swarm/swarm.db`` query workers should run before dismissing a claimed task assignment as prompt injection. The swarm system legitimately auto-relays queued or just-assigned tasks into a worker's PTY between turns — the in-session transcript is not authoritative for assignment state, the DB is. Pattern added after a 2026-05-05 incident where this worker dismissed a legitimate ``#331`` assignment (the rules.py ``ALWAYS_ESCALATE`` change shipped in 2026.5.5.23) as injection because the task wasn't visible in the transcript and the requested change was security-sensitive. The DB query would have resolved the ambiguity in under a second.

## [2026.5.5.23] - 2026-05-05

### Features

### Changes
- **drones: ``git push <remote> (main|master)`` is now user-configurable, not hardcoded.** Removed the regex line ``r"|git\s+push\s+\S+\s+(main|master)\b"`` from ``ALWAYS_ESCALATE`` in ``src/swarm/drones/rules.py:63``. The hardcoded escalation was designed for repos with PR-only workflows but it forced the same friction onto repos where direct-to-main is the legitimate workflow (personal IaC, single-maintainer side projects). It also blocked the ``rcg-network`` worker's ``/ship`` flow on Brad's HVAC firewall fix — every prior commit on that repo was direct-to-main, but the rule rejected the push and required a synthetic PR open + ``gh pr merge`` round-trip (also rejected). Repos that want PR-only enforcement add the rule themselves under ``drones.approval_rules`` (one-line YAML: ``- pattern: 'git\s+push\s+\S+\s+(main|master)\b'`` + ``action: escalate``). All other destructive-op coverage in ``ALWAYS_ESCALATE`` (force-push, ``--no-verify``, ``DROP TABLE``, ``rm -rf``, ``reset --hard``, ``DELETE FROM`` without ``WHERE``) is unchanged. Tests in ``tests/test_rules.py`` updated: the ``TestPushToMainEscalation`` class is replaced with ``TestPushToDefaultBranchUserConfigurable`` covering the new fall-through behavior and the user-rule opt-in path; the ``ALWAYS_ESCALATE`` parametrized list moves ``git push origin main`` from ``test_always_escalates`` to ``test_not_always_escalated`` along with ``git push upstream master``. Closes task #331.

### Fixes

## [2026.5.5.22] - 2026-05-05

### Docs
- **README + roadmap docs:** documentation audit covering the 33 release commits between 2026.4.30 and 2026.5.5.21. Three Critical drifts fixed in ``README.md``: architecture-diagram MCP-tool count corrected from "9 coordination tools" to "12 worker · 15 Queen tools" (matches actual count in ``src/swarm/mcp/tools.py`` + ``queen_tools.py``); the Config-page tab list is rewritten at all three callsites (Web Dashboard bullet, "What you get" section, and Configuration heading) to reflect the live tabs (General · LLMs · Workers · Automation · Notifications · Integrations · Security · Usage · Advanced · Logs); the Configuration loading priority is reframed so ``swarm.db`` is the canonical source per 2026.5.5.20 with YAML demoted to a bootstrap-only seed and ``-c <yaml>`` flagged as ignored on populated DBs. Coverage gaps closed: ``swarm holder-restart`` (added 2026.5.4.2) and ``swarm queen contribute-claude-md`` (shipped 2026.4.22.11, never documented) appear in the CLI Reference table; drag-and-drop Jira/Outlook import + ADF→Markdown + HTML→Markdown documented in the Email and Jira sections; WYSIWYG task editor + compact one-or-two-line task rows surfaced in the task-board bullets; ``swarm_task_status({number: N})`` full-detail mode added to the MCP tools table; ``-c`` flag clarified in the Global Flags table. Stale ``docs/features-roadmap.md`` and ``docs/claude-code-roadmap.md`` get a 2026-05-05 update block pointing at CHANGELOG for the post-2026-04-16 surface.

## [2026.5.5.21] - 2026-05-05

### Features

### Changes

### Fixes
- **service: stop installing legacy ``-c <yaml>`` flag in systemd unit, auto-strip on next start.** Companion fix to 2026.5.5.20.  ``service.generate_unit`` no longer writes ``-c ~/.config/swarm/config.yaml`` (or any ``--config``) into ``ExecStart=`` for new installs — the DB is canonical, the YAML override is forbidden when the DB has data, and the flag silently caused Amanda's "saves disappear on restart" symptom on existing installs.  ``ensure_killmode_process`` (auto-runs on every daemon startup via ``_maybe_patch_systemd_unit``) now also strips ``-c <yaml>`` / ``--config <yaml>`` / ``--config=<yaml>`` / ``-c<yaml>`` from the existing ``ExecStart=`` line — so operators on legacy units don't have to manually edit ``~/.config/systemd/user/swarm.service``.  Production unit's ``WorkingDirectory`` is now ``$HOME`` instead of the YAML's parent (load-bearing only when ``-c`` was passed).  Five regression tests in ``tests/test_service.py::TestEnsureKillmodeProcess``.

## [2026.5.5.20] - 2026-05-05

### Features

### Changes

### Fixes
- **cli: ``--config`` no longer overrides a populated swarm.db.** Root cause of Amanda's "I save workflows / approval rules / groups from the dashboard, restart, and they're gone" symptom: a legacy ``swarm.service`` ExecStart of ``swarm serve -c ~/.config/swarm/config.yaml`` survived from the pre-DB era. Every dashboard "Restart" reload preserved that argv through ``os.execv``, so ``_load_config_db_first`` saw ``-c <yaml>``, hit the explicit-override path, loaded a stale YAML that didn't have any of her edits — and silently overwrote the in-memory state with empty data. Save → DB write succeeded → restart → YAML loader won → dashboard rendered the YAML's empty value → operator concluded "the save didn't stick." The doc above ``_load_config_db_first`` already explicitly forbade this ("the daemon must never run against a YAML-sourced HiveConfig when the DB has data") but the implementation honoured ``--config`` unconditionally. Now ``--config`` is honoured ONLY when the DB has no user data — the test / fresh-install / explicit-YAML-bootstrap workflows still work; the legacy-systemd case correctly falls through to the DB. ``_exec_restart`` also strips ``-c`` / ``--config`` from argv before exec so the warning doesn't keep firing on every reload. Regression tests in ``tests/test_cli.py::test_load_config_db_first_yaml_ignored_when_db_has_data`` and ``test_strip_config_flag_handles_all_forms``.

## [2026.5.5.19] - 2026-05-05

### Features

### Changes
- **server: log run_daemon entry state at WARNING.** Decisive triage anchor for Amanda's empty-workflows-on-restart symptom: ``_load_config_db_first(None)`` was confirmed to return ``workflows={'verify': '/verify-skill'}`` from her installed Python, the DB was confirmed to retain the row across restart, but the daemon's ``__init__`` saw ``config.workflows={}``. Added a WARNING log at the top of ``run_daemon`` that prints ``config.workflows``, ``config_source``, and ``sys.argv`` — pinpoints whether the wipe is in cli.py between ``_load_config_db_first`` and ``run_daemon``, or inside daemon construction.

### Fixes

## [2026.5.5.18] - 2026-05-05

### Features

### Changes

### Fixes
- **cli: configure logging before any subcommand invocation.** Pre-fix the bare ``swarm`` path (no subcommand → ``ctx.invoke(start_cmd)``) skipped ``setup_logging`` in ``main()``, deferring it to ``setup_logging_from_cli`` inside ``start_cmd`` — but that runs AFTER ``_load_config_db_first``. Any log emitted by the loader on this path went to a handler-less swarm logger and was silently dropped. Including the 2026.5.5.17 ``load_config_from_db: returning workflows=...`` diagnostic anchor we shipped to triage Amanda's empty-workflows-on-restart symptom. ``setup_logging`` now runs unconditionally at the top of ``main()``; subcommand paths still re-configure with config-file values once cfg is loaded (``setup_logging`` clears handlers before re-attaching, so the early call is harmless).
- **web: log-level dropdown's "Current persisted" indicator updates on save.** The span at ``Logs > Running daemon log level > Current persisted`` was server-rendered Jinja that only refreshed on full page reload. Operator changed the dropdown, ``setRunningLogLevel`` correctly persisted to the DB, but the indicator kept showing the pre-save value — looking exactly like a save failure. JS now updates the span text in the success branch.

## [2026.5.5.17] - 2026-05-05

### Features

### Changes
- **server/db: bump diagnostic workflows logs to WARNING.** The 2026.5.5.15 INFO-level ``daemon init: config.workflows=...`` log was missing from Amanda's swarm.log even though she confirmed she's on 16 and the apply_update entry log fires. Most likely a log-level / handler-timing issue between daemon ``__init__`` and the first ``setup_logging`` call. Bumped both the daemon-init log and a new companion log inside ``load_config_from_db`` (``returning workflows=...``) to WARNING so they survive any verbosity config and can't be silently filtered. Pairs with the existing ``apply_update`` entry/exit logs to cover the full save-load chain — next reproduction will pinpoint whether the loader is dropping workflows or whether something post-load mutates them.

### Fixes

## [2026.5.5.16] - 2026-05-05

### Features

### Changes

### Fixes
- **config: workflows survive unrelated saves.** ``ConfigManager._apply_workflows`` now treats an empty body (``workflows: {}``) as a no-op rather than overwriting ``self._config.workflows`` with empty. The dashboard's ``saveSettings`` always serializes the four Automation-tab inputs into a ``workflows`` dict, omitting empty fields. When the user is editing a different tab and the workflow inputs render empty (because their daemon's ``cfg.workflows`` was already cleared, or browser cache), the body carries ``workflows: {}`` — and pre-fix this wiped the in-memory dict. ``serialize_config`` then skipped the ``workflows`` key on save (since the dict was empty), so the DB row was preserved on disk but the running daemon's state was stale until the next restart. Operators reported "I typed /verify, saved, restarted, it's gone" because every unrelated config save (group edit, drone toggle, …) cleared the in-memory dict in between. Same destructive-empty-overwrite footgun the ``approval_rules`` table had pre-#328; same guard pattern. Explicit clearing from the UI is a future enhancement. Regression test in ``tests/test_config_manager.py::TestConfigManagerApplyUpdate::test_empty_workflows_body_is_noop``.

## [2026.5.5.15] - 2026-05-05

### Features

### Changes
- **server: diagnostic logging on the workflows save/load chain.** Added INFO-level anchors at ``SwarmDaemon.__init__`` (``daemon init: config.workflows=...``), ``ConfigManager.apply_update`` (entry + post-save), and ``handle_get_config`` (``GET /api/config: cfg.workflows=...  serialized.workflows=...``). Triages a class of "config field reverts on restart" symptoms: the DB row + raw ``load_config_from_db`` both verify correct, but the running daemon's serialized config returns the field as ``undefined``. The new logs let an operator pinpoint exactly when ``self._config.workflows`` gets mutated to empty between init and the next GET — narrowing the suspect from "somewhere in the daemon" to a single dispatcher invocation. Pure additive logging; no behavior change.

### Fixes

## [2026.5.5.14] - 2026-05-05

### Features

### Changes
- **web/templates: config-field macros.** Added two narrow Jinja macros at the top of ``src/swarm/web/templates/config.html`` (``config_toggle`` for boolean toggles, ``config_number`` for numeric inputs) and migrated the matching blocks. ~28 of the original 77 ``<div class="config-field">`` blocks now flow through one of the macros — the toggle pattern (14 instances, 100% identical) and the numeric pattern (~14 instances with step/min/max/placeholder variation). The original plan called for a single mega-macro covering all 77 blocks, but a survey revealed three groups: toggles (uniform), numbers (near-uniform), and text/select/custom (~45 blocks with restart-badge + class variation + custom option loops + button layouts that don't fit a one-size macro). Forcing them all through one macro would either be too rigid or too parameter-heavy. The text/select/custom variants stay inline. Phase G of the duplication-cluster sweep — final phase.

### Fixes

## [2026.5.5.13] - 2026-05-05

### Features

### Changes
- **cli/logging:** unified the three identical 8-line blocks resolving CLI flag overrides + config-file fallbacks for log_level / log_file / log_format (in ``serve``, ``daemon``, and ``test`` subcommands at ``src/swarm/cli.py``) onto a new ``setup_logging_from_cli(cli_obj, cfg)`` helper at ``src/swarm/logging.py``. Behavior unchanged; future log-resolution tweaks (e.g. an env-var override) now have one canonical place to land. Phase F of the duplication-cluster sweep.

### Fixes

## [2026.5.5.12] - 2026-05-05

### Features

### Changes
- **server: origin / CSRF check unified.** Three near-identical inline copies of the Origin-header validation (``_csrf_middleware`` in ``server.api``, ``_check_auth`` in ``pty.bridge``, ``_check_ws_access`` in ``server.routes.websocket``) now route through a single ``check_origin_or_error`` helper at ``src/swarm/server/api.py``. Reject responses are unified on text ``Origin rejected`` (was ``CSRF rejected`` / ``WebSocket origin rejected`` / ``CSRF rejected`` respectively) — a 403 either way; no client-visible behavior change since no test or call site asserts on the body text. Phase E of the duplication-cluster sweep.

### Fixes
- **server logging:** origin-mismatch failures from ``_csrf_middleware`` and the pty WS bridge now log at WARNING level with the offending origin, request host, and path. Pre-Phase-E only the dashboard ``/ws`` reject path logged — the CSRF middleware and pty bridge silently returned 403 with no server-side anchor, so a misconfigured reverse proxy looked exactly like a client bug.

## [2026.5.5.11] - 2026-05-05

### Features

### Changes
- **web/toast:** unified the dashboard's and config page's ``showToast`` / ``_toastApplyResult`` implementations onto a single shared module (``src/swarm/web/static/toast.js``). Pre-Phase-D the dashboard's was the fully-featured copy (dedup, screen-reader announce, click-to-dismiss, notification-badge integration via ``addNotification``) and the config page's was a minimal "append a div, remove after 3.5s" copy that silently dropped accessibility and dedup. The shared module adopts the dashboard's feature set; the config page now gets dedup, screen-reader announcements, and click-to-dismiss for free. ``window.addNotification`` is called conditionally so non-dashboard pages don't fail. Phase D of the duplication-cluster sweep.

### Fixes
- **a11y:** the config page's toasts now announce to screen readers via the shared ``#sr-announcer`` aria-live region (relocated to ``base.html`` so all pages benefit). Pre-Phase-D the announcer existed only in ``dashboard.html`` and config-page save/error toasts were silent for screen reader users.

## [2026.5.5.10] - 2026-05-05

### Features

### Changes
- **server/web error handling:** unified the two HTTP error decorators (``handle_swarm_errors`` in ``swarm.web.app`` and ``handle_errors`` in ``swarm.server.helpers``) onto a single canonical implementation at ``src/swarm/server/helpers.py``. Pre-Phase-C the two decorators mapped ``SwarmOperationError`` to different status codes — 400 in server routes, 409 in web routes — which silently routed input-validation failures and state-conflict errors to the same code on one side and a different code on the other.
- **api:** ``SwarmOperationError`` now uniformly returns **HTTP 409 Conflict** across both ``/api/*`` and dashboard ``/dashboard/api/*`` routes (was 400 in server routes pre-Phase-C). 409 better fits the semantics — "operation can't proceed in current state" (Queen offline, worker in wrong state, name already taken, …) — than 400, which means "your input was malformed". Input-validation paths now consistently raise ``ValueError`` and map to **400 Bad Request** through the same canonical decorator. Phase C of the duplication-cluster sweep.

### Fixes

## [2026.5.5.9] - 2026-05-05

### Features

### Changes
- **web/ws-auth:** unified the three authenticated-WebSocket call sites (dashboard main ``/ws``, dashboard terminal ``/ws/terminal``, config page ``/ws``) onto a single ``window.swarmWS.openAuthenticated(path)`` helper at ``src/swarm/web/static/ws-auth.js``. The helper builds the ``ws://``/``wss://`` URL and sends the JSON auth message the server's first-message gate expects, using the shared ``swarmAuth.getToken()`` resolver from Phase A. Adding a new authenticated-WS endpoint no longer means copying URL-build + auth-send boilerplate, and the two cannot drift apart again. Phase B of the duplication-cluster sweep.

### Fixes

## [2026.5.5.8] - 2026-05-05

### Features

### Changes
- **web/auth:** unified the dashboard and config pages onto a single shared auth-token resolver (``src/swarm/web/static/auth.js``, ``window.swarmAuth``). Pre-unification each page resolved the WS-auth / Bearer-auth token independently, and the drift between them shipped the 2026.5.5.7 WS-lockout bug. Both pages now read the token through ``window.swarmAuth.getToken()``; ``setServerToken()`` handles the stale-clear once at page load, and ``clearStaleSessionToken()`` is exposed for runtime auth-failure paths. Phase A of the duplication-cluster sweep — six more clusters (WS auth flow, HTTP error decorators, toast helpers, origin/CSRF check, log-level resolution, config-field Jinja macro) follow in subsequent releases.

### Fixes

## [2026.5.5.7] - 2026-05-05

### Fixes
- **websocket:** the dashboard's main ``/ws`` connection no longer gets locked out for 5 minutes after navigating to the config page. **Real root cause** of the WS lockout symptom Brad reported through Cloudflare tunnel: ``config.html`` opened its own ``/ws`` and read the auth token from ``sessionStorage['swarm_api_password']`` only. For session-cookie-authenticated logins (the default flow) that key is empty, so the config page's WS upgrade sent ``token: ''``. After 5 of those within 5 minutes the IP was locked out — and the per-IP lockout is shared, so the dashboard's main ``/ws`` then got 429s too. ``/ws/terminal`` kept working because it's connected before the config page poisons the lockout, OR with a token from a different code path. Diagnosed via the ``WS auth FAIL (wrong-token, first-message): ... token=<empty>`` lines from 2026.5.5.6's logging. Fix: ``handle_config_page`` now passes ``ws_token`` to the template (same source dashboard uses), and ``config.html`` prefers it over the sessionStorage fallback.

### Features

### Changes

### Fixes

## [2026.5.5.6] - 2026-05-05

### Diagnostics
- **websocket:** ``ws_authenticate`` now logs a WARNING line on every wrong-token failure naming the path (``/ws`` vs ``/ws/terminal``), the IP, the ``type`` field of the received message, and a short summary of the token (length + first 8 chars). The 2026.5.5.4 reject logging told us the lockout was firing; this tells us *who* is feeding it. Dashboard's main /ws keeps tripping wrong-token failures even though /ws/terminal succeeds with the same token — these new lines will let us see whether the tokens actually differ between paths or whether something else is sending a non-``auth`` message at /ws first.

### Features

### Changes

### Fixes

## [2026.5.5.5] - 2026-05-05

### Features

### Changes

### Fixes

## [2026.5.5.4] - 2026-05-05

### Diagnostics
- **websocket:** ``_check_ws_access`` now emits a WARNING-level log on every reject path (origin mismatch / auth lockout / per-IP cap), naming the offending IP and the reason. Pre-fix the handler returned 403 / 429 silently — operators saw "WebSocket connection ... failed:" in the browser console with zero server-side context. The auth-lockout fix in 2026.5.5.3 closed one path; this logging makes the remaining ones diagnosable on the next reproduction.

### Fixes
- **dashboard:** the Logs-tab "Running daemon log level" dropdown now shows a success toast on save (and a warning toast if any body field was ignored), matching the structured ``_apply_result`` flow every other config-save endpoint uses since Phase 7. Pre-fix the dropdown only updated an inline status span, with no toast — looked like the "old saving mechanism".

### Features

### Changes

### Fixes

## [2026.5.5.3] - 2026-05-05

### Features

### Changes

### Fixes
- **websocket:** main ``/ws`` handshake no longer locks the operator out for 5 minutes after a few transient tunnel hiccups. Pre-fix ``ws_authenticate`` returned ``False`` on auth-message timeout, malformed JSON, and wrong-token alike, and the caller in ``handle_websocket`` (and ``handle_terminal_ws``) blindly recorded every ``False`` as a real auth failure via ``record_ws_auth_failure``. After 5 of those within 5 minutes the IP was rate-limited at 429, the dashboard's reconnect loop kept hitting the same wall, and only ``/ws/terminal`` (which doesn't go through ``_check_ws_access``) kept working. Reported through Cloudflare tunnel — slow tunnel makes the 5-second auth-message receive timeout fire intermittently. Now ``ws_authenticate`` records the failure internally and only when the token was actually wrong.
- **coordination:** Swarm-managed scaffolding files (``.claude/commands/swarm-*``, ``.claude/skills/swarm-*``, ``.claude/scheduled_tasks.lock``, ``.claude/ux-audit.json``) no longer produce ``file overlap: ...`` WARNING lines on every reload. Those files are installed identically into every worker repo by the Swarm hooks installer; they were producing 50+ near-identical WARNING lines per poll cycle (one per worker × per scaffolding file) with no actionable signal, drowning out real overlap alerts. Genuine cross-worker overlaps are still tracked and logged, but each (owner, intruder) pair now coalesces to a single WARNING listing up to 5 files plus a count rather than one line per file.

## [2026.5.5.2] - 2026-05-05

### Features
- **dashboard:** Logs is now its own tab in the config nav (was an unreachable nested view). Tab gets a taller log pane (``min-height: 60vh``) plus a "running daemon log level" dropdown that updates the live Python logger via ``PUT /api/config`` — no more hopping to the General tab to bump verbosity while debugging.
- **dashboard:** the dev-mode "Reload" button moved out of the page header into the Updates section under General, where it lives alongside the version number and an explanation of what it does (reinstalls from local source + ``os.execv``s into a fresh process). In dev mode the production "Check for Updates" button is now disabled with a tooltip pointing the operator at ``git pull`` + Reload.

### Changes
- **logs:** severity filter on ``/partials/logs`` is now an inclusive hierarchy — picking ``INFO`` returns INFO + WARNING + ERROR, mirroring how Python's logging module treats threshold severities. Pre-fix it was a naive substring match that hid every WARNING / ERROR line whenever INFO was selected; the only way to see anything beyond INFO was to switch the filter to "All". Filter logic factored into ``swarm.web.log_filter`` so it's testable without dragging in the full web stack.
- **logs:** dashboard log viewer no longer auto-scrolls to the bottom on load. The server returns lines newest-first; the prior ``scrollTop = scrollHeight`` would bury the relevant entries off-screen at the bottom under a screen of older logs.

### Fixes

## [2026.5.5] - 2026-05-05

### Features
- **config:** the remaining 3 multi-field save endpoints — ``POST /api/config/workers/{name}/save``, ``POST /api/config/workers/{name}/add-to-group``, and ``POST /api/config/approval-rules`` — now return a structured ``_apply_result`` and emit WARNING-level logs for unknown body keys. They aren't dataclass-shaped (their bodies are fixed-key dicts like ``{group, create}``), so a new ``validate_body_keys`` helper provides the same drift-detection contract as ``_apply_dataclass_dict``: consumed = body keys present in the expected set, unknown = the rest. Dashboard ``_toastApplyResult`` helper now lives on ``window`` and is invoked from ``dashboard.js`` save-worker / add-to-group / add-rule callsites. Phase 8 of #328 — every multi-field config save endpoint uses the shared instrumentation for success, failure, server logging, and dashboard toasts.
- **dashboard:** drones-toggle button (``POST /action/toggle-drones``) and drag-drop worker reorder (``POST /api/workers/reorder``) now show success and failure toasts. Pre-fix both were silent — the drones button just flipped its label, and drag-drop persisted with no confirmation. ``/api/workers/reorder`` also gains a server-side WARNING log if its raw SQL ``UPDATE workers SET sort_order`` fails, mirroring the forensic contract the dispatch chain has had since 2026.5.4.6. Phase 9 of #328 — closes the single-action save-path gap audit found after Phase 8.

### Changes

### Fixes

## [2026.5.4.11] - 2026-05-04

### Features
- **config:** every dispatch-using save endpoint now returns a structured ``_apply_result`` in its response: per-section ``consumed`` (fields validated and applied) and ``unknown`` (body keys with no matching dataclass field) lists. Covers ``PUT /api/config`` (bulk autosave), ``POST /api/config/workers``, ``POST /api/config/groups``, and ``PUT /api/config/groups/{name}``. Dashboard reads it and surfaces unknown-field warnings as a toast ("Saved, but 1 field(s) ignored: foo_bar"). Pre-fix the operator saw a bare success toast whether 5 fields persisted or 0 — server-side drift logs went to ``~/.swarm/swarm.log`` only. Now per-field outcomes surface in the UI. Phase 7 of #328.
- **config:** dispatch coverage extended to ``_apply_coordination``, ``_apply_jira``, ``_apply_advanced``, and ``_apply_test``. All four now return a ``FieldOutcome`` (consumed + unknown) that ``apply_update`` aggregates into the ``ApplyResult``. ``_apply_coordination``'s ``auto_pull`` and ``_apply_advanced``'s ``terminal`` sub-dataclass now flow through generic dispatch — new fields auto-apply, unknown sub-keys emit the standard WARNING. The two group CRUD endpoints (``POST /api/config/groups``, ``PUT /api/config/groups/{name}``) now use full ``_apply_dataclass_dict`` dispatch instead of warn-only sweeps. Phase 7 of #328.

### Changes

### Fixes

## [2026.5.4.10] - 2026-05-04

### Features

### Changes
- **config:** ``POST /api/config/workers`` now accepts every writable ``WorkerConfig`` field via generic dataclass dispatch, not just the previously cherry-picked ``name``/``path``/``description``/``provider``. Closes the audit-flagged ``isolation`` (worktree mode) and ``identity`` (per-worker CLAUDE.md path) silent-drop gaps — operators creating a worker through the API can now set those fields and have them persist. ``approval_rules`` and ``allowed_tools`` are intentionally skipped: rules use a dedicated endpoint with regex compile + DB sync semantics, and ``allowed_tools`` doesn't have a DB column yet (separate audit gap, deferred). Phase 6 of #328.
- **config:** ``POST /api/config/groups`` and ``PUT /api/config/groups/{name}`` now emit a section-prefixed WARNING for any unknown body key the dashboard might send, mirroring the per-section guards added in Phase 3 / 2026.5.4.9. GroupConfig only has ``name`` + ``workers`` so the active surface is small, but future schema drift between dashboard and server now surfaces as a default-level operator log instead of a silent drop. Phase 6 of #328.

### Fixes

## [2026.5.4.9] - 2026-05-04

### Features

### Changes
- **config:** per-section ``_apply_X`` handlers now run a generic dataclass-aware dispatch pass after their custom validators. This eliminates the cherry-pick allow-list pattern that produced the silent-drop bug class — adding a field to ``DroneConfig``, ``QueenConfig``, ``TestConfig``, or ``NotifyConfig`` no longer requires a corresponding manual update to a hand-maintained scalar list. Generic dispatch type-validates against ``__dataclass_fields__`` and emits a section-prefixed WARNING for any unknown sub-key (e.g. ``drones.garbage_field``) — same fail-loud signal as the top-level guard from 2026.5.4.8 but at section depth. Phase 3 of the multi-phase #328 fix.
- **config:** ``_apply_drones`` now persists fields that were silently dropped by the previous allow-list: ``enabled`` (drone toggle), ``context_warning_threshold``, ``context_critical_threshold``, ``speculation_enabled``, ``idle_nudge_interval_seconds``, ``idle_nudge_debounce_seconds``. ``_apply_test`` now persists ``enabled``. None of these were currently bug-causing because the dashboard didn't send them, but they're operator-editable from the API and were silently lost — the audit (Phase 1) flagged them as Bug C class drift.

### Fixes
- **dashboard:** group-edit modal now reads its source data (``allWorkers``, ``currentMembers``) from a live JS state cache rather than page-load Jinja. Pre-fix, creating a new group and immediately clicking Edit on it opened the modal with empty members because the inline ``{% for g in config.groups %}`` loop was rendered server-side at page load and never knew about groups created in the current session — operators had to Ctrl-Shift-F5 to recover. The cache (``window._configState.groups``, ``.workers``) is seeded at page load and mutated in lockstep with every successful group/worker CRUD response. Phase 5 of #328 (Bug A from Amanda's report).

### Tests
- **config:** comprehensive end-to-end ``HiveConfig`` round-trip test (``tests/test_config_store.py::TestComprehensiveRoundTrip``). Builds a config with non-default values for every persistable field, walks it through ``save_config_to_db → load_config_from_db``, and asserts the serialized dicts match. Locks in the persistence contract for every field; future drift fails this test loudly. Found one real bug along the way: the ``groups`` table has no ``sort_order`` column so group display order is lost on reload (Bug D, tracked separately for the next release). Phase 4 of #328.

## [2026.5.4.8] - 2026-05-04

### Features

### Changes
- **config:** ``ConfigManager.apply_update`` now warns at WARNING level on unknown top-level body keys. Previously every per-section ``_apply_X`` cherry-picked sub-fields it knew about and the dispatcher itself had the same bug for top-level keys — a dashboard typo or schema drift between client and server would silently drop entire sections with no operator signal. The fail-loud guard catches future schema drift the moment a key arrives that no handler consumes. Phase 2 of the multi-phase silent-drop fix from #328.

### Fixes
- **config:** ``ConfigManager.check_file`` (YAML hot-reload) no longer overwrites in-memory groups when the YAML on disk lacks a groups section. Mirrors the existing ``approval_rules`` preservation pattern at lines 152-154 — groups live in the DB in DB-first mode, so an unrelated scalar edit to ``swarm.yaml`` shouldn't wipe them. ``check_file`` has no production caller in this branch (operator-driven reloads use ``os.execv`` from ``_exec_restart``), but the path was a footgun for anyone wiring it up later. Phase 2 defensive fix from #328.

### Docs
- **audits:** added ``docs/audits/config-save-chain-2026-05-04.md`` — full layer-by-layer coverage matrix for every ``HiveConfig`` field across the six save-chain layers (dataclass / saveSettings JS / apply_update / per-section _apply_X / save_config_to_db / load_config_from_db). Identifies all currently-affected fields and informs Phase 3 (generic dispatch), Phase 4 (round-trip test), Phase 5 (UI reconciliation). Phase 1 deliverable for the multi-phase #328 plan.

## [2026.5.4.7] - 2026-05-04

### Features

### Changes

### Fixes
- **config:** ``ConfigManager._apply_notifications`` now persists the full ``NotifyConfig`` schema. The previous version only handled three top-level scalars (``terminal_bell``, ``desktop``, ``debounce_seconds``) and silently discarded everything else — ``email.*``, ``webhook.{url,events}``, ``templates``, ``desktop_events``, ``terminal_events``. Operators editing SMTP settings in the dashboard saw the "saved" toast but the values never reached ``save_config_to_db``; after a restart the page rendered the defaults again, looking like a load-time bug while the actual defect was here in the apply path. Reported in #328 (Bug C). Also factored a shared ``_validate_string_list`` helper to keep the per-section apply functions under the C901 complexity gate.
- **notify:** ``filtered_backend`` and ``make_email_backend`` now tolerate unknown event-type names by skipping them with a debug log instead of raising ``ValueError``. The pre-existing ``test_config_notification_validation`` contract — "validation is advisory; bad event names shouldn't block the save" — was being upheld accidentally because ``_apply_notifications`` was discarding the ``desktop_events`` field before it ever reached the bus. Once the apply path was fixed, the bus's strict construction would crash the whole apply chain on a single typo, returning HTTP 400 to the dashboard. Now an unknown name is skipped, the rest of the config saves, and the typo is preserved verbatim in the DB for forensics.

## [2026.5.4.6] - 2026-05-04

### Features

### Changes

### Fixes
- **config:** DB save failures in ``ConfigManager._save_to_db`` now log at WARNING level (was DEBUG) so they show up in default-level operator logs. Reported in #328: a user's Groups edits weren't persisting across reboots, and there was no forensic evidence at WARNING because the failure was being swallowed at DEBUG. Also locks in the existing runtime ``log_level`` propagation (config edit → ``setup_logging`` reconfigures the live ``swarm.*`` logger, no restart) with a regression test so the diagnostic flag itself can't decay.

## [2026.5.4.5] - 2026-05-04

### Fixes
- **dashboard:** task modal stops jumping when "View source" is toggled. Both the rich-text editor and the source textarea now use ``height: 18rem`` (exact pin) instead of ``min-height: 18rem`` — empty ``contenteditable`` collapsed tighter than an empty textarea on min-only constraints, so toggling moved the rest of the modal up or down by ~5rem. Overflow scrolls inside the editor; user-resize (``resize: vertical``) is off because asymmetric resizing would re-introduce the jump on the next toggle.

## [2026.5.4.4] - 2026-05-04

### Changes
- **dashboard:** task list collapses to one or two lines per task. The metadata row (status / `#N` / priority / type / cross-project / title / assigned worker / age / badges / actions) stays as the always-visible line; completed tasks add a single-line resolution excerpt below. Description preview, acceptance criteria summaries, context refs, tag chips, and attachment thumbnails no longer render inline — click the row (anywhere except a button/link/input) to open the Edit modal for full content. Hover the row for a native tooltip with the first ~200 chars of the description.

## [2026.5.4.3] - 2026-05-04

### Features

### Changes

### Fixes
- **worker:** ``WorkerService.launch`` now passes ``resume=True`` when re-launching workers post-holder-respawn. Previously the post-Reload re-launch path (``if workers:`` branch — fires when ``self._workers`` already has entries from the prior daemon process) called ``add_worker_live`` without the kwarg, defaulting to ``resume=False``, so the provider command came out as ``["claude"]`` instead of ``["claude", "--continue"]``. Result: every Reload that involved a holder respawn lost in-progress Claude Code conversation state for every worker. Regression test in ``test_worker_service`` asserts the kwarg.

## [2026.5.4.2] - 2026-05-04

### Features
- **pty:** graceful holder restart — new `restart_in_place` IPC command and `swarm holder-restart` CLI. The holder snapshots its worker registry + ring buffers to `~/.swarm/holder-handoff.json`, marks each PTY master FD as inheritable via `F_SETFD`, and `os.execv`s into a fresh `swarm.pty.holder --inherit` invocation. Worker child processes (Claude Code sessions) are unaffected — they own the slave end of the PTY and the kernel keeps it open as long as anyone holds the master. This makes future holder code rollouts (e.g. the 2026-04-21 `_MAX_WRITE_BUFFER` raise from 1 MB to 8 MB) zero-disruption: previously the only way to deploy a holder fix was `kill <holder_pid>` which terminated every running worker session.
- **dashboard:** task description editor became a real WYSIWYG. Visible surface is a `contenteditable` div that renders Markdown (headings, lists, bold/italic, code, blockquotes, images); a hidden source textarea always carries the markdown serialization (form submission + `htmlToMarkdown` round-trip every input/blur). New "View source" toggle reveals raw markdown for power users — toggling preserves height (`visibility: hidden` on the toolbar instead of `display: none`). New formatting toolbar with B / I / S, H1 / H2 / H3, bullet & numbered lists, blockquote, link, inline code, horizontal rule, clear formatting — all driven by `document.execCommand` against the contenteditable.

### Changes

### Fixes
- **dashboard:** description grid now drops to a single column when the preview pane is off, so the textarea fills the full modal width instead of staying half-width.

## [2026.5.4] - 2026-05-04

### Features
- **dashboard:** task descriptions now render Markdown — paste from Word, Outlook, or any rich source and headings, paragraphs, lists, bold/italic, links, images survive into the saved task. Live preview pane next to the textarea (toggleable) plus rendered descriptions in the task list.
- **paste:** HTML→Markdown converter for clipboard payloads, with fallbacks: Word desktop's RTF clipboard is parsed for embedded `\pngblip`/`\jpegblip` image hex when no file blobs are exposed; images upload immediately on paste so saved descriptions never carry stale `blob:` URLs; relative `![](media/foo.png)` refs (pandoc-style) are auto-rewritten to `/uploads/<basename>` when matching files are dropped onto the dropzone. Word `MsoListParagraph` paragraphs become real markdown bullets.
- **jira:** drag-and-drop import — drop a Jira issue URL (or bare `KEY-N`) onto the task panel and a single `/api/jira/import-by-key` call pulls the issue, comments, and attachments into a new task. New `JiraSyncService.import_one` + `POST /api/jira/import-by-key`.
- **jira:** ADF descriptions and comments now convert to Markdown — paragraphs, headings, lists, blockquotes, code blocks, inline marks (bold/italic/code/strike/links), mentions, emojis, hard breaks all preserved. Replaces the old `_extract_text` flatten that produced one space-joined run-on string.
- **email:** `_html_to_text` rewritten as an `HTMLParser`-based Markdown emitter — same fidelity as the Jira ADF path. Inline `cid:<contentId>` image refs in the body get rewritten to `/uploads/<basename>` after the matching attachment is saved, so embedded Outlook images render in the preview instead of showing as broken refs.
- **email-drop:** Outlook drag-and-drop now prefers the Graph fetch path (`multimaillistmessagerows` → `/me/messages/{id}?$select=…&$expand=attachments`) over the bare-subject `text/plain` fallback. Cascade: `body.content` → `uniqueBody.content` → `bodyPreview` so signature-only or stripped-body emails still produce text.
- **mcp:** `swarm_task_status({number: N})` returns the full task detail (description, priority, type, tags, deps, jira key, acceptance criteria, context refs, attachments, resolution) instead of just the title one-liner. List views stay compact.
- **mcp:** worker task messages include per-format extraction hints — `IMAGE: …`, `TEXT: …`, `WORD DOC: pandoc … / docx2txt …`, `PDF: pdftotext … / pypdf …`, `SPREADSHEET: openpyxl …`, `PRESENTATION: pandoc / python-pptx …` — so workers know which tool to reach for instead of trying `Read` on a binary blob.
- **dashboard:** task modal UX refactor. Description + live preview now sit side-by-side on screens ≥1100px (textarea fills full width when preview is off). Cross-project, acceptance criteria, context refs, and depends-on are consolidated into one `<details>`-based "Advanced" section that defaults closed with a count badge showing how many fields are populated; auto-expands on edit when data is present.
- **dashboard:** attachment chips in the modal and task list are now clickable links pointing at `/uploads/<basename>`, with the 12-char content-hash prefix stripped for display.

### Changes
- **hooks:** worker projects' `.claude/settings.json` now grants `Read(//<home>/.swarm/uploads/**)` and `Read(//<home>/.swarm/cross-tasks/**)` so absolute paths into Swarm-shared dirs (Jira attachments, pasted images, email imports) auto-allow without prompting.
- **dashboard:** Assign-and-start now dispatches to `SLEEPING` workers in addition to `RESTING`. Sleeping workers were previously left with a queued task that only the IdleWatcher would later push, with debounce — now they get the task message immediately.

### Fixes
- **email:** `<meta>` and `<link>` are no longer treated as skip containers in the HTML→markdown parser. They're void elements (no end tag) so including them in `_SKIP_TAGS` permanently elevated `_skip_depth` and silently dropped the entire `<body>` of any standard Outlook/Graph email envelope.
- **paste-render:** markdown image/link/code tokens now reserve via null-byte sentinels before the emphasis transforms run, so URLs like `/uploads/abc_pasted_0.png` no longer get their `_pasted_` segment mangled into `<em>pasted</em>`.
- **paste-render:** soft newlines within a paragraph render as `<br>` instead of being collapsed to a space, so email-header blocks (From/To/Subject/Sent on consecutive lines) display one-line-per-line in the rendered preview and task list.

## [2026.5.1] - 2026-05-01

### Features

### Changes

### Fixes
- **drones:** two-strike rule for IdleWatcher's `/mcp` recovery path (task #257). The original "no MCP activity since daemon boot" trigger was too coarse — a worker just legitimately parked on a task tripped the same signal as a worker whose Claude Code transport had really died, so every daemon reload produced a noisy `/mcp` injection on quiet workers. The watcher now records a first-strike marker and falls through to the normal task nudge on the first sweep; only a *second consecutive* sweep that still sees zero MCP activity injects `/mcp`. Workers with a healthy transport answer the warning-shot nudge with an MCP call and never see `/mcp`. New `_mcp_first_strike` set in `IdleWatcher`; updated `tests/test_mcp_tools_stale_recovery.py` with the three new sequence assertions (warning shot → /mcp on second sweep → no /mcp when activity recorded between).

## [2026.4.30] - 2026-04-30

### Features
- **Per-worker `/swarm-*` slash commands (task #283).** Workers now get six slash commands installed into `.claude/commands/` on every daemon start: `/swarm-status`, `/swarm-handoff`, `/swarm-finding`, `/swarm-warning`, `/swarm-blocker`, `/swarm-progress`. They wrap the most-used Swarm MCP tools so transcripts read cleanly and the coordination surface shows up in `/help`. The SessionStart bootstrap appends a one-line nudge listing the commands whenever a task or unread message is already injected, so workers discover the surface without needing to read CLAUDE.md. Sets the `install.py` pattern reused by the Skills work below.
- **`swarm-checkpoint` and `swarm-coordinate` Skills (task #284).** Two Claude Code Skills now install per-worker into `.claude/skills/` via the same `install.py` path that lands the slash commands. `/swarm-checkpoint` runs `/check` then branches: on green, stages changed files (never `-A`) and commits using the project's `/commit` conventions; on red, calls `swarm_report_progress(phase=blocked)` + `swarm_note_to_queen` and halts without committing. `/swarm-coordinate` is advisory only — surveys peer worker states and pending tasks, then outputs a delegation suggestion as text (never calls `swarm_create_task` itself; cross-worker dispatch stays Queen-only). The daemon's per-worker setup loop now invokes both `install_worker_commands` and `install_worker_skills`; the umbrella method was renamed `_install_worker_commands` → `_install_worker_artifacts` to reflect the broader scope.
- **Context-pressure drone — auto `/compact` (task #285 Phase 1).** Phase 0's audit confirmed `worker.context_pct` is already populated every 15s from session JSONL; this phase adds the action layer that turns the pressure signal into a `/compact` injection. Two tiers, state-aware paths: **Soft** (warn ≤ pct < crit, default 0.7) injects `/compact` for RESTING/SLEEPING workers and no-ops for BUZZING/WAITING (retries next sweep). **Hard** (pct ≥ crit, default 0.9) sends Ctrl-C then `/compact` to BUZZING workers, defers WAITING workers (operator owns the prompt), injects `/compact` directly for RESTING/SLEEPING, and skips STUNG. Hysteresis: each `(worker, tier)` fires at most once per approach; the worker must drop below `warn_threshold` to re-arm. Three new `SystemAction` values (`CONTEXT_COMPACT_INJECTED` / `INTERRUPTED` / `DEFERRED`) under `LogCategory.COMPACT`. New `src/swarm/drones/context_pressure.py` (~250 LOC, 94% covered); 24 new tests covering all state × pressure combinations.
- **Tiered verifier drone — adversarial post-completion check (task #286).** Item 4 of 4 from the 10-repo research bundle. Drift in multi-agent flows compounds: N workers means N opportunities for "I'm done" claims that don't match the spec. The verifier fires asynchronously after every `swarm_complete_task` and either confirms the work shipped clean or reopens the task with findings delivered as a `warning` peer message; existing `IdleWatcher` nudges the worker on the next sweep — no new dispatch path. **Tier 1** (deterministic, no LLM, runs first): empty git diff since task start? no `/check` evidence in worker buzz log? open peer warning on this task? → reopen. Most rejections short-circuit here; we never burn an LLM call when the failure is mechanically obvious. **Tier 2** (LLM verification via dedicated `VerifierClient` subprocess, distinct from the headless Queen) runs only when Tier 1 passes; verdict mapping covers `verified` / `uncertain` / `reopen`. Self-loop guard: `VERIFIER_MAX_REOPENS = 2` — after the second reopen still failing, drone escalates via a Queen thread of `kind=verifier-escalation` instead of reopening a third time. `queen_force_complete_task` honours an explicit operator override (`verify=False`). Schema bumped v7 → v8: new `verification_status`, `verification_reason`, `verification_reopen_count` columns on `tasks`. New `LogCategory.VERIFIER` and 7 new `SystemAction` values. Dashboard adds per-task verifier badge (`VERIFIED` / `REOPENED×N` / `ESCALATED` / `SKIPPED`) and a "Verifier flagged" filter chip persisted in localStorage. Files: `src/swarm/queen/verifier.py` + `src/swarm/drones/verifier.py` (~270 LOC) + 30 new tests across `tests/test_verifier_drone.py` (16) and `tests/test_verifier_subprocess.py` (14).
- **Email-completion replies styled as Aptos 12pt.** Replies drafted via `send_completion_reply` (the path that fires when an email-originated task is completed) now wrap the Queen's plain-text body in an inline-styled HTML `<div>` with `font-family: Aptos, Calibri, 'Segoe UI', sans-serif; font-size: 12pt;` so the inserted comment renders in Outlook's default Office 365 font. Inline styles (not `<style>` blocks) because Outlook's Word-based mail renderer drops `<style>` in message bodies but honours `style=""` on block elements. New `_format_reply_html()` helper escapes the body, converts newlines to `<br>`, and wraps in the styled div; empty input returns empty so failure-path callers don't emit a stray `<div>`. 5 new tests + 2 updated.

### Changes

### Fixes
- **Idle workers nudged on unread messages even with no active task.** Closed a structural blind spot: a RESTING/SLEEPING worker with unread messages but no active task on the board got ignored by both `IdleWatcher` (short-circuits when `active_tasks_for_worker()` is empty) and `InterWorkerMessageWatcher` (after #271 narrowed it, only nudged on `dependency` / `warning` types — `finding` / `status` / `note` slipped through silently). The watcher is now task-aware: same `_ACTION_REQUIRED_MSG_TYPES` filter when the worker has an active task (preserves #271's "don't distract in-flight work"), but lifts the filter when the worker has no active task — any unread message is reason to nudge. The buzz log entry now carries a `[no-task]` / `[with-task]` label so audits can tell the widened path from the #271 narrow path. 10 new tests + the existing 18 still green; the conservative `task_board=None` default preserves test fixtures without modification.
- **Daemon startup AttributeError after Skills rename (`_install_worker_commands` → `_install_worker_artifacts`).** Task #284's commit renamed the daemon method but missed the call site at `daemon.py:719`. Symptom: daemon crashed on startup with `AttributeError: 'SwarmDaemon' object has no attribute '_install_worker_commands'`. After a Reload (which `os.execv`s a fresh process) the AttributeError fired immediately and systemd flagged the service as failed → dashboard 502. Fix: update the call site to use the renamed method. The 247 daemon-suite tests still passed because the test fixture short-circuits `start()`; lesson noted that a future fixture should exercise `start()` so missing-attribute regressions in that path can't pass `/check` while breaking the live service.
- **Post-`/mcp` follow-up nudge so workers don't strand (task #315).** When `IdleWatcher` injects `/mcp` to recover a worker whose client-side MCP tool registry was dropped during a daemon reload (task #257), the worker dismisses the dialog and lands at an empty prompt. The same sweep cycle skipped the regular task nudge, so the worker would sit idle until the next sweep — up to `idle_nudge_interval_seconds` (default 180s). Operator evidence on 2026-04-29 (d365-solutions): `/mcp` fired, worker sat at empty prompt for 65s before the queen had to manually intervene with the task description. Fix: after firing `/mcp` successfully, schedule a fire-and-forget follow-up coroutine that waits 5s (configurable) and then sends the regular task nudge. Re-queries the task board so a task completed in the interim is respected, updates `_last_nudge` so debounce stays correct, and logs an `AUTO_NUDGE` entry tagged `post-/mcp follow-up:` for observability. 3 new regression tests covering happy path, task-completed-in-the-interim, and PTY error during follow-up.

## [2026.4.24.6] - 2026-04-24

### Features
- **PTY holder version-skew detection.** Root cause of the long-standing "terminal locks after reload, need 6 restarts" symptom: the holder is a double-forked persistent sidecar, so daemon reloads (os.execv) replace the daemon but leave the holder running with whatever bytecode it was spawned with. Commit 0df45be (2026-04-21) raised `_MAX_WRITE_BUFFER` 1 MB → 8 MB to fix the reload lockup, but the fix never actually ran in production because the operator's holder had been up since April 5 — Reload refreshed the daemon and immediately got dropped again as a "slow client" by the stale holder's 1 MB threshold. Diagnosed live 2026-04-24 by correlating `holder.pid` mtime (Apr 5) against the 5 consecutive `dropping slow client (buffer 1178874 bytes)` warnings in `~/.swarm/swarm.log` at ~1.18 MB — exactly the size the 8 MB change was supposed to tolerate. Fix: `holder.py` now captures a sha256 of its own source at module import time and exposes it via a new `version` MCP-like command (alongside `ping`, `spawn`, etc.). `ProcessPool._try_connect` hashes `holder.py` on disk after each successful ping, compares against the holder's import-time hash, and stores the result as `pool.holder_drift`. Drift triggers a loud `[holder-drift]` WARNING with the exact kill instructions naming the holder PID. Daemon surfaces `holder_drift` via `/api/health` and a dedicated `/api/holder/drift` endpoint. Dashboard adds a persistent red banner at the top ("PTY holder is stale. Reload won't help — kill PID X then restart swarm") with a one-click Copy button for the bounce command. Graceful degradation: an older holder that doesn't know the `version` cmd sets `unknown=true` without asserting drift, so the check itself never breaks the connection. 5 regression tests pin the contract: happy-path no-drift, drift detection + warning + PID naming, graceful-unknown fallback, `/api/health` exposure, `/api/holder/drift` endpoint returns pool state verbatim. Full suite: 3,964 passes.

### Changes

### Fixes

## [2026.4.24.5] - 2026-04-24

### Features

### Changes

### Fixes
- **Queen inbox auto-relay marks read at delivery (task #277).** Queen had no `swarm_check_messages` equivalent — `queen_view_messages` / `queen_view_message_stream` are read-only log views and the #235 PTY relay never touched `read_at`. Consequence: Queen acts on a worker note, but the dashboard inbox still shows it UNREAD forever unless the operator manually marks it. Live repro 2026-04-24: project-root note to queen (force-close #273/#274) → Queen processed + force-closed #274 + relayed the rest → operator checked "did you check your messages" → `queen_view_message_stream since_seconds=7200` still showed UNREAD. Option A from the task write-up: the auto-relay IS the Queen's consumption event, so `_auto_relay_to_queen` (`src/swarm/mcp/tools.py`) now takes an optional `message_id` and calls `d.message_store.mark_read(QUEEN_WORKER_NAME, [message_id])` right after firing the PTY inject. The three call sites (`swarm_send_message` direct-to-queen, `swarm_send_message` broadcast that includes queen via `roster_names.index`, `swarm_note_to_queen`) all pass the id. `queen_view_messages` / `queen_view_message_stream` stay read-only — they use `SELECT *` with no UPDATE. 5 new regression tests in `tests/test_mcp_tools.py::TestSendMessageQueenAutoRelay`: direct-to-queen marks read, broadcast marks queen's row only, note marks read, regular worker-to-worker doesn't touch queen's inbox, queen-self-message no-ops. Full suite: 3,959 passes.

## [2026.4.24.4] - 2026-04-24

### Features

### Changes

### Fixes
- **Remove legacy static-detail fallback that surfaced on mobile.** When the xterm CDN hadn't finished loading or the terminal WebSocket exhausted its reconnect attempts, `refreshDetailStatic()` rendered a pre-xterm HTML partial (`handle_partial_detail`) with `.detail-header`, `.msg-send-bar` ("Send message to …"), and `.worker-output` — a v1.0.0 view that looked stranded next to the modern action bar and mobile send bar on narrow viewports. Deleted `handle_partial_detail` + its `/partials/detail/{name}` route + the dead `sendWorkerMsg` handler + the now-orphaned CSS blocks (`.detail-header`, `.btn-icon`, `.worker-output`, `.tool-activity`, `.tool-pill`, `.msg-send-bar`, `.msg-input`). `refreshDetailStatic()` now renders a minimal spinner + "Connecting terminal…" card into `#detail-body` and retries `attachInlineTerminal(selectedWorker)` every 200 ms until `typeof Terminal !== 'undefined'`, mirroring the existing page-load `restoreWorker` poll at `dashboard.js:6613`. Full suite: 3,954 passes.

## [2026.4.24.3] - 2026-04-24

### Features

### Changes
- **Zero-drift invariant pinned: drone unread count and swarm_check_messages read from the same source (task #272).** Task was filed on the premise that `InterWorkerMessageWatcher` reported a phantom `4 total` nudge for `wifi-portal` while both the worker's `swarm_check_messages` and the Queen's `queen_view_messages since_seconds=86400` returned empty. Investigation: raw `sqlite3` dump showed four real rows — `id IN (123, 144, 164, 183)`, all `recipient='wifi-portal'`, all `read_at IS NULL`, all `msg_type='finding'`, from `public-website` / `project-root` / `public-website` / `public-website` on 2026-04-19 / 2026-04-20. Running `MessageStore.get_unread('wifi-portal')` directly against the live DB returned the same 4 rows. Both the drone's sweep and `_handle_check_messages` call `d.message_store.get_unread(worker_name)` — identical single-source query. No dual code path, no stale cache, no soft-delete hiding the rows from one caller and not the other. Queen's "no messages match" was a time-window artifact — `since_seconds=86400` excluded the 4-to-5-day-old messages. Worker's repeated empty `swarm_check_messages` results trace to a client-side stale-tools state (task #257's failure class: HTTP MCP transport dropped its session mid-reload, the call never reached the server). **The reported bug was a symptom of two already-shipped-but-not-deployed fixes**: (a) #271 (2026.4.24.2) filters `finding`-only inboxes to `AUTO_NUDGE_MESSAGE_SKIPPED` instead of nudging — buzz log confirms no `_SKIPPED` entries exist anywhere, meaning the running daemon predates #271; (b) #257 (2026.4.22.10) injects `/mcp` into workers whose client-side MCP tool registry is stale after a daemon reload — no `MCP_TOOLS_STALE` entries exist either. Once the operator reloads the dashboard, both fixes activate: #271 drops the nudge (informational-only), #257 detects wifi-portal's dead MCP session + forces a `/mcp` re-init so `swarm_check_messages` actually reaches the server and marks the 4 messages read. No drone code change required — the drone is reading from the right source. 8 new tests in `tests/test_unread_count_single_source.py` pin the zero-drift invariant permanently so any future refactor that introduces a denormalized unread counter or a dual query path gets caught: empty inbox agrees, 4 action-required agree, 4 informational agree, broadcast+direct agree, `mark_read` propagates to the drone view, queen-sourced also agrees, and structural assertions confirm both code paths literally import and call `MessageStore.get_unread`. Full suite: 3,952 passes.

### Fixes

## [2026.4.24.2] - 2026-04-24

### Features

### Changes
- **InterWorkerMessageWatcher narrowed to action-required message types (task #271).** Live repro 2026-04-24: wifi-portal was working a task and had self-resolved whatever dependency public-website's FYI message was about. The drone nudged anyway — "4 new messages, run swarm_check_messages" — risking derailing the worker mid-task. Same failure class as the hub #256 incident (Queen redirected a worker mid-plan) but at the drone layer. Fix: a new `_ACTION_REQUIRED_MSG_TYPES = {"dependency", "warning"}` gate in `src/swarm/drones/inter_worker_watcher.py`. Only unread messages of those types trigger a nudge; informational types (`finding`, `status`, `note`) no longer pull a worker off current work. When an inbox has only informational messages, the watcher writes an `AUTO_NUDGE_MESSAGE_SKIPPED` buzz entry (new `DroneAction`/`SystemAction` enum value) naming the sender + type summary so the operator has telemetry on the suppression. The skip entry is debounced per worker on the same window as regular nudges so the buzz log doesn't spam every sweep while the informational inbox sits unread. Mixed inboxes (at least one action-required message present) still nudge; the nudge wording surfaces the full unread count so the worker sees the informational backlog too. Queen-sourced messages remain excluded (her #235 Phase 1 relay already covers them). 7 new tests in `tests/test_inter_worker_watcher.py` pin: `finding` alone skips (the wifi-portal repro), `status` alone skips, `note` alone skips, `dependency` still nudges, `warning` still nudges, mixed inbox nudges on action-required while the count reflects total unread, and the SKIPPED entry is debounced. Existing 11 tests updated: the `_message` fixture defaults `msg_type="dependency"` so nudge-fires tests still pass. Full suite: 3,944 passes.

### Fixes

## [2026.4.24] - 2026-04-24

### Features
- **`swarm_draft_email` MCP tool — workers can create Outlook Drafts via the Graph integration.** Previously only the completion-reply auto-draft path used the Graph integration; that fires when an email-sourced task is completed and drops a reply in-thread. This adds the symmetric worker-initiated path: a worker can call `swarm_draft_email(to=[...], subject, body, cc?, body_type?, reason?)` to create a brand-new draft in the operator's Outlook Drafts folder. Use case: a worker needs the operator to reach out to a stakeholder (e.g. "ask for schema clarification on task #301 before implementing"), so the worker drafts the email + the operator reviews and sends manually from Outlook. **The draft is NEVER auto-sent** — operator must explicitly send from Outlook. New `GraphTokenManager.create_draft(to, subject, body, cc=None, body_type="text")` method in `src/swarm/auth/graph.py` wraps `POST /me/messages` on Graph; returns `{"id": "...", "web_link": "..."}` on success, `None` on failure. Tool handler in `src/swarm/mcp/tools.py` validates inputs (non-empty `to` list, required `subject` + `body`, `body_type ∈ {text, html}`, `cc` list of strings), then fire-and-forget schedules the Graph call as a background asyncio task (keeps `handle_tool_call` synchronous — existing 87-test sync caller surface unaffected). Success / failure writes a `DRAFT_OK` / `DRAFT_FAILED` buzz entry under `LogCategory.SYSTEM` so the dashboard surfaces the outcome without the worker needing to poll. Graph-not-connected / token-expired cases short-circuit with a clear "not connected" message pointing at the config page. 15 new tests in `tests/test_mcp_draft_email.py`: all 6 input-validation branches (missing/empty `to`, non-string entries, missing subject/body, invalid `body_type`), both integration-unavailable paths (`graph_mgr` is None, `is_connected()` returns False), the success round-trip (queued message + Graph call arguments + DRAFT_OK buzz entry + `html` body_type + `cc` list threading), the failure path (`DRAFT_FAILED` buzz entry on `None` return), and a Graph payload-shape pin (`toRecipients`/`ccRecipients`/`body.contentType` all match what Graph expects). README updated: MCP coordination-tool count 11 → 12 with the new tool row. Full suite: 3,937 passes.

### Changes

### Fixes

## [2026.4.23] - 2026-04-23

### Features

### Changes

### Fixes
- **`queen_force_complete_task` spurious `AttributeError` on email-originated tasks (task #270).** Symptom: Queen calls `queen_force_complete_task(number=N, resolution=..., reason=...)`, gets back `Error: '_asyncio.Task' object has no attribute 'assigned_worker'`, but the DB mutation actually landed (next `swarm_task_status` shows the task as `[completed]`, a second force-complete returns `Task ... cannot be modified (completed)`). Root cause: `SwarmDaemon.complete_task` had a local variable `task` bound to the `SwarmTask` at the top of the method, but the email-reply branch further down (`if source_email_id and self.graph_mgr and resolution`) did `task = asyncio.create_task(self._send_completion_reply(...))`, clobbering the local name. The post-ship self-loop added in task #225 Phase 3 (`self._auto_start_next_assigned(task.assigned_worker)`) then tried to read `.assigned_worker` off the `asyncio.Task`. Two consecutive nexus force-completes (tasks #266, #268, both with email sources) hit this in a single session. Fix: rename the local to `reply_bg` so it doesn't shadow the SwarmTask. Two-line change in `src/swarm/server/daemon.py`. Regression test `test_complete_task_email_path_does_not_clobber_task_variable` pins the exact path: assigned task with `source_email_id` + `graph_mgr` set, monkeypatched `_send_completion_reply` + `_auto_start_next_assigned`, asserts the captured worker_name is the original SwarmTask's `assigned_worker` rather than raising. Verified the test catches the pre-fix bug via temporary revert (reproduces the exact reported `AttributeError`). Full suite: 3922 passes.

## [2026.4.22.11] - 2026-04-22

### Features
- **`swarm queen contribute-claude-md` — local → shipped reverse sync (task #258).** Companion to #254's forward reconcile. Where `reconcile_queen_claude_md` pushes the shipped `QUEEN_SYSTEM_PROMPT` into the local `~/.swarm/queen/workdir/CLAUDE.md` on daemon start, this new flow pushes local edits back to the shipped constant. Before this, local improvements (Queen policy authored during operator corrections) accumulated on individual installs with no upstream path — one-off human curation only (the "Two Queens" section in #251, etc.). New module `src/swarm/queen/contribute.py` with: `compute_status()` (diff local vs shipped, return `ContributeStatus` with hunk count + unified diff), `emit_patch()` (produce a `git apply`-able unified diff targeting `src/swarm/queen/runtime.py` by rewriting the in-file `QUEEN_SYSTEM_PROMPT` triple-quoted literal; `_locate_constant_span` + `_rewrite_runtime_source` handle the surgery), `open_pr()` (full gh flow: new branch + rewrite + commit + push + `gh pr create` with graceful failure when `gh` isn't available or the worktree is dirty), `mark_synced()` (update `.claude_md_shipped` post-merge so #254's reconcile doesn't re-flag the same content), and `detect_repo_root()` (looks for the swarm checkout). New CLI subcommand `swarm queen contribute-claude-md [--emit-patch PATH | --open-pr | --mark-synced] [--repo-root DIR]`: no flags = status-only (diff summary, no writes); flags are mutually exclusive; auto-detect repo-root falls back to `--repo-root DIR`. Per operator clarification: the Queen is a global role, not operator-specific, so NO local-only marker subsystem was added — every hunk is a promotion candidate. Operator defers by not running the CLI, or strips a hunk from the emitted patch by hand. Integration with #254: the drift-flagged inbox notification now points at the contribute CLI so the Queen knows the mechanism on any future drift event. Port-in-pass: the `Tier-2-includes-redirect` rule the Queen authored locally under "High-confidence auto-actions" was promoted directly into `QUEEN_SYSTEM_PROMPT` as an exercise of the flow. 17 new tests in `tests/test_queen_claude_md_contribute.py`: `compute_status` diff + in-sync paths; constant-span locate + error on missing header; rewrite surgery; `emit_patch` produces git-applyable diff / writes empty on no-op / raises on missing runtime.py; `mark_synced` updates marker / raises without local file / prevents drift-flag on next reconcile; `count_hunks` utility; CLI smoke (help resolves + mutually exclusive flags rejected); and a commit-time guard test that fails if the live `~/.swarm/queen/workdir/CLAUDE.md` has diverged from the shipped constant (skips gracefully in CI/fresh-env). Full suite: 3921 passes.

### Changes

### Fixes

## [2026.4.22.10] - 2026-04-22

### Features

### Changes

### Fixes
- **Worker MCP tools-dropped recovery via IdleWatcher (task #257).** Root-cause for the recurring `rcg-dev-install` pattern ("swarm MCP server disconnected earlier this session — tools aren't available here anymore"): Claude Code's HTTP MCP transport hits its reconnect-retry ceiling during a daemon reload that the worker sits idle through, gives up, and marks the server's tool registry as unavailable client-side. Nothing ever triggers the server-side auto-revive path from #227 because the worker isn't making any new POSTs. The #239 SSE POST-response piggyback also can't help — it only fires on a POST. Worker wakes up with tools gone. **Fix (Option C per task spec)**: IdleWatcher drone detects the state and injects `/mcp` into the worker's PTY to force Claude Code's re-initialize flow. Detection criteria: worker is RESTING/SLEEPING with an active task, has made zero MCP dispatch calls since the daemon booted, and hasn't already had a refresh fired this boot cycle. Wiring: (a) `src/swarm/mcp/server.py` gains a `_worker_last_mcp_activity: dict[str, float]` module-level tracker updated on every `_dispatch()` call + a `get_worker_last_mcp_activity(worker_name)` getter; (b) `src/swarm/server/daemon.py` records `self.daemon_start_time = time.time()` on init (re-stamped on every `os.execv` reload); (c) `IdleWatcher` gains `mcp_activity_lookup` + `daemon_start_time` constructor args; (d) `pilot.set_idle_nudge_sender()` threads both through; (e) the daemon wires the tracker + boot timestamp when calling `set_idle_nudge_sender`. On a detected stale state, the watcher injects `/mcp` via `send_to_worker` and writes a `MCP_TOOLS_STALE` buzz entry under a new `LogCategory.MCP` (new category added alongside DRONE/TASK/QUEEN/etc. for MCP-session events). Each worker gets at most one refresh per boot cycle (`_mcp_refresh_fired` set); failed PTY injects clear the flag so the next sweep can retry. Operator / Queen get dashboard-visible telemetry for any future occurrence — no more diagnosing from screenshots. 9 new tests in `tests/test_mcp_tools_stale_recovery.py` pinning the fire-on-stale / skip-on-recent / at-most-once-per-boot / send-failure-retry / feature-disabled-when-callbacks-missing paths, plus two tests on the server-side activity tracker itself. Full suite: 3904 passes.

## [2026.4.22.9] - 2026-04-22

### Features

### Changes
- **Doc audit sweep — align README + CLAUDE.md + spec index with the post-#250/#253/#254/#255 reality.** Comprehensive in-repo doc audit (via `/audit-docs`) found 32 drift / stale / missing / structural findings across 16 markdown files. All applied. README.md: tool count 9→11 (added `swarm_report_blocker` and `swarm_note_to_queen`); new `swarm queen sync-claude-md` row in CLI reference; new Queen MCP tools subsection (15 tools); removed stale `"Ask Queen"` action-button example (that action was deleted in #253); corrected `queen.system_prompt` description at its second occurrence to name its headless-only scope. CLAUDE.md: fixed stale line-number references (`routes/system.py:201→218`, `daemon.py:2122→2307`); "Three mechanisms"→"Four mechanisms" (worker-reported blockers from #250 was added as the fourth); expanded module inventory to list specialized drones (idle_watcher, inter_worker_watcher, pressure, oversight_handler, state_tracker, task_lifecycle, directives, decision_executor, coordination, poll_dispatcher), the second Queen module (runtime.py, oversight.py, queue.py, context.py), the blockers store, and the full MCP tool split (11 worker + 15 Queen). docs/multi-llm-providers.md: added SHIPPED banner for Phase 1 (provider extraction refactor) with pointer to `src/swarm/providers/`; rewrote §2.1 Worker Startup (hardcoded `["claude","--continue"]` is gone) and §2.2 State Detection (pattern location moved from deleted `worker/state.py` to `providers/claude.py` + `drones/state_tracker.py`). docs/claude-code-roadmap.md: added "last-reviewed 2026-04-16" note + pointer to CHANGELOG for post-roadmap shipping (#248, #250, #251, #253, #254). Gitignored spec directory: also synced (local only, not committed) — `interactive-queen.md` status `READY_TO_BUILD`→`shipped`; `phase4-mcp-messaging.md` gained post-Phase-4 extensions table for the new tools; `sqlite-unified-storage.md` added full v6 schema (queen_threads/messages/learnings + `proposals.thread_id`) and v7 schema (worker_blockers); `headless-queen-architecture.md` gained YAML frontmatter; two new retrospective specs (`worker-blockers.md`, `pressure-threshold-tuning.md`) cover features that shipped without design docs. No source code touched.

### Fixes

## [2026.4.22.8] - 2026-04-22

### Features

### Changes
- **Fresh-install Queen onboarding audit + regression tests + README refresh (task #255).** Audit of the install path from "user runs swarm init" through "first daemon boot completes" to verify Queen setup lands correctly on a brand-new install. Findings: **(1) Runtime path is clean** — `reconcile_queen_claude_md()` handles the missing-parent-dir case via `mkdir(parents=True, exist_ok=True)`, `auto_migrate()` creates all 21 Queen-critical tables on a non-existent DB idempotently, `QueenConfig()` defaults are sane (enabled=True, empty system_prompt that the daemon seeds from `HEADLESS_DECISION_PROMPT`), `HiveConfig()` always includes `.queen`. `swarm queen sync-claude-md` resolves via the CLI subcommand group. Queen is a synthetic worker (never persisted to `workers` DB table) — by design, re-created per daemon boot. **(2) README had stale content** — fixed. Removed the `Alt+Q | Ask Queen` keyboard shortcut from the shortcuts table (that binding was deleted in task #253 when the Ask Queen UI was removed). Rewrote "Queen & Proposals" section to cover the two-Queens architecture (interactive PTY coordinator vs headless subprocess decision function), with specific mentions of: how to reach the interactive Queen (click her worker tile), what `~/.swarm/queen/workdir/CLAUDE.md` is and that the operator can edit it, the drift-detection / reconcile mechanism from #254, and the `swarm queen sync-claude-md` CLI flags. Corrected the `queen.system_prompt` config description — it's the headless-decision prompt only after #253, not a global Queen prompt (the interactive Queen's role lives in her CLAUDE.md). Added 9 regression tests in `tests/test_fresh_install_queen.py` covering: workdir creation when parent dir missing, CLAUDE.md seeded with expected role markers, marker equals shipped constant at seed time, all Queen-critical tables present after `auto_migrate` on a fresh DB, migrate idempotency, `QueenConfig` + `HiveConfig` defaults, headless-decision seed fires on empty, and a full end-to-end boot sequence (DB migrate → config seed → reconcile) asserting SEEDED action + workdir layout. Full suite: 3,895 passes.

### Fixes

## [2026.4.22.7] - 2026-04-22

### Features
- **Queen CLAUDE.md sync across swarm updates (task #254).** Problem: `~/.swarm/queen/workdir/CLAUDE.md` and the shipped `QUEEN_SYSTEM_PROMPT` constant in `src/swarm/queen/runtime.py` drift every release. The daemon preserves operator / Queen edits (good) but silently misses shipped content updates (bad) — existing installs age without the operator ever knowing. Fix: three-state reconciliation on every daemon startup. `reconcile_queen_claude_md()` compares **SHIPPED_LATEST** (current constant) vs **SHIPPED_AT_LAST_SYNC** (reference copy at `workdir/.claude_md_shipped`) vs **ON_DISK** (the live CLAUDE.md). Decision matrix: (a) shipped unchanged → no-op regardless of local edits; (b) shipped changed, on-disk clean → auto-update; (c) shipped changed, on-disk has local edits → drift-flagged: write side-by-side reference files `CLAUDE.md.shipped-latest` and `CLAUDE.md.shipped-last`, log warning, send a `finding` message to the Queen's inbox via `MessageStore` (triggers the #235 auto-relay so she surfaces it to operator next turn), emit `STATE_TRANSITION` buzz entry so the dashboard shows it; (d) first upgrade against pre-existing CLAUDE.md with no marker → seed marker from current on-disk baseline (treat current state as the reference point). New CLI: `swarm queen sync-claude-md` without flags shows three-way status; `--accept-shipped` overwrites on-disk with current constant + updates marker + clears drift refs; `--keep-local` updates marker only (acknowledge drift, preserve local edits) + clears drift refs. Mutually exclusive. Module-level constants `CLAUDE_MD_FILENAME`, `SHIPPED_MARKER_FILENAME`, `DRIFT_SHIPPED_LATEST_SUFFIX`, `DRIFT_SHIPPED_LAST_SUFFIX`, `ReconcileAction` exposed for test + CLI reuse. Daemon startup calls `reconcile_queen_claude_md(QUEEN_WORK_DIR)` unconditionally before Queen spawn so existing-Queen reloads also pick up new shipped content (not just fresh spawns); `_handle_queen_claude_md_reconcile` dispatches by action. Also synced `QUEEN_SYSTEM_PROMPT` with the Queen-authored "Two Queens: interactive and headless" section from her on-disk edits so shipping this release doesn't immediately trigger the auto-update path and erase her work. 12 new tests in `tests/test_queen_claude_md_reconcile.py` covering all four matrix cells + first-upgrade + idempotency + full lifecycle + CLI mode errors. Full suite: 3886 passes.

### Changes

### Fixes

## [2026.4.22.6] - 2026-04-22

### Features

### Changes

### Fixes
- **Pressure-suspend no longer trips on sticky swap with healthy memory.** Live incident 2026-04-22: 10 workers suspended on a dev machine sitting at `mem=62.7%, swap=60.7%` with no real memory pressure. Root cause was two-fold: (1) the swap-triggered HIGH branch in `classify_pressure` used hardcoded `mem_pct >= 60` and `mem_pct >= 70` guards, so any memory usage above 60% combined with >50% swap (the default `high_swap_pct`) would suspend workers, ignoring the fact that swap is "sticky" in Linux — once cold pages are paged out they stay there until explicit swap-off or a reboot even when RAM is abundant; (2) `high_swap_pct=50` was too tight for a dev machine that has swap enabled. Two coordinated fixes: **(a)** the inner memory guards in `classify_pressure` are now derived from the configured memory thresholds rather than hardcoded — HIGH requires `mem >= elevated_mem_pct` (default 80) alongside `swap >= high_swap_pct`; CRITICAL requires `mem >= high_mem_pct` (default 90) alongside `swap >= critical_swap_pct`. Tuning one pair pushes the coupling in sync. **(b)** Swap threshold defaults bumped to match reality: `elevated_swap_pct` 25→40, `high_swap_pct` 50→70, `critical_swap_pct` 75→85. Memory thresholds unchanged (80 / 90 / 95). Net effect on the reported state: mem=62%/swap=60% now classifies as ELEVATED (informational, no suspend) instead of HIGH (suspend). Genuine pressure (mem >= 80% AND swap >= 70%) still triggers HIGH. Three tests updated and one new regression test pinned (`test_swap_sticky_does_not_suspend`) to the exact observed dev-machine state. Defaults in `ResourceConfig` (`src/swarm/config/models.py`), the loader fallbacks (`src/swarm/config/loader.py`), and the `classify_pressure` / `take_snapshot` signatures all updated in sync so fresh installs get the new behavior. Existing deployments with `config.resources.*` values in swarm.yaml or DB keep their overrides — this only moves the defaults. Full suite: 3874 passes.

## [2026.4.22.5] - 2026-04-22

### Features

### Changes
- **Headless Queen architecture close-out (task #253 follow-up).** Three coordinated changes that locked in the "keep the headless Queen, don't route to interactive" decision from the `/interview` session summarized in `docs/specs/headless-queen-architecture.md`. **(A) High-confidence-not-done backoff** in `src/swarm/drones/task_lifecycle.py`: when Queen returns `done=False` with `confidence >= 0.8` on a completion analysis, the per-task re-propose cooldown extends from 5 min to 30 min (`_HIGH_CONF_NOT_DONE_BACKOFF = 1800`). New callback chain `analyzer.analyze_completion` → `daemon._record_completion_verdict` → `pilot.record_completion_verdict` → `TaskLifecycle.record_completion_verdict` feeds the verdict back. `done=True` clears the entry so completion proposals proceed. Projected savings from audit data: ~1,021 redundant LLM calls / 30d eliminated (34/day), top offender workers were getting 96-162 Queen completion calls on a single task across a 30-day window because the drone kept re-asking on unchanged PTY state. **(B) Periodic hive-coordination caller deleted**: `Queen.coordinate_hive`, `QueenAnalyzer.coordinate`, `EscalationHandler.coordinate_hive`, `DronePilot._coordination_cycle`, `CoordinationHandler.coordination_cycle`/`_process_coordination_result`/`_coordination_snapshot_unchanged`, daemon's `coordinate_hive` delegate, the `POST /api/queen/coordinate` route, and `_COORDINATION_INTERVAL` from `poll_dispatcher.py` — all removed. Coverage was duplicated by specialized drones (IdleWatcher, InterWorkerMessageWatcher, FileOwnership, PressureManager). `CoordinationHandler.capture_worker_outputs` preserved under the same import path since the DirectiveExecutor pipeline still depends on it. **(C) CLAUDE.md gained a "Two Queens: division of labor" section** naming the interactive Queen's conversational role vs the headless Queen's stateless-decision role, the division of labor for future callers (operator-facing → interactive, drone-driven + high-volume → headless), and a pointer to `docs/specs/headless-queen-architecture.md` so the "should we collapse these?" question doesn't recur. Also added the pressure-test heuristic: new "should we add a Queen call?" requests check deterministic drone rules first. 32 coordination-cycle tests removed across `test_pilot.py`, `test_queen.py`, `test_daemon.py`, `test_api.py`, `test_analyzer.py` (all dependent on deleted surface); 2 capture-output tests rewritten to exercise `CoordinationHandler.capture_worker_outputs` directly instead of through the deleted cycle wrapper; 8 new `TestTaskCompletionReproposal` tests pin the high-conf backoff, low-conf passthrough, `done=True` clear, and backoff-expiry paths. `docs/specs/headless-queen-architecture.md` documents the full audit + interview + decision for posterity. Full suite: 3,873 passes.

### Fixes

## [2026.4.22.4] - 2026-04-22

### Features

### Changes
- **Delete redundant "Ask Queen" dashboard UI; repopulate headless-decision prompt (task #253).** Task #252's audit documented that the legacy `swarm.queen.queen` headless path is load-bearing for four programmatic callers (drone auto-assign in `task_lifecycle.py`, oversight monitor in `queen/oversight.py`, hive coordination in `drones/coordination.py`, `QueenAnalyzer.analyze_worker` in `server/analyzer.py`) PLUS a redundant dashboard UI surface. Operator decision: keep the programmatic paths, delete the UI — the interactive Queen's worker tile is the single entry point for operator→Queen conversation. This commit removes: (a) the three `/action/ask-queen*` routes (`src/swarm/web/routes/queen.py` deleted entirely, its `register(app)` pulled from `web/routes/__init__.py`, re-exports removed from `web/app.py`); (b) the Ask Queen header button + mobile menu entry in `dashboard.html`; (c) the ask-query footer inside the queen-modal (question input + Ask/Re-analyze/Apply buttons) — the modal itself stays for proposal/escalation display; (d) the `askQueen` / `askQueenWorker` / `askQueenQuestion` / `applyDirectives` / `applyDirective` / `_execDirective` / `renderQueenResult` / `startQueenCooldown` JS functions (~280 LOC), plus the `lastDirectives` state, the `q` keyboard shortcut, the worker context-menu `queen` case, the `doAction('queen')` branch, and the action-button dropdown's 'Ask Queen' default + 'queen' action entry in `config.html`; (e) the misleading `system_prompt` textarea in the config page (load + save wiring). Post-#251 `config.queen.system_prompt` was cleared, which would have degraded the four programmatic callers to running with no role framing. This commit also adds `HEADLESS_DECISION_PROMPT` as a module constant in `src/swarm/queen/queen.py` — a tight, stateless decision prompt covering the six invocation shapes (task auto-assignment, oversight, completion evaluation, escalation response, hive coordination, prolonged-BUZZING analysis) with decision rules (>=0.85 act, <0.6 wait, destructive→wait unless durably authorized, no cross-worker file overlap) and evidence order (PTY tail > buzz log > messages > learnings, with learnings always taking primacy). Anchored back to the interactive Queen's `~/.swarm/queen/workdir/CLAUDE.md` for policy consistency. The daemon's `__init__` seeds the constant into `config.queen.system_prompt` when the field is empty — covers fresh installs and the post-#251 cleared deployment without a schema migration. Operator override still wins: any non-empty value in swarm.yaml or the DB bypasses the seed. Live DB also repopulated with the new prompt (one-shot SQL) so this deployment picks it up immediately. Six new tests in `tests/test_headless_decision_prompt.py` pin constant presence, required role markers, absence of stale UI references, empty→seed, override→preserve, and default-config→seed behavior. `src/swarm/config/models.py` docstring on `QueenConfig.system_prompt` rewritten: accurately describes it as the headless-decision prompt scope (drone auto-assign / oversight / hive coordination / analyzer) rather than the interactive Queen's role. Full suite: 3903 passes.

### Fixes

## [2026.4.22.3] - 2026-04-22

### Features

### Changes
- **Queen system prompt migrated from DB → `~/.swarm/queen/workdir/CLAUDE.md` (task #251).** The Queen has been running interactively for some time, but `config.queen.system_prompt` in swarm.db still held the old headless-mode prompt from the pre-interactive era — RCG-specific worker names that no longer exist, proposals-require-approval language, "set confidence to 0.0 for plans", "use assign_task not send_message" (both obsolete — Queen now writes via `queen_prompt_worker` / `queen_reassign_task` / etc). The interactive Queen already reads her role from `~/.swarm/queen/workdir/CLAUDE.md`, seeded on first spawn from `QUEEN_SYSTEM_PROMPT` in `swarm.queen.runtime`. This task (a) cleared `config.queen.system_prompt` on this deployment (empty string — idiomatic given the field's empty default + the serializer's omit-when-empty behavior), (b) sent the verbatim old prompt to the Queen's inbox for archival, (c) let the Queen author the interactive-mode CLAUDE.md replacement herself (operator + Queen collaboration — Queen is the subject matter expert on how she operates), (d) synced the refreshed CLAUDE.md content back into the `QUEEN_SYSTEM_PROMPT` module constant so new swarm installs get the same first-pass prompt, (e) added a deprecation note on `QueenConfig.system_prompt` pointing future readers at CLAUDE.md. The field is still read by the legacy headless `claude -p` coordinator path in `swarm.queen.queen` for backward compat — new deployments should leave it empty. The refreshed prompt adds: "Your jurisdiction (don't delegate these)" section listing Queen-owned content (CLAUDE.md, learnings, threads, synthesis memos, Queen-affecting proposals) vs worker jurisdiction (code, shells, tests, DB schema lookups); full Read+Write tool catalogue including the elevated write tools (`queen_prompt_worker`, `queen_reassign_task`, `queen_force_complete_task`, `queen_interrupt_worker`, `queen_save_learning`); inbox auto-push guidance naming the `full=true` flag for verbatim relay; drone-driven routine nudges paragraph distinguishing exception-handling from duplication; `swarm_report_blocker` usage note (task #250 integration); a "Drafting for non-technical staff" voice subsection preserving the email-reply guidance salvaged from the old prompt. Full suite: 3897 passes.

### Fixes

## [2026.4.22.2] - 2026-04-22

### Features
- **`swarm_report_blocker` MCP tool + IdleWatcher skip-on-blocker path (task #250).** Closes the loudest recurring operator-pain pattern from this session: admin has #246 (which is blocked on platform's #245), the operator knows it, admin knows it — and every 3 minutes the IdleWatcher nudges admin anyway with "You have #246 active but appear idle…" because the watcher has no way to distinguish "idle because stuck" from "idle because waiting on a dependency that hasn't shipped". New `swarm_report_blocker(task_number, blocked_by_task, reason)` tool lets a worker persist that declaration. Storage: new `worker_blockers` table (schema v7 migration) keyed on `(worker, task_number)` with `INSERT OR REPLACE` semantics so re-reports refresh the `created_at` — the refresh matters for the message-based auto-clear described below. New `BlockerStore` in `src/swarm/tasks/blockers.py` wraps the table with `report` / `list_for_worker` / `clear` / `has_active_blocker` APIs, sharing the `SwarmDB` connection + lock so writes serialize alongside tasks/messages/buzz. IdleWatcher gains two constructor args (`blocker_store`, `message_has_newer`) and a pre-nudge check: if `has_active_blocker(worker)` returns a live blocker, the sweep skips the nudge and writes an `AUTO_NUDGE_SKIPPED` buzz entry naming both tasks (`reported blocker on #246 (waiting on #245)`). Two auto-clear triggers purge the row without a second MCP call — (a) the `blocked_by_task` flips to `completed` on the task board, (b) a new message lands in the worker's inbox after the blocker was declared (operator-authored "something else changed, check your inbox" escape hatch). Daemon wires `message_store.get_unread()` into `message_has_newer` so option (b) works out of the box. 17 new tests across `tests/test_blockers.py` (persistence, both auto-clear paths, refreshed-timestamp path, multi-active-task guard) and `tests/test_mcp_tools.py::TestReportBlocker` (schema validation + handler). Full suite: 3897 passes. CLAUDE.md's "Autonomous task momentum" section gained bullet #4 documenting the new tool and when workers should call it.
- **`swarm_note_to_queen` MCP tool for side-channel Queen notes (task #248).** Extends #235's inbox-relay mechanism to cover the failure mode where a worker addresses the Queen through PTY side-channel text — pre-response reminders, inline coordination questions, "FYI queen" annotations — that never went through `swarm_send_message`. Live repro 2026-04-22: project-root wrote "Reminder: should I /clear before this dispatch run?" in their own PTY before sending a coordination memo; the Queen missed the reminder until the operator screenshotted it. New tool persists the note in the message store (new `note` msg_type added to `_VALID_MSG_TYPES`) and fires the same `_auto_relay_to_queen` path the formal-message handler uses, so the Queen's next turn sees it naturally. Self-notes (queen → queen) short-circuit to avoid PTY self-loop. Workers calling the tool log an `OPERATOR` buzz entry with an `→ queen (note): ...` prefix so the audit trail disambiguates notes from findings/warnings. Three new tests in `tests/test_mcp_tools.py::TestNoteToQueen` pin the persist + relay path, the missing-content guard, and the self-loop guard. CLAUDE.md's "Queen message-surface elevation" section names the new tool alongside the existing inbox-relay path. Full suite: 3880 passes.

### Changes

### Fixes

## [2026.4.22] - 2026-04-22

### Fixes
- **MCP auto-revive POST now responds as SSE with list_changed piggyback (task #239).** Closes the last propagation gap in the chain of #226 → #227 → #237. `broadcast_tools_list_changed()` delivered to `_broadcast_subscribers`, which only holds clients with an open `GET /mcp` stream. Claude Code's HTTP MCP transport doesn't maintain one — it opens GET briefly around `initialize` and closes it. So the broadcast had no audience for the common case, and every swarm iteration cycle required a manual Claude Code bounce for the Queen + workers to see schema changes (observed 4+ times this session across #195, #198, #225, #237). Fix: when the POST handler auto-revives a session (task #227 path — stale `Mcp-Session-Id` from a pre-reload daemon), it now returns `text/event-stream` carrying the `tools/list_changed` notification FIRST, then the JSON-RPC response. Per MCP Streamable HTTP spec §7 a POST response MAY be an SSE stream with multiple messages. Clients that can't receive out-of-band notifications still get the re-enumerate nudge bundled with their response. Known-session POSTs keep returning plain JSON — only auto-revive sessions (where we know the schema is likely stale) pay the SSE path. Also added diagnostic logging on every `_push_tools_list_changed` call: `[mcp] list_changed_sent session=<id> transport=<sse-get|http-post-piggyback>` for future gap debugging without guesswork. Two new tests in `tests/test_mcp_server.py` — one pinning the SSE response shape (both events in order + new session header), one pinning that known-session POSTs stay JSON. CLAUDE.md's "Live MCP tool-surface propagation" section now names the piggyback as a fourth mechanism alongside initialize advertisement, on-connect push, and broadcast. Full suite: 3877 passes.
- **`queen_view_messages` + `queen_view_message_stream` gain `full=true` for verbatim relay (task #237).** Direct follow-up to #235: the auto-relay prompt fired into the Queen's PTY on inbound messages points her at `queen_view_messages worker=queen` for the full content, but that tool truncated each body at 160 characters for list-view ergonomics. Operator repro on 2026-04-21: project-root sent the Queen a 2 kB decision memo (Option A / Option B / recommendation) and the Queen couldn't read past the Option A header via the view tool. Added a `full` boolean to both tools' input schema (default false) — when true, returns the complete message body and separates multi-row results with `\n\n---\n\n` so boundaries are unambiguous. Default preview behaviour unchanged. `_handle_view_message_stream` grew past the complexity cap as a side effect, so the row-formatting loop was extracted to `_render_message_stream_rows` + `_message_stream_worker_states` helpers. Two new tests pin that default is still truncated and `full=true` returns the complete body for both tools; CLAUDE.md's "Queen message-surface elevation" section names the new flag. Full suite: 3875 passes.
- **Pressure oscillation dampening + measured-value logging + stuck-BUZZING safety net (task #236).** Three coordinated fixes around the hub + realtruth observation: 10–13 rapid SUSPENDED/RESUMED cycles during a single npm install + deploy turn, followed by both workers wedged in BUZZING for 97–113 minutes after actual work ended. (1) **Hysteresis in `PressureManager.on_pressure_changed`.** New `_HYSTERESIS_SECONDS = 30.0` constant and `_last_resume_at` timestamp suppress re-entry into HIGH/CRITICAL for 30 s after any RESUME. Memory-pressure jitter around a threshold boundary no longer produces 10+ SUSPEND/RESUME cycles per turn. The `_last_resume_at` is primed even when a HIGH pressure wave found no SLEEPING workers to suspend, so the next-tick HIGH is still debounced. (2) **Measured mem/swap values in SUSPEND/RESUMED buzz entries.** `on_pressure_changed` now accepts `mem_pct` / `swap_pct` kwargs threaded from `ResourceMonitor`; `_suspend_workers` and `_resume_pressure_suspended` append them to the log detail (e.g. `pressure HIGH (mem=92% swap=55%)`). Future tuning has concrete data alongside each event. (3) **Stuck-BUZZING safety net in the state tracker.** New `_STUCK_BUZZING_THRESHOLD = 600 s` guard plus `_has_active_turn_signal()` helper — if the classifier calls BUZZING, the worker has been BUZZING for 10+ minutes, AND the narrow PTY tail has NONE of the active-turn signals (esc-to-interrupt, monitor, subagent spinner), force the classification back to RESTING. Catches the stuck-BUZZING mode where stale scrollback patterns (recently-completed subagent `↓ N tokens` lines) keep matching the wide-tail regex even though the worker is idle at the ❯ prompt. The narrow-tail check deliberately rejects stale-scrollback false positives. 9 new tests: 3 hysteresis + measured-value pressure tests, 5 stuck-BUZZING safety-net tests, one threshold-floor guard. Full suite: 3873 passes. Companion to #233 (inverse fix direction; fingerprint-cache race was RESTING-while-BUZZING, this is BUZZING-while-RESTING). Diagnostic-log note from the task description (the STATE_TRANSITION entries from #233 didn't appear in the operator's buzz log) is addressed only by the fact that this release needs a daemon reload — #233's logging was already shipped in 9966305 but hadn't been picked up by the running daemon at observation time.

### Features
- **Queen message auto-pickup + inter-worker nudge drone (task #235).** Three coordinated gaps filled around message-driven coordination. **Phase 1 — Queen inbox auto-relay.** Every `swarm_send_message(to="queen", ...)` (direct or `*` broadcast that includes the Queen) now fires a short PTY notification into the Queen's terminal via `send_to_worker`, so her next conversation turn processes the reply naturally. Self-messages (queen → queen) and worker-to-worker messages do NOT auto-relay — that bypass is intentionally Queen-only to preserve the "workers cannot auto-interrupt each other" hierarchy. Every relay logs as `INBOX_AUTO_RELAY` under `LogCategory.MESSAGE`. **Phase 2 — `queen_view_message_stream` MCP tool.** New Queen-only tool that joins recent messages against each recipient's current worker state. `actionable_only=true` narrows to unread messages whose recipient is idle (RESTING / SLEEPING / STUNG) — the subset the Queen needs to act on. Paired with the raw `queen_view_messages` tool. **Phase 3 — `InterWorkerMessageWatcher` drone.** New drone at `src/swarm/drones/inter_worker_watcher.py` mirroring the `IdleWatcher` pattern from #225. Periodic sweep (reuses `DroneConfig.idle_nudge_interval_seconds` / `idle_nudge_debounce_seconds`, defaults 180 s / 900 s) nudges RESTING / SLEEPING recipients of unread inter-worker messages via a server-side PTY inject; the injector is debounced per recipient and respects the rate-limit callback. Queen-sourced messages are skipped to avoid double-nudging (Phase 1 already covers those). Every nudge logs as `AUTO_NUDGE_MESSAGE` under `LogCategory.DRONE`. Acceptance #4 preserved: workers still cannot prompt each other directly via `swarm_send_message` — the auto-injection is a drone/server concern, never a worker privilege. 18 new tests across `tests/test_mcp_tools.py` (Phase 1 + Phase 2) and `tests/test_inter_worker_watcher.py` (Phase 3). Full suite: 3864 passes. CLAUDE.md gained a "Queen message-surface elevation" section documenting the three elevated privileges and the "workers cannot auto-interrupt" boundary.

### Changes

### Fixes
- **State tracker: pressure RESUME now clears fingerprints; STATE_TRANSITION buzz log (task #233).** Two-part fix for the "worker shows RESTING while demonstrably mid-turn" dashboard bug. (1) `PressureManager._resume_pressure_suspended()` now routes through `state_tracker.wake_worker()` via a new callback instead of discarding from the suspended set directly — this clears the content-fingerprint cache too. Without the clear, a worker whose PTY state changed during suspension (e.g. idle → running a Bash tool) kept its pre-suspend fingerprint, the RESTING short-circuit in `_poll_single_worker` kept short-circuiting, and the worker stayed tagged RESTING in the operator dashboard for the whole turn. (2) Every state transition now writes a `STATE_TRANSITION` buzz entry (new `SystemAction` enum value) with metadata: `from`, `to`, `esc_to_interrupt` (was the indicator present in the PTY tail?), `pty_delta_bytes`, `unchanged_streak`, `suspended`. Future mis-classifications leave a diagnostic trail instead of requiring a live operator to catch them. Three new tests: pressure resume routes through `wake_worker` callback, legacy fallback still empties the suspended set, and `_handle_state_change` emits the STATE_TRANSITION entry with the expected metadata shape. Full suite: 3846 passes.

## [2026.4.21.3] - 2026-04-21

### Features

### Changes

### Fixes
- **Holder backpressure threshold raised to 8 MB — root cause of the long-standing "terminal locks after reload, needs 2-3 reloads" bug.** Traced via `[term-trace]` logs collected across several reload events: every post-reload log ended with `dropping slow client (buffer 1178874 bytes)` from `swarm.pty.holder`, followed by 2+ minutes of zero PTY output across every worker (despite all of them being RESTING with live Claude Code sessions). The chain: (1) daemon reloads, new daemon connects to the holder, (2) `ProcessPool.discover()` fires `_send_cmd("snapshot", worker=X)` per worker, (3) holder writes the ~1.3 MB reply (1 MB raw ring buffer × ~1.33 base64 overhead) into the client socket buffer, (4) while the reply is still draining, `_broadcast` fires on a PTY readable event and writes more bytes into the SAME pending buffer, (5) `get_write_buffer_size()` returns ~1.18 MB, exceeds the old `_MAX_WRITE_BUFFER = 1 MB` threshold, and the holder drops the daemon as a "slow client". The daemon's UNIX socket to the holder is killed, no more live PTY output reaches the daemon, every worker's ring buffer freezes at the snapshot — dashboard terminals appear locked and the state tracker classifies every worker as RESTING because the stale content looks idle. The threshold is now 8 MB (6x headroom over a single snapshot reply while still catching genuinely stuck clients; tens of seconds of backlog at typical PTY output rates). Two new tests in `tests/test_holder.py` pin the positive path (1.5 MB mid-drain buffer ≠ slow client) and the negative (8 MB+ still drops). Full suite: 3843 passes.
- **MCP session auto-revive on unknown `Mcp-Session-Id` (task #227).** Replaces the 404-on-unknown-session behaviour shipped in the previous release. The 404 path was spec-correct per MCP Streamable HTTP §8.4 but broke Claude Code in the wild: its HTTP MCP transport didn't recover from 404 — it just kept re-sending the dead session ID, every tool call failed, and the Queen plus all workers went fully isolated after a daemon reload. The handler now auto-revives instead: when a POST arrives with a non-empty `Mcp-Session-Id` the new daemon process doesn't recognise, the server mints a new session ID on the fly, binds the incoming request to it, processes the original call, returns the new ID in the response header, and pushes `tools/list_changed` to any open `GET /mcp` stream so cached tool schemas get refreshed. `initialize` still issues its own fresh session; session-less clients (no header) still pass through unchanged; `DELETE /mcp` still terminates, but a follow-up on the terminated ID is now auto-revived rather than rejected. The server self-heals regardless of whether the client honours reconnect contracts. Seven tests in `tests/test_mcp_server.py` pin the positive path, reuse-after-revive, initialize-with-stale-ID, missing-header passthrough, DELETE-then-revive, and the auto-revive → `tools/list_changed` push. Full suite: 3841 passes. CLAUDE.md's "Live MCP tool-surface propagation" section rewritten to document auto-revive and explicitly call out why the earlier 404-based and listChanged-based attempts missed.
- **MCP session-ID invalidation on daemon reload — the load-bearing fix for stale tool schemas.** Third attempt at making MCP tool-surface changes propagate to running workers. The previous two attempts (advertising `capabilities.tools.listChanged: true` on initialize, and pushing `tools/list_changed` on SSE connect / to active subscribers) all relied on the client *voluntarily* re-enumerating. They didn't stick because Claude Code's HTTP MCP transport kept reusing its pre-restart `Mcp-Session-Id`, the server happily accepted it (we never validated), and so the client never saw its session break — no break signal, no re-initialize, no fresh `tools/list`. This commit closes that loophole per MCP Streamable HTTP spec §8.4: `handle_streamable_http` now tracks issued session IDs in `_active_session_ids` (wiped automatically on `os.execv`) and returns **404 + `session_not_found`** to any POST carrying an unknown non-empty `Mcp-Session-Id` (except `initialize`, which is always allowed). Per spec, Claude Code MUST then start a new session by sending a fresh `InitializeRequest` — which runs through the existing `listChanged` advertisement + `tools/list_changed` SSE push, triggering a `tools/list` re-fetch. `DELETE /mcp` now correctly deregisters the session. Session-less clients (no `Mcp-Session-Id` header) remain accepted for backward compat. Five new tests in `tests/test_mcp_server.py` cover the positive path, 404 on unknown session, initialize-always-allowed with stale ID, missing-session passthrough, and DELETE → 404. Full suite: 3839 passes. CLAUDE.md's "Live MCP tool-surface propagation" section rewritten to document the real load-bearing mechanism and call out why the earlier attempts missed.

## [2026.4.21.2] - 2026-04-21

### Features
- **Live MCP tool-surface propagation (task #226).** The MCP server now exposes `swarm.mcp.server.broadcast_tools_list_changed()` — an async function that pushes `notifications/tools/list_changed` to every currently-subscribed SSE session, both the Streamable HTTP GET `/mcp` stream and the legacy GET `/mcp/sse` stream. Complements the existing "push on connect" behaviour (unchanged): that covers clients reconnecting after a daemon reload, this covers clients that stayed connected while the tool surface changed. `SwarmDaemon.start()` calls it defensively at startup; future hot-reload-of-tools paths should call it whenever they mutate the registry. Also fixes a latent bug where the streamable SSE handler's request-content iterator would EOF on a body-less GET and exit the handler early; replaced with a transport-disconnect poll so the handler actually stays open for the lifetime of the client's stream. Four new tests in `tests/test_mcp_server.py` cover broadcast-to-open-session, no-op-when-empty, dead-subscriber pruning, and reconnect-after-bounce. CLAUDE.md gained a "Live MCP tool-surface propagation" section pointing future authors at the broadcast API.

### Changes

### Fixes

## [2026.4.21] - 2026-04-21

### Features
- **Autonomous worker momentum (task #225).** Workers no longer park on newly assigned tasks waiting to be polled — Swarm now *pushes* work in three coordinated ways:
  - **Phase 1: task-push dispatch on assignment.** `swarm_create_task(target_worker=X)` routes through `daemon.assign_and_start_task()` by default, which injects the task description straight into X's PTY within one poll cycle. Previously the handler only called `assign_task`, leaving the task queued in ASSIGNED status with nothing dispatching it — that's the root of the recurring "5 workers with hours-old in_progress tasks" operator-pain pattern. New `start: bool` argument on the MCP tool (default `true`) preserves queue-only behavior for Queen/operator staging flows (`start=false`). Self-targeted tasks (caller == target) never dispatch — no interleaving with the caller's own turn.
  - **Phase 2: idle-watcher drone (`drones/idle_watcher.py`).** Periodic sweep (`DroneConfig.idle_nudge_interval_seconds`, default 180 s) nudges RESTING / SLEEPING workers that have an ASSIGNED / IN_PROGRESS task but aren't moving on it. Nudge message points the worker at `swarm_task_status filter=mine` + `swarm_check_messages` so it can self-diagnose rather than treating the nudge as a fresh prompt. Per-(worker, task) debounce (default 900 s) prevents spam; new `AUTO_NUDGE` action in `DroneAction`/`SystemAction` makes every auto-prompt auditable in the buzz log. Rate-limited workers are skipped so we don't stack work behind a dead Claude quota.
  - **Phase 3: post-ship self-loop.** `daemon.complete_task()` now fires `start_task()` for the next ASSIGNED task belonging to the same worker (lowest task number first) as soon as the current one ships. IN_PROGRESS follow-ups are skipped — they're already running somewhere else. Empty queues get no follow-up, per spec ("skip if the worker has nothing else assigned, avoid pointless loops").
  - 19 new tests in `tests/test_idle_watcher.py`, `tests/test_mcp_tools.py::TestCreateTaskAutoDispatch`, and `tests/test_daemon.py` (post-ship auto-start). Full suite: 3828 tests pass. CLAUDE.md gained a new "Autonomous task momentum" section documenting the push semantics for future operators.

### Changes

### Fixes
- **Post-restart terminal reload race — output dropped during discovery window.** When the daemon `os.execv`s (the dashboard Reload button's happy path), `ProcessPool.connect()` starts the holder read loop immediately — but the worker map (`_workers`) is still empty and only gets populated one worker at a time by `discover()`, which does a separate snapshot roundtrip per worker. For the ~1–3 seconds that took, any live PTY output the holder broadcast for a not-yet-discovered worker was silently dropped in `_dispatch_message`. That's the race behind the long-standing "type in the terminal, nothing shows, a second Reload fixes it" bug: the worker's local ring buffer was missing a chunk, which sometimes truncated ANSI escape sequences and left the xterm in a glitched state. The fix buffers unknown-worker output into `_pending_output` and relies on the read loop's serial ordering: any chunks already buffered when the snapshot response resolves are pre-snapshot (already inside the snapshot bytes, dropped to avoid duplication); anything that arrives after resolution routes directly to the now-registered `WorkerProcess.feed_output`. Two new tests in `tests/test_pool.py` lock both paths in. Diagnostic `[term-trace]` logging added in the same session stays put until the reload flow has been stable through several restarts.
- **Operator bypass for the PreToolUse approval hook.** `src/swarm/hooks/approval_hook.sh` now honors a `SWARM_OPERATOR=1` escape hatch alongside the existing `SWARM_MANAGED=1` guard — the PTY holder exports `SWARM_MANAGED=1` for *every* worker it spawns, including sessions the operator is driving interactively, so the old "operator's own session is never gated" invariant was unreachable without a second marker. Operators who want a worker session to bypass drone approval rules (e.g. running `/ship` from an attached worker) now set `export SWARM_OPERATOR=1` in that session and the hook exits early before contacting the daemon. The comment at the top of the script was rewritten to describe this boundary accurately. Pinned by three new tests in `tests/test_approval_hook_script.py` that exercise the shell script against a counting HTTP stub (task #211).

## [2026.4.20] - 2026-04-20

### Features

### Changes

### Fixes

## [2026.4.19] - 2026-04-19

### Features
- **MCP `tools/list_changed` push on SSE connect.** The MCP server now advertises the `tools.listChanged` capability on initialize and, the moment a client opens the streamable SSE stream (GET `/mcp`) or the legacy SSE stream (GET `/mcp/sse`), pushes a `notifications/tools/list_changed` JSON-RPC message. Conformant MCP clients react by re-calling `tools/list`, so schemas cached from a pre-reload daemon no longer linger on the client side. Closes the gap exposed by task #169 — the fix had landed server-side but worker/host sessions kept the stale tool schema in their local cache because nothing told them to refresh. Legacy SSE's required first event (the `endpoint` URL) is preserved; the refresh notification is the second event. Four new integration tests in `tests/test_mcp_server.py` pin the behaviour.

### Changes

### Fixes

## [2026.4.18.3] - 2026-04-18

### Features
- **MCP tool schema-drift indicator.** `src/swarm/mcp/tools.py` hashes itself at import time; `tools_source_drift()` compares the frozen hash against the current file contents. The dev-mode dashboard footer polls `/api/health` every 30s (new `mcp_schema_drift` field) and highlights the Reload button in honey with "Reload needed (MCP tools edited)" status when the source has changed since daemon start. Standalone `GET /api/mcp/schema-drift` endpoint returns the full `{drift, source_path, startup_hash, current_hash}` payload for external tooling. Surfaces the exact scenario that hid task #169's fix in the running daemon until someone noticed the call still used the legacy code path.
- **Reload button on the config page header.** The dashboard footer Reload button is hidden on mobile, so the same dev-reload flow (POST `/api/server/restart`, poll `/api/health` until the daemon comes back, refresh the page) is now reachable from the config page header. Only rendered when `is_dev` is True.

### Changes

### Fixes

## [2026.4.18.2] - 2026-04-18

### Features

### Changes
- **Queen banners de-dup per worker, not per text.** The dashboard's queen/escalation banners now key dedup off a `data-worker` attribute instead of string-comparing `textContent`, so two banners for the same worker with different copy don't pile up. Selecting a worker in the sidebar now also removes any lingering banners tied to that worker — the operator is addressing it directly, the banner no longer adds signal.

### Fixes
- **`swarm_complete_task` silently closed the wrong task when a worker had multiple in_progress assignments (task #169).** The handler walked `task_board.all_tasks` and closed the first match for the calling worker, arbitrarily picking one task and attaching the caller's resolution to it. The MCP tool now takes an optional `number` parameter: singular active task + no `number` keeps the legacy behaviour, multiple active tasks + no `number` errors with the candidate list instead of guessing, and an explicit `number` validates ownership + status before closing. Seven regression tests pin the new contract.
- **Swarm's own MCP tools (`mcp__swarm__*`) could stall behind a PreToolUse permission prompt.** The hook handler (`routes/hooks.py`) now short-circuits to `approve` for any tool name starting with `mcp__swarm__` — these are the daemon's own coordination primitives (`swarm_check_messages`, `swarm_complete_task`, `swarm_task_status`, …) and gating them behind operator approval could leave a worker waiting indefinitely on something that's definitionally safe. Non-swarm MCP tools (e.g. `mcp__stripe__*`) still flow through the normal rules engine.

## [2026.4.18] - 2026-04-18

### Features

### Changes

### Fixes

## [2026.4.17.2] - 2026-04-17

### Features
- **Dashboard "Awaiting your input" pill on worker tiles.** When a worker sits in WAITING state past a 15-second grace window, the tile now shows a pulsing amber pill to make operator-action-required cases visually distinct from a plain WAITING badge. Drives off a new `Worker.needs_operator_input` property exposed via the workers API. Fixes the common confusion where a worker presenting an `AskUserQuestion` prompt looked indistinguishable from a stalled/silent worker.

### Fixes
- **Cross-project task attribution on MCP `swarm_create_task`.** When a worker called `swarm_create_task` with `target_worker=X`, the resulting task row landed in the DB with `source_worker=""` — the calling worker's identity was lost. The handler now calls `edit_task` to record `source_worker` (the calling worker) alongside `target_worker` before assigning, so `is_cross_project` lineage is preserved end-to-end. Self-targeted tasks skip the edit to avoid spurious cross-project flags.

## [2026.4.17] - 2026-04-17

### Features
- **`swarm_batch` MCP tool** — ninth coordination tool; runs multiple `swarm_*` ops sequentially in one round-trip so a worker no longer pays N round-trips for N related calls. Nested `swarm_batch` is rejected to prevent runaway recursion. Each op is still buzz-logged individually.
- **Richer MCP tool descriptions** — every `swarm_*` tool now carries a ≥150-char description with trigger hints ("when to call"), enum semantics (e.g. `finding` vs `warning` vs `dependency` vs `status`), and concrete `examples` in the input schema.
- **`swarm analyze-tools` CLI** — aggregates MCP tool usage from the buzz log (`mcp:*` entries) into per-tool stats: calls, errors, active workers, and up to five distinct error snippets per tool. Supports `--since=7d`, `--json` output, and `--db PATH` for offline DB analysis.
- **Approval-rate gauge** — `SystemLog.approval_rate(since=...)` returns `{approvals, escalations, rate}` from recent decisions; new `GET /api/drones/approval-rate?hours=N` endpoint; dashboard header badge shows the percentage over the last 24h.
- **`DroneDecision.confidence`** — optional float field so future LLM-classifier rules can slot in next to the existing rule-based decisions without a schema change.
- **Compact event telemetry** — every `/compact` logs a `SystemAction.COMPACT` entry under new `LogCategory.COMPACT` with `{tokens_before, tokens_after, ratio, trigger}` metadata. Makes compaction effectiveness measurable per worker and per run.
- **Cron-format pipeline schedules** — pipeline steps now accept full 5-field cron expressions (e.g. `"30 14 * * 1-5"` for weekdays at 14:30). Legacy `HH:MM`, `*:MM`, and `HH:*` still work and are translated to cron internally. Adds `croniter` as a dependency.
- **Skills registry** — SQLite-backed skills table (schema v5 migration, idempotent `CREATE TABLE IF NOT EXISTS`). `SkillsStore` CRUD + usage counters; `attach_skills_store()` seeds built-in defaults (`/fix-and-ship`, `/feature`, `/verify`) on first boot. New `GET /api/skills` endpoint. `get_skill_command()` consults the registry before falling back to the in-memory map and increments `usage_count` on each lookup.
- **`claude_code_security` service handler** — new pipeline AUTOMATED step that runs `claude code security scan --json`, parses the findings array, maps severity to Swarm task priority (`critical→urgent`, `high→high`, `medium→normal`, `low/info→low`), and deduplicates against a persistent state file fingerprinted by `sha256(rule_id\x00path\x00line)`. Supports `severity_filter`, configurable command, and custom dedup state path.
- **Test harness infra pinning** — every `swarm test` run captures an `InfraSnapshot` (model, provider, worker_count, port, claude_home, swarm_version, python_version, platform, env_hash, env_keys) and writes it as the first line of `test-run-{id}.jsonl`. The Markdown report gains an "Infrastructure Snapshot" section above the summary. New `swarm test --pin-model=<id>` flag records the model identifier explicitly, and `compute_env_hash` fingerprints tracked env vars (CLAUDE_MODEL, SWARM_PROVIDER, etc.) via SHA-256 so infra drift is debuggable without leaking secrets.
- **Opt-in Claude Code sandbox** — new `sandbox:` config block on `HiveConfig` (`{enabled, min_claude_version, settings_overrides}`). When enabled, `hooks.install.install()` calls `claude --version`, verifies the installed CC version meets `min_claude_version`, and merges `settings_overrides` into `~/.claude/settings.json["sandbox"]`. Unsupported or missing versions silently stay on the legacy approval flow. Disabled by default; no behaviour change for existing installs.
- **In-app feedback** — report bugs, feature requests, and questions directly from the dashboard footer. Submissions go through the GitHub CLI (`gh`) to bypass URL length limits, with a preview-and-edit step before the issue is filed. Sensitive paths and config values are auto-redacted.
- **Resource monitoring** — memory, swap, and load tracked on a 30s tick; workers auto-suspend on HIGH pressure and the operator is paged on CRITICAL. D-state (wedged process) scanning is optional.
- **Jira integration** — two-way sync with Jira Cloud over OAuth 2.0 (3LO). Import issues as tasks, push status and completion comments back, create Jira issues from the task board.
- **Email integration** — Microsoft Graph (Outlook) integration: drop `.eml`/`.msg` onto the task board, fetch emails from the dashboard, and draft a reply in the Drafts folder when a task completes (never auto-sent).
- **MCP server** — HTTP-based MCP server at `/mcp` (Streamable HTTP + legacy SSE). Workers get 9 coordination tools: `swarm_check_messages`, `swarm_send_message`, `swarm_task_status`, `swarm_create_task`, `swarm_complete_task`, `swarm_report_progress`, `swarm_claim_file`, `swarm_get_learnings`, `swarm_batch`.
- **Inter-worker messages** — typed messages (finding, warning, dependency, status, operator) delivered via MCP; dedup + rate-limit per `(sender, recipient, type)` pair.
- **Pipelines** — multi-step workflows combining AGENT, AUTOMATED, and HUMAN steps with per-step dependencies, templates, and start/pause/resume lifecycle. State persisted in SQLite.
- **Queen oversight** — proactive monitoring: prolonged-buzzing detection and task-drift analysis; interventions classified by severity (minor note, pause+redirect, escalate to operator).
- **File ownership & coordination** — single-branch mode (default) with Queen-managed file ownership map; warning or hard-block on overlap; worktree escape hatch when scopes are unavoidable.
- **Auto-pull sync** — workers auto-pull when another worker commits on the shared branch.
- **Multi-provider support** — Claude Code (production), Gemini CLI and Codex CLI (experimental), plus custom providers via `custom_llms` and per-provider overrides.
- **Cloudflare Tunnel** — one-click remote HTTPS access from the dashboard toolbar; optional named-domain configuration via `tunnel_domain`.
- **Dashboard push notifications** — browser push + desktop notifications + terminal bell; persistent Buzz Log history.
- **Interactive terminal attach** — full xterm.js PTY bridge over WebSocket, up to 20 concurrent sessions.
- **PWA** — installable app with service-worker offline shell and badge API for pending proposals.
- **Config editor in the dashboard** — tabbed UI for workers, groups, drones, Queen, workflows, and integrations; changes apply immediately.
- **Drone log & tuning analytics** — per-rule hit stats and AI-suggested approval rule patterns.
- **Speculation (experimental)** — preparatory read-only work on a queued task while a worker is RESTING.
- **Swarm CLI: `swarm db`** — `stats`, `export`, `prune`, `backup`, `check` for inspecting and maintaining the unified SQLite store.
- **Swarm CLI: `swarm test`** — supervised end-to-end orchestration test against a dedicated port with an AI-generated report.
- **Claude Code hook integration** — PreToolUse (drone-based approval), SessionEnd (immediate STUNG detection), and event hooks (SubagentStart/Stop, PreCompact/PostCompact) installed automatically by `swarm init`.

### Changes
- **Unified SQLite storage** — tasks, task history, proposals, messages, pipelines, buzz log, queen sessions, secrets, and config itself all live in `~/.swarm/swarm.db` (WAL mode). The legacy YAML is treated as a seed/import format; the database is the runtime source of truth after first run.
- **Jira auth is OAuth-only** — token auth was removed in favor of Atlassian OAuth 2.0 (3LO).
- **Config mutations are immediate** — dashboard edits write straight to the DB and hot-apply in the same request.
- **Calendar versioning** — version now tracks release date (`YYYY.M.D.patch`) rather than semver; the v1.0.0 section below is preserved for history.

### Fixes
- Numerous fixes to feedback submission (live `HiveConfig` serialization, `gh` CLI fallback for 8 KB URL limits, preview/edit gate before submission).
- See `git log` for the full per-commit history.

---

## v1.0.0

Initial release of Swarm — a hive-mind orchestrator for Claude Code agents.

### Features
- **Web Dashboard** — Browser-based dashboard with real-time WebSocket updates, inline terminal, and full task management
- **Worker Management** — Launch, kill, revive, and monitor Claude Code agents running in managed PTYs
- **Task Board** — Create, assign, complete, and track tasks with priority, tags, dependencies, and file attachments
- **Drones** — Background automation: auto-continue idle workers, auto-approve prompts, escalate stuck agents
- **Queen** — Headless Claude conductor for hive-wide coordination and per-worker analysis
- **Groups** — Organize workers into named groups for targeted broadcasts and management
- **Config** — YAML-based configuration with live-reload and web-based config editor
- **Notifications** — Browser notifications, terminal bell, and persistent Buzz Log
- **Task History** — Audit log tracking full task lifecycle events
- **Themed UI** — Warm beehive color palette, responsive layout, keyboard shortcuts
