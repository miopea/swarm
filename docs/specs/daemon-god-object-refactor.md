# SwarmDaemon Refactor — Spec

**Audit finding**: #1 (MAJOR — god object)
**Source**: `/audit-code` 2026-05-26 report
**Status**: draft — operator approval needed before code lands

---

## 1. Problem

`src/swarm/server/daemon.py` is **3392 lines**. `SwarmDaemon` itself
is the top ~2675 lines / ~140 methods. The remaining ~700 lines are
module-level entry-point code (`run_daemon`, `run_test_daemon`,
`_print_banner`, lock + systemd helpers) that has nothing to do with
the class but lives in the same file.

The class today is the **post-extraction** version — over the past
months `BroadcastHub`, `BackgroundLoopRunner`, `EscalationHandler`,
`StatePublisher`, `ProposalCoordinator`, `ResourceMonitor`,
`EmailService`, `TaskManager` (CRUD-only), `ConfigManager`,
`WorkerService`, `JiraService`, `TestRunner`, `TunnelManager`,
`ProposalManager`, and `QueenAnalyzer` have all been peeled off
into their own modules. What's left in `SwarmDaemon` is still
big because **task lifecycle operations** never made the cut.

### Remaining concerns inside SwarmDaemon

| Concern | Methods | Approx lines | Why it's still here |
|---------|---------|--------------|---------------------|
| **Construction wiring** | `__init__`, `_build_notification_bus`, `_build_graph_manager`, `_build_jira_token_manager`, `_rebuild_graph`, `_rebuild_jira`, `init_pilot` | ~420 | The orchestrator's natural home — but bloated by 17 inline service-construction blocks. |
| **Task lifecycle ops** | `assign_task`, `start_task`, `complete_task`, `_maybe_seed_goal`, `_spawn_handoff_task`, `assign_and_start_task`, `_auto_start_next_assigned`, `_auto_resolve_attention_for_task`, `_check_ownership`, `_require_task`, `unassign_task`, `reopen_task`, `fail_task`, `remove_task`, `edit_task`, `create_cross_task`, `approve_cross_task`, `reject_cross_task`, `create_task_smart`, `create_task` | ~600 | Touches `task_board`, `worker_svc`, `proposals`, `analyzer`, `pilot`, `email`, `playbook_synthesizer` — every coordinator at once. |
| **Playbook recall + attribution** | `_fire_playbook_synthesis`, `_recall_playbooks_for_task`, `_attribute_playbook_outcome`, `_log_verifier_skip`, `_record_completion_verdict`, `_consolidate_learnings` | ~250 | Glue between `playbook_synthesizer` / `playbook_consolidator` / `queen_chat` / `task_board`. |
| **Worker operation proxies** | `send_to_worker`, `continue_worker`, `interrupt_worker`, `escape_worker`, `force_rest_worker`, `arrow_*` (4), `redraw_worker`, `capture_worker_output`, `safe_capture_output`, `discover`, `poll_once`, `launch_workers`, `spawn_worker`, `sleep_worker`, `kill_worker`, `revive_worker`, `kill_session` | ~85 | 18 one-line delegates to `worker_svc`. |
| **Invariant reconciliation** | `_reconcile_active_per_worker`, `_working_workers`, `_blocked_task_ids`, `_run_invariant_reconciliation` | ~100 | Reads `task_board` + `workers`, writes `task_board.assign/unassign`. Self-contained. |
| **Event handlers** (`_on_*`) | `_on_escalation`, `_on_task_done`, `_on_park_proposal`, `_on_workers_changed`, `_on_oversight_alert`, `_on_operator_terminal_approval`, `_on_task_assigned`, `_on_state_changed`, `_on_drone_entry`, `_on_tunnel_state_change`, `_on_queen_queue_status_change`, `_on_task_board_changed`, `_on_pipeline_change` | ~150 | The glue between sub-component events and the various coordinators. |
| **Misc small ops** | `_check_context_pressure` (4-arg notification emitter, distinct from the per-worker watcher), `_cleanup_file_locks`, `_install_worker_artifacts`, `_write_worker_mcp_configs`, `_update_file_ownership`, `_handle_queen_claude_md_reconcile`, `_check_for_updates`, `_accumulate_task_costs`, `_track_task`, `_get_worker_state`, `_worker_descriptions`, `_worker_task_map`, `_collect_worker_pids`, `_handle_resource_snapshot`, `get_resource_snapshot`, `push_notification`, `_broadcast_queen_health` | ~400 | Each justifiable individually; cumulative weight is real. |
| **Backward-compat shims** | `_heartbeat_task`/setter, `_usage_task`/setter, `_mtime_task`/setter, `_set_loop_task`, `_state_dirty`/setter, `_state_debounce_handle`/setter, `_state_debounce_delay`/setter, `apply_config`, `reload_config`, `_watch_config_mtime`, `toggle_drones`, `check_config_file`, `apply_config_update`, `save_config` | ~120 | Property delegates to the already-extracted services. Tests depend on them. |
| **Module-level entry-point code** (not class methods) | `run_daemon`, `run_test_daemon`, `_print_banner`, `_print_test_banner`, `_wire_test_console`, `_read_lock_pid`, `_pid_alive`, `_acquire_daemon_lock`, `_maybe_patch_systemd_unit`, `_exec_restart`, `_strip_config_flag`, `_clear_pycache`, `_reachable_addresses`, `_db_ground_truth_counts`, `console_log` | ~700 | Server entry point — has no good reason to live in `daemon.py`. |

The cost shows up two ways:

1. **`daemon.py` is the slowest file to load in this codebase** (3392 lines of Python, ~140 method definitions on one class, plus all the module-level wiring). IDE jump-to-def + grep-for-method-X searches are noticeably slower.

2. **Tests for task ops have to stand up a full daemon.** A test for `complete_task`'s playbook-attribution behavior pays the cost of constructing `Queen` + `QueenAnalyzer` + `PipelineEngine` + 15 other subsystems just to exercise the 40-line completion path. The existing `TaskManager` only owns CRUD (`edit_task`, `fail_task`, etc.); the lifecycle path that touches workers, proposals, playbooks, and email never moved.

---

## 2. Goals & non-goals

### Goals

- **Reduce `daemon.py` line count by ≥40%** so navigation, grep, and
  IDE responsiveness improve materially.
- **Extract task-lifecycle operations into a `TaskCoordinator`** that
  takes the same dependency handles as the existing coordinators
  (`ProposalCoordinator`, `EmailService`, etc.) — `task_board`,
  worker callbacks, `playbook_synthesizer`, `analyzer`, etc.
- **Move module-level entry-point code out of `daemon.py`** into a
  new `swarm/server/runner.py` (or absorb into `webctl.py`) so the
  file is just the class.
- **Preserve every public API** the dashboard routes, MCP tools,
  and tests depend on. The class's exposed surface
  (`daemon.send_to_worker`, `daemon.complete_task`, etc.) stays;
  internal implementation can move freely.
- **No behavior change.** Same event emissions, same side effects,
  same error paths. The diff is moves + one mediator class +
  property/method shims for backward compat.

### Non-goals (deliberate)

- **Not removing the worker-operation proxies** (`continue_worker`,
  `interrupt_worker`, etc.). They're 18 one-line delegates to
  `worker_svc`. Removing them would force every caller to switch to
  `daemon.worker_svc.X` — wide blast radius for low gain. They stay
  as backward-compat surface.
- **Not collapsing the backward-compat property shims** for the
  background loops (`_heartbeat_task`, etc.). They cost ~60 lines and
  unblock the test suite without rewriting fixtures.
- **Not rewriting `__init__`'s service-construction logic.** Trimming
  it would only be cosmetic; it's already ~all dependency wiring.
  A future spec can pull each subsystem's construction into its own
  builder if the wiring shape causes more pain.
- **Not introducing a façade pattern** to hide the
  `daemon.tasks` + `daemon.proposals` + `daemon.publisher` +
  `daemon.tasks_coord` proliferation. The current attribute-per-
  service shape is readable and grep-friendly; a `daemon.svc.tasks`
  re-routing would obscure more than it hides.

---

## 3. Target shape

```
src/swarm/server/
├── daemon.py                (~1800 lines after extraction)
│   └── class SwarmDaemon
│       ├── __init__                       (still big — service wiring)
│       ├── start / stop / init_pilot
│       ├── event handlers (_on_*)
│       ├── construction helpers (_build_*, _rebuild_*)
│       ├── worker op proxies              (one-liners to worker_svc)
│       ├── task op proxies                (NEW: one-liners to tasks_coord)
│       ├── misc small helpers
│       └── backward-compat shims
├── runner.py                              (NEW — ~700 lines)
│   ├── run_daemon / run_test_daemon
│   ├── _print_banner / _print_test_banner
│   ├── _read_lock_pid / _pid_alive / _acquire_daemon_lock
│   ├── _maybe_patch_systemd_unit
│   ├── _exec_restart / _strip_config_flag / _clear_pycache
│   ├── _reachable_addresses / _db_ground_truth_counts
│   ├── _wire_test_console / console_log
│   └── _DAEMON_LOCK_PATH module constant
├── task_coordinator.py                    (NEW — ~600 lines)
│   └── class TaskCoordinator
│       ├── __init__(deps: TaskCoordinatorDeps)
│       ├── assign_task / start_task / assign_and_start_task
│       ├── complete_task / _spawn_handoff_task / _maybe_seed_goal
│       ├── _auto_start_next_assigned / _auto_resolve_attention_for_task
│       ├── _check_ownership
│       ├── _fire_playbook_synthesis / _recall_playbooks_for_task
│       ├── _attribute_playbook_outcome / _log_verifier_skip
│       ├── _record_completion_verdict / _consolidate_learnings
│       └── _send_completion_reply / retry_draft_reply
└── invariants.py                          (NEW — ~100 lines)
    └── class InvariantReconciler
        ├── working_workers / blocked_task_ids
        └── reconcile_active_per_worker / run(reason)
```

### TaskCoordinator dependency contract

```python
# swarm/server/task_coordinator.py
@dataclass
class TaskCoordinatorDeps:
    """Side-effect handles TaskCoordinator needs.

    Mirrors the ProposalCoordinator dependency-bundle pattern —
    callbacks instead of full subsystem references so the
    coordinator can be tested without a fully-wired daemon.
    """

    task_board: TaskBoard
    task_history: TaskHistory | SqliteTaskHistory
    drone_log: DroneLog
    notification_bus: NotificationBus
    blocker_store: BlockerStore
    worker_svc: WorkerService
    proposals: ProposalManager
    proposal_coord: ProposalCoordinator
    email: EmailService
    playbook_synthesizer: PlaybookSynthesizer
    queen_chat: QueenChatStore
    config: HiveConfig
    get_pilot: Callable[[], DronePilot | None]
    get_analyzer: Callable[[], QueenAnalyzer]
    emit: Callable[..., None]
    broadcast_ws: Callable[[dict[str, Any]], None]
    push_notification: Callable[..., None]
    invariants: InvariantReconciler
    track_task: Callable[[asyncio.Task[object]], None]
```

`SwarmDaemon.__init__` builds the deps bundle once; every existing
public method (`daemon.assign_task`, `daemon.complete_task`, etc.)
becomes a one-line proxy:

```python
async def assign_task(self, *args, **kwargs):
    return await self.tasks_coord.assign_task(*args, **kwargs)
```

---

## 4. Test impact

- `tests/test_daemon.py` (large) — most tests construct
  `SwarmDaemon` directly. The lifecycle / event-handler tests stay
  unchanged because the public methods still exist on the daemon
  (proxied to `tasks_coord`).
- Tests that reach into private task-op methods (e.g.
  `daemon._spawn_handoff_task(...)`) — pick **(a)** add a backward-
  compat shim on the daemon that delegates, or **(b)** update the
  test to call `daemon.tasks_coord._spawn_handoff_task(...)`. Each
  case is a 5-character edit; preference is (a) so the test churn
  stays at zero.
- New tests can hit `TaskCoordinator(deps=fake_deps())` directly,
  paying the cost of constructing only `task_board` + the few
  callbacks the path under test actually needs.

---

## 5. Migration plan

The work splits cleanly into two distinct phases (different risk
profiles, different shipping rhythms).

### Phase 1 — runner extraction + invariants extraction (low-risk, mechanical)

Two complementary moves bundled as one release:

**1a. Move entry-point code to `swarm/server/runner.py`.**
- `run_daemon`, `run_test_daemon`, `_print_banner` (171 lines!),
  `_print_test_banner`, `_wire_test_console`, lock helpers
  (`_read_lock_pid`, `_pid_alive`, `_acquire_daemon_lock`),
  systemd patcher, `_exec_restart`, `_strip_config_flag`,
  `_clear_pycache`, `_reachable_addresses`, `_db_ground_truth_counts`,
  `console_log`, and `_DAEMON_LOCK_PATH` module constant.
- Update import sites: `swarm/cli.py` (calls `run_daemon`/`run_test_daemon`),
  `tests/test_daemon.py`, anywhere else grep finds.
- ~700 lines removed from `daemon.py`. The class is untouched.

**1b. Extract invariant reconciliation to `swarm/server/invariants.py`.**
- `InvariantReconciler` class with `working_workers`,
  `blocked_task_ids`, `reconcile_active_per_worker`, `run(reason)`.
- Daemon's `_run_invariant_reconciliation` becomes a one-line proxy.
- Small, focused, ~100 lines.

**Estimated diff**: ~800 removed / ~830 added (~5% of which is
new file headers). 2 new source files, 0 broken tests.

### Phase 2 — TaskCoordinator extraction (the big one)

Move every task-lifecycle method into `TaskCoordinator`. Daemon
keeps the public method surface (backward-compat proxies) and the
event handlers that route into them.

Steps:
1. Create `src/swarm/server/task_coordinator.py` with the
   `TaskCoordinatorDeps` dataclass and `TaskCoordinator` class.
2. Move each lifecycle method as-is — `assign_task`, `start_task`,
   `complete_task`, `_maybe_seed_goal`, `_spawn_handoff_task`,
   `assign_and_start_task`, `_auto_start_next_assigned`,
   `_auto_resolve_attention_for_task`, `_check_ownership`, and the
   playbook recall/attribution methods. Replace `self._xxx` with
   `self._deps.xxx` or `self._xxx` (where the method is moving
   alongside its caller).
3. Add daemon-side proxy shims for the public methods
   (`daemon.assign_task` etc.) so external callers don't change.
4. Wire `TaskCoordinator` in `SwarmDaemon.__init__` next to the
   other coordinators.
5. Move `complete_task`-specific tests to a new
   `tests/server/test_task_coordinator.py` where they can construct
   the coordinator directly. Lifecycle integration tests stay in
   `tests/test_daemon.py`.
6. `/check`. Ship.

**Estimated diff**: ~700 added / ~600 removed. 1 new source file +
1 new test file. ~10–20 tests migrated.

**Risk**: medium. `complete_task` is 115 lines and touches the most
subsystems of any method in the codebase. Verifying behavior parity
needs careful read-through; the existing test coverage is the
backstop.

### Out-of-scope (separate follow-ups)

- **`__init__` slim-down.** Move each subsystem's construction
  into its own `_build_xyz()` method to chunk the init pass. Pure
  cosmetic; defer until the wiring shape causes pain.
- **Worker-operation proxy cleanup.** 18 one-liners that could
  become a `__getattr__` redirector to `worker_svc`. YAGNI for now.
- **Collapsing the `_on_*` event handlers.** Their job is the glue
  routing between subsystems; consolidating them into a registry
  would obscure data flow rather than improve it.

---

## 6. Risks

| Risk | Mitigation |
|------|-----------|
| Phase 2's `TaskCoordinator` swallows methods that hold subtle daemon-state side effects (e.g. dirty-state debounce flips, broadcast-state triggers) | Keep `_mark_state_dirty` / `broadcast_ws` callable in `TaskCoordinatorDeps`; every move includes a self-audit pass for the side-effect set. Full test suite passes as the contract. |
| `runner.py` imports daemon and daemon imports runner (cycle) | Runner imports the daemon class; daemon does NOT import runner. The entry points construct `SwarmDaemon` directly. Verified by grep before commit. |
| Test patches at the old module path (`patch("swarm.server.daemon.run_daemon")`) break | Re-export `run_daemon` from `swarm.server.daemon` with a deprecation note for one release cycle. Same trick used for the ConfigManager shims. |
| Phase 1 lands cleanly but Phase 2 is too big to review in one PR | Phase 2 can sub-divide further (e.g. just `complete_task` + playbook attribution first, then `assign/start/handoff` second). |

---

## 7. Definition of done

- [ ] `daemon.py` shrunk from 3392 → ≤2000 lines (target ~1800).
- [ ] `swarm/server/runner.py` exists with the entry-point code.
- [ ] `swarm/server/invariants.py` exists with `InvariantReconciler`.
- [ ] `swarm/server/task_coordinator.py` exists with
      `TaskCoordinator` + `TaskCoordinatorDeps`.
- [ ] Every public daemon method that moved keeps a backward-compat
      shim so existing callers (routes, tests, MCP handlers) don't
      change.
- [ ] No behavior change — full pytest green, same warning count
      (4605 → 4605).
- [ ] No new `Any` types, no new `# type: ignore` markers.
- [ ] Each phase committed as `release: X.Y.Z`.

---

## 8. Operator approval gate

This is the spec only. **No code changes have been made.** Before
implementation begins, confirm:

1. The 2-phase split is acceptable, or prefer a single bundled
   release like the ConfigManager refactor.
2. Whether to include the daemon shims for tests, or rewrite the
   tests in lock-step.
3. Whether to start with Phase 1 immediately or defer.
