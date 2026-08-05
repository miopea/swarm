# TaskBoard state-machine audit (#1104)

Completed 2026-08-05. **An audit, not a rewrite** — fixes are filed as
follow-ups. Guarded by `tests/test_status_exit_reachability.py`.

Seven instances motivated this: #1059 (assign/no release), #1060 (create/no
edit), #1070 (block/no unblock), the hub BLOCKED-internal→external cell, #1159
(verb succeeds and does nothing), #1237 (every exit falsifies), and the HOLD
class (edit verb structurally unreachable).

## The headline finding

**BLOCKED's only non-falsifying exit is dead code.**

`TaskBoard.unblock` (`board.py:405`) exists, works, and is covered by
`tests/test_board.py`. It is called by **nothing** in `src/`. So the exit set is
non-empty *and* contains an honest member *and* the honest member cannot be
invoked from either surface. The only exit any surface can reach is
`force_complete`, which records DONE for work that is still open.

Two comments in the codebase assert otherwise:

- `board.py:599` — *"BLOCKED is already off-active and has its own unblock verbs"*
- `queen_handlers/_tasks.py:45` — *"a BLOCKED task must be unblocked first"*

Both instruct the reader to do something no surface permits.

**Why every prior test missed it.** Unit tests on the board prove the
*transition*. Property (b) is about *reachability*, which no board-level test can
see. `test_board.py` passes and the verb is unreachable — the same shape as a
green check measuring nothing.

## Enumeration

`TaskStatus` has 7 members. Primitive transitions live on `SwarmTask`; the board
verbs wrap them.

| status | entered by | left by (board) | honest exit? | reachable? |
| --- | --- | --- | --- | --- |
| BACKLOG | `reopen`, task creation | `approve_task`→UNASSIGNED, `reject_task`→FAILED, `assign`→ASSIGNED | yes | yes |
| UNASSIGNED | `unassign`, `approve`, `release`, `unassign_worker`, INV reconcilers | `assign`→ASSIGNED, `release` | yes | yes |
| ASSIGNED | `assign`, `unblock`, `park`, `reopen_for_verifier`, `activate` demote, `_recon_inv1/2` | `activate`→ACTIVE, `complete`→DONE, `unassign`→UNASSIGNED, `block_on_external`→BLOCKED | yes | yes |
| ACTIVE | `activate` (2 callers) | `complete`, `park`, `unassign`, `block_on_external`, `block_for_operator` | yes | yes |
| **BLOCKED** | `block_on_external`, `block_for_operator`, `_recon_inv2` | `unblock`→ASSIGNED **(0 callers)**, `force_complete`→DONE *(falsifies)* | yes | **NO** |
| DONE | `complete`, `force_complete` | `reopen`→BACKLOG, `release` | yes | yes |
| FAILED | `fail`, `reject` | `reopen`→BACKLOG, `release` | yes | yes |

## Property assessment

| property | result |
| --- | --- |
| (a) exit set non-empty | **PASS** — all 7 statuses |
| (b) reachable from both surfaces | **FAIL** — BLOCKED (see above) |
| (c) achievable without falsifying history | **PASS at board level**, FAIL in practice for BLOCKED, because the only *reachable* exit falsifies |
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
