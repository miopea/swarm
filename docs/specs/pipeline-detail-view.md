# Pipeline Detail View + Retry — P3 of the Editor UX Series

Status: **specified, not yet implemented**
Date: 2026-05-20
Series: P1 (create/edit overhaul) → P2 (schedule builder + tz) → **P3 (detail view + retry)** → P4/P5/P6
Audience: implementer (almost certainly the same model that wrote this spec)

---

## Problem

After P1 + P2, the pipeline editor reads well, but the **inspect** path doesn't.
Every step's `error`, `result`, `started_at`, `completed_at`, and `task_id` are
on the model, and none of them surface in the dashboard. Failed steps can be
SKIPPED but not retried — once a step flips to FAILED the only recovery is
"manually skip everything that depends on it, then run again from scratch."

P3 adds a **detail view** that shows the full per-step state and a **retry**
path that resets a FAILED step plus its FAILED dependents so the engine can
fire them again under the existing lifecycle rules.

## Interview decisions (2026-05-20)

| Question | Decision |
|---|---|
| DAG rendering fidelity | **Smarter stacked list** — grouped by execution wave, indented by dep depth, `← blocked by …` annotation inline. No SVG, no library. |
| Detail view placement | **Modal** — matches task editor / pipeline editor / decision modal patterns. |
| Retry semantics | **Reset this step + cascade-reset FAILED downstream.** SKIPPED is left alone (operator intent is sticky). |
| Live updates | **Yes** — re-render on the existing `pipelines_changed` WebSocket event. |
| Fields surfaced | step error + result + timestamps + duration; linked task chip; pipeline header (tz / schedule / tags / template_name); read-only automated-step config. |
| Step event log | **Deferred** — would need a new event table; punt to a later phase. |
| Task link behavior | **Switch to Tasks tab + open task editor** for that ID. Reuses existing dashboard pattern; no modal-stacking. |
| Emergency actions | **Pause + per-step Skip only.** No force-complete / force-fail buttons yet. |
| Result rendering | **Pretty JSON + Copy + smart unwrap for `shell_command`** (stdout / stderr / returncode pulled out as labeled blocks above the raw JSON). |
| Retry on COMPLETED steps | **No** — only FAILED. COMPLETED re-runs invite side-effect duplication (double webhooks, double commands). Re-runs require duplicating the pipeline. |
| Open target | **Click anywhere on the card** (excluding the action buttons that already do something). |
| Edit button on detail modal | **Yes** — only when status ∈ {DRAFT, PAUSED}, matching the engine guard added in P1. |

---

## Implementation

### Backend

1. **New endpoint** `POST /api/pipelines/{id}/steps/{step_id}/retry`.
   - 404 if pipeline or step not found.
   - 409 if step is not currently FAILED (we don't support COMPLETED retry).
   - On success:
     - Step status → PENDING. Clears `started_at`, `completed_at`, `error`,
       `result`, `task_id`.
     - Walk forward through the DAG: every step whose `depends_on` includes the
       retried step (transitively, BFS) **and** is currently FAILED also resets
       to PENDING with the same cleared fields. SKIPPED / COMPLETED downstream
       stay untouched.
     - `engine.advance()` re-evaluates and emits the usual `change` event.
   - Returns `{ ok: true, reset: ["stepA", "stepC", ...] }` so the UI can toast
     the cascade.

2. **Engine helper** `PipelineEngine.retry_step(pipeline_id, step_id) -> list[str]`.
   - Lives next to `complete_step` / `skip_step` / `fail_step`.
   - Raises `ValueError` for not-found and not-FAILED — route handler maps to 404 / 409.
   - Internal helper `_failed_downstream(pipeline, step_id) -> list[str]` walks
     the forward dependency graph (every step that has `step_id` in its deps,
     recursively) collecting FAILED descendants. SKIPPED / COMPLETED are
     stopped at — we don't traverse past them.

3. **No model changes.** All necessary fields already exist.

### Frontend

1. **Click target.** `renderPipelines()` adds `data-action="showPipelineDetail"`
   `data-pipeline-id="..."` on the card body wrapper. The existing buttons
   (Start / Pause / Resume / Edit / Delete) keep their inline `onclick`
   handlers and stop propagation so they don't trigger the detail open.

2. **New modal** `#pipeline-detail-modal`. Sections, top-to-bottom:
   - **Header** — pipeline name, status badge, progress %, timezone, schedule
     human-readable + next-fire timestamp, tags, template_name (if any),
     created/updated timestamps. Edit + Close buttons.
   - **Steps** — vertical list grouped by execution wave. Each step:
     - Status icon + step name + step type pill.
     - "← blocked by stepA, stepB" annotation when dep list is non-empty.
     - Timestamps + duration when present.
     - For agent/human steps: linked task chip showing task number; click
       → close detail modal, switch bottom panel to Tasks tab, open task
       editor for that task.
     - For automated steps: service name + collapsible read-only JSON config.
     - Result block when present:
       - For `shell_command` results: labeled `stdout` / `stderr` /
         `returncode` blocks first, then raw JSON below.
       - For other handlers: pretty JSON only.
       - Copy button copies the raw result dict as JSON.
     - Error block (red, monospace) when present.
     - Per-step actions: **Retry** (only when FAILED), **Skip** (when
       READY / IN_PROGRESS — matches list-view behaviour), **Mark done**
       (human steps in READY / IN_PROGRESS — same).

3. **Execution waves.** Computed client-side by Kahn-style topological
   levelization: wave 0 = steps with no deps; wave N = steps whose deps are
   all in waves < N. Same DAG that the engine's `advance()` walks; just
   displayed in level order rather than chronological order.

4. **Live updates.** The modal subscribes to `pipelines_changed` WS messages.
   On match (event payload includes the pipeline ID being viewed), refetch
   `/api/pipelines/{id}` and re-render. Re-render preserves scroll position
   and any expanded automated-config blocks.

5. **Retry button flow.**
   ```
   Click Retry on step X
     → POST /api/pipelines/{id}/steps/X/retry
     → 409 → toast "Step is not FAILED" (race condition guard)
     → 200 → toast "Retried X (also reset: stepC, stepE)"
     → live update triggers re-render (will already show the cascade)
   ```
   No confirm dialog. The button is in the detail modal, the operator already
   navigated there intentionally, and the action is fully reversible (the
   reset steps just become PENDING again — Pause + un-retry not needed).

### Tests

1. **Engine.**
   - `retry_step` on a FAILED step resets it to PENDING + clears fields.
   - `retry_step` on a FAILED step with a FAILED downstream resets both.
   - `retry_step` skips SKIPPED downstream (sticky operator intent).
   - `retry_step` skips COMPLETED downstream (no double execution).
   - `retry_step` on a non-FAILED step raises `ValueError`.
   - Cascade is transitive: A FAILED → B FAILED depends on A → C FAILED
     depends on B → retry A resets A, B, C.

2. **Route.**
   - `POST /api/pipelines/{id}/steps/{step_id}/retry` returns 200 + `reset`
     list on success.
   - 404 on unknown pipeline.
   - 404 on unknown step.
   - 409 on non-FAILED step.

---

## Out of scope (defer)

- **Force-complete / force-fail pipeline** buttons. Not enough demand; the
  Pause + per-step Skip path already gets there.
- **Edit-step-config-then-retry.** Bigger lifecycle surface. Worth doing
  once we see operators actually editing service configs after failures.
- **Step event history table.** Would need a new SQL table + write path on
  every status transition. Punt until there's a real audit-log use case.
- **DAG SVG / Mermaid rendering.** Stacked list is enough for typical 3–10
  step pipelines. Revisit if pipelines get bigger.
- **Retry on COMPLETED steps.** Side-effect-safety story isn't here yet.

## Risks

- **Cascade-reset races a live engine.** If a worker completes a downstream
  step between the retry POST and the cascade walk, we might reset a step
  that's already COMPLETED. The walk reads the pipeline under the engine's
  in-process lock (same lock that guards `complete_step` / `fail_step`), so
  this should be impossible in practice — but the spec writer is flagging
  the assumption.
- **Live update + open detail collision.** If the WebSocket fires while the
  operator is mid-scroll, the modal re-renders. Mitigated by preserving
  scroll position; if it feels jarring in practice, debounce to once per
  500ms.
- **Task tab switching from a modal.** Existing `switchTab()` doesn't know
  about modals. Need to verify it cleanly dismisses the detail modal before
  swapping panels; otherwise we leak modal state.

## Done criteria

- All retry tests above pass.
- Detail modal opens on card click, shows every field listed in the
  "Steps" section, and reflects WS updates without page reload.
- Edit button on the detail modal opens the pipeline editor pre-filled
  (re-uses P1's `showEditPipeline()`).
- Task chip click switches tabs and opens the task editor for the
  linked task ID.
- `release: X.Y.Z` commit with this spec linked in the body.
