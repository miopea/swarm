# TaskBoard state-machine audit (#1104)

Completed 2026-08-05. **An audit, not a rewrite** — fixes are filed as
follow-ups. Guarded by `tests/test_status_exit_reachability.py`.

Seven instances motivated this: #1059 (assign/no release), #1060 (create/no
edit), #1070 (block/no unblock), the hub BLOCKED-internal→external cell, #1159
(verb succeeds and does nothing), #1237 (every exit falsifies), and the HOLD
class (edit verb structurally unreachable).

## The headline finding — CORRECTED 2026-08-05

**What this document originally claimed, and it was WRONG:** "BLOCKED's only
non-falsifying exit is dead code."

**What is true:** `board.release` accepts BLOCKED — its only guards are DONE/FAILED
and already-ownerless — and the Queen reaches it through `queen_reassign_task`
(`mcp/queen_handlers/_tasks.py:328`), whose own inline comment says *"board.release
accepts any holdable status"*. #1059 filed and fixed exactly this. **So the Queen
could always move a BLOCKED task without falsifying history, and the operator was
never stuck.**

**How the error happened.** The precondition-extraction script used to build the
table below listed `release`'s *refusal* set (DONE, FAILED, UNASSIGNED) in the
*accepts* column. I read that as "release rejects BLOCKED" and never opened the
method. The same script had already misled me about `park`; I caught that one by
reading, wrote below that I had therefore confirmed every interesting cell by
reading — and then did not read this one. **The claim in the (e) section that every
interesting cell was read was itself false when written.**

**The two gaps that were real**, and what #1268 fixed:

1. **No worker-surface exit from BLOCKED at all.** The worker that declared the
   blocker could not clear it. This is what sculpt-studio hit on #1237.
2. **No owner-preserving exit from either surface.** `release` drops the owner, so
   "the wait ended, resume where you left off" required reassigning the task back
   to the same worker.

Both closed by #1268: `swarm_unblock_task` (worker) and `queen_unblock_task`
(Queen), sharing one audit + BlockerStore-clearing helper so the surfaces cannot
drift.

**One comment I wrongly accused.** `board.py:599` says BLOCKED "has its own unblock
verbs (#1059 `release`)" — **that is CORRECT** and needed no change. The comment
that was genuinely wrong is `queen_handlers/_tasks.py:45`, which claimed
`queen_reassign_task` "does NOT move a BLOCKED task … must be unblocked first",
contradicting its own implementation 280 lines below. **That** is what misled the
Queen, and through her the operator, about #1237. Corrected in #1268.

## Enumeration

`TaskStatus` has 7 members. Primitive transitions live on `SwarmTask`; the board
verbs wrap them.

| status | entered by | left by (board) | honest exit? | reachable? |
| --- | --- | --- | --- | --- |
| BACKLOG | `reopen`, task creation, **`assign` (owner set, status kept)** | `approve_task`→UNASSIGNED, `reject_task`→FAILED, `activate`→ACTIVE *(worker, `unpark=true`, #1636)* | yes | yes |
| UNASSIGNED | `unassign`, `approve`, `release`, `unassign_worker`, INV reconcilers | `assign`→ASSIGNED, `release` | yes | yes |
| ASSIGNED | `assign`, `unblock`, `park`, `reopen_for_verifier`, `activate` demote, `_recon_inv1/2` | `activate`→ACTIVE, `complete`→DONE, `unassign`→UNASSIGNED, `block_on_external`→BLOCKED | yes | yes |
| ACTIVE | `activate` (2 callers) | `complete`, `park`, `unassign`, `block_on_external`, `block_for_operator` | yes | yes |
| BLOCKED | `block_on_external`, `block_for_operator`, `_recon_inv2` | `release`→UNASSIGNED *(drops owner)*, `unblock`→ASSIGNED *(keeps owner, #1268)*, `force_complete`→DONE *(falsifies)* | yes | yes |
| DONE | `complete`, `force_complete` | `reopen`→BACKLOG, `release` | yes | yes |
| FAILED | `fail`, `reject` | `reopen`→BACKLOG, `release` | yes | yes |

## Property assessment

| property | result |
| --- | --- |
| (a) exit set non-empty | **PASS** — all 7 statuses |
| (b) reachable from both surfaces | **PASS as of #1268.** Originally reported FAIL for BLOCKED on a false premise — `release` was always Queen-reachable. The real gaps were no worker-surface exit and no owner-preserving exit; both now closed. |
| (c) achievable without falsifying history | **PASS.** `release` never falsified; `unblock` now adds the owner-preserving route. |
| (d) transitions between distinct causes of one state | **FAIL** — see below |
| (e) no gate is racy rather than strict | **PASS** — see below |
| (f) no silent undo | **PASS** — now; it failed before this audit's sibling change |
| (g) reachable for every task CLASS, not just status | **FAIL** — the HOLD class |

### (d) BLOCKED is reachable by two distinct causes with no transition between them

`block_on_external` (waiting on an upstream artefact) and `block_for_operator`
(waiting on a human decision) both land in BLOCKED and are semantically
different — the board surfaces `awaiting-operator` separately. There is no verb
to move between them. This is the hub cell from 2026-07-30, closed via #1130 as
overtaken by the Hub backend fold-in and therefore **never closed on its
merits**. It remains a real gap.

### (e) PASS, and a correction to my own first pass

`park` originally required ACTIVE, while the INV-2 reconciler demotes
ACTIVE→ASSIGNED on every RESTING transition (27 times in 10h on #1158) — a
seconds-wide window outside the caller's control. #1159 relaxed it to
`(ACTIVE, ASSIGNED)` and that is still in place (`board.py`, verified by
reading).

**My extraction script reported `park` as ACTIVE-only** because its heuristic
binned ASSIGNED as a "set" rather than an "accept". I caught it by reading the
method. Recording it because the same heuristic produced the table above, so
every interesting cell here was confirmed by reading rather than by the script.

### (f) PASS — but only as of today

`board.activate()` now has exactly two callers, and **both write
`task_history`**:

| caller | history |
| --- | --- |
| `mcp/handlers/_start.py` (worker-asserted start) | `TaskAction.STARTED` |
| `server/task_coordinator.py` `_activate_with_history` | yes |

Before the worker-asserted-ACTIVE change,
`WorkerStateTracker._promote_one_assigned` was a third caller that wrote
**nothing** — which is exactly what made #1159 diagnosable: absence of a history
row discriminated write-failed from write-reverted. That caller no longer
activates at all.

### (g) FAIL — the HOLD class

`swarm_edit_task` requires assignment. HOLD tasks are unassigned **by design** —
that is the mechanism preventing auto-dispatch. Therefore no worker can correct
the description of any HOLD task. Verified live on #1104 and #1018, verbatim:
*"Task #1104 is unassigned — swarm_edit_task only corrects a task assigned to
you."*

Neither verb is individually wrong; the gap exists only in composition. The
class that loses the edit verb is the one whose premises rot most, because HOLDs
sit longest — both edits needed on 2026-08-05 existed *because* a HOLD had gone
stale, and #1128 had to be closed outright once its architecture no longer
existed. The Queen surface is reachable (`queen_edit_task` applied both); the
worker surface is not.

### (g) second instance — the ASSIGNED-and-BACKLOG class (#1636, fixed 2026-08-14)

The same shape as the HOLD gap above, found nine days later, and it is the reason
this section says *class* rather than *status*. A task can be BACKLOG **and** owned:
since 2026-08-07 `assign` sets the owner and deliberately KEEPS backlog status
("Backlog means parked, not for now; promoting it would un-park it"). Its safety
argument — no dispatch path accepts BACKLOG, so an owned backlog task is inert — is
sound for *starting*, and says nothing about *finishing*.

So a worker who completed the work met two refusals that were each individually
correct and jointly a dead end: `swarm_complete_task` requires in-progress,
`swarm_start_task` requires not-backlog, and nothing moved it between them. Neither
of `swarm_complete_task`'s documented hatches applied — the task was too assigned for
the unassigned-self-close hatch and not blocked enough for the blocked one. Hit on
sculpt-studio #1304; the operator force-completed it, and `queen_edit_task` takes no
status argument, so the manual Queen route did not exist either.

**Measured when filed: 4 of 4 BACKLOG rows carried an owner** (1589 tasks total). The
dead-end state was not an edge case; it was the only form BACKLOG took.

Fixed by making `activate` reachable for BACKLOG from the worker surface, gated on
explicit `unpark=true` — the consent word that already exists for the HOLD park.
BACKLOG stays out of `_startable`, so the bare call and the ambiguity list still
cannot reach one and nothing auto-starts it; the 2026-08-07 decision is preserved
rather than reverted.

**The audit itself was wrong here and the tests could not tell.** The BACKLOG row
listed `assign`→ASSIGNED as one of three exits. That stopped being true on
2026-08-07 and the table was never updated — property (a) only asks that the exit
set be non-empty, and the two remaining exits satisfied it. An enumeration can rot
into describing a transition that no longer exists while every property still
passes. Both the row and `_BOARD_EXITS` in the test now say `activate`.

### AC-6: `block_for_operator` is CORRECT as ACTIVE-only

Confirmed, and pinned by test. It is the Queen's auto-park path, where "no
longer ACTIVE" legitimately means the stall resolved. The worker-facing
`swarm_block_on_operator` routes through `block_on_external`, which already
accepts ASSIGNED. **Do not collapse them.**

## Filed follow-ups

| gap | property | follow-up |
| --- | --- | --- |
| `unblock` unreachable from both surfaces | (b), (c) | **#1268** (high) |
| No transition between BLOCKED's two causes | (d) | **#1269** (HOLD) |
| Edit verb unreachable for the HOLD class | (g) | **#1270** (HOLD) |

#1269 is sequenced after #1268 in practice — whatever verb clears BLOCKED is the
natural place to re-enter with a different cause, so building it first would
likely be rework.

## Two things NOT to change

1. `block_for_operator`'s ACTIVE-only precondition (above).
2. `park`'s `(ACTIVE, ASSIGNED)` precondition — narrowing it re-opens the INV-2
   race.

## What the guarding test does and does not prove

`tests/test_status_exit_reachability.py` asserts reachability, not transition
correctness. The BLOCKED cell is `xfail(strict=True)`: a new status with no exit
fails immediately, and *fixing* BLOCKED also fails the test as "unexpectedly
passed", forcing the marker off. The gap cannot be silently closed or silently
forgotten.

Its reachability scan carries a **positive control** — it asserts it can see
verbs known to be wired up before any absence it reports is trusted. A source
scan that matches nothing would otherwise report "nothing reachable" and every
assertion would pass for the wrong reason.
