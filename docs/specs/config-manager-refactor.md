---
title: "ConfigManager Refactor"
status: SHIPPED
shipped_date: 2026-05-26
shipped_releases: ["2026.5.26.6"]
---

# ConfigManager Refactor — Spec

> **Status: SHIPPED (2026-05-26, `2026.5.26.6`).** See the CHANGELOG entry
> "ConfigManager refactor (audit finding #2)". Every section applier was
> extracted into `src/swarm/server/config_appliers/` (`_base.py`, `drones.py`,
> `queen.py`, `playbooks.py`, `notifications.py`, `workflows.py`, `test.py`,
> `coordination.py`, `jira.py`, `advanced.py`, `llms.py`, `workers.py`), driven
> by `config_appliers.SECTION_REGISTRY`. `server/config_manager.py` is down from
> 1584 to ~702 lines and its module docstring points back here.
>
> One naming deviation worth knowing: the range validators are **module-level
> functions in the applier modules**, not `ConfigManager` methods — e.g.
> `_validate_playbook_ranges` in `config_appliers/playbooks.py` and
> `_validate_drone_ranges` in `config_appliers/drones.py`.
>
> Retained as a historical reference.

**Audit finding**: #2 (MAJOR — SRP violation)
**Source**: `/audit-code` 2026-05-26 report
**Status**: ~~draft — operator approval needed before code lands~~ → SHIPPED, see banner above

---

## 1. Problem

`src/swarm/server/config_manager.py` is 1584 lines, with one class
(`ConfigManager`) carrying 41 methods. The class conflates four
distinct responsibilities, none of which depend on each other's state:

| Concern | Methods | Approx lines | Touches |
|---------|---------|--------------|---------|
| **Section appliers** (per-section validate + assign) | `_apply_drones`, `_apply_queen`, `_apply_playbooks`, `_apply_notifications`, `_apply_workflows`, `_apply_test`, `_apply_coordination`, `_apply_jira`, `_apply_advanced`, `_apply_scalars`, `_apply_buttons`, `_apply_llms`, `_apply_provider_overrides`, `_apply_workers`, `_apply_worker_entry`, `_apply_worker_identity`, `_apply_default_group`, plus their `_validate_*` and `_apply_*_scalars` helpers (28 methods total) | ~830 | `self._config`, occasionally `self._get_worker_svc()`, `self._invalidate_provider_cache()` |
| **File watcher** | `watch_mtime`, `check_file` | ~95 | `self._config.source_path`, `self._config_mtime`, `self._broadcast_ws` |
| **Persistence facade** | `save`, `_save_to_db`, `toggle_drones` | ~50 | `self._swarm_db`, `self._config`, `self._broadcast_ws` |
| **Lifecycle / orchestration** | `__init__`, `hot_apply`, `reload`, `_invalidate_provider_cache`, `apply_update`, `_KNOWN_BODY_KEYS` | ~150 | All callbacks (`_apply_config`, `_get_pilot`, `_rebuild_graph`, `_rebuild_jira`, `_get_worker_svc`, `_broadcast_ws`, `_drone_log`) |

The cost shows up two places:

1. **Tests pay an 8-callback tax.** Today, exercising one section
   applier means standing up the full `ConfigManager` with mocks for
   `broadcast_ws`, `drone_log`, `apply_config`, `get_pilot`,
   `rebuild_graph`, `rebuild_jira`, `get_worker_svc`, plus an in-memory
   `SwarmDB`. The appliers themselves only need `self._config` —
   everything else is dead weight for their tests.

2. **`apply_update` reads like a 12-step pipeline** because it
   inlines every dispatch (`if "drones" in body: ... if "queen" in
   body: ...`). The post-#328 fail-loud guard at the end has to keep a
   manually-maintained `_KNOWN_BODY_KEYS` frozenset in sync with the
   dispatch list — exactly the maintenance burden the silent-drop
   bug class was supposed to be done with.

The module-level helpers (`_resolve_hints`, `_apply_dataclass_dict`,
`_apply_typed_value`, `validate_body_keys`, `_warn_unknown_subkeys`,
`FieldOutcome`, `ApplyResult`) are already free functions and don't
need to move — they're the framework the appliers consume.

---

## 2. Goals & non-goals

### Goals

- **Make each section applier testable as a free function** —
  `apply_drones(cfg: HiveConfig, body: dict) -> FieldOutcome` with no
  ConfigManager construction required. Same contract callers expect
  today.
- **Make `apply_update` data-driven** — a registry-of-appliers
  dispatch loop replaces the hand-rolled chain. `_KNOWN_BODY_KEYS`
  becomes a property derived from the registry, killing the
  "remember to update both places when adding a section" footgun.
- **Preserve exact behavior.** Same validation errors, same field
  outcomes, same logging, same fail-loud diagnostic at the end of
  `apply_update`. Diff is moves + renames + a dispatch table — no
  semantic change to validation logic.
- **Keep the public ConfigManager surface stable.** Routes still call
  `config_mgr.apply_update(body)`, `config_mgr.save()`,
  `config_mgr.reload()`, `config_mgr.toggle_drones()`,
  `config_mgr.check_file()`, `config_mgr.watch_mtime()`. Nothing
  outside this file changes import path or method name.

### Non-goals (deliberate)

- **Not rewriting the generic `_apply_dataclass_dict` framework.** It
  works; the audit acknowledged it as the post-#328 hardening. Leave
  it alone.
- **Not collapsing the bespoke validators into the generic dispatcher.**
  Each section has earned its bespoke handling (regex compile for
  rules, range checks for drone scalars, default-merge for
  `jira.status_map`). The goal is to move them, not redesign them.
- **Not extracting a separate `ConfigWatcher` class.** `watch_mtime` +
  `check_file` are 95 lines and only call `_broadcast_ws` + `load_config`
  + `hot_apply`. Extracting them adds an indirection without removing
  state coupling (the watcher would still need a handle back to the
  manager to call `hot_apply`). Borderline-YAGNI; defer.
- **Not extracting a separate `ConfigPersistence` class.** Same logic —
  `save` + `_save_to_db` are 50 lines and only call into
  `config_store.save_config_to_db`. Wrapping that in a class adds
  ceremony without removing coupling.

---

## 3. Target shape

```
src/swarm/server/
├── config_manager.py        (~500 lines after extraction)
│   ├── FieldOutcome / ApplyResult       (unchanged)
│   ├── module-level helpers              (unchanged)
│   │   _resolve_hints, _apply_scalar, _apply_collection,
│   │   _apply_typed_value, _warn_unknown_subkeys,
│   │   validate_body_keys, _apply_dataclass_dict,
│   │   _body_touches_approval_rules
│   └── class ConfigManager
│       ├── __init__(... appliers: ConfigAppliers)        (new param)
│       ├── hot_apply / reload / watch_mtime / check_file
│       ├── toggle_drones / save / _save_to_db
│       ├── _invalidate_provider_cache
│       └── apply_update(body)
│           # iterates self._appliers.registry, dispatches,
│           # collects outcomes into ApplyResult, runs fail-loud
│           # guard against derived _known_body_keys.
└── config_appliers/                         (new package)
    ├── __init__.py                          (re-exports + registry)
    ├── _base.py                             SectionApplier protocol
    ├── drones.py                            apply_drones + helpers
    ├── queen.py                             apply_queen + helpers
    ├── playbooks.py                         apply_playbooks + helpers
    ├── notifications.py                     apply_notifications + helpers
    ├── workflows.py                         apply_workflows
    ├── test.py                              apply_test
    ├── coordination.py                      apply_coordination
    ├── jira.py                              apply_jira
    ├── advanced.py                          apply_advanced + apply_buttons
    ├── llms.py                              apply_llms + apply_provider_overrides
    └── workers.py                           apply_workers + apply_worker_entry
                                              + apply_worker_identity +
                                              apply_default_group + apply_scalars
```

### Section applier contract

```python
# swarm/server/config_appliers/_base.py
from typing import Protocol

from swarm.config import HiveConfig
from swarm.server.config_manager import FieldOutcome


class SectionApplier(Protocol):
    """A function that validates and applies one config section."""

    def __call__(
        self,
        cfg: HiveConfig,
        body: dict[str, Any],
        *,
        deps: ApplierDeps,
    ) -> FieldOutcome:
        ...


@dataclass
class ApplierDeps:
    """Side-effect handles the appliers may need.

    Most appliers only touch ``cfg``. A few reach out for live worker
    state (`get_worker_svc`) or invalidate provider caches when LLM
    tuning changes — those handles travel here so the appliers stay
    pure functions of (cfg, body, deps).
    """

    invalidate_provider_cache: Callable[[], None]
    get_worker_svc: Callable[[], WorkerService | None]
```

Most appliers ignore `deps` entirely; `llms` and `workers` are the
two that actually need it. Cheaper than threading 8 callbacks through
every function signature.

### Registry-driven dispatch

```python
# swarm/server/config_appliers/__init__.py
from swarm.server.config_appliers.drones import apply_drones
from swarm.server.config_appliers.queen import apply_queen
# ... etc

# Order matches the existing apply_update sequence so legacy
# error precedence is preserved (drones validates before queen, etc.)
SECTION_REGISTRY: list[tuple[str, SectionApplier]] = [
    ("llms", apply_llms),                        # void-returning helpers
    ("provider_overrides", apply_provider_overrides),
    ("drones", apply_drones),
    ("queen", apply_queen),
    ("notifications", apply_notifications),
    ("workflows", apply_workflows),
    ("test", apply_test),
    ("coordination", apply_coordination),
    ("jira", apply_jira),
    ("playbooks", apply_playbooks),
    ("advanced", apply_advanced),                # virtual section (top-level keys)
    ("scalars", apply_scalars),                  # virtual section (workers/buttons/...)
]
```

`ConfigManager.apply_update` becomes:

```python
async def apply_update(self, body: dict[str, Any]) -> dict[str, Any]:
    result = ApplyResult()
    for name, applier in SECTION_REGISTRY:
        if name in ("advanced", "scalars"):
            outcome = applier(self._config, body, deps=self._deps)
        elif name in body:
            outcome = applier(self._config, body[name], deps=self._deps)
        else:
            continue
        if name in ("advanced", "scalars"):
            result.consumed.extend(outcome.consumed)
        else:
            result.merge_section(name, outcome)
    # fail-loud guard
    ...
```

The `_KNOWN_BODY_KEYS` set is derived from the registry plus the
known top-level scalar keys that `apply_scalars` / `apply_advanced`
consume. The "remember to update two places" footgun goes away.

---

## 4. Test impact

`tests/test_config_manager.py` has 50+ tests, most of which build a
ConfigManager with the 8-callback constructor just to call one
`_apply_*` method. After extraction, those tests can call the free
function directly:

```python
# Before:
def test_drones_threshold_validation():
    cm = _make_config_manager(...)        # 8 callbacks mocked
    with pytest.raises(ValueError, match="must be >= 0"):
        cm._apply_drones({"escalation_threshold": -1})

# After:
def test_drones_threshold_validation():
    cfg = HiveConfig(...)
    with pytest.raises(ValueError, match="must be >= 0"):
        apply_drones(cfg, {"escalation_threshold": -1}, deps=_test_deps())
```

The existing `_make_config_manager` helper stays for the lifecycle /
orchestration tests; only the section-applier tests move to the new
per-section files.

---

## 5. Migration plan

Two phases. Each phase ships as its own `release: X.Y.Z` per the
[[feedback_ship_phases_independently]] convention.

### Phase 1 — extract section appliers (the big move)

The bulk of the refactor. Touches ~830 lines of `ConfigManager` and
creates the new `config_appliers/` package.

Steps:
1. Create `src/swarm/server/config_appliers/__init__.py` (empty
   package) and `_base.py` with the `SectionApplier` protocol +
   `ApplierDeps` dataclass.
2. Move each `_apply_<section>` method into its own
   `config_appliers/<section>.py` file as a free function, preserving
   the exact validation logic and error messages. Section-local
   constants (`_DRONE_NON_NEGATIVE_NUMBERS`, `_QUEEN_FIELDS`, etc.)
   move with their owners.
3. Module-level helpers in `config_manager.py` get re-exported from
   the new package so appliers can `from swarm.server.config_manager
   import _apply_dataclass_dict` without circular import (or move them
   into the package — either works; pick the one with fewer cross-
   refs).
4. `ConfigManager.__init__` takes an `appliers: ConfigAppliers`
   parameter (a dataclass bundling `ApplierDeps` + the registry).
   Daemon constructs it once.
5. `apply_update` becomes the registry loop above.
6. Test files split: lifecycle tests stay in
   `tests/test_config_manager.py`; per-section tests move to
   `tests/server/config_appliers/test_<section>.py`.
7. `/check`. Ship.

**Estimated diff**: ~900 new lines / ~830 removed (largely 1:1
moves), 12 new source files + ~12 new test files.

### Phase 2 — registry-derive `_KNOWN_BODY_KEYS` + final cleanup

After Phase 1 settles:

1. Replace the hand-maintained `_KNOWN_BODY_KEYS` frozenset with a
   property computed from the registry + each applier's declared
   top-level keys (top-level appliers like `advanced` and `scalars`
   declare their key set as a module-level constant).
2. Remove now-unused class attributes (`_DRONE_SCALAR_KEYS` if it's
   only used internally, `_QUEEN_CUSTOM_KEYS` once it lives next to
   `apply_queen`, etc.).
3. Re-run drift detection — confirm no orphan attributes left on
   ConfigManager.
4. `/check`. Ship.

### Out-of-scope follow-ups

- Extracting `watch_mtime` / `check_file` into a `ConfigWatcher`. 95
  lines, tightly coupled to `hot_apply`. Defer — file a separate
  audit task if pressure builds.
- Extracting `save` / `_save_to_db` into a `ConfigPersistence`. Same
  reasoning — 50 lines, minimal coupling, minimal benefit.
- Auto-generating `SectionApplier` registrations from a decorator
  (`@section("drones")`). Tempting; YAGNI — 12 explicit entries are
  more debuggable than discovery.

---

## 6. Risks

| Risk | Mitigation |
|------|-----------|
| Error precedence drift (e.g. drones range error now fires after queen type error) | Registry order matches the existing `apply_update` sequence verbatim. |
| Circular import between `config_manager.py` (FieldOutcome / helpers) and `config_appliers/` (uses them) | Both options work: re-export, or move helpers into the package. Phase 1 step 3 picks whichever has fewer cross-refs. |
| Tests that reach into `cm._apply_drones(...)` break | Migration step 6 — split the tests in lock-step with the source moves. Lifecycle tests (apply_update integration) stay on ConfigManager. |
| Section-local constants like `_DRONE_NON_NEGATIVE_NUMBERS` are referenced from other modules | Grep shows they're not (they're all `_`-prefixed class attrs). |
| Phase 1's PR is large | This is the trade-off vs. a sub-phased extraction. The 12 appliers are independent enough that a single PR with section-by-section commits would also work — the PR shape is up to the operator. Default plan ships Phase 1 as one commit because the moves are mechanical. |

---

## 7. Definition of done

- [ ] `src/swarm/server/config_appliers/` exists with one module per
      config section (~12 files).
- [ ] `ConfigManager` shrunk from 1584 → ~500 lines, 41 → ~12 methods.
- [ ] `apply_update` body is a registry loop, not a hand-rolled
      dispatch chain.
- [ ] `_KNOWN_BODY_KEYS` derived from the registry (Phase 2).
- [ ] Per-section tests live in `tests/server/config_appliers/` and
      test the appliers directly with no `ConfigManager` mocking.
- [ ] `tests/test_config_manager.py` keeps the lifecycle /
      orchestration tests, now focused on the thin coordinator.
- [ ] No behavior change — `apply_update`, `reload`, `save`,
      `check_file`, `toggle_drones`, `watch_mtime` all produce
      identical outputs for identical inputs (full pytest green).
- [ ] No new `Any` types, no new `# type: ignore` markers.
- [ ] Each phase committed as `release: X.Y.Z`.

---

## 8. Operator approval gate

This is the spec only. **No code changes have been made.** Before
implementation begins, confirm:

1. The 2-phase split is acceptable, or prefer a single combined PR.
2. The `ApplierDeps` dataclass is the right way to thread side-effect
   handles (vs. five individual params per applier).
3. Whether to start with Phase 1 immediately or defer.
