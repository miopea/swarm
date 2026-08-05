# Worker-asserted ACTIVE

Status: **specified, not built.** Scoped 2026-08-05 with the operator.
Precedes the #1104 audit deliberately — see "Relationship to #1104".

## The problem

`ACTIVE` is **inferred by the daemon, never asserted by the worker.**

Two callers reach `TaskBoard.activate()`: `start_task` (dispatch) and
`WorkerStateTracker._promote_one_assigned`. Neither is the worker. The promoter
picks the **most-recently-updated ASSIGNED task** on a `RESTING→BUZZING`
transition, and `activate()` **silently demotes** any other ACTIVE task for that
worker to ASSIGNED.

That combination produced #1159: parking a task stamped `updated_at`, which made
it the top candidate for immediate re-activation, and `activate()` then cleared
the `PARKED_TAG`, erasing the evidence — so it repeated every cycle. The
operator's report is the same defect from the outside: *"multiple tasks
crashing. We see that now and again."*

The board can therefore disagree with reality: a worker is working on task A
while the board says B, because B was touched more recently. Nothing detects
this, because from the daemon's point of view both transitions succeeded.

Existing machinery is **not** the gap. `activate()` is already the single
chokepoint, `_assert_no_double_active` self-heals double-ACTIVE on the way to
disk, and INV-1/INV-2 reconcilers run. All of it enforces *at most one ACTIVE
per worker*. None of it can know **which one is right**, because the only party
that knows never gets asked.

## The change

The worker asserts. The daemon stops guessing.

### 1. A new MCP verb — `swarm_start_task`

The worker calls it as its first action on a task. This is the only path to
`ACTIVE` for a worker's own work.

**It can refuse, and the refusal names what resolves it.** #1057 was filed
because a refusal withheld the resolving fact, so the message text is part of
the feature, not decoration:

```
swarm_start_task(1200)
  -> REFUSED: #1200 is assigned to project-root, not you.
     Your queue: #1104, #1264. Ask the Queen to reassign it.
```

Refuse when the task belongs to another worker, is BLOCKED, or is already
closed. Every accepted call writes `task_history`, so an assertion is auditable
and a *missing* assertion is visible by its absence — the same discriminator
that settled #1159 (`_promote_one_assigned` writes no history row; absence of
the row was the evidence).

### 2. The daemon stops inferring

`_promote_one_assigned` no longer activates on a `RESTING→BUZZING` transition.
Removing this is the point of the change; a "fallback" that re-adds it on a
timer rebuilds the race under a new name.

### 3. A worker-asserted task is never demoted

`activate()` currently demotes any other ACTIVE task for the worker. Once a
worker has asserted, the daemon demoting it is the daemon overruling the only
party that knows what is running.

A second task for a busy worker **stays ASSIGNED and queues**, dispatched on
that worker's next idle transition. This reuses the promotion plumbing that
already exists — it just stops overruling an assertion.

Cost, accepted knowingly: an urgent task waits behind a long inline
conversation. Priority-based preemption was offered and declined; it needs a
second dispatch path, and `urgent` is set by whoever files.

### 4. Don't dispatch to a worker doing inline work

A worker talking to the operator is `BUZZING` but unavailable. Detect it with
the **existing** signal: `proc.set_terminal_active()` / `proc.mark_user_input()`,
already used by `continue_all` and `send_all` to skip operator-occupied workers,
already tested.

No new state. A new `busy_with_operator` flag was rejected because a stuck flag
means a worker silently receives no work again — a new state with no reconciler,
which is the #1104 pattern.

Known limitation: misses inline work begun before the terminal was attached.

### 5. Dispatched-but-never-asserted: do nothing

**No auto-activate. No nudge.** The task stays ASSIGNED, the dashboard shows
ASSIGNED, and that is *true*.

This is the operator's call and it is the sharper design. Auto-activation is the
inference being removed. A nudge is also a form of healing — and nudging about a
task that is legitimately waiting its turn treats a healthy queue as a stall,
which is how operators learn to ignore nudges. Repeated nudges on unactionable
state is exactly the failure `swarm_block_on_operator` was created to stop.

Accurate state **is** the mechanism. A forgotten task sits, visibly, until
someone looks at the board — and the board is not lying, which is the property
that was missing.

## Scope guard

**In:** entering/leaving `ACTIVE`, the dispatch gate, the new verb, and removing
the promoter's inference.

**Out, reserved for the #1104 audit:** BLOCKED exits, BACKLOG, operator-action
states, park/unpark. Watch the boundary — the ACTIVE cell touches reconcilers
that also serve other states, and `_recon_inv1` / `_recon_inv2` /
`reconcile_active_per_worker` will need reading even though they are not being
changed.

**Do not "fix" `board.block_for_operator`.** Its ACTIVE-only precondition is
correct — it is the Queen's auto-park path, where "no longer ACTIVE" legitimately
means the stall resolved. The worker-facing `swarm_block_on_operator` routes
through `block_on_external`, which already accepts ASSIGNED. Do not collapse
them.

## Relationship to #1104

#1104 is an audit of all 41 TaskBoard verbs against six properties, scoped
explicitly so that *fixes are follow-ups*. That separation was chosen because
filing another patch would have been "the fifth patch-shaped response to a
pattern that needs a non-patch-shaped one".

This spec reverses that ordering **on the operator's decision**, for a stated
reason: the ACTIVE cell is the one the audit would find first, it is causing
live pain, and the audit is easier to judge with one verb already done right as
a reference. #1104 remains approved and follows.

It is worth being honest that this is building on unaudited ground, which is why
the scope guard above is narrow.

## Acceptance criteria

1. A worker can assert ACTIVE via `swarm_start_task`, and the transition writes
   `task_history`.
2. The verb refuses a task owned by another worker, a BLOCKED task, and a closed
   task — each refusal naming what would resolve it.
3. `_promote_one_assigned` no longer activates a task. Demonstrated by driving a
   `RESTING→BUZZING` transition with an ASSIGNED task present and showing it
   stays ASSIGNED.
4. A worker-asserted ACTIVE task is not demoted when a second task is dispatched
   to that worker; the second stays ASSIGNED.
5. A worker with an active terminal and recent user input is not dispatched to.
6. A dispatched-but-unasserted task produces **no** nudge and **no**
   auto-activation, and reads as ASSIGNED on the board.
7. #1159's specific sequence cannot recur: park a task, drive a
   `RESTING→BUZZING` transition, and show it stays parked.
8. The existing INV-1 guarantees still hold — `_assert_no_double_active` and
   both reconcilers unchanged and passing.

## Rejected alternatives, with reasons

| Option | Why not |
| --- | --- |
| First-tool-call marks ACTIVE | Still inference, just better inference. A worker doing inline work also makes tool calls, and it cannot say which task the call belongs to. |
| Verb preferred, hook as backstop | Two paths to one transition — the thing #1104 exists to audit. |
| Auto-activate after a grace period | Rebuilds the inference path on a timer. #1159 with a delay. |
| Revert to UNASSIGNED and re-dispatch | Steals work from a worker about to start it; invites thrash. |
| Route a queued task to another idle worker | May hand work to a worker that does not own the repo — the #1068/#1085 shape. |
| `busy_with_operator` flag | New state with no reconciler; a stuck flag silently starves a worker. |
| Succeed-and-self-assign | Silent cross-worker theft; ownership stops being meaningful. |
| Boolean refusal | #1057 verbatim — a refusal that withholds the resolving fact. |
