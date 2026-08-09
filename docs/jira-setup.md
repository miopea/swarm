# Connecting your Swarm to Jira

For a developer setting this up on their own machine. Roughly 10 minutes, most of it
waiting for a sync cycle.

**Do the steps in order.** Step 2 exists so that a mistake in steps 3–5 lands on your
screen instead of in your team's ticket queue.

---

## What you are signing up for

Your swarm will **import the Jira issues assigned to you** that are not finished, and
will **write back** to those tickets: status transitions, a closing comment, time
logged, and a note when a task is blocked.

Two things follow from that, and they are the whole reason the setup is careful:

- **Routing is by Jira assignee.** Not by label, not by project alone. A ticket assigned
  to you comes to your board; a ticket assigned to nobody comes to nobody. This is what
  lets every developer enable Jira without all of us importing the same tickets and
  racing to transition them.
- **Writes go to a tracker other people read.** A misconfiguration is not private.

---

## 1. Register an OAuth app (once per person)

Settings → Integrations → Jira → *One-time setup: Register an OAuth app in Atlassian*.
Follow it, then paste the **Client ID** and **Client Secret** into the two fields and
save.

The app needs these scopes, which the setup link requests for you:

| Scope | Why |
| --- | --- |
| `read:jira-work` | import your issues, read workflows |
| `write:jira-work` | transition, comment, log time |
| `read:jira-user` | resolve *your* account, so tickets Swarm creates come back to you |
| `offline_access` | refresh the token without re-authorising every hour |

> If you authorised before `2026.8.9` you will not have `read:jira-user`. Nothing breaks
> — Swarm derives your account from your assigned work instead — but reconnecting is
> cleaner. Tokens are stored in the `secrets` table of `~/.swarm/swarm.db`, never in
> `swarm.yaml`.

## 2. Turn ON read-only — before anything else

Settings → Integrations → Jira → **Read-only mode**.

Imports, workflow discovery and reconciliation all run normally. Every write is refused
and logged saying what it *would* have done. Leave it on until step 5.

This is enforced where Swarm talks to Jira, not by hiding buttons, because the sync loop
writes on a **timer with nobody watching** — that is how a settings change once
transitioned 14 real tickets before anyone looked at it.

## 3. Add your projects

**Projects to sync** — comma-separated keys, e.g. `WWD, IS`.

Only Story, Task, Bug and Sub-task are imported. **Epics are deliberately excluded**: an
epic is a container, not work, and a worker cannot finish one.

## 4. Discover and confirm each project's workflow

For each project: type the key → **Discover workflow** → check the proposed mapping →
**Confirm**.

Do not skip the checking. Swarm proposes a target status for each of its own states, and
`Done` / `Resolved` / `Closed` are rarely interchangeable. Two real examples from our
own tracker:

- **WWD** maps `done → Done`.
- **IS** maps `done → Done` too, but its tickets finish as **Resolved** — a hardcoded
  `Done` was refused by 11 real tickets before per-project discovery existed.

Anything the proposal cannot justify is left **unmapped**, shown in amber. An unmapped
state simply never reaches Jira; that is safer than a confident wrong guess, but you
should know which ones they are.

**Until a project is confirmed, the bulk sweep reports what it would do and writes
nothing.** That gate is separate from read-only and stays in force afterwards.

## 5. Preview, then turn read-only off

**Preview sync plan** shows every ticket the reconciler would touch and what it would
change. Read it. If it lists tickets you do not expect, stop and re-check step 3.

When the plan looks right, turn **read-only off**. Writes begin on the next cycle
(default: every 5 minutes).

---

## What you will see afterwards

- Tickets assigned to you appear as tasks, with their description, comments and
  attachments mirrored.
- **New comments keep arriving** after import — they land under a `--- Jira sync ---`
  marker in the task description, and the assigned worker gets a message with the latest
  one. A scope change on the ticket reaches whoever is working it.
- Closing a task transitions its ticket, posts a closing comment, and logs the time the
  task was actually ACTIVE.
- Blocking a task puts a note on the ticket saying so, rewritten when the block clears.
- Every write Swarm makes to Jira is logged at WARNING, so `~/.swarm/swarm.log` at the
  default level shows what it did.

## Things that surprise people

**Anything you write above the `--- Jira sync ---` marker is yours and is preserved.**
Everything below it is regenerated each sync. `swarm_edit_task --append_description`
already puts text in the right place.

**The `swarm` label is reserved.** It is applied automatically to tickets Swarm
*created*, and means "an agent raised this". Do not use it for your own filing — it
routes nothing, so you lose nothing by avoiding it.

**Reassigning a ticket away from yourself in Jira removes it from your board.** The task
is released and parked, its link kept, and nothing is written back. That is how a
handover works: assign in Jira, and the work moves.

**Jira owns status.** If you move a ticket in Jira, Jira wins.

**Swarm does not create tickets on its own.** A worker can *request* one
(`swarm_request_jira_ticket`); it appears on the Decisions tab and nothing is created
until you approve it.

## Turning it off

Un-tick **enabled**. Writes stop and existing `jira_key` links are kept, so re-enabling
picks up where it left off. Clearing the links would recreate duplicates on the next
import.

## If something looks wrong

- `~/.swarm/swarm.log` — every Jira write and every refusal is at WARNING.
- Settings → Integrations shows each project's mapping, its confirmation state, and how
  many tasks are linked to it — including projects you have linked tasks in but have not
  configured.
- Turn **read-only** back on at any time. It takes effect on the next call, and imports
  keep working so you can keep diagnosing.

## For the person enabling this across a team

Two operational notes:

- **Per-cycle API cost is bounded**, not proportional to board size: roughly 14 calls
  per cycle regardless of how many tickets are linked (one import search, one ownership
  check, one batched refresh, a capped worklog backfill). Blocked tasks add one call
  each. At a 5-minute interval that is ~170 calls/hour per developer.
- **Bulk-filing work?** File the tickets **unassigned** and assign them one at a time as
  they become ready. An unassigned ticket is never imported, so nothing can be picked up
  out of order — that is the ordering mechanism, not Jira issue links, which Swarm does
  not read.
