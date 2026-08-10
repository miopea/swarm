# Spec index

Design and audit documents for Swarm. **None of these is a status source.**
[`../../CHANGELOG.md`](../../CHANGELOG.md) is the authoritative record of what
shipped; the code is authoritative for how it works. A spec's job is to preserve
the *reasoning* — the decisions, the rejected alternatives, and the incidents
that forced them — which is the part the source cannot tell you.

Every shipped spec below carries a banner naming the deviations between what it
proposed and what landed. Read the banner before the body.

> **Housekeeping:** `docs/specs/` matches a `.gitignore` rule (line 23) but the
> files here are tracked, having been force-added. A new spec needs `git add -f`.

## Shipped

| Spec | What it covers | Watch out for |
|---|---|---|
| [`phase4-mcp-messaging.md`](phase4-mcp-messaging.md) | MCP server + inter-worker messaging (4.1–4.4) | Describes a separate `messages.db`; the table lives in the unified `swarm.db`. Its `related_specs` names `sqlite-unified-storage.md`, which is not in this repo. |
| [`playbook-synthesis-loop.md`](playbook-synthesis-loop.md) | Self-improving procedural memory — mining completed work into reusable playbooks | Operator-editability of `PlaybookConfig` via the dashboard was deliberately deferred. Its `related_specs` names `headless-queen-architecture.md`, which is not in this repo. |
| [`state-tracker-refactor.md`](state-tracker-refactor.md) | Breaking `WorkerStateTracker` into per-worker health detectors | The shipped detector filenames differ from the spec's proposals — read `src/swarm/drones/detectors/`. `ContextPressureCheck` / watcher duplication is an open follow-up. |
| [`config-manager-refactor.md`](config-manager-refactor.md) | Extracting `ConfigManager`'s 28 section appliers into `server/config_appliers/` | Range validators shipped as module-level functions in the applier modules, not `ConfigManager` methods. |
| [`daemon-god-object-refactor.md`](daemon-god-object-refactor.md) | Decomposing `SwarmDaemon` — invariants, playbook ops, task coordination, entry point | Line counts in the body are pre-refactor. Later passes extracted more than the three planned phases. |
| [`pipeline-detail-view.md`](pipeline-detail-view.md) | P3 of the editor UX series — per-step detail view + retry with forward cascade | The WS re-render does not preserve scroll position — the risk the spec itself flagged. |
| [`post-overhaul-cleanup.md`](post-overhaul-cleanup.md) | The five gaps deferred out of the P1–P6 UX series | The `test_ws_auth` flake's real root cause was **not** the one the spec guessed. |
| [`worker-asserted-active.md`](worker-asserted-active.md) | Replacing daemon-inferred `ACTIVE` with the worker-asserted `swarm_start_task` verb | AC-4 (a worker-asserted ACTIVE task is never demoted) is satisfied incidentally and has no test. |
| [`jira-integration-v2.md`](jira-integration-v2.md) | Multi-dev Jira scope: assignee routing, per-dev OAuth, workflow discovery, dry-run | **Three decisions differ from the code** — token storage, acceptance-criteria synthesis, and mid-flight reassignment. See its banner. |

## Audits (not rewrites — findings filed as follow-ups)

| Spec | What it covers |
|---|---|
| [`taskboard-state-machine-audit.md`](taskboard-state-machine-audit.md) | #1104 — reachability of every TaskBoard status exit. Note the headline finding was **corrected in place**; read past the strikethrough. Guarded by `tests/test_status_exit_reachability.py`. |

## Proposed — not built

Nothing in these has landed; no corresponding code exists.

| Spec | What it covers |
|---|---|
| [`managed-browser-v1.md`](managed-browser-v1.md) | A real browser for workers — deploy verification, JS-rendered docs, posting paths. Prerequisite for parts of the content system. |
| [`content-system-v1.md`](content-system-v1.md) | Extending Swarm from coding orchestration into "no-AI-slop" content orchestration. Four phases, explicitly to ship as separate releases. |

## Referenced but absent

These are cited by name from `project-notes.md`, the roadmaps, and some
`related_specs` blocks, and **do not exist in this repo** — they were written
while `docs/specs/` was fully gitignored and were never committed. The CHANGELOG
entries are the surviving record:

- `headless-queen-architecture.md` → "Headless Queen architecture close-out (task #253 follow-up)" and "Delete redundant 'Ask Queen' dashboard UI (task #253)"
- `native-loop-functions.md` → the `#761` / `#762` / `#765` entries under `## [2026.6.23]`
- `sqlite-unified-storage.md` → the unified-SQLite migration entries
