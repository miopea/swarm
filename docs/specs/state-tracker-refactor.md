# WorkerStateTracker Refactor — Spec

**Audit finding**: #3 (MAJOR — SRP violation)
**Source**: `/audit-code` 2026-05-26 report (this conversation)
**Author**: pre-implementation spec — operator approval needed before code lands
**Status**: draft

---

## 1. Problem

`src/swarm/drones/state_tracker.py` is 856 lines, 30 methods in one
class (`WorkerStateTracker`). The class conflates ≥5 distinct
responsibilities:

| Concern | Methods | State (instance fields) |
|---------|---------|-------------------------|
| State machine + transition emit | `_classify_worker_state`, `_handle_state_change`, `_handle_waiting_exit`, `_detect_operator_terminal_approval`, `_log_state_transition`, `_sync_display_state` | `_prev_states`, `_waiting_content`, `_drone_continued`, `_operator_continued`, `_any_became_active`, `_needs_assign_check` |
| Suspension lifecycle | `wake_worker`, `_maybe_suspend_worker`, `_is_suspended_skip` | `_suspended`, `_suspended_at`, `_suspend_safety_interval` |
| Output fingerprint / sleeping throttle | `_update_content_fingerprint`, `_should_throttle_sleeping`, `_track_idle` | `_content_fingerprints`, `_unchanged_streak`, `_idle_consecutive`, `_last_full_poll`, `_focused_workers` |
| Health detectors (5 detectors, all consume `(worker, content)`) | `_check_diminishing_returns`, `_check_context_error`, `_check_context_pressure`, `_check_rate_limit`, `_track_context_files`, `_has_active_turn_signal` | `_rate_limit_seen`, (rest on `worker` directly) |
| Poll orchestration | `_poll_single_worker`, `_poll_sleeping_throttled`, `_poll_dead_worker`, `cleanup_dead_worker` | (none distinct) |

This makes the class hard to test in isolation — the existing
`tests/test_state_tracker.py` partitions tests by concern
(TestContextPressure, TestRateLimitDebounce, TestContextFileTracking,
TestDiminishingReturns, …) but every test still has to spin up the
full tracker with mocked dependencies for every assertion.

It also obscures a **real duplication**: `_check_context_pressure` in
state_tracker and `ContextPressureWatcher` in
`drones/context_pressure.py` both decide when to inject `/compact`,
with overlapping-but-not-identical thresholds. Today the tracker fires
synchronously per poll for BUZZING workers only, and the watcher
sweeps state-aware for all workers. Untangling that overlap is the
follow-up the refactor enables.

---

## 2. Goals & non-goals

### Goals
- **Isolate detectors as testable units.** Each detector becomes a
  class with `__init__(deps)` + `check(worker, content)` (or
  `check(worker)`) and its own state dict — testable with no
  WorkerStateTracker construction.
- **Preserve external API surface.** Everything outside
  `state_tracker.py` (pilot, poll_dispatcher, server/analyzer)
  continues to import and call the same names from the same module
  for the seam landing. Internal callers can switch to direct
  detector calls in a follow-up.
- **No behavior change.** Every detector keeps the same thresholds,
  same debounce windows, same log messages, same emit signatures.
  Diff should be moves + renames + signature normalization, not
  logic changes.
- **No new abstractions if the dependency only has one caller**
  (YAGNI). Helpers that exist solely to hide a 3-line subroutine stay
  inlined.

### Non-goals (deliberate)
- **Not collapsing the context-pressure duplication.** That's a
  separate task; once extracted, both call sites are clearly visible
  side-by-side and the merge becomes a small dedicated change rather
  than a refactor entangled with a behavior change.
- **Not extracting the poll orchestrator** (`_poll_single_worker`).
  It stays on `WorkerStateTracker` because it's the hot loop and
  benefits from direct field access. The detectors it composes
  become injected dependencies.
- **Not changing the state-machine class** itself — the suspension
  + display-state + transition logic stays on WorkerStateTracker.
  Those operations are mutually entangled with `_poll_single_worker`
  and extracting them would require widening the orchestrator's
  parameter surface significantly.

---

## 3. Target shape

```
src/swarm/drones/
├── state_tracker.py        (~400 lines after extraction)
│   ├── _build_safe_pattern          (module-level helper, unchanged)
│   └── class WorkerStateTracker
│       ├── __init__(workers, log, …, detectors: WorkerHealthDetectors)
│       ├── any_became_active / needs_assign_check  (properties)
│       ├── mark_operator_continue / mark_drone_continued
│       ├── wake_worker / _maybe_suspend_worker / _is_suspended_skip
│       ├── _classify_worker_state
│       ├── _handle_state_change / _handle_waiting_exit
│       ├── _detect_operator_terminal_approval
│       ├── _log_state_transition / _sync_display_state
│       ├── _update_content_fingerprint
│       ├── _should_throttle_sleeping / _track_idle
│       ├── _poll_sleeping_throttled / _poll_dead_worker / _poll_single_worker
│       ├── cleanup_dead_worker
│       └── _suggest_approval_pattern  (staticmethod — keep here for now;
│                                        used by server/analyzer)
└── detectors/                       (new package)
    ├── __init__.py                  (re-exports for clean imports)
    ├── context_files.py             ContextFileTracker
    ├── diminishing_returns.py       DiminishingReturnsDetector
    ├── context_pressure.py          ContextPressureCheck  (synchronous,
    │                                 BUZZING-only — distinct from the
    │                                 periodic ContextPressureWatcher)
    ├── context_recovery.py          ContextRecoveryDetector
    │                                 (tiered: compact → revive → escalate)
    ├── rate_limit.py                RateLimitDetector
    └── turn_signals.py              has_active_turn_signal()  (free function;
                                      no state needed)
```

### Detector contract

```python
class ContextRecoveryDetector:
    """Detect context-window errors in PTY output; trigger tiered recovery."""

    def __init__(
        self,
        log: DroneLog,
        decision_executor: DecisionExecutor,
        emit: Callable[..., None],
    ) -> None:
        self._log = log
        self._decision_executor = decision_executor
        self._emit = emit

    def check(self, worker: Worker, content: str) -> None:
        ...
```

Rules:
- **Stateless dependencies in __init__** — log, decision_executor, emit, plus
  whatever shared dicts the detector needs to *read* (e.g. drone_config).
- **Owns its own per-worker state** (`_rate_limit_seen`, etc.) — no
  cross-detector mutation.
- **Single public `check()` method.** No properties, no side accessors. If
  the caller needs to reset on dead worker, expose `forget(worker_name)`.
- **No async.** Detectors are synchronous; deferred actions go through
  `_decision_executor._deferred_actions` (existing pattern).

### Composition wrapper (optional — see §5)

```python
@dataclass
class WorkerHealthDetectors:
    context_files: ContextFileTracker
    diminishing: DiminishingReturnsDetector
    pressure: ContextPressureCheck
    recovery: ContextRecoveryDetector
    rate_limit: RateLimitDetector
```

`WorkerStateTracker._poll_single_worker` calls each in sequence:

```python
# Old:
self._track_context_files(worker, content)
self._check_diminishing_returns(worker)
self._check_context_pressure(worker)
self._check_context_error(worker, content)
self._check_rate_limit(worker, content)

# New:
self._detectors.context_files.check(worker, content)
self._detectors.diminishing.check(worker)
self._detectors.pressure.check(worker)
self._detectors.recovery.check(worker, content)
self._detectors.rate_limit.check(worker, content)
```

Net change in the orchestrator: 5 lines of code, same behavior.

---

## 4. Test impact

`tests/test_state_tracker.py` has 37 tests; the 5 test classes that
map 1:1 to extracted detectors (~18 tests) move to new files under
`tests/drones/detectors/`:

| Existing class | New file |
|----------------|----------|
| TestRateLimitDebounce | `tests/drones/detectors/test_rate_limit.py` |
| TestContextFileTracking | `tests/drones/detectors/test_context_files.py` |
| TestContextErrorRecoveryCounter | `tests/drones/detectors/test_context_recovery.py` |
| TestContextPressure | `tests/drones/detectors/test_context_pressure_check.py` |
| TestDiminishingReturns | `tests/drones/detectors/test_diminishing_returns.py` |

**Test fixture reduction**: each new file's fixture builds *just* the
detector under test (log + mock decision_executor + emit). No more
`_make_tracker()` boilerplate that mocks 12 dependencies per detector
test.

**Coverage win**: the rate-limit/context-files tests can drop the
mocked-Worker scaffolding (they were already noted as "small,
well-scoped private helpers" in the docstring). The tests get
shorter; new edge cases get cheaper to add.

The remaining 19 tests (TestBuildSafePattern, TestPropertiesAndSetters,
TestWakeWorker, TestContentFingerprint, TestTrackIdle,
TestShouldThrottleSleeping, TestCleanupDeadWorker) stay in
`test_state_tracker.py` — they cover the parts staying on the class.

---

## 5. Open question for the operator

There are two viable composition patterns. The choice changes the
PR shape:

**Option A — bundled holder (`WorkerHealthDetectors`)**

- Single new param on `WorkerStateTracker.__init__`: `detectors: WorkerHealthDetectors`.
- Pilot builds the holder once and passes it in.
- Pro: smallest API change, one new dataclass.
- Con: very thin abstraction over 5 fields — borderline-YAGNI.

**Option B — five individual params**

- `WorkerStateTracker.__init__(…, context_files, diminishing, pressure, recovery, rate_limit)`.
- Pilot constructs each detector and passes it in.
- Pro: zero new abstractions; each detector visible at the construction site.
- Con: tracker init signature grows by 5 params (already 14 today).

**My recommendation**: **Option A**, but only if `WorkerHealthDetectors`
gets a `from_config(...)` classmethod that hides the wiring. Otherwise
fall back to B — the dataclass adds nothing if every caller still
constructs each detector by hand.

---

## 6. Migration plan (3 phases, each independently shippable)

Each phase ships as its own `release: X.Y.Z` commit per the
[[feedback_ship_phases_independently]] memory — multi-phase plans
ship as separate releases, not batched.

### Phase 1 — extract pure detectors (no state machine touched)

The three with the cleanest seam: **context_files**, **diminishing**,
**rate_limit**. Each is a self-contained `(worker, content) → side
effects via log + emit` operation.

Steps:
1. Create `src/swarm/drones/detectors/__init__.py` (empty package).
2. Move `_RE_FILE_PATH`, `_MAX_CONTEXT_FILES`, `_track_context_files` →
   `ContextFileTracker.check`. Move dependencies in (log, drone_config).
3. Move `_DIMINISHING_DELTA`, `_DIMINISHING_STREAK`, `_check_diminishing_returns` →
   `DiminishingReturnsDetector.check`. The `worker._low_delta_streak`
   field stays on Worker.
4. Move `_check_rate_limit` + `_rate_limit_seen` → `RateLimitDetector.check`.
   Note: `poll_dispatcher.py:325` reads `state_tracker._rate_limit_seen`
   today — that read moves to a new `last_seen(name)` accessor on the
   detector. Sole external read; one edit.
5. Update `WorkerStateTracker.__init__` to receive the three detectors
   and delegate. Update pilot construction.
6. Move tests: TestContextFileTracking, TestDiminishingReturns,
   TestRateLimitDebounce → new files.
7. Run `/check`. Lint clean, all 4601 + new tests pass.

**Estimated diff**: ~250 added / ~150 removed across 4 source files +
3 new test files.

### Phase 2 — extract context-error recovery

`_check_context_error` is bigger (40 lines) and has tier-1/2/3 logic.
Extract it last in the detector wave because the tier 1 (`compact`)
and tier 2 (`revive`) paths queue deferred actions via
`_decision_executor._deferred_actions` — verifying the deferred-action
contract during isolation testing is the highest-risk part of the
whole refactor.

Steps:
1. Create `ContextRecoveryDetector` with `check(worker, content)`.
2. Move `_RE_CONTEXT_ERROR` constant in.
3. Migrate the three tiers as-is. The detector also resets
   `worker.recovery_attempts = 0` on the non-BUZZING transition; that's
   a worker-field write but doesn't violate "stateless dependencies."
4. Move `TestContextErrorRecoveryCounter` tests.
5. `/check`. Ship.

### Phase 3 — extract synchronous context-pressure check (and flag the duplication)

`_check_context_pressure` is the BUZZING-only synchronous check that
queues `/compact` when `worker.context_pct ≥ critical`. The dedicated
`ContextPressureWatcher` (drones/context_pressure.py) handles the
same signal periodically across all states. After extracting:

1. Create `ContextPressureCheck` (the new detector — note the name
   distinct from the `ContextPressureWatcher` to avoid confusion).
2. Move logic + `_check_context_pressure` body in.
3. Add a `# DUPLICATION:` block-level comment at the top of the new
   detector noting the overlap with `ContextPressureWatcher` and
   referring to a follow-up audit task. Operator decides whether to
   collapse the duplication separately.
4. Move `TestContextPressure`.
5. `/check`. Ship.

### Out-of-scope (separate tasks)

- Collapsing `ContextPressureCheck` vs `ContextPressureWatcher`. Filed
  as new audit finding after Phase 3 lands.
- Extracting `WorkerStateTracker`'s state-machine concern (the
  RESTING/SLEEPING/BUZZING/WAITING transition methods). The tight
  coupling with `_poll_single_worker` makes that a much bigger
  refactor than the detector extraction — separate spec if pursued.
- Extracting `_suggest_approval_pattern` to a util module. It's
  accessed from `server/analyzer.py:268` via the class today; moving
  it later costs ~3 file edits.

---

## 7. Risks

| Risk | Mitigation |
|------|-----------|
| Detector misses a worker-state field reset that the merged class did inline | Each detector ships with the exact tests that pinned down its behavior; running the full test_state_tracker suite after each phase catches regressions. |
| `poll_dispatcher` external access to `_rate_limit_seen` breaks | Phase 1, Step 4 — single call site, replaced with `last_seen()` accessor in the same commit. |
| Behavior drift between "old check_X" and "new detector.check()" due to subtle ordering inside `_poll_single_worker` | All detectors stay called in the same order from the same orchestrator. Ordering preserved by spec. |
| Pilot's construction of `WorkerStateTracker` grows hard to read | If Option A from §5 is chosen, the holder centralizes the wiring; Option B keeps it explicit at the cost of a longer call. |

---

## 8. Definition of done

- [ ] `src/swarm/drones/detectors/` exists with 5 classes (or 4 +
      `turn_signals.py` for the free function).
- [ ] `WorkerStateTracker` shrunk from 856 → ~400 lines, 30 → ~18 methods.
- [ ] `tests/drones/detectors/` has the 5 extracted test files, each
      directly testing its detector with no `_make_tracker()` mock.
- [ ] No behavior change — full `pytest` green, same test count or
      higher (no regressions added).
- [ ] No new `Any` types, no new `# type: ignore` markers introduced.
- [ ] Each phase committed as `release: X.Y.Z` per the project's
      multi-phase shipping convention.
- [ ] Follow-up audit task filed for the `ContextPressureCheck` ↔
      `ContextPressureWatcher` duplication.

---

## 9. Operator approval gate

This is the spec only. **No code changes have been made.** Before
implementation begins, the operator should confirm:

1. The 3-phase split is the right ship rhythm (vs. one big PR).
2. Option A or Option B from §5.
3. Whether to start with Phase 1 immediately or defer.
