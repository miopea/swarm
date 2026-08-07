# Jira Integration v2 — multi-dev scope and sync semantics

Status: **specified, not built.** Decisions taken with the operator 2026-08-07.
Supersedes the ad-hoc behaviour described under "Current state" below.

## Why this exists

Jira sync is being turned on for **every dev**, each running their own Swarm. The
existing integration was built for one instance and one label, and does not survive
that: it routes by `labels = "swarm"`, so N devs would each import the same tickets,
create N duplicate tasks, and race to transition the same issue.

Three structural blockers were fixed on 2026-08-07 before this spec was written
(`2026.8.7.11`–`.14`). They are prerequisites, not part of this scope:

1. **Status transitions were dashboard-only.** The whole grid lived in
   `web/routes/tasks.py` with one caller, so a Jira sync would have had to duplicate
   it. Now `swarm/tasks/policy.py` (rule) + `TaskCoordinator.change_status`
   (execution).
2. **Import deduped against `all_tasks`,** which excludes archived rows — archiving a
   linked task and re-importing would silently create a second task for one ticket.
   Now `TaskBoard.known_jira_keys()`.
3. **Exports were fire-and-forget with no reconciliation.** `fire_jira` ignored the
   boolean return, so an export that ran and did not take was silent, and nothing ever
   compared the two systems. Now `tasks.jira_exported_status` + `reconcile_exports()`.

## Current state (measured, 2026-08-07)

| Fact | Value |
| --- | --- |
| Tasks on the board | 1260 |
| Linked to Jira | 25 |
| Created in last 7 days | 148 |
| **Opened and closed within 1 hour** | **858 (68%)** |
| `task_type = chore` | 1118 of 1260 |
| Auth | single OAuth app (`client_id`/`client_secret`/`cloud_id`) |
| Routing | `labels = "swarm" AND status NOT IN (Closed, …)` |
| Sync interval | 5 minutes |

The 68% figure drove the outbound-creation decision: two-way creation without a filter
would push ~21 tickets/dev/day into a shared tracker, two thirds closing the same hour.

A live reconcile on this data transitioned **14 real WWD tickets** and was refused on
11 (`no transition to 'Done' found for IS-10278 (available: ['Waiting for support'])` —
they are already closed in Jira). That refusal class is why setup must discover the
real workflow rather than assume `Done`.

---

## Decisions

### 1. Routing — the Jira assignee decides

A swarm imports only tickets **assigned to its owner**. No label, no JQL, no per-dev
label convention.

*Why:* uses semantics Jira already has and the team already practises. One answer to
"who owns this" in both systems, and nothing to remember. Rejected: per-dev labels (a
second ownership channel that can disagree with the assignee), a claim-on-import pool
(makes Jira a lock manager, and races are real), and a single designated syncing
instance (kills the per-dev autonomy that motivated this).

### 2. Identity — per-dev OAuth

Each dev authorises their own Swarm. Transitions are attributable to a person, and
Jira's own permissions apply per user: a dev cannot move a ticket their Swarm could not
move by hand.

Tokens live in **1Password**, per the existing standard — one rotation story, not two.
Rejected: a shared bot account (loses which dev's work caused a change, and needs broad
permissions on the credential you would least like leaked) and OS keyring (no central
revocation visibility).

### 3. Scope — whole projects, not filters

Sync is configured per **space/project**, and **more than one project may be added**.
Within a configured project, a ticket becomes a Swarm task when:

- it is **assigned to this dev**, and
- it is **not in a terminal state** (terminal is read from the discovered workflow, not
  a hardcoded list), and
- its issue type is **Story / Task / Bug** — **not Epic**.

Epics are containers, not work: a worker cannot finish one, and it would sit open for
months — the shape that produced stale-blocker problems. Sub-tasks come across as
ordinary tasks.

A ticket assigned to a dev in a project they have **not** configured is **ignored**
(they can add the project).

### 4. Outbound creation — operator-approved promotion only

Swarm tasks do **not** automatically become Jira tickets.

- **Workers may REQUEST** promotion; the request lands on the **existing
  proposals/decisions surface**, which already has an operator UI, notifications and an
  autonomous-window concept. A second inbox is a thing that eventually goes unwatched.
- **The operator approves**, from the task modal.
- **Never for closed work**: no history backfill of the ~1235 closed tasks, and no
  ticket for a task that is already closed when the rule evaluates. One rule covers
  both the historical set and the 858 short-lived tasks.

Assignment of a created ticket:
- created **by a worker via MCP** → assigned to **the dev whose swarm created it**, so
  the outbound rule and the assignee-routing rule agree and it round-trips home;
- created **from the operator modal** → the modal offers the full set of options.

### 5. Provenance — `swarm` becomes a reserved label

`swarm` no longer routes anything. It is **auto-applied to tickets Swarm created**, and
means exactly one thing: an agent raised this.

*The trap this avoids:* the old import filter was `labels = "swarm"`. If created
tickets carried that label while it still drove routing, Swarm would re-import its own
output as a new task — an echo loop. Separating "came from Swarm" (provenance) from
"route to Swarm" (assignee) makes the loop impossible rather than merely deduped
against.

Swarm does **not** label tickets it merely transitions — that would write to other
people's tickets on every sync.

### 6. Conflict — Jira owns status, Swarm owns execution

| System | Owns |
| --- | --- |
| **Jira** | whether the work is wanted: status/workflow state, assignee, priority, the ticket's own fields |
| **Swarm** | how it is executed: which worker, ACTIVE/BLOCKED, progress, blockers, resolution text |

They rarely contest the same field, so most "conflicts" disappear. Rejected:
last-writer-wins (unreasonable-about after the fact, and the loser is usually the slower
human, with nothing recording the overwrite).

**Reassignment mid-flight:** if a ticket is reassigned in Jira while a worker is
ACTIVELY on the task, the worker **finishes the current work, then hands off**. Killing
in-flight work loses context and can leave a half-done change; Jira's reassignment is
about who owns it *next*. The losing swarm stops importing it afterwards.

### 7. Setup — discover the space, confirm the mapping

On enabling, the dev selects the Jira **space/project**, and Swarm **discovers that
project's real workflow states and transitions** and proposes a status map for
confirmation.

*This is the direct fix for the 11 refusals.* The hardcoded map said `Done` while the
IS project's workflow offered only `Waiting for support`, and nothing could tell until
it failed repeatedly. Discovery also survives a workflow change, where a hand-typed map
silently rots. Confirmation is required because `Done` / `Resolved` / `Closed` are
rarely interchangeable and a wrong automatic choice writes to real tickets.

### 8. First sync — dry-run, then explicit go

Enabling the integration **never writes**. The first reconcile produces a report of what
it *would* change; nothing is written until confirmed.

On 2026-08-07 a schema change made 25 tasks look unacknowledged and the reconciler
transitioned 14 real tickets before anyone looked. A settings toggle must not have a
shared tracker as its blast radius.

### 9. Unreachable transitions — surface once, decide once

When the discovered workflow has no transition to the mapped status, the divergence is
**reported once to the operator** as needing a decision (remap / mark already-done /
unlink). It is **not** retried on a loop, and it is **not** recorded as acknowledged —
"Jira refused the transition" is not evidence Jira is in the desired state.

Implemented in part already (`2026.8.7.14` suppresses repeat attempts per
`(task, target status)` in memory); the operator-facing surface is still to build.

### 10. Teardown — keep the link, stop syncing

Turning Jira off stops writes and preserves `jira_key`. Provenance stays true whatever
the toggle says, and re-enabling picks up where it left off. Clearing the link would
destroy the record and recreate duplicates on the next import — the failure fixed in
`2026.8.7.12`.

Archiving a linked task in Swarm likewise keeps `jira_key` (decided `2026.8.7.7`).

### 11. Freshness — polling, with reconciliation as the safety net

Keep the interval poll. **No webhooks.**

A webhook is a second push path with the failure mode that consumed 2026-08-06/07: a
dropped push nothing can detect. Polling plus a version/state comparison is the pattern
that fixed the task panel, and it needs no publicly reachable endpoint on a dev laptop.

### 12. Comments — status plus one closing comment

Swarm writes the status transition and **one comment when the task closes**, following
the existing `post_completion_comment` format: a non-technical summary for end users,
then the technical resolution for developers.

One comment per ticket is signal; a comment per progress update is noise that trains
the team to mute the ticket. *Open:* confirm this format against the team's documented
standard responses — the current implementation is a reasonable baseline, not a
ratified template.

### 13. Inbound field mapping

Acceptance criteria are **parsed from the ticket when present, left empty when absent**.

Never generated from the description: the verifier drone grades completions against
acceptance criteria and defaults to PASS when empty, so invented criteria become fake
grading standards — the stale-learnings problem in a new place.

---

## Data model

Already shipped:

| Field | Purpose |
| --- | --- |
| `tasks.jira_key` | the linked issue; provenance, survives archive and disable |
| `tasks.jira_exported_status` | the status Jira **acknowledged**; differs from `status` exactly when an export is outstanding |

Still needed:

- per-project sync configuration (multiple spaces), holding the confirmed status map
  and the discovered terminal states
- a promotion-request state for the proposals surface
- an operator-visible record of unreachable-transition divergences

## Deliberately not in scope

- **Epics and issue hierarchy.** Swarm has no parent/child task model; importing an
  Epic produces a task that can never be finished.
- **Progress comments on tickets.**
- **Webhooks.**
- **Backfilling closed history in either direction.**
- **Cross-project ticket movement.** If a ticket moves projects in Jira, treat it as
  out of scope for now and surface it rather than guessing.

## Open questions

1. **The 11 currently-unreachable tickets** need a one-time decision (remap, mark
   closed-in-Jira, or unlink). They are honest but permanently amber today.
2. **API budget.** N devs × M projects × 5-minute polls, plus reconciliation. Needs a
   measured estimate against Atlassian rate limits before rollout, not after.
3. **What "terminal" means per workflow.** Discovery gives the states; something must
   decide which of them mean "do not import". Probably part of the setup confirmation.
4. **Closing-comment template** vs the team's documented standard responses.
5. **Two devs, one ticket, sequentially.** Assignee routing handles who acts now; the
   history of a ticket that passed through two swarms is not yet specified.

## Verification standard for the build

Per the pattern established 2026-08-06/07, and because every dashboard bug in that
window was found by the operator rather than by a green suite:

- Anything touching the two-system boundary gets a **reconciliation test**, not only an
  emit/push test — a push that cannot be verified is the failure mode being designed
  out.
- **Negative controls on every guard**, with the injection asserted to have applied.
  Three tests in that window passed against deliberately broken code because a mock sat
  at the seam under test.
- **No mock at the seam under test.** `conftest`'s `MagicMock` daemon invalidated an
  entire reproduction and silently neutered the 42-pair transition sweep.
- Nothing is called verified against Jira until it has run against a **real ticket** —
  tonight's live run is what exposed both the unreachable-transition class and the
  infinite retry.
