# Mobile QA Findings — 2026-05-20

**Viewport:** 390×844 (iPhone 14 portrait), device-scale-factor 2.0
**Browser:** Chromium 148 (Playwright 1.60)
**User agent:** synthetic iPhone OS 16 Safari
**Auth:** real session cookie against live dashboard at `localhost:9090`
**Harness:** `scripts/mobile_qa.py` — re-runnable, saves screenshots
to `docs/qa-mobile-<timestamp>/`.

## Screenshots

| # | File | What it captures |
|---|---|---|
| 01 | `01-command-center.png` | Default landing (focus=Attention default) |
| 02 | `02-cc-focus-attention.png` | After localStorage wipe + Attention focus click |
| 03 | `03-cc-focus-queen.png` | Queen-focused (PTY visible) |
| 04 | `04-bottom-panel-tasks.png` | Bottom panel open, Tasks tab |
| 05 | `05-bottom-panel-pipelines.png` | Bottom panel open, Pipelines tab |
| 06 | `06-bottom-panel-playbooks.png` | Bottom panel open, Playbooks tab (P4a analytics visible) |
| 07 | `07-bottom-panel-activity.png` | Bottom panel open, Activity tab |
| 08 | `08-config-general.png` | Config page, General tab |
| 09 | `09-config-playbooks.png` | Config page, Automation tab (P4b playbooks form lives here) |

---

## JS errors — caught by the harness, not visible in screenshots (NEW)

### J1 — `queenCooldownTimer is not defined` fires on every page load (HIGH)

**Where:** Logged as `pageerror` on **every** screenshot capture (1, 2,
3, 4, 5, 6, 7, 8 — sometimes twice per load).

**Impact:** Real JS reference error. Even if it doesn't crash the
dashboard, it pollutes the console, may abort whatever code path
references it, and is a footgun for anyone debugging.

**Triage:** Search `dashboard.js` for `queenCooldownTimer`. Either:
- A `var` declaration was removed but the call site stayed
- Or the timer is referenced before its `var` initialization
- Or a function that closes over it was deleted

### J2 — `updateQueenHealthIndicator is not defined` on WS event (HIGH)

**Where:** Fires inside `ws.onmessage → handleEvent` at
`dashboard.js:624` when a queen-health WS event arrives.

**Impact:** Queen health indicator never updates from WS — operator's
live view of Queen status is silently broken whenever this code path
runs.

**Triage:** The function was likely renamed or removed but the WS
event handler still references it. `grep "updateQueenHealthIndicator"
src/swarm/web/` should be empty except for line 624; that's the call
site without a definition.

Both errors are **pre-existing**, not introduced by P5/P6/cleanup
batch. They reproduce on every load and only surfaced because
Playwright's `pageerror` listener catches them. The dashboard's
existing error-handling apparently swallows them silently in
production.

---

## Visual issues — ordered by severity

### P1 — Worker list eats 140+ px of vertical space at the top (BLOCKER)

**Symptom:** The "Queen Dashboard" card is ~140px tall with a 3-line
"operator command center" subtitle. "swarm (claude)" and "budgetbug"
workers appear as full-size cards beside it, forcing horizontal scroll
because they don't fit at 390px.

**Impact:** Operator sees ~250px of worker scroller before any
actionable content. On an 844px-tall phone, that's almost 40% of the
screen consumed before the main UI begins.

**Fix sketch:**
- Worker pills on mobile should be 40-44px tall (icon + name only).
- Drop the "operator command center" subtitle on mobile.
- Vertical scroll list, not horizontal — touch-scrolling a vertical
  list is more natural than discovering you must swipe horizontally.

**Affected:** `dashboard.html` worker-list partial; `base.html`
`.worker-item` / `.queen-card` CSS at `<=768px`.

### P2 — Status strip label/value collision (HIGH)

**Symptom:** "Queen Dashboard queue0/0 last hr14 today56 5h0%"
renders without spacing between label and value. Looks like one word:
`queue0/0`, `today56`, `5h0%`.

**Impact:** Unreadable at a glance. The strip's job is to give Queen
health in one sweep; instead it requires parsing each pair.

**Root cause likely:** `.cc-qs-item` uses `gap` between sibling
items but inside each item the label `<span>` and value `<span>` have
no separating margin/padding.

**Affected:** `base.html` `.cc-qs-item` / `.cc-qs-value` rules.

### P3 — Digest strip text overflows horizontally (HIGH)

**Symptom:** "2 shipped today · 7 task events completed: Ext..."
truncates at "Ext..." with no scroll affordance and no title-attribute
fallback.

**Impact:** Operator can't see which tasks shipped or what's pending.

**Fix:** `overflow-x: auto` on the digest strip, or `text-overflow:
ellipsis` + `title` attribute carrying the full text, or wrap to
multiple lines on mobile.

**Affected:** `dashboard.html` cc-digest-strip; `base.html`
`.cc-digest-strip` rule.

### P4 — Header status pills wrap to 3 vertical lines (MEDIUM)

**Symptom:** In screenshot 02, the `2BUZ / 1RES / 11SLE` worker-state
counts in the header stack vertically because the header can't fit
them in one row alongside the Drones / battery / hamburger buttons.

**Impact:** 60+ pixels of header height eaten on small viewports.

**Fix:** Hide the BUZ/RES/SLE pills on mobile (`header-hide-mobile`
already hides other items); the counts are visible inside the
dashboard. Or combine them into one pill: total worker count.

### P5 — Worker card subtitle wastes 3 lines (MEDIUM)

**Symptom:** Inside the Queen Dashboard card, "operator command
center" breaks across three lines.

**Impact:** Contributes to P1's vertical-real-estate problem.

**Fix:** `display: none` on `.queen-card-subtitle` under 600px.

### P6 — Attention card empty-state text truncates mid-word (LOW)

**Symptom:** "Nothing needs you — the swarm is running clean" renders
as "Nothing needs you — the swarm i...". Visible across 01, 02, 04.

**Fix:** Trace `.cc-empty` / `.cc-attention-card-body` from `base.html`;
P5 set `white-space: normal` on `.cc-attention-card-body` at <=600px,
but the empty-state container is a sibling, not a descendant — it
needs its own rule.

### P7 — Bottom-panel FAB timing fragile (NOTE)

**Symptom:** Required re-running the QA script with longer waits and
localStorage clear to reliably capture the bottom-panel screenshots.
Not a dashboard bug — a harness note.

**Status:** Already addressed in `mobile_qa.py` (clears storage +
reloads + waits 1500ms after the toggle click).

---

## Working as intended

- ✅ **P5 focus toggle**: Attention/Queen buttons flip correctly;
  active button has lavender background.
- ✅ **Worker state filter chips** wrap to two lines without overlap.
- ✅ **Queen PTY terminal** is readable at 390px — text legible,
  scrollback present.
- ✅ **Queen action buttons** wrap into a 2-column grid as P5 spec'd
  (visible at bottom of screenshot 03 with Refresh).
- ✅ **Bottom panel tab nav** fits 5 tabs (Tasks / Decisions /
  Pipelines / Playbooks / Activity) in one row at 390px.
- ✅ **Playbooks analytics summary** (P4a) renders — 6 stat tiles in
  a row, totals + 24h event counts visible. Movers list below.
- ✅ **44px tap targets** on the header hamburger and most buttons.
- ✅ **Config tab horizontal scroll** works; Automation tab tappable.

---

## Out of scope / not verified by this run

- **Pipeline editor modal at mobile width** — no pipelines exist in
  the dataset to open.
- **Pipeline detail modal at mobile width** — same reason.
- **Retry-on-COMPLETED confirmation modal (cleanup batch follow-up)**
  — same; needs a pipeline with a completed step.
- **Linked-task chip click flow** — same; needs a pipeline step with
  a task_id.
- **WebSocket reconnect behaviour** — not exercised by static
  screenshots.

If we want to verify these, the QA harness needs a "fixture setup"
step that creates a pipeline + tasks before screenshotting. Out of
scope for this pass.

---

## Next steps — suggested commit shape

A single follow-up "mobile fixes from QA" commit:

1. **J1 + J2 first** — these are real JS errors, not just visual
   polish. Worth diagnosing standalone before touching CSS.
2. **P1 + P5 together** — worker list compaction. Biggest visual
   win. ~30 LOC.
3. **P2 + P3** — status strip spacing + digest overflow. ~5 LOC each.
4. **P4** — hide header status pills on mobile. One-liner.
5. **P6** — attention empty-state word-wrap. Small CSS trace + fix.

Total: ~50 LOC + 2 JS reference-error fixes. Re-run
`scripts/mobile_qa.py` after to compare.
