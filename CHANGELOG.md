# Changelog

Swarm (legacy) uses calendar versioning (`YYYY.M.D.patch`) — see `pyproject.toml` for the current version. Notable changes since the initial v1.0.0 release are grouped below.

## Unreleased

### Features

### Changes

### Fixes

## [2026.8.21.9] - 2026-08-21

### Features

### Changes

### Fixes

- **The in-app update failed on any machine whose git rewrites GitHub URLs.**
  This is the root cause behind every "Update failed" report in this sequence.

  A rule like `url."git@github.com:".insteadOf = "https://github.com/"` — common
  on machines set up for other tooling — drags an anonymous HTTPS clone of a
  PUBLIC repository onto SSH. From a shell that still works, because the
  operator types the passphrase. In a systemd user daemon there is no terminal
  to prompt on and no `SSH_AUTH_SOCK` to unlock a key with, so the fetch dies as
  `error: Git operation failed / process didn't exit successfully` — with the
  cause never reaching any log, because uv does not pass through git's stderr.

  Better messages were not the fix; not needing credentials was. The updater now
  injects an identity `insteadOf` for its own owner prefix into the install
  child's environment via `GIT_CONFIG_*`. git resolves `insteadOf` by longest
  matching prefix, so `https://github.com/miopea/` outranks a rule written for
  all of `github.com`, and the clone stays on anonymous HTTPS. Scoped to the
  repo we install from, applied only to that child, and no other remote's
  routing is affected.

  Verified against a real git carrying that rule: without the override the fetch
  fails with `Host key verification failed`; with it, `git ls-remote` returns the
  HEAD sha.

## [2026.8.21.8] - 2026-08-21

### Features

### Changes

### Fixes

- **A failed git fetch now explains itself.** uv wraps git and does not pass
  through git's stderr, so a fetch failure surfaces as nothing but
  `error: Git operation failed / Caused by: process didn't exit successfully`.
  There is no cause in that message and nothing an operator can act on — the
  update simply appears broken.

  On a real box the actual reason was an `insteadOf` rule rewriting
  `https://github.com/` to SSH. This repository is PUBLIC, so an HTTPS clone
  needs no credentials at all; the rewrite sent it down an authenticated path
  instead, and a systemd user daemon has no `SSH_AUTH_SOCK` to unlock a key
  with. Nothing in any log said so.

  On a git failure the updater now reads the rewrite rules directly — which is
  deterministic where matching uv's prose is not — and names the offending rule,
  explains why a public repo should not have needed credentials, and gives a
  shell command that will work. With no rewrite to blame it hands over the
  `git ls-remote` that reproduces the failure with git's own error visible,
  rather than guessing.

## [2026.8.21.7] - 2026-08-21

### Features

### Changes

### Fixes

- **A failed update could not be diagnosed from the browser.** Both UI callers
  showed `output.substring(0, 200)` — the FIRST 200 characters, which is uv's
  download progress, while the line that explains the failure is appended LAST.
  The operator was shown progress and told it was an error. `dashboard.js` was
  corrected in 2026.8.21.5; `config.html` has its own copy of the handler and
  was missed, so the Updates panel kept doing it.

  Both now show the tail, and `perform_update()` writes the complete output to
  `~/.swarm-update.log` regardless — in `$HOME` rather than the install tree,
  because the update REPLACES the package and a log inside it is destroyed by
  the operation it is recording. A file on disk is not subject to anyone's
  formatting decisions.

## [2026.8.21.6] - 2026-08-21

### Features

### Changes

### Fixes

- **The relocation helper was killed by the very command it issued.** It ran as
  a child of the daemon, and `relocate()`'s first real step is
  `systemctl --user stop swarm.service` — so the process performing the
  shutdown was inside the thing being shut down. `start_new_session=True` gives
  a new session but SHARES THE CGROUP, which was never enough.

  Observed on a real box: the helper died mid-stop while blocked on its own
  `systemctl` child, which survived as an orphan
  (`Unit process 1141 (systemctl) remains running after unit stopped`). The
  relocation log ended after the plan with no traceback, because a signal
  leaves none. Nothing moved, and the hive was left stopped.

  `KillMode=process` did NOT prevent it — so the pre-flight that checked for
  `KillMode=process` was checking the wrong thing entirely: it passed, and the
  helper died anyway. That guard is removed rather than patched, because a
  guard that passes on the failing case is worse than no guard.

  The helper now runs under `systemd-run --user --collect`, in its own
  transient unit and cgroup, structurally out of reach of anything done to
  `swarm.service`. `--property=StandardOutput=append:` keeps the relocation log
  working, since systemd-run otherwise sends output to the journal instead of
  the caller's fd — and that log is the only reason this failure was
  diagnosable. Falls back to a plain spawn where `systemd-run` is absent, which
  is safe: no systemd means no unit to stop, so the hazard cannot arise.

## [2026.8.21.5] - 2026-08-21

### Fixed — the relocation banner froze the daemon it was offering to move

- **`/api/relocate` blocked the event loop, so the dashboard hung instead of
  responding.** `plan()` was correctly offloaded with `asyncio.to_thread` — and
  then `_relocation_payload()` was called on the loop, where `dir_size_bytes()`
  runs `rglob("*")` plus a `stat()` on every file in the state directory:
  `backups/` (up to seven daily copies of the database), `uploads/`, `memory/`,
  and the Queen's workdir. Nothing else runs during that walk — no HTTP, no
  WebSocket, no worker polling.

  Observed on a real box as a dashboard that HUNG rather than errored, and
  because the freeze happens on the very request the banner makes to render
  itself, the relocation it was offering could never be started at all: pressing
  "Move now" produced no `~/.swarm-relocate.log` because no helper was ever
  spawned. Both the plan and the payload now run in the thread. This is the rule
  the project already states — "Async everywhere. Never block the event loop" —
  and the cost of breaking it is a hang, which is strictly worse than an error
  because it looks like a network problem.

- **Relocation now verifies systemd BEFORE stopping anything.** systemd is what
  starts the hive again at the end; without it the sequence would stop the
  service, move the state, write a unit nothing can load, and leave the operator
  with no dashboard and no obvious way back — from pressing one button in a web
  page. The availability check moved into `preflight()`, and its refusal states
  that nothing was changed. Checking it beforehand is not something a dev is
  going to do, and a one-click action that expects a pre-flight command is not
  one click.

### Features

- **`swarm update --yes`** — skips the confirmation prompt. `click.confirm`
  aborts on EOF, so the command could not be driven from a script, which is
  precisely how a fleet gets moved onto a build before an old repo name is
  reused.

### Changes

- **The update refuses a repo that is not ours.** Freeing the `swarm` name
  means something else eventually claims it. GitHub's rename redirect — the
  only reason builds carrying the old URL still update — dies the moment that
  happens, and the same baked-in URL then resolves to another project, which
  would be installed straight over the hive with no error at any layer.
  `perform_update()` now resolves the repo BEFORE running uv and refuses
  anything outside `{miopea/swarm, miopea/swarm-legacy}`. A rename within our
  own history is the supported path and is reported, not refused; an
  unreachable probe never refuses, because a network failure is not evidence
  of a hijacked name.

### Fixes

- **A slow update did not just fail — it left the tool broken.**
  `_INSTALL_TIMEOUT` was 120s, which fit a warm cache and nothing else. A real
  update on a real connection was still fetching cryptography (4.5 MiB) and
  building red-black-tree-mod from source when the timeout fired and
  `proc.kill()` ran. But `uv tool install --force` uninstalls BEFORE it
  installs, so the kill landed after the old version was gone and before the
  data files were written: the `.py` modules were present, the daemon started
  and served, and the first page load died with `Template 'dashboard.html' not
  found` — a symptom four steps from its cause. The entrypoint shims were gone
  too, so `swarm-legacy update` could not repair it; only a raw
  `uv tool install` could.

  Timeout raised to 600s, and `missing_install_artifacts()` now checks the data
  files on disk after both the timeout path and a uv exit of 0. A partial
  install is named by the updater, with the recovery command, instead of being
  discovered later by the web layer.

- **The failure toast showed the least useful part of the output.** It printed
  the FIRST 200 characters — uv's download progress — while the line that
  explains the failure is appended last. The operator was shown progress and
  told it was an error. It now shows the tail.

- **git could hang the updater indefinitely.** The install runs with
  `stdin=DEVNULL`; an operator whose git rewrites `https://github.com/` to SSH
  sends a public-repo clone down an authenticated path, and a passphrase prompt
  against a closed stdin blocks until the timeout kills it mid-install. Now
  `GIT_TERMINAL_PROMPT=0` and `BatchMode=yes` turn that into an immediate,
  readable error, and an auth failure on a public repo is explained rather than
  surfaced as a raw ssh message.

## [2026.8.21.4] - 2026-08-21

### Features

### Changes

### Fixes

- **"Move now" would have killed a live hive to perform a move that could never
  succeed.** Seen on a relocated box with 1843 tasks in `~/.swarm-legacy` where
  something recreated a 3 MB `~/.swarm`. That makes `move_needed` true, so the
  banner offered the relocation — but `_move_state()` refuses to merge two
  hives, and it refused from INSIDE `relocate()`, after `_stop_live()` had
  stopped the service and SIGKILLed the daemon and holder. Full destructive
  price, zero chance of success. The check is now a `preflight()` that runs
  before anything is stopped, the endpoint returns 409 without launching the
  helper, and the banner drops its own button and explains what it found
  instead. A guard that fires after the damage is not a guard.

- **A failed relocation left no trace anywhere.** The detached helper was
  spawned with `stdout`/`stderr` to `DEVNULL`, so a `RelocationError` vanished
  and the dashboard sat on "Relocating..." forever. Output now goes to
  `~/.swarm-relocate.log` — deliberately in `$HOME` rather than the state
  directory, which the relocation is in the middle of moving.

- **`swarm relocate` reported "nothing to do" while the dashboard was down.**
  `already_done` was defined purely as "nothing of the old name is left" — moved
  state, no old unit, no old shims — and never asked whether the hive it
  relocated INTO was running. An operator whose daemon never came back was told
  the command had nothing to offer. `RelocationPlan` now carries
  `new_unit_exists` / `new_unit_active` with a separate `needs_repair`, and a
  non-destructive `repair()` writes a missing unit, reloads, enables, starts,
  then VERIFIES — exiting non-zero with the journal command when the daemon
  still is not up. Kept out of `already_done` on purpose: folding it in would
  show the destructive plan to an operator who had already relocated, which is
  the regression #1677 fixed.

## [2026.8.21.3] - 2026-08-21

### Features

- **The updater now reports when the repo has been renamed underneath it.**
  Every build bakes in the repo URL current when it shipped, and a rename does
  not break anything — GitHub redirects and git follows — so the failure is
  silent: the name compiled into the binary stops being true and nothing says
  so. After a successful update, `perform_update()` asks the API which repo it
  actually served and emits a note to the dashboard when that differs from
  `_REPO_FULL_NAME`. Reported, never acted on: silently retargeting an install
  at a repo the operator did not name is not a decision an updater gets to make.

- **Relocate from the dashboard — the last step that still needed a terminal.**
  `swarm relocate` was reachable only from a shell, so "update without a
  terminal" was a half-truth: the in-app update landed the new build, and then
  the one remaining move required a prompt. A banner now appears while the hive
  still answers to the `swarm` name, showing source, target, measured size and
  the live PIDs about to die, behind one `Move now` button.

  The work cannot run in the daemon. `relocate()` stops the unit and SIGTERMs
  the PID it reads from `daemon.lock` — which is the daemon serving the request
  — so an in-process call would kill itself between moving the state and
  rewriting the unit. `POST /api/relocate` hands off to a detached
  `swarm-legacy relocate --yes` instead and returns immediately; the helper
  stops the daemon, moves the state and starts `swarm-legacy.service`, which
  rebinds the same port for the dashboard to reconnect to.

  Two things decide whether that handoff survives, and both are now enforced
  rather than assumed. The helper is resolved as `swarm-legacy` and never
  `swarm`, because `_remove_old_entrypoints()` unlinks that shim partway
  through — a helper launched under it would delete its own executable
  mid-move. And `start_new_session=True` creates a new session but *not* a new
  cgroup, so without `KillMode=process` the `systemctl stop` takes the helper
  down with the daemon, leaving state moved and no unit written. That is the one
  outcome needing a terminal to repair, so it is now a pre-flight 409 naming the
  fix instead of a post-mortem.

### Changes

- `/api/health` carries `relocated` and `state_dir`. The banner polls that
  boolean rather than `plan()`, which shells out to `systemctl is-active` and
  walks the state directory — far too expensive per tick. Detail is fetched
  once, from `GET /api/relocate`, when the banner first renders.

### Fixes

- **A repo rename silently emptied the update banner's commit line.** The
  version probe reads `raw.githubusercontent.com`, which served the renamed repo
  fine; the commit probe reads `api.github.com`, which answered `301`. Both ran
  curl without `-L`, so `json.loads` succeeded on the redirect body — a dict
  where a list was expected — and `_fetch_latest_commit()` returned `{}`. No
  error, no log: the banner just rendered a stale sha and no message, which is
  exactly what an install here was observed doing. Both probes now follow
  redirects.

- **`dir_size_bytes()` reported a confident `0` for a directory it could not
  read.** `rglob` on a missing path yields nothing and raises nothing, so the
  sum succeeded at zero. Rendered in the banner that reads as "nothing to
  move" — an invitation to skip the very step the operator needs. Unknown and
  empty are now different values.

## [2026.8.21.2] - 2026-08-21

### Features

### Changes

### Fixes

- **After an update, a relocated install was told to relocate again — forever.**
  `uv` writes two copies of every console script: the shim on `PATH` and the
  script it points at inside the tool directory. The post-update cleanup stopped
  after removing the first, so the inner copy survived every update and kept
  `already_done` false. An operator who had relocated weeks ago would be shown
  the full destructive banner — "every running worker will be terminated" — on a
  routine `swarm-legacy relocate --dry-run`. It now removes every copy it owns
  and leaves anything it does not.

  The journey rehearsal missed this because it only checked the `PATH` shim. The
  regression test now asserts the end state an operator actually sees: after an
  update on a relocated install, `already_done` is true.

## [2026.8.21] - 2026-08-21

### Features

- **`swarm relocate` — the destructive update that frees the `swarm` name.**
  Moves `~/.swarm` → `~/.swarm-legacy`, renames `swarm.service` →
  `swarm-legacy.service` (carrying any `swarm.service.d/` drop-in across, with
  its `ExecStart` renamed too), and removes the `swarm` entrypoint. Nothing about
  a hive's contents changes — only where they live — and Legacy keeps working
  afterwards under `swarm-legacy`.

  It is genuinely destructive: the pty-holder binds `<state>/holder.sock`, and a
  Unix socket's path is fixed at `bind()`, so the sidecar and every running
  worker must go down for the directory to move. The command prints exactly what
  will move, lists the live daemon/holder PIDs by name, and requires the operator
  to type `relocate`. `--dry-run` shows all of it and touches nothing.

  Every step is idempotent and the directory moves *first*, so an interrupted run
  is fixed by re-running rather than by hand: state already in the new location is
  found by `state_dir()` regardless of what the unit still says.

  Both `swarm` and `swarm-legacy` entrypoints now ship together, so an install
  that has not relocated is completely unaffected by the update.

### Changes

- **All runtime state now resolves through `swarm.paths.state_dir()`** instead of
  ~50 hardcoded `~/.swarm` literals. Resolution is `$SWARM_STATE_DIR`, then
  `~/.swarm-legacy` if it exists, then `~/.swarm`. The relocated directory is
  preferred deliberately: freeing the old name means something else may create a
  fresh `~/.swarm`, and Legacy must keep reading its own hive rather than
  silently adopting an empty stranger.

  Config values that get *serialized* (`report_dir`, the log file) use
  `state_path_str()`, which stays `~`-anchored. Writing an absolute path into the
  user's config would freeze today's location into the file and break on the next
  relocation.

### Fixes

- **A lingering daemon could silently undo a relocation.** `_stop_live` sent
  SIGTERM and moved on; a daemon still shutting down keeps the log path it
  resolved at import — the old one — and recreates the directory the move just
  emptied, re-occupying the freed name and leaving a re-run convinced there is
  still state to move. Observed directly: a relocation reporting success with
  `~/.swarm` back moments later holding a lone `swarm.log`. It now waits for the
  signalled processes to exit and SIGKILLs anything ignoring SIGTERM. The result
  also carries `source_recreated`, so if the old directory does come back the
  command says something is still running against it instead of printing
  "Relocation complete" over the top.

- The relocated flake in `test_worker_selector_browser` is fixed. It pressed
  ArrowDown and evaluated the highlight immediately; the keypress is handled
  asynchronously, so roughly one run in five reported "no active row". It now
  waits for the state the assertion is about. Confirmed 8/8 after, from 1-in-5
  failures before — unrelated to the relocation work, but a random CI failure is
  friction nobody needs.

- **End-to-end rehearsal of the developer journey found three more defects.**
  A released 2026.8.20 install was built, given real state and a bound holder,
  updated through its own `perform_update()`, then relocated — on a machine with
  no systemd and a non-standard `uv` bin directory.

  - **Relocation reported success while leaving `swarm` occupied.**
    `$UV_TOOL_BIN_DIR` is an *install-time* variable and is normally absent from
    the shell that later runs `swarm relocate`, and `uv tool dir --bin` then
    reports the default rather than the directory actually used. The command's
    entire purpose failed silently. The shim search now starts from the
    directory of the running command — if you can type `swarm relocate`, the
    shim is right there — and also reads the `install-path` entries in `uv`'s own
    receipt.

  - **The command could claim success it had not achieved.** It now re-checks
    afterwards and, if any `swarm` is still present, says so in red with the
    paths rather than printing "Relocation complete".

  - **A move that would break the holder is now refused.** A Unix socket path is
    capped at ~104 bytes by `sockaddr_un.sun_path`, and `.swarm-legacy` is seven
    bytes longer than `.swarm`. A deep enough home directory would leave the
    holder unable to bind and no worker able to start — *after* a one-way move.
    Checked before anything is touched. (Hit for real while rehearsing, in a
    deep scratch directory.)

  - The completion message no longer tells a machine without systemd to run
    `systemctl`.

- **Pre-merge review of `swarm relocate` turned up five defects.** All were
  found by exercising the awkward paths rather than re-reading the code:

  - **No systemd meant a crash *after* the state directory had moved.** macOS
    and systemd-less WSL have no `systemctl` binary, and the raw call raises
    `FileNotFoundError`. Unguarded, that aborted the run partway through and
    reported it as a traceback. `_systemctl` now tolerates a missing binary.

  - **Relocating before first run half-applied, silently.** With no `~/.swarm`
    to move, nothing created `~/.swarm-legacy` — so the unit and entrypoint were
    renamed while `state_dir()` still resolved to the old path. The install
    looked relocated but would have written its state straight back to the name
    it was supposed to free, and neither `swarm init` nor the update cleanup
    would have recognised it as relocated. The target is now created even when
    there is nothing to move; its existence *is* the marker.

  - **A dangling enable link was left behind.** `_remove_old_unit` returned
    early when the unit file was already gone, so
    `default.target.wants/swarm.service` kept pointing at nothing and systemd
    complained on every reload. `disable` now always runs and the stale symlink
    is cleared.

  - **`already_done` ignored that stale link**, so re-running reported success
    and never cleaned it up. It now counts.

  - **Shims outside `~/.local/bin` were missed.** `uv` honours
    `$UV_TOOL_BIN_DIR` and `$XDG_BIN_HOME`; an install that moved its bin
    directory would have kept the old name occupied — the one thing the command
    exists to prevent. The search now covers both, and `perform_update()` shares
    the same helper so the two cannot drift.

- **An update would have destroyed whatever else owned the `swarm` name.**
  `uv tool install --force` overwrites whatever sits at a declared script's
  name — confirmed by running it against a foreign binary and watching it
  vanish. On a relocated install that name has deliberately been handed to
  something else, so an update silently replaced it. `perform_update()` now
  moves a `swarm` it does not own aside before installing and restores it
  afterwards (preserving a symlink as a symlink), verified end to end: the
  other project's binary is still there and still itself after a full update.

  Dropping the `swarm` entrypoint from the package would *not* have been a
  safe alternative: an un-relocated install would lose the command while its
  `swarm.service` still invoked `swarm serve`, breaking the service outright.

- **`swarm stop`, `swarm migration finish` and `swarm migration reverse` were
  broken by a circular import** and failed with `ImportError: cannot import name
  '_DAEMON_LOCK_PATH' from partially initialized module 'swarm.server.runner'`.
  `runner` imported `daemon` at module top while `daemon` re-exported names back
  from `runner` at its bottom (a back-compat shim from the god-object refactor),
  so importing `runner` first — which every one of those CLI paths does — blew up
  before doing any work. `runner` now imports `SwarmDaemon` lazily at the two
  sites that construct one. Our own migration procedure told operators to run
  `swarm stop` first, so this broke the documented path.

- Three tests asserted `~/.swarm` literals and so passed only on an un-relocated
  developer machine. They now assert the value follows the state dir.

- **`swarm init` would have resurrected `swarm.service` on a relocated install.**
  It installs the unit whenever one is absent — and after `swarm relocate` one
  *is* absent, so it wrote a fresh `swarm.service` re-occupying the freed name and
  pointing at a `swarm` entrypoint that no longer exists: a unit that can never
  start, created by a command the operator thought was safe. `install_service()`
  and `ensure_killmode_process()` now resolve the unit through
  `current_unit_name()`, which follows the state directory.

- **The test suite wrote, enabled and started the developer's real
  `~/.config/systemd/user/swarm.service`.** Eight `test_init_*` cases reached
  `install_service()` unpatched. On a relocated box this silently undid part of
  the relocation on every test run — which is how the bug above was found. An
  autouse fixture now redirects the unit directory and stubs `systemctl`, and
  pins the relocation state so unit naming does not depend on whether the machine
  running the tests happens to be relocated.

- `test_dev_reexec_when_installed` hung for 30s and timed out. It patched
  `os.execvp` with a bare mock, but `execvp` *replaces* the process — nothing
  after it runs — so execution fell through into `run_daemon()` and the test
  waited on a real daemon. The circular-import fix above is what exposed it: the
  `ImportError` had been aborting the test before it ever got that far. The mock
  now raises `SystemExit`, which is what the real call does to the interpreter.

- **Every in-app update re-occupied the `swarm` name on a relocated install.**
  `uv tool install` writes every console script the package declares, so each
  update handed `swarm` straight back to Legacy — silently undoing the one thing
  `swarm relocate` exists to do. Confirmed by running the real update command on
  a relocated box and watching `~/.local/bin/swarm` reappear. `perform_update()`
  now removes the recreated shim and says so at WARNING level.

  It removes the shim **only** when it resolves into this package's own tool
  directory. Once something else owns `swarm`, deleting it would be destructive
  rather than tidy, so a foreign binary is left alone and logged instead.

## [2026.8.20] - 2026-08-20

### Features

### Changes

- **This project is now Swarm (legacy).** Active development moved to
  [Swarm Next](https://github.com/miopea/swarm-next); this repository is
  maintenance-only. Every user-visible name — page titles, dashboard header,
  PWA manifest, systemd unit description, CLI help, notification titles,
  passkey relying-party name, API docs, OAuth consent page — now reads
  "Swarm (legacy)", and the dashboard footer, login page, README and
  CONTRIBUTING link to the successor. The GitHub repository was renamed
  `miopea/swarm` → `miopea/swarm-legacy`; the update checker, `gh` feedback
  submitter and install source follow the new path rather than relying on
  GitHub's rename redirect.
  Deliberately unchanged, because renaming them would break existing installs:
  the `swarm-ai` package name, the `swarm` CLI entry point, the `swarm.service`
  unit name, `~/.swarm/swarm.db`, and every Python module and class identifier.

- The systemd unit `Description=` is now corrected in place on daemon start for
  installs that predate the rename. `perform_update()` only reinstalls the
  package and never rewrites the unit, so an updated install would otherwise
  have kept advertising itself as plain "Swarm" in `systemctl` output forever.
  The existing `ensure_killmode_process()` patcher gained the transform, so only
  the exact pre-rename string is replaced — a Description the operator
  customised is left alone, and a hand-tuned `ExecStart` / `WorkingDirectory` is
  never regenerated.

- The unit-patch log line no longer claims `KillMode=process` regardless of
  which transform actually fired — it now names the three it covers. An
  operator reading the journal to explain a unit change was being pointed at
  the wrong one.

- INV-2's absence threshold is now its own knob, `drones.inv2_absent_threshold_seconds`,
  defaulting to 3600s instead of inheriting the display-only `drones.sleeping_threshold`
  (#1571).

### Fixes

- A worker pausing between turns no longer loses its ACTIVE task. INV-2 inherited the
  *display* sleep threshold, so any pause long enough to grey out a dashboard tile was read
  as abandonment — and the resulting ASSIGNED row then satisfied the idle-watcher's nudge
  trigger, so the worker was prompted about work it was actively holding. Measured across 45
  RESTING episodes where the worker held an ACTIVE task and then resumed, a 1200s threshold
  wrongly declared 24.4% of them absent versus 4.4% at 3600s; all 15 real demotions since the
  previous fix were followed by the same worker returning to the same task. Lowering
  `sleeping_threshold` for display reasons can no longer re-arm task demotion (#1571).

## [2026.8.13] - 2026-08-13

### Features

### Changes

- The dashboard no longer claims a state it has not measured. `Worker.state` defaults
  to `BUZZING` — the MOST ACTIVE state — so a freshly started daemon published a
  fully-busy swarm for the 4-6s before the pilot's first poll. The report's screenshot
  showed all 16 workers reading `BUZZING — 4m`: one identical stale figure across every
  tile, which is the tell that nothing had measured any of them. Workers now publish
  `UNCLASSIFIED` (and no duration — the identical `4m` implied four minutes of observed
  work, so it was as much of the lie as the state) until something actually measures
  them: the pilot classifying PTY output, a remembered state restored from a previous
  run, or a deliberate set such as the operator's put-to-sleep.

  DISPLAY-ONLY. `state` and `display_state` keep their values and types, so INV-2, the
  reconcilers, the idle watcher and every `== RESTING` / `== BUZZING` comparison are
  untouched — which is why this is a bool rather than a `WorkerState.UNKNOWN` member
  that would have put an unhandled case into all of them. The popped-out selector
  inherits it for free, rendering from the same partial.

  Half of the original report was already fixed: `db/worker_state_store.py` has
  persisted and restored worker state since 2026-08-09, so what remained was only the
  fallback for a worker with no usable remembered state (#1357).

- **Broadcast is Queen-only.** A worker sending `swarm_send_message` to `*` is now
  refused before anything is written; only the Queen can fan out. Operator ruling
  2026-08-12: "this is a constant pain point".

  The existing #647 gate was not enough because it is a *content* check — it reads
  the message for directive or authority language, so anything benign-sounding still
  reached every inbox, and fan-out was governed by phrasing rather than by authority.
  A broadcast's cost to the fleet does not depend on how politely it is worded. The
  new check does not read the message at all; a polite note and a naked directive are
  refused identically.

  The refusal names the way forward rather than just denying — send to the workers
  actually affected, or hand it to the Queen with `swarm_note_to_queen` and say you
  think it warrants a broadcast. A worker that cannot see the alternative rephrases
  and retries, which is how the #647 gate ended up being routed around.

  The tool surface changed with it, since a tool that advertises what it refuses just
  trains workers to hit the refusal: the description no longer says "prefer direct
  messages over `*` broadcast", the `to` field says `*` is Queen-only, and the
  `"to": "*"` schema example is gone. `/swarm-finding` hardcoded `to="*"` and would
  have broken outright — it now names affected peers, or routes to the Queen.

  `POST /api/messages/send` refuses `*` only when `from` is a known worker that is not
  the Queen. The operator's own broadcasts (`from: operator`, what the dashboard sends)
  are untouched. That route is a guardrail rather than a boundary — `from` is
  self-declared — and the enforcing check is the MCP verb workers actually call.

  Two branches are now unreachable and left in place deliberately, recorded here so
  they are not mistaken for live paths: `_handle_broadcast`'s queen-relay/mark-read
  logic (the only broadcaster is the Queen, and the roster excludes the sender, so she
  is never among her own recipients), and #647's directive-language-on-broadcast half
  (workers are refused earlier; the Queen is exempt from that gate by design). #647's
  authority-claim check on direct 1:1 messages is unaffected and still tested.

### Fixes

## [2026.8.12] - 2026-08-12

### Features

### Changes

- `queen_view_worker_state` declares its shape. Every structured exit now carries
  `mode` (`"summary"` or `"single"`), because the tool has two genuinely different
  result shapes — a summary keys `workers`, a targeted lookup keys `worker` — and
  a client had to infer which it held from whichever key happened to exist. A
  client should read a field to learn its shape, never the response's type (#1535).
- Task rows carry `dispatch_requested_at` (schema v20, nullable, additive). Without
  a marker, "ASSIGNED and not started" is indistinguishable from ordinary queued
  work, so nothing could tell a failed dispatch from a task waiting its turn
  (#1527).
- `GOAL_SET` records `goal_has_blocker_exit`, `condition_sha` and `condition_len`
  in metadata, so a query can establish which goal condition was seeded without
  reading the condition text. New shared `truncate_for_log()` appends
  `…(truncated, N chars total)` wherever a log detail is cut (#1524).

### Fixes

- INV-2 no longer demotes a paused worker's ACTIVE task. It treated anything
  outside BUZZING/WAITING as unable to "legitimately hold an ACTIVE task", but
  ACTIVE means *this is the task I am on*, not *I am mid-token-generation* — so
  every ordinary pause at the prompt was read as a violation and a correct row was
  "repaired". It was the steady state, not an edge case: 404 of 418
  `TASK_RECONCILED` rows were this demotion, and three workers were sitting in the
  demoted state when it was measured. It also read as a bug in `swarm_start_task`,
  because the worker asserts, sees success, and a later read shows ASSIGNED with
  nothing connecting the two. Demotion now requires absence (SLEEPING/STUNG),
  reusing the state machine's existing `sleeping_threshold` boundary rather than
  inventing a second grace period. `docs/specs/worker-asserted-active.md` §3 had
  already stated the rule; its AC-4 shipped without a test (#1538).
- `queen_reassign_task(start=true)` no longer claims a dispatch it cannot know
  happened. `_fire_async` returns before the dispatch runs and the synchronous
  precondition only covered a non-ASSIGNED status, so a dead worker process or PTY
  write failure still read as success. The verb now says dispatch was REQUESTED,
  failures land in the buzz log *and* on the task's own history rather than only a
  daemon-log line nobody reads, and a new reconciler rule reports a claimed
  dispatch that never reached ACTIVE — once, not every sweep (#1527).
- An armed native `/goal` is cleared when its task stops being the worker's active
  one. Goals were arm-only: there was exactly one `/goal` send in the codebase and
  no clear path, so a goal outlived its task through park, complete, block and
  reassign alike. Compounded by arming happening only on dispatch and never on a
  worker-asserted start, a stale goal kept grading a worker on criteria for work it
  had set down — nine times in one session, each pushing to override an operator
  ruling (#1536).
- SQL writes that are not deletions now always escalate. The safety net covered
  DROP/TRUNCATE/ALTER and DELETE-without-WHERE — every destructive verb except the
  two that change data in place — so `psql -c "UPDATE \"user\" SET hub_role='ADMIN'"`
  auto-approved. Patterns are shaped to SQL rather than to the words, so `npm
  update` and prose containing "insert into" stay approvable (#1526).
- Zero-result and not-found exits carry `structuredContent` like their populated
  path. `result["structuredContent"]["entries"]` raised the moment a filter matched
  nothing, which is a routine answer rather than an error — and it misled a
  buzz_log read into looking like the reader's own SQL was wrong. Empty is modelled
  as a successful empty collection with no error discriminator; the not-found path
  of `queen_view_worker_state` carries `worker: null` plus an `error` field
  (#1432, #1535).

## [2026.8.11.4] - 2026-08-11

### Features

### Changes

- Worker tiles show only a task the worker has ASSERTED via `swarm_start_task`.
  Assignment is not a claim about the present, so it no longer occupies the line
  that answers "what is this worker doing" — it appears as a quiet `N pending
  assigned` count instead. A count rather than a task on purpose: naming one of N
  would have to pick arbitrarily, which is the pick that made #1159's promoter
  activate the wrong task. Parked (HOLD) tasks are excluded from the count.

### Fixes

- The Edit/Write file-lock hook no longer refuses a claim holder its own file
  (#1498). `_identify_worker` is a CWD heuristic that returns `None` when it
  cannot match a worker path, and the old code substituted the literal string
  `"unknown"`, which compares unequal to every real owner — so an unidentified
  worker was refused every claimed file, the refusal named the legitimate holder,
  and `"unknown"` was then written into the lock table, dispossessing the real
  claimer. Claiming a file was therefore strictly harmful. It now fails open on
  unknown identity and never records a lock under a non-worker name.
- That hook also honours `coordination.file_ownership` (`off` / `warning` /
  `hard-block`), which it previously never consulted — hard-blocking fleet-wide
  while the configured mode was advisory. A block now names both parties, so a
  self-refusal is impossible to misread.
- File-lock conflicts reach the drone log and log at WARNING. They were logged at
  INFO under a daemon running `log_level=WARNING`, so a denial reached no
  destination at all, and it was the only decision on that route that skipped
  `_log_hook_decision` — a block nobody outside the blocked worker could diagnose.
- A declared blocker now satisfies the native `/goal` condition instead of reading
  as "goal not yet met, continue" (#1500). The only exit was a turn counter, so a
  worker waiting on an operator decision had to burn all `native_goal_max_turns`
  before it was allowed to stop, and every one of those turns was guaranteed waste.
  Measured: two workers each hit Claude Code's 9-consecutive-block cap re-emitting
  a blocker. Second instance of the class fixed at the call site for #523.
- The `/goal` condition's exit clauses survive truncation. They sat at the end of a
  string sliced to 4000 chars, so long criteria would have silently removed the
  only stop condition and produced a goal loop with no exit. Latent, never fired —
  0 of 799 tasks with usable criteria exceed the cap, longest 1964 chars.

## [2026.8.11.3] - 2026-08-11

### Features

### Changes

- Worker tiles name a task's state with the canonical `STATUS_LABEL` vocabulary
  ("Assigned" / "In Progress") instead of words invented in the template. The old
  ternary printed "queued" for an ASSIGNED task, which names work nobody has picked
  up — so a BUZZING worker's tile described the opposite of what was happening, and
  the task board beside it called the same row "Assigned".

### Fixes

- Task status chips meet WCAG 2.1 AA in light mode. Both rules set near-black text
  against a background token that inverts between themes (`--accent` is `#F1B83D`
  in dark, `#7A5000` in light), so light mode rendered dark-on-dark: measured
  2.33:1 for the assigned chip and 2.81:1 for the active one, against the 4.5:1
  AA needs at that text size. Now 7.07:1 and 6.06:1.

## [2026.8.11.2] - 2026-08-11

### Fixes

- `ExitPlanMode` joins `AskUserQuestion` in `_NEVER_AUTO_APPROVE`. Its purpose is also to
  obtain a human decision — approval of a plan before work begins — so auto-approving it
  means an agent approves its own plan and everything downstream inherits it unreviewed.

### Features

### Changes

### Fixes

## [2026.8.11] - 2026-08-11

### Fixes

- **Drones can no longer answer the operator's `AskUserQuestion` escalations.** The tool
  was approvable like any other, and the drone's approval response for Claude is a bare
  Enter — which on an option picker selects the highlighted option. Escalations returned
  verbatim option labels the operator never chose, identically on every re-ask. 400
  occurrences since 2026-07-13. A new `_NEVER_AUTO_APPROVE` set is checked before rule
  evaluation, so no rule, queen delegation or empty config can supply a human decision.

### Features

### Changes

### Fixes

## [2026.8.10.20] - 2026-08-10

### Fixes

- `queen_view_worker_state` now returns the PTY tail in `structuredContent`, not only in
  the human-readable text block. The value was read and then dropped, so MCP clients (the
  Queen) received worker state with no terminal output — while `pty_tail_lines` advertised
  a line count, making an absent field look like an empty terminal. The dashboard was
  never affected; it reads PTY output over `/ws/terminal`.

### Features

### Changes

### Fixes

## [2026.8.10.19] - 2026-08-10

### Features

### Changes

### Fixes

- **A Jira integration that is connected but switched off no longer looks like it is working.** A second developer completed the OAuth round-trip, saw a green “✓ Connected — OAuth active” banner, hit “Discover workflow” and got “Jira integration not enabled” — both statements true and contradicting each other on screen. `connected` (an OAuth token exists) and `enabled` (the integration setting every `/api/jira/*` route gates on) are two independent flags, and the banner was rendered from the OAuth payload alone, which never carried `enabled`. The UI *could not* have told her; this was structural, not poor wording.

  `/auth/jira/status` now reports `enabled` alongside `connected`, and the banner renders that case in **amber rather than green** — looking green and working is the failure mode, so it must not borrow the success styling. All seven routes that returned the bare refusal string now share one actionable message naming the setting and where to find it. The refusal itself was always correct; what was missing was a path from the error to the checkbox that fixes it. This is the first wall each new developer hits after a successful OAuth setup, since `docs/jira-setup.md` is written for per-developer onboarding.

## [2026.8.10.18] - 2026-08-10

### Features

- **The Queen's card now says what she is doing.** Every worker row carries a state badge with a duration (“BUZZING — 1m”) and, when relevant, an “Awaiting your input” pill; the Queen rendered her name and the static subtitle “operator command center” — the one sidebar entry that never reported its own activity. A coloured border keyed to state existed, but that is readable only once you know the code and answers nothing about *how long*, which is the difference between “deciding” and “stuck since this morning”.

  A rendering gap rather than plumbing: `state`, `state_duration` and `needs_operator_input` were already on the card's dict via `_queen_dict`, so nothing new is computed. The needs-input pill is asserted separately from the state badge on purpose — “thinking” and “blocked on you” are not the same thing and only one of them is the operator's problem.

  **Known wart, flagged rather than hidden:** the state colour mapping is inlined in `queen_card.html` instead of calling `state_color()`, because the macros in `worker_list.html` are not in scope where that partial is included. Inlined copies drift, so a test pins the mapping; hoisting the macros somewhere both partials can import is the real fix.

### Changes

### Fixes

## [2026.8.10.17] - 2026-08-10

### Features

### Changes

### Fixes

- **Acceptance-criteria synthesis now fires on both paths that make a Jira-linked task assignable (#1354).** The verifier default-passes a task with no criteria, so a task that becomes assignable without them is unverifiable by construction — with 109 imported tickets waiting on the Queen, that mattered.

  *Gap B, the release gate:* `_handle_reassign_task` assigned through the raw board method, bypassing the coordinator's `assign_task`, which is what writes the ASSIGNED history row **and** fires the synthesis hook. Measured on #1358, a real import: the Queen assigned it and its history contained neither. Both now happen on that path, scoped exactly as the assign hook is. The ASSIGNED row is part of the fix, not bookkeeping — it is the only thing distinguishing “synthesis never fired” from “fired and returned empty”.

  *Gap A, create-then-link:* the assign hook only covers tasks that already carry a `jira_key`, which is right for import and backwards for the dashboard's “create Jira issue” button, where assignment precedes the link. Measured on #1352: assigned at 17:03:19 with no key, synthesis returned nothing at 17:03:26, the link landed at 17:03:31 and nothing retried. Synthesis now runs when the link is **written**, because that is when the Jira context arrives; retrying at assign time would feed it the same pre-Jira description that already came back empty. The assign hook's `jira_key` scoping is deliberately unchanged.

  **Deviation from the approved plan, stated in the code too:** the plan said route through the coordinator rather than duplicate. `board.assign` was kept because this handler is synchronous and uses its boolean to build a precise refusal — routing would mean firing async and reporting success before knowing, trading a real error message for tidier structure.

  **Not closed:** live verification on a fresh fixture and the empty-synthesis rate with its denominator are still outstanding, so #1354 stays open. A complicating finding for its method: every Jira-linked task inspected (#1313, #1314, #1315, #1325) has *zero* `task_history` rows — the import flow writes no history at all — so “no ASSIGNED row = never fired” cannot discriminate on imported tickets until the row this change adds starts appearing.

## [2026.8.10.16] - 2026-08-10

### Features

### Changes

### Fixes

- **Popping out the tasks panel no longer rewrites the operator's collapse preference (#1360).** `popOutTasks` called `setBottomCollapsed(true, true)` and the second argument persists. The visible behaviour looked right — the main panel stayed collapsed across reloads — while the meaning was wrong: closing the pop-out never restored the operator's own choice, so a control labelled “pop out” silently became “minimize this panel forever” and the caret changed behaviour with nothing to explain why. It is now session-only.

  **Deliberately not done:** tracking the pop-out's lifetime, so reloading the main window while the pop-out is open shows the panel again. Closing that properly needs a live cross-window signal — another shared `localStorage` key — and that mechanism has already produced two bugs in this one control (the popped window inheriting a collapsed state and rendering blank, and the popped window writing the preference back). The trade is recorded in the code so the next person does not rediscover it by hitting it.

## [2026.8.10.15] - 2026-08-10

### Features

### Changes

### Fixes

- **The header icon button now sits on its siblings' baseline on mobile (#1359).** `.btn-icon` used `font-size: 0.8rem` with an explicit `line-height: 1.2` while every sibling in that row is `.btn-sm` at `0.75rem`; a taller box in a flex row sits off its neighbours' baseline, which is hidden on desktop where the row has slack and plain on mobile where it does not. Metrics now match `.btn-sm` exactly and the glyph is centred with `inline-flex` rather than resting on a text baseline — that last part is what made a bare arrow read low even once the heights agreed. The regression test **parses both font sizes out of the stylesheet and compares them**, so it keeps holding if either changes rather than pinning a literal that would drift.

  Recorded because the process failure matters: this came from an email with a dashboard link and no body, which was written off as “nothing actionable” in the 2026.8.9.26 commit message and #1359 reported as fully addressed. An empty email is a *missing* requirement, not an absent one — the right move was to ask, as had been done for the other three.

## [2026.8.10.14] - 2026-08-10

### Features

- **A Jira-synced task now links back to its ticket.** `jira_key` had been in the task API payload the whole time and was rendered nowhere; the only trace that a task came from Jira was prose under the `--- Jira sync ---` marker in its description. Task rows now show the key as a badge linking to the issue.

  The URL comes from the **server** because the client cannot build it: Swarm reaches Jira via `api.atlassian.com/ex/jira/<cloudId>`, which is not browsable, while the human-facing host is the `url` from the OAuth accessible-resources response, already persisted as `_site_url` at auth time — no new config. The badge stops click propagation, since task rows are themselves clickable and every click would otherwise open both the ticket and the task modal. Tokens predating the site-URL field still render a **dashed badge**: losing the link is acceptable, losing the fact that a task came from Jira is the original complaint returning. The lookup is defensive at every hop and returns empty on failure — a dashboard that will not render because Jira is unconfigured is a far worse bug than a missing hyperlink.

### Changes

### Fixes

- **Two self-inflicted breakages caught by the full suite.** The `tojson` filter on `jira_site_url` raised `ChainableUndefined` on any render path that does not pass it — a blank page rather than a missing badge — and now defaults to empty. Separately, a test required `/static/theme.js` inside `sw.js` for an inline offline shell the kill switch removed; since the worker serves nothing and is no longer registered at all (PR #11), that shell cannot exist. It was replaced with an assertion that actually holds — no fetch handler, and if it ever serves content again that content needs the theme — while `/static/offline.html` is still asserted to carry the theme directly.

## [2026.8.10.13] - 2026-08-10

### Features

### Changes

### Fixes

- **Stop re-registering the kill-switch service worker — a self-sustaining navigation loop (PR #11, found by codex).** 2026.8.10.10 turned `/sw.js` into a kill switch whose activate handler unregisters itself and navigates every controlled client, while `base.html` still registered `/sw.js` on every production load. That cannot converge: load → register → activate → unregister + navigate → load. Every iteration is a navigation plus a service-worker registration, both **browser-process SQLite writes** — precisely the 6,677MB of sqlite the operator's own trace measured. Reproduced twice in clean Edge sessions with only Swarm open; one process reached 4,096MB within a minute.

  Registering a worker whose entire purpose is to unregister itself is incoherent, so the registration is removed outright. The page-side cleanup stays — existing registrations are unregistered and `swarm-*` caches deleted — so installs from earlier releases are still cleaned up.

  **What this does not explain:** .10 shipped around 01:35 UTC and the leak began at 21:48 UTC, roughly four hours earlier. This loop accounts for the recent acceleration, not the original growth; the title-flash/app-badge experiment in .12 remains open. Credit to codex for finding it while the author was insisting the page was a bystander and blaming Edge, then extensions.

## [2026.8.10.12] - 2026-08-10

### Features

### Changes

- **Removed two per-event browser-process writes — an experiment, not a proven fix.** Stated plainly because three earlier guesses that evening were each called “found it” and were each wrong. `startTitleFlash` rewrote `document.title` **every second, indefinitely**, while any event went unacknowledged, and each assignment is an IPC the browser process records in its History database; it now sets the count once. `updateAppBadge` became a guarded no-op, since for an installed PWA the badge is persisted by the browser and every call was another browser-process write.

  Measured beforehand: browser process sqlite at 6,677.6 MB against a normal 286.7 MB GPU process and 131.7 MB renderer heap, with growth stopping and slowly dropping when Swarm was closed. Ruled out by measurement: Cache Storage (empty), all storage APIs (0B of a 306GB quota), network looping, the service worker (unregistered, still leaked), notifications (blocked, still leaked), renderer heap, DOM nodes, GPU.

  **Not proven:** that these two calls write that database — the trace reports sqlite in aggregate and will not break it down. They are suspects only because they are per-event browser-process writes on a path that the day's classifier fix turned from near-dormant into continuous. Either outcome is useful: the theory holds or these are exonerated and the search moves on.

### Fixes

## [2026.8.10.11] - 2026-08-10

### Features

### Changes

- **The dashboard event socket is now instrumented — the last uninstrumented channel.** Storage was 0B of a 306GB quota, Cache Storage empty, the service worker unregistered, the network tab quiet after ~45 requests at load, and the renderer sitting at 264MB and 0% CPU with 1,639 DOM nodes — while the **browser** process was at 5.2GB and 4.3% sustained CPU. Every counter built that night measured the renderer or the terminal socket; `ws.onmessage` on the main `/ws` connection was never counted, and it is the one remaining place a page can grow the browser process, because frames arriving faster than the handler drains them buffer *there*, not in the renderer — exactly the observed shape. `evMB` and `evMsgs` now report bytes and message count for that socket, so the next heartbeat either names it or clears it.

  The timing points the same way: 2026.8.9.24 at 21:47 fixed the classifier so workers actually change state, the first crash was at 21:48:47, and every state change broadcasts on this socket.

### Fixes

## [2026.8.10.10] - 2026-08-10

### Features

### Changes

### Fixes

- **The service worker is removed entirely — both a kill switch and a diagnostic.** With only Swarm open, the operator's Edge **browser process** climbed past 5GB at 4.3% sustained CPU and “very high” power while the Swarm renderer sat at 264MB and 0% CPU. The page is not looping; something between the page and the network/storage layer is, and the service worker is the only Swarm component living there — it intercepts every request and writes Cache Storage, both browser-process work in exactly the process that was growing.

  The previous fix to its fetch handler was real (it had cached `/api/health?_=<timestamp>`, a unique URL per poll, into an unbounded cache; removing that took the browser process from 12,316MB to 88.8MB in a controlled test) but evidently not the whole story. Rather than adjust it a third time, the worker now unregisters itself, deletes every cache it ever created, and navigates open windows so it takes effect without a second reload.

  This answers the question either way: if browser memory and CPU normalise the worker is confirmed and can be restored piece by piece from git history; if they do not it is exonerated outright and the search moves elsewhere. That is worth more than a seventh speculative patch — six hypotheses that night were wrong and one of them caused an 11GB regression of its own. **Cost:** the PWA loses offline support and app-shell precaching; the app is fully server-rendered and does not otherwise depend on the worker.

## [2026.8.10.9] - 2026-08-10

### Features

### Changes

### Fixes

- **Bumped the PWA cache to `swarm-v23` so the already-leaked entries are actually evicted.** 2026.8.10.8 stopped *new* entries being written but did not remove the gigabytes already banked in `swarm-v22` — one permanent Cache Storage entry per `/api/health?_=<timestamp>` poll, which had grown the browser process to 12.3GB. The activate handler already deletes every cache whose name is not `CACHE_NAME`, and `skipWaiting()`/`clients.claim()` were already set, so renaming the cache is what reclaims the space, on the next load and with no manual “clear site data” step for the operator.

  Two tests, deliberately paired: the version must be past the leaking one **and** the eviction that gives the bump its meaning must still exist. They live in different parts of the file, so a bump with the eviction removed would reclaim nothing while looking correct — the same “computed but not connected” shape that produced three separate defects that night.

## [2026.8.10.8] - 2026-08-10

### Features

### Changes

### Fixes

- **The service worker cached every URL — a 14GB browser-process leak.** With all extensions disabled and only Swarm open, memory climbed to 12–14GB and was reclaimed only by fully quitting Edge, never by reloading. `sw.js` put every response into the cache in its catch-all branch, and `cache.put` is keyed by URL — bounded only while the URLs are. They were not: the dashboard polls `/api/health?_=<Date.now()>`, a unique URL every call, so each poll wrote a permanent Cache Storage entry that nothing ever evicted.

  **Why it survived an entire evening of investigation:** Cache Storage lives in the *browser* process, and every instrument built that night measured the *renderer* — JS heap flat at 17MB of a 4192MB limit, ~1,450 DOM nodes, canvases, WebSocket bytes. All flat, all accurate, all irrelevant. It also explains each symptom that kept contradicting the working theories: the renderer stayed tiny, reloads never freed anything (the cache outlives the page), and only a full browser exit reclaimed it. A Task Manager screenshot — Swarm's renderer at 116MB beside a 12,316MB browser process — is what localised it, after five wrong hypotheses; no page-side counter could have produced that reading.

  Only same-origin GETs under `/static/` are now cached, and never with a `_=` cache buster. API responses are live state, so caching them durably was a correctness bug waiting to happen as well as a leak, and the offline **read** fallback is preserved — skipping the write must not skip the read. Existing Cache Storage is not cleared by this change; the operator should clear site data for the PWA once.

## [2026.8.10.7] - 2026-08-10

### Features

### Changes

- **Reverted 2026.8.10.5's blanket disabling of xterm's GPU renderers — it cost 11GB in two minutes.** That release turned off both GPU renderers on the theory that a WebGL crash explained the operator's dead tabs, which dropped xterm to its DOM renderer — one DOM element per character cell. With a full-size terminal and 5000-line scrollback it allocates without bound, and the browser climbed to eleven gigabytes at roughly 1% per second, then twelve.

  **Every counter added so far sat flat while it happened:** heap 20MB, `wsMB` 1MB, canvases 0, two terminal attaches in three minutes, daemon idle. DOM nodes live in C++ memory, so the instrument was blind again — the third distinct memory class this investigation could not see, after the JS heap and array buffers. The `nodes` field added to the heartbeat here is the reading that would have caught it, shipped so the next blind spot is smaller.

  Windows had WebGL→Canvas before and crashed every 6–9 minutes; that is bad and **still open**, but it is far better than 11GB in two minutes, and reverting to known prior behaviour is correct while the true cause is unidentified. Kept from the reverted releases: the `_isMac` fix (a genuine bug — a case-sensitive match never hit “macOS”, which is what `userAgentData` reports and is checked first), the 256KB replay cap and the reconnect-storm fixes (attach rate fell from 99/hour to single digits), and the heartbeat itself, which is what made any of this diagnosable.

### Fixes

## [2026.8.10.6] - 2026-08-10

### Features

### Changes

### Fixes

- **Terminal replay is capped at 256KB, and the heartbeat can finally see buffer memory.** The crash dump settled it: Edge exception `0xE0000008`, Chromium's out-of-memory code, on a 2MB allocation — not the GPU crash the previous release assumed from the signature. It also explains the contradiction that had been reasoned past twice: `performance.memory.usedJSHeapSize` counts **only** the JS heap and excludes ArrayBuffer backing stores, and the terminal replay arrives as binary WebSocket frames — exactly that. The instrument was blind to the memory that ran out, and a positive control confirming the metric *can* move would have caught it before two wrong diagnoses.

  Replay drops from 1MB to 256KB, with the cut landing on a **line boundary**: slicing mid-line hands xterm a partial ANSI escape, which it renders as garbage or swallows along with the text after it, and a shorter first screen beats a corrupted one. About 3,000 lines of scrollback survive. The heartbeat now also reports `wsMB` (cumulative bytes delivered over terminal sockets) and `procMB` (`measureUserAgentSpecificMemory` where permitted), so the next buffer-memory problem cannot present as a flat heap and a dead tab.

  **Caught by the negative control, not by writing the test first:** removing the trim's *call site* left the whole suite green, because every test exercised the helper in isolation — the cap could have been deleted invisibly. It is now pinned including ordering, since trimming after `send_bytes` would be a very tidy no-op. That is the third instance in this investigation of the same shape: something computed correctly and then not connected. “Is it correct?” and “is it wired in?” are separate questions.

## [2026.8.10.5] - 2026-08-10

### Features

### Changes

### Fixes

- **xterm's GPU renderers are disabled on every platform — the tab crash is a GPU crash.** The heartbeat shipped in the previous release made this measurable rather than arguable: five minutes of perfectly flat memory (17–20MB of a 4192MB limit, one terminal, four canvases) followed by instantaneous death with the popped-out window not even open. No climb, no spike — that excludes memory, leaks and the pop-out by measurement. An earlier crash killed both browser windows in the same instant, which is a shared GPU-process crash and nothing else.

  **Platform correction that changes the conclusion:** the previous release fixed the `_isMac` WebGL guard on the belief the operator was on macOS, from a month-old memory note. They are on Windows with Edge, against a daemon on Linux. So `_isMac` was correctly false for them and that fix, while a real latent bug for Mac users, did nothing here — and what it reveals is worse for WebGL: they are on the platform where the code deliberately left it enabled and they get the macOS crash signature anyway. The macOS carve-out was treating a platform-specific symptom of a problem that is not platform-specific.

  The Canvas fallbacks live inside the WebGL branch and are skipped with it, since Canvas is also a GPU path with the same exposure; a test pins that, because hoisting a Canvas fallback “for perf” would silently reopen this. The code's own comment already conceded the trade — “perf is a non-issue for viewing worker output”. The heartbeat now counts **canvases** specifically, replacing a vague `canvas, .xterm` mix that could not tell the renderers apart: xterm's DOM renderer creates zero canvases, so the count is a direct read of whether a GPU path is live, and the verification comes from the operator's browser rather than from inference. The misleading memory note was corrected too — the daemon running on Linux says nothing about the browser, and a month-old note is not evidence about today.

## [2026.8.10.4] - 2026-08-10

### Features

### Changes

### Fixes

- **The macOS WebGL guard never fired — one missing `/i` behind four tab crashes.** `dashboard.js` already documented this exact failure (“macOS Chromium/Edge crashes the *whole* renderer through xterm's WebGL path on a redraw”) and the guard was correct in intent. It just never matched: `navigator.userAgentData.platform` returns `"macOS"` with a lowercase m and is checked **first**, against a case-sensitive `/Mac|iPhone|iPad|iPod/`; `navigator.platform`'s `"MacIntel"` would have matched but was never reached. So `_isMac` was false on every Mac with `userAgentData`, WebGL loaded, and the guard protected nobody. It is now case-insensitive and matches the spec's closed set of platform values as well as the legacy ones — with `"Chrome OS"` asserted as the trap in the other direction, since a naive `/os/i` would drop Windows-adjacent platforms to the DOM renderer for nothing.

  Diagnosed by a **heartbeat posted to the daemon every 30s** (so it survives the tab), which showed the heap flat at 17–28MB of a 4192MB limit with one cached terminal, and both windows stopping in the same instant — ruling out every memory hypothesis and pointing at a GPU-process crash.

  **The same class of bug, twice in one investigation:** the heartbeat client sent `plat` and `webgl` and the server never logged them, so the first reload after the fix produced a heartbeat that could not confirm the fix — which is the entire point of having one. Both fields now reach the log, with a test.

## [2026.8.10.3] - 2026-08-10

### Features

### Changes

### Fixes

- **The terminal reconnect storm is broken — the retry cap was decorative.** `MAX_TERM_RECONNECT` caps retries at 3 and the per-entry counter honoured it (the console dutifully printed 1/3, 2/3, 3/3), but on exhaustion the code called `destroyTermEntry`, the re-render re-entered `attachInlineTerminal`, and *that* built a new entry with a fresh budget. Connect, fail, retry three times, destroy, re-attach, forever — with every cycle calling `term.reset()` and pulling a new 1MB replay snapshot from the server. Measured in a browser: eleven terminal sockets in eight seconds for one worker, unprompted; in the daemon log, 35–43% of every attach across the whole day lasted under two seconds, up to 99 attaches in a single hour at 1MB each.

  Two independent ways the cap was unenforceable, both closed. A 30s **cooldown** after exhaustion, stamped *before* `destroyTermEntry` since destroy is what triggers the re-entry — an explicit `selectWorker` clears it, because the cooldown must break an automatic loop and never make a terminal unreachable. And the reconnect budget is now earned by **surviving** rather than by connecting: it was zeroed in `onopen`, so a socket that opened and then dropped refunded its budget every time; it now clears only after ten seconds of a live connection. Cache size (10) and the 1MB replay are unchanged — fixing the churn came before trading away scrollback.

  **A correction owed to the record:** the claim that this predated the day's changes, on the grounds that the churn appears in yesterday's logs, was bad reasoning. Churn existing earlier says nothing about what started the crashes; the sub-2s rate was flat all day, but the storm's cost scales with how heavy each cycle is, and 2026.8.9.27 doubled the worker-list swap.

## [2026.8.10.2] - 2026-08-10

### Features

### Changes

- **The worker list is no longer re-rendered into two windows at once.** `partials/worker_list.html` is re-fetched and swapped wholesale on `workers_changed` plus about a dozen other socket events, and the custom selector roughly doubled the cost of every swap — for 16 workers, 12,141 → 25,442 bytes, 147 → 280 elements, 17 → 34 `<img>`. With the tasks panel popped out it was happening in two windows.

  Three guards: the popped-out window no longer fetches it at all (`.worker-list` is `display: none` there, so every swap was pure waste in exactly the window the crash was reported against); bursts coalesce over 120ms, so sixteen workers changing state produce one repaint instead of a flurry of full fetches; and the list is never swapped while the selector is **open** — that surfaced as “Element is not attached to the DOM” in the browser tests, which is the same event a finger hits when a refresh replaces the rows mid-tap. `window.refreshWorkers` is exposed so the coalescing can be driven directly; without a handle the test would call `window.refreshWorkers && …`, silently do nothing, and pass while measuring nothing — how two earlier tests here went green against a broken build. The pop-out guard is likewise asserted by counting the **requests the browser makes**, since a guard that is present but bypassed by another caller reads identically in a source scan.

  **What this does not establish:** that it is the whole cause of the Edge crash. Sustained DOM churn is consistent with a tab dying, but so is memory growth in xterm or a socket leak, and neither was measured. The discriminating question on the next crash is whether it happens with the pop-out closed.

### Fixes

## [2026.8.10] - 2026-08-10

> Shipped in commit `8464592`, whose subject reads `release: 2026.8.9.29`. The
> date rolled over between the version bump and the commit, and that subject was
> hardcoded rather than taken from the release script's output. **2026.8.10 is
> the version that shipped** — `pyproject.toml` and `__init__.py` both say so.
> Corrected here rather than by rewriting a pushed commit.

### Features

- **Worker state is now a background wash in the worker selector, not just a stripe.** The 3px stripe still made you read a row to place it; a tinted field is what lets a block of sixteen sort itself at arm's length, which is the real ask behind “hard to scan because it all looks the same” (#1359).

  The tint is **mixed into `var(--panel)`** rather than layered over it as `rgba`, so it stays dark in the dark theme and light in the light one without being tuned twice. It is held at 14% (18% for STUNG) on purpose: the row text is already state-coloured and the app targets WCAG 2.1 AA, so a heavier wash starts eating the contrast of the very text it is meant to highlight — raising it is a judgement for the operator looking at a real phone, not a colour value chosen here. Beyond the flat tint, WAITING and STUNG get the only edge treatment (WAITING means “this one needs you”, the stated reason for scanning the list at all, so it must be findable without comparing tints against each other), SLEEPING recedes rather than competing, and the hover/keyboard highlight was strengthened because a wash under every row can swallow the active-row indicator on the brighter states and make the list *harder* to drive.

  **Tested as computed style in a real browser, not as CSS source.** A rule can be present and still lose to a later selector, resolve to the same colour through two different variables, or fail outright if `color-mix` is unsupported — each renders sixteen identical rows while a source scan stays green. This control had already produced three defects that only a browser caught.

### Changes

### Fixes

## [2026.8.9.28] - 2026-08-09

### Features

### Changes

- **Two of the session's own test bugs fixed rather than worked around.** `wait_for_selector` was being used to wait on a `[hidden]` element, and it defaults to waiting for *visible* — a wait that can never succeed. Fixed-size character windows in the source scans went red purely because a function grew; they are now bounded by the function itself, the second time in one session that a guessed character window manufactured a false failure.

### Fixes

- **The worker selector was clipped to a ~45px row — 14 of 16 workers unreachable at any scroll position.** Two ancestors did the cropping, neither visible from the selector's own markup or CSS: `.panel { overflow: hidden }` (and `.worker-list` *is* a `.panel`), plus `overflow-y: hidden` on the pill scroller `.worker-list > .panel-body`. On mobile that body is a single row, so the absolutely-positioned list was cropped to roughly one option — worse than showing fewer, because nothing on screen indicates the rest exist. It is now un-clipped only while open, via the body class the control already toggles, and raised above sibling panels: escaping the overflow without that just lets it paint underneath the terminal panel, which looks identical to still being clipped.

  **The first two tests written for this could not detect it** — they passed with the fix removed. Ancestor clipping does not change an element's layout box, so `bounding_box()` and `is_visible()` both report a cropped row as present and correct, and the reachability loop called `scroll_into_view_if_needed`, which scrolls the *page* and destroys the measurement it was set up to take. The honest measure is what the browser **paints**: scroll the list through its own range and collect the rows `elementFromPoint` resolves to — sixteen reachable with the fix, one without. Found only because the negative control was run.

- **The selector trigger ignored your tap until the next server render.** Its label is rendered from `selected_worker`, so after choosing a worker it kept reading "Select a worker" until the next partial refresh, making the control look like it had dropped the input. A native `<select>` repaints its selection instantly; a custom one gets nothing for free. The trigger now updates from the chosen row immediately, with the server render confirming rather than supplying it. A regression introduced in the preceding release and caught in the browser.

## [2026.8.9.27] - 2026-08-09

### Features

- **A custom worker selector, because a native `<option>` is a structural dead end (#1359).** The operator's diagnosis — "hard to scan because it all looks the same" — was correct about the *limit*, not about styling: an `<option>` holds text and nothing else, no icon, no colour, no second line, and no CSS reaches inside it, so sixteen workers rendered as sixteen identical grey strings. Each row now carries a per-state colour stripe down its left edge, the state bee icon, the name coloured by state, a "needs you" marker for workers awaiting the operator, and the current task; provider is kept but demoted, having been taking equal visual weight for the least useful field.

  **Two things preserved deliberately.** Row order still follows the pill list, honouring the operator's 2026-08-06 decision ("the order should follow the same order that the workers are listed in the UI, that'll help with visual muscle memory") which had already reversed an earlier attempt to sort attention to the top — a custom control makes redoing that quietly very tempting. The guard for it was a blunt scan for `selectattr(` anywhere in the file, which the new trigger label would have tripped for an unrelated reason; it is now scoped to the option list so it measures the guarantee it claims. And open state lives on `<body>`, not on the control: the partial is re-rendered wholesale on every `workers_changed`, so control-local state dies with it and the list slams shut mid-read. Every handler is delegated on `document` for the same reason.

  Accessibility is explicit because this replaces a control that was accessible for free: combobox/listbox roles, `hidden` when collapsed so a screen reader is not read sixteen off-screen workers, full keyboard including Escape, focus held on the listbox with `aria-activedescendant`, and 44px rows per WCAG 2.1 AA.

### Changes

### Fixes

- **The first version of the selector hid the entire dashboard.** It closed `.wsel` but not `.worker-switcher`, so the browser reparented the whole rest of the page inside a wrapper that is `display: none` above mobile width — reproducing the exact symptom reported twice before ("the panel is not visible; the popped window is blank"). **All fifteen new source-scan tests passed on that broken markup**: each asks whether a string is present, none asks whether the document parses, and only the real-browser test found it. Tag-balance checks now cover the switcher block and the whole partial, since editing templates by string replacement is precisely how it happened. Bisecting rather than guessing mattered — the first hypothesis was a JS error killing the IIFE, and reverting `dashboard.js` alone did not fix it.

## [2026.8.9.26] - 2026-08-09

### Features

### Changes

### Fixes

- **Saving the config page no longer jumps the document under you (#1359).** `#unsaved-banner` is the first child of `<body>`, above `<header>`, toggled between `display: none` and `block`. A save cycle fires it three times — "Unsaved changes *" on the first keystroke, "Saving…", "✓ Saved" — so a ~30px band was inserted and removed above everything, moving the whole page twice per save. Config autosaves 1.5s after the last change, so this happened unprompted with the operator's eye still on a field. The banner is now floated, matching `.toast-container` on the dashboard so both pages behave the same way, offset below the header rather than over it (floating it across the Dashboard/Config buttons would trade a jump for a misclick), and moved to the bottom under 600px so it is not sitting on the field being edited.

- **The pinned worker chip beside the mobile switcher is gone (#1359).** "It was showing the worker to the right of the drop-down menu which was odd cuz it didn't reflect anything." This reverses an earlier request of the operator's — dropdown *plus* the active worker always visible — at his request, having now seen it in use: a native `<select>` always renders its selected option, so the chip repeated the text a few pixels to its left. The only thing it added was the bee icon and state colour, which belong *inside* a formatted row rather than bolted alongside. The two tests that pinned the chip now assert its absence instead of being deleted, so re-adding it has to be a decision rather than an accident. The fourth email in the batch was a bare dashboard link with no body — recorded as non-actionable rather than invented into a requirement.

## [2026.8.9.25] - 2026-08-09

### Features

### Changes

### Fixes

- **Root cause of the blank popped-out panel, after two reports: it inherits a *minimized* main window.** The collapse preference lives in `localStorage`, which the popped-out window shares with the main one, so a panel minimized in the main window opened the pop-out already collapsed — a bare header and nothing else until a tab was clicked. Adding "popping out collapses the main window" in .24, at the operator's own request, turned that from intermittent into every single time, which is what finally made it reproducible. In the popped window the panel *is* the window, so the stored preference does not apply there, and the pop-out never writes it back either: with one shared key, persisting from the pop-out would silently redefine what the caret means in the main window. The .24 grid fix was a real defect (a `flex-direction` on a grid, doing nothing) but was **not** the cause — recorded so it is not re-litigated.

- **Putting a worker to sleep now survives the daemon.** Carrying `state_since` in the store (.24) was necessary but not sufficient — nothing was writing it. Every other state change goes through the pilot and emits `state_changed`, which is what the publisher persists on; `sleep_worker` assigns the attribute directly, and on an already-RESTING worker there is no transition to emit at all. The backdated timestamp therefore lived only in memory and died with the daemon: "I set several to resting and some to sleep, but on reloading only public-website was resting." The regression test starts its worker RESTING deliberately, so a fix that merely emitted an event on transition still fails it.

- **A `ReferenceError` shipped in .24.** `setBottomCollapsed` lives in the main IIFE and `popOutTasks` in the Command Center one, so the bare cross-IIFE call threw and took the rest of the function with it. Caught by `test_dashboard_no_bare_cross_iife_calls`, which is exactly what it exists for.

## [2026.8.9.24] - 2026-08-09

### Features

### Changes

- **The temporary first-poll content dump is removed** now that it has served its purpose — it was also writing test workers into the real `~/.swarm` during the suite.

### Fixes

- **Root cause of #1357, and it was never the persistence: a completed turn was being read as a running one.** The restore worked — the rebuild diagnostic shows "16 remembered … 14 left non-BUZZING after restore", and one second later all fourteen read `was=RESTING decided=BUZZING`. The pilot's first poll overwrote correct state, so persisting harder could never have fixed it. `_RE_SUBAGENT_ACTIVE` treats "`<glyph> <verb> for <digits>`" as an active turn, and that shape means two opposite things: mid-turn ("✻ Sautéed for 16m 13s", still working) and turn-over ("✻ Brewed for 1m 58s", Claude Code's completion summary). The second sits above the returned input box on an idle worker, short-circuiting to BUZZING before the RESTING branch was reachable.

  Both meanings are real, so the pattern is **split rather than narrowed**: where a prompt is already visible with no "esc to interrupt", the turn has ended and elapsed time is history, so those three sites use `_RE_SUBAGENT_IN_PROGRESS` (live spinner ellipsis or subagent token counter). The stuck-BUZZING safety net keeps the broad pattern, where a false positive merely holds a busy worker at BUZZING — the safe direction, and the reason the 16m capture put it there. **Measured, not reasoned**: all sixteen first-poll buffers were captured during a real reload and replayed offline, 16/16 BUZZING before and 14 RESTING / 2 BUZZING after, matching the persisted map exactly. Four buffers are kept as fixtures with a positive control asserting the ambiguous shape is still present, so the test cannot quietly stop reproducing the bug.

- **SLEEPING survives a restart.** SLEEPING is not a stored state — `display_state` derives it from how long a worker has been RESTING — and "put to sleep" works by setting RESTING and *backdating* `state_since`. Persisting the state alone threw away the only thing that made it SLEEPING. The store now carries both, reads its own earlier bare-string payload rather than discarding a good map mid-upgrade, and refuses a future timestamp.

- **Popping the panel out now collapses the one left behind.** Operator request: the point is to move the panel off this window, not to run two renderers side by side. The collapse is persisted so it survives a reload the way the caret would, and a blocked popup leaves the panel alone — collapsing then would hide the only copy there is.

## [2026.8.9.23] - 2026-08-09

### Features

### Changes

- **Two diagnostics added for #1357, because the first measurement answered only half the question.** Every worker read `was=BUZZING` at first classification, so the restore did not take effect — contradicting an earlier claim that it ran. But the same log shows workers whose visible tail is plainly idle (a bare `❯`, or `⏸ manual mode on · ? for shortcuts`) *also* deciding BUZZING on the first poll; classification is stateless per poll, so fixing the restore alone would not have fixed the operator's screenshot. Persistence is not the broken half — `save()` is wired and the stored row is fresh and correctly varied. So: a WARNING at the rebuild site counting remembered vs adopted-existing vs adopted-new vs restored, which tells "loaded nothing", "took the existing-worker branch" and "restored then overwritten" apart in one restart; plus a one-shot dump of each worker's full first-poll content to `~/.swarm/first-classification/` so the classifier decision can be replayed offline instead of guessed at from a 160-character tail.

### Fixes

- **The popped-out panel is now laid out as the grid it actually is.** `.detail-area` is `display: grid`, and the panel-mode rule shipped in .21 set `flex-direction: column` on it — which parsed, applied, and did nothing, so the panel kept the terminal's three grid tracks. Measured live at 1100x800: rows `415.86px 0px 233.94px`, i.e. ~230px of the window given to an empty track, and at shorter heights the track the panel lands in collapses and the window renders blank. It is now collapsed to a single cell with `.bottom-tabbed` placed into it. The pop-out icon also picks up the `margin-left: auto` that `.btn-collapse` used to carry, so the two sit together at the right edge instead of stranding at the end of the left group, styled neutral so it reads as a control rather than a third orange action button. The earlier panel tests asserted what should be **absent** and never that the panel was **present**, which is exactly how a blank window passed them; the new ones assert the property and are negative-controlled.

## [2026.8.9.22] - 2026-08-09

### Features

### Changes

- **Diagnostic logging for what the first classification after a restart decides, and from what (#1357).** 2026.8.9.21 persisted worker state and did **not** fix the reported symptom — the fleet still came up all-BUZZING after a reload, and the store then faithfully recorded that. Already established, so the next step does not re-litigate it: the *save* side works (it tracked three live transitions, 16 BUZZING → 15/1 → 14/2 → 13/3, matching the API at each step), the *restore* runs (`self.workers` starts empty, so every worker takes that branch on a cold start), and the saved map was ~2 minutes old, well inside the 30-minute staleness window. So something overwrites the restored states immediately; the leading hypothesis is that re-attaching to a PTY replays a snapshot of recent output whose activity indicator is classified as live work, which would mean the restore can never win — and fits the original report (4-6 seconds, then correct, all sixteen flipping together).

  This ships evidence rather than a second guess, having already been wrong once on this bug by reasoning from code instead of measuring. For each worker it logs the state going in, the state decided, the content length and a bounded tail of what it decided from. WARNING level because operators run at the default and an INFO diagnostic is invisible — the same mistake the worklog success line made — and bounded to one line per worker per daemon start. **Deliberately temporary**, tagged `[#1357]` so it can be removed in one grep once the cause is known.

### Fixes

## [2026.8.9.21] - 2026-08-09

### Features

### Changes

- **Toolbar tidy-up.** The "Preview Jira" button is removed as requested, along with its now-unreachable handler — the export dry run still lives on the config setup page, which is where it belongs. The `/api/jira/preview` route is deliberately left in place: removing a working API surface is more than "remove the button". "Pop out" becomes an icon (↗) in the utilities row beside the collapse caret, rather than a bordered button wedged into the tab group where it read as another tab.

### Fixes

- **Worker state is remembered across restarts (#1357).** Both halves of the operator's screenshot report — "all workers show BUZZING for 4-6 seconds after a reload" and "state is not remembered between reloads" — were one line: `src/swarm/worker/worker.py:190`, `state: WorkerState = WorkerState.BUZZING`. Nothing persisted worker state, so every daemon start constructed all sixteen workers with the dataclass default — and that default is the *most active* state. The dashboard rendered the daemon's belief faithfully until the pilot's first poll classified each worker from its PTY; the screenshot's tell was every worker reading an identical "BUZZING — 4m".

  States are now saved **on change** (one small write per real transition, not one per poll across sixteen workers) and restored when a worker is adopted, stored as a config key rather than a new table — it is one map, rewritten in place, never queried or joined, and a schema migration for data with no relationships and no history is the wrong trade. **Three guards, each with a control**: state older than 30 minutes is discarded, since a daemon down overnight knows nothing useful and stale state shown as current is the quieter version of the bug being fixed; STUNG is never restored, because a crashed worker may well have been revived *by* the restart; and an empty save does not wipe the memory, so a rebuild that momentarily sees no workers cannot erase it. The pilot still re-classifies from the PTY immediately — this only decides what is shown in the meantime.

## [2026.8.9.20] - 2026-08-09

### Features

### Changes

### Fixes

- **Panel mode rendered completely blank.** Opening `/?panel=tasks` gave an empty page: correct title, correct header, nothing else. `.bottom-tabbed` is a *child* of `.detail-area`, and the CSS hid `.detail-area` wholesale — hiding the one panel the mode exists to show. The grid rules were wrong for the same reason: they placed `.bottom-tabbed` at `grid-column 1`, but grid placement applies only to direct children and it is nested a level down. The detail area's *other* children — the terminal panel and its resize handle — are now hidden instead, and the detail area carries the tabbed panel at full height.

  **Every test passed anyway, and that is the part worth recording.** The source scans checked the CSS rules existed; the browser test checked `#tab-tasks` was in the DOM, the sidebar was not visible and the detail area was not visible — all true of a blank page. Nothing asserted the panel was *visible*. Absence was tested and presence never was, so "hid everything including the target" satisfied the suite completely. The browser test now asserts the panel is visible **and** taller than 200px, since a collapsed-to-zero panel is technically present too, and a unit test pins that `.detail-area` is never hidden wholesale again.

## [2026.8.9.19] - 2026-08-09

### Features

- **The tasks/decisions panel can be popped out into its own window (#1353).** Task management now lives in that panel — promotion approvals, blocked tasks, the Decisions tab — so it is watched continuously while the worker terminals are used for something else, and on one screen the two compete. **It is the same page**, opened at `/?panel=tasks` with a `panel-mode` body class that hides everything else; deliberately not a second template, because a standalone page would need its own copy of the task renderer, the socket wiring and every element id, and two renderers for one panel *drift* with the server-rendered one winning on load — which already happened to the Jira mappings panel. Reusing the page means one renderer, one reconciler, and every action handler (approve, dismiss, start, complete, and the `X-Requested-With` header they need) works unchanged.

  **The cost that had to be designed around:** `window.open` copies the opener's `sessionStorage`, and the dashboard restores the previously-selected worker from it on load. Unguarded, the popped window would mount an xterm and hold a *second* PTY subscription for a terminal nobody can see — and terminal traffic is the heaviest thing this daemon moves. Both reads of the stored worker are now gated on panel mode, including the one at the top of the IIFE that runs before `init()`. The body class is applied at parse time rather than in `init()`: it is purely presentational, must not depend on all of `init()` completing, and applying it before first paint avoids flashing the full dashboard. The window is **named**, so pressing the button twice focuses the existing one rather than opening a second copy holding its own socket, and the control hides itself inside the popped window.

  One of the four negative controls passed with the fix removed: the scan looked for `_panelMode` within 500 characters of the read and swept up its *declaration* a few lines above. Proximity of a name is not a guard — tightened to 160 characters and cross-checked against the browser test, which fails independently by asserting no xterm is mounted.

### Changes

### Fixes

## [2026.8.9.18] - 2026-08-09

### Features

### Changes

### Fixes

- **The Jira blocker note is written for the reporter, not the operator.** The same defect as the closing comment on a different surface, found because testing that comment left one on a live ticket reading "Swarm is BLOCKED on this: Closing-comment template needs the 2026.8.9.17 reload before I can test it…". Block reasons are written worker-to-operator; that text landed verbatim on a ticket a reporter reads, and on a service desk it tells a customer nothing except that something internal is happening.

  **The split is drawn by what is safe to say.** `external_blocker_ref` is an artifact by design — `swarm_block_on_external` asks for "npm eslint@^10" or a PR URL — so naming it tells a reader something true and checkable ("Work on this is paused while we wait on: platform release 6.2."). The free-text reason stays inside Swarm where the operator sees it; the operator-decision sentinel reports as "pending a decision from the team" with no reason at all, those being the most internal of the three; and with no artifact it degrades to "waiting on a dependency". "Swarm is BLOCKED on this" is gone too — that is our vocabulary, not the reporter's. Four tests from 2026.8.9.6 asserted the old wording, including that the raw reason appears; they now assert its **absence** with the reason recorded, rather than keeping the old contract alive because tests encoded it.

## [2026.8.9.17] - 2026-08-09

### Features

### Changes

### Fixes

- **Jira-synced content was mutated in memory and never written to the database.** Found live while testing the closing comment: for one linked task the API (daemon memory) showed the synced block present and the database showed it **absent**. `refresh_synced_content` mutates the board's own `SwarmTask` object, and the sweep persisted only when there was *also* a notifiable comment — so a reporter or due-date update, or a ticket whose only new comment is Swarm's own (which 2026.8.9.15 now suppresses), changed memory and never reached the database. It survived until some unrelated board write flushed it incidentally, and was lost on restart. That incidental flush is why it looked fine an hour earlier: one task had blocker activity writing the board on its own, the other had none.

  **Persist on change, notify on news** — two different questions that one condition was answering. The sweep now compares the description before and after, writes when it actually changed, and notifies only when there is something a worker should read. Unconditional writing is equally wrong: this runs every cycle for every open linked task and would churn the board and its broadcasts forever. Both directions have a control. Three older tests mocked `refresh_synced_content` to *report* a change without making one, which resembled nothing once persistence keyed off the real mutation — the mocks now mutate like the code they stand in for, rather than the assertion being loosened to keep them green.

## [2026.8.9.16] - 2026-08-09

### Features

- **The Jira closing comment is rewritten in the team's own voice.** The last open question from the v2 interview was to confirm the format against the team's documented standard responses — **there is no such document**, so the house style was measured from the team's own resolved service-desk tickets instead: short, plain, addressed to the reporter, signed off, with org-preferences adding "professional, ministry-oriented". The old template matched none of it — internal jargon ("Task completed in Swarm."), the worker name (internal routing, meaningless to a customer), and the entire technical resolution, up to 3,804 characters on real linked tasks. That last one is wrong **in kind**, not merely in length: resolutions are written for the *next worker* and become learnings, while the person who raised the ticket wanted to know their problem is fixed.

  The replacement greets the reporter by name — possible only because reporter is imported as of 2026.8.9.13 — states the outcome in one plain sentence from the title, and adds the *first paragraph* of the resolution capped at 400 characters with a visible ellipsis, because truncating silently would be its own small lie. It signs off and says plainly that it is automated, the same provenance principle behind the reserved `swarm` label. An unsynced or unlinked task simply gets no greeting rather than an error. Two assertions in the old test pinned the removed behaviour and now assert its absence with the reason. **Still unverified by a human who knows the voice**: the style was inferred from eight real tickets, and if the team prefers different wording this is one string to change.

### Changes

### Fixes

## [2026.8.9.15] - 2026-08-09

### Features

### Changes

### Fixes

- **Swarm was notifying workers about its own Jira comments.** Found in an inbox during an idle nudge, not by a test: posting a blocker note made the comment sync see "new activity" and message the assigned worker about a comment Swarm had just written — twice, once for the block and once for the clear — and completion comments would have done the same. An echo like that trains workers to ignore the notification, which is exactly when a real stakeholder comment gets missed; it is the same shape as the `labels = "swarm"` echo the provenance decision was designed to prevent, arriving through a different door.

  `_latest_comment` now returns `""` when the newest comment carries a swarm marker, so no message is sent. The comment is **still mirrored** into the description — only the notification is suppressed, because the mirror is what a human reads and Swarm's own note belongs in the thread. The completion comment carried no marker at all, unlike the blocker note and the worklog, so it now appends one; swept rather than pinned per call site, with a test that walks the three comment writers and fails on any that writes to Jira without marking the text as Swarm's. Only the *newest* comment decides, so an older swarm note does not mute everything after it. **Known limitation, stated rather than hidden:** a human comment landing in the same cycle as a swarm note is not notified — the tail still mirrors it, so it is visible on the task, but it raises no message.

## [2026.8.9.14] - 2026-08-09

### Features

### Changes

### Fixes

- **The scheduled Jira refresh never fetched reporter or due date.** 2026.8.9.13 added both to `_JIRA_ISSUE_FIELDS` and rendered them in the synced block; assigning a real task and waiting five minutes, they never appeared. **The batched sweep asks for its own field list** — `fetch_synced_fields`, added by the batching work in 2026.8.9.10, requested `comment,attachment`, written before reporter and duedate existed. So the two fields reached the *single-task* refresh, which uses the full default set, and never reached the path that actually runs every five minutes. Every unit test passed because they drive `refresh_synced_content` directly with a mocked `get_issue`: two correct changes, each tested, that did not compose — and only running it showed that. One constant, `_SYNC_BLOCK_FIELDS`, is now used by both paths, held together by two tests (the batch must request exactly it, and every field the renderer reads must be in it) so neither can quietly fall behind the other again.

## [2026.8.9.13] - 2026-08-09

### Features

- **Reporter and due date are imported for Jira tickets, and nothing else is.** Measured before building, across 50 real tickets on both projects: reporter 100%/100%, duedate 36%/12%, and components, parent, environment, fixVersions and issuelinks **zero on both** — as were descriptions mentioning "acceptance". The never-populated fields are deliberately not requested; importing a field nobody fills is the sprint mistake again. Both fields live in the **regenerated block**, not on `SwarmTask`: a due date moved in Jira updates on the next sync, whereas a stored copy would silently go stale, and neither field is Swarm's to own. No schema change, and a test moves the date and checks the old one is gone. The due date deliberately does **not** touch task priority — it is a fact the worker sees; letting it reorder the board is a separate decision with its own blast radius.

- **Acceptance criteria are synthesised when a linked ticket is assigned — and the real finding is that nothing was doing it at all.** `apply_synthesized_criteria` runs for `swarm_create_task` and `create_task_smart`; the Jira import path never called it. So every imported ticket arrived with no acceptance criteria, and the verifier default-passes a task with none — **every piece of Jira work was unverifiable by construction**. That, not the absence of a parser, was the gap. Criteria are synthesised rather than parsed from the ticket deliberately: zero of fifty tickets mention acceptance criteria, so a parser would import nothing, and criteria parsed at import go stale the moment someone edits the ticket because the refresh is additive and does not touch them. It runs **on assign** rather than on import, because an imported ticket may never be worked and this costs a model call; assignment is also the last point before dispatch, so the criteria reach the worker in its task message, which is why it is awaited rather than backgrounded. Scoped to linked tasks — locally created ones already get criteria at creation.

### Changes

### Fixes

## [2026.8.9.12] - 2026-08-09

### Features

### Changes

### Fixes

- **The 2026.8.8.16 proposal-expiry fix was applied to the wrong store, so it never ran in production.** That release taught `ProposalStore` to stop expiring a proposal merely because its task is not ASSIGNABLE — a promotion, completion or park always references a task its worker already owns, so checking it against the assignable set expired it within seconds. **The daemon uses `SqliteProposalStore`**, which was never updated, so once the caller began passing the new argument every sweep raised `TypeError: SqliteProposalStore.expire_stale() got an unexpected keyword argument 'assignable_task_ids'` — and expired nothing at all. Every test for that fix built the in-memory store, so all of them passed against code the daemon never runs.

  **It also invalidates an earlier claim**: the expiry fix was reported as "verified live" because a promotion proposal survived the sweep. It survived because the sweep was *crashing*, not because the logic worked — the observation was real, the explanation of it was wrong. `tests/test_proposal_store_parity.py` now runs the same assertions against both stores, parametrised rather than duplicated (a copied test file is exactly what did not get written last time), and pins that the two signatures match. Fixing it surfaced a second defect the in-memory store never had: with an *empty* assignable set the SQL guard skipped the check entirely, leaving stale assignment proposals lingering. Found by the parity test immediately. Both defects were caught by reading the log after a real sync cycle — neither was visible to 6,242 passing tests.

- **`assign_to_me` had never worked on this install.** The token manager resolves the account once at connect time via `/myself`, which 401s with "scope does not match" on any install authorised before `read:jira-user` was requested, so the account id stayed empty forever and `assign_to_me` refused every call. `_my_account_id` already solves exactly this — `/myself`, then deriving the account from `assignee = currentUser()`, which needs only `read:jira-work` — so `assign_to_me` reuses it rather than growing a second fallback that could rot separately.

## [2026.8.9.11] - 2026-08-09

### Features

### Changes

- **A per-developer Jira setup guide, with every mechanical claim pinned by a test.** Each developer is about to point a write-capable integration at a shared tracker, and the safe *order* for doing that existed only in commit messages. `docs/jira-setup.md` walks it: register the OAuth app, **turn read-only on first**, add projects, discover and confirm each workflow, preview the sync plan, then turn read-only off — step 2 sits deliberately before steps 3-5 so a mistake lands on the developer's own screen rather than in the team's ticket queue. It also names the things that surprise people: text above the `--- Jira sync ---` marker is preserved and everything below regenerated, the `swarm` label is reserved provenance, reassigning a ticket in Jira moves the work off your board, Jira owns status, and Swarm never creates a ticket without operator approval. For team rollout it records that per-cycle API cost is bounded rather than proportional to board size (~14 calls/cycle after 2026.8.9.10) and that bulk-filed work should be filed *unassigned* and assigned one at a time — an unassigned ticket is never imported, which is the ordering mechanism, not Jira issue links, which Swarm does not read.

  Every mechanical claim is tested, because documentation drifts silently and is believed anyway: `config.html` told operators for months that tokens lived in `~/.swarm/jira_tokens.json` after they had moved into the database. The tests check the scopes it tells you to grant are the ones actually requested, that the issue types and sync interval match the config, that the sync marker and reserved label it quotes are the real constants, that the verbs it names exist, and that read-only still blocks writes at the client. Prose and judgement are deliberately not tested. All fourteen claims were fact-checked against the code before committing; one was wrong (the marker had moved to `swarm/tasks/task.py`) and was corrected.

### Fixes

## [2026.8.9.10] - 2026-08-09

### Features

### Changes

- **The Jira sync loop no longer scales with the size of the board (#1350).** Measured after shipping, which is the uncomfortable part: the spec flagged API budget as an open question, then two passes were added that are each O(open linked tasks) with a full API call per task per cycle, and nothing was measured until someone asked whether this was production-ready. At 5 open linked tasks it was 23 calls/cycle, at 20 it was 53, at 55 it was 123 — now a flat **14/cycle** at every size. At ten devs on a 55-ticket board that is ~14,760 calls/hour before versus ~1,680 after, with ~55 tickets about to be filed.

  Two fixes. `refresh_linked_tasks` issues **one search** for the whole set (`key IN (…)`, `fields=comment,attachment`) instead of one `get_issue` per task, chunked at 50 because a single JQL cannot carry unbounded keys; keys are *validated*, not escaped, the same guard the ownership check uses, and a task missing from the response is simply not refreshed this cycle — a no-op rather than a truncation. `reconcile_blockers` no longer reads every open ticket's comments to discover nothing, checking a task only when it *is* blocked or is known to carry a note we posted. That costs **one full pass per daemon start**: narrowing purely to "blocked now" would strand a note written by a previous daemon instance, because the known-notes set lives in memory — the honest cost of keeping that state in memory rather than adding a column, bounded to once per restart.

  The tests assert the **shape of the cost**, not just that the code works: twenty tasks must cost one search, 120 keys must be three searches, and an unblocked task with no note must cost nothing in steady state — the failure is invisible on a small board and only bites once Jira is enabled for a team. Three older fixtures built a `JiraService` by hand and mocked the per-task call; they were updated to the batched shape rather than the production code being bent to keep them passing.

### Fixes

## [2026.8.9.9] - 2026-08-09

### Features

### Changes

### Fixes

- **A worklog refused because the project was unconfirmed is now retried instead of lost forever.** `log_work` correctly refuses to write to a ticket whose project the operator has not confirmed — a worklog is a write to a shared tracker — but nothing ever tried again, so every task closed before confirmation forfeited its time permanently. `backfill_worklogs` re-offers recently-closed linked tasks each cycle.

  **Idempotent by reuse, not by new bookkeeping**: `log_work` already reads the ticket's existing worklogs and skips its own marker, so re-offering an already-billed task writes nothing. There is no "already backfilled" flag, which is why it survives a restart or a rebuilt database. Bounded twice — a seven-day window and ten candidates per cycle — because each check costs one worklog read; unbounded, a board with hundreds of closed linked tasks would re-read all of them every five minutes forever. A task whose ACTIVE time cannot be substantiated is skipped rather than filled in with a guess.

- **Correction: the dashboard force-complete path was never missing a worklog.** The earlier record claimed only `fire_completion` was wired; in fact `queen_force_complete_task` and the dashboard route both call `d.complete_task(...)`, with `force` a parameter of that same function, so the side-effects block runs either way. There was nothing to fix — the guarantee is now asserted by test instead, so a future path that bypasses `complete_task` is caught. The duration test was also repointed to `_worked_seconds`, where the history lookup moved when backfill started sharing it; following the call was preferred over loosening the assertion, which would have quietly stopped testing anything.

## [2026.8.9.8] - 2026-08-09

### Features

### Changes

### Fixes

- **Every write to the shared Jira tracker is now visible at the log level operators actually run at.** The worklog success line sat at INFO while the default is WARNING, so nothing in `swarm.log` recorded that a worklog had been written — verifying #1339 meant reading Jira instead, exactly the position an operator would be in.

  Fixed as a class rather than as the one line reported: five write-success logs were invisible — transition, completion comment, assignee, worklog, created issue. Raising only the worklog would have left the log incoherent, since a transition changes someone's ticket and is more consequential than a time entry. The rule is **changing someone else's data**, not volume, so sprint-field discovery, the import count and the already-terminal path stay at INFO — that last one records agreement *without* writing. Pinned by a test that walks the AST over the known write methods and fails on any `_log.info` inside them, with a positive control asserting the name set matches real functions in the module.

## [2026.8.9.7] - 2026-08-09

### Features

- **Sprint membership now raises a task's priority — off by default, and the design settled by measurement (#1341).** Probing the operator's real site first changed the shape of the answer: the sprint field exists as `customfield_10020`, but WWD has zero issues carrying one and IS rejects sprint JQL outright. Neither configured project uses sprints, so the acceptance criterion "verified against a real sprint-using project" is not satisfiable here — which is why this ships **off by default** rather than changing import results for teams that cannot be tested against.

  **Restrict or prioritise is settled as prioritise.** Filtering imports to a sprint would hide genuinely assigned work, a surprising way to lose a ticket; sprint membership decides *order*, never membership, and a test asserts in-sprint, out-of-sprint and closed-sprint issues all still import with no sprint clause in the JQL. The boost raises one step and never reaches URGENT — urgent means production is affected, in-sprint means scheduled, and collapsing them would make the signal that wakes people up indistinguishable from planned work.

  **The field is discovered by name, never hardcoded**, because the id differs per site — the same mistake the hardcoded `Done` transition made, right on one project and refused by eleven tickets on another. It is requested only when the feature is on and the site actually has one, since asking Jira for a nonexistent field id rejects the whole search and would take imports down for a feature nobody enabled. Only an ACTIVE sprint counts, in both the modern dict shape and the legacy `state=ACTIVE` string shape, because work that rolls over sits in a closed sprint and the current one at once and history must not raise priority. Config lands across all four layers plus a UI toggle, with the round trip asserted.

### Changes

### Fixes

## [2026.8.9.6] - 2026-08-09

### Features

- **A blocked task now says so on its linked Jira ticket (#1340).** When a worker blocked, the ticket said nothing — a PM looking at the board saw idle work with no explanation and the reason lived only inside Swarm. This is the item that most directly makes Swarm legible to people who never open it.

  **A comment, not an issue link — decided, not defaulted.** A Jira `blocks` link can only express a dependency between two tickets, and most Swarm blockers are on things with no ticket at all: another Swarm task, an operator decision, a deploy. The comment covers every case; the link covers a minority and needs per-site link-type discovery, so issue links stay a follow-up.

  **Reconciled, not hooked.** There are four block/unblock call sites (two MCP verbs, the coordinator, the board), and hooking each means the fifth one added later silently does not report; comparing state each cycle also self-heals a note that failed to post, which fire-and-forget cannot. One comment is rewritten in place rather than appended, because on a five-minute loop posting per block and unblock turns a ticket into a changelog nobody reads. Three guards, each with a control: an unchanged blocker writes nothing; clearing rewrites the note rather than leaving a ticket asserting a block after work resumed; and an unblocked task with no prior note says nothing at all.

  Not verified live — it needs a daemon reload to run on the real loop — though the underlying `add_comment`/`update_comment` path is the one verified live on WWD-6717. The two new client writes were picked up automatically by the read-only sweep from 2026.8.9.5, which is what that sweep exists for.

### Changes

### Fixes

## [2026.8.9.5] - 2026-08-09

### Features

- **Jira read-only mode — try the integration without writing to anyone's tickets (#1342).** With Jira being enabled for every dev, a newcomer's first misconfiguration would otherwise land in the team's ticket queue rather than on their own screen; verifying v2 required creating seven throwaway tickets in a real shared project because there was no alternative. With `read_only` on, imports, workflow discovery and reconciliation run exactly as normal, and every write is refused and logged at WARNING saying what it would have done — silent refusal would make the mode indistinguishable from broken.

  **Enforced at the client**, the only place every Jira write passes through (transition, comment, worklog, assignee, create). Hiding buttons in the UI would not help: the sync loop writes on a timer with nobody watching, which is exactly how 14 real WWD tickets were transitioned on 2026-08-07 by a settings toggle. The durable protection is a **sweep, not five assertions** — a test walks the AST and fails if any method issuing `session.post/put/delete` does not consult the guard. It found `create_issue` unguarded on the first run, because that one returns a dict rather than a bool and did not match the pattern of the others. `create_issue` returns `{}` in read-only mode rather than a fabricated key, so existing callers refuse instead of linking a task to a ticket that does not exist, and no caller had to learn about the mode.

  **Verified live against real Jira, no daemon reload needed:** with `read_only` on, a full cycle read 3 issues and discovered 7 status sets while all five writes refused, and WWD-6718 was checked afterwards and is untouched — same status, same single worklog, no comment. The config field is added across all four layers plus the UI toggle with a round-trip test, because v2 shipped three of the four once already and the UI then reported a setting that vanished on restart.

### Changes

### Fixes

## [2026.8.9.4] - 2026-08-09

### Features

### Changes

- **Jira's worklog granularity is now measured and recorded rather than guessed at.** Verifying #1339 against WWD-6718 produced a number the code could not explain: a task with a measured 163s ACTIVE span read back from Jira as 120s. Probing rather than assuming — posting 3661s and reading back 3660 — established that Jira **truncates `timeSpentSeconds` down to whole minutes**. The prior comment claimed only that "Jira rounds sub-minute worklogs to zero", a guess that happened to be adjacent to the truth; it now states the measurement with the numbers so nobody re-derives it.

  **Behaviour is unchanged and that is the point.** Swarm sends the true figure and lets Jira truncate rather than rounding up to the nearest minute itself — rounding 163s up to 180s would bill 17 seconds nobody worked. Truncation under-reports, the safe direction for a timesheet, and the 60s floor exists precisely because anything shorter truncates to zero and vanishes. Pinned by a test asserting the sent value is neither adjusted nor rounded up. The probe worklog was deleted from WWD-6718 afterwards.

### Fixes

## [2026.8.9.3] - 2026-08-09

### Features

- **Closing a linked task now posts a worklog to its Jira ticket (#1339).** Swarm already knows how long a task was ACTIVE and devs are measured on time they hate logging by hand. This writes to a shared tracker and it is somebody's timesheet, so the refusals matter more than the happy path.

  **Duration is reconstructed from history, not from `completed_at - started_at`.** `SwarmTask.activate` resets `started_at` every time, so that subtraction reports only the final stretch — a task worked three hours, parked, then resumed for five minutes would bill five minutes. `swarm/tasks/worklog.py` walks the task's events and sums the STARTED → (COMPLETED|FAILED|UNASSIGNED|BLOCKED) intervals; time parked on a blocker is not work and is not counted. It returns `None`, **not `0.0`**, when it cannot tell, and the caller then logs nothing — "no record of work" and "worked for no time" are different claims and only the second is safe to write. An unclosed interval is ignored rather than assumed to run to now.

  **Idempotence by comparison, not by memory.** Each entry carries a marker keyed on (task number, completion time); before writing, Swarm reads the ticket's existing worklogs and skips if its own marker is there, so it survives a daemon restart or a rebuilt database in a way a local "already sent" flag would not. Keying on the completion rather than the task alone is deliberate: a task reopened and genuinely worked again *should* get a second entry. If the existing worklogs cannot be read, nothing is written — "I cannot tell whether I already billed this" must not resolve to "bill it again". Unconfirmed projects are refused with a WARNING naming what would fix it, the same gate as the export sweep.

### Changes

### Fixes

## [2026.8.9.2] - 2026-08-09

### Features

### Changes

### Fixes

- **Worker findings appended to a task description landed below the Jira sync marker and were deleted five minutes later.** Found on real data against WWD-6717: findings appended to task #1334 were gone after one sync cycle, falsifying the previous release's claim that the refresh "has no path by which it can remove any" information.

  **Two features, each correct alone.** #1289 added `append_description` precisely so that adding to a description cannot lose it; the Jira sync owns everything after the `--- Jira sync ---` marker and rebuilds it. But append put the text at the end of the *string*, and once a task has synced, the end of the string is inside the block the sync regenerates — together they deleted exactly what `append_description` exists to protect. The addition now goes before the marker and the generated tail is re-attached unchanged, and the marker constant moved to `swarm/tasks/task.py` because it is a contract between two unrelated writers rather than a Jira detail.

  **Why the tests said this was impossible**: `test_worker_authored_description_text_survives` passed and proved nothing — its fixture had no existing sync tail, so the appended text landed above the marker. The real sequence is import → sync → worker appends → sync again, and only by the second sync does the description end with generated content. Demonstrated rather than asserted: with the naive append restored the two new tests fail while the original still passes. Swept for other appenders — `_edit.py` is the only one, so this is the whole blast radius.

## [2026.8.9] - 2026-08-09

### Features

- **Linked tasks keep receiving Jira comments after import.** `import_issues` dedupes on `jira_key` and skips tasks that already exist, so comments and attachments were mirrored exactly once — at creation — and never again. On a service desk the comment thread *is* the requirement: a stakeholder writes "actually the customer needs X" and the worker never saw it, because nothing ever looked again.

  **Why the obvious fix is destructive**, and most of the new test file exists to hold this line: `refresh_task` already existed for the manual button and re-derives the description from the Jira body while *replacing* `task.attachments`. On a timer that would silently delete, every five minutes, everything a worker wrote into the description and every attachment Swarm added itself. So `refresh_synced_content` is additive by construction — it rebuilds only the region below the `--- Jira sync ---` marker and merges attachments, and a failed fetch is a no-op rather than a rebuild from an empty payload. Change is detected by comparing the **derived tail**, not by counting comments: the tail is size-capped so counting undercounts, and an edited comment changes no count at all.

  The worker is then **messaged**, because mirroring a comment into a description nobody re-reads is half an answer. A message lands in their inbox rather than cutting across whatever they are mid-way through saying, and carries the latest comment itself — "Larissa: do X instead" is the thing they needed, not "the ticket was updated". Only open tasks this swarm still owns are refreshed.

### Changes

### Fixes

- **Three broken callers of `MessageStore.send`, found by copying the pattern and watching it do nothing.** `send()` takes `(sender, recipient, msg_type, content)`; all three passed a `Message` object as the first positional with the other required arguments missing, so every call raised `TypeError` straight into a surrounding `except`. The daemon's Queen CLAUDE.md-drift notification logged at DEBUG and was invisible at the operator's default level — the Queen has never once received it. The `task_coordinator` verifier-drone warning logs at WARNING but has zero occurrences in the log: that path had never fired, so it was latent. Pinned with a repo-wide sweep rather than a test per call site, because one of these was wrong for months and a signature test would not catch it — the bad call type-checks fine at import.

## [2026.8.8.17] - 2026-08-08

### Features

### Changes

### Fixes

- **Ownership is now established before anything is written to a ticket.** Observed live on WWD-6715: the export sweep ran at 23:43:21 and wrote to the ticket, and ownership reconciliation discovered at 23:43:23 that the ticket was no longer ours — two seconds apart, in the wrong sequence. Swarm wrote to a ticket and then found out it had been taken off it. Ownership now runs first; the defect is in neither sweep but in their order, so the order is what the test asserts.

- **A released task is no longer export-reconciled forever.** Releasing changes the task's status, which by itself creates an export divergence — so the sweep would push Swarm's status onto a ticket the swarm no longer owns, every cycle, and could genuinely transition someone else's work. UNASSIGNED + on hold + no owner is now excluded from the export sweep. That shape is also an ordinary parked backlog item, which is equally not ours to be reporting into Jira.

  Live verification of the ownership feature itself on WWD-6715: the ticket was unassigned in Jira (chosen over handing it to a colleague — same code path, nobody's queue disturbed), after which #1330 went UNASSIGNED with owner `None`, tags `["hold"]` and its `jira_key` retained, while Jira itself was untouched — still To Do, still labelled swarm, nothing written. The log line also confirms the status is captured *before* the release; it read "It was unassigned" until 2026.8.8.15 fixed it reporting its own effect.

## [2026.8.8.16] - 2026-08-08

### Features

### Changes

### Fixes

- **A proposal about a task you already own was expired seconds after it was raised.** `expire_stale` validated every pending proposal's task against `available_tasks` — UNASSIGNED and not on hold. A `jira_promotion` proposal always references a task assigned to the worker that requested it, so it could never be in that set and was expired on the very next sweep. Nothing failed and nothing logged; the request simply vanished from the operator's surface, the worst shape for an approval queue, which then looks like oversight while quietly dropping what it was asked to hold. The first live test survived only because approval happened inside the sweep window.

  **Two different questions were conflated.** "Does this task still exist and is it open?" is the right test for a proposal about a task the worker already owns; "could the auto-assigner still take it?" is only meaningful for an *assignment* proposal, where an owned task genuinely makes it moot. Fixed as a rule rather than a special case, because it was never only about Jira — COMPLETION and PARK proposals reference ACTIVE tasks and were exposed to the same premature expiry. `assignable_task_ids` defaults to `valid_task_ids`, so existing callers keep their behaviour. One control passed with the fix removed for the sixth time in this work, always the same shape: three tests exercised the store while the defect was in what the manager passed it, so a test now drives `ProposalManager.expire_stale` against a real board.

## [2026.8.8.15] - 2026-08-08

### Features

- **A ticket reassigned in Jira stops being this swarm's work.** Routing is `assignee = currentUser()`, the whole reason Jira can be enabled for every dev without them colliding — but nothing re-checked it after import, so handing a ticket over left *both* swarms holding the task: the new owner's imports it, the old owner's keeps working it, and they race to transition the same ticket.

  **The detection is the design, and the obvious implementation is dangerous.** "It fell out of the import query" is not evidence of reassignment — a ticket disappears from that query when it is reassigned, closed, moved, deleted, when permissions change, and when the call simply fails and returns fewer rows. Inferring from absence would release every linked task the first time Jira errored. `find_reassigned` instead asks Jira what the assignee actually *is*, in one batched JQL for all open linked tasks, and acts only on a definite mismatch; a key missing from the response is reported as nothing at all. Three guards each carry a test and a control: an unresolvable account releases nothing (reachable — it is what the `read:jira-user` scope gap produced), a failed query releases nothing, and keys are validated rather than escaped so a malformed `jira_key` never reaches the JQL.

  The task is **released and put on hold**, not deleted or completed: the work is not done and the link is still true, so it stays visible and traceable while belonging to nobody. HOLD is not decoration — a bare release returns it to UNASSIGNED where the auto-assign drone hands it to another worker in *this* swarm, the same wrong answer with a different name. Existing tags survive, since `update(tags=...)` replaces the list. Nothing is written to Jira: ownership moved there already, and Swarm's job is to stop working the ticket, not to argue with the person who took it.

### Changes

### Fixes

- **The release log line reported its own effect.** It read `task.status.value` *after* the release, so every message said "It was unassigned" instead of naming the status the operator needed to know.

## [2026.8.8.14] - 2026-08-08

### Features

### Changes

### Fixes

- **A comparison is not a write, so the confirmation gate no longer blocks it.** MTR-11806 is done in Swarm and already `Done` in Jira, but sits in a project that is neither in the sync scope nor confirmed — so the sweep dropped it at the confirmation gate *before* the agreement check and warned about it every five minutes, permanently, over a divergence that does not exist. The gate exists to stop an unattended sweep bulk-writing to a shared tracker; recording that Jira already agrees writes nothing, so gating a comparison behind write-confirmation buys no safety and costs real noise. Unconfirmed tasks now get the read-only agreement check while the gate itself is unchanged — a ticket that genuinely needs a transition in an unconfirmed project is still refused, with a test for exactly that.

  Verified live first: after the 2026.8.8.13 reload the 20:45 sweep cleared all 10 IS tickets, and a read-back confirms all 10 are **still `Resolved`** in Jira. That second half is what mattered — their only available transition was `Waiting for support`, which reopens, so a fix that wrote instead of compared would have reopened ten resolved service-desk tickets.

- **The saved-mappings panel hid the one project that mattered.** It listed projects from `projects` and from stored maps, so MTR — the project the board was already entangled with — was the only one the operator could not see. The endpoint now derives rows from linked tasks too and reports a per-project linked count. One negative control passed when it should have failed, for the third time in this work and always the same shape: the test called `_record_existing_agreement` directly, so deleting its call site in the sweep left it green. Replaced with one that drives `reconcile_exports` itself — the wiring is what breaks, so the wiring is what needs the test.

## [2026.8.8.13] - 2026-08-08

### Features

### Changes

### Fixes

- **A ticket already finished in Jira is agreement, not a failed export**, closing the last open Jira v2 item — and measuring first dissolved the decision it was waiting on. The spec filed this as an operator decision needing 11 judgement calls; checking the tickets against live Jira showed all 10 (not 11 — the repeated figure was wrong) were already `Resolved`, statusCategory `done`, offering only a `Waiting for support` transition, which reopens. Nothing to remap, nothing to unlink: Jira was right and Swarm had simply never recorded it. The cost was a reconciler retrying an impossible transition every sync interval forever, two WARNING lines each, with the refused-set held in memory so every restart tried the whole set again.

  `export_status` now asks whether the ticket is already terminal **before** reporting failure, and records agreement without writing. Name equality was the wrong test — this project calls finished `Resolved`, the confirmed map targets `Done`, and both mean the work is over; `statusCategory` is universal across every Jira workflow, so it answers "is this finished?" with no per-project discovery. Each asymmetry has a test: only for terminal Swarm statuses (a done-category ticket while Swarm says ACTIVE is real divergence); only when a transition could not be found (a movable ticket should be moved, and the happy path pays no extra API call); an unreadable ticket does *not* claim agreement; and it never writes, because reopening a resolved ticket to close it again would be destructive on a shared service desk.

- **`input-field` was used on a real `<input>` in the approval-rule modal and defined in no stylesheet** — the other three inputs in that file use `modal-input`, which is defined. Fixed. Deliberately **not** fixed and recorded so the next pass does not "fix" them: `muted` is a phantom of the scan's tokenizer (every real use is `text-muted`) and `local-time` is a JS hook for timestamp formatting that carries `text-muted text-xs` for its appearance. Both had been reported as likely typos; that was speculation and it was wrong. `config.html` also claimed Jira tokens live in `~/.swarm/jira_tokens.json`; they are in the secrets table of `swarm.db`, now corrected.

## [2026.8.8.12] - 2026-08-08

### Features

### Changes

### Fixes

- **Task ownership now lands before the reply, closing a create-then-act race that was never Jira-specific.** `swarm_create_task` returns as soon as the row exists, while the assignment rode a background coroutine that first awaited Outcomes-criteria synthesis — an LLM call taking seconds. Any verb reading ownership straight after creating a task saw it UNASSIGNED for that whole window; it surfaced as `swarm_request_jira_ticket` reporting "#1326 is not assigned to you" about a task just routed to the caller.

  The synthesize-then-dispatch order is deliberate and **stays** that way when there is a dispatch, because the criteria have to be in the message the target worker receives. With `start=False` no message is ever sent, so that path now assigns first and synthesizes after, and ownership lands on the next loop tick instead of behind the model. The refusal text now separates "never yours" from "not yours *yet*", rather than describing a sub-second window as a permanent condition and sending the caller hunting a routing bug that is not there.

- **The Jira approval modal rendered unstyled**, written with `queen-section`, `queen-section-label`, `queen-section-body` and `queen-actions` — four classes that exist in no stylesheet. Every section came out as a flat run of lines while the escalation and completion cards beside it looked right, and nothing failed. It now uses the existing vocabulary (`queen-text-block`, `modal-footer`) rather than inventing more.

  `tests/test_mobile_ui_1291.py` already existed for this defect class — a class used but never defined, which fails silently and looks like a layout bug — but it pinned only `.text-center`, the one instance #1291 happened to hit, which is why the class kept recurring. It now sweeps the `queen-*` namespace. Scoped to `queen-*` on purpose: a repo-wide sweep flags JS hook classes such as `view-proposal-btn` and `msg-select-cb` that exist to be queried rather than styled, and a test that cries wolf gets suppressed instead of read — measured before choosing, at 11 undefined classes repo-wide, all hooks except three worth a separate look.

## [2026.8.8.11] - 2026-08-08

### Features

### Changes

### Fixes

- **The new MCP verb 500'd in production while 6092 tests passed.** `handle_tool_call` invokes handlers *without* awaiting them — every existing handler is `def`, this one was `async def`. The dispatcher stored a coroutine that was never run and the failure happened on the next line, outside the try/except: a bare 500 with no traceback in the log. The unit tests passed because they called the handler directly and awaited it; nothing went through the dispatcher, the seam where the contract lives. The handler is now synchronous (a promotion request has nothing to await; the Jira call happens at approval), with a test asserting every registered handler is sync, because the next person to add a verb will reach for async too.

- **Every promoted ticket was created unassigned.** `/rest/api/3/myself` returned 401 "Unauthorized; scope does not match" while create and search succeeded on the same token — the OAuth app requested only `read:jira-work` and `write:jira-work`. An unassigned ticket does not route back to the swarm that raised it, which is the entire point of assignee routing, so the feature created tickets that looked right and silently did not work. `read:jira-user` is now requested, but that alone was insufficient: existing tokens keep the scopes they were granted, so every dev who authorized earlier would have kept producing unassigned tickets until they happened to reconnect. `_my_account_id` therefore falls back to deriving the accountId from `assignee = currentUser()` — the same query imports already use, needing only `read:jira-work` — and it resolves against the operator's real, un-reconnected token.

  Verified in real Jira rather than by test: WWD-6712 (before) carried labels `['swarm']` and was unassigned; WWD-6713 (after) carried the same label and was assigned to the operator. Both were created in WWD resolved from `projects` rather than the legacy field, and both transitioned back to Done via the real export path, exercising the confirmed WWD status map.

- **The account-id fallback would have silently never worked.** The default search field set does not include `assignee`, so it would have received issues without it and read the absence as "no assigned issues" — indistinguishable from the failure it exists to fix. `search_issues` now takes an explicit `fields` override and the caller asks for what it needs.

## [2026.8.8.10] - 2026-08-08

### Features

- **Workers can now request that a Swarm task be raised as a Jira ticket, via the new `swarm_request_jira_ticket` MCP verb.** They cannot create one: Jira is a shared tracker, an agent-raised ticket is visible to a whole team and cannot be un-seen, so the operator approves. It rides the **existing proposals surface** rather than a second inbox — that surface already has an operator UI, notifications and an autonomous-window concept, and a second queue is a thing that eventually goes unwatched, which is worse than none because it looks like oversight while providing none.

  **The property this turns on: every refusal is re-checked at approval time**, not trusted from request time. A proposal waits for a human and the world moves while it waits — the task can be finished, archived, or linked by someone else in between — and validating only at request time makes approval a rubber stamp on a fact that has stopped being true, the same class as the empty-`jira_exported_status` default that transitioned 14 real tickets. Refused at approval: already linked, done or failed ("never for closed work", one rule covering both the ~1235 historical closed tasks and the short-lived ones), and Jira disconnected. A create returning no issue key raises rather than reporting a promotion that did not happen.

- **`swarm` is now reserved provenance, auto-applied to tickets Swarm created and to nothing else.** It means exactly one thing: an agent raised this. Swarm does not label tickets it merely transitions — that would write to other people's tickets on every sync. The trap this avoids: the old import filter was `labels = "swarm"`, so had created tickets carried that label while it still drove routing, Swarm would re-import its own output as a new task. Separating "came from Swarm" (provenance) from "route to Swarm" (assignee) makes the echo loop impossible rather than merely deduped against.

### Changes

- **The promotion proposal gets its own badge and modal.** Without them it fell through to the assignment renderer and showed an "ASSIGN" badge — the operator would have believed they were approving a task assignment while authorising a team-visible ticket. The modal states the consequence: what is created, where, assigned to whom, labelled how, and that nothing happens unless they approve. `get_jira` is injected as a **callable, not a reference**, because the daemon builds the proposal manager before it builds `self.jira` and rebuilds `self.jira` on every config reload — a captured reference would be `None` at construction and would pin a stale service across a reconnect.

### Fixes

- **The existing Jira create path used the legacy single-project field and set no assignee.** `self._config.project` is empty on a v2 config that only sets `projects`, which Jira rejects, and on a multi-project config it silently pinned creation to whichever project was in the old field; creation now resolves from `projects` and refuses rather than posting an empty key. A created ticket is also assigned to the dev whose swarm raised it, so the outbound rule and the assignee-routing rule agree and it round-trips home — an unresolvable account is deliberately *not* fatal, because an unassigned ticket that exists is recoverable while a promotion lost to a failed identity lookup is just gone.

## [2026.8.8.9] - 2026-08-08

### Features

### Changes

### Fixes

- **The global Jira `status_map` fallback is removed — it was silently mapping every unconfigured project.** The per-project maps landed but the global map stayed, both stored and consulted (`status_map_for()` returned `project_status_maps[key] or status_map`); removing its textarea made it invisible, not harmless. What made it worse than a leftover field is that `status_map` had a **non-empty default**, so the fallback never returned empty and every project without a confirmed map silently received the hardcoded `done -> "Done"`, on every install including fresh ones that had never configured Jira. The docstring justified the fallback as v1 upgrade safety, but the default defeated that reasoning: "genuine v1 config" and "nobody ever touched this" were indistinguishable.

  The IS refusal was the **lucky** failure mode — those 11 tickets failed loudly because that workflow has no Done transition. Where an inherited status name *does* exist in the target project, and most workflows have a "Done", the export succeeds and moves someone's ticket to a state nobody chose while reporting success. `status_map_for()` is now strict: an unmapped project returns `{}` and the export is refused, applying the module's own rule one level up — an absent mapping means "we do not know" and can be refused honestly, while a wrong mapping transitions a real ticket and looks like success. The field is deleted from model, loader, serializer and applier, and joins the REMOVED key set so an existing config reports it as gone rather than as a typo.

- **A refused export is no longer invisible.** Strictness alone would have made the silence worse: the refusal logged at DEBUG, meaning a task moved in Swarm while its ticket silently did not move in Jira, with nothing an operator running at default WARNING would ever see. It is now WARNING, naming the project, the ticket and what to do, suppressed to once per (project, status) because a discovered map legitimately omits states it could not justify. It also no longer calls `get_transitions` first, so a misconfiguration costs no API calls.

  **Upgrade consequence, stated rather than buried:** an install with projects that were never discovered will stop exporting those projects instead of using the inherited map. That is the intended change — verified against the operator's live config, where WWD and IS are both mapped and confirmed, so the blast radius there is zero.

## [2026.8.8.8] - 2026-08-08

### Features

### Changes

### Fixes

- **The saved-mappings panel updates without a page refresh** — operator-reported: "after I save discover the map list doesn't update. I have to refresh the page." The panel was a Jinja loop, so the table was built once at page load; confirming a workflow updated the config, saved it, and wrote a success line into a different element, with no path at all from "config changed" to "panel re-renders".

  Fixed the way the task board was fixed, not the way it was first patched: the panel is not handed a delta after confirm, it **re-reads the authority** via a new `GET /api/jira/mappings` — on load, on every visit to the Integrations tab, and after each confirm. A view that can re-derive its state recovers from updates nobody thought to send, which is the whole difference between the two approaches. The Jinja block is deleted rather than kept alongside, because two renderers for one panel drift and the server-rendered one wins at load. Verified against the operator's live config: the rendered table matches the stored WWD and IS maps exactly, both confirmed.

- **The panel now shows what a user actually needs to see.** One row per *configured* project, not per mapped project — a project listed in step 1 with no map imports issues and silently exports nothing, and was previously absent from the table entirely, indistinguishable from not being configured. Unmapped states are named in amber, since `export_status` refuses a transition whose target is missing and omitting it made "not mapped" look like "not shown". A stored map for a project no longer in sync scope is still listed and marked as such, because the map applies again the moment the key is re-added. The project field is datalist-backed by the configured keys so nobody retypes WWD, still free text because a project must be discoverable before it is added to step 1. And a failed read says so instead of sitting on "Loading" forever, which reads as "nothing configured" rather than "I could not tell you".

  One negative control initially passed when it should have failed: with the endpoint unregistered the panel renders "Could not read saved mappings", which is neither "Loading" nor empty, so the error branch satisfied the assertion — and the browser fixture's daemon has no `jira` attribute at all, so the working and broken paths produced identical-looking empty states. The test now wires a real `JiraSyncService` over a real `JiraConfig` and asserts the actual rows, and both controls bite.

## [2026.8.8.7] - 2026-08-08

### Features

- **Saved Jira mappings are rendered from stored config rather than from the last Discover click.** Before this, a reload erased the only view of what a project was confirmed as. Each row now shows its confirmation state and a Re-discover button, and an unconfirmed project says plainly that the sync will report and write nothing. The section is numbered 1-2-3 (projects, workflow mapping, cadence) with the routing model stated where the decision is actually made.

### Changes

- **Three dead Jira settings are removed from every layer, not just greyed out on the screen.** `import_filter` and `import_label` routed imports by label, which assignee routing replaced; `lookback_days` was read by no query in the codebase. Leaving them disabled was the worse state — a greyed-out input still reads as configuration, and a field the operator can see but cannot affect is the UI telling them something untrue. They are gone from the model, serializer, loader, applier and config page.

  Because an existing `swarm.yaml` still carries all three keys, they move to a REMOVED set that warns **by name**. Previously the single legacy-key message told the operator to fix `lookback_days` by switching to OAuth — remediation advice for an entirely different group of keys.

- **The raw `status_map` JSON textarea is gone.** Hand-typed JSON is how a map targeting "Done" got saved for a project whose workflow has no Done transition, which 11 real IS tickets then refused every sync interval. The dropdowns built from the project's discovered vocabulary are now the only way in.

### Fixes

- **Two element-id defects that would have aborted the *entire* config save — not just the Jira section — were caught by the id sweep during this change.** Removing the `status_map` textarea left a `getElementById(...).value` dereference on it in the save payload, and restructuring the block dropped the sync-interval input while the payload still read it. Same class as the earlier `cfg-jira-project` rename.

## [2026.8.8.6] - 2026-08-08

### Features

- **Saved Jira mappings are visible after the fact, rendered from stored config rather than from the last Discover click.** Previously the mapping existed on screen only in the moments after discovering; a reload left no way to see what a project was confirmed as, or whether it was confirmed at all. Projects with a discovered-but-unconfirmed map are shown as such, and when nothing is confirmed the page says plainly that the reconcile sweep will not converge.

### Changes

- **Jira settings that no longer make sense are addressed rather than left to confuse.** `lookback_days` is dead — plumbed through loader, applier and known-keys but read by no query since imports became assignee-routed — so it is disabled and labelled LEGACY. `status_map` is still live as a *fallback* for projects with no confirmed mapping, so it stays editable, but the hint now says so and names the seven real Swarm statuses; the operator's stored map contained `completed`/`in_progress`/`pending`, which are not statuses this system has and can never match.

- **`apply_jira` exceeded the C901 complexity gate once the new fields landed, so the handling was extracted to a helper** rather than the gate being raised.

### Fixes

- **The Jira v2 config never persisted — silent data loss across three fields.** `projects`, `project_status_maps` and `confirmed_projects` were added to the dataclass and wired into *neither* the serializer, *nor* the loader, *nor* the config applier. The projects box never saved and the operator watched their input revert with no error; confirming a project updated memory only, so the confirmation vanished on the next restart and the sweep would refuse a project already approved; and the serializer wrote keys the known-key validator did not recognise, so every load logged "unrecognized key 'projects' (typo?)" for a key the system itself writes — a warning that trains operators to ignore warnings.

  Adding a field to a config model is **four** changes, not one: model, serializer, loader, applier. A test now asserts the whole round trip rather than any single leg, and each leg carries its own control.

## [2026.8.8.5] - 2026-08-08

### Features

- **Status mapping is a dropdown of the project's real statuses, grouped by status category, replacing free text.** Typing a status the project does not have produces an export Jira refuses — the precise failure this phase exists to prevent — and there was no way to know which names were legal. Every Swarm status gets a row *including the ones discovery could not map*, since omitting them hides exactly the case that failed silently; a blank "not mapped" option makes "leave this deliberately unset" expressible, and unmatched rows are flagged amber. Labels are human ("Waiting for an owner" rather than `unassigned -> Reopened`), with the Swarm key kept in muted text beside them. The dropdown behaviour is asserted in a real browser against the operator's actual IS vocabulary, not in the template source.

### Changes

### Fixes

- **Workflow discovery stopped matching status hints inside other words.** Against a real IS project, discovery proposed backlog, assigned *and* unassigned → "Reopened": the hint `open` substring-matched inside "Re-**open**-ed", so new work was proposed to land in a reopened state, and the hint `to do` failed against a status literally named "ToDo" purely because of the space, so the better candidate never won. Matching is now three tiers — normalised exact (`ToDo` == `to do`), then **whole-word** containment (`waiting` still matches "Waiting for customer"; `open` no longer matches "Reopened"), then the single-candidate fallback. The real workflow now proposes backlog/assigned/unassigned → ToDo, active → In Progress, done → Done, failed → Canceled.

  A control exposed a weak test: restoring substring matching left all 17 tests green, because on that workflow normalisation finds "ToDo" before the whole-word tier is ever consulted — so those cases were evidence for normalisation only. A case with "Reopened" and no To Do status, where nothing short-circuits, makes the whole-word rule independently load-bearing.

## [2026.8.8.4] - 2026-08-08

### Features

- **The Jira setup screen — discover a project's workflow, preview, confirm (v2 phase 3 UI).** The plumbing shipped in 2026.8.8.3 but you had to curl it. Three controls now sit in the integrations tab: **Discover workflow** (proposes a mapping, each row editable), **Preview sync plan** (the dry run — what a sweep *would* change, nothing written), and **Confirm**, which posts back exactly what is on screen including edits. Re-deriving at confirm time would let what is stored differ from what was approved.

  It replaces a `status_map` textarea you typed JSON into — a map that was hardcoded and global, refused by 11 real tickets on 2026-08-07 whose workflow offered only "Waiting for support" while working fine for another project, and the artefact that silently rots when a Jira admin edits a workflow. Statuses the project offers no target for are shown in amber *before* anything is confirmed, rather than discovered later when an export is refused.

  The legacy routing fields are now **disabled and labelled LEGACY** rather than quietly ignored, and `project` becomes `projects`, comma-separated. Browser tests — not source scans — assert the block renders, is reachable via the tab, the legacy inputs really are disabled, and that clicking Discover with an empty project does not hit the Jira API.

### Changes

### Fixes

- **A field rename would have silently broken saving *every* config section, and now a test sweep prevents it.** Renaming the input to `cfg-jira-projects` left `saveSettings` calling `getElementById('cfg-jira-project').value` — `null.value`, a TypeError that aborts the save before any section is written, so renaming one Jira field would have broken saving workers, LLMs, approval rules, everything. `tests/test_config_element_ids.py` now sweeps every id the page *dereferences* against every id it *renders*, counting three sources of rendered ids: literal `id=`, Jinja macro arguments (which render an id with no literal `id=` anywhere), and `base.html`.

  The sweep is deliberately scoped to **unguarded** dereferences: `var el = getElementById(x); if (el)` is harmless, and `config.html` has one such dangling reference (`tool-buttons-list`, a drag-reorder list that no longer exists) which is dead code, not a defect — flagging it would have meant a false failure or deleting an unrelated line to make a test pass.

## [2026.8.8.3] - 2026-08-08

### Features

- **A dry-run plan and a per-project go-ahead before any bulk Jira write (v2 phase 3).** On 2026-08-07 a schema migration added `jira_exported_status` with an empty default, so all 25 linked tasks read as "never acknowledged", and the reconciler ran on its own five-minute schedule and transitioned **14 real WWD tickets** before anyone had looked. Nothing broke — those tickets were already done — but the blast radius of a settings toggle was other people's tickets.

  The gate draws one distinction, and it is the whole design: an **individual** export is the direct consequence of something a person or worker just did, and gating those would break a working integration on upgrade. The **reconcile sweep** is a bulk convergence that runs unattended on a timer and can move many tickets at once. Only the sweep is gated, per project, on the operator having confirmed that project's discovered workflow — and it reports how many it skipped and why rather than going quiet.

  `plan_exports()` is the dry run: task, ticket, the status Jira last acknowledged, the status it would move to, and whether the project is confirmed. It is a pure read — a "preview" that writes is worse than no preview, because it teaches the operator the button is safe. New endpoints `GET /api/jira/discover`, `GET /api/jira/plan` and `POST /api/jira/confirm` back the setup flow, with confirm the only writer, storing the mapping the operator actually approved rather than re-deriving it.

  **Upgrade safety:** a pre-v2 config object has no `is_confirmed`, and treating that as "unconfirmed" would silently stop a working integration, so such an install is not gated and a test pins it.

### Changes

### Fixes

## [2026.8.8.2] - 2026-08-08

### Features

- **Each Jira project's workflow is discovered rather than assumed (v2 phase 2).** The status map was hardcoded *and* global: it worked for WWD and was refused by all 11 IS tickets, whose service-desk workflow has no Done transition (`no transition to 'Done' found for IS-10278 (available: ['Waiting for support'])`). Nothing could see that until an export failed, and then it repeated every sync interval.

  `get_project_statuses` reads the project's real vocabulary and `jira_workflow.propose_status_map` proposes a mapping from it, asked at **project** level rather than per issue — `get_transitions` only reports transitions available from one issue's *current* state, which is how a map can look complete while being wrong for every ticket not in that state. `status_map_for(project)` then resolves by the ticket's own project key, since "what does done look like here" has no global answer; a project with no confirmed map falls back to the global one so a v1 install keeps working on upgrade.

  **The heuristic is category-first, name-second.** `statusCategory` (new / indeterminate / done) is universal across every Jira workflow while names are arbitrary — a status literally called "Done" sitting in the To Do category must not be chosen to mean finished, because trusting the name is how an export marks a ticket done by moving it backwards. Name hints only break ties *within* a category.

  **An unmappable status is omitted, not guessed.** Two equally plausible candidates with no hint match produce no mapping at all; an absent mapping means "we do not know" and can be refused honestly, whereas a wrong one transitions someone's ticket and reports success. `discover_workflow` returns the unmapped list explicitly rather than as an absence. Discovery writes and confirms nothing — Done / Resolved / Closed are rarely interchangeable, so confirmation is tracked separately from the map. The pure mapping logic lives in its own module needing no network, token or sample ticket.

### Changes

### Fixes

## [2026.8.8] - 2026-08-08

### Features

- **Jira imports route by assignee, not by label (v2 phase 1).** Jira is being enabled for every dev, each running their own Swarm, and the previous `labels = "swarm"` query does not survive that: every swarm imports the same tickets, creates a duplicate task per dev for one issue, and races to transition it. Routing is now `assignee = currentUser()` scoped to configured projects — one answer to "who owns this" in both systems, using semantics Jira already has, with no per-dev labelling ritual whose failure mode is a ticket nobody's swarm picks up, silently.

  `statusCategory` is the terminal test deliberately: it is a universal three-value field valid in any workflow, so "not finished" needs no per-project discovery. Discovery is still required for the *export* transition map — import and export need different mechanisms, and conflating them is what made the hardcoded map look adequate.

  Also: multiple projects via `projects` (the legacy single `project` migrated by `active_projects()` rather than rewriting the operator's config); configurable issue types defaulting to Story/Task/Bug/Sub-task and **not Epic**, which is a container a worker cannot finish and would sit open for months; and no configured project now imports **nothing** rather than everything, because the alternative puts a whole Jira site on one dev's board.

### Changes

- **Legacy `import_filter`/`import_label` no longer route, and say so once at WARNING.** Silently ignoring configuration the operator can still see in the UI turns a setting into a lie about what the system is doing; warning every sync would bury real signal, which is the noise problem that made the export reconciler retry 11 tickets forever. Two obsolete test classes were replaced rather than adapted, with the reasoning recorded in their docstrings — the behaviour they pinned is gone, not changed.

### Fixes

- **A client-side label filter would have made the new routing import nothing while appearing to run** — caught by a failing old test that the JQL change alone would have shipped past. Beyond the query, code dropped any issue lacking `import_label`; under assignee routing that net catches everything, since the query returns this dev's assigned work, almost none of it labelled. Removed, with a regression test that an unlabelled assigned ticket is still imported.

## [2026.8.7.14] - 2026-08-07

### Features

### Changes

### Fixes

- **The export reconciler stopped retrying transitions Jira will never accept.** Found against the operator's real Jira minutes after shipping the reconciler, and a flaw in what shipped rather than in his instance: on reload it found 25 linked tasks outstanding, repaired 14 (real transitions on real WWD tickets) and was refused on 11 — tickets that are **already closed** in Jira, with nothing diverged. The empty default on `jira_exported_status` made every historical task look unacknowledged, and the reconciler treated a stable property of the ticket's workflow as a transient error worth repeating, so 11 tickets were re-exported every 5 minutes forever at two WARNING lines each. That hammers the API and buries genuine divergence in noise — the exact failure this feature's own test already asserted against for tasks with no `jira_key`.

  A refused `(task, target status)` pair is now recorded and skipped on later cycles, with one INFO line naming how many were skipped. Keyed on the **pair**, not the task, so a genuine status change retries — a ticket that cannot go to Done may well accept In Progress. Held in memory deliberately: one retry per daemon start recovers from a workflow or permission change without another column or another migration. Refusals are still reported loudly the first time.

  **What this does not do:** it does not mark those tickets acknowledged. "Jira refused the transition" is not evidence that Jira is in the desired state, and recording an acknowledgement nobody gave would be exactly the lie this column exists to prevent — they stay visibly outstanding.

## [2026.8.7.13] - 2026-08-07

### Features

### Changes

### Fixes

- **Jira exports are reconciled instead of fired and forgotten (Jira blocker 2).** Two failures, one completely silent: `fire_jira` created a background task, caught exceptions — and *ignored the boolean return*, so an export that ran and did not take produced no exception, no log and no record. And `sync_loop` only imported, so nothing compared the two systems afterwards: a single dropped export left Jira showing a ticket open while the swarm had it done, permanently, because nothing looked again. The operator has hit precisely that. Same architecture as the task panel that only reacted to a pushed frame, failing the same way for the same reason — reacting optimises *latency*; correctness needs a comparable fact.

  `tasks.jira_exported_status` (migration v19) records what Jira **acknowledged**; the task's own status is the desired state, and the difference between them means the export is outstanding whatever the cause — exception, `False` return, restart mid-flight, Jira being down. `reconcile_exports()` re-exports every task where they differ and runs each cycle of `sync_loop` alongside the import, logging the divergence it repairs, so a lost export costs one sync interval instead of lasting until someone notices Jira is wrong. `fire_jira` now binds and inspects the awaited result. `record_jira_export` is deliberately separate from `update()` because it records a fact about the *other* system and must not masquerade as an operator edit, and the empty column default is deliberate so the first reconcile brings existing links up to date rather than assuming a state nobody recorded.

  **Not claimed:** none of this proves the Jira credentials work or that a real Atlassian instance accepts a transition. It proves divergence is detected and retried, which is the property that was missing. Needs a reload (migration v19).

## [2026.8.7.12] - 2026-08-07

### Features

### Changes

### Fixes

- **Jira imports now dedupe against archived rows too (Jira blocker 3).** Archiving is a soft delete — the row survives so its history survives — but `load()` hides archived rows from the board, so anything deriving uniqueness from the board is blind to identifiers those rows still own. That class already caused a live outage the same day via task numbers; `jira_key` is the same fact in different clothes, found by looking rather than by breaking. `JiraService` deduped against `all_tasks`, so archiving a Jira-linked task and re-running the import would create a **second** task pointing at the same issue — worse than the number case in one respect, because no constraint stops it, so it fails silently and leaves two tasks tracking one ticket. Keeping `jira_key` on archived rows (2026.8.7.7) is what made it reachable.

  Fixed the same way as the number: `SqliteTaskStore.jira_keys()` answers for every row it holds, `TaskBoard.known_jira_keys()` unions that with what it can see, and **both** import paths use it — the scheduled sweep and the drag-one-issue path, because fixing one and leaving the other is how #1270/#1281/#1286 became three tickets for one class. An archived-but-known key returns a duplicate marker with `archived=True` instead of `None`, since `None` reads as "nothing happened" in the UI and would leave the operator re-importing an issue that was deliberately archived. Tested across a restart, which is what makes it catch the real bug — the in-memory set hides the problem until the board is rebuilt from the store.

## [2026.8.7.11] - 2026-08-07

### Features

### Changes

- **The status-transition grid is lifted out of the web route (Jira blocker 1).** The entire ruleset lived in `src/swarm/web/routes/tasks.py` with exactly two references — its definition and its single caller — so arbitrary status changes were a **dashboard-only capability**. A Jira sync is fundamentally a status-transition consumer ("Done in Jira, close the task"), and with the grid inside a route it would have had to duplicate it or import from a route module, becoming the fourth copy of a rule whose every previous divergence was a bug: #1280 (BLOCKED had no dashboard exit), #1288 (In Progress selectable but unimplemented), and the 2026-08-07 un-parking.

  The **rule** goes to `swarm/tasks/policy.py` beside the assignment rule — `_LEGAL_TRANSITIONS` plus `status_transition_refusal`, which returns the operator-facing reason so wording cannot drift from the check that produces it (#939's failure mode). The **execution** goes to `TaskCoordinator.change_status`, reachable by any surface rather than sitting behind an HTTP handler; the route keeps a named thin adapter. `_leave_blocked` and the #529 blocker-row obligation moved with it, and `clear_blocker_rows` is now public on the coordinator because the assign route releases a BLOCKED task too and owes the identical cleanup.

  The 42-pair sweep still passes through the same entry point, but its fixture and `test_operator_blocked_hold_surface`'s now carry a **real** `TaskCoordinator` — they had MagicMock daemons, so after the move every call returned a truthy mock and the sweep asserted nothing. The policy is also tested directly, callable without a daemon, a board or a mock, which is what makes it usable from a Jira sync.

### Fixes

## [2026.8.7.10] - 2026-08-07

### Features

### Changes

### Fixes

- **The Tile controls no longer float in the middle of the detail header.** `.panel-header` is `justify-content: space-between` and the header had three loose children — title, Tile button, size select — so the button sat in the middle with gaps on both sides. The two controls are now one flex child, which is the actual fix. It was never noticed because the Tile button's reveal was dead code until 2026.8.6.24 (#1292): a decorator captured `window.selectWorker` before it was defined and was then overwritten, so the button shipped with `display:none` and nothing ever cleared it — the alignment bug is as old as the button and became visible the moment the feature started working.

  The test is a **geometry assertion** in the browser harness, checking both that the controls are adjacent and that they sit at the right of the header, since either alone is satisfiable by a layout that still looks wrong. Its first version did not reproduce the bug and its control caught that: with tile mode off the size select is hidden, leaving two children, and space-between right-aligns two children anyway — it now clicks the real Tile button first. `margin-left:auto` on the wrapper is explicitly **defensive, not load-bearing**, and the CSS comment says so, since no control can distinguish its presence today.

## [2026.8.7.9] - 2026-08-07

### Features

- **A real browser drives the dashboard in tests (item 3).** Every other client-side test here scans `dashboard.js` as text — and those were green throughout every dashboard bug of the last two days, because they were green *while production was broken*. This runs the real app, loads the real page in Chromium, executes the real JavaScript and asserts what the DOM actually shows. The `browser` marker is registered in `pyproject` so the warnings-as-errors rule accepts it; deselect with `-m "not browser"`.

  A control also found a worse bug in the new test itself: the reconciliation case closed the socket from inside the page, which silently did nothing because the main socket is a closure variable, not `window.ws` — so the test proved the *push* worked and stayed green when a control removed the reconciler's re-render entirely. It now severs the push server-side by clearing the hub's clients and asserts the page was connected first.

### Changes

### Fixes

- **The task editor save now sends only what changed (item 4).** The edit route treats a field's *presence* as an instruction to overwrite (`if field in body`), so submitting every field on every save let a field the modal got wrong silently destroy good data — exactly how `target_worker` was wiped on #1301-#1303, where the select could not hold an off-list worker, reported `""`, and the save posted that over a real value. Fetching the task fixed the *display*; sending a diff removes the *mechanism*, since a field the modal cannot represent is simply not mentioned. `status` already worked this way and carried a comment explaining why; this extends the same discipline to the other eleven fields. Per-field `.trim()` is preserved exactly so the value reaching the server does not change as a side effect, and a missing snapshot falls back to **sending** (an unnecessary write) rather than skipping (a silently dropped edit).

- **The initial full-page render never stamped `board_version`** — only the htmx partial handler did — so every page load started at version 0, the reconciler saw instant drift and burned a wasted refresh. Both the template and the partial handler were individually correct; only rendering the actual page in a browser could show it.

## [2026.8.7.8] - 2026-08-07

### Features

### Changes

- **The assignability rule is stated once, in `swarm/tasks/policy.py` (item 1 of four).** "Can this task be assigned?" was answered independently in **three** places — `TaskBoard.assign`, `TaskCoordinator.assign_task`, and `/action/task/assign`, which normalised the status first so the other two would accept it. Every divergence between those copies has been a bug, and three landed in one evening: #894/#1281, where `is_available` ("the auto-assign *drone* may take this") was used to gate an **operator's** explicit routing so nobody could assign a HOLD task; the route working around that gate by calling `approve()` on a BACKLOG task, which un-parked it; and relaxing the board's copy without the coordinator's, turning "silently un-parks" into "409, cannot assign". `task_coordinator` already carried a comment saying its check had to be edited in lockstep with the board's — a rule that must be edited in lockstep across layers should be written down once.

  **It returns the reason, not a boolean,** so refusal text cannot drift from the check it explains. #939 cost the Queen an hour on the theory that the target worker's load mattered — the target is never consulted — because one layer's message said only "(not available)". Every refusal now names the task's own status and what would resolve it (#1057), asserted as a property across every refusal the policy can produce rather than per-message. A second test asserts the consolidation itself as a property: no layer may pair an `is_available` check with a hold/status special case, so a fourth caller — a Jira sync, say — cannot quietly add a fifth copy. `is_available` stays legitimate for the drone's own selection, which is a different question.

### Fixes

## [2026.8.7.7] - 2026-08-07

### Features

### Changes

### Fixes

- **One archive write path, and blocker rows cleared in *both* directions.** Three surfaces archive a task — the dashboard x, `swarm_archive_task` and `queen_archive_task` — and each did its own board call plus its own history entry. The blocker-row obligation was missing from all three, because there was nowhere shared for it to live: `BlockerStore.clear_for_task` only ever removed rows where the task is BLOCKED, and nothing removed rows where it is the BLOCKER. So archiving a task others were waiting on left them blocked on something that had left the board — invisible, unclearable, and nudged about forever by the IdleWatcher. That is #529's shape, made reachable again by a feature shipped four hours earlier.

  The fix is the consolidation, not another copy of the rule: `TaskManager.archive_task` is the single write path (board archive, both blocker directions, history, drone log), the two MCP verbs and the dashboard route all call it, and a test asserts the **property** — no surface may call `board.archive` directly — so a fourth surface that skips the obligation fails a test instead of stranding a worker. `BlockerStore.clear_blocking` is the new mirror of `clear_for_task`, and the store is injected into `TaskManager` rather than reached through the daemon so the path that owns the obligation owns the dependency; when it is absent the archive still succeeds and **logs** that the rows were not cleared rather than pretending.

  **Deliberately not cleared:** `jira_key` and the cross-project fields. Those record where a task came from, which stays true after it leaves the board, and an external system may still reference it — pinned by a test so a future "tidy up on archive" is a decision rather than a side effect.

## [2026.8.7.6] - 2026-08-07

### Features

### Changes

### Fixes

- **Archiving the highest-numbered task broke *all* task creation via number reuse.** A live outage: `swarm_create_task` began failing outright with `UNIQUE constraint failed: tasks.number`. Archiving keeps the row — including its unique `number` — but `load()` deliberately excludes archived rows and `TaskBoard.__init__` derives `_next_number` from the loaded tasks, so the counter is blind to exactly the rows that still hold numbers. After archiving #1305 the DB's max was 1305 while the max among live rows was 1304, and the board handed 1305 out again. The archive design deliberately keeps rows so history survives; the consequence that a hidden row still owns a unique number was not followed through.

  `SqliteTaskStore.max_number()` now reports the high-water mark across all rows, archived included, and the board seeds its counter from `max(visible, high_water)`. The tests assert it **across a restart**, which is what makes them catch the real bug — the in-memory counter hides the reuse until the board is rebuilt from the store, so a same-process test would have passed while production failed on the next reload. Both were written red first, failing with `IntegrityError`. Needs a reload: the running daemon still holds the bad counter in memory.

## [2026.8.7.5] - 2026-08-07

### Features

### Changes

### Fixes

- **The task view converges on server state instead of reacting to a push — the structural fix rather than a fifth patch.** The operator saved #1300 and #1301 repeatedly, saw nothing change, and concluded the assignment was not sticking; `task_history` showed all three saves had **succeeded** and both rows already held the values he wanted. He was re-saving work that had already worked, because the view never told him.

  **Root cause 1 — two sources of truth for a task.** The editor was built from ~17 `data-*` attributes baked into the row at render time, so a row that had not re-rendered made the modal display stale values *and write them back on save* — the mechanism that silently wiped `target_worker` on #1301-#1303. Every edit path now goes through `showTaskEditorById`, which fetches `/api/tasks/{id}`; the 17-argument DOM-sourced opener is **deleted rather than shimmed** so it cannot quietly come back, and a test asserts the detail payload carries every field the save writes back, since a field the editor does not load opens blank and is posted blank.

  **Root cause 2 — no way to detect drift.** Reacting to a pushed frame optimises latency; correctness needs reconciliation. Four fixes had already shipped for four ways the push can be lost (a stranded debounce timer #1294, a reconnect that skipped the resync, a frame dropped with no running loop, a filter-restore swallowed by an empty catch) — each real, none of which could have been the last, because a design that only reacts cannot notice it missed something. The board now carries a monotonic version bumped in `_notify`, the single choke point every mutating verb already passes through, so a mutation cannot change the version without broadcasting nor broadcast without advancing it. Renders are stamped with it, `GET /api/tasks/version` is a one-integer probe, and the client compares every 15s while visible, logging the drift it repairs.

  The backstop deliberately does **not** check socket health first: every failure it exists to catch presents as a perfectly healthy connection from the browser, so gating on that would switch it off in exactly the cases that need it. Needs a reload.

## [2026.8.7.4] - 2026-08-07

### Features

### Changes

### Fixes

- **Assigning through the dashboard un-parked a Backlog task.** "Which holds fine" was the clue in the operator's report: the board layer was already correct from 2026.8.7.3, since `SwarmTask.assign` preserves BACKLOG, so the promotion had to be coming from somewhere else. `/action/task/assign` **normalised** the task to UNASSIGNED before assigning, calling `existing.approve()` on a BACKLOG one — which existed purely so the old `is_available` gate would accept the assign, and un-parked the task as a side effect *before* `board.assign` ever ran. The previous fix could not possibly have helped him; it was one layer below the thing that broke.

  Two more layers went in the same pass. `auto_start` defaults to true, so once a task legitimately stays BACKLOG through assignment that branch would hand an idle worker the very task the operator took out of play — strictly worse than the reported bug — and it is now guarded on the status **read back** from the board after assignment, not the pre-assign snapshot. And `TaskCoordinator.assign_task` has its own `is_available` gate; removing the route's normalisation without widening that one would have turned "silently un-parks" into "409, cannot assign", the same bug in different clothes.

  Tests are driven through the **real route**, not the board: every board-level test from 2026.8.7.3 passed while this was broken, which is the whole lesson — a fix verified one layer below the reported symptom is not verified. A control caught a weak test too, where stubbing `start_task` with a non-idle fixture worker meant the start branch never ran and the test passed even with the guard deleted.

## [2026.8.7.3] - 2026-08-07

### Features

- **A Backlog task can now carry an owner — "this is sculpt-studio's, later".** The operator set a task to Backlog, assigned it, and the assignment was dropped on save; Backlog is meant to park work, not to strip its routing. The existing answer was ASSIGNED plus a `hold` tag (`auto_start_next_assigned` skips `is_on_hold`), but the direct route is provably safe, so Backlog carries an owner.

  **Three obstacles stood in the way, and fixing any one alone would have *looked* like a fix while still failing:** `SwarmTask.demote_to_backlog` dropped the owner — deliberate and documented, on a rationale about *display* which is rewritten rather than deleted; `TaskBoard.assign` gated on `is_available`, which requires UNASSIGNED, so the call just returned `False` (the same shape as #894/#1281, where a drone-selection question is used to refuse an operator's explicit routing); and `SwarmTask.assign` forced `status = ASSIGNED`, so even once permitted, assigning a parked task would have un-parked it.

  **The safety half of the old rationale is untouched and now asserted rather than assumed.** Every dispatch gate keys on status — `is_available` needs UNASSIGNED, `auto_start_next_assigned` and the state tracker need ASSIGNED, the Queen selects only from `available_tasks` — so nothing dispatches on `assigned_worker` alone and an owned Backlog task is inert. Tests assert that directly, because "safe by construction" is exactly the claim that rots when a new dispatch path appears; a control that makes BACKLOG drone-available fails.

### Changes

- **Fallout from Backlog-with-an-owner, all deliberate.** `queen_reassign_task` no longer refuses a Backlog task — the danger swaps sides, so a test pins that routing parked work does *not* un-park it — and that handler's hardcoded "(ASSIGNED, not started)" reply became a lie the moment assignment stopped implying that status, so it now reads the status back from the board (#1268's AC). Three pinned assertions in `test_operator_blocked_hold_surface.py` that asserted the owner was dropped were updated with the reasoning recorded, and rewritten to preserve *whatever* the owner was rather than hardcoding one. Two #939 refusal tests used a Backlog task as their "cannot be reassigned" fixture and were retargeted to a terminal task, since what they protect is #939's guarantee (name the reason, never imply the target is at fault), not the status that triggers it.

  **Not changed:** `reopen` still drops the owner. Reopening finished work is a different act from parking live work and was not part of the decision — pinned so it stays a decision rather than becoming an oversight.

### Fixes

## [2026.8.7.2] - 2026-08-07

### Features

### Changes

### Fixes

- **Editing a cross-project task silently wiped its target worker, and the CROSS badge outlived the routing it described.** Two operator-reported bugs from a single edit that only added a tag, and they compose: the first empties the routing, the second keeps the badge. That is how #1301-#1303 reached `is_cross_project=1` with both `source_worker` and `target_worker` empty — a state unreachable on purpose and un-fixable through the UI.

  **Bug 1 was silent data loss, entirely in the browser.** The modal's worker `<select>` options are built from the workers rendered on the page, but a cross-project task can legitimately target a worker that is not one of them (another project's, a renamed one, a decommissioned one — #1301-#1303 targeted `claude-team-config`). Assigning `select.value = x` with no matching `<option>` is a **silent no-op**: the select stays on "—" and reports `value === ""`, `submitTaskModal` then unconditionally posts `target_worker=""`, and the edit route passes the present key straight through. An off-list value now gets its own labelled option so the round trip is lossless.

  **Bug 2 was a one-way latch**, the same shape as #1294's debounce: `if source_worker or target_worker: task.is_cross_project = True` could only ever *set* the flag and nothing cleared it, while `task_list.html` gates the CROSS badge purely on it. It is now recomputed from the post-assignment state, and only when the edit actually touched one of the two fields (`is not None`, not truthiness), so an unrelated edit cannot silently reclassify a task.

  **Two of the new tests were weak and the negative controls caught them** — both would have shipped as false assurance. The JS region helper used a fixed 1400-character window that ran past the function into a neighbouring loop with its own `sel.appendChild(opt)`, so deleting the real line still matched (same mistake as #1292's classifier); it is now bounded by the closing brace. And the "tags-only edit must not reclassify" test used a *normal* cross task, where a correct recompute and an over-broad one agree — it passed against a deliberately broken `if True:`. It is now seeded in the exact #1301 state (flag set, routing already empty), the only state where the two disagree. Five negative controls, each failing exactly one test.

  **Not touched, and recorded rather than swept in:** the assigned-worker select has the same display flaw but *not* the data loss — its save fires only when the value is non-empty and changed, so an off-list assignee is never cleared.

## [2026.8.7] - 2026-08-07

### Features

### Changes

### Fixes

- **The CROSS badge was invisible in *both* themes because `.type-cross` had no background rule.** Operator-reported as "doesn't show in light mode either" and then "even worse in dark mode, seems like they are inverted" — and "inverted" is the precise description. `.type-badge` sets `color: var(--canvas)`, text the colour of the *page background*, which is readable only on top of a coloured badge, so every sibling must supply one: `.type-bug` has poppy, `.type-feature` lavender, `.type-verify` leaf, `.type-cross` had nothing. The label rendered as canvas-coloured text on a transparent row — near-white on white, near-black on dark — always the one colour that cannot be read against the row behind it. Given amber, which no other `type-*` badge claims.

  This is #1291 item 5 recurring exactly (`.text-center` used 8 times, defined nowhere). **The guard from that ticket could not have caught it**: `test_utility_classes_used_by_config_are_defined` checks a hardcoded list of four names in one file, and a guard that enumerates the instances already known cannot catch the next one — which is the entire failure mode of this class. The new sweep collects every `type-*` class from every template and asserts each is defined, so a badge added to markup without a rule fails a test instead of presenting to the operator as an invisible label.

  **Two scan bugs found and fixed while writing it, both the kind that make a green test meaningless.** The file glob was `(_WEB / "templates")` when `_WEB` already points at `templates/`, so it scanned a non-existent directory and found zero classes — caught only because the positive control asserts the scan finds real ones; without it the sweep would have passed vacuously forever. And `\btype-` matched the *tail* of `.pb-event-type-applied`, because a hyphen is a word boundary, reporting seven perfectly-defined playbook classes as missing; now `(?<![\w-])type-`. Negative controls include adding a **new** undefined badge to `task_list.html`, which proves the guard catches a future badge rather than only this one.

  **Not touched:** `.type-chore` and `.type-tag` are defined in `base.html` and used by no template and no JS. Dead, but deleting them is not this bug.

## [2026.8.6.30] - 2026-08-06

### Features

- **Archive a task from every surface, and stop deletion destroying its history (#1298).** Deleting a task was reachable from the dashboard and nowhere else — 0 of 22 worker verbs, 0 of 17 Queen verbs, 0 CLI actions, against a `DELETE /api/tasks/{id}` route that had existed all along. A worker that filed a task by mistake, in duplicate, or as a throwaway probe could only close it with a resolution that is a lie or leave it on the board, and resolutions become learnings that are re-served to future workers as advice.

  **The silent data loss underneath it is the more serious half and was not in the ticket.** `task_history.task_id` is `REFERENCES tasks(id) ON DELETE CASCADE` with `PRAGMA foreign_keys=ON` applied per connection, so the dashboard's x destroyed every history entry for the task — the audit trail `swarm_get_learnings` and playbook synthesis read. Worse, `TaskManager.remove_task` appended its REMOVED event *after* the delete, so the one row guaranteed to be missing was the record of the removal itself.

  **The design touches very little on purpose.** An archived task is stamped with `tasks.archived_at` (schema v18) and dropped from the board's in-memory dict while the SQLite row stays. Every query reads that dict, so roughly 40 `all_tasks` call sites exclude archived work with none of them changed. Two store reads are the whole trick: `load()` skips archived rows so a restart cannot resurrect them, and `save()` scopes its which-rows-disappeared query to live rows — unscoped it classifies every archived task as removed and hard-deletes it on the next persist, cascading the history away and defeating the mechanism entirely. `board.archive()` stamps *before* dropping from memory for the same reason.

  **Surfaces per the operator's decisions:** `swarm_archive_task` (own, unstarted only) and `queen_archive_task` (unrestricted). The worker's two preconditions are deliberate — erasing another worker's work is not a capability any worker needs, ACTIVE is refused because archiving live work loses that it was under way, and CLOSED is refused because a resolution may already have been served as a learning (correct those with `swarm_annotate_resolution`, #1274). **Not one verb with a role check**: authority that depends on the caller is how #1281's `is_available` ended up gating two different questions with one predicate. `board.remove()` stays a hard delete for the test harness, whose tasks have no history worth keeping.

  17 tests, including the property that a hard delete really *would* have lost the history (so the archive assertion is not vacuous), that a restart does not resurrect, and that a later persist does not hard-delete the archived row. AC-5's cross-surface property is asserted over the **registries** rather than as "the two verbs I added exist", so a future surface added without archiving fails here instead of being found by the operator — which is how #1270/#1281/#1286 became three tickets for one class. Six negative controls, injection verified applied each time. Needs a daemon reload: migration v18 plus two new MCP verbs.

### Changes

### Fixes

## [2026.8.6.29] - 2026-08-06

### Features

### Changes

### Fixes

- **A stranded debounce silently killed `tasks_changed` for the life of the process (#1294).** `broadcast()` debounces `tasks_changed` by putting a `TimerHandle` in `_broadcast_pending` and returning; the only thing that ever removed an entry was `_flush_broadcast`, which runs when the handle *fires*. Schedule one on a loop that stops before the 100 ms elapses and the entry is never removed, so every later `tasks_changed` found the key present, scheduled nothing, and returned — the frame type dead for the whole life of the process. No exception, no warning, and not even the no-running-loop branch, because there *is* a running loop at that point.

  **What identified it was two facts from the operator's screenshots.** The stale summary line ("2 blocked, 1247 done") is rendered by the same `/partials/tasks` fetch as the rows, so the whole partial had not re-fetched — never a row-rendering problem. And the Activity badge incremented 3 → 5 between shots, so the WebSocket was alive and delivering. A live socket with one frame type dead is only explicable if that type is handled differently, and it is: `tasks_changed` and `worker_changed` are debounced, Activity/buzz frames are not. Clicking a chip issues a plain HTTP fetch that never touches the hub, which is why the board was right the instant he interacted and never before.

  **The fix stores the loop alongside the handle**, since `TimerHandle` exposes no public way to ask which loop it belongs to. Before trusting a pending entry it must belong to the currently running loop, the loop must be open, and the handle must not be cancelled; anything else is stranded — cancelled, dropped, re-scheduled, and logged at WARNING, because a stranded debounce means frames of that type have been dropped since the moment it stranded. The reproduction was written first and failed on the old code.

  **Not yet confirmed on the operator's daemon.** This is a real defect producing his exact symptom and it is now impossible by construction, but what stranded the handle in his process is unidentified — the obvious `asyncio.run` site in `mcp/handlers/_email.py` is only the no-loop fallback. After a reload the new WARNING names the stranding if it recurs, which is the evidence that was missing every previous round.

## [2026.8.6.28] - 2026-08-06

### Features

### Changes

### Fixes

- **A failed filter-restore was swallowed by an empty `catch`, and the client half of the #1294 contract is now guarded (AC-4).** Every guard added for this bug class asserts the *server* emitted a frame — the change event for all 11 verbs (2026.8.6.10), and a real client receiving it over a real socket (2026.8.6.27). All of them stay green if the frame arrives and changes nothing on screen, which is the operator's actual report.

  **The mechanism reproduces the report exactly and is invisible to every server-side test.** `refreshTasks` builds `status=` from `activeTaskFilters`; send no status param and the server returns the *unfiltered* list, which includes DONE — so with the operator's chips (Backlog + Unassigned + Assigned + In Progress + Blocked, Done off) a closed task is re-rendered rather than removed, and clicking any chip then filters it away. Frame delivered, refresh performed, nothing appears to happen. The path where that can happen is the restore-saved-filters IIFE, which populated `activeTaskFilters` from localStorage inside `try { ... } catch(e) {}`: a part-way failure left the Set empty while the chips still read as active. That catch now logs to `console.error` and names the consequence — an empty catch around state restoration is not defensive, it is a symptom with the evidence deleted.

  **Not claimed as the cause**: the operator's screenshot shows five chips active, consistent with the Set having been populated, so this is demonstrated-possible rather than confirmed. `tests/test_task_panel_client_contract.py` covers the three silent ways a delivered frame changes nothing — an htmx swap into an id no template defines (checked for all four targets), the `tasks_changed` case no longer calling `refreshTasks`, and `refreshTasks` losing the status param — plus that the chip handler and the refresher read the **same** filter state, asserted as a conjunction because an `or` would pass on merely finding the handler and the two halves disagreeing *is* the bug. Comment-only lines are stripped before scanning: the comment added with this fix names both `status=` and `activeTaskFilters`, and a scan that reads comments reports a fix as the defect it fixed (fourth time in this repo).

## [2026.8.6.27] - 2026-08-06

### Features

### Changes

- **An MCP-originated completion is now proven to reach a real WebSocket client (#1294).** The emit chain had been hand-traced correct three times while the symptom kept recurring, and `tests/test_task_board_broadcasts.py` drives `StatePublisher` directly — it proves the middle of the chain and cannot fail for either end, since it never goes through the MCP tool handler that mutated the task and never puts a frame on a real socket. The new test asserts both ends: real MCP handler → real daemon → real `BroadcastHub` → real aiohttp `/ws` → client. **The frame does arrive**: closing a task through `_handle_complete_task` pushes `tasks_changed` to a connected authenticated client, a UI-path status change behaves identically on the same daemon (AC-2's direct comparison), and the closed task does leave the operator's exact five-chip filter.

  **The false reproduction is the more useful half.** The first version of this test showed *no* frame on *either* path and was one step from being reported as #1294 reproduced. The cause was entirely the fixture: `conftest.py:244` sets `d.broadcast_ws = MagicMock()` and `StatePublisher` captures it at construction, so `on_task_board_changed` ran, called the mock, raised nothing and reported success while no frame reached the hub. Third MagicMock false result in this investigation, and in the direction that matters most — mocks do not only make broken code look fine, they make correct code look broken.

  Two controls exist so neither trap can return silently: one asserts the broadcast seam is real *before* any conclusion is drawn from a missing frame, and one asserts `task_board.on_change` is actually subscribed, since `make_daemon` skips `__init__` and therefore skips `_wire_task_board`. Both are established by rebinding production's own functions rather than re-implementing the wiring in the test — a substitute would test the substitute. No production code changed; #1294 stays open because delivery across the tunnel to the operator's browser is still unproven.

### Fixes

## [2026.8.6.26] - 2026-08-06

### Features

### Changes

- **What the task partial returns per filter is now pinned, and a wrong theory about #1294 is recorded as dead.** The proposal was that "the finished task didn't disappear" might not be a stale panel at all: the unfiltered view returns finished tasks, so a successful refresh would re-render the completed row rather than remove it. Mechanism real, conclusion wrong — the operator's screenshot shows his chips are Backlog + Unassigned + Assigned + In Progress + Blocked with Done and Failed off, so #1292 left his filtered set the moment it closed and a real refresh *would* have dropped the row. #1294 is a genuine live-update failure. The theory lives in the test docstring rather than being deleted, because the lesson is that a plausible mechanism found by reading code is not evidence about what the operator was looking at.

  Three tests appended to the existing file for this surface rather than a new one: the unfiltered view returns finished tasks (pinned so hiding them by default would have to be deliberate); an open-status-only filter *does* exclude them, which is the property that makes the observation a bug rather than correct behaviour; and `_display_sort` orders finished work by priority before recency, so a just-closed normal task sorts below older high/urgent ones — on the live board that put #1292 at row 26 of 1249. Negative controls: hiding finished by default fails 3, leaking finished into an open-status filter fails 1, sorting finished by recency fails 1. No production code changed.

### Fixes

## [2026.8.6.25] - 2026-08-06

### Features

### Changes

### Fixes

- **A reconnect never resynced on the two paths that need it most.** `ws.onopen` re-fetches all four panels after a reconnect, because events that arrived while the socket was down are gone for good — but that block was gated on `reconnectDelay > 1000`, a proxy for "we retried at least once" that is true only when the reconnect came through `onclose`'s backoff doubling. `forceReconnectMainWs()` sets `reconnectDelay = 1000` itself before connecting, and the restart watchdog makes `onclose` return *before* the doubling and then calls `ensureMainWsConnected()`. Both reach `onopen` with the delay still 1000, so `1000 > 1000` was false: green connection dot, four stale panels, no toast, no log. Now gated on a `hasConnectedBefore` flag, which asks the actual question instead of inferring it from a backoff value, and is set *after* the guard reads it so a fresh page load still does not re-fetch what the server just rendered.

  **A broadcast owed to connected clients was dropped in silence.** `broadcast()` and `_send_ws_now()` both bail with a bare `return` when there is no running event loop — right in CLI and test contexts where nobody is listening, but when `ws_clients` is non-empty a real frame is thrown away leaving no evidence of any kind, which is precisely why this class cannot be diagnosed after the fact. Now WARNING with `stack_info`, gated on `ws_clients` being non-empty so test and CLI broadcasts stay quiet rather than training everyone to ignore it. Operators run at default WARNING, so that is the level that reaches them.

  Neither is confirmed to be the cause of the operator's recurrence of #1275 (a task completed over MCP stayed in the panel until he clicked a filter chip) — the investigation continues as #1294 and #1275 is annotated stale — but both are real, both were provable by reading, and both are the same shape as the report: the state change is real and durable and nothing can see it. **Ruled out for #1294 so it is not re-traced a fourth time:** the emit chain is wired and covered for all 11 verbs, MCP transport is HTTP to the same process that serves the dashboard so there is no two-daemon split, there is no executor in the MCP dispatch path, the normal reload path ends in a full page reload, and `onAppFocus` already refreshes on visibility return. 9 cases in `tests/test_reconnect_resync.py`, each negative control asserting the injection applied first.

## [2026.8.6.24] - 2026-08-06

### Features

### Changes

### Fixes

- **Tile view was unreachable because a `selectWorker` decorator was dead twice over (#1292).** `dashboard.js` carried a decorator on `window.selectWorker` about 130 lines *above* the base definition: it captured `window.selectWorker` before anything had assigned it, so its captured original was `undefined`, and the base definition below then overwrote the wrapper outright so it never ran. Net effect — `#tile-mode-btn` ships with `style="display:none"` and nothing ever cleared it, while both the markup and the JS read as fully implemented. The wrapper is deleted and the reveal moved into the base itself, at the top before any early return, which removes the ordering hazard rather than relocating it.

  `tests/test_select_worker_ordering.py` pins the shape: exactly one base definition, no capture of `window.selectWorker` before it, the reveal inside the base, and every call to the surviving Command Center decorator's captured original keeping its if-guard. **Three of that test's own iterations were wrong first, all in ways that would have let the bug back in silently** — classifying bodies by a fixed 1200-character window and by whether they *mention* `_origSelectWorker`, which matched the comment explaining the removal so the fix read as the defect (third time a scan here has matched its own prose); stripping comments with a DOTALL `/\*.*?\*/` over the whole file, which pairs a `/*` inside a string literal with the next `*/` downstream and ate one of the two assignments the test exists to count; and searching an 800-character window for the guard when there are two guarded calls in it, so deleting one guard left the other for the substring to find (same first-match false negative as #1291's D-pad test). Now bounded by the next assignment, keyed on a call, line-based comment blanking that preserves line numbers, and per call site.

## [2026.8.6.23] - 2026-08-06

### Features

### Changes

### Fixes

- **The d-pad's blanket transparency is reverted.** Operator, after seeing 2026.8.6.22 on the phone: "now it is natively transparent on light mode. How it looked colour wise on the last pass was good." Idling the whole pad at 0.55 opacity so transcript text showed through was the wrong trade and undid the previous fix — dimming the pad dims the *arrows* as well as the background, which is the light-mode contrast complaint that opened item 6 in the first place. Readability of the control beats readability of what is behind it. The per-button rgba background is as see-through as this should get, and the layering rule already handles the case that actually mattered (the pad covering an open menu with a red destructive item).

  **The Queen card and worker switcher now share one row, 25/75**, on the operator's request for "more real estate on mobile": they were two stacked full-width rows, so two rows of vertical space went before any transcript appeared. 75% to the switcher because it carries the long text (state + name + provider), 25% to the Queen, which only needs to stay tappable and identifiable. **"Queen Dashboard" is renamed to "Queen"** in the card and in `dashboard.html` — at 25% width the longer label would not have fitted, so the rename and the layout change are one fix rather than two.

  **A false-negative test caught by its own control, and this is the part worth keeping.** The new test asserted "no opacity on `.term-dpad`" using `re.search`, which inspects only the *first* matching rule — re-adding a second `.term-dpad { opacity: .55 }` later in the stylesheet passed all 23 tests. Now it checks every `.term-dpad` rule via `findall`. A test that cannot detect the regression it guards is worse than no test because it reads as coverage, and CSS is especially prone to this since a later duplicate rule silently wins.

## [2026.8.6.22] - 2026-08-06

### Features

### Changes

### Fixes

- **The mobile worker switcher now follows the pill list's order, reversing what shipped an hour earlier.** WAITING/BUZZING had been sorted to the top on the theory that the attention-needing worker should come first; the decisive fact that reasoning missed is that the pill list is **drag-to-reorder**, so its order is the operator's own arrangement. Re-sorting the dropdown silently overrode a choice he had made by hand, and position stability is the entire point of muscle memory. Iterating the list untouched also deletes a hazard the sorted version needed a fallback loop to cover: with no filtering and no sorting, no worker can be dropped from what is the only way to reach one on mobile.

  **The d-pad now stands down while an overflow menu is open — completing item 6 and #1291.** Evidence 084202 showed the d-pad painting on top of the open menu, obscuring items including a red destructive one. **Not fixed with a z-index bump, and that is the interesting part**: the menu is already z-index 100 and the d-pad only 11, so raising the menu changes nothing — the d-pad's container establishes a stacking context that outranks the header's, so the two values are never compared in the same context, and a test asserting z-index numbers would have passed while the bug remained. Instead `body:has(.mobile-overflow-menu.open) .term-dpad { display: none }`: it cannot lose a stacking-context argument it is not having, and arrow keys are useless while a menu is up. Untangling the contexts would mean re-parenting the terminal chrome — large and risky for a small symptom.

  Test count was checked before and after the edit (21 collected → 20: two obsolete order tests removed, one replacement added), deliberate arithmetic because in #1287 a careless region-rewrite silently deleted 7 tests and the suite still passed at a lower count. **One deviation from #1291's written acceptance criteria, recorded rather than quietly claimed as met**: AC-5 says the Focus button should be toggleable from the Action Buttons config; the operator chose removal instead once it was explained what the button did.

## [2026.8.6.21] - 2026-08-06

### Features

- **A mobile worker switcher replaces the scrolling worker strip (#1291 item 1).** The operator's top complaint was "I find myself scrolling left and right all the time to find active workers"; the evidence screenshots show the strip clipping mid-word ("sculpt-studio (cla…") and, with 16 workers, the active one sitting entirely off-screen. A scroller's cost grows with worker count and a dropdown's does not. The affordance was chosen by the operator in interview: a dropdown for switching **plus** the active worker pinned beside it, so switching never loses sight of who you are looking at.

  **Rendered inside `partials/worker_list.html` on purpose** — that partial re-renders on every `workers_changed` swap, so the dropdown stays in sync with worker state for free rather than through a second sync path that could drift from the pills. Ordered by an **explicit state list** rather than `sort(attribute='state')`, because alphabetical would put WAITING last and WAITING is the state that needs the operator most; the order is WAITING, BUZZING, STUNG, RESTING, SLEEPING, with a fallback loop for any state not in the list — on mobile the dropdown is the only way to reach a worker once the pills are hidden, so a dropped worker is an unreachable worker. The change handler is **delegated on document**, since a listener bound directly to the `<select>` would be discarded by the first htmx swap and present as an intermittently dead dropdown rather than a broken one.

  **Pills are hidden on mobile, not deleted**: desktop still uses them and they are the drag-to-reorder surface there. The `.worker-list::after` scroll fade goes with them — it exists only because the row scrolled (#543, where #515/#540/#541 all chased the wrong element) and leaving it would paint a gradient over the new switcher. A deliberate removal of a hard-won rule, scoped to mobile only.

  **A near-miss worth recording.** The first render check used MagicMock workers and showed the dropdown listing only one worker, with every normal one missing — one step from "fixing" the template. The cause was the harness: Jinja's `selectattr('state','equalto',X)` returns zero matches against MagicMock while working correctly on dicts and real objects, and production passes plain dicts from `_worker_dicts`. This is the mirror of #1281, where a MagicMock board made a genuinely broken fix look fine — mocks can fail in both directions, so the render test now asserts against dicts and says why.

### Changes

### Fixes

## [2026.8.6.20] - 2026-08-06

### Features

### Changes

### Fixes

- **The task panel is readable at 390px (#1291 item 7).** The operator, pointing at screenshot 084114: "on mobile the task panel looks terrible" — titles came out one or two words per line. **The cause is not bad wrapping**: the row is a single flex line carrying about ten children (status icon, number, status badge, target, title, worker, age, blocked/verify badges, then Edit/Unassign/Log/x), and only `.task-title` has `flex:1`, so it is the only child that can shrink while the fixed-size ones consume the whole 390px first. The title was starved, not mis-wrapped — which is also why every other element in the screenshot looked fine. The row now wraps: number and status badge stay on line 1 (they are what you scan by), the title takes full width on line 2, meta and buttons fall to line 3, and the buttons keep `flex:0 0 auto` so they wrap as a group instead of squeezing the title back down.

  **What was deliberately not touched is the more useful half.** The same screenshot shows the bottom tab strip and the filter chip row clipped, and #1291 listed both as part of the defect — but `.filter-bar` and `.bottom-tabbed .tab-group` already carry `overflow-x:auto` on mobile, added with a recorded rationale after a tab was rendered at x=647 in a 414px window, "permanently off-screen with nothing to scroll it into view". The clipping is that affordance working, and overriding it would have undone a documented fix to a worse bug — exactly what the ticket's own instruction 3 warns about. Left alone and pinned by a test so a later pass does not "fix" it.

## [2026.8.6.19] - 2026-08-06

### Features

### Changes

### Fixes

- **Items 2-6 of the #1291 mobile UI report, a cross-project handoff from sculpt-studio.** All nine screenshots were opened and described before any code, and every item was interviewed with the reasoning stated first, per the ticket's standing instruction that nothing should be assumed.

  **Item 5's root cause was a missing CSS class, not a layout bug.** `.text-center` appeared 8 times in `config.html` and was defined nowhere, so every use was a silent no-op. The visible symptom was the notification event-filter matrix: Desktop/Terminal/Email/Webhook headers appeared offset right of their checkbox columns, and the offset grew with the header word's length (Email ~15px, Webhook ~36px) — because header and checkbox both start at their grid column's left edge and only the header is wide. That prediction is what identified the cause; defining the utility fixes the matrix and the other seven dead usages.

  **Item 4's right-alignment was deliberate and is scoped rather than reversed.** `base.html` sets `text-align:right` on `.config-input` on purpose and ships two opt-out classes alongside it; it is coherent on desktop where the input is 550px in a label-left/value-right row, and wrong at 390px where the input goes full-width and the value drifts from its label. Confined to mobile by a media query with the rationale comment rewritten rather than deleted. **Item 3**: the page must never scroll sideways — screenshots 084423/084512 show content clipped at the *left* with empty space at the right, meaning part of the page was unreachable. Causes in order were fixed 550px inputs inside content-sized flex rows and `.approval-rule-row` packing six controls into one line; wide tables turned out *not* to be a page-level cause, because the Per-Worker Breakdown is already wrapped in `.overflow-x-auto` — and unlike `.text-center`, that utility is defined. Rule adopted: wide content scrolls inside its own container, the page never does.

  **Item 2 — Focus mode removed, the operator's choice over making it configurable.** `toggleFocusMode` added `.focus-mode` to `.detail-area`, collapsing the bottom panel so the transcript filled the viewport, persisted in `sessionStorage`, and could not be turned off because it was hardcoded markup rather than one of the config-driven Action Buttons. Removed in full — button, CSS, dispatch entry, both functions, the `switchTab` side effect, and the on-load restore IIFE — because a half-removal would strand anyone who had it enabled in a collapsed layout with no button to get out. **Item 6's contrast half**, raised during the interview rather than in the original report: the d-pad is a dark translucent pad with a leaf-green glyph, so in light mode the arrows are nearly invisible; a light-theme override keyed off both `data-theme` and `prefers-color-scheme` was added, leaving the dark appearance untouched.

  **The tests pin the class of defect, not the pixels** — these are visual claims and the ticket rightly says a browser at 390px is the judge. What *is* testable is a utility that is used but never defined, which is how item 5 happened and which nothing in 5849 existing tests could have caught. Also pinned: that the Focus removal is complete rather than partial, and that the stylesheet is still brace-balanced, since the focus-mode rules were nested inside a media query and a careless cut would have silently broken every rule after it.

## [2026.8.6.18] - 2026-08-06

### Features

### Changes

### Fixes

- **`swarm_edit_task` can append, and it now reports the character delta (#1289).** The author caused this and measured it: working #1274 they appended AC-1 findings to a 3,819-character description, produced 6,124 characters in a staging file, then called `swarm_edit_task` by retyping the text — a later read returned 3,950 characters, with roughly 2,200 characters of verified findings gone. **The call reported success**, and the loss was noticed hours later by accident because a length looked wrong. Two properties made it dangerous rather than annoying: the failure is silent and reports success (#1159's shape), and it scales with the value of the record — the longer and more carefully built the description, the more one edit destroys and the less likely anyone notices.

  **The delta is the fix**, and it is the half that protects callers who change nothing: the reply now reads `description 3950 -> 1750 chars (-2200)`, so a shortening is visible at the moment it happens whether or not the caller intended to replace. `append_description` is the second half — text to *add*, blank-line separated, so the caller supplies only what is new. A named parameter rather than an `append=true` flag on `description`, because a flag is forgettable and forgetting it performs a full replace, the exact loss this prevents. Supplying both is **refused** rather than resolved by preference, since silently preferring one is how a caller who meant append loses everything. `_describe_edit` records the same delta in history alongside the preview: the preview alone shows the head of the *old* value and looks identical whether a line was corrected or a third of the text dropped, which is why #1274's loss left no hint in its history entry.

  **The failure mode here is a success message, which dictated how the tests are written** — a test asserting "updated" in the reply would have passed throughout the incident, so the load-bearing assertions are on character counts and the reproduction uses the real 3950 → 1750 magnitude rather than a synthetic pair. AC-3 held without change: the terminal guard runs before any description resolution, so the new parameter is not a route around it, and a closed task's resolution stays structurally unreachable (`TaskBoard.update` has no `resolution` parameter).

  **Deliberately not done, recorded rather than left as an oversight:** `queen_edit_task` does not get `append_description` — the delta is the safety property and belongs everywhere, but the append is ergonomics for the caller who hit the problem, and a second append path would double the surface for a use nobody has reported. No `allow_shrink` refusal gate either, since with the delta visible a shrink is observable and a refusal path would make legitimate rewrites annoying. And no previous-text-in-history: recoverable beats detectable, but storing full prior descriptions is a size question that has not been measured.

## [2026.8.6.17] - 2026-08-06

### Features

### Changes

### Fixes

- **The operator can set a task In Progress, and the whole transition grid is now swept (#1288).** Setting #1255 to In Progress failed because sculpt-studio was working it but had not asserted ACTIVE — diagnosed from one log line, the WARNING that 2026.8.6.14 had added an hour earlier. Root cause: `_apply_status_change` had **no branch where the target was `active`**. The dropdown offered "In Progress" and nothing implemented it — the same selectable-but-unreachable shape as BLOCKED in #1280, one cell over; #1280 fixed its own cell and did not sweep, and this was the cell it missed.

  A new `daemon.mark_task_in_progress` routes through `task_coordinator._activate_with_history` rather than `board.activate`, so **no new `activate()` caller is added** — the property-(f) test pins that count at 2, because a caller that activates without writing history is what made #1159 undiagnosable. The wrapper writes the STARTED row, logs any INV-1 demotions, and fires the Jira export. **Deliberately not `start_task`**, the other route to ACTIVE: that one also sends the task into the worker's PTY, and sculpt-studio was already working #1255, so re-dispatching would paste the prompt a second time. This corrects the board, not the work, and a test asserts nothing reaches `send_to_worker`. ASSIGNED is the only accepted source, because ACTIVE means "this worker is working it" and an ownerless ACTIVE task is a claim about nobody. This does **not** relax worker-asserted ACTIVE (#1282) — that design forbids the *daemon* from inferring which task a worker is on; an operator stating it explicitly is an assertion by the party entitled to make it.

  **The real deliverable is the sweep, because fixing cells one at a time is how this happened twice.** `tests/test_status_transition_matrix.py` enumerates all 42 ordered (current, target) pairs the dropdown can request against an explicit grid — each is supported or expected to refuse, so a behaviour change has to edit the table deliberately — and it reads the option list out of the **markup**, so a new dropdown entry cannot slip past. **It immediately found a third instance**: the dropdown offers `blocked` as a target and nothing reaches it. That one should *not* become settable — blocking requires a reason and the form has nowhere to collect one, and #1287 showed a blocker with an unrecorded cause lands in no operator batch at all — so it stays display-only (the option must exist or a blocked task's own status cannot be shown) and is now a named exemption in the grid with that reasoning, not a loosened assertion.

  **Refusals now say what to do** (#1057). `_unsupported_reason` replaces "X -> Y is not a supported transition" with the resolving fact: selecting Blocked names `swarm_block_on_external`/`swarm_block_on_operator`, In Progress from a non-ASSIGNED status says to assign it first, and → assigned points at the 'Assign to' picker.

## [2026.8.6.16] - 2026-08-06

### Features

- **`swarm_annotate_resolution` — mark a stale or wrong resolution without rewriting it (#1274, partial).** A resolution is not an archived note: it becomes `task.learnings`, and learnings are recalled into future dispatches by `recall_learnings_for_task`, so a stale one is actively **re-served as advice**, carrying a completed task's authority, to a worker with no way to know it aged out. AC-1 was verified before any code against a throwaway closed task on a *copy* of the live DB: `swarm_complete_task`, `swarm_edit_task` and `queen_edit_task` all refuse a closed task, and `queen_save_learning` only appends, so a stale learning could be supplemented but never corrected and a reader saw both with nothing saying which wins.

  **A fifth finding changed the design**: resolutions are immutable *structurally*, not by policy — `TaskBoard.update` does not accept a `resolution` kwarg at all. AC-4 was therefore already satisfied by construction, and its test asserts the **absent parameter** rather than a refusal message someone could later soften. Unlike #1270's HOLD-class gap where the fix was to *allow* an edit, an edit here would be wrong: rewriting a closed resolution destroys the record of what was actually believed and done at the time. So this annotates alongside — two new columns (v17 migration), `board.annotate_resolution`, and the worker-surface verb. `'stale'` (was true, expired) and `'wrong'` (never true) stay distinct all the way to the reader, rendering as NO LONGER TRUE vs WAS NEVER CORRECT; collapsing them would either impugn correct original work or understate a real error.

  **AC-3 is the load-bearing one and it is done at the reader's end, not the writer's**: the caveat renders *inside* the recalled entry rather than after the block, because the failure mode is a reader who takes one entry at face value and a footnote at the bottom is read after the damage. Demonstrated by injecting a known-stale learning of #1174's exact shape and asserting the caveat appears in the block pasted into the next worker's PTY. **Cheaper than the whole feature and applying to every learning**: each recalled learning now carries ", closed YYYY-MM-DD" — #1174's text reached #1267 with no timestamp, so nothing prompted the reader to ask how old the advice was, and a date alone would have prevented the incident.

  **Any worker may annotate any closed task, deliberately**: whoever was just served the bad advice is who discovers it, and gating on ownership would put the correction path behind the worker least likely to be looking — the exact composition trap #1270 documents. Verified rather than assumed that `learnings` is still the live recall path (1062 tasks carry it, the most recent written that day), and the v17 migration was run against a copy of the real 1244-task DB including its WAL, preserving 1244 tasks / 1222 resolutions / 1062 learnings — a migration that only works on fresh DBs is the dangerous kind.

### Changes

- **AC-6 is not done and deliberately not faked.** Annotating #1174 needs two things that were unavailable: the daemon must reload onto v17 before the verb exists (a direct DB write would be clobbered by the running daemon's in-memory board), and *what* made the claim stale could not be established — #1274's text says #1267 found it wrong, but #1267 is about the `--delete-branch` CLI flag closing dependent stacked PRs, a different thing from the repo setting `delete_branch_on_merge`. Writing "no longer true because X" without establishing X would create a stale annotation on day one, the exact failure this feature exists to prevent.

### Fixes

## [2026.8.6.15] - 2026-08-06

### Features

### Changes

### Fixes

- **`block_for_operator` now enters the operator's ask batch (#1287).** The verb *named* for operator blocking called `task.block(reason)` with no `external_ref`, so `external_blocker_ref` stayed empty and `is_awaiting_operator` was false — it produced tasks that were operator-blocked in substance and appeared in **no batch at all**, so the operator was never asked. That is the same failure #1070 created `AWAITING_OPERATOR_REF` to fix, on a different entry point. Both callers match the operator's two cases exactly: `server/proposals.py:368`, an operator-approved park proposal ("me telling the queen"), and `server/daemon.py:1436`, the #762 token-ceiling governor, which already fires a notification but left the task out of the batch ("I need to know").

  **The two are deliberately not kept distinguishable.** #1287 asked whether "the Queen parked a stalled worker" should stay separable from "a worker asked a question"; the operator's reasoning collapses them, since both need the same outcome, which is reaching him. Recorded in the test as well as the task, because the test is where someone would try to re-split them. Volume was measured before deciding, since the objection was that this changes what the operator is shown: 39 TASK_PARKED and 46 PARK_PROPOSED entries across a 1243-task board — a trickle, not a flood.

  **AC-3's audit was answered by measurement rather than by reading**: the behaviour was changed and the full suite run, and exactly two tests failed — both the pinning tests written in 2026.8.6.13 for this specific purpose, one of which asserted the old behaviour precisely so that changing it would have to be a decision rather than a drift. Nothing else in 5770 tests depended on the old value. Both were rewritten, the second replaced by its reverse: an autopark re-labelled to an artifact wait must *leave* the batch, or the operator keeps being asked about something he already settled.

  **A mistake made and caught, recorded because a green run hides it:** rewriting those two tests truncated the file and silently deleted the 7 MCP-surface tests that followed — including the reachability guard that exists because #1268 shipped a board verb with zero callers. The suite still passed, at 13 tests. Caught by comparing the test count before and after (20 → 13) and restored from the committed version.

## [2026.8.6.14] - 2026-08-06

### Features

### Changes

### Fixes

- **Refused status changes are now logged, and the error names the task.** The operator reported that moving #1255 out of blocked failed and he had to go unassigned, then assigned. **The report could not be reproduced and this commit does not claim to fix it** — verified against a *copy* of the live DB on current code: `_apply_status_change(blocked -> assigned)` returns True with the owner preserved; the full `handle_action_edit_task` with his own form payload returns HTTP 200 and moves the task blocked → assigned with the owner kept; `board.unblock` requires only BLOCKED status and no owner; and the JS only sends `status` when it differs from the select's recorded original and only fires the assign action when the worker changed. #1255's history corroborates the report without explaining it — EDITED rows at 12:36:51 and 12:37:08 with no status transition, then ASSIGNED at 12:37:16, which is the workaround he described.

  **So the actual deliverable is diagnosability, not a guess-fix.** The refusal existed only in an HTTP 409 his browser threw away; nothing reached `~/.swarm/swarm.log`, so there was no forensic trace to read. Now the refusal logs at WARNING with the task number and the exact from → to pair (WARNING because operators run at WARNING and this is a forensic anchor rather than a debug aid — #1263's lesson), and the error text names the task number so the toast is self-identifying when several tasks are open. A test asserts the handler both returns 409 **and** logs it, since asserting only the return value would leave the log gap exactly as it was.

  **A hole in the first reproduction, recorded because it nearly stopped the investigation early:** `d.edit_task` was substituted with a `board.update` wrapper, which mocked out the very code that could be raising — the real `task_manager.edit_task` also runs `require_task`, `_describe_edit` and a history append. Driving the real handler still passed, but the first result was worth less than it looked.

## [2026.8.6.13] - 2026-08-06

### Features

- **`swarm_relabel_blocker` — a BLOCKED task can move between its two causes without leaving the hold (#1269).** BLOCKED is reachable either by waiting on an upstream artifact (`block_on_external`) or by an operator ask (`external_blocker_ref == AWAITING_OPERATOR_REF`, which is what `is_awaiting_operator` keys off and what the Queen batches into one set of operator questions). A task whose cause *changed* stayed described by whichever cause was recorded first, so the operator was either asked about something that was no longer his call or never asked at all. Closes the last failing property, (d), from the #1104 audit.

  **One verb in place, deliberately not exit-and-re-enter via #1268's unblock.** Re-entry is not available in both directions — `unblock` lands in ASSIGNED while `block_for_operator` requires ACTIVE — so re-labelling toward an operator ask would need unblock → activate → block, passing through two states the task was never in, minting a spurious STARTED history row and briefly making it the worker's one ACTIVE task. The cause is one field; a verb that rewrites one field is honest about what happened. The binding and `block_reason` move together, because a machine-readable cause disagreeing with its human explanation is worse than either being stale alone, and the history detail names **both** ends (`stopped waiting on platform#234 and became an operator ask`) under a new `SystemAction.TASK_BLOCKER_RELABELLED` — neither TASK_PARKED nor TASK_UNBLOCKED happened.

  Wired to the worker MCP surface rather than left as a board method, and a test asserts it appears in `TOOLS` and `_HANDLERS` — #1268's lesson was that `board.unblock` existed, worked, was tested, and had zero callers. `block_for_operator`'s ACTIVE-only precondition is untouched and asserted in two places, because the tempting implementation relaxes it and that would break the Queen's auto-park semantics where "no longer ACTIVE" legitimately means the stall resolved.

### Changes

- **Two defects caught while building the re-label verb, recorded rather than papered over.** `SystemAction.TASK_BLOCKED` does not exist — the audit helper is wrapped in try/except, so the `AttributeError` would have been swallowed and logged as a warning, shipping a verb whose history row silently never wrote. And `board.block_for_operator`, the verb *named* for operator blocking, calls `task.block(reason)` with no external ref and therefore leaves `is_awaiting_operator` **False**; the sentinel is set only by the worker-facing `swarm_block_on_operator`, so the Queen's auto-park path produces tasks that are operator-blocked in substance but absent from the batch that exists to collect exactly those. Not fixed here — it changes which tasks the Queen surfaces to the operator — but pinned by a test that documents current behaviour and fails if someone changes it.

### Fixes

## [2026.8.6.12] - 2026-08-06

### Features

### Changes

- **A sweep now requires every "re-call" refusal in the MCP surface to name a parameter, value or other verb**, generalising the #1286 fix past its one instance. Getting there took two wrong turns worth recording: a regex over raw source produced 5 false positives (it matched a comment quoting the old bad text, and real messages put `with task_number=<n>` on a following line), and flattening whitespace to fix that made it *worse* — the window bled past the string into surrounding code, which always contains an `=`, so it passed against a deliberately planted bad refusal. It is now built on `ast`, reading string literals as the reader receives them, with comments excluded for free, and verified by asserting the injection **applied** before the control was trusted.

### Fixes

- **`swarm_start_task`'s parked refusal named an action that did not exist (#1286).** Found trying to start #1269: the refusal read "Starting it will un-park it — re-call this to resume it deliberately", and re-calling produced the identical refusal. `_start.py` returned it whenever `target.is_on_hold`, with no confirmation token, no parameter and no state that could make a second call differ — and no code path ever removed the hold tag, so even a caller who got past the refusal would have left a parked task in progress. The task could not be started through the sanctioned verb at all.

  This is worse than #1057's shape, which the module's own docstring cites: #1057 *withheld* the resolving fact, this *stated a false one*, and a caller who trusts it retries forever — an agent caller did exactly that. It is also the third instance of hold-class unreachability on a third verb, after #1270 (edit) and #1281 (assign).

  The fix is an explicit `unpark=true` parameter, chosen over auto-unparking because HOLD exists precisely to stop work starting by accident (#894) — the deliberate step is the feature — and over a bare refusal because the caller then has nowhere to go. Hold tags are cleared *before* `activate`, so what the message promises is what happens, and the parameter is registered in the input schema with an example rather than merely implemented (#1282 was exactly the failure of shipping an undiscoverable capability). **The test is the two-call sequence, not a string check**: asserting the refusal merely *mentions* `unpark` would pass even if `unpark` did nothing — the precise defect being fixed. Also pinned: a bare retry still refuses (persistence is not consent), non-hold tags survive unparking, and an unstarted parked task stays out of `board.available_tasks`.

## [2026.8.6.11] - 2026-08-06

### Features

### Changes

### Fixes

- **The test suite was writing into the operator's production `~/.swarm/swarm.log` (#1285).** Found while trying to establish whether the running daemon had reloaded onto current code — the log was the only usable evidence, since Reload uses `os.execv` and therefore preserves both PID and start time — and it held 3400 lines of test output: `/tmp/pytest-of-*` socket paths and `unittest/mock.py` tracebacks interleaved with real daemon entries, from roughly a dozen full-suite runs.

  The existing guard patched `setup_logging` in two namespaces and cleared the swarm logger **once**, at session start. The leaked lines were INFO, which a WARNING logger cannot emit, so something re-configured logging mid-session; running the offending tests in isolation is completely clean, which is exactly why it survived — no single test file reproduces it, only an ordering within a full session does. The new guard is therefore **behavioural rather than a list of known call sites**: a per-test autouse fixture strips any handler whose `baseFilename` is the production log before and after every test, and `pytest_sessionfinish` names the tests that attached one. `_DEFAULT_LOG_FILE` is redirected as a second layer, since anything reaching the real function with `log_file=None` would otherwise land on the production log.

  **Measured by attribution, not size.** The live daemon appends to the same file continuously, so an earlier size-based check showed +328 bytes that were entirely the daemon's own term-trace and would have read as "still polluting". Counting lines matching `pytest-of`/`unittest`/`mock.py` instead: a full 5743-test run adds 0. The control matters as much as the fix — "no pollution appeared" is equally consistent with the guard working and with nothing having tried to pollute — so a test attaches a real production handler and asserts the guard removes it, with `delay=True` so proving the strip does not itself write to the operator's log.

  The 3400 lines already in the log are **left alone**: it is the operator's forensic record, rewriting it would destroy the evidence this bug existed, and the file is at the 5MB rotation threshold so it will age out on its own. Production logging is untouched — the daemon still writes WARNING+ there, so #1263's observability is intact.

## [2026.8.6.10] - 2026-08-06

### Features

### Changes

### Fixes

- **`TaskBoard.reassign_worker` persisted without notifying, so a worker rename moved every one of its tasks with no event at all (#1275 partial).** The whole path was traced first and found correctly wired — board mutation → `_notify()` → `emit("change")` → `daemon._on_task_board_changed` → `publisher.on_task_board_changed` → `tasks_changed` frame → `refreshTasks()`/`refreshWorkers()` — then every mutating board verb was swept rather than guessed at, and exactly one hole turned up: `reassign_worker` persisted and never notified, while its immediate sibling `unassign_worker` does both, and the `tasks_changed` handler in `dashboard.js` carries a comment *assuming* reassignment fires it. Persisting without notifying is precisely "the change is real and durable and nothing can see it". The notify is conditional on having actually moved something, since every connected dashboard re-fetches on `tasks_changed`.

  **What this is not:** every status transition the edit modal can request already notified, so this is almost certainly *not* the operator's reported cause — his symptom was ordinary status changes and worker rename is rare. #1275 stays open; its AC-2 requires the refresh demonstrated in a browser. Guards ship with the fix: a test asserts the change event fires for all 11 dashboard-reachable verbs (deliberately not "did the board mutate", which would pass on every path that cannot refresh the UI), a sweep fails any future verb calling `_persist()` without `_notify()`, and an end-to-end check drives the real `StatePublisher` and asserts a `tasks_changed` frame arrives — so if staleness persists, the remaining fault is client-side. Both sweeps carry positive controls.

## [2026.8.6.9] - 2026-08-06

### Features

### Changes

- **`TaskBoard.active_tasks` is renamed `assigned_or_active_tasks` (#1284).** The property returned ASSIGNED **or** ACTIVE and its docstring always said so; the name said otherwise, and six operator-visible bugs came from reading the name instead of the docstring — #1277, #1278, #1279, the worker title bar (2026.8.6.6), the idle nudge (2026.8.6.7) and `summary()` reading "4 in progress" with one ACTIVE task (#1283). Each was fixed at its own call site while the generator stayed put; six instances is a naming problem, not six independent mistakes. Verbose on purpose — the test was "could a reader plausibly think it means ACTIVE-only", and a name listing both statuses cannot fail it. `claimed_tasks` was rejected (a BLOCKED task is also "claimed" by its owner while this predicate excludes it, so it invites a *new* misreading) and so was `open_tasks` (`_OPEN_STATUSES` already defines "open" as including BACKLOG and BLOCKED).

  **The predicate is unchanged, which is the point.** IdleWatcher, directives, `poll_dispatcher` and `task_lifecycle` all genuinely need ASSIGNED as well as ACTIVE, and since #1282 made ACTIVE worker-asserted, ASSIGNED is the normal resting state of a dispatched-but-unasserted task — narrowing it to match the old name would have silenced the idle nudge for the common case. The rename was applied longer-token-first with word-boundary anchors (so `_bucket_active_tasks_by_worker` survived) followed by a zero-survivors grep, across 40 files; it was then *proved* mechanical by re-applying it to the original tree and diffing — 34 files are exactly the rename, 3 differ only by parentheses ruff added when the identifier crossed 100 chars, 3 are deliberate. The suite moved 5719 → 5720, the +1 being the new test.

  **Recorded decision: no ACTIVE-only accessor.** Only two callers want in-progress-only work and each is one line; adding `in_progress_tasks` would double the surface a reader must keep straight in a family whose too-similar names just caused six bugs. Write `t.status == TaskStatus.ACTIVE` at the call site. Revisit at four such callers.

### Fixes

## [2026.8.6.8] - 2026-08-06

### Features

### Changes

### Fixes

- **`board.summary()` summed ASSIGNED and ACTIVE into one "in progress" figure (#1283).** Sixth surface of one conflation, and the operator's complaint nearly verbatim: "if it's not set to in progress, assigned just means it's a pending task for that worker." The live board read "4 in progress" with exactly one ACTIVE task; it now reads "1239 tasks: 1 unassigned, 3 queued, 1 in progress, 1234 done" — measured on the live board, and it adds up.

  **Both existing assertions were encoding the bug.** `test_board.py` and `test_tasks.py` each created a task, called `assign()` without activating, and asserted "1 in progress"; read before changing, neither meant ACTIVE, so both now assert "1 queued" plus an explicit "not in progress". A test that asserts the defect is how the defect survives a rewrite. Lanes are now **derived from `TaskStatus`**, one per status: #1279 was BLOCKED having no category at all, and the sum-equals-total test caught it only because a blocked task happened to exist — a status with zero instances would have slipped through. Building the lanes from the enum makes the omission impossible rather than merely detectable. The stray space in "1233 done , 2 blocked", left out of scope by #1279, is fixed by joining parts in one pass instead of appending them pre-punctuated. Negative control: re-lumping ASSIGNED into the in-progress lane fails 3 tests.

## [2026.8.6.7] - 2026-08-06

### Features

- **Workers are now taught `swarm_start_task`, the verb that was shipped and never delivered (#1282).** 2026.8.5.5 made ACTIVE worker-asserted and removed the guessing promoter — then nothing told workers the verb existed. Measured across `src/`, it appeared only in its own arg-type docstring, two comments in `state_tracker.py`, and `swarm_unblock_task`'s success text, while `server/messages.py` appended a block to *every* dispatch teaching the **closing** verb with no counterpart for starting, and all five executable `WORKFLOW_TEMPLATES` ended with a closing step while none opened with marking work in progress. So "tasks stuck in ASSIGNED" was never evidence against the design — it was evidence the mechanism it depends on was undiscoverable. The dispatch block now teaches both ends of the lifecycle (`_COMPLETION_INSTRUCTIONS` → `_LIFECYCLE_INSTRUCTIONS`), the five executable templates gain a leading step, and the existing idle nudge carries the hint.

  **The wording is conditional on purpose.** "Always call this" would move the deleted promoter's inference into the worker rather than removing it — a worker asserting ACTIVE on a task it is not working reproduces #1159 one layer up — so a test asserts the instruction stays scoped to work actually underway, and another asserts it says a freshly dispatched task is already in progress so the call does not read as noise. The OPERATOR template is deliberately excluded and pinned by test: it says DO NOT EXECUTE, and telling a worker to start a task no worker may perform would contradict the only instruction it exists to give.

  **Recorded decision, correcting the filing task's own recommendation:** automatic promotion when unambiguous is *not* restored. `docs/specs/worker-asserted-active.md` already rejects it — "verb preferred, hook as backstop" is two paths to one transition, the thing #1104 exists to audit, and owning exactly one assigned task removes ambiguity about *which* task, not about *whether* the worker is on a task at all. No new `activate()` caller: the property-(f) test still asserts exactly 2, and that it needed no change is the evidence this did not touch the state machine. Accepted limitation, recorded in the spec: a worker may still not comply, which is not fixable by prompt alone — poor compliance **measured** is what would justify revisiting automatic promotion, not assumed.

### Changes

### Fixes

- **The idle nudge stopped misreporting status.** It buckets from `task_board.active_tasks` — ASSIGNED **or** ACTIVE — and called them all "active", the same conflation that put queued tasks in the worker title bar in 2026.8.6.6. They are now called "open".

## [2026.8.6.6] - 2026-08-06

### Features

### Changes

### Fixes

- **The worker title bar named merely-queued work as though the worker were doing it.** Operator-reported: "if it's not set to in progress, assigned just means it's a pending task for that worker." Both display sites built the worker-to-task map from `task_board.active_tasks`, whose docstring is "Tasks currently assigned or in progress" — a claim about what a worker is doing right now, derived from a fact about what it has been given. Measured at the time of the report: 4 ASSIGNED and 0 ACTIVE on the live board, so every title shown was pending and none was in progress.

  `active_tasks` is deliberately **not** narrowed — IdleWatcher and the directive drone both need ASSIGNED as well as ACTIVE, since a worker with queued work is not idle-with-nothing-to-do, so narrowing the predicate would have broken nudge logic to fix a label. A test pins that it still includes ASSIGNED; the conflation was in the display, not the predicate. Both sites had the same four lines duplicated and now share `_worker_task_titles` in `web/app.py`, because fixing one and missing the other would have left the initial page render and every subsequent htmx swap disagreeing — a discrepancy that reads as a flicker rather than a bug, so nobody files it.

  This also answers the operator's related question about task progression: exactly two callers set ACTIVE — `swarm_start_task` (the worker asserting its own start) and `task_coordinator.start_task` (dispatch) — so a task that is merely queued correctly stays ASSIGNED, and "only sometimes" was the display treating those two as one thing. One real gap is left unfixed and filed separately: `swarm_start_task` appears in no worker prompt anywhere in the tree, so a worker resuming parked work has nothing telling it to assert ACTIVE.

## [2026.8.6.5] - 2026-08-06

### Features

### Changes

- **Every exit from BLOCKED now delegates to one `_leave_blocked` helper** inside `_apply_status_change`, so the #529 blocker-row obligation lives in a single place instead of being repeated per target and forgotten on one. The per-target version had already grown three copies of the same two lines, and adding the backlog target tripped the complexity gate — which was the right signal rather than something to squeeze past.

### Fixes

- **A task could not be parked back to BACKLOG (operator-reported).** "When I change a task to backlog I get an 'error' that says it saved but it doesn't save." The error text was accurate — the transition genuinely was unsupported — but the real gap is that BACKLOG had **no way in** for an open task: it was entered only by task creation and by `reopen()`. Every open lane could be promoted *out* of backlog via `approve()`; none could be parked back, and parking something as not-ready is an ordinary operator action. This was silent before 2026.8.6.3 added the 409, which is why it went unnoticed — the status simply did not change while the modal said "Task updated". The 409 did not cause the defect, it revealed it.

  `SwarmTask.demote_to_backlog()` and `TaskBoard.demote_to_backlog()` are the missing inverse of `approve()`. They drop the owner, as `reopen()` does, because leaving a parked task owned would claim a worker holds work that is out of play. **Safe with respect to dispatch by construction**: BACKLOG is excluded from `is_available` (only UNASSIGNED qualifies), so the transition can only ever make a task *less* dispatchable — asserted rather than assumed. DONE/FAILED are still refused and routed through `reopen_task`, which also clears the resolution; sending them here would park a task with a completed resolution still attached, and a test pins that the resolution survives.

## [2026.8.6.4] - 2026-08-06

### Features

- **`swarm_edit_task` can now reach HOLD tasks, which no worker could ever correct (#1270).** Follow-up 3 of 3 from the #1104 audit, closing failing property (g): a precondition that is structurally unsatisfiable for a whole task *class* is the same defect as a missing verb. Neither verb was individually wrong, which is why auditing either alone never surfaced it — HOLD tasks are UNASSIGNED by design (that is what stops the auto-assign drone, #894, after a HOLD item was auto-dispatched to wifi-portal), and `swarm_edit_task` required assignment because it exists so a worker corrects its *own* task. Composed, no worker could correct any HOLD task's description; verified live on 2 of 2 attempted (#1104, #1018).

  The HOLD class is exempted from the ownership rule, because for this class the rule has no owner to protect — an unassigned HOLD task has no other worker whose work could be rewritten, so the check was guarding nothing and only blocking. A separate verb was rejected (doubles discovery cost and invites drift between two implementations of one correction path) and so was routing through the Queen (the status quo, a round trip per correction, and it leaves the asymmetry that made #1270 itself uneditable by its owner).

  **Two edits, and the second is the one that would have been missed:** the unassigned refusal is now conditional on "not on hold", but the owner-match branch below it compares `assigned_worker` against the caller, and an unassigned HOLD task has no `assigned_worker` — so it would have re-closed the class through the next branch down and the fix would have *looked* applied while changing nothing. Editing does not adopt: the task stays UNASSIGNED, keeps its hold tag, and stays out of `board.available_tasks`, asserted directly, because "can edit" must not leak into "can start". The tool description documents the exception — a capability the model cannot discover is not reachable either — and the exemption is asserted to key on `is_on_hold` rather than an incidental condition, since this exact gap already recurred on a second verb (#1281).

### Changes

### Fixes

## [2026.8.6.3] - 2026-08-06

### Features

### Changes

### Fixes

- **The operator can move a task out of BLOCKED from the dashboard (#1280).** "I see 5 blocked tasks but no way to change their status from blocked." Three layers, each independently sufficient: the `#tm-status` select offered no "blocked" option, so a blocked task's status could not even be represented (`selectedIndex=-1`, an empty value submitted, and the `if new_status` guard skipped the change); `_apply_status_change` had no branch whose *current* status was blocked, so any target chosen fell through and did nothing; and `handle_action_edit_task` returned `{"status": "updated"}` regardless, so the no-op reported success and the UI said "Task updated" — #1159's shape, where a verb that succeeds and does nothing is worse than one that refuses, because the caller stops looking.

  Fixed by adding the option, wiring blocked→assigned via `board.unblock` (owner-preserving per #1268; `release` would drop the owner) and blocked→unassigned via `board.release`, returning a bool, and answering **409** with what did and did not happen. blocked→done stays unsupported: that is `force_complete`, recording a completion for work that is still open. Leaving BLOCKED clears the `BlockerStore` rows (#529) — the board has no handle on that store, so the caller owns them, and skipping it would have reproduced #529 on this surface only. This is the #1104 audit's property (b) on the one surface the audit never covered: #1268 wired the owner-preserving exit to the worker and Queen MCP surfaces and the operator's own surface got neither.

- **A HOLD task could not be assigned by the operator, because the drone's own guard was answering the wrong question (#1281).** Assigning #1270 failed with "not available (unassigned)". `task.is_available` means "the **auto-assign drone** may take this" and #894 excludes HOLD from it — but both `TaskCoordinator.assign_task` and `TaskBoard.assign` used that predicate to answer a different question, may this caller route it *deliberately*, so the mechanism that stops the drone also stopped the operator and HOLD became a trap. Two layers, which is why a one-line fix would have looked right and still failed; a test driving the real board caught it where the mock-only version passed against a fix that did not work.

  `override_hold` is threaded `board.assign` → coordinator → daemon and passed `True` only from the operator's `/action/task/assign` route. **Opt-in on purpose**: defaulting it `True` would hand the capability to the Queen's directive path, the proposal coordinator and the worker create-with-target path, trading a routing defect for the auto-dispatch of parked work that #894 exists to prevent — a test pins the default at `False`. BLOCKED had no normalisation branch either, so reassigning a blocked task 409'd identically; added. Verified against a **copy** of the live DB using the real tasks: #1270 refused under the old gate and allowed with the override, #915 unblocked with owner `platform` preserved and no completion recorded, and a fresh HOLD task re-checked in the same run is still absent from `available_tasks` — #894 intact. The irony is load-bearing: the task he could not assign was #1270, whose subject is HOLD tasks being unreachable because a precondition is unsatisfiable for the whole class.

## [2026.8.6.2] - 2026-08-06

### Features

### Changes

### Fixes

- **`board.summary()` counted BLOCKED tasks in `total` and in no category (#1279).** Third defect of the same class as #1277/#1278, on a third surface, and the one that produced the number the operator actually quoted: the string did not add up, and the dashboard renders it verbatim into `#task-summary`, so he was reading a count that silently excluded blocked work. This reconciles his "I see 6" — reconstructed from `task_history`, 1233 tasks existed, 9 were open, and `summary()` counted 6 because the three BLOCKED were counted nowhere. The Queen's arithmetic 9 − 3 = 6 was the correct explanation rather than the coincidence it was first called; her mechanism was right in substance and only the location she named was wrong, and killing the whole lead threw away the sound half. Her "three blocked" was also not stale — the board moved between her look and the measurement.

  Guarded by a **completeness invariant** — the category counts must sum to `total` — rather than an assertion that the word "blocked" appears. A blocked-specific check would pass while the next status added without a category vanished identically, and a status with no category is how both this and the missing filter chip happened. The test drives each status through the real board verbs and carries a positive control asserting a blocked task is actually present, so it cannot pass by testing nothing. The new part is conditional, matching backlog and failed, so a board with nothing blocked reads exactly as before. Counting a blocked task does not make it dispatchable (#1270).

## [2026.8.6] - 2026-08-06

### Features

### Changes

### Fixes

- **The default task view silently truncated 1000 of 1235 tasks, discarding exactly the newest open work (#1277).** `_paginate` keeps the first `MAX_QUERY_LIMIT` items and `board.all_tasks` sorts by (priority, created_at) **ascending**; with 1226 of 1235 tasks DONE those two combined to drop #1275, #1274, #1270, #1269 and blocked #1255 out of the rendered window entirely. `_paginate`'s own docstring records this shipping once before at limit 100, and the fix then was to raise the ceiling to 1000 — which recurred the moment the board crossed 1000. So the ceiling is **not** raised again. A new `_display_sort` (`server/helpers.py`) orders unfinished work before finished work, then priority, then newest first, applied *before* paginating, so a ceiling can only ever discard completed tasks. The sort lives in the web layer and is display-only: `all_tasks` has ~40 call sites and reordering it would change which task a worker picks up — visible must not become startable (#1270). `handle_partial_tasks` already computed `task_total` and `task_has_more` and the template used neither, so dropping 235 tasks looked identical to having 1000; it now renders "Showing N of M". A truncation nobody can observe is the defect; the limit's value is not.

- **No BLOCKED filter chip existed, so blocked tasks were reachable only under "All" (#1278).** The chips send `status=<csv>` to the server and the selection persists in `localStorage`, so any operator who had ever picked a subset carried a durable filter in which no blocked task could appear — and composed with #1277's truncation under "All", #1255 was visible in no reachable filter state at all. Neither defect was an edit-permission problem: `task_manager.edit_task` has no status or assignment guard and `task_list.html` has no `show=false` rule for the edit action, so the Edit button renders for every status. The rows were simply never rendered. Verified by rendering the real template against the live 1235-task board; negative control: un-wiring `_display_sort` drops exactly the five tasks the operator named. Killed lead recorded: `queen_view_task_board`'s "open" filter was **not** the cause — `_OPEN_STATUSES` does include blocked, only its tool description is wrong, and the dashboard never uses an "open" concept at all.

## [2026.8.5.8] - 2026-08-05

### Features

- **`swarm_unblock_task` and `queen_unblock_task` — an owner-preserving exit from BLOCKED, on both surfaces (#1268).** A blocked task returns to ASSIGNED and *stays with the same worker*, so "the thing I waited for happened, I'm resuming" is now expressible. `board.unblock` already did the right transition and had zero callers; both verbs wire it up and share one helper for the audit trail and blocker-row clearing, so the two surfaces cannot drift. Refusals name what would resolve them and mutate nothing; success text quotes the status **read back** from the board rather than the transition requested. Blocker rows are cleared with `clear_for_task` rather than the per-worker `clear`, because a blocked task can carry rows from several workers and a leftover row is what kept the IdleWatcher nudging (#529).

### Changes

- **Correction to the #1104 audit's headline, which was wrong.** It claimed BLOCKED's only non-falsifying exit was dead code. In fact `board.release` accepts BLOCKED and the Queen has always reached it via `queen_reassign_task` — so the operator was never stuck. The error came from an extraction script that listed `release`'s *refusal* set in its *accepts* column; the same script had already misled me about `park`, and the audit's own claim that every interesting cell had been read was false when written. The two gaps that were real — no worker-surface exit, and no owner-preserving exit from either surface — are what #1268 closes. `docs/specs/taskboard-state-machine-audit.md` now carries the correction, and the reachability test's `xfail` is removed rather than weakened (0 xfailed).

- **`queen_reassign_task`'s description no longer contradicts its own implementation.** It claimed a BLOCKED task "must be unblocked first"; the code releases first and `release` accepts any holdable status. That text is what sent a worker chasing a path that did not exist (#1237). It now states that reassign *does* move a blocked task but **drops the owner**, and points at `queen_unblock_task` for the owner-preserving case. `board.py:599` was accused of the same fault and was in fact **correct** — left unchanged.

### Fixes

## [2026.8.5.7] - 2026-08-05

### Features

### Changes

- **TaskBoard's state machine is audited, and the audit is now guarded by a test (#1104).** All 7 `TaskStatus` members have their entering and leaving verbs enumerated and assessed against seven properties; `docs/specs/taskboard-state-machine-audit.md` carries the table and `tests/test_status_exit_reachability.py` keeps it honest.

  **The headline finding: BLOCKED's only non-falsifying exit is dead code.** `TaskBoard.unblock` exists, works, and is covered by `tests/test_board.py` — and is called by *nothing* in `src/`. The only exit any surface can invoke is `force_complete`, which records DONE for work that is still open. Two comments in the codebase (`board.py:599`, `queen_handlers/_tasks.py:45`) instruct the reader to unblock a task, which no surface permits.

  Why every prior test missed it: unit tests on the board prove the *transition*. Reachability is a different property and no board-level test can see it. The board tests pass; the verb is unreachable — the same shape as a green check measuring nothing. The new test therefore asserts **reachability**, carries a positive control (it proves it can see verbs known to be wired up before any absence it reports is trusted), and marks the known gap `xfail(strict=True)` so that a new status with no exit fails immediately *and* fixing BLOCKED also fails, forcing the marker off rather than letting the gap close silently.

  Results: (a) non-empty, (e) no racy gate and (f) no silent undo all **pass** — (f) only as of the sibling worker-asserted-ACTIVE change, which removed the one `activate()` caller that wrote no history. (b), (d) and (g) **fail**, filed as #1268, #1269 and #1270. `board.block_for_operator`'s ACTIVE-only precondition is confirmed **correct** and pinned by test, as is `park`'s relaxed `(ACTIVE, ASSIGNED)` precondition — narrowing it would re-open the INV-2 race.

### Fixes

## [2026.8.5.6] - 2026-08-05

### Features

- **`POST /api/notifications` — external tools can raise an operator notification without writing `buzz_log` directly (#1265).** `GET /api/notifications` was history-only and `/api/hooks/event` only speaks Claude Code lifecycle events, so the credential-check cron had no option but to `INSERT` into `buzz_log` itself. It did, and documented the coupling in the script rather than hiding it, filing this endpoint as the follow-up.

  **The endpoint deliberately does exactly one thing**: appends a drone-log entry with `is_notification=True`. It does *not* also call `push_notification` — `StatePublisher` already fans notification-worthy entries out to the WebSocket, so doing both would deliver every external notification twice. Using the same single entry point is what makes an external notification indistinguishable from an internal one on the dashboard rather than merely similar-looking, and a test asserts (via AST, not a substring search) that the handler never calls the fan-out directly.

  The caller's label travels in `metadata["label"]`, not as a new `SystemAction` member. That enum drives routing, filtering and priority mapping, so letting callers invent members would silently break every consumer that switches on it — one closed `EXTERNAL_NOTIFICATION` member covers them all.

  **Failure mode, stated because a notifier that fails quietly is worse than none:** a rejected request returns 4xx with a reason and records nothing; a caller that cannot reach the daemon gets a connection error. In both cases the failure is the *caller's* to surface — the daemon cannot notify you that it failed to notify you. Callers that must not lose the signal should treat non-2xx as a hard failure (`curl --fail`) rather than best-effort.

### Changes

### Fixes

## [2026.8.5.5] - 2026-08-05

### Features

- **`swarm_start_task` — a worker now declares which task it is working on.** Previously `ACTIVE` was *inferred by the daemon and never asserted by the worker*: `start_task` (dispatch) and `WorkerStateTracker._promote_one_assigned` both reached `TaskBoard.activate`, and neither is the worker. The promoter picked the **most-recently-updated ASSIGNED** task on a `RESTING→BUZZING` transition, so the board could say a worker was on task B while it was on A — the operator's "multiple tasks crashing", and the mechanism behind #1159 (`park` stamps `updated_at`, so the just-set-down task sorted first and was re-activated seconds later). The verb refuses rather than guessing — another worker's task, a blocked or closed task, an ambiguous queue, or a worker that already has something in progress — and every refusal names what would resolve it (#1057). Spec: `docs/specs/worker-asserted-active.md`.

  The pre-existing machinery was never the gap: `activate` was already the single chokepoint, `_assert_no_double_active` self-heals double-ACTIVE at persist, and two reconcilers run — all enforcing *at most one* ACTIVE per worker. None of it could know *which one is right*, because the only party that knows was never asked.

### Changes

- **The daemon no longer infers `ACTIVE` from PTY activity.** `_promote_one_assigned` activates nothing. Going BUZZING is evidence a worker is doing *something*; it was never evidence of *which task*. A dispatched-but-unasserted task therefore stays ASSIGNED — and the board reading "queued" is **true**, which is the property that was missing. Deliberately **no nudge and no timed fallback**: a grace-period auto-activate would restore exactly the inference being removed, just on a delay, and would pass every positive test while doing so.

- **The auto-dispatch chain no longer interrupts an operator conversation.** A worker the operator is actively working with is BUZZING but unavailable, and pushing a task prompt into that PTY interleaves with the operator's turn. Such a task stays ASSIGNED and queued *on that worker* — it keeps ownership — and the next idle transition picks it up. Only the automatic chain is gated; an operator explicitly starting a task still goes straight through. This needed a new threshold rather than the existing `is_user_active`, whose 2-second window answers a different question ("would writing now collide with a keystroke?") and reports *available* mid-conversation because humans pause longer than that between sentences. `is_operator_engaged` reads the same two inputs with a 5-minute window, so there is no new state to keep in sync and nothing to get stuck — it clears when the terminal detaches.

### Fixes

## [2026.8.5.4] - 2026-08-05

### Features

### Changes

- **Shared-config sync outcomes are now visible at the log level the daemon actually runs at (#1263).** Every non-failure outcome was INFO while `log_level` defaults to WARNING in four independent places, so a healthy sync, an already-current skip, a dev-mode skip and a sync that was never invoked at all were *all* silence in `swarm.log`. Failures were WARNING and stayed visible, so this was never the exit-126 defect rebuilt — what was missing was positive confirmation that the distribution path was alive. There is now one WARNING-level summary line per invocation naming each repo's outcome (`claude-team-config=installed; codex-team-config=already-current`), and `_sync_one_shared_config` returns an outcome token rather than a bool, because a bool cannot distinguish "already current" from "never ran" — the exact distinction at issue.

  WARNING for a healthy outcome is a deliberate abuse of the level, taken because the alternative is unobservability at the level this fleet deploys at. Once per daemon start is not spam. **The daemon's own dev-mode early return was also raised from DEBUG**: it fires *before* `sync_team_config()`, so raising the inner outcomes alone fixed nothing on a dev box — the summary line was unreachable there.

### Fixes

- **Copy and paste now work in the operator shell.** Loading `ClipboardAddon` is not sufficient, and the shell shipped without the two pieces that actually matter. Ctrl/Cmd+V now returns `false` from a custom key handler so it never reaches xterm — which would send raw `0x16` to the PTY (readline's quoted-insert) instead of pasting — and a capture-phase `paste` listener on the terminal textarea `stopPropagation()`s, because a document-level paste handler for email import would otherwise consume the event first. Copy is Cmd+C or Ctrl+Shift+C. **Plain Ctrl+C is deliberately left as SIGINT**: making it copy whenever text happens to be selected would remove the only way to interrupt a runaway command, and a stale selection is exactly the state an operator forgets about.

## [2026.8.5.3] - 2026-08-05

### Features

### Changes

- **Worker state is legible in light mode without competing with the selection cue.** Colour alone is too weak to scan down a long worker list on a light background, and tinting the row fought with the selected-worker highlight. State now gets its own channel: a right-edge rail (selection keeps the left), a quiet background tint applied only to unselected rows, and a bordered pill on the state label. Sleeping stays deliberately neutral so the states that need attention are the ones that draw it.

### Fixes

## [2026.8.5.2] - 2026-08-05

### Features

### Changes

- **The worker state filter chips are now multi-select.** They behaved as radio buttons, so the only expressible answers were "one state" or "all of them" — "everything except Sleeping", the obvious thing to want on a board where most workers are asleep, was unreachable. Chips now toggle independently and combine. "All" is a *mode* represented by the selection set being **empty**, not a sixth member mixed in among the real states: a sentinel in the same set forces every read to special-case it, and the one read that forgets silently filters every worker out. Deselecting the last chip therefore lands back on "All" rather than on a filter that matches nothing. Chips carry `aria-pressed` and the group carries `role="group"` — with more than one selectable, pressed state is the only thing conveying to assistive tech that several filters are live.

### Fixes

- **A Queen notification no longer hijacks the viewport.** Every proposal opened the bottom panel, even mid-read on another tab. The `proposal_created` handler called `switchTab('decisions')` beneath a comment reading *"Flash the Decisions badge so users notice even if not on that tab"* — the comment described the intent, the code did something much larger. `switchTab()` exits focus mode, expands a collapsed bottom panel, changes the active tab **and** persists it to `sessionStorage`, so an event arriving at the Queen's convenience relocated the operator and kept them relocated across a reload. It now does what the comment always said: flashes the badge, honouring `prefers-reduced-motion` (the count is the real signal; the flash is only the cue that it changed). The sibling Queen events are covered by the same rule, and a test asserts none of them call `switchTab` — user-initiated switches, such as opening a linked task, are unaffected and still correct.

## [2026.8.5] - 2026-08-05

### Features

### Changes

### Fixes

- **The bundled `/swarm-*` command docs and the `swarm-coordinate` skill no longer fail markdownlint**, so repos that lint their markdown recursively stop inheriting the failures. Six bare ` ``` ` fences gained a `text` language tag (MD040) and a blank line now precedes the list in `swarm-status.md` (MD032) — seven single-line changes, nothing else. These files are packaged under `src/swarm/hooks/{commands,skills}/` and written into every worker's `.claude/` by `install_worker_commands()` / `install_worker_skills()`, both of which overwrite unconditionally (`copy2`; `rmtree` + `copytree`). That is why a consuming repo could fix them and not stay fixed: the next install wrote the bundled copies straight back over the fix. Fixing the source is therefore the only fix that holds, and it reaches every consumer on their next install.

  Scoped deliberately to the two rules that gate anything. MD013 (line length) and MD041 (first-line H1) also fire under markdownlint's defaults but are disabled in the consuming repo's `.markdownlint.json`, so no line rewraps or added headings appear in the diff.

  Blast radius was larger than the single repo that reported it: 26 checkouts on this box carry these files, 6 of them with the files tracked in git (`rcg-dev-install`, `d365-solutions`, `bfg-solutions`, `budgetbug`, `sculpt-studio`, `sillytavern`). Repos that lint only `docs/**` were carrying the same broken files silently.

## [2026.8.4.6] - 2026-08-04

### Features

- **The shared-config sync now covers codex-team-config as well as claude-team-config**, through the same registry and therefore under the same dev-mode and already-current gates by construction rather than as a parallel implementation that can drift. **Recorded decision: the codex path installs what is committed and does NOT run `scripts/sync-from-claude.sh`.** That script regenerates `AGENTS.md` as `cat AGENTS.header.md <claude-team-config>/CLAUDE.md > AGENTS.md`, so it reads whatever branch claude-team-config happens to be on — on a box running the prompt-ablation experiment it would rewrite `AGENTS.md` from the cut CLAUDE.md and push the experiment into the codex config, a successful-looking sync that corrupts what it distributes. Distributing config and authoring it are different jobs; regeneration belongs with a human on a known branch.

### Changes

- **The shared-config sync skips repos that are already current**, keyed on a `branch:HEAD:origin/main` fingerprint persisted to `~/.swarm/shared-config-state.json`. The branch is part of the fingerprint deliberately, so checking out a different branch invalidates a fingerprint recorded on another one and the install that makes the switch take effect still runs. An unreadable repo yields an empty fingerprint, which never compares equal to a stored value — otherwise a broken git would look "already current" forever and silently stop updating.

- **The sync is skipped in development mode**, gated on `update.dev_mode_active()` — the `SWARM_DEV` env var **or** `_is_dev_install()`. The env var alone was the old gate and is set nowhere on this box, including the daemon started from the project `.venv`, so the sync had been running against developers' working checkouts. The gate lives in `sync_team_config` itself as well as at the daemon call site, so a future caller cannot route around it.

### Fixes

- **The shared-config sync had never been observed to succeed — it exited 126 every time.** `sync_team_config` invoked `install.sh` directly, but both config repos commit it as mode **100644**: not executable, in git, in every clone. Direct invocation exits 126 ("found, not executable"). Measured on the swarm box: 9 × `team config install.sh exited 126` across ~31 h. Developers received the shared config at clone time only, because `.rcg.yaml`'s post_clone hook says `bash install.sh --yes` — an explicit interpreter, which works. The fix is in the caller (`yes | bash <installer>`) rather than a mode bit every clone must carry.

  **A second, quieter defect made the first one unmeasurable.** Failure logged at WARNING; success logged at DEBUG — and the daemon runs at `log_level = WARNING`, so a success could never appear in `swarm.log` at all. The reported "9 failures, 0 successes" overstates what that log can prove: the zero was a grep for a line that could not have been written either way, so "synced fine" and "never ran" produced identical evidence. Outcomes are now logged symmetrically — installed / already-current / skipped at INFO, failures still at WARNING — and a test asserts the success line clears INFO so the asymmetry cannot silently return.

  Verified against the real repos, not mocks: both installers ran to exit 0 and logged `team config sync complete` (`2026-08-04 22:11:19` claude, `22:11:21` codex), a second run skipped both with zero installer invocations, and with claude-team-config checked out on the non-`main` `ablation` branch the branch, HEAD, working tree and every installed artifact were byte-identical before and after. The sync runs only `rev-parse` and `fetch` — read-only with respect to HEAD, branch and working tree — and a test asserts no branch-mutating git verb appears anywhere in the sync path.

## [2026.8.4.5] - 2026-08-04

### Features

### Changes

- **Killing a worker now shuts the agent down gracefully instead of signalling it cold.** Esc to interrupt any running turn, then the provider's quit command (`/quit` on Claude Code) so the agent can save its session, then `exit` to close the login shell `shell_wrap` leaves behind. The existing SIGTERM/SIGKILL still runs unconditionally afterwards — graceful is an *attempt*, and a wedged agent that ignores its quit command must not survive a kill, or "graceful" would be a regression. The whole budget is ~3 s: kill is an interactive action, and a long wait reads as the click not having worked. `LLMProvider.quit_command()` returns `""` by default rather than guessing — a wrong quit string gets typed into the prompt as literal text and left there, so providers whose command isn't known simply skip that step.

### Fixes

- **A killed worker stayed killed.** Killing a worker often took several attempts, and the log says why:

  ```
  2026-08-03 16:16:44 | rcg-dev-install | OPERATOR | killed
  2026-08-03 16:16:59 | rcg-dev-install | REVIVED  | worker exited
  ```

  Fifteen seconds. Three independent same-worker instances (`rcg-dev-install`, `sculpt-studio-codex`, `bfg-solutions`). `kill` marked the worker `STUNG` and left it in the roster; the drone decision rule revives **any** STUNG worker — correct and wanted for a crash, exactly wrong for a deliberate kill — and had no way to tell the two apart. With `max_revive_attempts = 3` the operator had to kill up to four times before `revive_count` exhausted the budget, and fewer if earlier crashes had already spent some of it, which is why it felt intermittent rather than reliably broken.

  **The fix is an ordering, not a flag:** the worker leaves the roster *before* any shutdown step runs. The revive rule only ever sees workers in the roster, so there is nothing left to revive — no flag for a future edit to forget to check, and no window during the ~3 s graceful sequence for a drone poll to land in. Crash recovery is deliberately untouched: a worker that dies on its own is still in the roster, still marked STUNG, still revived. Manual revive still works too — the `#1187` respawn path already handled an absent-from-roster worker, and is now the normal route back rather than a defensive one.

  Not the cause, and worth recording since it looked like the obvious suspect: the holder's kill is sound. Reproduced against a real holder with a child that ignores SIGTERM — the whole process group was still reaped, because closing the PTY master sends SIGHUP.

- **The kill toast no longer claims success on failure.** `killWorker()` reported `Killed <name>` from a bare `.then()` with no status check, so a failed kill looked exactly like a successful one and the operator's only clue was the worker still sitting there — which reads as "the click didn't register" and invites clicking again. It now checks the response, and reports success without the error styling it was previously using for both outcomes.

## [2026.8.4.4] - 2026-08-04

### Features

- **Modern Hive colour system.** The dashboard, configuration, login, offline,
  and PWA surfaces now share accessible light and dark palettes, with a
  persistent System / Light / Dark control that follows OS changes without a
  flash of the wrong theme. Embedded terminals update their palette live when
  the mode changes, without reconnecting or losing scrollback.

### Changes

- UI typography, panel hierarchy, controls, focus indicators, status colours,
  and minimum text sizes now target WCAG 2.1 AA readability across desktop and
  mobile layouts.

### Fixes

- Light-mode terminal ANSI colours no longer wash out against the white
  background. Every configured foreground meets at least 4.5:1 contrast, and
  xterm enforces the same floor in light mode while leaving dark-mode rendering
  unchanged.

## [2026.8.4.3] - 2026-08-04

### Features

### Changes

### Fixes

- **Open shell now works.** Every click failed with an internal server error: `Spawn failed: invalid worker name: 'shell:swarm'`. The session-name prefix was `shell:`, chosen *because* `:` cannot appear in a worker name and so could never collide with one — and `:` is precisely what the holder forbids. It validates every spawn name against `[a-zA-Z0-9_-]+` and rejects the rest outright, so the property that made the prefix safe on the daemon side made it impossible on the holder side. Prefix is now `swarm_shell_`.

  **Nine unit tests passed over a name production can never spawn**, because they ran against a fake pool that validated nothing — the double encoded an assumption about the boundary rather than the boundary's rule, and a double more permissive than the real thing cannot fail the way production fails. The fake now enforces the holder's actual preconditions (name regex *and* absolute-cwd), importing `WORKER_NAME_RE` from the holder's own command handler so the two cannot drift; that regex was extracted from an inline literal in `_cmd_spawn` for exactly this reason. Verified end-to-end against a real holder on a throwaway socket: the old name is rejected with the production error, the new one spawns a live bash in the worker's directory.

  A legal prefix shares the worker-name charset, so unlike `:` it *could* collide. `WorkerService.discover` therefore defers to configuration — a configured worker always wins over the prefix heuristic. Without that, a worker named `swarm_shell_*` would be silently dropped from the roster, and an absent worker reports as nothing at all.

## [2026.8.4.2] - 2026-08-04

### Features

### Changes

### Fixes

- **The mobile composer's Send now actually submits.** Tapping Send delivered the text into the worker's input box and left it sitting there — the operator still had to reach into the terminal and press Enter, which is most of what the composer exists to avoid. Cause: the composer wrote `text + '\r'` as a **single** WebSocket frame. `WorkerProcess.send_keys` (`pty/process.py`) has always split text and Enter into separate writes with a 50 ms drain between them, precisely so an interactive TUI has time to process the input before the carriage return arrives; delivered in one chunk, Claude Code takes `text\r` as pasted content carrying a newline and does not treat it as submit. Every server-side send already honoured that rule — this WebSocket path was the only caller that concatenated, which is why the same text submitted fine when sent through `/action/send/`. The composer now mirrors the backend, and captures the socket in a local first: the second write is deferred, and `inlineTermWs` is an alias that gets repointed on worker switch and terminal reconnect, so re-reading it in the callback could deliver the Enter to a different worker's PTY.

## [2026.8.4] - 2026-08-04

### Features

- **Open a bash shell in a worker's folder from its right-click menu.** Running a local command where a worker lives previously meant leaving the dashboard, or typing into the agent's own PTY — which interleaves with whatever turn it is running. "Open shell" spawns a login shell rooted in the worker's directory and attaches the existing web terminal to it in its own modal window. Sessions are ephemeral: closing the window kills bash, so a shell can never outlive the UI showing it and become an orphan nobody can see or reap (the tradeoff being that a long-running command dies with the window). Closing a worker's shell also happens automatically when the worker is killed, since a shell is only reachable through its worker's menu.

  **A shell is deliberately not a `Worker`, and that is the load-bearing part.** It is spawned into the same process pool — that is where PTYs live and the terminal already knows how to attach to one — but the pool is a flat namespace shared with real workers and is *not* the boundary it looks like: `WorkerService.discover` wraps **every** process the holder reports in a `Worker`. A shell left visible to it becomes a worker on the next reconcile: present in the sidebar, eligible for task assignment, polled by drones, its bash prompt classified into BUZZING/RESTING, nudged by the IdleWatcher. A task handed to a bash prompt is lost silently — nothing is running that could execute it. Two things prevent that and have to agree: every session name carries a `shell:` prefix, and `discover` skips names matching it. The terminal bridge consults the shell registry only for prefix-matching names, so an unknown worker still takes the 404 path rather than a lookup that happens to also return `None`.

### Changes

- **Sleep is now offered from any live state, not just RESTING.** Parking a busy worker took two trips through the context menu — *Force to rest*, then right-click again for *Sleep*. The two-step existed because sleep alone does not stick: SLEEPING is a *display* state (RESTING plus a backdated `state_since`), and the state tracker re-reads the PTY on its next tick — if the PTY still shows an active turn or an approval prompt, it re-detects that and the worker leaves SLEEPING again. *Force to rest* was doing the load-bearing half by sending Escape, which is what actually changes what the PTY shows. So the fix is not a looser state check — that would produce a menu item that appears to work and silently undoes itself seconds later, which is worse than two clicks. `sleep_worker` now sends the Escape itself, and only when there is a turn or prompt to interrupt (an already-RESTING worker sits at an idle prompt the operator may be mid-thought in, where Escape buys nothing). STUNG is still refused: the process has exited, and rendering a dead worker as SLEEPING files it under a state that reads as idle-and-fine.

### Fixes

## [2026.8.2.6] - 2026-08-02

### Features

- **A reload that loaded uncommitted code is now diagnosable after the fact.** Dev-mode Reload re-execs the daemon, which imports `src/swarm/` from the **working tree** — that is the point of the button (edit, Reload, test) and it is unchanged. The hazard is that in this fleet the editor is usually the *swarm worker*, and the operator has no visible connection between "I clicked Reload" and "a worker is mid-refactor". Measured 2026-08-02: a Reload at ~15:06Z picked up a half-finished #1195 and worker creation began failing with a `TypeError` naming a function signature that existed **in no commit**; `origin/main` was self-consistent throughout, so the natural first read — "the thing that just shipped is broken" — pointed at innocent code, and the source on disk had already moved on. The daemon now determines at **startup** whether the code it actually imported was committed, and when it was not, writes a `SOURCE_TREE_DIRTY` buzz entry **naming the files** (an investigator needs to know which subsystem to suspect, and by then the tree has changed again). `/api/health` gains `source_checked` / `source_dirty` / `source_dirty_files` so the question is answerable live rather than only by grepping a log. Silent on a clean tree: Reload is routine, and a line every time would train the operator to skip the one that matters.

  Recorded at **startup rather than at restart-request time** deliberately — request time captures *intent*, and the tree can change between the click and the `os.execv`; only the process that imported the modules can say what got loaded. Two things this could not rest on, both measured: `os.execv` preserves PID **and** process start time (`ps -o lstart=` reported a Jul 31 start for a daemon that had just reloaded), and `build_sha()`'s `<git sha>+<source hash>` is a fingerprint rather than a diagnosis — it cannot say whether the hash is what the SHA checks out to or the result of uncommitted edits, so "running the last release" and "running code from no commit" look identical. `checked=False` is kept distinct from `is_dirty=False` so "we could not tell" never reads as "it is clean".

### Changes

### Fixes

## [2026.8.2.5] - 2026-08-02

### Features

### Changes

- **Test coverage for the identity writer's production call sites, which two safety nets both missed.** #1195 made `add_worker_live` / `launch_workers` refuse to run without a `write_identity` — a runtime guarantee — but nothing verified the real callers supply one, and the live daemon found that out instead: `POST /api/config/workers` returned 500 with `add_worker_live() missing 1 required keyword-only argument: 'write_identity'`. Both nets were measured, not assumed. **mypy**: deleting the kwarg from a call site in `swarm.server.worker_service` type-checked clean — "Success: no issues found in 286 source files" — because that module is in the `[[tool.mypy.overrides]]` silence list; the identical deletion in `swarm.queen.runtime` (not silenced) *was* caught, and that pair is the discriminator showing the miss is the silencing rather than a mypy limitation. **The suite**: 1195 tests passed with the kwarg deleted, because every test reaching that path patches `add_worker_live`, so the signature is never exercised. Three new tests in `tests/test_worker_service.py` drive `WorkerService.spawn` and both branches of `.launch` through the *real* helper; dropping the kwarg from any one of the three call sites now fails exactly one of them. The `[[tool.mypy.overrides]]` "KNOWN GAP" comment records the measurement so the next person adding a required parameter across modules does not re-derive it — including that un-silencing is not free (`worker_service` = 18 pre-existing errors, `daemon` = 41).

### Fixes

## [2026.8.2.4] - 2026-08-02

### Features

### Changes

- **The worker identity-file write is now enforced by construction, not by a docstring.** #1187 fixed the bug by calling `ensure_worker_identity` in `daemon.spawn_worker` and held the rest of the invariant with a comment on `WorkerService.spawn` reading "NOT the public entry point". That is a convention, and a convention is exactly what the original bug *was*: `_write_worker_mcp_configs` existed, worked, and the create path simply never called it. Both spawn helpers in `swarm/worker/manager.py` — `add_worker_live` and `launch_workers`, the only two functions that call `pool.spawn` for a worker — now take a **required keyword-only `write_identity`** and invoke it after worktree resolution and before the process starts. Omitting it is a `TypeError` at the call site rather than a live worker that silently inherits a parent directory's identity. `WorkerService` takes the writer as a required constructor argument and forwards it; `ensure_queen_running` takes one too.

  **`WorkerService.spawn` was the wrong chokepoint**, and measuring it is what showed why: it is only *one* of the four production paths that bring a worker to life. `WorkerService.launch` reaches `add_worker_live` on its resume branch and `launch_workers` on its fresh branch, and `queen.runtime.ensure_queen_running` calls `add_worker_live` directly — all three route around `WorkerService.spawn`. Injecting there would have closed a quarter of the door. `add_worker_live` is also the only layer that knows `spawn_path`, the directory the session is actually started in, which differs from the configured path under `isolation: worktree`; today the worktree nests inside the repo so an inherited file still resolves to the right name, but that is a property of the layout rather than a guarantee, and the writer no longer depends on it.

  `daemon.spawn_worker` keeps its own `ensure_worker_identity` call — not as duplication, but because it additionally installs the worker's `/swarm-*` commands and Skills, which the spawn-time writer deliberately does not (it runs on every re-launch and on the Queen, where reinstalling buys nothing). The `WorkerService.spawn` docstring now states the guarantee instead of asking callers to use a different entry point.

### Fixes

## [2026.8.2.3] - 2026-08-02

### Features

### Changes

- **`POST /api/workers/spawn` resolves a configured worker's path from config.** Given only `{"name": …}` it returned 400 "path is required" even though the daemon already knew the path — which turned the documented recovery for a killed worker into a dead end. When the name is not in config the error now says so, rather than a bare "path is required" that reads as the caller's mistake.

### Fixes

- **A worker created after boot never got its own `.mcp.json`, so it transmitted a PARENT directory's identity.** `_write_worker_mcp_configs` had exactly one call site — the startup sweep in `daemon.start()`. Nothing on the create path invoked it, so `POST /api/config/workers` returned 201 with a live, RESTING worker that had no identity file; Claude Code then walks up the tree and loads the nearest parent's, which under `~/projects/*` carries `?worker=project-root`. Every ownership guard is an exact comparison against the canonicalised name, so the new worker **was** project-root to the board — able to read and close its tasks. This is #1055's failure mode in its non-recoverable form: wrong-CASE is rescued by case-insensitive canonicalisation, while wrong-IDENTITY resolves cleanly to a different real worker and nothing detects it. **Not an API-only edge case** — the dashboard's Add Worker button posts the same endpoint, so the operator's normal path produced the same broken worker, silently. The write now happens in `daemon.spawn_worker`, the one path every spawn route funnels through, **before** the process starts: a session reads `.mcp.json` at STARTUP, so writing it afterwards lands the file while the running session keeps transmitting the inherited name until a respawn. A post-boot worker was missing its `/swarm-*` commands and Skills for the same reason and until the same restart, so those are installed on the same path.
- **`kill` then `revive` was a dead end — kill was effectively irreversible through the API.** `kill` marks a worker STUNG without removing it, but `WorkerService.discover` rebuilds the roster from LIVE pool processes only, so once the PTY is gone the worker is erased from `daemon.workers` and `POST /api/workers/{name}/revive` answered 404 "Worker not found". Revive now respawns a worker that is absent from the roster but still in config, routed through `spawn_worker` so the recreated worker gets its identity file — a recovery path that resurrected a worker without one would have reintroduced the borrowed-identity bug above. An unconfigured name still 404s, so the fallback cannot turn a typo into a silent spawn.

## [2026.8.2.2] - 2026-08-02

### Features

### Changes

### Fixes

- **Task dispatches were pasting other sessions' terminal screens into workers.** An operator saw a worker receive its task alongside what was plainly terminal-session data, and had seen it several times. It was exactly that. `PlaybookOps.consolidate_learnings` ran on **every** task completion and built `task.learnings` by scraping the assigned worker's PTY — `get_content(30)`, strip CSI escapes, keep the last 15 non-blank lines — and `recall_learnings_for_task` then injected the top 3 of those, **untruncated**, into the next worker's dispatch. Measured across the 951 stored learnings when this was found: **37.8% of all lines were Claude Code UI chrome** (5382 of 14240), **96.5% of rows carried the footer tray**, 73% carried a token counter, and **26.6% began mid-sentence** because a screen capture cuts at the pane width. Task #1083's "learnings" ended with the thinking indicator, a box rule, and *the operator's next prompt* — filed as knowledge and replayed into other workers. Learnings are now the worker's **resolution**, which is the text `swarm_complete_task` already tells workers to write for exactly this audience ("shown to future workers picking up similar tasks — write it for *them*"). That promise was false: `complete()` writes `resolution` while the scrape overwrote the separate `learnings` field, so the deliberately-authored writeup never reached recall. A task completed with no resolution (a force-complete) now records no learnings rather than a screenful of chrome — recall skips it. The PTY-scrape path is deleted, not filtered: chrome patterns are provider-specific and would rot, and Swarm is multi-provider by design.
- **The recall block is now size-bounded, which is load-bearing rather than cosmetic.** Capping only the *number* of entries never bounded the paste — three entries of 10k chars is a 30k-char paste. Fixing the source alone would have made the reported symptom **worse**: measured on the live board, resolutions average 2077 chars against the scrape's 1178 (p90 3748, max 10539), so an uncapped block of three routinely ran past 6k and could reach 31k. Entries are clipped to `_LEARNING_CHARS_PER_ITEM` (800) against a `_LEARNING_BLOCK_CHARS` (2400) running budget, always **on a line boundary** — a cap that cut mid-word would reintroduce from the other direction the very defect being fixed. Verified against all 952 real rows on the board: worst single entry 2876 → 1014 chars, worst 3-entry block **7352 → 2584**, and **zero** entries cut mid-line. Truncated entries name `swarm_get_learnings`, which stays uncapped, so nothing is lost — only deferred to a deliberate pull. The cap applies to the pre-existing scraped rows too, which is what limits their reach now that they are being left in place rather than migrated.

## [2026.8.2] - 2026-08-02

### Features

### Changes

- **Auto-handoff task titles carry the source message id and its send time** — `Handoff from hub (msg #3151, sent 2026-08-02T03:49Z): …`. #1180's title read as fresh work while the message it wrapped was already 18 minutes old and partly overtaken by another task; nothing in the title said so, and the worker only found out after spending a turn on it. Omitted when the message has no timestamp: staleness is the whole point of the segment, and without a send time there is none to report — the title stays in its legacy form so #647's same-title collapse goes on covering the case the source key cannot discriminate.

### Fixes

- **One inter-worker handoff could still spawn two tracked tasks, and a daemon reload was never involved.** #1180 and #1181 both cite the *same* message row (#3151, hub → platform — a direct message with no fan-out siblings), so #1116's `(sender, created_at)` key was not at fault; it would have collapsed them correctly. The buzz log names the real cause: `TASK_ASSIGNED` followed 0.2 s later by `TASK_SEND_FAILED`, twice. **Spawning is not atomic** — `board.create` + `assign` persist a task row, and only then does the PTY write happen. A delivery failure makes `assign_and_start_task` return False, so `spawn_handoff_task` returns False, so the watcher takes its `if not ok: return False` branch and records *neither* the in-memory dedup *nor* the #894 `mark_read`. The source message stayed unread (`read_at` was stamped only after the second spawn). #1180 then held the recipient's `has_task` gate shut for 19 minutes; the moment it completed, the worker was task-less with the message still unread, and the #647 guard checks only **open** tasks, so it could not see the now-DONE #1180. #1181 spawned 166 seconds later. The guard is now a task tag written at `board.create` time (`handoff-src:<sender>:<created_at>`) and checked against tasks at **any status**, so it cannot miss the create-succeeded/dispatch-failed window and is restart-durable with no migration. The comment asserting the old dedup "prevents re-spawning the same handoff in the interim" was false when written and has been corrected in place. Verified by replaying a real 23-recipient broadcast and the real #3151 sequence from a copy of the live DB across a genuine board reopen; a positive control against the pre-fix code isolates the re-offer-after-completion case as the one that actually flipped.
- **`is_duplicate_work` compared provenance as if it were work.** The new title segment is per-message, so with it on both the existing task and the incoming handoff those tokens count against the Jaccard score twice — #913's duplicate-handoff suppression would have silently stopped matching work it matched the day before. It now takes an optional `normalize` applied to both titles; the handoff spawner passes the provenance stripper.

## [2026.8.1.4] - 2026-08-01

### Features

- **Opt-in text composer in the fullscreen terminal (mobile).** Fullscreen is how a worker is actually read on a phone, but the overlay is `position: fixed; inset: 0; z-index: 9999` and `openTerminalFullscreen` moved only the terminal element into it — the composer stayed behind in the detail panel, fully covered. The only reachable input was the raw xterm, which gets no autocorrect, autocapitalize or dictation. A **⌨ Type** button in the fullscreen bar now docks the composer, and lights up while it is open so the button reports state rather than merely offering an action. **Default off** — it costs terminal rows and reading is the common case. It *moves* the existing `#mobile-send-bar` rather than building a second input, so there is one textarea, one `mobileSend()` and one set of autocorrect attributes to keep correct. The close path restores it **before** removing the overlay: the composer is a moved node, not a copy, so tearing the overlay down while docked would delete the detail panel's only touch input for the rest of the session — a regression that would surface later on a surface nobody associates with fullscreen. A test pins that ordering and was proven to fail when the two calls are swapped.

### Changes

### Fixes

- **The mobile composer never rendered, on any device.** `#mobile-send-bar` carried an inline `style="display:none"` in the markup, and the rule meant to reveal it — `.mobile-send-bar.visible { display: flex }` inside `@media (pointer: coarse)` — has no `!important`. An inline style attribute outranks any selector without one, so `selectWorker`'s `classList.add('visible')` could never take effect. Measured in Chromium at 414px with touch emulation, with a positive control on each link in the chain: coarse pointer matched **true**, the rule existed and matched **true**, the class was applied **true**, and computed display was still `none`. So the one input path offering autocorrect, autocapitalize, spellcheck and voice dictation was dead from the start, which is why typing on mobile meant typing into the terminal. The inline attribute was redundant as well as harmful — `base.html` already hides the composer by default — so removing it is the whole fix. Desktop is unaffected: with a fine pointer the media query does not match and the composer stays hidden.

## [2026.8.1.3] - 2026-08-01

### Features

### Changes

### Fixes

- **The task panel had no reachable minimize button on mobile (#1159).** The button was rendered the whole time — it was permanently off-screen. `.panel-header` is a nowrap flex row with no overflow handling, and `.tab-group` is a flex *item*, so its default `min-width: auto` refused to shrink below its content. Measured at a 414px viewport: the tab strip claimed 615px and pushed `.btn-collapse` — the last child, `margin-left: auto` — to x=647..670. Non-zero size, `display: flex`, and 233px past the right edge with nothing to scroll it into view. Also failed at 768px. `min-width: 0` lets the tab strip shrink and `overflow-x: auto` lets it scroll its own tabs, the same idiom `.filter-bar` already uses two rules up in that media query; the chevron keeps `flex-shrink: 0` so it stays pinned at the edge. Measured after: 414px → chevron at x=382..405 fully visible, header `scrollWidth` 669 → 412; desktop at 1200px unchanged in every measurement. Not covered by the pytest suite — this is CSS layout and the repo has no Playwright harness — so it was verified by direct measurement of the rendered header, before and after, at three viewports.

## [2026.8.1.2] - 2026-08-01

### Features

### Changes

### Fixes

- **`swarm_park_task` accepts an owned ASSIGNED task, not just an ACTIVE one (#1159).** Requiring ACTIVE made the verb **racy rather than strict**: the INV-2 reconciler demotes a worker's ACTIVE task to ASSIGNED every time that worker goes RESTING — six times in two hours on #1158 — so the window in which park was reachable was seconds wide and not under the caller's control. A worker who had deliberately set a task down could not set it down. From ASSIGNED the status does not change; what the call adds is the `PARKED_TAG` marker, which is the part that actually stops the momentum machinery. BLOCKED and terminal tasks stay refused — BLOCKED is already off-active and has its own unblock verb (#1059 `release`), and a DONE/FAILED task has nothing to set down. `parkable_tasks_for_worker` now also excludes already-parked tasks, so re-parking is a no-op rather than something that makes the "which one did you mean?" disambiguation prompt ambiguous, and sorts ACTIVE first so the no-argument form still picks the task the worker is on.

## [2026.8.1] - 2026-08-01

### Features

### Fixes

- **`swarm_park_task` reported success and left the task ACTIVE (#1159).** The park write *did* land — what undid it was `WorkerStateTracker._promote_one_assigned`, which fires on every RESTING → BUZZING transition and promotes the worker's most-recently-updated ASSIGNED task. `TaskBoard.park` stamps `updated_at`, so the task just set down sorted **first** there and was re-activated by the worker's very next turn — a turn usually about something else entirely — after which `activate` cleared `PARKED_TAG` and erased the evidence, so it repeated every cycle. #1015's fix **landed but was incomplete rather than regressed**: it guarded `auto_start_next_assigned` and the IdleWatcher, both of which correctly skip `is_on_hold`, and missed this third re-activation path. The diagnosis is anchored in `task_history`, not inferred — the re-activation left **no** history row, which rules out `start_task` (that path always writes `TaskAction.STARTED`) and leaves the promoter, which writes none. The confirmation message was fixed too: it asserted *"Board is truthful now — no reload needed"*, a claim the caller could not verify, worded to discourage the very re-query that would have caught the bug, and it stayed word-for-word convincing for months. It now reports the status and owner **read back from the board** after the write, so a failed write says so.

- **The Queen view remembers whether you minimized the task panel.** It hardcoded `setBottomCollapsed(false, false)` — "the Queen dashboard always shows the task board expanded" — so every return to the Queen silently discarded a minimize, and because expanding also re-applies the saved drag split, the panel reappeared at its dragged size when you had deliberately put it away. The worker view already honoured the preference, so the two views disagreed; both now read it through one `readBottomCollapsed()` helper so they cannot drift apart again. Two further defects surfaced while fixing it: the worker view's `storedCollapse !== ''` read a *never-set* preference as "collapse", minimizing a panel nobody had asked to minimize on a fresh session; and the collapsed flag lived in `sessionStorage` while the panel's size lives in `localStorage`, so one preference had two lifetimes — reopen the tab and the panel came back expanded at the size you'd dragged it to while minimized. Both halves are now durable, with a one-time migration of any session-scoped value.

- **A filed task's requirements can now be corrected — `swarm_edit_task` and `queen_edit_task` (#1060).** Once a task was filed, nobody could fix its description: workers had `swarm_create_task` and no edit, and the Queen had no edit verb either, so the obvious workaround — the worker drafts a correction, the Queen writes it in — did not exist. The failure mode is silent: the task keeps its stale description and the next reader works from wrong requirements with nothing signalling that a correction was attempted and lost. It cost two message round-trips in one day, including #1055's restart note, which had nowhere to live. Both verbs are thin wrappers over the existing `daemon.edit_task`; no new persistence. **Authority reuses the existing ownership rule** rather than inventing a second one — a worker may edit a task assigned to it, the same comparison `swarm_complete_task` makes, so the guard is not weakened and a worker still cannot touch another's task. Gating on *filer* was rejected because self-filed tasks silently lose `source_worker`, which would refuse the verb for exactly the person who needs it. **`acceptance_criteria` is Queen-only, deliberately:** the verifier drone grades a completion against those, so an assignee editing its own criteria is self-grading — a worse failure than the stale description this fixes, because it corrupts the check rather than the requirement. Unassigned and terminal tasks are refused. `TaskManager.edit_task` already recorded `TaskAction.EDITED` but with an **empty detail**, so the trail proved something changed and never what; it now names each changed field and previews the value it replaced, because an edit that can rewrite requirements invisibly is its own hazard.

### Changes

- **The CI type-check step now gates instead of being decorative (#1062).** It ran under `continue-on-error` because ~450 errors across 83 modules would have redded every run — a step structurally incapable of failing, the same defect class as the pip-audit step in #1051. Those 83 modules are now silenced explicitly in `[[tool.mypy.overrides]]`, so `mypy` exits 0 and the step gates for real: the other ~200 modules, and every new file, fail CI on a new type error. Verified the gate is not theatre by planting a deliberate error in a non-listed module — CI-equivalent run exits 1 — then reverting it. Per-module rather than a baseline because `uv.lock` is gitignored, so CI resolves dependency versions fresh and the error set drifts (450/82 locally vs 442/77 in CI on the same commit); a line-or-message baseline captured locally would report phantom "new" errors in CI. Known gap, stated rather than hidden: a new error *inside* an already-listed module is not caught — shrinking that list is the work, and a module should be deleted from it the moment it type-checks clean.

### Fixes

- **`_tasks_create`'s priority annotation no longer lies (#1062).** It declared `prio: int` while its only caller passes `PRIORITY_MAP[...]`, which yields a `TaskPriority`. Measured before changing anything: the runtime was already correct, so the CLI worked and no user was affected — this was a mis-annotation, not the runtime bug it was reported as. It still matters, because an actual `int` reaching the board would `KeyError` on `_PRIORITY_ORDER[task.priority]` when sorting and break `.value` on serialize; a regression test now pins that behaviour across all four priorities.
- **`aiohttp` timeouts use `ClientTimeout` (#1062).** Three `session.get`/`post` calls in `cli.py` passed a bare `float`, which `aiohttp` 3.14 no longer accepts on those signatures — surfaced by the 3.13.3 → 3.14.3 bump in #1061, which is why that bump had to land first.

## [2026.7.30] - 2026-07-30

### Features

### Changes

- **CI's dependency audit can now fail, and 25 of its 35 findings are fixed rather than waived (#1051, #1061).** The "Security audit dependencies" step ran `uv run pip-audit` under `continue-on-error` while `pip-audit` was declared nowhere, so it exited 2 with "Failed to spawn" and reported green on every run since it was added — it had never audited anything. Declaring it surfaced 35 advisories at once, waived in `.github/pip-audit-waivers.txt` so the gate could start working immediately (#1051, no changelog entry of its own). #1061 then retired the runtime ones by actually bumping: `aiohttp>=3.14.1` (21 advisories), `cryptography>=48.0.1` (3), `click>=8.3.3` (1). `cryptography` is transitive via `webauthn` and is now pinned directly **only** to carry a security floor — that means owning its upgrade cadence, done deliberately because these are runtime advisories in a package doing crypto. Waivers are down to 10; the remainder are transitive (`cbor2`, `idna`, `pyasn1`, `soupsieve`) plus two dev-only, each of which would need its own direct pin, which is a separate call about how much transitive surface to own. Since `uv.lock` is gitignored, CI resolves fresh each run, so this step can legitimately go red with no code change — the correct response is a reviewed waiver line, never re-adding `continue-on-error`.

### Fixes

- **`queen_reassign_task`'s refusal now names the resolving fact (#1057).** It answered a failed move with a bare `(not available)`, which reads as though the *target* is unavailable. The gate is `task.is_available` — `status == UNASSIGNED and not is_on_hold` — a property of the **source** task; the target worker is never inspected at all. That misdirection cost an hour on the theory that reassignment fails when the target already holds work. The message now states the actual blocker (`#1013 is BLOCKED (waiting on review) — a blocked task cannot be released or moved. Unblock it first.`), including the block reason. Behaviour is unchanged: `is_available` and `board.assign` are untouched, and the move is still refused. The tool's own description was also corrected — it advertised moving a task whose assignee "can't reach the work (**blocked**, …)", which is precisely the case it cannot handle — and the same bounded sweep fixed two docstrings on `TaskBoard.block_for_operator` and `block_on_external` that both claimed "unpark is the normal operator re-dispatch (`activate` → BLOCKED→ACTIVE)". That path is unreachable: `activate` has no status gate, but both of its call sites are gated upstream on ASSIGNED, so nothing can take a BLOCKED task back to ACTIVE.
- **Every *configured* worker gets an identity file, not just the running ones (#1055).** `daemon.workers` holds live PTY processes — `worker_service.discover` builds it from `pool.discover()` — but `_write_worker_mcp_configs` iterated it, so a worker whose process happened to be down when the daemon last started got no `.mcp.json` at all, and nothing writes one later because the sweep only runs at startup. sculpt-studio and aria ended up with none despite valid, existing paths; their sessions then inherit the **parent directory's** config and transmit `project-root`. This is the failure #1045's canonicalisation cannot catch — `project-root` is a real registered worker, so a borrowed identity resolves cleanly and stays silent, which is why sculpt-studio's own work was recorded against project-root on #1035/#1036. The new `_identity_targets()` sources the sweep from `config.workers`, unioning in running workers so a `isolation: worktree` session (whose live path differs from the configured repo path) still gets its file. `_install_worker_artifacts` had the identical running-only defect and shared the same fix — a worker that was down also silently lost its `/swarm-*` commands and Skills. An unknown provider string is now caught per-worker instead of aborting the sweep and stranding every worker after it in iteration order.
- **Worker identity is now validated at the MCP boundary, so a worker can close its own task again (#1045).** Caller identity is a free-form string in the MCP URL (`?worker=<name>`), written into each worker's `.mcp.json` by the daemon, and nothing downstream re-checked it. Every ownership guard is an exact, case-sensitive comparison, so a stale or wrong-cased file silently turned a legitimate worker into an identity that owns nothing. rcg-platform's config said `worker=Platform` while the board stores `platform`: every attempt to close a **pre-assigned** task was rejected with "not assigned to you", naming the caller's own worker on both sides of the message. The reason it looked intermittent rather than deterministic is that the **unassigned** self-close path compares nothing — it accepted the bogus name and stamped it onto the board, leaving `#1044` as the only task among 1000+ owned by `Platform`, which read as a "working control". `_resolve_worker_identity` now matches the incoming name case-insensitively against the worker registry and returns the *registry* spelling, so downstream comparisons stay exact and the ownership guard stays strict — it still rejects a worker closing another's task. A name matching no registered worker resolves to `unknown` rather than passing through, which routes it into the existing fail-fast diagnostic that points at the MCP URL instead of blaming the assignment. `queen` is reserved (she calls the same endpoint but is only in the registry while her PTY runs). Same fix covers `swarm_task_status filter='mine'` and `swarm_park_task`, which compare identity the same way.
- **Tilde-path workers get their `.mcp.json` written — and corrected (#1045).** `Path("~/proj")` is a *relative* path whose first component is literally `~`, so `is_dir()` answers False and `_write_worker_mcp_configs` silently skipped every `~`-configured worker: 10 of 24 on the reporting box, `platform` among them. That is why platform's hand-written `?worker=Platform` was never repaired. The new `_worker_dir()` helper expands the path (mirroring `WorkerConfig.resolved_path`) and is applied at all four daemon sites that were skipping workers the same way — MCP config, worker artifacts (`/swarm-*` commands + Skills), file-ownership scanning, and the worktree conflict map. On the reporting box this recovers 8 workers; the 2 still skipped have genuinely missing directories.
- **A failed PTY send no longer silently un-assigns the task (#1045).** A send failure is a *delivery* failure, not an assignment decision — the assignment had already succeeded and been logged. The rollback rewrote board state while returning success to the caller, recorded only as `send failed to <worker> — returned to pending`; it is the mechanism behind #1039, #1044, #1045, #1048 and both reverts of #980. #527 had already established that keeping the task ASSIGNED is correct for auto-handoffs; that reasoning applies to every assignment, so the special case is now the general rule (scoping it to explicitly-targeted tasks would have left #980, which carries no `target_worker`, still broken). Genuine worker *death* is still handled — and better — by `TaskBoard.unassign_worker` from the dead-worker cleanup, which is the deliberate return-to-the-pool mechanism; the operator still gets the `TASK_SEND_FAILED` notification either way.
- **A parked task stays parked (#1015).** `swarm_park_task` moves ACTIVE → ASSIGNED and keeps the owner, which left it indistinguishable from freshly-queued work — so `auto_start_next_assigned` re-activated the very task that had just been set down, and the IdleWatcher kept nudging about it. `TaskBoard.park` now applies a `parked` marker (added to the existing `HOLD_TAGS` set rather than inventing a parallel concept), the auto-start chain and the IdleWatcher's bucketing both skip held tasks, and `TaskBoard.activate` — the single re-dispatch chokepoint — clears the marker, so resuming is still one normal action and the suppression is a pause rather than a one-way door.
- **`swarm init` no longer tells you to run `swarm start all` after it already started the daemon.** Installing the systemd/launchd service also *starts* it, so the final "Ready! Next: swarm start all" sent every fresh installer straight into `Another swarm daemon is already running. Run 'swarm stop' to stop it.` — the first thing a new dev saw was an error. Init now probes the unauthenticated `/health` endpoint (up to 15s when this run installed the service, one quick look when it was already there) and prints the dashboard link instead. When nothing answers, the advice depends on whether anything supervises the daemon: with a service installed it points at `systemctl --user status swarm` / `launchctl list com.swarm.dashboard`, and only a host with no service manager still gets the `swarm start all` hint. The port is read straight from the `config` row in `swarm.db` (or the YAML seed, else 9090) rather than through `_load_config_db_first`, which would create and migrate the DB and break init's promise never to touch it. The `domain` branch also stopped printing two separate "Ready!" lines.

## [2026.7.27] - 2026-07-27

### Features

### Changes

- **The test suite now enforces the project's zero-warning rule.** `filterwarnings` gained `"error"`, so a warning fails the run instead of scrolling past in the summary. Each of the four warnings tolerated until now hid something: two orphaned coroutines, the aiohttp started-app mutation below, and a conftest safeguard notice. The narrow pre-existing ignores are kept. The live-DB safeguard notice moved from `warnings.warn` to the terminal reporter — it is an expected, environment-dependent message that only fires when an operator's daemon is running and never in CI, so as a warning it would have permanently blocked the gate; it still prints, so a skipped safeguard is never silent.
- **Coverage and hook-install artifacts are ignored.** `.coverage` / `coverage.json`, the six `.claude/commands/swarm-*.md` slash commands, the two `.claude/skills/swarm-{checkpoint,coordinate}/` directories, and `.claude/ux-audit.json` all sat untracked in `git status`. Every one is generated — `swarm install-hooks` copies the commands and skills in from the tracked originals under `src/swarm/hooks/{commands,skills}/`, and `ux-audit.json` is already treated as machine-local by `coordination/ownership.py` and `git/conflicts.py`. The `.claude/` entries are named individually rather than ignoring `.claude/commands/` or `.claude/skills/` wholesale, mirroring the explicit `WORKER_COMMAND_FILES` / `WORKER_SKILL_NAMES` tuples in `hooks/install.py`, so a hand-authored command or skill added later still shows up in `git status` instead of vanishing — those lists and these patterns need to stay in sync.

### Fixes

- **The dashboard mutated a started aiohttp Application on three request paths.** aiohttp freezes the `Application` mapping when the server starts; writing to it from a handler emits `DeprecationWarning: Changing state of started or joined application` and is slated to become a hard error. Three handlers created their state lazily on first request: `pty/bridge.py` (`_terminal_sessions`), `server/routes/websocket.py` (`_ws_ip_counts`, two call sites), and `web/routes/login.py` (`passkey_store`). All three now read state seeded before freeze — in `create_app` for the first two, `setup_web_routes` for the passkey store — matching the existing `daemon` / `rate_limits` convention. This fired on every live daemon, not just under test. Four `@pytest.mark.filterwarnings("ignore::DeprecationWarning")` markers that had been suppressing the symptom in `tests/test_terminal.py` are removed, so the next genuine deprecation there is visible.
- **`auto_start_next_assigned` leaked an un-awaited coroutine when no event loop was running.** `d.start_task(...)` is evaluated before `asyncio.create_task` runs, so the sync/CLI path where `create_task` raises `RuntimeError` dropped a live coroutine on the floor — surfacing as `coroutine 'SwarmDaemon.start_task' was never awaited`, attributed to whatever unrelated test or request happened to trigger the collection. The coroutine is now bound first and closed in the `except`, the idiom already used in `mcp/handlers/_queen_relay.py`.

## [2026.7.26] - 2026-07-26

### Features

### Changes

- **QA screenshot runs are ignored for every surface, not just mobile.** `.gitignore` only listed `docs/qa-mobile-*/`, so the desktop and playbook runs (`docs/qa-desktop-*/`, `docs/qa-pb*/`) plus loose one-off captures (`docs/qa-share-*.png`) sat in `git status` indefinitely — 77 untracked files of noise that made it easy to miss real changes. Widened to `docs/qa-*/` and `docs/qa-*.png`. Deliberately **no** `!.../FINDINGS.md` negation: the per-run `FINDINGS.md` is a generated manifest indexing the very PNGs that stay ignored, and the one substantive write-up in the tree is already byte-identical to the tracked `docs/qa-mobile-findings-2026-05-20.md`. Curated findings survive by being *promoted out* of the run directory to `docs/qa-<surface>-findings-<date>.md`, which sits outside both patterns; the comment now says so, since the previous wording claimed findings were committed while the pattern was silently hiding them.

### Fixes

## [2026.7.24] - 2026-07-24

### Features

### Changes

- **The task board now stays mounted behind a focused worker.** Worker view used to `display:none` the bottom panel outright, so checking a task meant leaving the worker for the Queen dashboard. It now collapses to a header-only tab strip — click any tab or the collapse chevron (no longer phone-only) to pop it open in place. The Queen view always shows it expanded, and a worker-view expand/minimize choice persists across view switches without overwriting the other. The collapsed resize handle is hidden with `visibility`/`height:0` rather than `display:none`, because a `display:none` grid item drops out of auto-placement and slides the panel into the wrong track.

### Fixes

- **The Command Center has been dead on arrival since 2026.6.8.2.** Its `init()` called `setupMobileComposer()` bare, but that function is declared in the *other* top-level IIFE in `dashboard.js` — separate scopes, so the call threw `ReferenceError` on `init()`'s **first statement** and every statement after it never ran. Consequences: `attachCcResizeHandles()` never wired the Queen/Attention column splitter (dragging it did nothing — the reported symptom), `body.cc-active` was never applied so the dashboard landed on an empty "Select a worker" pane with the task panel dangling below it at the CSS 50/50 default, and the digest/attention pollers never started (the digest sat on "Loading today's summary…" while real escalations went uncounted). `setupMobileComposer` is now exported on `window` and called defensively. Added a static regression scan for bare cross-IIFE calls in `dashboard.js` — the same bug class previously hit `updateQueenHealthIndicator`.
- **Dragging the horizontal split persisted a ratio the operator never chose.** `endDrag` re-measured the detail-area at mouseup instead of storing what `moveDrag` had applied, and that measurement caught a mid-relayout height (xterm refits and the action-bar reflow run during the drag) — observed storing `0.697` for a split actually applied at `0.575`, so the panel jumped on the next view switch or reload. It now persists the applied ratio.

## [2026.7.23.7] - 2026-07-23

### Fixes

- **Drone auto-approve now works for non-Claude providers (Codex/OpenCode/Gemini).** `_decide_idle_state` switched to event-based prompt routing whenever `events is not None`, but every non-Claude provider inherits the base `parse_events` that returns a single `UNKNOWN` event — a non-None list that has no `choice`/`plan`/`accept_edits`/`user_question` type. That silently **disabled the provider's regex `has_*_prompt` detectors**, so a Codex `git status` approval was classified WAITING but never auto-approved (it stalled indefinitely where a Claude worker sails through). Added `_has_structured_events()` and gated event-routing on it, so providers without typed events fall back to their regex detectors. Verified live: an injected `git status` on a Codex worker is now auto-approved end to end (WAITING → `✔ You approved` → RESTING). Follows the Stage 1 Codex detection work (2026.7.23.6).

### Features

### Changes

### Fixes

## [2026.7.23.6] - 2026-07-23

### Changes

- **Codex support Stage 1: real state detection (the glyph stub is gone).** `providers/codex.py` was detecting Ratatui glyphs `[◇□]`/`[▶▷]` that never appear in the PTY under `--no-alt-screen`, so a Codex worker was **permanently BUZZING** — never RESTING, never WAITING — and its command-approval prompts were invisible to the drone (a `git status` approval stalled where a Claude worker auto-approves instantly). Rewrote detection from **empirically-captured raw Codex PTY output** (scraped live 2026-07-23; see the `reference_codex_pty_patterns` note): `classify_output` now reaches **WAITING** on the approval widget (`Press enter to confirm or esc to cancel`), **BUZZING** on the live turn timer (`Working (Ns · esc to interrupt)`), and **RESTING** on the idle composer footer; `has_choice_prompt` / `get_choice_summary` detect the approval and extract the `$ <command>` awaiting it; `safe_tool_patterns` matches Codex's `$ git status` / `$ ls` format so the drone auto-approves read-only commands; `approval_response` is Enter/Esc (not the base `y`/`n`). Verified live: the daemon now reports RESTING for an idle Codex worker (was stuck BUZZING). Codex tests rewritten around the captured fixtures.

### Fixes

## [2026.7.23.5] - 2026-07-23

### Changes

- **Better Codex support (Stage 0): provider-aware dispatch text + state correctness.** Drones were injecting Claude-specific formatting into Codex workers. Fixed by making two things provider-owned: (1) `LLMProvider.plan_mode_preamble()` — the user-request plan-mode preamble no longer hardcodes Claude's `ExitPlanMode` tool; Claude keeps its exact wording (dispatch stays byte-identical), Codex gets its present-plan-and-wait / `AskUserQuestion` convention (and keeps `swarm_complete_task`, which Codex has via `~/.codex/config.toml`), and the base default is provider-neutral. (2) `LLMProvider.has_active_turn_signal()` — the stuck-BUZZING safety net and nudge guards now delegate the "is this worker mid-turn?" check to the worker's provider instead of applying Claude's private regexes to every provider, so a genuinely-busy Codex worker (`[▶▷]`) is no longer flipped to RESTING after 10 min. Also gated the inert `.claude/commands` / `.claude/skills` / `.mcp.json` writes off non-hooks providers (Codex reaches MCP via `~/.codex/config.toml`, not a per-worker `.mcp.json`). Codex state-detection parity (reaching WAITING, detecting approval prompts) is a documented Stage 1 follow-up requiring live-PTY validation.

## [2026.7.23.4] - 2026-07-23

### Changes

- **OAuth `/authorize` now shows an explicit consent screen instead of auto-approving.** Previously a logged-in operator's session silently issued the code, which felt like a rubber stamp. `/authorize` now renders an Approve/Deny page; approving POSTs to a new `/oauth/consent` endpoint that issues the code. The pending request is carried in a signed consent token (`mint_consent_token`/`verify_consent_token`) so the POST can't be forged or tampered with, and `/oauth/consent` is session-gated **and** origin-checked (removed from the CSRF origin bypass — only `/oauth/token` and `/oauth/register`, which are cross-origin from the connector, stay exempt). Denying redirects back with `error=access_denied`. This adds a genuine second gate on top of the session + redirect-URI allowlist.

## [2026.7.23.3] - 2026-07-23

### Features

- **OAuth 2.0 for the MCP endpoint + a "MCP / Connectors" settings card.** Claude Desktop's remote-MCP connector authenticates via OAuth (auth-code + PKCE, optionally preceded by Dynamic Client Registration), so the static bearer token alone couldn't drive its "Connect" button. Added a minimal authorization server (`swarm.auth.oauth_server`, `routes/oauth.py`): RFC 8414/9728 discovery (`/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource`), DCR (`/oauth/register`), `/oauth/authorize` (auto-approves on a valid dashboard session, else bounces through `/login?next=`), and `/oauth/token` (PKCE code exchange + refresh). Tokens and client secrets are stateless HMAC-signed blobs keyed by a persisted signing secret — no DB migration. `/mcp` now accepts an OAuth access token alongside the static token and advertises the resource metadata via `WWW-Authenticate` on 401 so discovery kicks in. `/authorize` auto-approve is guarded by a redirect-URI host allowlist (Claude/Anthropic/localhost, extensible via the `oauth_allowed_redirect_hosts` secret) to prevent an open-redirect code exfiltration. The dashboard settings page (Security tab) gained an "MCP / Connectors" card showing the MCP URL, the static token (copy + rotate), and the OAuth Client ID/Secret (copy + rotate the signing key), with connect instructions for Claude Desktop and ChatGPT.

## [2026.7.23.2] - 2026-07-23

### Security

- **Authenticate the `/mcp` endpoint.** The MCP HTTP surface was exempt from the session-auth middleware under a localhost-only assumption, but once the daemon runs behind a public tunnel that made it an anonymous remote-code-execution hole — any unauthenticated caller could `swarm_create_task` a prompt straight into a worker's Claude Code PTY. `/mcp`, `/mcp/sse`, and `/mcp/message` are now gated by a dedicated bearer token (`swarm.auth.mcp_token`, persisted in the `secrets` table, separate from the dashboard password so external MCP connectors never hold the dashboard credential). Local workers authenticate transparently via an `Authorization` header injected into their `.mcp.json`. The gate follows the existing password regime — a password-less (local, unexposed) install is unchanged. Token accepted as a `Bearer` header or `?token=` query param; the 401 carries no `WWW-Authenticate` header to avoid tripping Claude Code's OAuth discovery.

## [2026.7.23] - 2026-07-23

### Fixes

- Dashboard: task reassignment now updates both the old and new worker cards live. The `tasks_changed` WS handler (and sibling task-lifecycle events) only refreshed the central task panel, not the per-worker `worker-task` labels — so a reassignment stayed stale on the worker cards until a manual page reload. Both handlers now also call `refreshWorkers()`.

## [2026.7.18] - 2026-07-18

### Features

### Changes

### Fixes

- **Idle dashboard CPU: eliminate continuous repaints.** The pulsing status
  indicators (BUZZING/WAITING/STUNG worker dots, WAITING Queen card, needs-input
  pill, ACTIVE task badge) animated `box-shadow`, which forces a main-thread
  repaint every frame for the life of the infinite animation. Moved the glow/ring
  onto a static-box-shadow `::after` overlay that animates only `opacity`/
  `transform` (GPU-composited → no per-frame repaint); visuals unchanged. Also
  disabled the xterm cursor blink (`cursorBlink: false`), which was repainting the
  cursor cell ~2×/sec whenever a terminal was on screen. Both are pure idle-CPU
  wins with no functional change.

## [2026.7.17] - 2026-07-17

### Features

### Changes

- **Queen prompts made criteria-aware (context-engineering pass).** The headless
  Queen's completion-evaluation guidance now judges output against a task's
  acceptance criteria when present, and the interactive Queen's completion-
  judgment guidance points at the acceptance criteria + the verifier's Harness-tab
  verdicts (a FAILED / shadow would-reopen signals the work may not be done). The
  live Queen `CLAUDE.md` was synced to the shipped prompt.

### Fixes

## [2026.7.16] - 2026-07-16

### Features

- **Criteria-graded verification (Outcomes pattern).** At task creation the
  headless Queen now synthesizes best-effort acceptance criteria (+ an advisory
  effort tier) so the verifier has a rubric to grade against and the worker gets
  an explicit done-definition. Empty for open-ended/exploratory tasks (no
  fabricated criteria → no false failures); skipped for standing-loop filler.
  Gated by `verifier_criteria_synthesis` (default on).
- **Verifier re-wired into completion — SHADOW mode by default.** The tiered
  verifier (dead-wired since 2026.5.25.4) fires again after `swarm_complete_task`
  but, by default, only *records* verdicts for the Harness metrics without
  reopening tasks. Flip `verifier_enforce` on once the verdict stream looks
  trustworthy. No-diff task types (content/research/operator) grade the
  resolution instead of a diff; the reopen cap is now configurable
  (`verify_reopen_cap`, default 2).
- **Dispatch enrichment.** Task messages carry the acceptance-criteria
  done-definition plus a Claude-gated advisory effort tier (never shown to
  non-Claude workers, never enforced). Gated by `dispatch_enrichment`.
- **Learning preload.** The top relevant prior-task learnings are pushed into the
  dispatch message (keyword-overlap relevance) instead of waiting for a
  `swarm_get_learnings` pull. Gated by `learning_preload`.
- **Harness digest: verifier metrics + stale-learning surfacing.** New
  display-only read-outs — verdict mix, acceptance-criteria coverage %, shadow
  would-reopen count, and dreamer learnings old enough to review for retirement
  (operator-gated; never auto-deleted).

### Changes

- New `DroneConfig` flags (all default-on except `verifier_enforce`):
  `verifier_criteria_synthesis`, `verifier_enabled`, `verifier_enforce`,
  `verify_reopen_cap`, `dispatch_enrichment`, `learning_preload`. Documented in
  `swarm.yaml.example`.
- DB schema v16: adds `tasks.effort_tier`.

### Fixes

## [2026.6.27] - 2026-06-27

### Features

### Changes

- **Mobile terminal d-pad moved to top-right:** the floating directional pad on a
  worker terminal now anchors top-right instead of bottom-right, clearing the
  mobile composer and the jump-to-bottom pill. Drops the bottom-edge coordination
  (`.dpad-raised` shift, coarse-pointer `:has()` nudges, and the dead JS toggle).

### Fixes

## [2026.6.23] - 2026-06-23

Loop-engineering pass — inspired by the 2026 "loop engineering" / agent-harness
writing. Audited Swarm against the loop checklist and closed the gaps.

### Features

- **Native `/loop` coexistence (#761):** a worker parked between native `/loop`
  fires is no longer nudged or assigned over. A stateful `LoopDetector` reads the
  ScheduleWakeup signal (`Next wakeup scheduled for … (in Ns)`) for a precise
  no-disturb window; the worker stays `RESTING` with a dispatch-protection guard.
  Provider-gated via `supports_native_loop`.
- **Per-task token-budget governor (#762):** the "non-negotiable budget ceiling"
  stopping condition. `DroneConfig.task_token_ceiling` (output tokens, default 0 =
  off) charges each worker's output-token delta to its ACTIVE task; on breach the
  task is escalated and parked (`ACTIVE → BLOCKED`) without interrupting the PTY.
- **Standing background-improvement loops (#765):** operator-controlled recurring
  task generators. Idle-triggered off the empty-queue self-loop hook (preempted by
  any real task), a rolling daily per-loop token cap, and a dashboard "Loops" tab
  with per-worker start/pause/stop, a global kill switch, and a live burn readout.
- **Operator-gated harness-improvement digest (#789):** closes the hill-climbing
  loop the safe way — aggregates the signals Swarm already mines (error-prone
  tools, suggested approval rules, playbook win-rates, dreamer patterns, override
  tuning) into a dashboard "Harness" tab with one-click apply for low-risk actions
  only. Display-only items (tool/prompt rewrites) never auto-apply; the route is
  GET-only and adds no apply endpoints.

### Changes

### Fixes

## [2026.6.21] - 2026-06-21

### Features

### Changes

- **Messages tab rows are now clickable to a detail modal.** Message
  content is truncated in the list, but messages are often long — so a row
  click now opens a read-only detail modal with the full content, type,
  route, and timestamp (and, for a collapsed broadcast, the per-recipient
  read state). This replaces the previous broadcast-only inline expand and
  makes the Messages tab consistent with the Queen history tab's
  click-to-detail behavior. (Operator feedback on B10.)

### Fixes

## [2026.6.20.8] - 2026-06-20

### Features

### Changes

### Fixes

- **Test suite no longer intermittently ERRORs when a local `swarm serve`
  daemon is running.** The session-scoped `_assert_live_db_untouched`
  safeguard asserts `~/.swarm/swarm.db`'s mtime is unchanged across the run
  to catch a test bypassing the DB sandbox. But a *running* daemon
  legitimately WAL-checkpoints that file every 300s, so a ~130s test run
  had a ~44% chance of overlapping a checkpoint and tripping the assertion
  (reported, misleadingly, against the alphabetically-last test). The
  safeguard now stands down (with a warning) when `~/.swarm/daemon.lock`
  holds a live PID — an external writer makes the mtime signal
  unattributable — while still running strict in CI where no daemon
  exists. The real protection (the setup-time `_DEFAULT_DB_PATH` sandbox)
  is unchanged.

## [2026.6.20.7] - 2026-06-20

### Features

- **Messages tab: bulk delete + compose** (feature B10, phase 3 of 3 —
  completes B10). The Messages tab gained a compose box (pick a worker or
  `*` broadcast, a type, and content → `POST /api/messages/send`) and
  multi-select bulk delete (reusing `POST /api/messages/delete` behind the
  themed confirm dialog). Deleting a collapsed broadcast row removes all
  its underlying recipient rows at once. Both reuse existing endpoints; the
  list live-refreshes after either action.

### Changes

### Fixes

## [2026.6.20.6] - 2026-06-20

### Features

- **Messages tab** (feature B10, phase 2 of 3). New "Messages" tab in the
  dashboard bottom panel: a searchable, read-only view of inter-worker
  traffic (findings, warnings, dependencies, status, notes). Content
  search, an unread-only toggle, and a date range; rows show a type badge,
  sender → recipient, content, time, and a read/unread dot. A `*` broadcast
  (one DB row per recipient) is **collapsed client-side** into a single
  `→ * (N)` row, expandable to per-recipient read state. "Load more" pages
  through history, and the list **live-refreshes** off the existing
  `message` WebSocket event when the tab is open. The view never marks
  anything read — worker read-state is left untouched. (Phase 3 — bulk
  delete + a compose box — follows.)

## [2026.6.20.5] - 2026-06-20

### Features

- **Messages backend** (feature B10, phase 1 of 3). `GET /api/messages` is
  now filterable for the upcoming Messages tab: new `q` (content search),
  `unread_only`, `since`/`until` (created_at), and `offset` params on the
  endpoint and `MessageStore.get_recent`. All optional — a bare call is
  unchanged. The endpoint is **read-only and never marks anything read**
  (a regression test pins this — operator browsing must not corrupt the
  worker read-state that drives coordination nudges).

### Changes

- **Inter-worker message retention is now scoped, configurable, and
  periodic.** `MessageStore.prune()` defaults flipped from *7 days, delete
  everything old* to **30 days, read messages only** — an unread message is
  unconsumed coordination and is never auto-deleted regardless of age. The
  window is a new `coordination.message_retention_days` config knob (`0` =
  keep forever), and the prune now runs daily in the maintenance loop (not
  just at startup). Pass `read_only=False` for the legacy delete-all-old
  behavior.

### Fixes

## [2026.6.20.4] - 2026-06-20

### Features

- **Queen history: reopen + live-refresh** (feature B4, phase 3 of 3 —
  completes B4). The thread-detail modal now lets you act on what you find:
  a **resolved** thread shows a "Reopen & reply" composer that flips it back
  to active and forwards your message to the Queen's PTY in one call (new
  `POST /api/queen/threads/{id}/reopen` + `QueenChatStore.reopen_thread`);
  an **active** thread shows a "View in command center" deep-link instead.
  The tab also **live-refreshes** off the existing `queen.thread` /
  `queen.message` WebSocket events (debounced, only when the Queen tab is
  open), so a thread resolved or posted-to elsewhere moves without a manual
  reload.

### Changes

### Fixes

## [2026.6.20.3] - 2026-06-20

### Features

- **Queen history tab** (feature B4, phase 2 of 3). New "Queen" tab in the
  dashboard bottom panel: a searchable, filterable archive of every Queen
  chat thread (operator chats, oversight findings, escalations, proposals).
  Filter by status (active/resolved), kind, and worker; narrow by date
  range; and search titles + message bodies (debounced). Each row shows a
  kind badge, status, last-activity time, message count, and the associated
  worker/task. "Load more" pages through history. Clicking a thread opens a
  **read-only transcript** of the full conversation. Backs the tab on the
  filterable `/api/queen/threads` from 2026.6.20.2. (Phase 3 — reopen a
  resolved thread, deep-link active threads to the command center, and
  live-refresh on WS events — follows.)

### Changes

### Fixes

## [2026.6.20.2] - 2026-06-20

### Features

- **Queen history backend** (feature B4, phase 1 of 3). `GET /api/queen/threads`
  is now filterable + searchable for the upcoming history tab: new `q`
  (LIKE over thread title **and** message bodies, via an `EXISTS` sub-query
  so each thread returns once), `since`/`until` (on `updated_at`), and
  `offset` params, and each row now carries a `message_count` (batched in
  one query via `QueenChatStore.message_counts`). All new params are
  optional — the command center's existing poll is unaffected.

### Changes

- **Resolved Queen chat threads now have a retention policy.** The
  previously-dormant `QueenChatStore.purge_old()` is wired into the daily
  DB-maintenance loop, governed by a new `queen.queen_thread_retention_days`
  config knob (default **90**; `0` = keep forever). Active threads are
  never purged. Without this the `queen_threads`/`queen_messages` tables
  grew unbounded.

### Fixes

## [2026.6.20] - 2026-06-20

### Features

- **`swarm_query_peers` MCP tool** (feature B11). A worker can now get a
  read-only snapshot of its peers' live state to make an informed handoff
  decision: per running peer (excluding the Queen and the caller) it
  returns state, current task, context %, idle duration, and **queued-task
  count** — so a peer that reads RESTING but has work queued isn't
  mistaken for free. Idle peers sort first. The tool exposes **no** action
  surface: workers still cannot interrupt each other, so to act they use
  `swarm_create_task` or `swarm_send_message`. (`mcp/handlers/_peers.py`.)
  The `swarm_list_my_tasks` half of B11 was intentionally dropped —
  `swarm_task_status(filter='mine')` already covers it.

### Changes

- **`/swarm-status` now reads real peer state** via `swarm_query_peers`
  instead of inferring peer activity from the task board, making good on
  the command's long-standing "peer worker status" promise.
- **`swarm_task_status` description foregrounds `filter='mine'`** so a
  worker looking for "what am I supposed to be doing?" finds its own-tasks
  lookup immediately (discoverability fix; no behavior change).

### Fixes

## [2026.6.13.5] - 2026-06-13

### Features

- **Tunnel auto-restart.** An unexpected cloudflared exit now triggers
  automatic restart with exponential backoff (5s doubling to 80s, 5
  attempts). Exhausted attempts flip the tunnel to ERROR, which fires the
  `tunnel_down` notification from 2026.6.13.3. Previously a cloudflared
  crash silently dropped remote access until the operator noticed.
- **Transient-failure retry for Jira/Graph side effects.** New
  `swarm.integrations.retry.retry_transient` helper (3 attempts,
  exponential backoff on 429/5xx + connection errors/timeouts) applied to
  the one-shot mutating calls where a single 503 lost state permanently:
  Jira transition/comment/assign/create-issue and Graph draft creation.
  Read paths are unchanged — their sync loops already retry by design.

### Changes

### Fixes

## [2026.6.13.4] - 2026-06-13

### Features

- **Task + pipeline lifecycle notifications.** New event types `task_failed`
  (WARNING), `task_reopened` (INFO), `pipeline_started` (INFO), and
  `pipeline_finished` (URGENT when a step failed, INFO otherwise), emitted
  from the TaskManager fail/reopen chokepoints and transition-edged pipeline
  status changes in the engine. Previously only assignment and completion
  notified — failures, the events operators most need, were silent.
- **Daily digest.** New `daily_digest` event type + daemon loop that pushes a
  24h summary (completed/failed/new counts, avg completion time, top
  workers, open-board size) through the notification backends once a day.
  Off unless enabled in the notification matrix. Rendering lives in
  `swarm/notify/digest.py` over the throughput analytics from 2026.6.13.

### Changes

### Fixes

## [2026.6.13.3] - 2026-06-13

### Features

- **Daemon self-health alerting.** New `health_sweep` background loop
  (`src/swarm/server/health.py`) checks disk space on the `~/.swarm` volume
  every 10 minutes (alerts when free space drops below 10% AND 5 GiB) and
  runs `PRAGMA integrity_check` daily, pushing URGENT notifications through
  the existing backends. Alerts are sticky per condition — one notification
  when it trips, re-armed when it clears. New `daemon_health` event type.
- **Tunnel-down notification.** A Cloudflare tunnel ERROR now fires an
  URGENT `tunnel_down` notification — previously it only broadcast to open
  dashboard WebSockets, which is exactly the channel a tunnel-dependent
  operator just lost. Both new event types are selectable in the config
  notification matrix (and included in the Crashes preset).

### Changes

### Fixes

## [2026.6.13.2] - 2026-06-13

### Features

- **`swarm db restore` command.** Restores swarm.db from a backup file (or
  the newest auto-backup in `~/.swarm/backups/` when no argument is given).
  Verifies the backup passes `PRAGMA integrity_check` before touching the
  live file, keeps the replaced database at `swarm.db.pre-restore`, removes
  stale WAL/SHM sidecars, and refuses to run while the daemon holds the
  lock. Closes the backup loop: daily auto-backups existed since the
  maintenance loop landed, but recovery still required hand-copying files.

### Changes

- **Backup failures now log at WARNING.** Both the daily DB backup
  (`_db_maintenance_loop`) and the 30-minute task-state backup
  (`_backup_loop`) logged failures at DEBUG — invisible at the default
  operator log level despite being data-safety regressions.

### Fixes

## [2026.6.13] - 2026-06-13

### Features

- **STUNG crash diagnostics on the worker card.** A dead worker now shows its
  exit code ("Exited with code 137") and a collapsible "Last output" tail of
  the final PTY lines, so revive-crash loops are diagnosable from the
  dashboard without terminal access. New `crash_tail` / `exit_code` fields on
  `Worker.to_api_dict()`, populated only in STUNG.
- **Task throughput analytics.** New `GET /api/analytics/summary?days=N`
  aggregates created/completed/failed counts, completions per day, avg +
  median completion time, per-worker stats, and a current backlog snapshot
  (`swarm.analysis.throughput.compute_throughput`).
- **Bulk "All" selector.** Bulk-select mode gained an All button that selects
  every task visible under the current status/priority/search filters.
- **Message + learning cleanup endpoints.** `POST /api/messages/delete`
  (delete by ids) and `GET /api/queen/learnings` +
  `DELETE /api/queen/learnings/{id}` for pruning stale Queen corrections.

### Changes

- **Active bottom-panel tab persists across reloads** (sessionStorage),
  joining the existing selected-worker / focus-mode / filter persistence.
- **Destructive-action confirmations unified on the themed dialog.** Bulk
  reassign and pipeline delete now confirm before firing; the install-update
  and kill-Queen flows swapped native `confirm()` for `showConfirm()`.
- **OpenAPI spec refreshed** to the current route surface: removed the dead
  `/api/queen/coordinate`, added queen threads/learnings, playbooks,
  analytics summary, task force-complete, and message delete.

### Fixes

- `tests/test_testing_report.py` infra-section test shelled out to a real
  `claude -p` (missing the `_mock_analysis()` guard every sibling test uses)
  and hung in sandboxed environments.

## [2026.6.11] - 2026-06-11

### Features

### Changes

### Fixes

- **IdleWatcher fired AUTO_NUDGE at workers that weren't actually idle.** The
  `RESTING`/`SLEEPING` `display_state` gate was the *only* idleness check, so
  two false-idle cases slipped through: (1) a worker the operator was actively
  typing in, and (2) a worker mid a long *quiet* foreground command (e.g.
  `gh run watch` on a deploy) whose state momentarily classified `RESTING`
  because detection keys off output-quiet time. Both now suppress the nudge
  (logged as `AUTO_NUDGE_SKIPPED`): a new shared `operator_engaged()` helper
  reuses the affinity-router's `assign_operator_engagement_minutes` window, and
  a new `WorkerStateTracker.worker_has_active_turn()` re-reads the **live** PTY
  for a mid-turn signal instead of trusting the cached `display_state`. The same
  guard is applied before the task-lifecycle `PROPOSED_COMPLETION` path
  (parity). Genuinely idle workers with an unstarted active task are still
  nudged.

## [2026.6.10.4] - 2026-06-10

### Features

- **Dev-only auto-reload on build change.** When the dev daemon restarts on a
  changed build, the dashboard tab now reloads itself on WebSocket reconnect
  instead of silently running stale cached JS/CSS — ending the "hard-refresh
  after Reload" trap. Keys off `build_sha` (which hashes the source tree, so it
  changes on committed *and* uncommitted edits) embedded in the page vs.
  `/api/health` on reconnect. Gated strictly on `is_dev` (`termDebug`): the
  Reload button and the production auto-update flow (`waitForRestart`) already
  reload, and production users must never get a surprise reload.

### Changes

### Fixes

## [2026.6.10.3] - 2026-06-10

### Features

### Changes

### Fixes

- **Queen-view mixed render (real root cause).** Selecting "Queen Dashboard"
  while a worker was focused left the worker terminal stacked over the Queen
  panel. The 2026.6.10.2 CSS-only attempt was insufficient: `showTermEntry`
  sets an inline `display: flex` on `#detail-body` when a worker terminal
  mounts, which beat the non-`!important` `body.cc-active #detail-body` rule.
  Fixed by (1) adding `!important` to the cc-active panel-visibility rules
  (mirroring the existing `#terminal-actions` precedent), and (2) detaching the
  active worker terminal in `show()` via a now-exposed `hideActiveTermEntry()`
  (which also clears the inline styles) before mounting the Queen embed.

## [2026.6.10.2] - 2026-06-10

### Features

- **Worker mass-broadcast gate (task #647).** Workers can no longer broadcast a
  swarm-wide directive or claim operator authority unchecked. A deterministic
  gate (`messages/broadcast_gate.py`) on `swarm_send_message` blocks before
  delivery: operator-authority claims ("OPERATOR DIRECTIVE", "Brad said",
  "standing policy") gate at any recipient count; directive/policy language
  ("everyone should", "all workers must") gates on fan-out (`*`). Coordination
  about the sender's OWN concrete change passes. A block escalates to the
  operator (Attention card + `BROADCAST_GATED` buzz entry) and fires a
  fire-and-forget headless-Queen provenance analysis. The Queen's own sends are
  exempt. Deterministic by design — injection-proof where an LLM gate is not;
  the Queen runs only as async enrichment (MCP handlers are synchronous).

### Changes

### Fixes

- **Dashboard mixed-render after reload.** `#command-center` and `#detail-body`
  both rendered visible in the markup with no default `display:none`, so the
  correct single-panel view existed only after JS reconciled — and an
  `os.execv` reload race could leave BOTH painted (a worker terminal *and* the
  Queen panel). Visibility is now driven solely by the `body.cc-active` class
  via CSS; the inline-display juggling in `show()`/`hide()`/`init()` is gone.
- **Handoff fan-out: one directive is not N tasks (task #647 part 5).** An
  all-workers handoff fanned to N idle recipients spawned one near-identical
  task row each (the #638-645 incident — one directive shown as 8 "tasks on
  many workers"). `spawn_handoff_task` now dedups by title against open tasks,
  collapsing to a single tracked task; other recipients still get a watcher
  nudge.

## [2026.6.10] - 2026-06-10

Triage of the 2026-06-09 Claude Code Insights report → three Queen/dispatch improvements.

### Features

- **Environmental-causes nudge for bug-fix tasks.** `build_task_message` now
  prepends a "rule out stale/dev data, file locks, missing env vars before
  assuming a code bug" preamble to `TaskType.BUG` dispatches (ordered inside the
  plan-mode preamble, which stays outermost). Scoped to bug tasks only so
  feature/chore/verify work isn't nagged.
- **Queen rejection memory (inform-first).** The headless Queen's escalation
  context now includes recent operator rejections for the worker, so she
  declines to re-propose actions already refused — addressing cross-session
  repeats. Surfaced via a new `recent_rejected_escalations` store query, fed
  into `gather_context` with state-change staleness (a rejection from a state
  the worker has since left is dropped). Logged as `QUEEN_REJECTION_CONTEXT`.

### Changes

- **Pre-call proposal dedup.** `QueenAnalyzer.analyze_escalation` now short-
  circuits the headless Queen call when the resulting proposal would only be
  dropped downstream (operator focused on the worker, or a matching escalation
  already pending) — avoiding a wasted `claude -p` invocation. `is_focused` is
  shared with `ProposalManager` (its `_is_focused` is now public).

### Fixes

- **Revived dead rejection-feedback wiring.** `_rejection_feedback_section` in
  `queen/context.py` was never fed (`proposal_history` had no caller), so the
  headless Queen received no rejection memory at all. Now wired, and the section
  renders escalations correctly (worker + rule_pattern + reason, not the empty
  `task_title` it previously emitted).

## [2026.6.8.3] - 2026-06-08

### Features

### Changes

### Fixes

- Mobile dashboard: the "↓ Jump to bottom" pill no longer crowds the composer /
  command-bar zone. Same coarse-pointer `:has(.mobile-send-bar.visible)` pattern
  as the d-pad raise — the pill lifts to clear the composer (bottom 68px) while
  the d-pad stays above it. Touch-only; no desktop impact.

## [2026.6.8.2] - 2026-06-08

### Features

- Mobile composer: the touch send-bar input is now a multi-line auto-growing
  `<textarea>` (was a single-line `<input>`), so native autocorrect and voice
  dictation work properly — the raw xterm keystroke path doesn't get them.
  Enter sends, Shift+Enter inserts a newline; it auto-grows up to ~5 lines then
  scrolls, and is styled as the primary, obvious touch input (so composing here
  is the default over tapping straight into the terminal). Purely additive — the
  direct-terminal keystroke path, the d-pad, the command buttons, and skills all
  keep working unchanged. No desktop impact (the composer is touch-only).

### Changes

### Fixes

## [2026.6.8] - 2026-06-08

### Features

### Changes

### Fixes

- Mobile dashboard: the round terminal d-pad no longer overlaps the mobile
  composer (send-bar) text input. A coarse-pointer rule lifts the d-pad clear of
  the send bar whenever it's visible (`.detail-area:has(.mobile-send-bar.visible)
  .term-dpad`), stacking higher when the jump-to-bottom pill is also up. Touch-
  only; no desktop impact (the d-pad and send bar are both mobile-only).

## [2026.6.6.14] - 2026-06-06

### Features

### Changes

- Playbooks: extracted the duplicated headless-Queen invocation from the
  synthesizer and consolidator into a shared `playbooks/_queen.py`
  (`run_queen_json` + the `QueenLike` protocol), so the cancellation-reraise +
  error-log-and-bail semantics live in one place.

### Fixes

- Playbooks: the LLM-generated playbook `body` is now capped at `MAX_BODY_LEN`
  (8000) in both the synthesizer and the consolidator merge — consistent with
  the existing name/title/trigger caps — so a malformed/runaway Queen response
  can't bloat the DB or the rendered `SKILL.md`.
- Playbooks: typed the `PlaybookConsolidator._maybe_merge(a, b)` parameters as
  `Playbook` (were untyped).

## [2026.6.6.13] - 2026-06-06

### Features

### Changes

### Fixes

- Feedback redaction: activated the dormant env-value scrub. The collector now
  extracts `$VAR` references from the config (`_config_env_refs`) and threads
  `env_refs` into every `redact_text` call, so the live values of
  config-referenced env secrets are scrubbed from the logs/drone-events/config
  attachments before a report is sent to GitHub. (The scrub existed in
  `redact.py` but `collect_attachments` never passed `env_refs`.)
- Feedback redaction: added webhook-URL patterns — Slack/Discord (token in the
  path) and a generic secret query-param scrubber (ntfy `?auth=`, `?token=`,
  etc.) — so a configured webhook URL's token no longer survives into the
  config dump.
- Feedback `gh` submit: the no-label retry path (`_submit_without_label`) now
  wraps its subprocess in the same try/except as the main path, so a timeout /
  OSError surfaces as a clean `GhSubmitError` instead of an uncaught exception.

## [2026.6.6.12] - 2026-06-06

### Features

### Changes

- Notify: removed the dead `osc777_backend` (never wired to config or the bus);
  `make_webhook_backend` return type is now `Callable[[NotifyEvent], None]`;
  hoisted desktop.py's per-function `threading` imports to module scope.

### Fixes

- Notify: the email (`smtplib`) and webhook (`urllib`) backends no longer block
  the daemon's async event loop. `bus.emit()` dispatches backends synchronously,
  so a slow/hung SMTP or webhook server previously froze the whole daemon (WS
  broadcasts, polling) for the backend's timeout. Both now run their blocking
  send on a daemon thread via `notify._util.run_detached` (matching desktop's
  existing offload).
- Notify: a failed webhook POST now logs only `scheme://host` — the configured
  URL can embed a token in its path (Slack/Discord) or query (ntfy), which must
  not land in the logs.

## [2026.6.6.11] - 2026-06-06

### Features

### Changes

- Hooks installer: removed two orphan shell scripts (`complete_task_hook.sh`,
  `cross_task_hook.sh`) — never installed (replaced by the
  `swarm_complete_task` / `swarm_create_task` MCP tools; the installer only
  legacy-*removes* them by name).

### Fixes

- Hooks installer: writes to a worker's `.claude/settings.json` and `.mcp.json`
  are now atomic (temp file + `os.replace` via `_atomic_write_text`) — a crash
  mid-write previously truncated/corrupted the worker's CC config; for `.mcp.json`
  that meant losing the `?worker=` identity (the worker would then resolve as
  `unknown` at the MCP server).
- Hooks installer: the two silent `except … : pass` blocks (preserving the
  `?worker=` param from an existing `.mcp.json`, and reading the MCP port from
  config) now log at debug instead of swallowing — a malformed `.mcp.json` that
  drops worker identity is no longer invisible.

## [2026.6.6.10] - 2026-06-06

### Features

### Changes

- Auth audit: extracted the byte-identical OAuth token-response/error parsing
  shared by `JiraTokenManager` and `GraphTokenManager` into `auth/_oauth.py`
  (`apply_token_response` / `parse_token_error`), so the two managers can't
  drift.

### Fixes

- Auth: a token-endpoint 200 with no `access_token` is no longer treated as
  success (it set `None` but returned True → silent auth failure); both OAuth
  managers now fail cleanly. A non-numeric `expires_in` falls back to 3600
  instead of raising an uncaught `TypeError`.
- Auth: `get_token()` now serializes refresh with an `asyncio.Lock` + re-check,
  so two concurrent callers can't both refresh — a rotated refresh token (e.g.
  Atlassian) would otherwise invalidate the second.
- Auth: `JiraTokenManager.disconnect()` / `GraphTokenManager.disconnect()` log
  at WARNING when clearing the secret store fails, instead of silently
  swallowing the error.

## [2026.6.6.9] - 2026-06-06

### Features

### Changes

### Fixes

- (#614) Inter-worker message nudges no longer churn forever on an unread
  message a worker won't clear. The repeat-nudge guard's "progress" fingerprint
  is now the **set of unread message ids**, not the worker's display-state —
  so a recipient that *responds* to a nudge (oscillating RESTING↔SLEEPING↔
  BUZZING) no longer resets the escalate-and-quiet streak every sweep. A stale
  unread message now reaches `idle_nudge_max_repeats` → escalates to the
  operator once → goes silent, instead of re-nudging indefinitely (the aria
  case: 72 nudges over 22h on one informational `finding`). The idle-watcher
  task-nudge path is unchanged.

## [2026.6.6.8] - 2026-06-06

### Features

### Changes

- (#611 P5) Web task routes no longer write `task.status` raw. The create
  action refuses to author a task straight into ACTIVE/BLOCKED/ASSIGNED
  (ACTIVE must go through the `activate()` chokepoint, BLOCKED via the blocker
  flow, ASSIGNED via the worker-assign branch) — only backlog/unassigned/done/
  failed lane authoring is allowed. The Backlog→Unassigned "promote / Hand to
  Queen" transition (edit-modal dropdown + promote button) now routes through
  the guarded `board.approve_task()` instead of a raw `task.approve()`.

### Fixes

## [2026.6.6.7] - 2026-06-06

### Features

### Changes

### Fixes

- (#611 P4) Defense in depth: `TaskBoard._persist` now self-heals a
  >1-ACTIVE-per-worker state before it can reach disk — any mutation path that
  bypasses the `activate()` chokepoint (now or in future) is collapsed to the
  earliest-started task and logged at WARNING naming the offender. A
  double-active can no longer be persisted silently regardless of how it was
  produced.

## [2026.6.6.6] - 2026-06-06

### Features

### Changes

- (#611 P3) `board.activate()` is now the single ACTIVE chokepoint. It demotes
  any other ACTIVE task for the worker (INV-1), starts this one (stamps
  `started_at`), persists + notifies, and returns the demoted ids (or `None` if
  not startable). `start_task` and the state-tracker's BUZZING promotion both
  route through it instead of each hand-rolling demote + `task.start()` — so the
  drone path now persists + notifies (it previously did neither). Removed the
  now-orphaned `board.demote_other_active()`.

### Fixes

## [2026.6.6.5] - 2026-06-06

### Features

### Changes

- (#611 P2) `_recon_inv1` / `reconcile_active_per_worker` now keep the
  **earliest-started** ACTIVE task per worker and demote the rest, instead of
  keeping newest-by-`updated_at`. `updated_at` bumps on any edit, so the old
  rule could demote a long-running in-flight job (it would have demoted #604's
  27k-record run rather than the newer #605). Each demotion logs at WARNING
  with both task numbers. New `tasks.started_at` field (set in `task.start()`,
  persisted via a v14 schema migration; legacy/NULL rows fall back to
  `created_at`).

### Fixes

## [2026.6.6.4] - 2026-06-06

### Features

### Changes

- (#611 P1) Added a periodic invariant-reconcile sweep so INV-1/INV-2 are healed
  on a timer, not only on worker state changes. The reactive trigger fires only
  when a worker *leaves* a working state, so a >1-ACTIVE violation created while
  a worker stays BUZZING previously persisted until it idled or the daemon
  restarted (platform #604/#605 lasted ~1.5h that way). New daemon loop
  `_invariant_reconcile_loop` runs `reconcile_invariants` every
  `DroneConfig.reconcile_interval_seconds` (default 90s; 0 disables; floored at
  15s). Cheap — only writes when a violation actually exists.

### Fixes

## [2026.6.6.3] - 2026-06-06

### Features

### Changes

### Fixes

- INV-1 (one IN-PROGRESS task per worker, #405) was bypassable by the
  state-tracker drone: on a worker → BUZZING transition it promoted **every**
  ASSIGNED task for that worker to ACTIVE (raw `task.start()` in a loop — no
  one-active cap, no `demote_other_active`, no STARTED history). A worker with
  two assigned tasks going BUZZING ended up with two IN-PROGRESS tasks (the
  platform #604/#605 violation). Now promotes at most one: if the worker
  already has an ACTIVE task it promotes nothing, otherwise it promotes the
  single most-recently-updated ASSIGNED task (`_promote_one_assigned`).

## [2026.6.6.2] - 2026-06-06

### Features

- Force-close capability for wedged BLOCKED tasks (#609 follow-up). A task
  stuck in BLOCKED could not be closed through any normal path
  (`complete` / reassign / `queen_force_complete_task` all require
  ASSIGNED/ACTIVE) — #574 had to be unstuck via a fragile
  fail→reopen→approve→assign→complete chain. New clean path:
  `complete_task(force=True)` clears the task's blocker rows
  (`BlockerStore.clear_for_task`) and completes from any non-terminal status
  via `board.force_complete`, reusing all the normal completion side-effects.
  Exposed two ways: `queen_force_complete_task` now force-closes BLOCKED tasks
  (was a no-op against them), and a new operator endpoint
  `POST /api/tasks/{id}/force-complete`.

### Changes

### Fixes

- `swarm_report_blocker` now rejects blocker filings that would close a CYCLE
  (A→B→A or longer), not just direct self-blocks. `BlockerStore.would_create_cycle`
  walks the blocker graph; a filing where `blocked_by` already waits on
  `task_number` (directly or transitively) is refused before the write — it
  would wedge every task in the ring in BLOCKED with no terminal task to fire
  the auto-clear. (#609 follow-up)

## [2026.6.6] - 2026-06-06

### Features

### Changes

### Fixes

- `swarm_report_blocker` now rejects a self-referential blocker
  (`task_number == blocked_by_task`) with a clear, actionable error instead of
  persisting it. A self-blocker never auto-clears (its `blocked_by` never
  reaches a terminal status), which wedges the task in BLOCKED permanently —
  and a BLOCKED task is uncloseable through the normal API (`complete`,
  `queen_force_complete_task`, and reassign all refuse it). Task #574
  deadlocked exactly this way; this guard makes the deadlock impossible to
  create. (#609 PART B)

## [2026.6.2.2] - 2026-06-02

### Features

### Changes

- Jira sync: consolidated the seven copy-pasted API-error `except` handlers
  (`last_error` / `errors++` / log) into a single `JiraSyncService._record_error`
  helper.
- Jira ADF→markdown extractor: hoisted three per-issue regexes to module level
  (matching the existing `_SAFE_FILENAME_RE` convention); typed the
  `uploads_dir` parameter as `str | Path` instead of bare `object`.

### Fixes

- Jira ADF import no longer silently drops inline `status` and `date` nodes —
  a status badge's label (`attrs.text`) and a date node's epoch-ms timestamp
  carry no content children, so the generic fallback walker discarded them.
  They now render as the badge label and an ISO `YYYY-MM-DD` (UTC) date.
- Jira JQL: the `import_label` value is now escaped before interpolation, so a
  label containing a `"` or `\` can't break out of the query string literal.
- Jira status export: added the missing `blocked` → `In Progress` mapping to
  the default `status_map` (both `config/models.py` and `config/loader.py`).
  Exporting a `BLOCKED` task previously hit an empty mapping and silently
  no-opped.

## [2026.6.2] - 2026-06-02

### Features

### Changes

- Resource monitor `/proc` walk de-duplicated: `top_workers_by_rss` and
  `find_dstate_descendants` now share a single `_parse_proc_stat_map` walk +
  `_walk_descendants` helper (removed the copy-pasted parse in three places and
  the dead `_get_descendants`). Dropped the unused `enabled` param from
  `take_snapshot`.

### Fixes

- The resource-monitor loop no longer re-reads `/proc/vmstat` synchronously on
  the event loop each tick — `take_snapshot` already captures the cumulative
  swap counters inside its worker thread, and the loop now carries those
  forward (counters ride `ResourceSnapshot` as internal fields, excluded from
  `to_dict`).
- Added a positive test for D-state detection (`find_dstate_descendants` with a
  mock `/proc` containing a `state=D` descendant) — previously only empty-input
  and a no-op error-path test existed (the latter monkeypatched a function the
  code didn't call).

## [2026.6.1] - 2026-06-01

### Features

### Changes

- `webhook_notify` no longer mutates the caller's config dict — it copies
  `config["headers"]` before `setdefault`-ing `Content-Type` (the shared dict
  is reused across pipeline runs).

### Fixes

- **`file_uploader` no longer blocks the event loop or hangs forever.** The
  upload-file `read_bytes()` and credentials `read_text()` ran synchronously
  inside `async execute()` (a large upload file stalled the whole daemon loop)
  — both now run via `asyncio.to_thread`. Added an `aiohttp.ClientTimeout` to
  the upload session so a hung Google API call can't hang the step indefinitely.
- Added `tests/test_service_executors.py` covering the previously-untested
  `ShellCommand` (success / non-zero exit / missing command / timeout) and
  `WebhookNotify` (success / HTTP error / missing url / no-config-mutation)
  service handlers.

## [2026.5.31.14] - 2026-05-31

### Features

### Changes

- Pipelines DRY: extracted `_get_pipeline_or_raise` / `_get_step_or_raise`
  (deduped 7 + 4 copy-pasted lookup blocks in `engine.py`), and the engine now
  reuses `schedule.normalize_schedule` instead of an inlined copy of the legacy
  HH:MM→cron logic (drops the duplicate `_LEGACY_HHMM` regex). `fail_step` now
  returns `list[PipelineStep]` like its sibling step methods.

### Fixes

- **A malformed pipeline no longer hangs silently.** A pipeline with a circular
  (`a↔b`) or missing (`depends_on=["ghost"]`) dependency previously started
  `RUNNING` and stuck forever with no runnable step (never DONE/FAILED).
  `start_pipeline` now calls `Pipeline.validate_dependencies()`, which raises
  `ValueError` (→ clean 400 via the route's `@handle_errors`) on a missing or
  circular dependency. Added regression tests for both.
- `pipeline_from_dict` no longer crashes on an explicit `"depends_on": null` in
  stored JSON (coerces to `[]` instead of passing `None` into `ready_steps`).

## [2026.5.31.13] - 2026-05-31

### Features

### Changes

- Providers DRY: `gemini`/`codex`/`opencode` now use the `TAIL_WIDE` constant
  instead of a hardcoded `30`, and the identical codex/opencode safe-tool regex
  is shared from `base.SHELL_STYLE_SAFE_PATTERNS` (was copy-pasted in both).
- Rate-limit detection (`claude._RE_RATE_LIMIT`) is now case-insensitive
  (`re.IGNORECASE`) so a non-title-case banner still trips it.

### Fixes

- **A tuned Claude no longer silently loses dynamic-workflow detection and
  `/goal` support.** `TunedProvider` delegated 24 methods to its inner provider
  but missed `is_long_running_tool_active` and `supports_native_goal` — both
  default to `False` in the base, so `TunedProvider(ClaudeProvider, …)` shadowed
  Claude's overrides: the worker looked idle mid-workflow (false nudges) and
  `/goal` seeding was skipped. Added both delegations + contract tests proving a
  tuned Claude keeps both.

## [2026.5.31.12] - 2026-05-31

### Features

### Changes

- Aligned `MessageStore._SCHEMA` (the standalone/test `messages.db` path)
  with the canonical `messages` table in `db/schema.py` — same indexes
  (recipient, unread, dedup, created_at) so the two definitions can't drift
  and the standalone path isn't silently missing the dedup index that
  `send()`/`broadcast()` rely on.

### Fixes

- **The `messages` table no longer grows unbounded.** `MessageStore.prune()`
  (7-day retention) existed but was never called — unlike the buzz log, which
  is pruned on startup. Wired `message_store.prune()` into `daemon.start()`
  alongside `drone_log.prune_store()`. Added prune regression tests.
- `MessageStore.prune()` now logs at WARNING with `exc_info` on a SQLite error
  (it was the one method here that swallowed errors silently).

## [2026.5.31.11] - 2026-05-31

### Features

### Changes

- **DB schema v13: two indexes for the Queen's triage scans.** Added
  `idx_buzz_category_time` on `buzz_log(category, timestamp)` (serves the
  drone-actions view) and `idx_messages_created_at` on `messages(created_at)`
  (serves the message-stream view) — both tables grow unbounded and were
  previously range-scanned. Applied to fresh DBs (schema) and existing DBs
  (v12→13 migration); verified both paths produce identical schemas.
- Refactored `SwarmDB._apply_migrations` into a data-driven `(version, fn)`
  registry (behaviour-identical; lower complexity; trivial to extend).

### Fixes

- Added `tests/test_db.py::TestSchemaConsistency` — a fresh-vs-migrated
  divergence guard that introspects every migration `ADD COLUMN` / `CREATE
  INDEX` and asserts each exists in the fresh-create schema. Catches the most
  dangerous DB bug class (a migration column/index not mirrored into the fresh
  DDL) automatically for all future migrations.

## [2026.5.31.10] - 2026-05-31

### Features

### Changes

- Removed the unused `TaskDict` TypedDict from `tasks/task.py` (nothing imported
  or returned it). Deduplicated the ACTIVE-tasks-by-worker grouping shared by
  `_recon_inv1` and `reconcile_active_per_worker` into `_group_active_by_worker`.

### Fixes

- **Legacy `FileTaskStore` round-trip fidelity.** `tasks/store.py` dropped
  `block_reason` and the verifier fields (`verification_status`/`reason`/
  `reopen_count`) on save/load — so the `swarm test` task store (and any
  `FileTaskStore` fallback use) silently lost that state. Wired all four through
  `_task_to_dict`/`_dict_to_task` so it faithfully matches the production
  `SqliteTaskStore`. (Production was already lossless — verified empirically.)
- The file `ProposalStore` (`tasks/proposal.py`) now persists `rejection_reason`
  through `_serialize`/`_deserialize`.
- Added `tests/test_store.py::test_every_field_survives_roundtrip` — a generic
  guard that introspects the SwarmTask dataclass and fails if any field is
  dropped on a FileTaskStore round-trip, preventing future drift.

## [2026.5.31.9] - 2026-05-31

### Features

### Changes

- Deduplicated `Worker.update_state` / `Worker.force_state`: the shared
  state-reset block (revive-count reset, state/state_since, confirmation
  counters, api-dict cache) is now a single `_apply_state_transition` helper.
  Behaviour-preserving.

### Fixes

- Added test coverage for two previously-untested edge paths: `cache_read_ratio`
  (including its division-by-zero guard) and the Queen's `display_state`
  never-SLEEPING exemption (with a non-Queen control).

## [2026.5.31.8] - 2026-05-31

### Features

### Changes

- Removed the deprecated `terminal.replay_max_bytes` config field (the loader
  already ignored it; old configs still parse with a deprecation notice).

### Fixes

- **Config no longer silently dropped on save (round-trip fidelity).** A full
  serialize → save → load round-trip was losing operator config: **10
  `DroneConfig` fields** (`context_warning_threshold`, `context_critical_threshold`,
  `speculation_enabled`, `idle_nudge_max_repeats`, `native_goal_enabled`,
  `native_goal_max_turns`, `user_request_plan_mode`, `dreamer_interval_seconds`,
  `dreamer_lookback_hours`, `dreamer_min_pattern_count`), the **entire
  `resources` section** (never serialized), and the **`sandbox` section** (never
  loaded or serialized, despite being consumed by `hooks/install.py`). Wired all
  of them through `serialization.py`, `loader.py`, and the known-keys allowlist
  so values set in `swarm.yaml` persist instead of reverting to defaults.
- Added `tests/test_config.py::TestEveryScalarFieldRoundTrips` — a generic guard
  that introspects every nested config dataclass and fails if any scalar field
  doesn't survive a round-trip, so future fields can't be silently dropped.

## [2026.5.31.7] - 2026-05-31

### Features

### Changes

- Type safety: aligned `ProcessPool._send_cmd`/`_dispatch_message` to
  `dict[str, Any]` to match the `_SendCmd` protocol alias (was `dict[str, object]`).

### Fixes

- **PTY holder read loop is more resilient.** `_on_pty_readable` re-raised any
  `OSError` other than EIO/EBADF straight out of the asyncio `add_reader`
  callback — which leaves the reader registered and re-fires in a tight loop.
  It now treats `EAGAIN`/`EWOULDBLOCK` as a spurious wakeup (retry) and, on any
  other unexpected error, logs at WARNING and removes the reader (mirroring the
  EOF path) instead of spinning.
- **Process-control failures are no longer silent.** `write_to_worker`,
  `signal_worker`, and `resize_worker` swallowed `OSError`/`ProcessLookupError`
  with a bare `return False`; they now log at WARNING with `exc_info` per the
  ops-visibility rule. Normal write backpressure stays quiet (it's handled
  separately via `BlockingIOError`), so this doesn't spam.
- Added `tests/test_command_handler.py` — unit coverage for the holder's JSON
  command dispatcher (dispatch routing, spawn/write/signal/resize validation,
  snapshot), previously only exercised indirectly through the socket protocol.

## [2026.5.31.6] - 2026-05-31

### Features

### Changes

- Type safety in `mcp/queen_handlers`: `_clamp`'s `value` is now
  `int | str | float | None` (was `Any`) and `_fire_async`'s `coro` is
  `Coroutine[Any, Any, None]` (was `Any`). Removed the unused `ErrorContent`
  alias from `mcp/types.py`.

### Fixes

- **`swarm_batch` no longer breaks on structured sub-tools.** A batched op
  whose handler returns a `StructuredResponse` dict (e.g. `swarm_task_status`)
  hit `op_result[0]` in `handlers/_batch.py` → `KeyError: 0`, surfacing as a
  useless "Error: 0" for the whole batch (after earlier ops' side effects had
  already applied). The batch loop now normalizes the dict/list result shape
  the same way `handle_tool_call` does internally. Regression test added.

## [2026.5.31.5] - 2026-05-31

### Features

- **Mobile D-pad for worker terminals.** Each worker terminal now shows a
  floating directional pad (↑ ← → ↓) with a **center Enter circle** in the
  bottom-right corner, on mobile (`≤768px`) only. Arrows send
  `/action/arrow-{up,down,left,right}/{worker}`; the center circle sends
  `/action/continue/{worker}` (Enter). It rides above the jump-to-bottom pill
  when that's showing and drops into the pill's corner otherwise. Each button
  targets its own terminal's worker (correct for worker views and the Queen
  embed) and does not refocus the terminal, so it won't pop the soft keyboard.

### Changes

### Fixes

## [2026.5.31.4] - 2026-05-31

### Features

### Changes

- **Server audit — attention queue de-N+1'd.** `GET /api/attention` (polled by
  the dashboard) previously queried the blocker store once per worker, ran two
  buzz-log queries per STUNG worker, and fetched up to 500 messages per thread
  just to read the latest line. Added `BlockerStore.active_worker_names()` (one
  `SELECT DISTINCT`) and `QueenChatStore.latest_message()` (one row), and
  batched the buzz lookups into two action-scoped queries. Behaviour-preserving.

### Fixes

- **Pipeline routes now map errors to clean HTTP statuses.** All 14
  `routes/pipelines.py` handlers were missing `@handle_errors`, so bad input
  (`request.json()`, `int(count)`, `StepType(...)`) surfaced as raw 500s
  instead of 400s. Decorated them, and hardened `handle_errors` to re-raise
  `web.HTTPException` (e.g. a handler's own `503`) instead of masking it as a
  500.
- Removed a doubled `@handle_errors` decorator on `handle_search_task_history`
  (`routes/tasks.py`).
- Type safety: typed `_task_dict`/`_task_full_dict` params as `SwarmTask`
  (`routes/tasks.py`) and `_record_tool_activity`'s `worker` as `Worker`
  (`routes/hooks.py`). Added `tests/test_attention_routes.py` covering the
  batched gather helpers.

## [2026.5.31.3] - 2026-05-31

### Features

### Changes

- **Drones audit — type safety.** Filled in missing/bare type annotations
  across the drones module (explicit-types rule): `idle_watcher`
  (`_active_blocker -> Blocker | None`, `_on_auto_clear(b: Blocker)`,
  `drone_config: DroneConfig`, `active: list[SwarmTask]`),
  `inter_worker_watcher` (`drone_config: DroneConfig`), `backoff`
  (`workers: list[Worker]`), `rules` (`is_user_question_fn: Callable[[str],
  bool]`), `task_lifecycle` (Queen-assignment list → `list[dict[str, Any]]`),
  and `verifier` (`buzz_entries: list[DroneEntry | SystemEntry]`).

### Fixes

- Added `tests/test_directives.py` covering `DirectiveExecutor`'s static
  prompt-detection helpers (`has_operator_text_at_prompt`,
  `has_pending_bash_approval`, `has_idle_prompt`) — previously only mocked,
  never exercised against the real regex/substring logic.

## [2026.5.31.2] - 2026-05-31

### Features

### Changes

- **Queen audit — JSON-extraction dedupe.** The headless Queen and the
  verifier previously each carried their own copy of the `claude -p`
  JSON-extraction logic (`_JSON_FENCE_RE` + plain/fenced/balanced-brace
  parsing). Consolidated into a single shared `swarm/queen/json_extract.py`;
  `queen.py` and `verifier.py` now import it (also tightens verifier's bare
  `dict` return to `dict[str, Any]`).

### Fixes

- **Queen session persistence no longer swallows DB errors silently.**
  `_save_to_db` / `_load_from_db` / `_clear_from_db` in `queen/session.py`
  caught all exceptions and returned without logging — a silent failure mode
  for Queen session continuity. They now log at WARNING with `exc_info`
  (ops-visibility rule). Removed a dead `field(default_factory=list)`
  assignment in `OversightMonitor.__init__` (immediately overwritten). Added
  unit tests for the previously-untested
  `OversightMonitor.check_resource_pressure` heuristic.

## [2026.5.31] - 2026-05-31

### Features

### Changes

- **Audit remediation — type safety.** Replaced `Any`/untyped parameters in
  the playbooks module (`synthesizer.py`, `consolidator.py`) with concrete
  types (`SwarmTask`, `Playbook`, `SystemLog`, `Callable[[], float]`), and
  tightened bare `dict`/`list` annotations to `dict[str, Any]` / `list[...]`
  across `config/loader.py`, `hooks/install.py`, `cli.py`, `tasks/cross_task.py`,
  `tasks/proposal.py`, `db/proposal_store.py`, `drones/store.py`,
  `testing/operator.py`, and `web/routes/partials.py`.

### Fixes

- **Proposal-existence hot-path no longer fetches the full pending list.**
  Added `ProposalStore.has_pending()` / `SqliteProposalStore.has_pending()`
  (a `SELECT 1 … LIMIT 1` / `any(...)` existence check) and wired the
  poll-loop gate (`daemon.set_pending_proposals_check`) to it, instead of
  building and discarding a full list of deserialized proposals on every
  send-message / assign-task decision.
- Added 18 unit tests covering previously-untested Jira pure helpers
  (`_format_comment_author`, `_format_comment_timestamp`, `_truncate`,
  `_build_synced_description`).

## [2026.5.30.2] - 2026-05-30

### Features

### Changes

- **The embedded Queen's quick-action bar now reuses the worker
  `action_buttons` config** (the one on the advanced config tab) instead of the
  separate `queen_action_buttons` section added in 2026.5.30 — so the Queen
  matches the workers and is managed in one place. The separate
  `queen_action_buttons` config (model, loader, serialization, known-keys, DB
  store, server applier, package exports) is removed. The Queen bar renders
  from `action_buttons` with the same `btn btn-{style}` styling; each worker
  action is routed to the Queen via the explicit-name `ccQueen*` handlers
  (revive/kill → `ccQueenVerb`, refresh → `ccQueenRefresh`, export → new
  `ccQueenExport`, custom command → `ccQueenSend`, blank → continue). The
  "Ask Queen" action is skipped on the Queen herself (asking the Queen to ask
  the Queen is circular).

### Fixes

- **Active workers no longer shown RESTING/SLEEPING while mid-turn (state
  misclassification).** Claude Code's interruptible-turn footer
  "… · esc to interrupt" **truncates to "… · esc to…"** at narrow PTY widths
  (observed live on `my-rcg` / `budgetbug`, Claude Code v2.1.158). The state
  classifier keyed off the full literal, so when an active worker's animated
  spinner glyph wasn't on-screen at poll time (between animation frames or
  while a tool result rendered), it fell through to RESTING and flickered
  BUZZING↔RESTING. The classifier now matches a truncation-tolerant interrupt
  hint (`_RE_INTERRUPT_HINT`: `esc to interrupt` / `esc to stop` / truncated
  `esc to…`) in both the text and styled paths and in the stuck-BUZZING safety
  net. The hint is interrupt-specific (it must NOT match choice-menu footers'
  "Esc to cancel") and, on the styled path, must be dim-styled (a non-dim
  "esc to interrupt" is pasted text, not the live footer). Idle auto-mode
  footers show "· ← for agents" / "· ? for shortcuts" (never "esc to"), so idle
  workers stay RESTING.

## [2026.5.30] - 2026-05-30

### Features

- **Queen quick-action bar is now config-driven and styled to match the worker
  action bar.** The embedded Queen's buttons were small, all-grey, and
  hardcoded in the template — visually and structurally inconsistent with the
  worker action bar (full-size, color-coded, config-driven). New
  `queen_action_buttons` config — model `QueenActionButtonConfig` +
  `DEFAULT_QUEEN_ACTION_BUTTONS`, wired through the loader, serialization,
  known-keys, DB config store (load + save), the server config applier, and
  package exports — managed the **same way** as worker `action_buttons`. The
  Queen bar now renders from that config using the same `btn btn-{style}`
  classes (Kill = danger, etc.) while keeping the Queen's own actions
  (Continue, 1, 2, Get Latest, Clear Session, Kill, Revive, Refresh) wired
  through the existing `ccQueen*` handlers — no JS change. The field defaults
  to the populated set, so DBs predating it still render the bar (no
  regression).

### Changes

### Fixes

- **Mobile: worker status no longer goes stale after the tab is backgrounded.**
  The WebSocket reconnect (`ws.onopen`) and resume (`onAppFocus`) catch-up
  paths refreshed tasks/buzz/pipelines but not the worker list/status, so on
  mobile — where the tab is frequently backgrounded (screen lock, app switch),
  dropping the WS and pausing background polling — worker state badges stayed
  stale on resume. Both paths now also call `refreshWorkers()` +
  `refreshStatus()`, mirroring the live `'state'` WS handler.

## [2026.5.28.8] - 2026-05-28

### Features

### Changes

### Fixes

- **Auto-focus the Queen PTY terminal when the Queen view (Command Center)
  opens** (#551). Opening the Queen view embedded her live PTY via
  `mountQueenEmbed()` but never focused it, so the operator needed an extra
  click before typing. `mountQueenEmbed()` now schedules a staged re-focus
  (80 ms + 250 ms) after its refit ladder — mirroring the worker-view focus in
  `showTermEntry()` — surviving the WS reset/reconnect it performs. The actual
  focus is factored into a shared `focusTermEntryNow(entry)` helper (extracted
  from `focusInlineTerm`, no behavior change for worker terminals). A new
  `isTermInputFocused()` helper teaches the four global keyboard-shortcut
  yield-guards (Ctrl+L/D, Alt+letter, `?`, Escape) to recognize the Queen embed
  textarea as well as the active worker terminal, so focusing the Queen PTY
  doesn't leak Alt+A/K/N or Ctrl+L/D shortcuts through while typing. New
  Playwright acceptance probe `scripts/check_queen_focus.py` covers all four
  criteria. All changes in `src/swarm/web/static/dashboard.js`.

## [2026.5.28.7] - 2026-05-28

### Features

- **Coexistence guardrails for Claude Code dynamic workflows (Opus 4.8+, the
  `Workflow` tool) running inside a worker.** A launched dynamic workflow runs
  in the *background*: the tool call returns, the worker's turn yields, and the
  prompt reappears while subagents execute — so the worker *looks idle* but is
  not free and will be re-invoked on completion. Without a guardrail Swarm
  would nudge it, propose completion, or assign a new task over the in-flight
  run.
  - New `_RE_WORKFLOW_ACTIVE` in `providers/claude.py` matches the Claude Code
    footer status tray (verified against the installed binary v2.1.156):
    `N background dynamic workflow(s)`, `N remote dynamic workflow(s)`,
    `running dynamic workflow`. The count prefix distinguishes an *active* run
    from `"Run a dynamic workflow?"` (a WAITING prompt) and
    `"No dynamic workflows in this session."` (the /workflows history browser).
  - Both classify paths now route an in-flight workflow to `BUZZING` (same path
    as background shells/monitors), which cascades to suppress IdleWatcher
    nudges, premature auto-completion, and new-task assignment. The
    stuck-BUZZING safety net (`state_tracker._has_active_turn_signal`) treats
    the footer as a live turn so a long workflow isn't force-flipped to RESTING.
  - New `LLMProvider.is_long_running_tool_active()` (base returns `False`;
    `ClaudeProvider` implements via the background/subagent/workflow regexes).
    `OversightMonitor.check_prolonged_buzzing` is suppressed for a worker whose
    PTY shows an in-flight long-running tool (threaded through
    `OversightHandler` → `pilot._get_provider`), so a legit long workflow
    doesn't burn a Queen oversight call or inject a note.
  - **Provider-gated by construction:** the base default returns `False`, so
    Gemini/Codex/OpenCode workers (which don't run dynamic workflows) are
    unaffected. No PressureManager change — the existing rate-limit detector
    already covers the token-concentration failure mode (documented in
    CLAUDE.md).

### Changes

### Fixes

## [2026.5.28.6] - 2026-05-28

### Features

### Changes

- **Watchers stop nudging + escalate to the operator after N no-progress
  repeats (task #546 — coordination-machinery fix + audit).** The
  IdleWatcher and InterWorkerMessageWatcher debounce nudge *frequency*
  but had no *termination* condition, so a worker idle on a task it
  cannot progress (e.g. a shipped fix awaiting operator verification —
  the live #543/#546 repro — or a genuinely stuck worker) got poked
  every debounce window forever, burning tokens (same shape as #529's
  ~$51 rcg-networks stale-blocker incident, via a different hole).
  - New shared `swarm/drones/nudge_guard.py::RepeatNudgeGuard`: tracks
    consecutive no-progress nudges per worker keyed on a cheap
    "fingerprint" (worker state + outstanding-work signature). After
    `DroneConfig.idle_nudge_max_repeats` (default 3) repeats with an
    unchanged fingerprint it returns ESCALATE once, then SILENT until
    the fingerprint changes (worker made progress / operator acted).
    Both watchers use it via a small `_dispatch_or_escalate` helper —
    not duplicated.
  - On escalation the watcher emits a new
    `SystemAction.AUTO_NUDGE_ESCALATED` buzz entry and calls an
    injected `escalate_to_operator` callback, wired in the daemon to
    `push_notification(event="idle_nudge_escalated", priority="high")`
    — one operator-facing dashboard notification instead of an endless
    silent loop. Both failure modes (awaiting-verification, genuinely
    stuck) resolve to the same correct action: stop poking the worker,
    hand it to the operator.
  - `idle_nudge_max_repeats=0` disables the cap (pre-#546 unbounded
    behavior) for opt-out via `swarm.yaml`.
  - **Audit** (the 4th coordination gap in a week — #524/#527/#529/#546
    — warranted a sweep): reviewed IdleWatcher, InterWorkerMessageWatcher,
    BlockerStore, and the auto-handoff spawn. The systemic pattern was
    *"a coordination loop with no escalation-to-human terminal state."*
    BlockerStore (post-#529) and auto-handoff (post-#527) are healthy;
    both watchers had the hole and are now fixed by the shared guard.
  - 8 new tests: 6 unit (`tests/test_nudge_guard.py`) + 2 IdleWatcher
    integration (escalate-after-N, streak-resets-on-progress) in
    `tests/test_blockers.py`.

### Fixes

## [2026.5.28.5] - 2026-05-28

### Features

### Changes

### Fixes

- **Mobile dashboard: scroll-fade gradient no longer paints a dark bar
  over a random worker pill (task #543 — actual root cause of the
  #515 / #540 / #541 overlay reports).** The mobile worker-list has a
  30px `linear-gradient` scroll-fade ("scroll → for more workers")
  applied via `.worker-list .panel-body::after`. But `.panel-body` is
  the horizontal scroll container (`overflow-x: auto`), and an
  absolutely-positioned `::after` with `right: 0` inside a scroll
  container anchors to the scroll **content box**, not the visible
  viewport edge. So the dark fade painted wherever the pill row was
  currently scrolled — landing on top of whatever worker sat at that
  offset. It followed the **scroll position**, which is why it
  appeared on `platform` (slot 2), then `rcg-networks` (slot 3), then
  `admin` across successive operator screenshots: not a per-slot and
  not a per-worker artifact.
  - **Fix**: moved the `::after` from the scroll container
    (`.panel-body`) to the non-scrolling wrapper (`.worker-list`),
    which now has `position: relative`. The fade pins to the true
    right edge of the visible column. Mirrors the already-correct
    `.config-tab-nav-wrap::after` pattern in the same file (whose
    comment reads "on wrapper so it stays pinned").
  - **Why #515 / #540 / #541 all missed it**: those fixes chased
    per-worker elements (`.context-bar`), a sticky-hover guard, and
    the Queen modal `display` — none touched this scroll-fade
    pseudo-element. The real culprit was structural (wrong
    positioning anchor), not per-worker. Mobile-scoped (inside the
    `@media (max-width: 768px)` block); desktop's vertical worker
    list is unaffected.

## [2026.5.28.4] - 2026-05-28

### Features

### Changes

### Fixes

- **Mobile dashboard: Queen modal body no longer renders as a
  1-2-words-per-line vertical strip (task #541).** Operator reported
  the "Queen — Hive Conductor" modal on mobile rendering its body
  content in a super-narrow column (~30-40px wide) at the left side
  with most of the modal width unused.
  - **Root cause**: `.queen-card` is defined TWICE in
    `src/swarm/web/templates/base.html` — once at line 657 for the
    modal-popup cards (Resolution / Escalation / Assignment shown
    inside `#queen-result`), then AGAIN at line 1104 for the side-
    panel Queen card with `display: flex; align-items: center`. CSS
    cascade picks the LATER rule, so the modal cards inherited
    `display: flex` and their text children packed to min-content
    width — wrapping prose to 1-2 words per line on any narrow
    viewport. Cleanly visible at mobile widths where there's less
    fallback room.
  - **Fix**: added a scoped override at the modal context —
    `#queen-result .queen-card { display: block; }`. The side-panel
    Queen card keeps its flex layout (intentional — bee-icon +
    name + meta line up horizontally). The modal cards lay out as
    blocks and text wraps to the modal's actual content width.
  - **Minimal change**: 1-rule addition, ~14 LOC including the
    explanatory comment about the duplicate selector. No other
    modal styling touched.
  - **Bug-class note**: the `.queen-card` duplicate-selector
    pattern is the underlying smell. A future hygiene pass could
    rename the side-panel queen card to `.queen-sidebar-card` so
    the modal-vs-sidebar distinction is encoded in the class name
    rather than relying on a scope-override. Filed as a follow-up
    note, not addressed here per #541's "minimal scoped change"
    directive.
  - **Validation**: full pytest 4779 passed (matches baseline);
    ruff format + ruff check --max-warnings 0 clean.

## [2026.5.28.3] - 2026-05-28

### Features

### Changes

### Fixes

- **Mobile dashboard: worker-tab dark overlay (task #540, second
  attempt at the #515 bug class).** Operator reported a dark overlay
  on the `platform` tab (slot 2) in the worker-tab row of the mobile
  dashboard. The previous attempt (#515, 2026.5.27.7) patched a
  per-element symptom (hiding `.context-bar` inside mobile pills).
  Today's overlay is a different bug class.
  - **Root cause hypothesis (a priori most likely)**: the
    `.worker-item:hover` rule at `base.html:1042` paints
    `background: var(--panel)` (dark brown `#3E2B1B`) + an inset
    honey stripe, with no `@media (hover: hover)` guard. On mobile
    browsers' well-known sticky-hover-on-tap quirk, the rule applies
    when the user taps a tab and STICKS until the next focus shift —
    producing the visible dark overlay. The operator's framing
    "whatever is in the 2nd position has the same issue" is
    explained as: it follows whichever tab was most recently
    tapped (which happened to be slot 2 when the screenshot was
    captured).
  - **Fix**: wrapped `.worker-item:hover` in
    `@media (hover: hover) and (pointer: fine)` so the rule only
    applies on devices with a real hover-capable pointer
    (mouse / trackpad). Touch devices get no hover-paint — the
    artifact cannot persist. Desktop hover behaviour is preserved.
  - **Why this is a bug-class fix, not a per-element patch**: any
    `:hover` rule that paints visible background or color on a
    tappable element is at risk for the same sticky-hover artifact
    on mobile. This fix is specific to `.worker-item:hover` (the
    one rule with the reported symptom). A broader hygiene audit
    of every `:hover` rule in `base.html` is filed as a follow-up
    (out of scope here per the approved plan).
  - **Diagnostic-pending caveat**: the plan asked the operator to
    confirm position-dependence vs. last-tapped via a drag-reorder
    test before code touched. The operator approved the plan; the
    fix shipped as the most-likely candidate (risk-symmetric — the
    media-query guard is harmless if the actual cause turns out to
    be different). If the overlay persists after operator visual
    smoke post-deploy, Phase 2 of the plan (slot-specific
    investigation) kicks in.
  - **No new tests**: CSS rendering can't be asserted in pytest
    without Playwright/Cypress infra. Visual smoke after deploy
    (tap each worker tab → verify no sticky overlay; mouse-hover
    on desktop → verify hover paint preserved) is the
    authoritative check.

## [2026.5.28.2] - 2026-05-28

### Features

### Changes

### Fixes

- **`swarm_report_blocker` now rejects filings against terminal targets;
  IdleWatcher's auto-clear emits a `BLOCKER_AUTO_CLEARED` buzz entry
  (task #529).** Operator-relayed bug after rcg-networks burned ~$51 in
  worker tokens being nudged on a blocker against task #528, which
  platform had completed hours earlier.
  - **Root-cause finding** (DB investigation falsified the operator's
    stated theory): the auto-clear path in
    `BlockerStore.has_active_blocker` was already working correctly —
    the blocker row IS purged on the next sweep when the target task
    becomes done. The actual problem was **visibility**: the worker had
    no signal that its blocker was auto-cleared, so it kept re-filing
    the same blocker (3 times across an hour, all silently no-op'd),
    and kept being nudged with no understanding of why.
  - **Fix 1 (MCP handler)**: `_handle_report_blocker` in
    `src/swarm/mcp/handlers/_blockers.py` now checks the blocker target
    task's status before recording. If status is `done`/`failed`/
    `removed`, returns an explanatory error response naming the target
    and pointing the worker at re-evaluating their blocked task. The
    worker breaks the re-file loop at the filing surface instead of
    looping silently.
  - **Fix 2 (BlockerStore observability)**: added
    `SystemAction.BLOCKER_AUTO_CLEARED` next to the existing
    `AUTO_NUDGE_SKIPPED` in `src/swarm/drones/log.py`.
    `BlockerStore.has_active_blocker` gained an optional
    `on_auto_clear(blocker, reason)` callback that fires once per
    cleared blocker. The IdleWatcher wires this to emit a
    `BLOCKER_AUTO_CLEARED` buzz entry so operator audits can see
    exactly when and why a previously-blocked worker is being nudged
    again. Callback exceptions are swallowed (clear is load-bearing,
    observability is best-effort).
  - **Bug B verification** (operator asked me to check rcg-networks's
    secondary theory): `get_unread` is recipient-only — SQL
    `WHERE recipient = ? OR recipient = '*'` cannot match rows where
    the worker is the sender. Outbound messages do NOT trigger the
    pause-reset path. Documented + pinned with a regression test
    (`test_outbound_messages_excluded`) in
    `tests/test_message_store.py`.
  - **Light refactor of `BlockerStore.has_active_blocker`** to keep
    cyclomatic complexity ≤ 12 after the callback paths landed:
    extracted `_check_target_done` and `_check_message_since`
    statics; main loop now reads as
    `if check: clear → continue` per path.
  - 7 new regression tests across 3 files. Existing IdleWatcher /
    BlockerStore / MCP tests unchanged and still pass.
  - Takes effect on the next operator-initiated daemon reload.
    Combined with the deferred #524 stop-hook fix, #527 auto-handoff
    send-failure park, and #442 itself, the next reload activates
    all four coordination changes together — combined smoke test
    recommended.

## [2026.5.28] - 2026-05-28

### Features

### Changes

### Fixes

- **Auto-handoff tasks no longer get re-routed to a random worker on
  send-failure (task #527).** When `start_task` failed to deliver a
  task body to its recipient (PTY not ready / transient OSError),
  the unassign-on-failure handler dropped the task into the pending
  pool — where the queen's auto-assigner could (and did) re-route
  it to a different idle worker, ignoring the original recipient
  intent. For tasks tagged `"auto-handoff"` (the #442 inter-worker
  watcher's spawn output), that's a misroute by construction: the
  watcher resolved THIS recipient from a direct message addressed
  to them.
  - Concrete bite: task #525 (platform → rcg-networks via message
    #1156) ended up completed by public-website after rcg-networks's
    send failed. DB history: ASSIGNED rcg-networks → UNASSIGNED
    "send failed" → ASSIGNED queen → public-website. Operator's
    stated theory was "recipient resolution bug" in the auto-spawn,
    but DB evidence falsified that — the original assignment was
    correct; the bug was downstream in the failure-recovery path.
  - Fix: extend the exception handler in
    `src/swarm/server/task_coordinator.py::start_task` to detect
    `"auto-handoff" in task.tags` and KEEP the task ASSIGNED to
    the original recipient instead of unassigning. The
    IdleWatcher's nudge-on-RESTING-with-ASSIGNED path will retry
    delivery once the recipient's PTY recovers; the auto-spawn's
    `_spawned_msg_ids` dedup prevents re-spawn in the interim.
  - Operator visibility preserved: the `TASK_SEND_FAILED` buzz
    entry and the `task_send_failed` WS broadcast still fire on
    either branch. The buzz detail now carries
    `[auto-handoff: kept ASSIGNED for retry]` so the operator can
    see what was done differently.
  - Non-handoff tasks are unchanged — they still unassign and
    rejoin the pending pool. Only the `tags=["auto-handoff"]`
    branch (set by `spawn_handoff_task` and nowhere else) gets
    the no-requeue treatment.
  - 2 regression tests in `tests/server/test_task_coordinator.py`:
    one pinning the #525 repro (auto-handoff task kept ASSIGNED
    on send failure), one pinning the inverse (regular task still
    unassigns).
  - Takes effect on the next operator-initiated daemon reload.
    Combined with the deferred #524 stop-hook fix and #442
    itself, the next reload activates all three coordination
    changes together.

## [2026.5.27.12] - 2026-05-27

### Features

### Changes

### Fixes

- **Native `/goal` no longer pins cross-project from-worker into a
  Stop-hook loop (task #524).** When `_maybe_seed_goal` dispatched
  on the from-worker of a cross-project task (source_worker !=
  target_worker), the to-worker's acceptance criteria were seeded
  as a `/goal` on the from-worker — whose repo physically cannot
  satisfy them. The Stop-hook then re-prompted indefinitely.
  Concrete repro: cross-project task #523
  (from=rcg-networks → to=platform) burned ~$10 / 257K output
  tokens on rcg-networks before operator reassignment.
  - Added an explicit cross-project guard at the top of
    `_maybe_seed_goal` in `src/swarm/server/task_coordinator.py`:
    if `task.is_cross_project and worker_name ==
    task.source_worker and task.source_worker != task.target_worker`,
    skip the seed and emit a `GOAL_SKIPPED` buzz entry naming the
    from/to pair for audit.
  - New `SystemAction.GOAL_SKIPPED` enum in
    `src/swarm/drones/log.py` sits next to the existing
    `GOAL_SET` so the suppression is visible alongside seedings.
  - 2 regression tests added in `tests/test_goal_seeding.py`:
    one covering the from-worker bug repro (no `/goal` sent,
    `GOAL_SKIPPED` logged); one covering the legitimate
    to-worker cross-project path (`/goal` still seeded as before).
  - Backward-compatible: same-worker tasks and the happy
    cross-project target-worker path are untouched. Only the
    buggy from-worker dispatch path is altered, toward a NO-OP.
  - Takes effect on the next operator-initiated daemon reload.
    Currently running workers with already-seeded goals are
    unaffected; only newly dispatched tasks consult the new guard.

## [2026.5.27.11] - 2026-05-27

### Features

### Changes

- **Refactor — MCP handler return TypedDicts (task #520).** Final
  child of the #514 audit-code decomposition. With #518 + #519
  done, every handler now lives in a per-domain module ≤ 300 LOC,
  making this sweep tractable.
  - New `src/swarm/mcp/types.py` (44 LOC) defines `TextContent`,
    `ErrorContent` (alias), `StructuredResponse`, and the
    `HandlerResult` union — the single shared vocabulary every
    handler return signs to.
  - Every `_handle_*` function across `src/swarm/mcp/handlers/` (14
    worker handlers) and `src/swarm/mcp/queen_handlers/` (15 Queen
    handlers) had its return signature rewritten:
    `list[dict[str, Any]]` → `list[TextContent]`;
    `list[dict[str, Any]] | dict[str, Any]` → `HandlerResult`.
  - `handle_tool_call` in `src/swarm/mcp/tools.py`, `_assert_queen`
    + `_PERMISSION_DENIED` in `src/swarm/mcp/queen_handlers/_common.py`,
    `_lookup_task_by_number` in `src/swarm/mcp/handlers/_task_format.py`,
    and `_resolve_task` in `src/swarm/mcp/queen_handlers/_tasks.py`
    all picked up the same typed surface.
  - Runtime no-op verified: TypedDicts are dicts at runtime, dict
    literals every handler emits today already satisfy `TextContent`'s
    `{"type": "text", "text": str}` shape. Smoke tests show identical
    JSON output before/after.
  - Out of scope (intentional, per the audit-criterion wording about
    "return signatures"): `TOOLS: list[dict[str, Any]]` schema
    declarations, `_HANDLERS: dict[str, Any]` registry dicts, and
    `arguments: dict[str, Any]` input parameters all stay as-is.
    Each tool has its own input-schema shape; unifying them would
    be over-engineering.
  - All 29 MCP tools (14 worker + 15 Queen) dispatch unchanged.
    Full pytest 4767 passed; ruff format + ruff check clean.
  - **With #520 done, #514's 5-task decomposition is fully shipped**:
    #516 (PtyHolder SRP split, 2026.5.27.8), #518 (mcp/tools split,
    2026.5.27.9), #519 (mcp/queen_tools split, 2026.5.27.10), #520
    (TypedDict sweep, this release). #517 (proposal extraction) was
    closed earlier as already-done after investigation showed
    `ProposalCoordinator` already existed.

### Fixes

## [2026.5.27.10] - 2026-05-27

### Features

### Changes

- **Refactor — `mcp/queen_tools.py` split by concern (task #519).** Fourth
  of 5 deferred refactors from #514, applying the same pattern proven
  in #518. `queen_tools.py` was 1695 LOC and mixed the 15 Queen-only
  MCP handler schemas + bodies + helpers + the shared `_assert_queen`
  permission gate.
  - Decomposed into 11 modules under `src/swarm/mcp/queen_handlers/`:
    `_common.py` (`_assert_queen`, `_PERMISSION_DENIED`, `_clamp` —
    used by every handler), `_views.py` (view_worker_state +
    view_task_board), `_logs.py` (view_buzz_log + view_drone_actions),
    `_messages.py` (view_messages + view_message_stream),
    `_message_stream_helpers.py` (the render + structured-payload
    helpers split off to keep `_messages.py` under the LOC budget),
    `_threads.py` (post_thread + reply + update_thread),
    `_thread_helpers.py` (the operator-thread + broadcast helpers
    shared with `_learnings.py`), `_learnings.py` (query_learnings +
    save_learning), `_workers.py` (interrupt_worker + prompt_worker),
    `_tasks.py` (reassign_task + force_complete_task, plus the shared
    `_fire_async` + `_resolve_task` helpers).
  - `src/swarm/mcp/queen_tools.py` shrinks from 1695 → 58 LOC (97%
    reduction). Now purely an aggregator that concatenates per-domain
    `TOOLS` lists and merges `HANDLERS` dicts into `QUEEN_TOOLS` and
    `QUEEN_HANDLERS`, re-exporting `_assert_queen`, `_clamp`,
    `_PERMISSION_DENIED`, and `_handle_view_worker_state` for the
    handful of tests that reach those by name.
  - Wire protocol unchanged: 15 Queen tools carry over verbatim; the
    `_HANDLERS.update(QUEEN_HANDLERS)` merge in `tools.py` keeps the
    unified 29-tool registry intact.
  - Every handler module ≤ 300 LOC; `queen_tools.py` ≤ 400 LOC.
  - Zero test file edits — `tests/test_queen_tools.py` (which imports
    `QUEEN_HANDLERS`, `QUEEN_TOOLS`, `_assert_queen`, `_clamp`) and
    `tests/test_structured_content.py` (which imports
    `_handle_view_worker_state`) keep working through the re-exports.
  - 1 child of #514 remains: #520 (MCP TypedDict sweep). With #518 +
    #519 done, the handler files are now small enough to type
    per-file cleanly — #520 is the natural next step.

### Fixes

## [2026.5.27.9] - 2026-05-27

### Features

### Changes

- **Refactor — `mcp/tools.py` split by concern (task #518).** Third of
  the deferred refactors from #514. `tools.py` was 1985 LOC and mixed
  the MCP `TOOLS` schema list (~687 LOC of pure data), 14 handler
  functions + helpers (~1100 LOC), and the dispatcher + source-drift
  probe.
  - Decomposed into 13 modules under `src/swarm/mcp/handlers/`:
    `_messages.py` (check_messages / send_message / note_to_queen +
    schemas), `_queen_relay.py` (the auto-relay + Attention-thread
    helpers `_messages.py` shares with `note_to_queen`), `_blockers.py`,
    `_park.py`, `_email.py`, `_tasks.py` (task_status + complete_task),
    `_create.py` (create_task — its own module so neither file blows
    the per-module budget), `_task_format.py` (the formatters used by
    task_status), `_files.py`, `_learnings.py`, `_playbooks.py`,
    `_progress.py`, `_batch.py`. Each domain module owns BOTH its
    schemas and its handler(s); `tools.py` is now a thin aggregator.
  - `src/swarm/mcp/tools.py` shrinks from 1985 → 212 LOC. Every
    handler module is ≤ 300 LOC.
  - Wire protocol unchanged: 14 worker tools + 15 Queen tools (29
    total) carry over verbatim through `TOOLS` / `_HANDLERS`. The
    dispatcher (`handle_tool_call`), the source-drift probe
    (`tools_source_drift`), and `_TOOL_NAMES` remain in `tools.py`.
  - Zero test file edits required. The handlers tests reach for as
    private symbols (`_handle_check_messages`, `_handle_park_task`,
    `_handle_get_playbooks`, `_handle_create_task`, `_handle_complete_task`,
    `_handle_task_status`) are re-exported from `tools.py` so every
    existing call site keeps working.
  - 2 children of #514 remain queued (#519 mcp/queen_tools split,
    #520 MCP TypedDict sweep). #517 (proposal extraction) was
    closed earlier as already-done.

### Fixes

## [2026.5.27.8] - 2026-05-27

### Features

### Changes

- **Refactor — `pty/holder.py` SRP split (task #516).** First of the
  5 deferred refactors from #514. `PtyHolder` was 1058 LOC and mixed
  two concerns: PTY process lifecycle (spawn / kill / signal /
  resize / snapshot / inherit) AND command-routing dispatch
  (`_dispatch_cmd` + 11 `_cmd_*` methods + `_CMD_HANDLERS` ClassVar).
  - Extracted dispatch into new `src/swarm/pty/command_handler.py`
    containing `PtyCommandHandler` — receives a `PtyHolder` reference
    in `__init__` and routes lifecycle ops through `self.holder.*`.
  - `PtyHolder.__init__` now instantiates `self._cmds = PtyCommandHandler(self)`;
    `_handle_command` calls `self._cmds.dispatch(msg)` instead of the
    removed `self._dispatch_cmd(msg)`.
  - Wire protocol unchanged — daemon still sends `{"cmd": "<name>", ...}`
    and receives `{"ok": bool, ...}`. The 11 dispatch keys
    (ping / version / spawn / list / write / signal / resize / kill /
    snapshot / shutdown / restart_in_place) are identical.
  - `holder.py` shrinks from 1058 → 851 LOC (-207).
  - Tests updated: `tests/test_holder.py:945` and
    `tests/test_pool.py:351` retarget `PtyCommandHandler._CMD_HANDLERS`
    in place of the prior `PtyHolder._CMD_HANDLERS` references.
  - 4 children of #514 remain queued (#517 proposal extraction,
    #518 mcp/tools split, #519 mcp/queen_tools split, #520 MCP
    TypedDict sweep).

### Fixes

## [2026.5.27.7] - 2026-05-27

### Features

### Changes

### Fixes

- **Mobile dashboard: worker-pill context bar visual regression
  (task #515).** Operator-reported visual delta in the worker-tab row
  at the top of the dashboard on a narrow viewport — the `my-rcg`
  tab had a dark stripe its neighbours (`platform`, `swarm`) didn't.
  Root cause: the context-pressure bar (introduced in `607e350`
  task #285 Phase 1) renders a 3px brown stripe inside the worker
  pill whenever `context_pct > 0.05`. On mobile, `worker-meta` and
  `worker-task` are already hidden inside pills, so a single worker
  with active context becomes the only pill with an inner visible
  element — asymmetric. Fix: extend the existing mobile-hide rule
  to `.context-bar` in `worker-item`. The context-pressure drone
  runs on its own cadence regardless, so the visual signal is
  informational only; desktop view continues to show it.

## [2026.5.27.6] - 2026-05-27

### Features

### Changes

- **Audit remediation — code quality, observability, perf, and test coverage.**
  Closes the actionable findings from the full-project `/audit-code` sweep
  (zero TODOs, zero `pytest.mark.skip`, only 1 `# type: ignore` in the
  tree, async I/O clean — these were the headline confirms).
  - **N+1 fixes** in two drone sweeps: `_check_task_completions`
    (`drones/task_lifecycle.py`) and `IdleWatcher.sweep`
    (`drones/idle_watcher.py`) snapshot the board's active tasks once
    and bucket by `assigned_worker` instead of calling
    `tasks_for_worker` / `active_tasks_for_worker` once per worker.
    Drops the per-sweep work from O(W·T) → O(T).
  - **Observability — silent-swallow logging.**
    `playbooks/consolidator.py:84`, `cli.py:1800` (pool-disconnect),
    `update.py:165/347/374` (GitHub-commit parse, source-path parse,
    `git rev-parse` failure), and `client.py:180`
    (`is_daemon_running` probe) gained `_log.debug` /
    `_log.warning` with `exc_info=True` so an operator
    diagnosing flaky updates / restarts can find the cause in the
    log instead of staring at a sentinel return value.
  - **Config validation no longer recompiles regexes.**
    `DroneApprovalRule.__post_init__` now captures the
    `re.error` message on `compile_error` instead of dropping it;
    `_validate_approval_rules` reads `compile_error` instead of running
    `re.compile` a second time at validate-time.
  - **Stale `# type: ignore[unused-ignore]`** in
    `server/config_manager.py:356` removed (mypy was already
    reporting the ignore as dead).
  - **New tests**: `tests/test_db_migrate.py` (10 tests covering
    `auto_migrate` over tasks.json / proposals.json / task_history.jsonl
    — happy path, corrupt JSON, idempotent re-run, FK-cascade behavior,
    pre-v9 status vocabulary translation — was 0% covered before this);
    `tests/test_reverse_proxy.py` (17 tests covering Caddy install /
    Caddyfile write / reload / setup pipeline / status — was 0%
    covered before this, sat in the operator-facing reverse-proxy
    setup path with no regression net).
  - **Mock-board test helpers** in `tests/test_blockers.py`,
    `tests/test_idle_watcher.py`, `tests/test_mcp_tools_stale_recovery.py`
    updated to expose `active_tasks` + per-task `assigned_worker`
    so they round-trip through the new bucketing path.
  - Deferred (logged for follow-up, not in this commit): large
    refactors of `SwarmDaemon` (god object, 2085 LOC / 62 public
    methods), `mcp/tools.py` and `mcp/queen_tools.py` monoliths
    (~1700–2000 LOC each), `pty/holder.py` SRP split, and a TypedDict
    pass over MCP handler returns. These are risky / multi-day
    refactors that warrant their own focused tasks.

### Fixes

## [2026.5.27.5] - 2026-05-27

### Features

### Changes

- **Test coverage gap-fill — phase 3: web routes**. Close the
  user-facing auth surface gap.
  - **`web/routes/auth.py`** (Microsoft Graph + Jira OAuth) sat at
    **13%** — the status/disconnect endpoints had partial coverage
    through config-dashboard integration use, but the login /
    callback handlers were unreached. New `tests/web/test_auth_routes.py`
    adds 23 direct tests covering: unconfigured / error / expired
    state / exchange-failure / happy-path for both Graph and Jira
    OAuth flows, status JSON for connected vs unconfigured, and
    disconnect (including the no-mgr no-op path). Tests mock the
    request + daemon directly rather than spinning up an aiohttp
    test app, since each handler is a thin async function over
    `request.query` + `daemon.{graph,jira}_mgr`. Module coverage:
    **13% → 91%**.
  - **`web/routes/login.py`** sat at **0% coverage** (217 lines) —
    the most painful gap on the audit shelf because a login
    regression locks the operator out. New
    `tests/web/test_login_routes.py` adds 14 tests for the
    load-bearing helpers: IP-based rate-limit window
    (`_is_login_locked` / `_record_login_failure` /
    `_clear_login_failures` including the auto-prune on read for
    stale failures), WebAuthn `_get_rp_id` / `_get_expected_origin`
    derivation from `config.domain` / `request.host`, and the
    `_passkey_store` lazy-cache. WebAuthn route handlers and the
    integration POST flow stay out of scope — the prod path is
    exercised via dashboard integration use and the cryptographic
    challenge/response needs heavy mocking; the helpers cover the
    load-bearing pieces. Module coverage: **0% → 31%**.
  - **Coverage gate** lifted **75 → 76** to lock in the new
    headroom; future regressions trip `/check`.
  - **Suite metrics**: 4703 → 4740 (+37 new tests). Overall
    coverage **75.62% → 76.11%**.

### Fixes

## [2026.5.27.4] - 2026-05-27

### Features

### Changes

- **Test coverage gap-fill — phase 2: storage layer**. Close two
  silent-correctness gaps in the SQLite-backed stores.
  - **`db/buzz_store.py`** had **0% direct coverage** — every prod
    write goes through the `DroneLog` facade (mocked in most tests)
    and reads through dashboard routes (also mocked). New
    `tests/db/test_buzz_store.py` adds 29 direct tests covering
    insert + round-trip, `load_recent` chronology, all 7 query
    filter combinations + AND, search across detail/worker_name +
    limit, count under each filter, rule_analytics aggregation +
    since cutoff, `mark_overridden` noop semantics, and prune
    TTL deletion. Module coverage: **0% → 99%** (1 line — a
    sqlite Row passthrough branch — uncovered by design).
  - **`db/task_history.py`** sat at **45%** with the search /
    get_events / prune surfaces unexplored. New
    `tests/db/test_task_history.py` adds 18 direct tests covering
    chronological order, per-task filtering, limit, malformed
    action skip-on-load (`KeyError`/`ValueError` swallowed —
    protects the dashboard when an old daemon wrote an action enum
    the new build dropped), search across all filter combinations +
    pagination, and prune by TTL. The tests seed parent `tasks`
    rows directly so the `task_history.task_id REFERENCES tasks(id)`
    FK constraint passes — production gets this via
    `task_board.create() → task_history.append()` sequence. Module
    coverage: **45% → 100%**.
  - **Suite metrics**: 4656 → 4703 (+47 new tests). Overall
    coverage **75.24% → 75.62%**.
  - **Up next**: phase 3 (web routes) — `web/routes/login.py` 0%,
    `web/routes/tasks.py` 31%, `server/routes/events.py` 15%.

### Fixes

## [2026.5.27.3] - 2026-05-27

### Features

### Changes

- **Test coverage gate + refactor-adjacent gap-fill (audit items
  #10–#16, phase 1)**: pin a coverage floor and close the gaps the
  recent refactors exposed.
  - **Coverage gate**: new `[tool.coverage.run]` + `[tool.coverage.report]`
    in `pyproject.toml` with `fail_under = 75` (lifted from the
    initial 74 baseline after the gap-fills below put real headroom
    on the gate). `/check` auto-detects the threshold and runs
    pytest with `--cov`; future drops below 75% break the build.
  - **`apply_llms` + `apply_provider_overrides`** had **0% direct
    coverage** post-extraction (the ConfigManager refactor moved
    them out of the `_apply_*` daemon-side path the old tests hit).
    New `tests/server/config_appliers/test_llms.py` adds 21 direct
    tests covering body-shape validation, happy-path cfg writes, the
    `display_name` strip + string-command split, and the
    regex-validation guard on tuning fields. Module coverage:
    **10% → 100%**.
  - **`TaskCoordinator`** had 66% coverage post-extraction — the
    daemon-proxy tests reached the public surface but not the
    branch-y internals. New `tests/server/test_task_coordinator.py`
    adds 30 direct tests covering `check_ownership` across all 4
    `OwnershipMode` paths, `start_task` / `assign_task` validation
    errors, `spawn_handoff_task` (#442) creation +
    `source_worker` tagging + error swallowing,
    `auto_resolve_attention_for_task` thread sweep,
    `auto_start_next_assigned` early-return branches, and
    `retry_draft_reply`'s 4 error paths. Module coverage:
    **66% → 91%**.
  - **Suite metrics**: 4605 → 4656 passing (+51 new tests, same
    pre-existing `test_ws_auth` flake). Overall coverage
    74.81% → 75.24%.
  - **Up next** (per the three-step test-gap plan): storage layer
    (`db/buzz_store.py` 0%, `db/migrate.py` 54%), then web routes
    (`web/routes/login.py` 0%, `web/routes/tasks.py` 31%,
    `server/routes/events.py` 15%).

### Fixes

## [2026.5.27.2] - 2026-05-27

### Features

### Changes

- **SwarmDaemon refactor — Phase 3 TaskCoordinator (audit finding #1
  complete)**: extract every task-lifecycle method from the daemon
  into a new `swarm.server.task_coordinator.TaskCoordinator` class.
  - **Moved**: `assign_task`, `start_task`, `assign_and_start_task`,
    `complete_task`, `_maybe_seed_goal`, `_spawn_handoff_task`,
    `_auto_resolve_attention_for_task`, `_auto_start_next_assigned`,
    `_check_ownership`, `_send_completion_reply`, `retry_draft_reply`.
  - **Pattern**: back-reference to daemon (`self._d`) — same pattern
    `TestRunner` already uses. Cleaner than threading the 15+ daemon
    attributes (`task_board`, `task_history`, `drone_log`,
    `notification_bus`, `jira_svc`, `graph_mgr`, `pilot`,
    `pipeline_engine`, `playbook_ops`, `queen_chat`, `file_ownership`,
    `send_to_worker`, `push_notification`, `_track_task`,
    `_require_worker`, `_require_task`, `get_worker`, `broadcast_ws`,
    `email`, …) through a dedicated dependency dataclass — would
    obscure the wiring more than the back-reference does.
  - **Daemon proxies preserved**: every public method
    (`daemon.assign_task`, `daemon.complete_task`, etc.) keeps its
    signature as a thin one-line proxy to `self.tasks_coord.X`. Zero
    route / MCP / test churn.
  - **Patchability**: `complete_task`'s inner calls
    (`auto_start_next_assigned`, `auto_resolve_attention_for_task`,
    `_send_completion_reply`) route through the daemon proxies
    (`d.X`) rather than calling `self.X` directly, so existing tests
    that `monkeypatch.setattr(daemon, "_send_completion_reply", …)`
    still intercept the dispatch.
  - **Cleanup**: 4 now-unused imports dropped from `daemon.py`
    (`DroneAction`, `ProcessError`, `TaskAction`, `_log_task_exception`).
  - **Metrics**: `daemon.py` 2519 → 2087 lines (−432, additional −17%
    on top of Phase 1+2). Combined audit-#1 reduction: 3392 → 2087
    (−1305, −38%). Audit finding #1 is now fully shipped.
  - No behavior change. 4605 pytest pass (same pre-existing
    `test_ws_auth` cross-test flake that passes in isolation).

### Fixes

## [2026.5.27] - 2026-05-27

### Features

### Changes

- **SwarmDaemon refactor — Phase 1 + partial Phase 2 (audit
  finding #1)**: shrink the daemon module by extracting three
  cohesive concerns into focused sibling modules.
  - **`swarm.server.runner`** (NEW, 747 lines): all entry-point code
    (`run_daemon`, `run_test_daemon`, `_print_banner`,
    `_print_test_banner`, `_wire_test_console`, `_acquire_daemon_lock`
    + lock helpers, `_exec_restart` + restart helpers, `console_log`).
    Daemon re-exports these names for one release so external
    callers (cli, web routes, MCP) don't churn.
  - **`swarm.server.invariants`** (NEW, 137 lines): the four
    invariant-reconciliation methods (`working_workers`,
    `blocked_task_ids`, `reconcile_active_per_worker`, `run(reason)`)
    moved into `InvariantReconciler`. Daemon keeps the
    `_working_workers`/`_blocked_task_ids`/`_run_invariant_reconciliation`
    method names as thin shims for tests + #405 state-change paths.
  - **`swarm.server.playbook_ops`** (NEW, 246 lines): the five
    playbook-glue methods (`fire_synthesis`, `recall_for_task`,
    `attribute_outcome`, `log_verifier_skip`, `consolidate_learnings`)
    moved into `PlaybookOps`. Store/synthesizer/config flow through
    getters so tests that reassign `daemon.playbook_synthesizer`
    post-construction still pick up the new value.
  - **`daemon.py`**: 3392 → 2519 lines (−873, −26%). `SwarmDaemon`
    class itself trimmed; the rest is module-level service wiring
    that didn't belong in `daemon.py`.
  - **Out of scope (deferred follow-up)**: the full TaskCoordinator
    extraction (`assign_task`, `start_task`, `complete_task`,
    `_spawn_handoff_task`, `_maybe_seed_goal`,
    `assign_and_start_task`, `_auto_start_next_assigned`) — ~600
    more lines that are tightly wired to every coordinator on the
    daemon. Worth its own spec discussion and shipping cycle; the
    extraction this release lays the runway by clearing the
    surrounding noise.
  - No behavior change. 4605 pytest pass (1 pre-existing flake in
    `test_ws_auth.py::TestRateLimitLogic::test_mixed_old_and_new`
    that passes in isolation). No new `Any` types, no new
    `# type: ignore` markers.

### Fixes

## [2026.5.26.6] - 2026-05-26

### Features

### Changes

- **ConfigManager refactor (audit finding #2)**: extract every
  per-section validate-and-assign method out of the 1584-line
  `ConfigManager` into a new `swarm.server.config_appliers/` package
  (12 modules, one per section). The class is now a thin coordinator
  around the lifecycle pieces (`hot_apply`, `reload`, `watch_mtime`,
  `check_file`, `save`, `toggle_drones`) and a registry-driven
  `apply_update`.
  - `config_manager.py` shrunk from 1584 → 699 lines, 41 → 16 methods
    (4 are backward-compat shims kept so existing tests don't have
    to rewrite their direct method calls).
  - New `swarm.server.config_appliers.SECTION_REGISTRY` drives
    dispatch — adding a new section is now a 2-file change (new
    module + one registry entry). The hand-maintained
    `_KNOWN_BODY_KEYS` frozenset is replaced by a `known_body_keys()`
    function that derives the allow-list from the registry plus
    each virtual applier's declared top-level keys, killing the
    "remember to update both places" footgun the #328 silent-drop
    class was vulnerable to.
  - Appliers are free functions of `(cfg, body, *, deps) -> FieldOutcome`
    where `deps: ApplierDeps` carries the two side-effect handles
    (`invalidate_provider_cache`, `get_worker_svc`) the two appliers
    that need them (`llms`/`provider_overrides`, `workers`) reach for.
    Pattern mirrors `WorkerHealthDetectors` from the state-tracker
    refactor — small dataclass bundle instead of N callback params.
  - No behavior change. Same validation errors, same FieldOutcomes,
    same fail-loud diagnostic at the end of `apply_update`. Full
    4605-test pytest run green. See
    `docs/specs/config-manager-refactor.md` for the extraction spec.

### Fixes

## [2026.5.26.5] - 2026-05-26

### Features

### Changes

- **mcp/tools.py `Any` cleanup (audit finding #6)**: drop the
  unnecessary `Any` annotations on the task-shaped helpers and the
  JSON-input coercers. `_format_task_line`, `_format_task_meta_line`,
  `_format_cross_project_line`, `_format_task_detail`,
  `_sort_tasks_for_display` (+ its inner `key`), `_apply_task_filter`,
  `_task_to_payload` now declare their `SwarmTask` parameter.
  `_lookup_task_by_number` and `_coerce_limit` take `int | str | None`
  (the actual JSON shape) and narrow `None` explicitly so the runtime
  `int()` only sees the supported types. `_enum_value` takes
  `Enum | str | None` and uses `isinstance(v, Enum)` instead of the
  duck-typed `hasattr(v, "value")` check. `_format_section.items`
  becomes `list[str]`. `_validate_batch_op.op` becomes `object` since
  the function explicitly validates the runtime type. New
  `ToolsSourceDrift` TypedDict replaces `dict[str, Any]` as the
  `tools_source_drift()` return type. `Any` usages in the file:
  34 → 22 (the remaining 22 are all MCP protocol JSON shapes — tool
  schemas, content blocks, JSON-RPC arg dicts — kept by design). No
  behavior change; no `# type: ignore` markers added.

### Fixes

## [2026.5.26.4] - 2026-05-26

### Features

### Changes

- **WorkerStateTracker refactor — Phase 3 (final)**: extract
  `ContextPressureCheck` (synchronous, BUZZING-only) into
  `swarm.drones.detectors.context_pressure_check`. The new detector
  owns the inline-per-poll path that warns at `context_warning_threshold`
  and queues a deferred `/compact` at `context_critical_threshold`.
  - `state_tracker.py` shrunk to ~643 lines, 25 methods. All 5
    health detectors now extracted; `_poll_single_worker` now just
    sequences `detector.check()` calls.
  - `WorkerHealthDetectors` gains a `pressure` field.
  - Pre-refactor `TestContextPressure` (4 tests) migrated from
    `tests/test_state_tracker.py` into the new
    `tests/drones/detectors/test_context_pressure_check.py`, plus 2
    new edge-case tests (non-BUZZING skip, zero-pct skip).
  - `# DUPLICATION:` comment added to the new module pointing at the
    periodic `ContextPressureWatcher` in `drones/context_pressure.py`
    — overlap is intentional today (sync check catches in-poll
    critical excursions before the watcher's next sweep) but should
    be untangled in a follow-up audit task.
  - No behavior change.

### Fixes

## [2026.5.26.3] - 2026-05-26

### Features

### Changes

- **WorkerStateTracker refactor — Phase 2**: extract
  `ContextRecoveryDetector` (tier 1 `/compact` → tier 2 revive → tier
  3 escalate) into `swarm.drones.detectors.context_recovery`. The
  detector owns the `_RE_CONTEXT_ERROR` regex and walks
  `worker.recovery_attempts` through the three tiers via deferred
  actions on the shared `DecisionExecutor`.
  - `state_tracker.py` shrunk 751 → 682 lines (−9%), 27 → 26 methods.
  - `WorkerHealthDetectors` gains a `recovery` field.
  - Pre-refactor `TestContextErrorCompactGuard` (5 regression tests
    for the six-/compact-in-queue bug) migrated from
    `tests/test_context_awareness.py` into the new
    `tests/drones/detectors/test_context_recovery.py`, consolidated
    with the smaller `TestContextErrorRecoveryCounter`.
  - No behavior change. Phase 3 (`ContextPressureCheck`) is next.

### Fixes

## [2026.5.26.2] - 2026-05-26

### Features

### Changes

- **WorkerStateTracker refactor — Phase 1**: extract per-worker health
  detectors into `swarm.drones.detectors/`. Three detectors moved out
  of `state_tracker.py` (which shrunk from 856 → 751 lines, 30 → 27
  methods) and into their own modules with isolated tests:
  - `ContextFileTracker` — records BUZZING workers' touched paths
    for revive context restoration.
  - `DiminishingReturnsDetector` — escalates BUZZING workers whose
    token growth stalls.
  - `RateLimitDetector` — spots provider rate-limit messages in PTY
    output and emits a `rate_limit` event (60s debounce).
  - New `WorkerHealthDetectors` dataclass bundles the three for a
    single `detectors=` param on `WorkerStateTracker.__init__`.
  - Tests for each detector moved to `tests/drones/detectors/` and
    no longer carry the WorkerStateTracker fixture overhead. Net
    test count: +2 (new edge cases for the extracted contracts).
  - No behavior change. Phases 2 (`ContextRecoveryDetector`) and 3
    (`ContextPressureCheck`) ship separately per the spec at
    `docs/specs/state-tracker-refactor.md`.

### Fixes

## [2026.5.26] - 2026-05-26

### Features

### Changes

- **Audit remediation pass** — closes 9 findings from a full-project
  audit (#5, #7, #8, #9, #17, #18, #19, #20, #21):
  - **DB perf (v12 schema migration)**: new `idx_messages_dedup`
    composite index on `messages(sender, recipient, msg_type,
    created_at)` matching the dedup probe in `MessageStore.send()` —
    previously a full table scan on every inter-worker message.
  - **DB perf**: `MessageStore.broadcast()` now runs one batched
    dedup `SELECT` for all recipients instead of N per-recipient
    probes.
  - **DB perf**: `config_store._save_workers` returns the worker
    name→id map so `_save_groups` can skip its redundant
    `SELECT id, name FROM workers`. Per-worker approval-rule deletes
    now batch into a single `DELETE … WHERE owner_id IN (…)` instead
    of one delete per worker.
  - **DB perf**: `SqliteTaskStore.load()` uses an explicit
    `_TASK_COLUMNS` list instead of `SELECT *` — narrower row
    payload + insulates the read path from schema additions that
    aren't materialized on `SwarmTask`.
- **Type safety**: removed all 6 `# type: ignore[attr-defined]`
  markers in `server/routes/{workers,queen}.py` by typing
  parameters against the proper `Worker` / `SwarmDaemon` types.
- **API clarity**: `_validate_draft_email_args` now returns
  `(fields|None, error_message)` instead of `fields | str` —
  clearer branching at the call site.

### Fixes

- **CLI restart polling**: narrowed `except Exception` to
  `(aiohttp.ClientError, TimeoutError)` so genuine bugs in the
  health-check loop surface instead of being silently swallowed.
- **Email attachment fetch**: promoted error logging from
  `console_log` to `_log.warning(..., exc_info=True)` so ops have
  forensic anchors for missing attachments; narrowed `except` to
  network errors only.

## [2026.5.25.14] - 2026-05-25

### Features

- **Add 3 regression tests for operator-reported BUZZING-detection
  screenshots.** After the 2026.5.25.13 fix shipped, the operator
  surfaced three additional PTY tail patterns to verify:
  - Foreground spinner with a multi-word verb phrase
    (``⊹ Verifying end-to-end + shipping… (5m 57s · ↓ 13.5k tokens
    · thought for 8s)``). The glyph+verb portion of the regex doesn't
    match a multi-word verb, but the ``thought for 8s`` clause does
    — proves the multiple-signal design holds when the verb is a
    phrase or the glyph isn't in the canonical set.
  - Background shell running
    (``✳ Sautéed for 30m 16s · 1 shell still running`` plus
    ``⏵⏵ auto mode on · 1 shell · ↓ to manage``). Two signals fire:
    the ``✳`` spinner + ``for 30m 16s`` elapsed time AND the
    ``1 shell still running`` background banner. Either is sufficient.
  - Middle-dot spinner with ellipsis
    (``· Osmosing… (7m 54s · ↑ 25.4k tokens · thought for 4s)``).
    Verifies the ``·`` glyph isn't rejected as ambiguous when
    followed by a real verb + ellipsis.
  All three tests pass under the .13 regex, documenting that the
  shipped fix already covers these patterns. The tests live in
  ``tests/test_pilot.py::TestStuckBuzzingSafetyNet`` and are named
  after their source screenshots so future regex tightenings can be
  cross-referenced. Full suite: 4601 passed.

### Changes

### Fixes

## [2026.5.25.13] - 2026-05-25

### Features

### Changes

### Fixes

- **State classifier wasn't recognising the modern Claude Code 2.x
  spinner format.** Operator reported `platform` showing
  `RESTING for 3m` in the sidebar while the worker was actively
  running a 16+ minute background task (live PTY tail had
  `✻ Sautéed for 16m 13s`). The `_RE_SUBAGENT_ACTIVE` regex in
  `src/swarm/providers/claude.py` only knew about legacy Braille
  spinners (`⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`) followed by a verb + `...` (three dots).

  When the stuck-BUZZING safety net (task #236) fires after 10 min of
  BUZZING state, it checks the narrow tail (last 5 lines) for an
  active-turn signal. Modern Claude Code's spinner uses sparkle
  glyphs and `…` (U+2026), so the narrow-tail check returned False
  and the safety net incorrectly flipped BUZZING → RESTING. The
  primary classifier still saw `esc to interrupt` in the wider tail
  and called BUZZING — but after the safety-net flip the state went
  back to RESTING until the next state change. The dashboard's
  "RESTING for Nm" came from that flip.

  Fix: updated `_RE_SUBAGENT_ACTIVE` to match the canonical Claude
  Code 2.x spinner character set per the source mirror
  (kdxsydq/ClaudeCode, src/components/Spinner/utils.ts):

      macOS:        · ✢ ✳ ✶ ✻ ✽
      Linux/Win:    · ✢ * ✶ ✻ ✽
      Ghostty:      · ✢ ✳ ✶ ✻ *

  The union (`· ✢ ✳ ✶ ✻ ✽ *`) plus the legacy Braille set is now
  accepted, followed by a verb + termination (`…`, `...`, or
  `for <digit>...`). `·` and `*` are ambiguous on their own
  (separators, list bullets) so the verb + termination is required
  to avoid false-positives on lines like
  `auto mode on · esc to interrupt`. Verb is `\w+` rather than a
  fixed list — Claude Code rotates verbs constantly (Cooking,
  Sautéed, Brewing, Verifying, Shipping, …) and pinning the list
  would break with each Claude Code release.

  4 new regression tests in `tests/test_pilot.py::TestStuckBuzzingSafetyNet`
  pin the modern formats: live `✻ Sautéed for 16m 13s` capture,
  `…` (U+2026) ellipsis variant, all 7 canonical spinner glyphs,
  and the false-positive guard for ambiguous-glyph-without-suffix.

  Same root cause explains the "slow to update" feel on mobile — the
  safety net was repeatedly flipping the worker RESTING within minutes
  of long background work starting, then the next poll saw the actual
  BUZZING state and flipped back. Now the spinner is recognised on
  every poll while the work runs.

## [2026.5.25.12] - 2026-05-25

### Features

### Changes

- **Type MCP tool argument payloads with TypedDicts.** Audit-flagged
  581 `Any` annotations across `src/swarm/`, concentrated in
  `mcp/tools.py` (47) and `mcp/queen_tools.py` (47). Defined 30
  TypedDicts in a new module `src/swarm/mcp/_arg_types.py` — one per
  MCP tool — mirroring each tool's `inputSchema.properties`. Updated
  every handler signature in both files from `args: dict[str, Any]`
  to the concrete TypedDict (`SendMessageArgs`,
  `QueenReassignTaskArgs`, etc.). The shapes were extracted
  programmatically from the existing schemas to guarantee the
  TypedDict and the wire schema stay in lock-step.

  Conventions documented in `_arg_types.py`:
  - All TypedDicts use `total=False` because runtime input may omit
    any field — required-field enforcement happens inside the handler
    body (the `if not field: return error` guard), not at the type-
    system layer. A JSON-RPC mis-send produces a polite tool error,
    not a Python `KeyError`.
  - Enum-like fields (`msg_type`, `priority`) stay as `str` — the
    handlers already validate the value, and `Literal[...]` would
    force every test fixture to cast for marginal gain.
  - Arrays of dicts (`swarm_batch.ops`, `queen_post_thread.widgets`)
    stay as `list[dict[str, Any]]` — the per-element shape is its
    own schema and a nested TypedDict variant union would
    reintroduce the Any-soup the audit flagged.

  Net: 30 dict[str, Any] → typed args. The remaining `Any` in
  `tools.py` / `queen_tools.py` is on return types (genuinely
  heterogeneous JSON-RPC payloads) and on `metadata` dicts that
  legitimately carry arbitrary keys. Full suite: 4594 passed.

### Fixes

## [2026.5.25.11] - 2026-05-25

### Features

- **New test file `tests/test_runtime.py` (22 tests).** Covers
  `swarm.queen.runtime` — Queen CLAUDE.md reconcile + sync CLI entry
  + small support functions. The reconcile decision matrix
  (SEEDED / MARKER_SEEDED / NO_OP / AUTO_UPDATED / DRIFT_FLAGGED) is
  now unit-pinned with `tmp_path` filesystem fixtures rather than
  relying on integration coverage. Pins:
  - `ClaudeMdReconcileResult` — equality semantics, repr, default
    details, `NotImplemented` on cross-type comparison.
  - `reconcile_queen_claude_md` — fresh seed (creates workdir +
    target + marker), marker-seed-from-disk for pre-existing files
    upgraded from a swarm version without the marker, no-op when
    shipped unchanged, auto-update when shipped changed with no
    local edits, drift-flagged with both diff-ref files when both
    shipped and on-disk diverged.
  - `_ensure_queen_claude_md` — confirms back-compat alias still
    returns the same shape.
  - `sync_queen_claude_md` — `accept-shipped` replaces on-disk +
    clears drift artifacts; `keep-local` updates marker only and
    preserves on-disk; unknown mode raises `ValueError`; missing
    workdir auto-created.
  - `queen_worker_config` — uses `QUEEN_WORKER_NAME` + `QUEEN_WORK_DIR`,
    falls back to `"claude"` when `config.provider` is None.
  - `find_queen` — None on no-queen list, returns the queen Worker
    when present, returns the first queen when multiple (pins
    deterministic behaviour).

  The PTY spawn path (`ensure_queen_running`) is left to integration
  coverage in `test_queen.py` / `test_fresh_install_queen.py` — it
  needs a real pool + worker manager and is more integration than
  unit. Audit-flagged gap for `state_publisher.py`, `mcp/server.py`,
  and `queen/contribute.py` were checked too; the first was filled
  in 2026.5.25.10, the other two already have comprehensive coverage
  (17 tests in `tests/test_mcp_server.py`, 8 test classes in
  `tests/test_queen_claude_md_contribute.py`) that the original audit
  Agent 2 missed. Net: only one real gap remained, now filled.

### Changes

### Fixes

## [2026.5.25.10] - 2026-05-25

### Features

- **New test file `tests/test_state_publisher.py` (23 tests).** Covers
  the previously integration-only `StatePublisher` (the broadcast
  layer that ferries worker / task / pipeline state to WS clients).
  Pins:
  - `_terse_detail` helper — empty input, whitespace collapsing,
    first-non-empty-line picking, 160-char ellipsis cap.
  - Single-shot broadcasts — `on_task_board_changed`,
    `on_workers_changed`, `broadcast_state`, `broadcast_usage`,
    `on_tunnel_state_change` (running / stopped / error).
  - `on_drone_entry` — non-notification entries broadcast only;
    notification entries also fire `push_notification`; STUNG and
    TASK_FAILED actions get `priority="high"`.
  - `on_state_changed` — BUZZING clears inflight + expires stale
    ESCALATION/COMPLETION proposals; RESTING is no-op; STUNG logs to
    drone_log with terminal tail; `mark_dirty` callback is invoked.
  - Internal debounce path — `_flush_state_broadcast` no-op when
    clean; `_mark_state_dirty` flushes immediately outside an event
    loop.

### Changes

### Fixes

## [2026.5.25.9] - 2026-05-25

### Features

### Changes

- **Replace `SELECT *` with explicit column lists in db stores.**
  Audit-flagged: `buzz_store.py` (3 sites) and `playbook_store.py`
  (6 sites) used `SELECT *` while the `_row_to_dict` / `_row_to_pb`
  consumers each had a fixed, known set of columns. Added module-
  level `_BUZZ_COLS` / `_PB_COLS` constants and switched every
  query to `f"SELECT {_BUZZ_COLS} FROM ..."`. Zero behaviour change
  today; the guardrail is schema-evolution safety — a future column
  addition won't silently inflate every query payload, and a column
  rename now fails at SQL execution time rather than masquerading
  as a `KeyError` inside `_row_to_*`. (CHANGELOG body added in the
  2026.5.25.10 commit; the .9 commit shipped with an empty body
  because the release script rewrote CHANGELOG.md mid-edit.)

### Fixes

## [2026.5.25.8] - 2026-05-25

### Features

### Changes

- **DronePilot full clean — migrate remaining sub-handler delegations.**
  Final pass after the targeted state-tracker migration in 2026.5.25.7.
  Migrated 17 delegation methods + 17 `@property` shims:
  - DecisionExecutor: `_run_decision_sync`, `_execute_deferred_actions`,
    `_had_substantive_action`, `_emit_decisions`, `_deferred_actions`,
    `_revive_loop_max`, `_revive_loop_window`.
  - TaskLifecycle: `record_completion_verdict`,
    `_cleanup_stale_proposed_completions`, `_check_task_completions`,
    `_auto_assign_tasks`, `_auto_complete_min_idle`,
    `_COMPLETION_REPROPOSE_COOLDOWN`, `_saw_completion`,
    `_needs_assign_check`.
  - PressureManager: `_suspend_workers`, `on_pressure_changed`,
    `_resume_pressure_suspended`, `_suspend_on_critical_pressure`,
    `_pressure_level`, `_suspended_for_pressure`.
  - DirectiveExecutor: `_execute_directives`.
  - OversightHandler: `_oversight_cycle`.
  - PollDispatcher (state + methods): `_cleanup_dead_workers`,
    `_poll_once_locked`, `_compute_backoff`, `_handle_poll_error`,
    `_loop`, `_running`, `_task`, `_idle_streak`, `_poll_lock`,
    `_poll_failures`, `_consecutive_errors`.
  Migrated callers across 9 files: `src/swarm/drones/poll_dispatcher.py`,
  `src/swarm/drones/backoff.py` (docstring ref), `src/swarm/server/daemon.py`,
  `src/swarm/server/proposals.py`, `src/swarm/server/resource_monitor.py`,
  `tests/test_pilot.py` (~80 sites), `tests/test_daemon.py`,
  `tests/test_terminal_approval.py`, `tests/test_testing_integration.py`.
  Plus `monkeypatch.setattr(pilot, "_poll_once_locked", ...)` repointed at
  `pilot._dispatcher.poll_once_locked` and the `DronePilot._compute_backoff`
  docstring ref in `backoff.py` updated to `PollDispatcher._compute_backoff`.
  Method-name mismatches reconciled mid-pass: handler-side names dropped
  the leading underscore for several (`oversight_cycle`, `loop`,
  `poll_once_locked`, `execute_directives`).

  **Kept as load-bearing pilot facade API:** `wake_worker`,
  `mark_operator_continue`, `note_park_rejected` (oversight-coordination
  glue), `clear_proposed_completion` (used by `TaskManager` which takes
  `pilot` as a dep and mocks `spec=DronePilot` in tests), `_safe_worker_action`
  + the `_classify_worker_state` lambda (`DirectiveExecutor` init-time
  callbacks that need late binding because `_state_tracker` /
  `_decision_exec` are constructed later in `__init__`).

  Internal `pilot.py` updated: `get_diagnostics`, `is_loop_running`,
  `needs_restart`, `restart_loop`, `toggle` read dispatcher state directly
  (`self._dispatcher._running` / `._task` / etc.) instead of bouncing
  through the deleted shims. Dropped unused `MemoryPressureLevel` import.

  **Bug surfaced + fixed during migration:** the test fixture for
  `TaskManager` uses `MagicMock(spec=DronePilot)`, which restricts
  attribute access to what's on the spec. Migrating `task_manager.py`
  to use `pilot._task_lifecycle.clear_proposed_completion` broke 5 tests
  (3 in `test_task_manager.py`, 2 in `test_api.py`). Restored
  `clear_proposed_completion` as a pilot facade method and reverted the
  `task_manager.py` change — services that take `pilot` as a dep
  shouldn't reach into its sub-handlers. The kept-shim docstring notes
  the rationale so future cleanups don't re-delete it.

  Net: pilot.py 1118 → 716 LOC (-36%), 136 → 45 methods (-67%).
  Full suite: 4549 passed (unchanged).

### Fixes

## [2026.5.25.7] - 2026-05-25

### Features

### Changes

- **Migrate DronePilot's WorkerStateTracker-family shims to direct
  sub-handler access.** Targeted follow-up to 2026.5.25.6: that
  release deleted only zero-caller shims; this release migrates the
  WorkerStateTracker cluster's external callers to access
  `pilot._state_tracker.*` directly, then deletes the now-orphaned
  shims. Migrated 5 method delegations + 7 `@property` shims across
  4 caller files:
  - `src/swarm/drones/poll_dispatcher.py` (3 sites): `_is_suspended_skip`,
    `_poll_single_worker`, `_any_became_active`.
  - `tests/test_pilot.py` (~13 sites): `_classify_worker_state`,
    `_handle_state_change`, `_poll_single_worker`, `_any_became_active`,
    `_content_fingerprints`, `_unchanged_streak`, `_last_full_poll`.
  - `tests/test_code_review_fixes.py` (~10 sites):
    `_update_content_fingerprint`, `_content_fingerprints`,
    `_unchanged_streak`.
  - `tests/test_terminal_approval.py` (~7 sites): `_waiting_content`,
    `_drone_continued`, `_operator_continued`.
  Bug found mid-migration: the original
  `pilot._poll_single_worker(...)` shim threaded `enabled=self.enabled`
  through to the tracker, but the dispatcher call site was migrated
  without it, silently defaulting `enabled=True`. Under the test
  fixture's default `pilot.enabled=False`, the tracker fired
  `_run_decision_sync` against WAITING workers, which auto-CONTINUE'd
  them and marked them `_drone_continued`, defeating
  `_detect_operator_terminal_approval` on the next transition (the
  test_terminal_approval suite caught this — 3 failures). Fixed by
  passing `enabled=p.enabled` explicitly in `poll_dispatcher`. The
  test sites of `_poll_single_worker` use the default `True` (matches
  their pre-shim expectation since they didn't toggle `pilot.enabled`).
  `_classify_worker_state` had to be kept as a late-binding lambda in
  `__init__` (`DirectiveExecutor` construction depends on it, before
  `_state_tracker` exists). `wake_worker` and `mark_operator_continue`
  stay as pilot delegations — they're semantically pilot-level public
  API used widely by `worker_service.py`, `daemon.py`, and tests.
  Net: -100 LOC removed from `pilot.py`, full suite 4549 pass.

### Fixes

## [2026.5.25.6] - 2026-05-25

### Features

### Changes

- **Remove 27 dead delegation methods + property shims from
  `DronePilot`.** Same audit pattern as the SwarmDaemon cleanup
  (2026.5.25.5): the pilot was already a facade over 12 sub-handlers
  (`DirectiveExecutor`, `CoordinationHandler`, `OversightHandler`,
  `PressureManager`, `_DecisionExecutor`, `WorkerStateTracker`,
  `TaskLifecycle`, `IdleWatcher`, `InterWorkerMessageWatcher`,
  `ContextPressureWatcher`, `Dreamer`, `PollDispatcher`), and the
  delegation/shim layer on top had accumulated more methods than
  callers. Census across `src/` and `tests/`:
  - 3 `@property` shims with **zero** external callers: deleted
    outright (`_escalation_timeout`, `_tick`, `_all_done_streak`).
  - 24 delegation methods with **zero** external callers: deleted
    outright (`_maybe_suspend_worker`, `_sync_display_state`,
    `_track_idle`, `_handle_waiting_exit`, `_detect_operator_terminal_approval`,
    `_suggest_approval_pattern`, `_should_throttle_sleeping`,
    `_poll_sleeping_throttled`, `_poll_dead_worker`,
    `_should_skip_decide`, `_is_revive_loop`, `_record_revive`,
    `_execute_deferred_continue`, `_should_eager_assign`,
    `_has_pending_bash_approval`, `_has_idle_prompt`,
    `_has_operator_text_at_prompt`, `_capture_worker_outputs`,
    `_signal_worker_async`, `_suspend_on_high_pressure`,
    `_run_periodic_tasks`, `_speculate_for_idle_workers`,
    `_on_loop_done`).
  - Census initially missed two external usage shapes
    (`DronePilot.X` class-static syntax in `server/analyzer.py` and
    `test_terminal_approval.py`; `monkeypatch.setattr(pilot, "X", …)`
    string-lookup in `test_pilot.py`). Migrated each to the owning
    sub-handler:
    - `server/analyzer.py`: `DronePilot._suggest_approval_pattern` →
      `WorkerStateTracker._suggest_approval_pattern`.
    - `test_terminal_approval.py` (8 sites): same migration.
    - `test_pilot.py` (3 sites): `monkeypatch.setattr(pilot,
      "_signal_worker_async", …)` →
      `monkeypatch.setattr(pilot._pressure_mgr, "_signal_worker_async", …)`.
    - `test_pilot.py` (`DronePilot._on_loop_done` static call) →
      `PollDispatcher._on_loop_done`.
  - Two internal pilot uses preserved: `self._safe_worker_action` is
    passed as a callback into `DirectiveExecutor` during `__init__`
    *before* `_decision_exec` exists (circular dep — the delegation
    is the lazy-binding indirection that breaks it), and
    `get_diagnostics()`'s `_tick` read was switched directly to
    `self._dispatcher._tick`.
  - `types` import dropped (only `_suspend_on_high_pressure` needed
    it).
  Net: -160 LOC of pure indirection, zero behaviour change. Full
  suite: 4549 passed (unchanged).

### Fixes

## [2026.5.25.5] - 2026-05-25

### Features

### Changes

- **Remove 11 backward-compat `@property` shims from `SwarmDaemon`.**
  When subsystems were progressively extracted (`BroadcastHub`,
  `ResourceMonitor`, `EscalationHandler`, `StatePublisher`), each
  refactor left behind delegation properties on the daemon so external
  callers wouldn't break. Audited the actual usage and migrated every
  caller to the extracted service directly:
  - `daemon.ws_clients` / `daemon.terminal_ws_clients` → `daemon.hub.*`
    (callers: `pty/bridge.py`, `routes/websocket.py`, several tests).
  - `daemon._broadcast_hook` → `daemon.hub._broadcast_hook` (callers:
    `daemon._on_ws_broadcast` setup in `run_daemon`, tests).
  - `daemon._notification_history` → `daemon.escalation._notification_history`
    (callers: `routes/drones.py` notification history endpoint, tests).
  - `daemon._state_dirty` / `_state_debounce_handle` /
    `_state_debounce_delay` → `daemon.publisher.*` (callers: tests
    plus daemon's own `_mark_state_dirty` / `_flush_state_broadcast`
    methods, which were updated to thread `pub = self.publisher`
    once instead of bouncing through the shim per field).
  - `daemon._broadcast_pending` / `_broadcast_latest` /
    `_resource_snapshot` / `_prev_pressure_level` had **zero**
    external callers — pure dead shim. Deleted outright.
  Result: -85 LOC of pure indirection, no behaviour change, no public
  API surface change (the shims were on private attributes anyway).
  The 3 `BackgroundLoopRunner` shims (`_heartbeat_task`, `_usage_task`,
  `_mtime_task`) from 2026.5.25.2 stay — they're 24 hours old and
  the cost of churning tests off them outweighs the indirection.

### Fixes

## [2026.5.25.4] - 2026-05-25

### Features

### Changes

- **Remove dormant verifier wiring from `SwarmDaemon`.** Audit
  surfaced that `_init_verifier_drone` was defined in commit `4249a39`
  (`feat(verifier): tiered verifier drone — adversarial
  post-completion check`) but the activation call site was never
  added — `_init_verifier_drone` had zero callers, so
  `self.verifier_drone` was never set, and `_fire_verifier`'s
  `getattr(self, "verifier_drone", None)` always returned `None`. The
  verifier code path has been dormant in production since landing.
  Removed: `_init_verifier_drone`, `_verifier_diff`,
  `_verifier_check_evidence`, `_verifier_peer_warnings`,
  `_verifier_send_warning`, `_verifier_escalate`, and `_fire_verifier`
  (~115 LOC of dead code). The `complete_task` `verify` kwarg is
  preserved on the public API (queen_force_complete_task still passes
  `verify=False` to leave a SKIPPED stamp) — the `verify=True` branch
  is now a no-op. `_log_verifier_skip` stays (it's live on the
  force-complete path). The `VerifierDrone` class and its 70 unit
  tests in `tests/test_verifier_drone.py` are unchanged; if the
  verifier ever comes off the shelf, the wiring is documented in
  commit `4249a39`. `test_complete_task_default_verify_runs_verifier_when_wired`
  renamed and re-docstringed to reflect the no-op semantics.

### Fixes

## [2026.5.25.3] - 2026-05-25

### Features

### Changes

- **Remove dead Jira delegation shims from `SwarmDaemon`.** Six
  methods (`_fire_jira`, `_fire_jira_export`, `_fire_jira_assign`,
  `_fire_jira_completion`, `_run_jira_import`, `jira_export_status`)
  plus `_jira_sync_loop` were one-line forwarders to methods that
  already existed on `JiraService` since the service was extracted.
  Two of them (`_run_jira_import`, `jira_export_status`) had zero
  callers anywhere in the codebase — pure dead code. The four
  `_fire_jira_*` shims had seven internal callers inside daemon.py;
  those now call `self.jira_svc.fire_*` directly. `_jira_sync_loop`'s
  one caller (the `BackgroundLoopRunner` registration in `start()`)
  now points at `self.jira_svc.sync_loop` directly. Net: -30 LOC of
  pure indirection, zero behaviour change.

### Fixes

## [2026.5.25.2] - 2026-05-25

### Features

### Changes

- **SwarmDaemon background-loop lifecycle hoisted into
  `BackgroundLoopRunner`** (`src/swarm/server/loop_runner.py`). Before
  this commit each periodic loop was wired inline: a
  `self._foo_task = asyncio.create_task(self._foo_loop())` line in
  `start()` and a matching entry in the cancellation tuple in
  `_cancel_timers`. The two lists drifted whenever a loop was added
  (resource, backup, db_maintenance, playbook_consolidation each
  needed an edit in both sites), and a missed cancellation handle
  would leak the task across `os.execv` reloads. The runner
  centralises the lifecycle:
  `register(name, factory, *, enabled=True)` collects loops;
  `start_all()` materialises tasks (idempotent — already-live entries
  are skipped); `start(name)` covers the late-enable path used by
  `reload_config` for the resource monitor; `cancel_all()` cancels
  every registered task and awaits them under
  `gather(return_exceptions=True)` so shutdown never raises on a
  worker that already errored. Loop *bodies* still live on
  `SwarmDaemon` because they're tightly coupled to daemon state —
  moving them would require plumbing ~25 closures into the runner
  constructor and split one god class into two. The win that matters
  is separating lifecycle plumbing from business logic; that's what
  this module does. Backward-compat `@property` shims for
  `_heartbeat_task` / `_usage_task` / `_mtime_task` keep the daemon
  tests that directly assigned those attributes working without a
  parallel rename pass. 14 new unit tests in
  `tests/test_loop_runner.py` pin register / start / cancel semantics
  including the idempotent-restart case, single-loop start,
  done-task replacement, and exception-swallowing cancel.

### Fixes

## [2026.5.25] - 2026-05-25

### Features

- **New focused unit tests for three core modules.** Added
  `tests/test_oversight_handler.py` (14 tests) covering the
  signal-to-intervention dispatch in `swarm.drones.oversight_handler`
  — guard clauses, park-proposal emission, rate-limited evaluation,
  operator-engagement skip, redirect message sanitisation;
  `tests/test_state_tracker.py` (37 tests) covering
  `WorkerStateTracker` public surface plus the small private helpers
  the pilot loop depends on (`_build_safe_pattern`, content
  fingerprinting, idle counter, rate-limit debounce, diminishing
  returns, context-pressure thresholds, dead-worker cleanup); and
  `tests/test_queen_tools.py` (55 tests) covering every Queen MCP
  tool — permission gates for non-Queen callers, validation errors
  for missing required args, audit-reason gates, and happy-path
  side-effects on the daemon mock. The three modules were previously
  exercised only indirectly through integration tests; these add
  unit-level coverage that pins behaviour without spinning up a
  daemon.

### Changes

- **SSE keepalive poll loosened from 0.5 s to 5.0 s
  (`src/swarm/mcp/server.py`).** Each long-lived MCP client opened a
  `while True` loop that woke twice per second just to check whether
  the underlying transport had closed. Disconnect detection isn't
  user-visible — broadcast notifications fire on the broadcast call,
  not the poll tick — so the tighter cadence was pure idle CPU. With
  a typical operator running ~5–10 Claude Code sessions concurrently,
  this drops ~10–20 unnecessary wakeups per second per daemon.
- **Dropped a stale `# type: ignore[name-defined]` in
  `src/swarm/drones/pilot.py:53`.** The comment was attached to a
  function definition; the `Any` typevar it referenced is imported at
  module top, so the suppression silenced an error that couldn't
  occur. Removed.
- **De-duplicated repeated `asyncio.get_event_loop().time()` reads in
  `src/swarm/tunnel.py:121-123`.** Three calls in three lines became
  one local-variable assignment, both for clarity and to avoid the
  per-call hash-lookup into the loop registry.

### Fixes

## [2026.5.23.2] - 2026-05-23

### Features

- **Right/Left arrow custom-button actions.** The worker action-button
  picker (Config → Workers) already offered Arrow Up and Arrow Down for
  navigating Claude Code's plan-mode approval prompts and other arrow-
  driven TUIs. Right and Left are now in the same dropdown — useful for
  TUIs that put choices on a horizontal axis (`Y/n` style approvals,
  carousel pickers, file-tree navigation). End-to-end wiring follows
  the existing pattern: ANSI escape (`\x1b[C` / `\x1b[D`) sent through
  `PtyProcess.send_arrow_right` / `send_arrow_left` →
  `WorkerService.arrow_right_worker` / `arrow_left_worker` → daemon
  delegate → `POST /action/arrow-right/{name}` / `arrow-left/{name}` →
  `sendSpecialKey('arrow-right' / 'arrow-left')` from the
  `doAction(action, ...)` dispatcher. The config template (both
  server-rendered options and the JS `buildActionBtnRow` factory) lists
  both alongside the existing Arrow Up / Arrow Down options.

### Changes

### Fixes

- **Drag-reorder for custom buttons now works on mobile.** The
  drag-and-drop reorder on Config → Workers (Action Buttons, Task
  Buttons, Tool Buttons) only wired HTML5 `dragstart` / `dragover` /
  `drop`, which never fire on iOS Safari or most Android touch
  browsers — the rows looked draggable but couldn't actually be
  reordered from a phone. `initDragReorder` in `config.html` now
  carries a touch path that mirrors the desktop flow: a 250 ms
  long-press on a row enters drag mode (with haptic feedback if
  `navigator.vibrate` is available); `touchmove` blocks scroll, uses
  `document.elementFromPoint` to find the row under the finger, and
  paints the same `drag-over` indicator; `touchend` reuses the
  insert-before-vs-after midpoint logic to drop. Touches starting on
  an `input` / `select` / `textarea` / `button` / `label` never
  initiate a drag, so the row's editable fields stay tappable; a
  finger that wanders more than 8 px before the long-press timer
  fires cancels (treated as a scroll). Desktop drag behavior is
  unchanged.

## [2026.5.23] - 2026-05-23

### Features

- **Plan-mode gate for user-request tasks.** Tasks originating from a
  user channel (Jira sync, email import, or the operator dashboard —
  i.e. anything where `SwarmTask.source_worker` is empty) now ship
  with a plan-mode preamble prepended to the dispatch message. The
  worker is instructed to investigate read-only, present a concrete
  plan via Claude Code's `ExitPlanMode`, and park in `WAITING` until
  the operator approves from the dashboard. After approval the worker
  executes the agreed plan. Worker-to-worker handoffs (cross-project
  tasks, MCP `swarm_create_task` with a sender, and the inter-worker
  auto-handoff drone — now correctly tagged with `source_worker`) skip
  the gate entirely: the originating worker has already done the
  reasoning, so a second plan round would just slow the swarm. Wired
  through a single chokepoint in `build_task_message`
  (`src/swarm/server/messages.py`); the preamble explicitly warns
  workers not to fire `/feature` / `/fix-and-ship` skills or call
  `swarm_complete_task` before approval. The behavior is gated by
  `DroneConfig.user_request_plan_mode` (default `True`) — set to
  `False` in `swarm.yaml` to revert to legacy fire-and-forget dispatch.

### Changes

### Fixes

## [2026.5.21.8] - 2026-05-21

### Changes

- **Shared screenshots always route to the Queen.** The Web Share Target
  flow previously read `localStorage.swarm.lastActiveWorker` to decide
  which worker's PTY should receive the shared file — a guessing game
  that mis-routed often enough that the operator had to re-attach by
  hand. Now `checkShareIntent` in `dashboard.js` sends straight to
  `queen` whenever the share has files; the operator tells the Queen
  which worker should pick it up (she can forward via
  `queen_prompt_worker` / `swarm_send_message`). The fallback task
  modal also defaults to Queen, and the `ccMobileFocus` Queen-focus
  hack that wrote `lastActiveWorker = 'queen'` is gone — it was only
  there to paper over the now-removed heuristic.

## [2026.5.21.7] - 2026-05-21

### Fixes

- **CC Queen focus toggle now updates `lastActiveWorker`.** Operator
  follow-up: shared a screenshot while looking at the Queen panel
  (via the mobile Attention/Queen focus toggle) and it routed to
  the `swarm` worker, not the Queen. Pattern: the
  `localStorage.swarm.lastActiveWorker` value was only written by
  `selectWorker()` — the sidebar click handler. The mobile CC focus
  toggle was a separate mechanism that just flipped a body CSS
  class to show/hide panels; it never told the share-target flow
  "the Queen is your active terminal now."

  `ccMobileFocus()` now writes `'queen'` to `lastActiveWorker` when
  the operator picks Queen focus. The attention-focus path
  intentionally doesn't write — the Attention panel spans all
  workers (it's the inbound-escalations surface), so a share while
  attention is focused should fall back to whichever sidebar
  worker was last clicked.

### Features

### Changes

### Fixes

## [2026.5.21.6] - 2026-05-21

### Fixes

- **Web Share Target into worker: don't auto-press Enter.** Operator
  follow-up — the screenshot shared into a worker submitted before
  they could add context. Mobile typing is slow; auto-Enter shipped
  the path without prose.

  Threaded an `enter` kwarg through `daemon.send_to_worker` →
  `worker_service.send_to_worker` → `PTY.send_keys(enter=...)`.
  `POST /api/workers/<name>/send` now accepts an optional
  `"enter": false` in the JSON body (default True preserves the
  prior contract for every existing caller). The share-target JS
  passes `enter: false` so the bracketed `[/path/to/file]` lands in
  the PTY's input buffer, focus switches to the worker, and the
  operator can add prose before pressing Enter themselves. Toast
  updated: "Attached N to <worker> — add context + press Enter."

- **Dashboard URL no longer pasted alongside the path.** Same flow:
  when sharing FROM the PWA, the OS share sheet auto-attaches the
  current page URL as the `url` field — which is the dashboard's
  own host. That ended up in the worker's input buffer as noise.
  JS now drops `share.url` when it parses to the same host as
  `window.location.host`. Cross-app shares (e.g. sharing a tweet,
  a video URL, an article) still include the URL as expected.

### Features

### Changes

### Fixes

## [2026.5.21.5] - 2026-05-21

### Changes

- **Share-target default behavior: route into the active worker's PTY,
  not the New Task modal.** Operator follow-up after `.21.4`: "it
  shared into the app but opens as a task, not just an image into the
  open worker." The original `.21.3` design created a task; the
  operator wanted the screenshot to land directly in whatever worker
  was currently active. This release flips the default.

  New flow when the share lands:
  - Dashboard JS reads `localStorage.swarm.lastActiveWorker` (set by
    `selectWorker()` whenever the operator focuses a worker).
  - If that's set AND the share carries at least one file: build a
    message of `[/abs/path/to/file]` tokens (Claude Code parses
    those as image attachments) + any shared text/url, then POST to
    `/api/workers/<name>/send`. Toast: "Sent N attachment(s) to
    <worker>". Switches focus to the worker so the operator sees the
    result land in the PTY immediately.
  - If no last-active worker OR no file was shared: falls back to
    the New Task modal pre-filled with attachments — the original
    behavior, kept as a safety net.
  - Any send-to-worker failure (HTTP error, worker not found,
    transient state) also drops back to the task modal so the share
    isn't lost — toast surfaces the underlying error.

  Verification: `scripts/check_share_target.py` now exercises BOTH
  paths in one run. With `localStorage.swarm.lastActiveWorker` set,
  the task modal stays closed (`task-modal opened: False`); cleared,
  the modal opens (`task-modal opened: True`). Live screenshot
  capture confirms the toast "Sent 1 attachment(s) to
  public-website" + the dashboard's focus switch to that worker.

  Caught a test-script bug while verifying: the dashboard's boot
  code at `dashboard.js:9667` restores the previously-selected
  worker from sessionStorage via `selectWorker()`, which re-writes
  `localStorage.swarm.lastActiveWorker`. To simulate a
  never-selected state in the fallback test, both storages must
  be cleared.

## [2026.5.21.4] - 2026-05-21

### Fixes

- **Web Share Target was 403'ing on real iOS / Android shares.** Hot on
  the heels of `.21.3` — the operator tried it and got "Origin
  rejected." Root cause: iOS Safari and Android Chrome send Web
  Share Target POSTs with `Origin: null` (because the share is
  initiated by the OS share sheet, not by a page). Our CSRF
  middleware rejects any cross-origin mutating request, including
  the `null` origin.

  Fix: exempt `/share-receive` from the origin check. The session
  cookie still travels with the PWA — and the session-auth
  middleware still runs — so we trust the cookie as the auth
  signal. The X-Requested-With check (applied only to `/api/*` and
  `/action/*` paths) doesn't apply to `/share-receive` anyway, so
  no further bypass needed.

  Verified by replaying the iOS POST shape via curl with
  `Origin: null` — server now returns 303 → `/?share=<id>` instead
  of 403.

## [2026.5.21.3] - 2026-05-21

### Features

- **PWA Web Share Target — share screenshots from phone to Swarm.** When
  the dashboard PWA is installed on iOS (Safari ≥ 16.4) or Android
  (Chrome), "Swarm" now appears in the OS share sheet alongside Mail /
  Messages / Notes / etc. Take a screenshot, tap Share, pick Swarm →
  the screenshot lands as an attachment on a pre-filled New Task
  modal in the dashboard. Title and any shared text/URL pre-populate
  too. If the operator was last looking at a specific worker
  (tracked in `localStorage.swarm.lastActiveWorker`), that worker is
  pre-selected in the assignee dropdown so Submit → task auto-routes
  to "whatever terminal was active."

  Implementation:
  - PWA manifest declares `share_target` with `method: POST`,
    `enctype: multipart/form-data`, file accept covers `image/*`,
    `text/*`, `application/pdf`.
  - New `POST /share-receive` endpoint accepts the OS multipart POST,
    saves attachments via the existing `daemon.save_attachment` path
    (lands in `~/.swarm/uploads/`), stashes the payload + filenames
    in an in-process cache, 303-redirects to `/?share=<id>`.
  - `GET /share/<id>` is single-shot — first caller gets the payload,
    subsequent calls 404. 5-minute TTL keeps interrupted shares from
    lingering.
  - Dashboard JS detects `?share=<id>` on load, fetches the payload,
    opens the New Task modal pre-filled with title + description +
    attached file thumbnails via the existing `taskModalAttachmentPaths`
    + `addThumbnail` path (same flow the email-drop already uses).
    Query string cleaned via `history.replaceState` so refresh
    doesn't re-trigger.
  - `selectWorker()` now also writes `localStorage.swarm.lastActiveWorker`
    alongside the existing sessionStorage entry — sessionStorage
    doesn't survive the OS share-sheet → browser bounce; localStorage
    does. The share landing reads localStorage to pre-select the
    assignee.

  Verification: `scripts/check_share_target.py` simulates a Web Share
  Target POST via Playwright's request context, follows the redirect,
  asserts the modal opens with the title / description / thumbnail
  populated and the URL cleaned. Captures
  `docs/qa-share-target.png` for the record.

### Changes

### Fixes

## [2026.5.21.2] - 2026-05-21

### Fixes

- **Mobile dashboard tighter — three operator complaints addressed.**
  - **Worker search + state filter chips hidden** under 600 px. They
    were eating vertical space on a phone where the worker list is
    short anyway and the workers themselves are visible / tappable
    right below.
  - **Focus toggle buttons (Attention / Queen) sized to content** —
    they were `flex: 1` so each claimed half the row width, which
    the operator called "huge." Now `flex: 0 0 auto` + 0.4/0.8 rem
    padding. Still satisfies the 44 px touch min-height.
  - **Queen action buttons wrap inline instead of locking to a
    2-column grid.** Was `display: grid; grid-template-columns:
    1fr 1fr` so every button (Refresh / Continue / 1 / 2 /
    Get Latest / Clear / Kill / Revive) claimed 50% of the screen.
    Operator: "they should only be as wide as they need to be so
    more fit on one line." Now `display: flex; flex-wrap: wrap`
    + content-sized buttons. Six fit per row instead of two; same
    44 px touch target preserved on each.

  All three changes are pure CSS under `@media (max-width: 600px)`.
  Verified via Playwright at 390×844 — header is now ~50 px shorter
  (no worker search bar), focus toggle compact, action row holds
  5 buttons in one line where the old grid held 2.

### Features

### Changes

## [2026.5.21] - 2026-05-21

### Features

- **Playbook detail modal — body, trigger, provenance, actions, events all
  in one place.** Previous version showed events-only, which left
  operators with empty modals on every candidate (uses=0 → no events).
  Operator couldn't see what a playbook actually CONTAINS before
  deciding whether to promote.

  Modal now renders:
  - Title + status badge + scope / uses / winrate / version / last-used
  - **Promote to Active** (candidates) + **Retire** (anything non-retired)
    buttons inside the modal — no need to dismiss + find the row
  - **Trigger** — what conditions tell a worker this playbook applies
  - **Body** — the actual playbook content in a monospace `<pre>` block,
    scrollable at max-height 320px; this is what a worker would see
    if the playbook got injected at task dispatch
  - **Provenance** — task chips that link to the linked-task editor
    (uses the cleanup-batch's `openLinkedTask` flow)
  - **Source worker + timestamps** for context
  - **Events** timeline (was previously the only content) below; if
    empty, shows "(none yet — playbook hasn't been applied to a task)"
    so operators understand why it's blank instead of assuming the
    modal is broken

  Modal sized up from `modal-md` (550px) to `modal-lg` (650px) for the
  longer body content. Title de-duplicated when `pb.title` matches the
  slug. `GET /api/playbooks/{name}/events` enriched to return the
  playbook itself alongside the events array; clients get everything
  in one fetch.

- **Bulk select on the playbook list.** Operator follow-up: 23
  candidates one-at-a-time was painful. New **Select…** button in the
  filter bar flips bulk mode on; each row gets a checkbox; a bulk
  action bar appears showing the selected count plus **Promote
  selected** / **Retire selected** / **Cancel**. Promote-selected
  parallel-POSTs to `/api/playbooks/{name}/promote` for every checked
  row; retire-selected prompts once for a reason and applies it across
  the batch. Summary toast names success + failure counts.

### Changes

### Fixes

## [2026.5.20.15] - 2026-05-20

### Fixes

- **Playbooks tab: one-row-per-card layout.** Follow-up to `.14`'s
  compact analytics — the cards themselves still rendered 3 rows
  deep each (title row + trigger row + Promote/Retire button row),
  so 23 candidate playbooks looked like a wall of identical-looking
  buttons. Restructured the card to a single row: status icon +
  title (ellipsis-truncated, full text on hover) on the left;
  status badge + scope + win/uses/prov + Promote/Retire all
  right-aligned. Trigger snippet moved to the events-timeline
  modal (open by clicking the title). Roughly 2× the density —
  3 playbooks visible in the space that used to hold 1.5. Below
  900 px the row wraps to two rows (title above, meta + actions
  below) so phone widths stay readable. Dropped the per-mover
  panel scrollbar from `.14` since the top-3 entries always fit
  without it.

## [2026.5.20.14] - 2026-05-20

### Changes

- **Per-tab action buttons in the bottom panel header.** The
  tab-header utility buttons (Preview Jira / Sync Jira / + New
  Task) used to show on every tab regardless of which one was
  active, and "+ New Pipeline" lived in a separate row below the
  tab nav — visual inconsistency the operator flagged with a
  screenshot. P-fix: every button in `.tab-header-utils` now
  declares `data-show-on-tab="<tab>"`; `switchTab` toggles inline
  display so only the relevant actions for the current tab
  appear. "+ New Pipeline" moved up into the same row as
  "+ New Task" (Pipelines tab); the in-content `filter-bar`
  wrapper around it is gone. Tasks tab → Preview Jira + Sync
  Jira + New Task. Pipelines tab → New Pipeline. Playbooks /
  Decisions / Activity → nothing (no creation action; playbooks
  are auto-synthesized, decisions are inbound, activity is read-only).

### Fixes

- **Playbooks tab layout compacted.** Operator screenshot showed
  the P4a analytics summary band consuming ~60% of the visible
  bottom panel — stat tiles + 3-column movers each at 240+ px
  pushed the filter chips and the actual playbook cards below
  the fold. Tightened: `.pb-analytics` now uses a flex row that
  floats movers next to the stat tiles instead of stacking;
  stat tiles dropped from 70 px → 56 px min-width with smaller
  font; mover lists clamp at 5.5 em with internal scroll instead
  of growing unbounded; mover names ellipsis on overflow.
  Result: filter chips + the first few playbook cards visible
  on a typical bottom-panel height; no information lost, just
  packed.

## [2026.5.20.13] - 2026-05-20

### Features

- **Spec: No-AI-Slop content system v1.** New
  `docs/specs/content-system-v1.md` captures the 4-round, 16-decision
  interview for the content orchestration system the
  `project_no_ai_slop_content_system` memory anticipated. Single-creator
  with a `creator_id` hedge for future multi-tenancy. New
  `content_ideas` + `content_pieces` tables (v12 schema). Eight-stage
  enum (idea → planned → scripted → filming → edited → staged →
  published → analyzed). Six target platforms (YouTube as anchor +
  X / Instagram / TikTok / Pinterest / Facebook). API where available,
  browser v2 as fallback. Source idea → platform-specific children
  for repurposing. OneDrive integration via the existing Microsoft
  Graph OAuth. Voice corpus warms over time (no day-1 corpus). Idea
  capture nightly @ 2am from YouTube competitor scrape + new
  `swarm_capture_idea` MCP tool + email forwarding to an `ideas@`
  address. Weekly planning Queen brief Sunday @ 9am ingests captured
  ideas + analytics snapshots. Analytics daily @ 6am scrape feeds
  back into next week's planning. Dashboard "Content" tab (Ideas /
  Pieces / Analytics sub-views) + Queen escalations for every HITL
  gate. Ships as **4 phases** across ~12-15 weeks (A: idea capture
  ~2w, B: planning + scripting ~2-3w, C: filming/editing/posting
  ~6-8w, D: analytics feedback ~2w). v1 explicitly accepts the
  months-scale commitment. Force-added past `docs/specs/` gitignore.

### Changes

### Fixes

## [2026.5.20.12] - 2026-05-20

### Features

- **Spec: managed browser capability v1.** New
  `docs/specs/managed-browser-v1.md` capturing the 4-round interview
  decisions for the upcoming `swarm_browse` MCP tool. Scope covers
  Playwright Python in-process, named persistent profiles + ephemeral
  default, `swarm browser login <profile>` headed CLI for setup, five
  v1 actions (`navigate` / `screenshot` / `extract_links` / `fill_form`
  / `click`), per-call timeout, per-profile domain allowlist,
  confirm-before-submit Queen escalation on sensitive forms, audit
  log per call. Spec only — implementation downstream. Force-added
  past the `docs/specs/` gitignore, matching the pattern used for the
  P3 + post-overhaul-cleanup specs.

### Changes

### Fixes

## [2026.5.20.11] - 2026-05-20

### Fixes

- **Mobile visual fixes from the QA findings doc — P1 through P6.**
  Pure CSS, no JS / HTML / backend changes. Before/after screenshots
  via the QA harness confirm every item:
  - **P1 (BLOCKER) — Queen card compacted on phone.** Was ~140px tall
    eating 40% of mobile viewport; now ~50px with `padding: 0.4rem
    0.55rem`, 24×24 bee icon (down from 32×32), and `queen-name`
    forced to single-line ellipsis. Worker list now starts within
    a thumb's reach of the top instead of after a half-screen scroll.
  - **P2 (HIGH) — Status strip label/value separation.** Was
    "queue0/0 last hr14 today56 5h0%" (labels colliding with
    values); now "queue **0/0**  last hr **9**  today **59**" with
    a proper `0.35em` gap inside each `.cc-qs-item`. Reads at a glance.
  - **P3 (HIGH) — Digest strip horizontal scroll.** Was truncating
    "completed: Ext…" with no affordance; now `overflow-x: auto` on
    mobile so the operator can scroll the whole digest if it spills.
  - **P4 (MEDIUM) — Hide BUZ/RES/SLE pills in header on mobile.**
    Was wrapping to 3 vertical lines (60+ px header height); now
    hidden under 768px. The same counts are available via the
    worker-state filter chips directly below.
  - **P5 (MEDIUM) — Hide "operator command center" subtitle on
    phone.** Was wasting 3 lines inside the Queen card; now
    `display: none` on `.queen-meta` under 600px.
  - **P6 (LOW) — Attention empty-state word-wrap.** Was truncating
    mid-word ("the swarm i…"); now `white-space: normal` +
    `word-break: normal` on `.cc-empty` under 600px renders the full
    "Nothing needs you — the swarm is running clean" cleanly.
  Re-run of `scripts/mobile_qa.py` after the fix confirms zero
  pageerrors, all six visual issues resolved at 390px. Spec /
  findings at `docs/qa-mobile-findings-2026-05-20.md`.

## [2026.5.20.10] - 2026-05-20

### Features

- **Mobile QA Playwright harness + first run's findings.** New
  `scripts/mobile_qa.py` drives Chromium at iPhone-14 viewport
  (390×844) through nine touch points across the dashboard — Command
  Center default, Attention/Queen focus toggle, each bottom-panel
  tab (Tasks / Decisions / Pipelines / Playbooks / Activity),
  config General + Automation — and captures full-page screenshots
  plus a `FINDINGS.md` scaffold listing any console errors or
  pageerrors the page produced. Auth uses a session cookie minted
  from the API password (gitignored `.env`). One-shot QA tool, not
  a test suite — re-run with `uv run python scripts/mobile_qa.py`
  after fixes to compare. Screenshots land in
  `docs/qa-mobile-<timestamp>/` (gitignored — large PNG binaries,
  easy to regenerate); the curated findings doc is at
  `docs/qa-mobile-findings-2026-05-20.md`.

### Changes

### Fixes

- **Two pre-existing JS reference errors uncovered by the QA harness.**
  Both fired on every page load before; only surfaced now because
  Playwright's pageerror listener doesn't swallow them like the
  dashboard's defaults did.
  - `queenCooldownTimer` was referenced in the unload cleanup at
    `dashboard.js:9383` but never declared at module scope. Added
    the missing `var queenCooldownTimer = null` next to the other
    timer declarations in IIFE 1.
  - `updateQueenHealthIndicator` is defined in IIFE 2 (line ~10465)
    but called as a bare reference from the WS event dispatcher
    in IIFE 1 (line 624). The two IIFEs are separate scopes —
    fixed by exposing the function on `window` from IIFE 2 and
    guarding the call site with `typeof window.X === 'function'`
    so a future scope shuffle can't silently break it again.
  Both fixes verified by re-running the QA harness: the
  `pageerror` listener captured zero errors on the second run.

## [2026.5.20.9] - 2026-05-20

### Features

- **Cleanup batch — follow-up to the P1–P6 UX overhaul series.** Closes
  the four gaps named in earlier commits' "deferred" sections, plus
  the unrelated test_ws_auth flake that bit three full-suite runs.
  Spec at `docs/specs/post-overhaul-cleanup.md`. New tests + lint
  clean; full suite 4421 passing (up from 4406).
  - **Linked-task-by-ID.** New `GET /api/tasks/{task_id}` returning
    the rich task dict (every field the editor reads, not just the
    7-field list-view summary). New `showTaskEditorById(id)` helper
    in `dashboard.js` fetches that endpoint and feeds the existing
    `openTaskModal('edit', data)`. P3's pipeline-step task chip now
    actually opens the editor instead of falling back to
    scroll-and-flash. The scroll-and-flash code stays as a defensive
    fallback if the fetch 404s.
  - **PlaybookConfig range validation.** New `_validate_playbook_ranges`
    mirroring `_validate_drone_ranges`. Rejects winrate / similarity
    values outside `[0.0, 1.0]`, `auto_promote_uses` / `prune_min_uses`
    below 1, negative `min_resolution_chars` / `max_synth_per_hour`,
    and `consolidation_interval_seconds` below the engine's 300s
    floor. Dashboard sliders prevent the common case but the REST
    endpoint is publicly addressable so this is the only gate
    against a direct bad POST. Errors raise `ValueError` → 400 with
    explicit `playbooks.X must be …` messages.
  - **Retry-on-COMPLETED with confirmation modal.** Operator who
    really needs to re-run a COMPLETED step can now do so —
    `engine.retry_step` gains a `confirmed=False` kwarg; without
    it the engine still rejects non-FAILED (back-compat preserved).
    Route accepts `{"confirmed": true}` body and threads it through.
    A new modal in the P3 detail view gates the action behind a
    required checkbox + explicit warning about side effects
    (shell commands re-execute, webhooks re-fire, agent tasks
    re-create). FAILED retry still skips the modal. Cascade
    behaviour is unchanged: only FAILED downstream descendants
    reset, even on a confirmed COMPLETED retry — re-firing a
    whole completed subtree is a separate decision we deliberately
    deferred. The detail view's per-step button is `⚠ Retry`
    (amber) for COMPLETED to visually distinguish from FAILED retry.
  - **test_ws_auth flake fixed.** The 30s pytest-timeout was
    catching `selector.poll` during pytest-asyncio's event-loop
    teardown — but the timed body did nothing async itself. Root
    cause: imports were inside each test function (`from
    swarm.server.api import _RATE_LIMIT_WINDOW`), so the first
    test in the file paid the full `swarm.server.api` import cost
    while the timeout was already counting. Hoisting the imports
    to module level moves the work into collection. Was a
    pre-existing flake that hit three earlier full-suite runs.

### Changes

### Fixes

## [2026.5.20.8] - 2026-05-20

### Features

- **Playbook config tuning UI — P4b, the deferred half of P4.** Wires
  `PlaybookConfig` through all six layers of the config-save-chain so
  operators can edit the synthesis loop's tuning knobs from the
  dashboard instead of hand-editing `swarm.yaml`. Adds a new
  Playbooks pane to the Automation tab with three sections —
  Synthesis (enabled / eligible task types / min resolution chars /
  hourly cap), Promotion + Pruning (auto-promote uses + winrate
  slider, prune min uses + winrate slider), and Consolidation
  (interval seconds + dedupe similarity slider + Skills install
  toggle). Winrate / similarity fields render as 0–100% sliders
  backed by hidden float inputs so `buildPayload` reads clean
  0.0–1.0 values without re-doing the math.

  Per the config-save-chain audit (`docs/audits/config-save-chain-2026-05-04.md`):
  L1 dataclass (already existed); L2 form added to `config.html`;
  L3 dispatcher — `"playbooks"` added to `_KNOWN_BODY_KEYS` + a new
  `if "playbooks" in body` branch in `apply_update`; L4 handler —
  new `_apply_playbooks()` method routing through the generic
  `_apply_dataclass_dict` dispatcher so unknown keys land in the
  structured FieldOutcome (no silent drops); L5 persistence —
  `"playbooks"` added to `_JSON_KEYS` + new `_serialize_playbooks()`
  in `serialization.py`; L6 load — `"playbooks"` added to the
  `_DATACLASS_BLOBS` map so the generic `_parse_json_dataclass`
  loader picks it up automatically. `PlaybookConfig` is now
  re-exported from `swarm.config` for consistency with the other
  top-level dataclasses.

  3 new tests: round-trip through save/load (all 11 fields verified),
  `apply_update` happy path, and an unknown-key check that asserts
  bogus body fields surface in the FieldOutcome's `unknown` list
  rather than getting silently swallowed — that was exactly the
  failure mode the audit's silent-drop bug class produced before
  Phase 7 of #328 added the structured outcome shape.

### Changes

### Fixes

## [2026.5.20.7] - 2026-05-20

### Features

- **Mobile global polish — P6 of the editor UX series.** Wraps up the
  cross-cutting mobile pass that the audit punch-list flagged. All
  interactive elements inside `@media (pointer: coarse)` are now at
  the 44px iOS/Material tap-target minimum — `.btn`, `.tab-btn`,
  `.worker-item`, `.queen-banner-actions .btn`, `.mobile-overflow-btn`
  (the header hamburger), and `.resize-handle` (24px on touch — the
  prior 12px was unreliable). `.filter-chip` jumps from 32px to 40px
  with more padding so the chip rows are actually tappable. Under
  600px, the task editor's primary-metadata row (priority / type /
  status / worker / tags) wraps with each field at 100% width instead
  of fighting for space in the flex line, and the first filter-chip
  in every filter bar gets `position: sticky; left: 0` so the "All"
  reset stays grabbable while the row horizontal-scrolls. Worker
  names in the sidebar pill drop the 140px max-width truncation at
  ≤768px — names wrap to a second line rather than ellipsis-clipping
  on a phone where there's room for the full string. The Activity
  (Buzz Log) filter chips were previously hidden entirely on mobile;
  P6 brings them back as a `<select>` paired with the chips, kept in
  sync by `switchBuzzFilter` and a change listener — single-category
  filtering on phone, multi-chip on desktop.

### Changes

### Fixes

## [2026.5.20.6] - 2026-05-20

### Features

- **Mobile Queen dashboard rescue — P5 of the editor UX series.** The
  Command Center stacked at 900px and then never adapted further; on a
  phone (~390px) it was effectively unusable — the Queen action row
  crushed eight buttons into one cramped line, the status strip
  shrunk to 0.65rem to fit, and the Attention card body clipped
  worker messages at 4em. P5 adds a `<600px` breakpoint that:
  switches the layout into a one-panel-at-a-time mode controlled by a
  new tab strip above the grid (Attention / Queen) that lets the
  operator pick which surface gets the full screen height — Attention
  defaults if there's pending work, Queen otherwise, and the choice
  persists in localStorage so a re-render doesn't reset it; Queen
  action buttons render as a 2-column grid with 44px tap targets;
  status strip wraps to multiple lines at 0.75rem instead of
  shrinking to unreadable; Attention card body / detail lose the 4em
  max-height so escalation messages render in full; Queen terminal
  holds a `min-height: 280px` when focused so the PTY isn't tiny. The
  Attention focus button mirrors the pending-attention count in its
  label so the operator sees what's waiting before flipping panels.

### Changes

### Fixes

## [2026.5.20.5] - 2026-05-20

### Features

- **Playbooks analytics — P4a of the editor UX series.** The Playbooks
  tab gains a summary band at the top showing per-status totals
  (active / candidate / retired) and a rolling 24-hour event window
  (applied / wins / losses), plus a movers panel: top 5 by uses, top 5
  by winrate (gated on `uses >= 3` so a single lucky win can't dominate
  a 50-and-10 active), and a per-scope breakdown (global / project / worker
  with totals and derived winrate). The flat list below gains status
  chips (All / Active / Candidate / Retired) and a scope dropdown that
  filters client-side from a single fetch. Clicking a playbook title
  (or a row in the movers panel) opens a new event-timeline modal
  rendering the `playbook_events` rows newest-first, color-coded by
  event type (synthesized / applied / win / loss / promoted / retired /
  consolidated) with the task ID / worker / detail on each row. Two new
  PlaybookStore methods (`get_events_for_playbook` + `get_analytics`)
  power two new endpoints (`GET /api/playbooks/{name}/events`,
  `GET /api/playbooks/analytics?since_hours=N`). Pure aggregation — no
  schema changes, rides the existing `(playbook_id, ts)` index on
  `playbook_events`. Winrate is `-1.0` when no outcomes have been
  attributed yet so the UI can render "—" instead of misleading "0%".
  Config tuning UI for `PlaybookConfig` was split off as P4b — the
  config-save-chain wiring is risky enough (silent-drop bug class lives
  there) to deserve its own pass rather than getting bundled.

### Changes

### Fixes

## [2026.5.20.4] - 2026-05-20

### Features

- **Pipeline detail view + retry — P3 of the editor UX series.** Adds a
  read-only inspect modal that opens when an operator clicks anywhere on
  a pipeline card. The step list is grouped by execution wave (Kahn-style
  levelization client-side; same DAG the engine's `advance()` walks),
  each step shown with status / duration / linked task chip / error +
  pretty-printed result. For `shell_command` results, stdout / stderr /
  returncode are surfaced as labeled blocks above the raw JSON. A Copy
  button is on every result block. New `POST /api/pipelines/{id}/steps/
  {step_id}/retry` endpoint resets a FAILED step plus its FAILED
  downstream descendants (BFS forward through the DAG); SKIPPED and
  COMPLETED downstream are left alone — SKIPPED is sticky operator
  intent and re-running a COMPLETED side-effecting step would
  double-fire it. The retry resets `status`, `started_at`,
  `completed_at`, `error`, `result`, and `task_id` so the engine
  re-creates fresh tasks for agent steps. 404 for unknown pipeline/step,
  409 for non-FAILED targets. The detail view subscribes to the existing
  `pipelines_changed` WS event for live re-render — steps tick through
  pending→ready→in_progress→completed without refreshing the page.
  Detail modal has its own Edit button (only when status ∈
  {DRAFT, PAUSED}, matching the engine guard) that warps into the
  P1 editor pre-filled. Pipeline metadata header shows timezone /
  schedule / tags / template_name / created. Linked-task chips switch
  to the Tasks tab + scroll-and-flash the row (the existing task editor
  isn't ID-addressable so we don't open it directly — flagged as
  deferred in the spec). Spec lives at
  `docs/specs/pipeline-detail-view.md`. 11 new tests (7 engine, 4 route)
  covering the cascade-reset semantics, SKIPPED/COMPLETED preservation,
  and status-code mapping. P4 in the series adds the playbook
  analytics + config tuning surface.

### Changes

### Fixes

## [2026.5.20.3] - 2026-05-20

### Features

- **Pipeline schedule builder + per-pipeline timezone — P2 of the editor
  UX overhaul.** Replaces the free-form `HH:MM`/cron text input with a
  preset picker (On-demand / Daily / Weekly / Weekdays / Hourly / Custom
  cron) that emits the same cron string the engine reads, plus a live
  preview wired to a new `POST /api/pipelines/schedule/preview` endpoint
  — "Weekdays at 14:30" and the next five fire timestamps update as the
  operator edits. Per-step `schedule` inputs gain the same inline
  preview without the full builder so quick edits stay quick. Added a
  curated 30-zone IANA timezone select to the Basics section (custom
  values typed previously are preserved as a sticky option so saves
  don't drop them). `Pipeline.timezone` is a new optional string field
  — empty preserves legacy server-local evaluation; populated routes
  through `zoneinfo.ZoneInfo` so cron expressions fire in the
  operator's frame regardless of where the daemon happens to run.
  Timezone is the only field freely editable while a pipeline is
  RUNNING (steps still need DRAFT/PAUSED); fixing a misconfigured
  zone shouldn't require pausing the work. New
  `swarm.pipelines.schedule` module holds the normalize / humanize /
  preview helpers and is pure-stdlib + croniter so the same code runs
  the engine match path and the editor preview. Edit mode reverse-
  engineers a saved cron back into the matching preset for visual
  consistency; un-presettable expressions land in the Custom cron pane.
  Persistence rides the existing JSON-blob column on the `pipelines`
  table — no schema migration needed since `pipeline_from_dict`
  tolerates the absent field on old rows. P3 in the series adds the
  detail view + DAG visualization.

### Changes

### Fixes

## [2026.5.20.2] - 2026-05-20

### Features

- **Pipeline editor — P1 of the multi-phase UX overhaul.** Replaces the
  single-modal create flow that couldn't reach automated steps with a
  sectioned editor (Basics / Steps / Schedule). Each step gets a card
  layout; conditional fields appear by step type — Agent shows worker +
  task_type dropdowns plus description; Human shows description; Automated
  finally surfaces a Service dropdown (populated from the new
  `GET /api/pipelines/services` endpoint) and a JSON config field with a
  "Use example" button that pre-fills the registered handler's
  `example_config`. Dependencies became a chip picker over already-defined
  steps; cycles and duplicate IDs are rejected client-side before submit.
  Step rows on the list now surface `error` and `result` text (previously
  hidden in the model), and pipelines in DRAFT/PAUSED show an **Edit**
  button that re-opens the same modal pre-filled and submits via
  `PUT /api/pipelines/{id}` — `PipelineEngine.update()` was extended to
  accept a `steps=` list under the DRAFT/PAUSED guardrail, raising
  `ValueError` that the route handler maps to 409 once a pipeline is
  RUNNING. Built-in handlers (`shell_command`, `webhook_notify`,
  `headless_claude`, `file_uploader`, `youtube_scraper`,
  `claude_code_security`) now advertise `description` + `example_config`
  attrs that feed the dropdown via `ServiceRegistry.describe()`. P2 in the
  series replaces the still-text-only schedule input with a cron builder.

### Changes

### Fixes

## [2026.5.20] - 2026-05-20

### Features

### Changes

### Fixes

- **Task editor accepts a literal `?` again.** The global `?`
  keyboard-shortcut handler bailed on `INPUT`/`TEXTAREA`/`SELECT` but
  not on `contenteditable`, and the task editor's description field is
  a contenteditable div — so typing `?` opened the shortcuts modal and
  swallowed the keystroke. Added an `isContentEditable` guard.

## [2026.5.19.4] - 2026-05-19

### Features

- **Operator-blocked-stall guard — a task waiting on the operator no
  longer churns ACTIVE forever.** Incident: #443 sat `active` while the
  worker stood by for an operator hand-back; over 12h that drew ~259
  drone CONTINUED + ~63 oversight interventions + ~46 completion
  proposals (each a headless Queen call), zero progress. Now the
  oversight monitor tracks a per-(worker, task) no-progress streak
  (`task.updated_at` frozen while ACTIVE across N drift-cadence checks —
  deterministic and Queen-free so it survives a rate-limit storm); at
  `auto_park_no_progress_checks` (default 3, ~30 min) it raises **one**
  `ProposalType.PARK` proposal. Approve → `TaskBoard.block_for_operator`
  parks it to the existing #405 `BLOCKED` hold (idle-watcher, completion
  loop and reconciler already skip BLOCKED, so every churn loop stands
  down with no new guards); the worker resumes on the normal operator
  re-dispatch (`activate` → BLOCKED→ACTIVE, `block_reason` cleared).
  Dismiss → `auto_park_reject_backoff_seconds` (default 2h) before it
  can re-propose. Pending-park dedupe (`has_pending_park`) freezes the
  oversight/completion churn while the proposal awaits the operator. The
  proposal surfaces as a normal Approve/Dismiss decision card via the
  existing exception-queue path — deliberately *not* an extra
  modal/push (single-source-of-truth, no new interruptive notification).
  New `queen.oversight` knobs: `auto_park_enabled`,
  `auto_park_no_progress_checks`, `auto_park_reject_backoff_seconds`.

### Changes

### Fixes

## [2026.5.19.3] - 2026-05-19

### Features

### Changes

### Fixes

- **WAITING workers poll at the base cadence (fast resume detection).**
  `compute_backoff` applied the same idle exponential backoff to WAITING
  as to truly-idle RESTING workers, so a WAITING worker was polled as
  rarely as every `max_idle_interval` (30s default) and memory pressure
  doubled it again — WAITING→BUZZING took 30–60s to show after the
  worker actually resumed. A WAITING worker is the one *most* likely to
  resume imminently (it was just answered/unblocked), so it now polls at
  the flat base interval (`poll_interval_waiting or base`), exempt from
  both the idle-streak multiplier and the memory-pressure doubling.
  Focus still only speeds it up further; RESTING/BUZZING backoff
  unchanged. Resume is now observed in ~base seconds (5s default).

## [2026.5.19.2] - 2026-05-19

### Features

### Changes

### Fixes

- **Toast notifications can no longer wall the screen.** Toasts are
  one-line glances, but `showToast` had no text cap, no stack limit and
  no height clamp, and `WORKER_STUNG` broadcast its full 30-line
  terminal tail straight into a toast — a burst produced an unreadable
  paragraph wall while the Attention panel stayed empty (those events
  are Queen-handled / live in the Activity tab). Now: `toast.js`
  collapses whitespace and hard-caps to one ellipsised line
  (`TOAST_MAX_CHARS`), and keeps only the newest `TOAST_MAX_STACK` so a
  flurry can't fill the viewport; `.toast` CSS clamps height as
  defense-in-depth; `StatePublisher.on_drone_entry` broadcasts a terse
  first-line summary (`_terse_detail`) for the `system_log` WS event and
  the push notification while the **full** multi-line detail still lands
  in the buzz log for the Activity tab (no diagnostics lost). Per the
  operator decision, Queen-handled events stay as terse FYI toasts
  rather than being suppressed.

## [2026.5.19] - 2026-05-19

### Features

- **Answer a waiting worker's choice prompt from the Attention card.**
  When a `worker-waiting` exception is a numbered Claude choice menu,
  the card now renders the worker's *own* options as buttons; clicking
  one sends that selection straight to the worker's PTY (same path the
  Queen 1/2 strip uses) instead of forcing "Open terminal" + typing.
  `attention_model.extract_choice_options()` parses the captured WAITING
  tail with the same cursor/plain-option shape the Claude provider uses
  for detection (requires a focused `>`/`❯` option **and** another
  numbered line, so prose with stray "1." doesn't sprout fake buttons);
  options ride on `ExceptionItem.options`; the generic Open terminal /
  Force rest verbs stay as the fallback. Pure + unit-tested; no options
  parsed ⇒ unchanged behaviour.

### Changes

### Fixes

## [2026.5.18.3] - 2026-05-18

### Features

### Changes

### Fixes

- **Actionable cross-worker handoffs no longer fall through both drone
  nets (task #442).** A handoff carried only by a `dependency`/`warning`
  message to a recipient who is idle *and* task-less was silently lost:
  the IdleWatcher skipped it (no task to carry) and a one-shot
  InterWorkerMessageWatcher nudge dies on a missed turn or a daemon
  restart, leaving the published work unconsumed with nothing tracking
  it (the public-website #985 → realtruth incident; #441 was the manual
  backfill). The watcher now spawns a **tracked task** assigned to the
  recipient (`daemon._spawn_handoff_task` → `assign_and_start_task`), so
  the IdleWatcher durably carries it to completion. Scoped to
  action-bearing types only (informational `status`/`finding` still just
  nudges — no board flooding), idempotent per message id, logged as
  `AUTO_HANDOFF_TASK`, and a no-op when the spawn callback is unwired
  (graceful fallback to the prior nudge-only behaviour).

## [2026.5.18.2] - 2026-05-18

### Features

- **Attention panel → exception queue.** The dashboard Attention panel
  was the operator's old coordinator feed (every worker→Queen message,
  every worker idle >15s, recency-sorted, bare Reply/Dismiss). Now that
  the Queen coordinates the swarm, that feed is mostly already-handled
  noise. It is rebuilt as an exception queue that surfaces only what is
  genuinely escalated to a human or a hard failure the autonomous layers
  can't resolve.
  - New pure classifier `swarm.server.attention_model.classify()` —
    snapshot-in, `{critical, decision, handled}`-out, no I/O, fully
    unit-tested. `routes/attention.py` just gathers live snapshots
    (threads, pending proposals, worker state, buzz log, blockers,
    resource pressure) and delegates.
  - **Suppression filter:** worker-messages (Queen owns them via #235
    auto-relay), nudged/blocked waiting workers, reviving crashes, and
    proposals inside the autonomous-approval window drop into a
    collapsed "the swarm is handling" drawer instead of the queue.
  - **Severity model:** `Critical` / `Needs your decision` sections,
    oldest-first within each, plus age-escalation — a decision
    unresolved past 30m auto-promotes to Critical with a `STALE` marker
    (fixes "a stale proposal looks like a fresh crash").
  - **Action-first cards:** each carries a "what's been tried / why
    it's yours" detail line and type-correct verbs — proposals get
    inline Approve/Dismiss (reusing existing endpoints), crashes get
    Revive, waiting workers get Open terminal / Force rest.
  - **Layout:** the queue fills the top; the "swarm is handling" region
    is pinned to the bottom third with its own scroll and a sticky
    collapse toggle.

### Changes

### Fixes

- **Attention no longer claims the Queen is working on an idle thread.**
  A `worker-message` thread stays `active` until something explicitly
  resolves it, which the Queen rarely does — she just moves on. The
  classifier now keeps a worker-message in the drawer only if it is
  fresh (touched < 10m) **or** the Queen is actively BUZZING; a stale
  thread with an idle Queen is dropped entirely. Honest reasons
  ("with the Queen now" / "relayed — awaiting her next turn"), never a
  false "handling".
- **Interruptive notifications aligned to the exception queue (single
  source of truth).** Browser/OS notifications fired on event creation
  while the panel surfaces on escalation-to-a-human-decision, so a
  worker hitting a choice menu pinged the operator with an empty
  Attention panel. `escalation` and `proposal_created` are downgraded
  to FYI toasts; `escalation_handler.on_escalation` no longer emits a
  premature desktop notification (the Queen handles it; if she can't it
  returns as a `queen_escalation` proposal with its own banner +
  decision card); `proposals._notify_proposal` notifies only for
  ESCALATION proposals (assignment/completion sit silently in the
  autonomous window). The classifier-derived `maybeNotifyAttention`
  remains the one path that pings when something actually needs you.

## [2026.5.18] - 2026-05-18

### Features

- **Native `/goal` seeding from task acceptance criteria (v1).** When a
  task with `acceptance_criteria` is dispatched to a worker whose CLI
  has a native session-scoped `/goal` (Claude Code v2.1.139+, Codex),
  Swarm injects `/goal <condition>` after the task message. The
  provider's own small-fast-model evaluator then runs the keep-working
  loop — Swarm builds **no** evaluator, subprocess, or metered API call.
  This is the *proactive* complement to the existing *reactive*
  post-completion verifier (which stays as the backstop): it reduces
  premature `swarm_complete_task` calls rather than reopening after the
  fact. Inspired by Claude Code's native `/goal` (the "separate the
  agent that works from the one that decides it's done" pattern).
  - `LLMProvider.supports_native_goal` capability — `True` for Claude
    and Codex; Gemini/OpenCode/generic inherit `False` → a clean no-op
    there (the generic idle-watcher remains the only safety net;
    provider-neutral by capability detection, not assumption).
  - `render_goal_condition()` turns criteria into a one-line condition
    with a proof directive (the evaluator only judges the transcript,
    not files) and the docs-recommended `or stop after N turns` runaway
    bound; ≤ 4000 chars.
  - `DroneConfig.native_goal_enabled` (default on, operator-reversible)
    and `native_goal_max_turns` (default 25).
  - Seeded only from `start_task` (the dispatch boundary) so it is
    set-once-per-dispatch — idle-watcher nudges never re-arm it.
    Best-effort: a `/goal` send failure cannot unwind a started task.
    Logged as `SystemAction.GOAL_SET`.
  - Coordinator/orchestrator-level `/goal` (Queen / project-root holding
    a macro objective) is deliberately **out of scope for v1** — filed
    as a separate `/interview`-driven initiative.

## [2026.5.17.9] - 2026-05-17

### Fixes

- **Attention queue cards now word-wrap their titles instead of
  truncating.** `.cc-attention-card-title` forced single-line
  truncation (`overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap`), so a multi-line escalation title showed as
  `swarm: Status correction (oper…`. Now `white-space:normal;
  overflow-wrap:anywhere; word-break:break-word` — the full title wraps
  to as many lines as it needs (long unbroken tokens like a path/URL
  wrap too). `.cc-attention-card-meta` gained `white-space:nowrap;
  flex:0 0 auto` so the `worker · age` meta stays pinned on one line as
  the title grows downward (the card head is a baseline-aligned flex
  row). CSS-only, `base.html`.

## [2026.5.17.8] - 2026-05-17

### Fixes

- **`swarm_park_task` no longer silently parks the wrong task** (#407;
  follow-up to #406, off the 2026-05-17 public-website incident). #406
  shipped with no task argument — it parked "the" active task via
  `current_task_for_worker()`. When a worker legitimately owns >1 ACTIVE
  task (legal pre-#405-reload / un-reconciled board state), that
  iterated `_tasks` and set down an arbitrary one: public-website owned
  #393/#394/#398/#399, intended #399, the tool parked the
  genuinely-blocked #393 instead. A state-mutating worker tool that
  silently targets the wrong task corrupts board truth and can
  de-silence a correctly-blocked task's idle-watcher — the exact skew
  #405 was meant to end.
  - `swarm_park_task` now accepts an explicit `task_number`; parks
    exactly that task (rejected, no mutation, if not owned by the caller
    or not ACTIVE).
  - Omitted + caller owns exactly one ACTIVE task → parks it
    (back-compat with the common #406 case).
  - Omitted + caller owns >1 ACTIVE task → **REFUSES**, lists the
    candidate numbers, mutates nothing — never a silent guess.
  - New `TaskBoard.parkable_tasks_for_worker()` accessor (keeps the
    `TaskStatus` enum in the board layer; mirrors the existing
    `current_task_for_worker` / `active_tasks_for_worker` family).
  - Regression: explicit-id-among-several, omitted+multiple refusal,
    omitted+single back-compat, not-owned / not-active / invalid-arg
    rejections, and a faithful public-website-incident-shape test.

## [2026.5.17.7] - 2026-05-17

### Changes

- **Command Center: retired the "Now" (live activity) panel.** The
  per-worker live-activity feed and its row-resize handle added no
  signal over the worker tiles + Attention queue, so it's removed
  entirely — JS cluster (`loadLive`/`renderLive*`/poll loop/row-resize
  geometry/`CC_LIVE_*` storage keys) and the `cc-live-panel` markup +
  `.cc-live*`/`.cc-row-resize` CSS. `ccFocusLive` is kept (still used by
  the Attention card). The CC grid collapses to `auto 1fr` and the
  column-resize handle now sits on the single content row.
- **Command Center: Queen on the left, Attention on the right.** The
  Queen's live terminal is now the primary left pane and the Attention
  queue moved to the right of it (was reversed). The column-resize
  handle and stored `--cc-attention-pct` split track the new order.

### Fixes

- **Task/bottom panel now remembers its split position.** The persisted
  `swarm-split` ratio was restored once at page load but wiped on every
  return to the Command Center: `show()` cleared
  `gridTemplateRows` with no re-apply (every other panel survives via
  `applyCcLayoutFromStorage`; the bottom split had no equivalent).
  Extracted `applySavedSplit()` and `show()` now calls it — clears the
  stale per-visit inline state first (preserving the original intent),
  then re-applies the operator's persisted ratio.
- **Pasting an image into the Queen no longer lands in the last active
  worker.** The embedded Queen terminal is deliberately not
  `activeTermWorker`, but `uploadAndPaste()` hard-coded the
  `inlineTerm`/`inlineTermWs` globals (= last-focused worker), so Queen
  pastes/drops were routed to whatever worker was active. Refactored to
  `uploadAndPaste(file, targetTerm, targetWs)`; the per-terminal
  paste/drop handlers in `createTermEntry` now pass their own
  `term` + `entry.ws`, so an image pasted into the Queen reaches the
  Queen's PTY. The global drop-outside fallback still defaults to the
  active terminal.

## [2026.5.17.6] - 2026-05-17

### Features

- **`swarm_park_task` — workers can hand back their own task** (#406;
  followup flagged during #405). A worker MCP tool that transitions the
  caller's single ACTIVE task back to ASSIGNED with a required reason —
  an intentional set-down, **not** a blocker (no `swarm_report_blocker`
  binding created) and not completion. Closes the gap that bit during
  the #405 Playbooks→urgent preempt: a parked worker couldn't proactively
  un-stick its own task, so the board lied (`active` on an idle worker)
  and misled the Queen into a false STOP.
  - `TaskBoard.park(task_id, worker, reason)` — pure transition;
    rejects unless the task exists, is ACTIVE, and is owned by the
    caller (no cross-worker parking by construction).
  - `_handle_park_task` parks the caller's own active task (found via
    `current_task_for_worker`), reason required, records to task history
    + buzz (`SystemAction.TASK_PARKED`).
  - Composes with #405 INV-1/2/3 **immediately** — the worker has zero
    ACTIVE tasks right after, no daemon reload / reconciler needed; the
    board is truthful at once.
  - Distinct from `swarm_report_blocker` (waiting on upstream) and
    `swarm_complete_task` (done). Tool description satisfies the
    `test_every_tool_description_explains_when` meta-guard.
  10 new tests incl. the preempt scenario + not-a-blocker assertion;
  full suite green; ruff clean.

## [2026.5.17.5] - 2026-05-17

### Features

- **Playbook synthesis loop — Phase 4: operator surface** (spec:
  `docs/specs/playbook-synthesis-loop.md`, now **status: shipped**;
  swarm task #404). Final phase — the loop (synthesize → recall →
  outcome → propagate → consolidate → **operate**) is complete.
  - `src/swarm/server/routes/playbooks.py`: `GET /api/playbooks`
    (all statuses incl. candidates; optional `?status=`/`?scope=`),
    `POST /api/playbooks/{name}/promote`, `POST /.../retire` (body
    `reason`). Same global auth/CSRF middleware as every `/api` route;
    registered via `routes.register_all`.
  - Dashboard **Playbooks** bottom-tab: active-first list with a
    status badge (active / **candidate** / retired visually distinct),
    winrate / uses / provenance / scope / trigger, and operator
    Promote (candidates) / Retire controls wired to the routes.
  - Spec frontmatter flipped `proposed → shipped` (+ `shipped_date`,
    per-phase release map, Phase 4 closeout).
  - **Deferred by decision** (acceptance-criterion option B):
    operator-editability of `PlaybookConfig` via the dashboard /
    `config_store` DB round-trip is *not* implemented — the audited
    config-save chain is sensitive and `PlaybookConfig` already has
    sane `HiveConfig`/`swarm.yaml` defaults. Documented in the spec's
    Phase 4 closeout.
  Route tests in `tests/test_playbook_routes.py`; full suite 4285
  passed; ruff + JS syntax clean. Headless-only / no metered API; v5
  `skills` table / `SkillsStore` untouched.

## [2026.5.17.4] - 2026-05-17

### Features

- **Playbook synthesis loop — Phase 3: propagation + consolidation**
  (spec: `docs/specs/playbook-synthesis-loop.md`; swarm task #403).
  Release record for Phase 3, whose code shipped functionally in
  `d7b8fef` (a deliberate WIP park during the urgent #405 preempt) +
  ruff-normalized in `c107730`; this is the missing CHANGELOG/release
  marker — no new code.
  - **`.claude/skills/` installer** (`playbooks/installer.py`):
    `install_worker_playbooks` renders ACTIVE, in-scope playbooks to
    `pb-<name>/SKILL.md` so a Claude worker discovers them by
    description match. Idempotent with stale-cleanup; wired into
    `daemon._install_worker_artifacts` and **provider-gated** — native
    install for Claude workers only; other providers reach playbooks via
    the provider-neutral `swarm_get_playbooks` MCP tool.
  - **Consolidation sweep** (`playbooks/consolidator.py` +
    `daemon._playbook_consolidation_loop`): a low-frequency
    (`PlaybookConfig.consolidation_interval_seconds`, floored 300s,
    clean-shutdown) sweep that uses `PlaybookStore.find_near_duplicate`
    + the headless Queen (decision shape #8) to merge **same-scope**
    near-duplicate ACTIVE playbooks — `consolidate_into` bumps version,
    unions provenance, recomputes content-hash + FTS, retires the loser.
    Never cross-scope. `SystemAction.PLAYBOOK_CONSOLIDATED`.
  - Fixed a Phase-1 latent bug found here: `find_near_duplicate` used
    `search(limit=1)` so a body-vs-self query + self-exclude always
    returned `None`; now `limit=5`.
  Headless `claude -p` only (no metered API); v5 `skills` table /
  `SkillsStore` untouched. Phase 4 (dashboard + config editability)
  remains queued (#404).

### Changes

### Fixes

## [2026.5.17.3] - 2026-05-17

### Fixes

- **Task-lifecycle invariant bug (#405, operator-trust)** — the board
  was showing multiple in-progress tasks per worker and ACTIVE tasks on
  RESTING workers ("that shouldn't be possible"). Roots: activation-time
  demotion existed but reconciliation was startup-only and INV-1-only;
  nothing demoted an ACTIVE task when its worker went idle; operator-only
  tasks (e.g. GitHub org-admin) could occupy a worker-ACTIVE slot. Fix
  enforces three invariants with a one-shot + ongoing self-healing
  reconciler:
  - **INV-1** ≤1 ACTIVE/worker — `TaskBoard.activate()` demotes a
    worker's other ACTIVE tasks; reconciler collapses any drift.
  - **INV-2** ACTIVE ⇒ worker working or task blocked —
    `daemon._on_state_changed` demotes a worker's ACTIVE task when it
    leaves BUZZING/WAITING (→ ASSIGNED, or → the new **`BLOCKED`**
    status when a `swarm_report_blocker` binding exists).
  - **INV-3** a worker's current task IS its single ACTIVE task —
    `TaskBoard.current_task_for_worker()` (no separate desyncing pointer).
  - **Operator-action tasks**: new `TaskType.OPERATOR` (never ACTIVE;
    `is_operator_action`; non-executable workflow template).
  - **Reconciliation** (`TaskBoard.reconcile_invariants`) runs at daemon
    start and on every worker state transition, repairs INV-1/2/3 +
    operator-action drift deterministically + idempotently, and
    buzz-logs each auto-repair (`SystemAction.TASK_RECONCILED`) so the
    operator can audit what self-healed.
  - **Blocked status added inline** (spec implementer's-call): a
    distinct `TaskStatus.BLOCKED` (+ `block_reason`, schema v11
    migration, persisted) — INV-2 is incoherent without a real target
    state and the blocker binding already exists.
  Enum-ripple completed (STATUS_ICON/STATUS_LABEL, WORKFLOW_TEMPLATES,
  jira `_SWARM_TYPE_TO_JIRA`). New regression suites
  (`test_task_lifecycle_invariants`, `test_task_lifecycle_daemon`);
  full suite 4280 passed; ruff clean. The documented corrupt records
  (public-website/swarm/my-rcg/project-root) self-heal on the next
  daemon reload via the startup reconciler.

  *(Incidental: ruff-format normalization of Playbooks Phase-3 files
  committed earlier in d7b8fef — formatting only, no logic change.)*

## [2026.5.17.2] - 2026-05-17

### Features

- **Playbook synthesis loop — Phase 2: the outcome loop** (spec:
  `docs/specs/playbook-synthesis-loop.md`; swarm task #402; builds on
  Phase 1 / 2026.5.17). Playbooks now learn from real results:
  - **Recall-at-dispatch:** `daemon.start_task()` injects the top
    (`_PLAYBOOK_RECALL_LIMIT`) FTS-relevant **active**, in-scope
    playbooks into the worker's task message and records a
    `playbook_events 'applied'` row per injection (+ bumps `uses`,
    `PLAYBOOK_APPLIED` buzz). Candidates are never injected; gated by
    `PlaybookConfig.enabled`.
  - **Win/loss attribution:** a new decoupled `on_verdict` hook on
    `VerifierDrone` (invoked from `fire_and_forget` with the terminal
    status) wires to `daemon._attribute_playbook_outcome` — `VERIFIED`
    → win, `REOPENED`/`ESCALATED` → loss for every playbook applied to
    that task; `SKIPPED`/`NOT_RUN` → no signal. Off the
    verification-resolution path, not `complete_task` directly.
  - **Auto-promote / prune:** `PlaybookStore.evaluate_lifecycle` flips a
    candidate → active at `auto_promote_uses`/`auto_promote_winrate`,
    and retires at `prune_min_uses`/`prune_max_winrate` (never on a 0.0
    winrate that just means no decided outcomes yet).
    `PLAYBOOK_PROMOTED`/`PLAYBOOK_RETIRED` buzz.
  - New `PlaybookStore` methods (`mark_applied`,
    `playbooks_applied_to_task`, `record_outcome`, `promote`, `retire`,
    `evaluate_lifecycle`) — config-free (thresholds passed in). All
    best-effort: never block dispatch or the verification path.
  Subscription-safe (no metered API); the v5 `skills` table /
  `SkillsStore` remains untouched. Phase 3 (`.claude/skills/`
  propagation, consolidation) and Phase 4 (dashboard) remain out of
  scope (tasks #403/#404).

### Changes

### Fixes

## [2026.5.17] - 2026-05-17

### Features

- **Playbook synthesis loop — Phase 1** (spec:
  `docs/specs/playbook-synthesis-loop.md`). Self-improving procedural
  memory: when a task ships successfully, `daemon.complete_task()` fires
  a fire-and-forget, non-blocking `PlaybookSynthesizer` that asks the
  **headless** Queen (decision shape #7 — no metered API) whether the
  task encoded a generalizable procedure and, if so, persists a
  `candidate` playbook. New v10 schema (`playbooks` + `playbook_events`,
  optional fts5 with LIKE fallback) and `PlaybookStore` with exact-
  duplicate folding by `content_hash`. Synthesis is volume-gated
  (`PlaybookConfig`: eligible task types, min resolution length,
  per-(worker,task) memoization, `max_synth_per_hour`) and logged to the
  buzz log (`PLAYBOOK_SYNTHESIZED` / `PLAYBOOK_SKIPPED`, category DRONE).
  New `swarm_get_playbooks` MCP worker tool recalls scoped active
  playbooks via fts5. Distinct from the `skills` registry / `SkillsStore`
  (untouched) and Claude Code `.claude/skills/` artifacts. Later phases
  (recall-at-dispatch, win/loss attribution, auto-promote/prune,
  `.claude/skills/` propagation, dashboard) are deliberately out of
  scope. Borrowed from Hermes Agent's learning loop, re-scoped to
  Swarm's true-multi-agent + subscription model.

### Changes

### Fixes

## [2026.5.16.4] - 2026-05-16

### Changes

- **The Command Center now embeds the interactive Queen's real live PTY
  session, replacing the chat-relay UI.** The "Ask Queen" chat box was an
  indirect bridge (operator → HTTP → inject into her PTY → she calls
  `queen_reply` → WS → panel must swap a placeholder) with ~5 independent
  failure points; it kept leaving the panel stuck on "thinking" even though
  the Queen had answered (the reply was persisted in `queen_messages` but
  never rendered). It now mounts her actual `/ws/terminal?worker=queen`
  session in the right CC panel using the same proven, worker-agnostic
  terminal infrastructure every worker uses — one cached xterm, one
  connection, moved between the embed holder and `#detail-body` via
  `appendChild`. A "⛶ Full screen" button opens her exactly like a worker
  (the queen-card stays the Command Center nav — it is the only path back
  to the CC, so it was deliberately *not* repurposed). The fragile
  chat-relay JS, the `queen.message`/`queen.thread`/`queen.activity` WS
  handlers, the daemon `queen.activity` ticker loop, and
  `extract_queen_activity_line` are deleted. The backend thread machinery
  (`/api/queen/threads`, `_forward_to_queen`, `queen_reply`,
  `queen.message`/`queen.thread` broadcasts) is unchanged — it still
  serves the Attention queue, worker→queen messaging, and oversight
  threads. The Queen health dot (`queen.health`) is retained.

## [2026.5.16.3] - 2026-05-16

### Changes

- **Ask Queen now talks to the interactive Queen, not the headless
  subprocess.** The Command Center "Ask Queen" panel posted operator
  questions to `/api/queen/ask`, which fired the stateless, toolless
  headless `claude -p` Queen — she has no `queen_view_task_board` /
  `queen_view_buzz_log` / `queen_view_message_stream` /
  `queen_view_worker_state`, so coordination questions ("why did
  rcg-networks get a task?") timed out at 120s or got speculation. The
  panel now posts to the interactive-Queen thread path
  (`/api/queen/threads`), which forwards into her PTY; she answers with
  real tools and her reply renders live via the `queen.message` /
  `queen.thread` / `queen.health` WebSocket events (previously broadcast
  but never consumed by the dashboard). Matches the documented
  division of labor in `docs/specs/headless-queen-architecture.md`.
- The Ask Queen panel shows a live activity ticker while she works
  (what she's doing — tool calls, board reads — instead of a frozen
  spinner), driven by a new daemon `queen.activity` broadcast
  (2s cadence, BUZZING-gated, debounced) + `extract_queen_activity_line`
  (ANSI/terminal-chrome stripping).
- `_forward_to_queen` now reports delivery; create/post-message
  responses include `queen_delivered` so the panel surfaces "Queen
  offline — saved, she'll answer when back" instead of hanging.
- Removed the headless `/api/queen/ask` endpoint and the now-unused
  `operator-question` thread kind; the panel standardizes on the
  `operator` thread kind.

## [2026.5.16.2] - 2026-05-16

### Fixes

- Expired/revoked Jira OAuth tokens now surface a clear, actionable
  message instead of an opaque 500. `_ensure_session` raised a bare
  `RuntimeError` when the refresh token was invalid or no token
  manager was configured; uncaught, `handle_errors` turned it into
  "Internal server error" + error_id across `/api/jira/preview`,
  `/sync`, and `/import-by-key`. New `JiraAuthError(RuntimeError)` is
  raised instead and mapped by `handle_errors` to a 400 with
  "Jira authorization expired or revoked — reconnect Jira on the
  Config page", which the dashboard shows as a toast. Subclassing
  `RuntimeError` keeps existing catchers working.

## [2026.5.16] - 2026-05-16

### Fixes

- BACKLOG tasks can now be moved to Unassigned and assigned from the
  dashboard. Two stacked guards made a normal operator action fail
  nonsensically: (1) `_apply_status_change` had no `backlog → *` case,
  so changing a BACKLOG task's status to Unassigned via the edit modal
  silently no-op'd; (2) `handle_action_assign_task` tried to reach
  UNASSIGNED via `board.unassign()`, but that method only accepts
  ASSIGNED/ACTIVE and silently no-ops on BACKLOG, so `d.assign_task`'s
  `is_available` gate still 409'd. Both paths now use the same
  `task.approve()` BACKLOG → UNASSIGNED transition the "Hand to Queen"
  promote button uses, so the edit-modal status dropdown and the
  Assign action both work for backlogged tasks. Completes the
  2026.5.15.4 reassignment fix, which only covered ASSIGNED/ACTIVE.

## [2026.5.15.4] - 2026-05-15

### Fixes

- Task reassignment from the dashboard edit modal no longer silently
  lost. `/action/task/assign` 409s for any task not in `UNASSIGNED`
  (the `is_available` gate is meant to stop the auto-assign *drone*
  poaching in-flight work, not to block an explicit operator assign).
  The frontend chained the edit POST off the assign without checking
  its result, so the edit succeeded and the modal reported "Task
  updated" while the reassignment was dropped. Server now mirrors the
  proven Queen reassign path (unassign-then-assign) so operator
  (re)assignment of ASSIGNED/BACKLOG/ACTIVE tasks works; the frontend
  now surfaces a failed assign instead of a false success.

## [2026.5.15.3] - 2026-05-15

### Fixes

- Holder-bounce button now actually works. `bounceHolder()` used a bare
  `fetch()`, so the request carried no `X-Requested-With` header and the
  `_csrf_middleware` rejected it with `403 "Missing X-Requested-With
  header"` — every click since the button shipped (2026.5.14.2). The
  pre-2026.5.15.2 swallow-all error handling hid the 403 entirely
  (silent no-op); the 2026.5.15.2 honesty fix surfaced it as the visible
  "Not authorized (status 403)" toast that exposed the real cause. Now
  uses `actionFetch()` like every other state-changing dashboard POST
  (Reload, task actions, …), which sets the CSRF header.

## [2026.5.15.2] - 2026-05-15

### Fixes

- Holder bounce / server Reload no longer wedge the daemon. Both
  `handle_holder_bounce` and `handle_server_restart` did a bare
  unbounded `await reinstall_from_local_source()`; that runs up to
  three `uv` subprocess steps at 120 s each (~6 min worst case). For
  the bounce the holder is already SIGTERM'd before that await, so a
  stalled reinstall meant the daemon never restarted — a silent
  multi-minute no-op (reported on 2026.5.15). Extracted a shared
  `_best_effort_reinstall()` helper that wraps the reinstall in a 30 s
  `asyncio.wait_for` and swallows timeout/failure; the restart now
  always proceeds. Applied to both restart paths so the class can't
  reappear.
- Holder-bounce button now reports outcomes honestly. The frontend
  did `r.json().catch(()=>({}))`, which swallowed every non-JSON error
  (404/401/HTML) into silence behind an optimistic "Bouncing…" toast.
  It now branches on `r.ok`/status with distinct messages and states
  the connection-drop case (expected mid-restart) instead of implying
  success.

## [2026.5.15] - 2026-05-15

### Fixes

- Worker terminal no longer wraps Claude's output at ~6 columns after
  switching from the Queen Dashboard back to a worker. `showTermEntry`
  reconnects the WS before the flex layout settles, so
  `fitAddon.proposeDimensions()` measured a ~54px container and returned
  ~6 cols, which got sent in the `/ws/terminal` query string and
  SIGWINCH'd to the holder. Added a `MIN_TERM_COLS=20` / `MIN_TERM_ROWS=4`
  sanity floor enforced at all four resize paths (WS-open URL,
  `sendResizeIfChanged`, `forceFitAndResize`, ResizeObserver); sub-floor
  measurements are treated as not-ready and the resync retry ladder
  (rAF/80/220/600 ms) applies the correct size once layout settles.
  Self-healing for already-mis-wrapped sessions.

## [2026.5.14.2] - 2026-05-14

### Features

- "Bounce holder" button on the PTY holder drift banner. New endpoint
  `POST /api/holder/bounce` SIGTERMs the holder PID, removes the
  socket + PID files, reinstalls from local source, and triggers the
  same daemon-restart path as the Reload button. One-click upgrade
  flow for `holder.py` changes — no terminal paste required. Confirm
  modal warns that all workers will be killed (the daemon respawns
  them) and that a browser/PWA hard-refresh may be needed.

## [2026.5.14] - 2026-05-14

### Features

- Floating "Jump to bottom" pill on each worker terminal. Appears when the
  operator scrolls away from the bottom; one click re-arms auto-follow.
  Mobile-friendly (44 × 44 px tap target).

### Fixes

- Worker terminal viewport no longer snaps back to the bottom when the
  operator scrolls up during heavy worker output. Replaced the
  `_isAutoScrolling` / `_writesPending` guards in the scroll handler with
  a wheel-capture listener on the xterm root, a DOM scroll listener on
  `.xterm-viewport`, and an unguarded xterm `onScroll` — three
  independent signals converging on a single `stickyBottom` truth.
- Set `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1` on Claude worker PTY
  spawns so output flows into xterm.js's main buffer (5000-line
  scrollback) instead of the alternate buffer (no scrollback). Upstream
  context: anthropics/claude-code#42670.
- "Copy" button on the holder-drift banner now actually copies — added
  the missing entry to the `data-action` dispatch table.

## [2026.5.11] - 2026-05-11

### Features

### Changes

- **The `TaskStatus` enum is renamed to the operator-facing vocabulary** — BACKLOG / UNASSIGNED / ASSIGNED / ACTIVE / DONE / FAILED, replacing PROPOSED / PENDING / IN_PROGRESS / COMPLETED — completing a v9 cleanup whose call sites had already been pushed ahead of the definition. A v8→v9 SQLite migration (`_migrate_v9_status_rename`) rewrites legacy status values at daemon startup, idempotently, so existing databases upgrade in place with no wipe, and a `_LEGACY_STATUS_MAP` shim covers legacy `tasks.json` imports. Call sites across `drones/`, `queen/`, `server/`, `web/` and `tasks/` were updated in the same pass along with templates and the CLAUDE.md narrative, with new coverage for the value rewrite, its idempotency, the status label map and the task-list partial.

### Fixes

- **A fresh install from git crashed the daemon at startup with `AttributeError: type object 'TaskStatus' has no attribute 'ACTIVE'`.** Prior releases had pushed call sites referencing the new status names while `tasks/task.py` still defined the old ones, so anyone installing from `git+https://github.com/bschleifer/swarm.git` hit the error whenever the tasks table contained an assigned row.

## [2026.5.8.6] - 2026-05-08

### Features

### Changes

### Fixes

- **Inline mini swap bar now colors from `pressure_level`, not raw
  `swap_percent`** (task #353). Followup to #352: the popover was
  reworked but the small inline bar in the dashboard top row still
  derived its color from `swap_percent`, so on a healthy long-uptime
  workstation with sticky cold pages the bar would go orange/red
  while the pressure badge sat at NOM. Bar *width* still reflects
  `swap_percent` (a fair "how full is the disk-backed pool"
  indicator); only the *color* switches to the pressure-driven
  palette so it tracks the badge instead of contradicting it. Single-
  file change in `src/swarm/web/static/dashboard.js:734–738`. No
  backend changes needed — `pressure_level` was already on the
  snapshot from #352.

## [2026.5.8.5] - 2026-05-08

### Features

- **Resource widget surfaces PSI + swap I/O instead of standing percentages**
  (task #352). The dashboard "Bee Hive" popover and the underlying
  `ResourceSnapshot` now expose three pressure signals that actually correlate
  with worker performance:
    * `psi_cpu_avg10`, `psi_mem_avg10`, `psi_io_avg10` — kernel PSI from
      `/proc/pressure/{cpu,memory,io}` (the `some avg10=` value, % of last 10 s
      processes stalled). `psi_available` flag tells the UI when CONFIG_PSI=n
      kernels should hide the row instead of showing zeros.
    * `swap_in_per_sec`, `swap_out_per_sec` — pages/sec derived from
      `pswpin`/`pswpout` deltas in `/proc/vmstat`. `ResourceMonitor` keeps the
      previous `(in, out, ts)` reading on the instance for stateful diffing
      (counter rollback or zero-dt → 0.0 instead of negative/divide errors).
    * `top_workers_by_rss` — top-N worker process trees by total RSS, populated
      only when pressure ≠ NOMINAL so the per-tick cost stays trivial under
      healthy load.
  `classify_pressure` now accepts `psi_mem_avg10` as a floor: ≥ 10 forces at
  least ELEVATED, ≥ 30 forces HIGH. The override never demotes — percentage-
  based escalations stay where they are. The dashboard popover reorders to PSI →
  Memory → Load (% utilized vs cpu_count) → Swap I/O (✓ when zero) → Top by RSS
  → pressure box → suspended / d-state → collapsible details (standing swap
  pool, demoted from headline). Backwards compatible: `to_dict()` keeps every
  legacy key (`mem_percent`, `swap_percent`, `pressure_level`, …); existing API
  consumers see a strict superset.

### Changes

### Fixes

## [2026.5.8.4] - 2026-05-08

### Features

- **MCP `structuredContent` sidecars on view tools.** Phase 3 of the
  Apr–May 2026 Anthropic-features bundle. The original spike plan
  targeted speculative SEP-1865 UI widgets, but a read of Claude Code
  2.1.x's source (verified at `services/mcp/client.ts:2662`,
  `transformMCPResult`) showed that `structuredContent` was already a
  shipped, supported feature — when a tool result includes it, Claude
  Code prefers it over the markdown content array, JSON-stringifies
  it, and pairs it with an inferred compact schema for the model.
  The six Queen view tools (`queen_view_worker_state`,
  `queen_view_task_board`, `queen_view_messages`,
  `queen_view_message_stream`, `queen_view_buzz_log`,
  `queen_view_drone_actions`) plus the worker-side `swarm_task_status`
  now return both the existing markdown text content AND a typed JSON
  sidecar with the same data. The Queen sees both — text for thread
  rendering, JSON for queryable reasoning. Handlers may opt into the
  new shape by returning `{"content": [...], "structuredContent":
  {...}}` instead of the bare list; `handle_tool_call` and
  `_handle_tools_call` thread either shape through. Empty-result
  paths still return the legacy list so older clients never see
  half-built sidecars. Fully backwards compatible — clients that
  ignore `structuredContent` see exactly the prior payload shape.

### Changes

### Fixes

## [2026.5.8.3] - 2026-05-08

### Features

- **`acceptance_criteria` is now wired into the verifier.** The field has
  lived on the `tasks` table and `SwarmTask` since v1 but was unread by the
  verifier — workers could declare success criteria and the verifier
  ignored them. Phase 2 of the Apr–May 2026 Anthropic-features bundle
  closes that loop: the Tier-2 verifier prompt now requests an optional
  per-criterion `criteria: [{"text", "passed"}]` array in its JSON output;
  the parser carries it through as `VerifierVerdict.criteria_results`;
  the drone formats failed criteria verbatim into `verification_reason`
  (e.g. `"diff missed criterion (failed criteria: 'returns 200',
  'logs event')"`). `swarm_create_task` accepts a new optional
  `acceptance_criteria: list[str]` argument that flows through `edit_task`
  to the task row at creation. Empty / whitespace-only entries are
  filtered. Backwards compatible: tasks without criteria see no behaviour
  change.

### Changes

### Fixes

## [2026.5.8.2] - 2026-05-08

### Features

- New **Dreamer drone** (`src/swarm/drones/dreamer.py`) periodically scans the
  buzz log for recurring failure / oversight signatures (verifier reopens, task
  failures, oversight interventions, worker-reported blockers) and auto-curates
  matching `queen_learnings` rows tagged `discovered_by_dreamer:{action}:{key}`.
  Workers and the Queen surface them through the existing
  `swarm_get_learnings` / `queen_query_learnings` tools — no new client surface
  needed. v1 is fully deterministic (regex-based signature normalization, no
  LLM call); promotion requires both `dreamer_min_pattern_count` and ≥2
  distinct workers so a single chatty worker can't manufacture patterns.
  Dedupe rewrites the same pattern only after a 7-day refresh window. New
  config knobs on `DroneConfig`: `dreamer_interval_seconds` (default 4h, 0
  disables), `dreamer_lookback_hours` (24h), `dreamer_min_pattern_count` (3).
  Sweeps emit a `PATTERN_DISCOVERED` buzz entry under `LogCategory.DRONE`.
  Inspired by Anthropic's "Dreaming" announcement (2026-05-06).

### Changes

### Fixes

## [2026.5.8] - 2026-05-08

### Features

### Changes

- Queen proposals are now suppressed for whichever worker the operator is
  currently viewing in the dashboard. Focus is signalled by the existing
  `focus` WS command (`pilot._focused_workers`); when the operator is
  hands-on with a worker, escalation/completion/assignment proposals get
  dropped at the `ProposalManager.on_proposal()` chokepoint with a
  `QUEEN_PROPOSAL_SKIPPED_FOCUSED` log entry under `LogCategory.QUEEN`.

### Fixes

- Only one task per worker can show as IN PROGRESS at a time. Previously,
  rapid `swarm_create_task(target_worker=X)` dispatches would each call
  `start_task` and flip every task to ACTIVE without demoting the prior
  one — the dashboard then showed multiple "IN PROGRESS" badges for a
  single worker. `start_task` now demotes any other ACTIVE task for the
  worker back to ASSIGNED before promoting the new one, and a startup
  reconcile (`TaskBoard.reconcile_active_per_worker`) cleans up state
  left behind by older daemon versions on first boot after upgrade.

## [2026.5.7.2] - 2026-05-07

### Features

### Changes

### Fixes

- **The Queen's redirects are now gated on operator engagement and must cite a contradiction (#340)**, so she stops interrupting a worker on the strength of an inference she cannot point at.

- **Auto-assignment gained a deterministic project-affinity gate (#341).** Shipped together with the redirect gate as one release because both target the same incident loop — mis-route, operator redirect, drift flag, operator interruption.

## [2026.5.7] - 2026-05-07

### Fixes
- **Workers stuck RESTING when background shells are running.** Claude Code 2.x's auto mode lets workers background async ``Bash`` commands ("shells") in addition to long-running monitors (dev servers, watchers). The two surface forms are identical except for the noun (``"N shells still running"`` / ``"auto mode on · N shells"`` vs the same with ``monitors``), but the state classifier's ``_RE_MONITOR_RUNNING`` regex only matched the monitor variant — so workers with active background shells were classified RESTING (and eventually SLEEPING) while real work continued, causing the pilot/idle-watcher to consider them free and the dashboard sidebar to mislead operators. Renamed the constant to ``_RE_BACKGROUND_RUNNING`` and broadened the pattern to ``(?:monitors?|shells?)``; updated all three call sites (``classify_output``, ``classify_styled_output``, ``state_tracker._has_active_turn_signal``) and added five regression tests including one that reproduces the original ``budgetbug`` screenshot exactly. Workers will now flip to BUZZING when shells are running, suppressing both auto-assignment and idle-watcher nudges until the background work clears.

## [2026.5.5.24] - 2026-05-05

### Docs
- **CLAUDE.md: ``Verifying out-of-band task assignments`` runbook subsection.** New section in ``CLAUDE.md`` (between Queen message-surface elevation and Live MCP tool-surface propagation) documenting the defensive ``sqlite3 ~/.swarm/swarm.db`` query workers should run before dismissing a claimed task assignment as prompt injection. The swarm system legitimately auto-relays queued or just-assigned tasks into a worker's PTY between turns — the in-session transcript is not authoritative for assignment state, the DB is. Pattern added after a 2026-05-05 incident where this worker dismissed a legitimate ``#331`` assignment (the rules.py ``ALWAYS_ESCALATE`` change shipped in 2026.5.5.23) as injection because the task wasn't visible in the transcript and the requested change was security-sensitive. The DB query would have resolved the ambiguity in under a second.

## [2026.5.5.23] - 2026-05-05

### Features

### Changes
- **drones: ``git push <remote> (main|master)`` is now user-configurable, not hardcoded.** Removed the regex line ``r"|git\s+push\s+\S+\s+(main|master)\b"`` from ``ALWAYS_ESCALATE`` in ``src/swarm/drones/rules.py:63``. The hardcoded escalation was designed for repos with PR-only workflows but it forced the same friction onto repos where direct-to-main is the legitimate workflow (personal IaC, single-maintainer side projects). It also blocked the ``rcg-network`` worker's ``/ship`` flow on Brad's HVAC firewall fix — every prior commit on that repo was direct-to-main, but the rule rejected the push and required a synthetic PR open + ``gh pr merge`` round-trip (also rejected). Repos that want PR-only enforcement add the rule themselves under ``drones.approval_rules`` (one-line YAML: ``- pattern: 'git\s+push\s+\S+\s+(main|master)\b'`` + ``action: escalate``). All other destructive-op coverage in ``ALWAYS_ESCALATE`` (force-push, ``--no-verify``, ``DROP TABLE``, ``rm -rf``, ``reset --hard``, ``DELETE FROM`` without ``WHERE``) is unchanged. Tests in ``tests/test_rules.py`` updated: the ``TestPushToMainEscalation`` class is replaced with ``TestPushToDefaultBranchUserConfigurable`` covering the new fall-through behavior and the user-rule opt-in path; the ``ALWAYS_ESCALATE`` parametrized list moves ``git push origin main`` from ``test_always_escalates`` to ``test_not_always_escalated`` along with ``git push upstream master``. Closes task #331.

### Fixes

## [2026.5.5.22] - 2026-05-05

### Docs
- **README + roadmap docs:** documentation audit covering the 33 release commits between 2026.4.30 and 2026.5.5.21. Three Critical drifts fixed in ``README.md``: architecture-diagram MCP-tool count corrected from "9 coordination tools" to "12 worker · 15 Queen tools" (matches actual count in ``src/swarm/mcp/tools.py`` + ``queen_tools.py``); the Config-page tab list is rewritten at all three callsites (Web Dashboard bullet, "What you get" section, and Configuration heading) to reflect the live tabs (General · LLMs · Workers · Automation · Notifications · Integrations · Security · Usage · Advanced · Logs); the Configuration loading priority is reframed so ``swarm.db`` is the canonical source per 2026.5.5.20 with YAML demoted to a bootstrap-only seed and ``-c <yaml>`` flagged as ignored on populated DBs. Coverage gaps closed: ``swarm holder-restart`` (added 2026.5.4.2) and ``swarm queen contribute-claude-md`` (shipped 2026.4.22.11, never documented) appear in the CLI Reference table; drag-and-drop Jira/Outlook import + ADF→Markdown + HTML→Markdown documented in the Email and Jira sections; WYSIWYG task editor + compact one-or-two-line task rows surfaced in the task-board bullets; ``swarm_task_status({number: N})`` full-detail mode added to the MCP tools table; ``-c`` flag clarified in the Global Flags table. Stale ``docs/features-roadmap.md`` and ``docs/claude-code-roadmap.md`` get a 2026-05-05 update block pointing at CHANGELOG for the post-2026-04-16 surface.

## [2026.5.5.21] - 2026-05-05

### Features

### Changes

### Fixes
- **service: stop installing legacy ``-c <yaml>`` flag in systemd unit, auto-strip on next start.** Companion fix to 2026.5.5.20.  ``service.generate_unit`` no longer writes ``-c ~/.config/swarm/config.yaml`` (or any ``--config``) into ``ExecStart=`` for new installs — the DB is canonical, the YAML override is forbidden when the DB has data, and the flag silently caused Amanda's "saves disappear on restart" symptom on existing installs.  ``ensure_killmode_process`` (auto-runs on every daemon startup via ``_maybe_patch_systemd_unit``) now also strips ``-c <yaml>`` / ``--config <yaml>`` / ``--config=<yaml>`` / ``-c<yaml>`` from the existing ``ExecStart=`` line — so operators on legacy units don't have to manually edit ``~/.config/systemd/user/swarm.service``.  Production unit's ``WorkingDirectory`` is now ``$HOME`` instead of the YAML's parent (load-bearing only when ``-c`` was passed).  Five regression tests in ``tests/test_service.py::TestEnsureKillmodeProcess``.

## [2026.5.5.20] - 2026-05-05

### Features

### Changes

### Fixes
- **cli: ``--config`` no longer overrides a populated swarm.db.** Root cause of Amanda's "I save workflows / approval rules / groups from the dashboard, restart, and they're gone" symptom: a legacy ``swarm.service`` ExecStart of ``swarm serve -c ~/.config/swarm/config.yaml`` survived from the pre-DB era. Every dashboard "Restart" reload preserved that argv through ``os.execv``, so ``_load_config_db_first`` saw ``-c <yaml>``, hit the explicit-override path, loaded a stale YAML that didn't have any of her edits — and silently overwrote the in-memory state with empty data. Save → DB write succeeded → restart → YAML loader won → dashboard rendered the YAML's empty value → operator concluded "the save didn't stick." The doc above ``_load_config_db_first`` already explicitly forbade this ("the daemon must never run against a YAML-sourced HiveConfig when the DB has data") but the implementation honoured ``--config`` unconditionally. Now ``--config`` is honoured ONLY when the DB has no user data — the test / fresh-install / explicit-YAML-bootstrap workflows still work; the legacy-systemd case correctly falls through to the DB. ``_exec_restart`` also strips ``-c`` / ``--config`` from argv before exec so the warning doesn't keep firing on every reload. Regression tests in ``tests/test_cli.py::test_load_config_db_first_yaml_ignored_when_db_has_data`` and ``test_strip_config_flag_handles_all_forms``.

## [2026.5.5.19] - 2026-05-05

### Features

### Changes
- **server: log run_daemon entry state at WARNING.** Decisive triage anchor for Amanda's empty-workflows-on-restart symptom: ``_load_config_db_first(None)`` was confirmed to return ``workflows={'verify': '/verify-skill'}`` from her installed Python, the DB was confirmed to retain the row across restart, but the daemon's ``__init__`` saw ``config.workflows={}``. Added a WARNING log at the top of ``run_daemon`` that prints ``config.workflows``, ``config_source``, and ``sys.argv`` — pinpoints whether the wipe is in cli.py between ``_load_config_db_first`` and ``run_daemon``, or inside daemon construction.

### Fixes

## [2026.5.5.18] - 2026-05-05

### Features

### Changes

### Fixes
- **cli: configure logging before any subcommand invocation.** Pre-fix the bare ``swarm`` path (no subcommand → ``ctx.invoke(start_cmd)``) skipped ``setup_logging`` in ``main()``, deferring it to ``setup_logging_from_cli`` inside ``start_cmd`` — but that runs AFTER ``_load_config_db_first``. Any log emitted by the loader on this path went to a handler-less swarm logger and was silently dropped. Including the 2026.5.5.17 ``load_config_from_db: returning workflows=...`` diagnostic anchor we shipped to triage Amanda's empty-workflows-on-restart symptom. ``setup_logging`` now runs unconditionally at the top of ``main()``; subcommand paths still re-configure with config-file values once cfg is loaded (``setup_logging`` clears handlers before re-attaching, so the early call is harmless).
- **web: log-level dropdown's "Current persisted" indicator updates on save.** The span at ``Logs > Running daemon log level > Current persisted`` was server-rendered Jinja that only refreshed on full page reload. Operator changed the dropdown, ``setRunningLogLevel`` correctly persisted to the DB, but the indicator kept showing the pre-save value — looking exactly like a save failure. JS now updates the span text in the success branch.

## [2026.5.5.17] - 2026-05-05

### Features

### Changes
- **server/db: bump diagnostic workflows logs to WARNING.** The 2026.5.5.15 INFO-level ``daemon init: config.workflows=...`` log was missing from Amanda's swarm.log even though she confirmed she's on 16 and the apply_update entry log fires. Most likely a log-level / handler-timing issue between daemon ``__init__`` and the first ``setup_logging`` call. Bumped both the daemon-init log and a new companion log inside ``load_config_from_db`` (``returning workflows=...``) to WARNING so they survive any verbosity config and can't be silently filtered. Pairs with the existing ``apply_update`` entry/exit logs to cover the full save-load chain — next reproduction will pinpoint whether the loader is dropping workflows or whether something post-load mutates them.

### Fixes

## [2026.5.5.16] - 2026-05-05

### Features

### Changes

### Fixes
- **config: workflows survive unrelated saves.** ``ConfigManager._apply_workflows`` now treats an empty body (``workflows: {}``) as a no-op rather than overwriting ``self._config.workflows`` with empty. The dashboard's ``saveSettings`` always serializes the four Automation-tab inputs into a ``workflows`` dict, omitting empty fields. When the user is editing a different tab and the workflow inputs render empty (because their daemon's ``cfg.workflows`` was already cleared, or browser cache), the body carries ``workflows: {}`` — and pre-fix this wiped the in-memory dict. ``serialize_config`` then skipped the ``workflows`` key on save (since the dict was empty), so the DB row was preserved on disk but the running daemon's state was stale until the next restart. Operators reported "I typed /verify, saved, restarted, it's gone" because every unrelated config save (group edit, drone toggle, …) cleared the in-memory dict in between. Same destructive-empty-overwrite footgun the ``approval_rules`` table had pre-#328; same guard pattern. Explicit clearing from the UI is a future enhancement. Regression test in ``tests/test_config_manager.py::TestConfigManagerApplyUpdate::test_empty_workflows_body_is_noop``.

## [2026.5.5.15] - 2026-05-05

### Features

### Changes
- **server: diagnostic logging on the workflows save/load chain.** Added INFO-level anchors at ``SwarmDaemon.__init__`` (``daemon init: config.workflows=...``), ``ConfigManager.apply_update`` (entry + post-save), and ``handle_get_config`` (``GET /api/config: cfg.workflows=...  serialized.workflows=...``). Triages a class of "config field reverts on restart" symptoms: the DB row + raw ``load_config_from_db`` both verify correct, but the running daemon's serialized config returns the field as ``undefined``. The new logs let an operator pinpoint exactly when ``self._config.workflows`` gets mutated to empty between init and the next GET — narrowing the suspect from "somewhere in the daemon" to a single dispatcher invocation. Pure additive logging; no behavior change.

### Fixes

## [2026.5.5.14] - 2026-05-05

### Features

### Changes
- **web/templates: config-field macros.** Added two narrow Jinja macros at the top of ``src/swarm/web/templates/config.html`` (``config_toggle`` for boolean toggles, ``config_number`` for numeric inputs) and migrated the matching blocks. ~28 of the original 77 ``<div class="config-field">`` blocks now flow through one of the macros — the toggle pattern (14 instances, 100% identical) and the numeric pattern (~14 instances with step/min/max/placeholder variation). The original plan called for a single mega-macro covering all 77 blocks, but a survey revealed three groups: toggles (uniform), numbers (near-uniform), and text/select/custom (~45 blocks with restart-badge + class variation + custom option loops + button layouts that don't fit a one-size macro). Forcing them all through one macro would either be too rigid or too parameter-heavy. The text/select/custom variants stay inline. Phase G of the duplication-cluster sweep — final phase.

### Fixes

## [2026.5.5.13] - 2026-05-05

### Features

### Changes
- **cli/logging:** unified the three identical 8-line blocks resolving CLI flag overrides + config-file fallbacks for log_level / log_file / log_format (in ``serve``, ``daemon``, and ``test`` subcommands at ``src/swarm/cli.py``) onto a new ``setup_logging_from_cli(cli_obj, cfg)`` helper at ``src/swarm/logging.py``. Behavior unchanged; future log-resolution tweaks (e.g. an env-var override) now have one canonical place to land. Phase F of the duplication-cluster sweep.

### Fixes

## [2026.5.5.12] - 2026-05-05

### Features

### Changes
- **server: origin / CSRF check unified.** Three near-identical inline copies of the Origin-header validation (``_csrf_middleware`` in ``server.api``, ``_check_auth`` in ``pty.bridge``, ``_check_ws_access`` in ``server.routes.websocket``) now route through a single ``check_origin_or_error`` helper at ``src/swarm/server/api.py``. Reject responses are unified on text ``Origin rejected`` (was ``CSRF rejected`` / ``WebSocket origin rejected`` / ``CSRF rejected`` respectively) — a 403 either way; no client-visible behavior change since no test or call site asserts on the body text. Phase E of the duplication-cluster sweep.

### Fixes
- **server logging:** origin-mismatch failures from ``_csrf_middleware`` and the pty WS bridge now log at WARNING level with the offending origin, request host, and path. Pre-Phase-E only the dashboard ``/ws`` reject path logged — the CSRF middleware and pty bridge silently returned 403 with no server-side anchor, so a misconfigured reverse proxy looked exactly like a client bug.

## [2026.5.5.11] - 2026-05-05

### Features

### Changes
- **web/toast:** unified the dashboard's and config page's ``showToast`` / ``_toastApplyResult`` implementations onto a single shared module (``src/swarm/web/static/toast.js``). Pre-Phase-D the dashboard's was the fully-featured copy (dedup, screen-reader announce, click-to-dismiss, notification-badge integration via ``addNotification``) and the config page's was a minimal "append a div, remove after 3.5s" copy that silently dropped accessibility and dedup. The shared module adopts the dashboard's feature set; the config page now gets dedup, screen-reader announcements, and click-to-dismiss for free. ``window.addNotification`` is called conditionally so non-dashboard pages don't fail. Phase D of the duplication-cluster sweep.

### Fixes
- **a11y:** the config page's toasts now announce to screen readers via the shared ``#sr-announcer`` aria-live region (relocated to ``base.html`` so all pages benefit). Pre-Phase-D the announcer existed only in ``dashboard.html`` and config-page save/error toasts were silent for screen reader users.

## [2026.5.5.10] - 2026-05-05

### Features

### Changes
- **server/web error handling:** unified the two HTTP error decorators (``handle_swarm_errors`` in ``swarm.web.app`` and ``handle_errors`` in ``swarm.server.helpers``) onto a single canonical implementation at ``src/swarm/server/helpers.py``. Pre-Phase-C the two decorators mapped ``SwarmOperationError`` to different status codes — 400 in server routes, 409 in web routes — which silently routed input-validation failures and state-conflict errors to the same code on one side and a different code on the other.
- **api:** ``SwarmOperationError`` now uniformly returns **HTTP 409 Conflict** across both ``/api/*`` and dashboard ``/dashboard/api/*`` routes (was 400 in server routes pre-Phase-C). 409 better fits the semantics — "operation can't proceed in current state" (Queen offline, worker in wrong state, name already taken, …) — than 400, which means "your input was malformed". Input-validation paths now consistently raise ``ValueError`` and map to **400 Bad Request** through the same canonical decorator. Phase C of the duplication-cluster sweep.

### Fixes

## [2026.5.5.9] - 2026-05-05

### Features

### Changes
- **web/ws-auth:** unified the three authenticated-WebSocket call sites (dashboard main ``/ws``, dashboard terminal ``/ws/terminal``, config page ``/ws``) onto a single ``window.swarmWS.openAuthenticated(path)`` helper at ``src/swarm/web/static/ws-auth.js``. The helper builds the ``ws://``/``wss://`` URL and sends the JSON auth message the server's first-message gate expects, using the shared ``swarmAuth.getToken()`` resolver from Phase A. Adding a new authenticated-WS endpoint no longer means copying URL-build + auth-send boilerplate, and the two cannot drift apart again. Phase B of the duplication-cluster sweep.

### Fixes

## [2026.5.5.8] - 2026-05-05

### Features

### Changes
- **web/auth:** unified the dashboard and config pages onto a single shared auth-token resolver (``src/swarm/web/static/auth.js``, ``window.swarmAuth``). Pre-unification each page resolved the WS-auth / Bearer-auth token independently, and the drift between them shipped the 2026.5.5.7 WS-lockout bug. Both pages now read the token through ``window.swarmAuth.getToken()``; ``setServerToken()`` handles the stale-clear once at page load, and ``clearStaleSessionToken()`` is exposed for runtime auth-failure paths. Phase A of the duplication-cluster sweep — six more clusters (WS auth flow, HTTP error decorators, toast helpers, origin/CSRF check, log-level resolution, config-field Jinja macro) follow in subsequent releases.

### Fixes

## [2026.5.5.7] - 2026-05-05

### Fixes
- **websocket:** the dashboard's main ``/ws`` connection no longer gets locked out for 5 minutes after navigating to the config page. **Real root cause** of the WS lockout symptom Brad reported through Cloudflare tunnel: ``config.html`` opened its own ``/ws`` and read the auth token from ``sessionStorage['swarm_api_password']`` only. For session-cookie-authenticated logins (the default flow) that key is empty, so the config page's WS upgrade sent ``token: ''``. After 5 of those within 5 minutes the IP was locked out — and the per-IP lockout is shared, so the dashboard's main ``/ws`` then got 429s too. ``/ws/terminal`` kept working because it's connected before the config page poisons the lockout, OR with a token from a different code path. Diagnosed via the ``WS auth FAIL (wrong-token, first-message): ... token=<empty>`` lines from 2026.5.5.6's logging. Fix: ``handle_config_page`` now passes ``ws_token`` to the template (same source dashboard uses), and ``config.html`` prefers it over the sessionStorage fallback.

### Features

### Changes

### Fixes

## [2026.5.5.6] - 2026-05-05

### Diagnostics
- **websocket:** ``ws_authenticate`` now logs a WARNING line on every wrong-token failure naming the path (``/ws`` vs ``/ws/terminal``), the IP, the ``type`` field of the received message, and a short summary of the token (length + first 8 chars). The 2026.5.5.4 reject logging told us the lockout was firing; this tells us *who* is feeding it. Dashboard's main /ws keeps tripping wrong-token failures even though /ws/terminal succeeds with the same token — these new lines will let us see whether the tokens actually differ between paths or whether something else is sending a non-``auth`` message at /ws first.

### Features

### Changes

### Fixes

## [2026.5.5.5] - 2026-05-05

### Features

### Changes

### Fixes

- **The Logs tab's content leaked into the Advanced tab.** The `tabGroups` map still merged the logs section into Advanced after Logs got its own dedicated tab button, so clicking Advanced showed Coordination, Terminal, Server, buttons, test *and* Debug Log — with the log viewer rendering in two places. `logs` is dropped from the advanced group; the Logs tab is now the only place it renders.

## [2026.5.5.4] - 2026-05-05

### Diagnostics
- **websocket:** ``_check_ws_access`` now emits a WARNING-level log on every reject path (origin mismatch / auth lockout / per-IP cap), naming the offending IP and the reason. Pre-fix the handler returned 403 / 429 silently — operators saw "WebSocket connection ... failed:" in the browser console with zero server-side context. The auth-lockout fix in 2026.5.5.3 closed one path; this logging makes the remaining ones diagnosable on the next reproduction.

### Fixes
- **dashboard:** the Logs-tab "Running daemon log level" dropdown now shows a success toast on save (and a warning toast if any body field was ignored), matching the structured ``_apply_result`` flow every other config-save endpoint uses since Phase 7. Pre-fix the dropdown only updated an inline status span, with no toast — looked like the "old saving mechanism".

### Features

### Changes

### Fixes

## [2026.5.5.3] - 2026-05-05

### Features

### Changes

### Fixes
- **websocket:** main ``/ws`` handshake no longer locks the operator out for 5 minutes after a few transient tunnel hiccups. Pre-fix ``ws_authenticate`` returned ``False`` on auth-message timeout, malformed JSON, and wrong-token alike, and the caller in ``handle_websocket`` (and ``handle_terminal_ws``) blindly recorded every ``False`` as a real auth failure via ``record_ws_auth_failure``. After 5 of those within 5 minutes the IP was rate-limited at 429, the dashboard's reconnect loop kept hitting the same wall, and only ``/ws/terminal`` (which doesn't go through ``_check_ws_access``) kept working. Reported through Cloudflare tunnel — slow tunnel makes the 5-second auth-message receive timeout fire intermittently. Now ``ws_authenticate`` records the failure internally and only when the token was actually wrong.
- **coordination:** Swarm-managed scaffolding files (``.claude/commands/swarm-*``, ``.claude/skills/swarm-*``, ``.claude/scheduled_tasks.lock``, ``.claude/ux-audit.json``) no longer produce ``file overlap: ...`` WARNING lines on every reload. Those files are installed identically into every worker repo by the Swarm hooks installer; they were producing 50+ near-identical WARNING lines per poll cycle (one per worker × per scaffolding file) with no actionable signal, drowning out real overlap alerts. Genuine cross-worker overlaps are still tracked and logged, but each (owner, intruder) pair now coalesces to a single WARNING listing up to 5 files plus a count rather than one line per file.

## [2026.5.5.2] - 2026-05-05

### Features
- **dashboard:** Logs is now its own tab in the config nav (was an unreachable nested view). Tab gets a taller log pane (``min-height: 60vh``) plus a "running daemon log level" dropdown that updates the live Python logger via ``PUT /api/config`` — no more hopping to the General tab to bump verbosity while debugging.
- **dashboard:** the dev-mode "Reload" button moved out of the page header into the Updates section under General, where it lives alongside the version number and an explanation of what it does (reinstalls from local source + ``os.execv``s into a fresh process). In dev mode the production "Check for Updates" button is now disabled with a tooltip pointing the operator at ``git pull`` + Reload.

### Changes
- **logs:** severity filter on ``/partials/logs`` is now an inclusive hierarchy — picking ``INFO`` returns INFO + WARNING + ERROR, mirroring how Python's logging module treats threshold severities. Pre-fix it was a naive substring match that hid every WARNING / ERROR line whenever INFO was selected; the only way to see anything beyond INFO was to switch the filter to "All". Filter logic factored into ``swarm.web.log_filter`` so it's testable without dragging in the full web stack.
- **logs:** dashboard log viewer no longer auto-scrolls to the bottom on load. The server returns lines newest-first; the prior ``scrollTop = scrollHeight`` would bury the relevant entries off-screen at the bottom under a screen of older logs.

### Fixes

## [2026.5.5] - 2026-05-05

### Features
- **config:** the remaining 3 multi-field save endpoints — ``POST /api/config/workers/{name}/save``, ``POST /api/config/workers/{name}/add-to-group``, and ``POST /api/config/approval-rules`` — now return a structured ``_apply_result`` and emit WARNING-level logs for unknown body keys. They aren't dataclass-shaped (their bodies are fixed-key dicts like ``{group, create}``), so a new ``validate_body_keys`` helper provides the same drift-detection contract as ``_apply_dataclass_dict``: consumed = body keys present in the expected set, unknown = the rest. Dashboard ``_toastApplyResult`` helper now lives on ``window`` and is invoked from ``dashboard.js`` save-worker / add-to-group / add-rule callsites. Phase 8 of #328 — every multi-field config save endpoint uses the shared instrumentation for success, failure, server logging, and dashboard toasts.
- **dashboard:** drones-toggle button (``POST /action/toggle-drones``) and drag-drop worker reorder (``POST /api/workers/reorder``) now show success and failure toasts. Pre-fix both were silent — the drones button just flipped its label, and drag-drop persisted with no confirmation. ``/api/workers/reorder`` also gains a server-side WARNING log if its raw SQL ``UPDATE workers SET sort_order`` fails, mirroring the forensic contract the dispatch chain has had since 2026.5.4.6. Phase 9 of #328 — closes the single-action save-path gap audit found after Phase 8.

### Changes

### Fixes

## [2026.5.4.11] - 2026-05-04

### Features
- **config:** every dispatch-using save endpoint now returns a structured ``_apply_result`` in its response: per-section ``consumed`` (fields validated and applied) and ``unknown`` (body keys with no matching dataclass field) lists. Covers ``PUT /api/config`` (bulk autosave), ``POST /api/config/workers``, ``POST /api/config/groups``, and ``PUT /api/config/groups/{name}``. Dashboard reads it and surfaces unknown-field warnings as a toast ("Saved, but 1 field(s) ignored: foo_bar"). Pre-fix the operator saw a bare success toast whether 5 fields persisted or 0 — server-side drift logs went to ``~/.swarm/swarm.log`` only. Now per-field outcomes surface in the UI. Phase 7 of #328.
- **config:** dispatch coverage extended to ``_apply_coordination``, ``_apply_jira``, ``_apply_advanced``, and ``_apply_test``. All four now return a ``FieldOutcome`` (consumed + unknown) that ``apply_update`` aggregates into the ``ApplyResult``. ``_apply_coordination``'s ``auto_pull`` and ``_apply_advanced``'s ``terminal`` sub-dataclass now flow through generic dispatch — new fields auto-apply, unknown sub-keys emit the standard WARNING. The two group CRUD endpoints (``POST /api/config/groups``, ``PUT /api/config/groups/{name}``) now use full ``_apply_dataclass_dict`` dispatch instead of warn-only sweeps. Phase 7 of #328.

### Changes

### Fixes

## [2026.5.4.10] - 2026-05-04

### Features

### Changes
- **config:** ``POST /api/config/workers`` now accepts every writable ``WorkerConfig`` field via generic dataclass dispatch, not just the previously cherry-picked ``name``/``path``/``description``/``provider``. Closes the audit-flagged ``isolation`` (worktree mode) and ``identity`` (per-worker CLAUDE.md path) silent-drop gaps — operators creating a worker through the API can now set those fields and have them persist. ``approval_rules`` and ``allowed_tools`` are intentionally skipped: rules use a dedicated endpoint with regex compile + DB sync semantics, and ``allowed_tools`` doesn't have a DB column yet (separate audit gap, deferred). Phase 6 of #328.
- **config:** ``POST /api/config/groups`` and ``PUT /api/config/groups/{name}`` now emit a section-prefixed WARNING for any unknown body key the dashboard might send, mirroring the per-section guards added in Phase 3 / 2026.5.4.9. GroupConfig only has ``name`` + ``workers`` so the active surface is small, but future schema drift between dashboard and server now surfaces as a default-level operator log instead of a silent drop. Phase 6 of #328.

### Fixes

## [2026.5.4.9] - 2026-05-04

### Features

### Changes
- **config:** per-section ``_apply_X`` handlers now run a generic dataclass-aware dispatch pass after their custom validators. This eliminates the cherry-pick allow-list pattern that produced the silent-drop bug class — adding a field to ``DroneConfig``, ``QueenConfig``, ``TestConfig``, or ``NotifyConfig`` no longer requires a corresponding manual update to a hand-maintained scalar list. Generic dispatch type-validates against ``__dataclass_fields__`` and emits a section-prefixed WARNING for any unknown sub-key (e.g. ``drones.garbage_field``) — same fail-loud signal as the top-level guard from 2026.5.4.8 but at section depth. Phase 3 of the multi-phase #328 fix.
- **config:** ``_apply_drones`` now persists fields that were silently dropped by the previous allow-list: ``enabled`` (drone toggle), ``context_warning_threshold``, ``context_critical_threshold``, ``speculation_enabled``, ``idle_nudge_interval_seconds``, ``idle_nudge_debounce_seconds``. ``_apply_test`` now persists ``enabled``. None of these were currently bug-causing because the dashboard didn't send them, but they're operator-editable from the API and were silently lost — the audit (Phase 1) flagged them as Bug C class drift.

### Fixes
- **dashboard:** group-edit modal now reads its source data (``allWorkers``, ``currentMembers``) from a live JS state cache rather than page-load Jinja. Pre-fix, creating a new group and immediately clicking Edit on it opened the modal with empty members because the inline ``{% for g in config.groups %}`` loop was rendered server-side at page load and never knew about groups created in the current session — operators had to Ctrl-Shift-F5 to recover. The cache (``window._configState.groups``, ``.workers``) is seeded at page load and mutated in lockstep with every successful group/worker CRUD response. Phase 5 of #328 (Bug A from Amanda's report).

### Tests
- **config:** comprehensive end-to-end ``HiveConfig`` round-trip test (``tests/test_config_store.py::TestComprehensiveRoundTrip``). Builds a config with non-default values for every persistable field, walks it through ``save_config_to_db → load_config_from_db``, and asserts the serialized dicts match. Locks in the persistence contract for every field; future drift fails this test loudly. Found one real bug along the way: the ``groups`` table has no ``sort_order`` column so group display order is lost on reload (Bug D, tracked separately for the next release). Phase 4 of #328.

## [2026.5.4.8] - 2026-05-04

### Features

### Changes
- **config:** ``ConfigManager.apply_update`` now warns at WARNING level on unknown top-level body keys. Previously every per-section ``_apply_X`` cherry-picked sub-fields it knew about and the dispatcher itself had the same bug for top-level keys — a dashboard typo or schema drift between client and server would silently drop entire sections with no operator signal. The fail-loud guard catches future schema drift the moment a key arrives that no handler consumes. Phase 2 of the multi-phase silent-drop fix from #328.

### Fixes
- **config:** ``ConfigManager.check_file`` (YAML hot-reload) no longer overwrites in-memory groups when the YAML on disk lacks a groups section. Mirrors the existing ``approval_rules`` preservation pattern at lines 152-154 — groups live in the DB in DB-first mode, so an unrelated scalar edit to ``swarm.yaml`` shouldn't wipe them. ``check_file`` has no production caller in this branch (operator-driven reloads use ``os.execv`` from ``_exec_restart``), but the path was a footgun for anyone wiring it up later. Phase 2 defensive fix from #328.

### Docs
- **audits:** added ``docs/audits/config-save-chain-2026-05-04.md`` — full layer-by-layer coverage matrix for every ``HiveConfig`` field across the six save-chain layers (dataclass / saveSettings JS / apply_update / per-section _apply_X / save_config_to_db / load_config_from_db). Identifies all currently-affected fields and informs Phase 3 (generic dispatch), Phase 4 (round-trip test), Phase 5 (UI reconciliation). Phase 1 deliverable for the multi-phase #328 plan.

## [2026.5.4.7] - 2026-05-04

### Features

### Changes

### Fixes
- **config:** ``ConfigManager._apply_notifications`` now persists the full ``NotifyConfig`` schema. The previous version only handled three top-level scalars (``terminal_bell``, ``desktop``, ``debounce_seconds``) and silently discarded everything else — ``email.*``, ``webhook.{url,events}``, ``templates``, ``desktop_events``, ``terminal_events``. Operators editing SMTP settings in the dashboard saw the "saved" toast but the values never reached ``save_config_to_db``; after a restart the page rendered the defaults again, looking like a load-time bug while the actual defect was here in the apply path. Reported in #328 (Bug C). Also factored a shared ``_validate_string_list`` helper to keep the per-section apply functions under the C901 complexity gate.
- **notify:** ``filtered_backend`` and ``make_email_backend`` now tolerate unknown event-type names by skipping them with a debug log instead of raising ``ValueError``. The pre-existing ``test_config_notification_validation`` contract — "validation is advisory; bad event names shouldn't block the save" — was being upheld accidentally because ``_apply_notifications`` was discarding the ``desktop_events`` field before it ever reached the bus. Once the apply path was fixed, the bus's strict construction would crash the whole apply chain on a single typo, returning HTTP 400 to the dashboard. Now an unknown name is skipped, the rest of the config saves, and the typo is preserved verbatim in the DB for forensics.

## [2026.5.4.6] - 2026-05-04

### Features

### Changes

### Fixes
- **config:** DB save failures in ``ConfigManager._save_to_db`` now log at WARNING level (was DEBUG) so they show up in default-level operator logs. Reported in #328: a user's Groups edits weren't persisting across reboots, and there was no forensic evidence at WARNING because the failure was being swallowed at DEBUG. Also locks in the existing runtime ``log_level`` propagation (config edit → ``setup_logging`` reconfigures the live ``swarm.*`` logger, no restart) with a regression test so the diagnostic flag itself can't decay.

## [2026.5.4.5] - 2026-05-04

### Fixes
- **dashboard:** task modal stops jumping when "View source" is toggled. Both the rich-text editor and the source textarea now use ``height: 18rem`` (exact pin) instead of ``min-height: 18rem`` — empty ``contenteditable`` collapsed tighter than an empty textarea on min-only constraints, so toggling moved the rest of the modal up or down by ~5rem. Overflow scrolls inside the editor; user-resize (``resize: vertical``) is off because asymmetric resizing would re-introduce the jump on the next toggle.

## [2026.5.4.4] - 2026-05-04

### Changes
- **dashboard:** task list collapses to one or two lines per task. The metadata row (status / `#N` / priority / type / cross-project / title / assigned worker / age / badges / actions) stays as the always-visible line; completed tasks add a single-line resolution excerpt below. Description preview, acceptance criteria summaries, context refs, tag chips, and attachment thumbnails no longer render inline — click the row (anywhere except a button/link/input) to open the Edit modal for full content. Hover the row for a native tooltip with the first ~200 chars of the description.

## [2026.5.4.3] - 2026-05-04

### Features

### Changes

### Fixes
- **worker:** ``WorkerService.launch`` now passes ``resume=True`` when re-launching workers post-holder-respawn. Previously the post-Reload re-launch path (``if workers:`` branch — fires when ``self._workers`` already has entries from the prior daemon process) called ``add_worker_live`` without the kwarg, defaulting to ``resume=False``, so the provider command came out as ``["claude"]`` instead of ``["claude", "--continue"]``. Result: every Reload that involved a holder respawn lost in-progress Claude Code conversation state for every worker. Regression test in ``test_worker_service`` asserts the kwarg.

## [2026.5.4.2] - 2026-05-04

### Features
- **pty:** graceful holder restart — new `restart_in_place` IPC command and `swarm holder-restart` CLI. The holder snapshots its worker registry + ring buffers to `~/.swarm/holder-handoff.json`, marks each PTY master FD as inheritable via `F_SETFD`, and `os.execv`s into a fresh `swarm.pty.holder --inherit` invocation. Worker child processes (Claude Code sessions) are unaffected — they own the slave end of the PTY and the kernel keeps it open as long as anyone holds the master. This makes future holder code rollouts (e.g. the 2026-04-21 `_MAX_WRITE_BUFFER` raise from 1 MB to 8 MB) zero-disruption: previously the only way to deploy a holder fix was `kill <holder_pid>` which terminated every running worker session.
- **dashboard:** task description editor became a real WYSIWYG. Visible surface is a `contenteditable` div that renders Markdown (headings, lists, bold/italic, code, blockquotes, images); a hidden source textarea always carries the markdown serialization (form submission + `htmlToMarkdown` round-trip every input/blur). New "View source" toggle reveals raw markdown for power users — toggling preserves height (`visibility: hidden` on the toolbar instead of `display: none`). New formatting toolbar with B / I / S, H1 / H2 / H3, bullet & numbered lists, blockquote, link, inline code, horizontal rule, clear formatting — all driven by `document.execCommand` against the contenteditable.

### Changes

### Fixes
- **dashboard:** description grid now drops to a single column when the preview pane is off, so the textarea fills the full modal width instead of staying half-width.

## [2026.5.4] - 2026-05-04

### Features
- **dashboard:** task descriptions now render Markdown — paste from Word, Outlook, or any rich source and headings, paragraphs, lists, bold/italic, links, images survive into the saved task. Live preview pane next to the textarea (toggleable) plus rendered descriptions in the task list.
- **paste:** HTML→Markdown converter for clipboard payloads, with fallbacks: Word desktop's RTF clipboard is parsed for embedded `\pngblip`/`\jpegblip` image hex when no file blobs are exposed; images upload immediately on paste so saved descriptions never carry stale `blob:` URLs; relative `![](media/foo.png)` refs (pandoc-style) are auto-rewritten to `/uploads/<basename>` when matching files are dropped onto the dropzone. Word `MsoListParagraph` paragraphs become real markdown bullets.
- **jira:** drag-and-drop import — drop a Jira issue URL (or bare `KEY-N`) onto the task panel and a single `/api/jira/import-by-key` call pulls the issue, comments, and attachments into a new task. New `JiraSyncService.import_one` + `POST /api/jira/import-by-key`.
- **jira:** ADF descriptions and comments now convert to Markdown — paragraphs, headings, lists, blockquotes, code blocks, inline marks (bold/italic/code/strike/links), mentions, emojis, hard breaks all preserved. Replaces the old `_extract_text` flatten that produced one space-joined run-on string.
- **email:** `_html_to_text` rewritten as an `HTMLParser`-based Markdown emitter — same fidelity as the Jira ADF path. Inline `cid:<contentId>` image refs in the body get rewritten to `/uploads/<basename>` after the matching attachment is saved, so embedded Outlook images render in the preview instead of showing as broken refs.
- **email-drop:** Outlook drag-and-drop now prefers the Graph fetch path (`multimaillistmessagerows` → `/me/messages/{id}?$select=…&$expand=attachments`) over the bare-subject `text/plain` fallback. Cascade: `body.content` → `uniqueBody.content` → `bodyPreview` so signature-only or stripped-body emails still produce text.
- **mcp:** `swarm_task_status({number: N})` returns the full task detail (description, priority, type, tags, deps, jira key, acceptance criteria, context refs, attachments, resolution) instead of just the title one-liner. List views stay compact.
- **mcp:** worker task messages include per-format extraction hints — `IMAGE: …`, `TEXT: …`, `WORD DOC: pandoc … / docx2txt …`, `PDF: pdftotext … / pypdf …`, `SPREADSHEET: openpyxl …`, `PRESENTATION: pandoc / python-pptx …` — so workers know which tool to reach for instead of trying `Read` on a binary blob.
- **dashboard:** task modal UX refactor. Description + live preview now sit side-by-side on screens ≥1100px (textarea fills full width when preview is off). Cross-project, acceptance criteria, context refs, and depends-on are consolidated into one `<details>`-based "Advanced" section that defaults closed with a count badge showing how many fields are populated; auto-expands on edit when data is present.
- **dashboard:** attachment chips in the modal and task list are now clickable links pointing at `/uploads/<basename>`, with the 12-char content-hash prefix stripped for display.

### Changes
- **hooks:** worker projects' `.claude/settings.json` now grants `Read(//<home>/.swarm/uploads/**)` and `Read(//<home>/.swarm/cross-tasks/**)` so absolute paths into Swarm-shared dirs (Jira attachments, pasted images, email imports) auto-allow without prompting.
- **dashboard:** Assign-and-start now dispatches to `SLEEPING` workers in addition to `RESTING`. Sleeping workers were previously left with a queued task that only the IdleWatcher would later push, with debounce — now they get the task message immediately.

### Fixes
- **email:** `<meta>` and `<link>` are no longer treated as skip containers in the HTML→markdown parser. They're void elements (no end tag) so including them in `_SKIP_TAGS` permanently elevated `_skip_depth` and silently dropped the entire `<body>` of any standard Outlook/Graph email envelope.
- **paste-render:** markdown image/link/code tokens now reserve via null-byte sentinels before the emphasis transforms run, so URLs like `/uploads/abc_pasted_0.png` no longer get their `_pasted_` segment mangled into `<em>pasted</em>`.
- **paste-render:** soft newlines within a paragraph render as `<br>` instead of being collapsed to a space, so email-header blocks (From/To/Subject/Sent on consecutive lines) display one-line-per-line in the rendered preview and task list.

## [2026.5.1] - 2026-05-01

### Features

### Changes

### Fixes
- **drones:** two-strike rule for IdleWatcher's `/mcp` recovery path (task #257). The original "no MCP activity since daemon boot" trigger was too coarse — a worker just legitimately parked on a task tripped the same signal as a worker whose Claude Code transport had really died, so every daemon reload produced a noisy `/mcp` injection on quiet workers. The watcher now records a first-strike marker and falls through to the normal task nudge on the first sweep; only a *second consecutive* sweep that still sees zero MCP activity injects `/mcp`. Workers with a healthy transport answer the warning-shot nudge with an MCP call and never see `/mcp`. New `_mcp_first_strike` set in `IdleWatcher`; updated `tests/test_mcp_tools_stale_recovery.py` with the three new sequence assertions (warning shot → /mcp on second sweep → no /mcp when activity recorded between).

## [2026.4.30] - 2026-04-30

### Features
- **Per-worker `/swarm-*` slash commands (task #283).** Workers now get six slash commands installed into `.claude/commands/` on every daemon start: `/swarm-status`, `/swarm-handoff`, `/swarm-finding`, `/swarm-warning`, `/swarm-blocker`, `/swarm-progress`. They wrap the most-used Swarm MCP tools so transcripts read cleanly and the coordination surface shows up in `/help`. The SessionStart bootstrap appends a one-line nudge listing the commands whenever a task or unread message is already injected, so workers discover the surface without needing to read CLAUDE.md. Sets the `install.py` pattern reused by the Skills work below.
- **`swarm-checkpoint` and `swarm-coordinate` Skills (task #284).** Two Claude Code Skills now install per-worker into `.claude/skills/` via the same `install.py` path that lands the slash commands. `/swarm-checkpoint` runs `/check` then branches: on green, stages changed files (never `-A`) and commits using the project's `/commit` conventions; on red, calls `swarm_report_progress(phase=blocked)` + `swarm_note_to_queen` and halts without committing. `/swarm-coordinate` is advisory only — surveys peer worker states and pending tasks, then outputs a delegation suggestion as text (never calls `swarm_create_task` itself; cross-worker dispatch stays Queen-only). The daemon's per-worker setup loop now invokes both `install_worker_commands` and `install_worker_skills`; the umbrella method was renamed `_install_worker_commands` → `_install_worker_artifacts` to reflect the broader scope.
- **Context-pressure drone — auto `/compact` (task #285 Phase 1).** Phase 0's audit confirmed `worker.context_pct` is already populated every 15s from session JSONL; this phase adds the action layer that turns the pressure signal into a `/compact` injection. Two tiers, state-aware paths: **Soft** (warn ≤ pct < crit, default 0.7) injects `/compact` for RESTING/SLEEPING workers and no-ops for BUZZING/WAITING (retries next sweep). **Hard** (pct ≥ crit, default 0.9) sends Ctrl-C then `/compact` to BUZZING workers, defers WAITING workers (operator owns the prompt), injects `/compact` directly for RESTING/SLEEPING, and skips STUNG. Hysteresis: each `(worker, tier)` fires at most once per approach; the worker must drop below `warn_threshold` to re-arm. Three new `SystemAction` values (`CONTEXT_COMPACT_INJECTED` / `INTERRUPTED` / `DEFERRED`) under `LogCategory.COMPACT`. New `src/swarm/drones/context_pressure.py` (~250 LOC, 94% covered); 24 new tests covering all state × pressure combinations.
- **Tiered verifier drone — adversarial post-completion check (task #286).** Item 4 of 4 from the 10-repo research bundle. Drift in multi-agent flows compounds: N workers means N opportunities for "I'm done" claims that don't match the spec. The verifier fires asynchronously after every `swarm_complete_task` and either confirms the work shipped clean or reopens the task with findings delivered as a `warning` peer message; existing `IdleWatcher` nudges the worker on the next sweep — no new dispatch path. **Tier 1** (deterministic, no LLM, runs first): empty git diff since task start? no `/check` evidence in worker buzz log? open peer warning on this task? → reopen. Most rejections short-circuit here; we never burn an LLM call when the failure is mechanically obvious. **Tier 2** (LLM verification via dedicated `VerifierClient` subprocess, distinct from the headless Queen) runs only when Tier 1 passes; verdict mapping covers `verified` / `uncertain` / `reopen`. Self-loop guard: `VERIFIER_MAX_REOPENS = 2` — after the second reopen still failing, drone escalates via a Queen thread of `kind=verifier-escalation` instead of reopening a third time. `queen_force_complete_task` honours an explicit operator override (`verify=False`). Schema bumped v7 → v8: new `verification_status`, `verification_reason`, `verification_reopen_count` columns on `tasks`. New `LogCategory.VERIFIER` and 7 new `SystemAction` values. Dashboard adds per-task verifier badge (`VERIFIED` / `REOPENED×N` / `ESCALATED` / `SKIPPED`) and a "Verifier flagged" filter chip persisted in localStorage. Files: `src/swarm/queen/verifier.py` + `src/swarm/drones/verifier.py` (~270 LOC) + 30 new tests across `tests/test_verifier_drone.py` (16) and `tests/test_verifier_subprocess.py` (14).
- **Email-completion replies styled as Aptos 12pt.** Replies drafted via `send_completion_reply` (the path that fires when an email-originated task is completed) now wrap the Queen's plain-text body in an inline-styled HTML `<div>` with `font-family: Aptos, Calibri, 'Segoe UI', sans-serif; font-size: 12pt;` so the inserted comment renders in Outlook's default Office 365 font. Inline styles (not `<style>` blocks) because Outlook's Word-based mail renderer drops `<style>` in message bodies but honours `style=""` on block elements. New `_format_reply_html()` helper escapes the body, converts newlines to `<br>`, and wraps in the styled div; empty input returns empty so failure-path callers don't emit a stray `<div>`. 5 new tests + 2 updated.

### Changes

### Fixes
- **Idle workers nudged on unread messages even with no active task.** Closed a structural blind spot: a RESTING/SLEEPING worker with unread messages but no active task on the board got ignored by both `IdleWatcher` (short-circuits when `active_tasks_for_worker()` is empty) and `InterWorkerMessageWatcher` (after #271 narrowed it, only nudged on `dependency` / `warning` types — `finding` / `status` / `note` slipped through silently). The watcher is now task-aware: same `_ACTION_REQUIRED_MSG_TYPES` filter when the worker has an active task (preserves #271's "don't distract in-flight work"), but lifts the filter when the worker has no active task — any unread message is reason to nudge. The buzz log entry now carries a `[no-task]` / `[with-task]` label so audits can tell the widened path from the #271 narrow path. 10 new tests + the existing 18 still green; the conservative `task_board=None` default preserves test fixtures without modification.
- **Daemon startup AttributeError after Skills rename (`_install_worker_commands` → `_install_worker_artifacts`).** Task #284's commit renamed the daemon method but missed the call site at `daemon.py:719`. Symptom: daemon crashed on startup with `AttributeError: 'SwarmDaemon' object has no attribute '_install_worker_commands'`. After a Reload (which `os.execv`s a fresh process) the AttributeError fired immediately and systemd flagged the service as failed → dashboard 502. Fix: update the call site to use the renamed method. The 247 daemon-suite tests still passed because the test fixture short-circuits `start()`; lesson noted that a future fixture should exercise `start()` so missing-attribute regressions in that path can't pass `/check` while breaking the live service.
- **Post-`/mcp` follow-up nudge so workers don't strand (task #315).** When `IdleWatcher` injects `/mcp` to recover a worker whose client-side MCP tool registry was dropped during a daemon reload (task #257), the worker dismisses the dialog and lands at an empty prompt. The same sweep cycle skipped the regular task nudge, so the worker would sit idle until the next sweep — up to `idle_nudge_interval_seconds` (default 180s). Operator evidence on 2026-04-29 (d365-solutions): `/mcp` fired, worker sat at empty prompt for 65s before the queen had to manually intervene with the task description. Fix: after firing `/mcp` successfully, schedule a fire-and-forget follow-up coroutine that waits 5s (configurable) and then sends the regular task nudge. Re-queries the task board so a task completed in the interim is respected, updates `_last_nudge` so debounce stays correct, and logs an `AUTO_NUDGE` entry tagged `post-/mcp follow-up:` for observability. 3 new regression tests covering happy path, task-completed-in-the-interim, and PTY error during follow-up.

## [2026.4.24.6] - 2026-04-24

### Features
- **PTY holder version-skew detection.** Root cause of the long-standing "terminal locks after reload, need 6 restarts" symptom: the holder is a double-forked persistent sidecar, so daemon reloads (os.execv) replace the daemon but leave the holder running with whatever bytecode it was spawned with. Commit 0df45be (2026-04-21) raised `_MAX_WRITE_BUFFER` 1 MB → 8 MB to fix the reload lockup, but the fix never actually ran in production because the operator's holder had been up since April 5 — Reload refreshed the daemon and immediately got dropped again as a "slow client" by the stale holder's 1 MB threshold. Diagnosed live 2026-04-24 by correlating `holder.pid` mtime (Apr 5) against the 5 consecutive `dropping slow client (buffer 1178874 bytes)` warnings in `~/.swarm/swarm.log` at ~1.18 MB — exactly the size the 8 MB change was supposed to tolerate. Fix: `holder.py` now captures a sha256 of its own source at module import time and exposes it via a new `version` MCP-like command (alongside `ping`, `spawn`, etc.). `ProcessPool._try_connect` hashes `holder.py` on disk after each successful ping, compares against the holder's import-time hash, and stores the result as `pool.holder_drift`. Drift triggers a loud `[holder-drift]` WARNING with the exact kill instructions naming the holder PID. Daemon surfaces `holder_drift` via `/api/health` and a dedicated `/api/holder/drift` endpoint. Dashboard adds a persistent red banner at the top ("PTY holder is stale. Reload won't help — kill PID X then restart swarm") with a one-click Copy button for the bounce command. Graceful degradation: an older holder that doesn't know the `version` cmd sets `unknown=true` without asserting drift, so the check itself never breaks the connection. 5 regression tests pin the contract: happy-path no-drift, drift detection + warning + PID naming, graceful-unknown fallback, `/api/health` exposure, `/api/holder/drift` endpoint returns pool state verbatim. Full suite: 3,964 passes.

### Changes

### Fixes

## [2026.4.24.5] - 2026-04-24

### Features

### Changes

### Fixes
- **Queen inbox auto-relay marks read at delivery (task #277).** Queen had no `swarm_check_messages` equivalent — `queen_view_messages` / `queen_view_message_stream` are read-only log views and the #235 PTY relay never touched `read_at`. Consequence: Queen acts on a worker note, but the dashboard inbox still shows it UNREAD forever unless the operator manually marks it. Live repro 2026-04-24: project-root note to queen (force-close #273/#274) → Queen processed + force-closed #274 + relayed the rest → operator checked "did you check your messages" → `queen_view_message_stream since_seconds=7200` still showed UNREAD. Option A from the task write-up: the auto-relay IS the Queen's consumption event, so `_auto_relay_to_queen` (`src/swarm/mcp/tools.py`) now takes an optional `message_id` and calls `d.message_store.mark_read(QUEEN_WORKER_NAME, [message_id])` right after firing the PTY inject. The three call sites (`swarm_send_message` direct-to-queen, `swarm_send_message` broadcast that includes queen via `roster_names.index`, `swarm_note_to_queen`) all pass the id. `queen_view_messages` / `queen_view_message_stream` stay read-only — they use `SELECT *` with no UPDATE. 5 new regression tests in `tests/test_mcp_tools.py::TestSendMessageQueenAutoRelay`: direct-to-queen marks read, broadcast marks queen's row only, note marks read, regular worker-to-worker doesn't touch queen's inbox, queen-self-message no-ops. Full suite: 3,959 passes.

## [2026.4.24.4] - 2026-04-24

### Features

### Changes

### Fixes
- **Remove legacy static-detail fallback that surfaced on mobile.** When the xterm CDN hadn't finished loading or the terminal WebSocket exhausted its reconnect attempts, `refreshDetailStatic()` rendered a pre-xterm HTML partial (`handle_partial_detail`) with `.detail-header`, `.msg-send-bar` ("Send message to …"), and `.worker-output` — a v1.0.0 view that looked stranded next to the modern action bar and mobile send bar on narrow viewports. Deleted `handle_partial_detail` + its `/partials/detail/{name}` route + the dead `sendWorkerMsg` handler + the now-orphaned CSS blocks (`.detail-header`, `.btn-icon`, `.worker-output`, `.tool-activity`, `.tool-pill`, `.msg-send-bar`, `.msg-input`). `refreshDetailStatic()` now renders a minimal spinner + "Connecting terminal…" card into `#detail-body` and retries `attachInlineTerminal(selectedWorker)` every 200 ms until `typeof Terminal !== 'undefined'`, mirroring the existing page-load `restoreWorker` poll at `dashboard.js:6613`. Full suite: 3,954 passes.

## [2026.4.24.3] - 2026-04-24

### Features

### Changes
- **Zero-drift invariant pinned: drone unread count and swarm_check_messages read from the same source (task #272).** Task was filed on the premise that `InterWorkerMessageWatcher` reported a phantom `4 total` nudge for `wifi-portal` while both the worker's `swarm_check_messages` and the Queen's `queen_view_messages since_seconds=86400` returned empty. Investigation: raw `sqlite3` dump showed four real rows — `id IN (123, 144, 164, 183)`, all `recipient='wifi-portal'`, all `read_at IS NULL`, all `msg_type='finding'`, from `public-website` / `project-root` / `public-website` / `public-website` on 2026-04-19 / 2026-04-20. Running `MessageStore.get_unread('wifi-portal')` directly against the live DB returned the same 4 rows. Both the drone's sweep and `_handle_check_messages` call `d.message_store.get_unread(worker_name)` — identical single-source query. No dual code path, no stale cache, no soft-delete hiding the rows from one caller and not the other. Queen's "no messages match" was a time-window artifact — `since_seconds=86400` excluded the 4-to-5-day-old messages. Worker's repeated empty `swarm_check_messages` results trace to a client-side stale-tools state (task #257's failure class: HTTP MCP transport dropped its session mid-reload, the call never reached the server). **The reported bug was a symptom of two already-shipped-but-not-deployed fixes**: (a) #271 (2026.4.24.2) filters `finding`-only inboxes to `AUTO_NUDGE_MESSAGE_SKIPPED` instead of nudging — buzz log confirms no `_SKIPPED` entries exist anywhere, meaning the running daemon predates #271; (b) #257 (2026.4.22.10) injects `/mcp` into workers whose client-side MCP tool registry is stale after a daemon reload — no `MCP_TOOLS_STALE` entries exist either. Once the operator reloads the dashboard, both fixes activate: #271 drops the nudge (informational-only), #257 detects wifi-portal's dead MCP session + forces a `/mcp` re-init so `swarm_check_messages` actually reaches the server and marks the 4 messages read. No drone code change required — the drone is reading from the right source. 8 new tests in `tests/test_unread_count_single_source.py` pin the zero-drift invariant permanently so any future refactor that introduces a denormalized unread counter or a dual query path gets caught: empty inbox agrees, 4 action-required agree, 4 informational agree, broadcast+direct agree, `mark_read` propagates to the drone view, queen-sourced also agrees, and structural assertions confirm both code paths literally import and call `MessageStore.get_unread`. Full suite: 3,952 passes.

### Fixes

## [2026.4.24.2] - 2026-04-24

### Features

### Changes
- **InterWorkerMessageWatcher narrowed to action-required message types (task #271).** Live repro 2026-04-24: wifi-portal was working a task and had self-resolved whatever dependency public-website's FYI message was about. The drone nudged anyway — "4 new messages, run swarm_check_messages" — risking derailing the worker mid-task. Same failure class as the hub #256 incident (Queen redirected a worker mid-plan) but at the drone layer. Fix: a new `_ACTION_REQUIRED_MSG_TYPES = {"dependency", "warning"}` gate in `src/swarm/drones/inter_worker_watcher.py`. Only unread messages of those types trigger a nudge; informational types (`finding`, `status`, `note`) no longer pull a worker off current work. When an inbox has only informational messages, the watcher writes an `AUTO_NUDGE_MESSAGE_SKIPPED` buzz entry (new `DroneAction`/`SystemAction` enum value) naming the sender + type summary so the operator has telemetry on the suppression. The skip entry is debounced per worker on the same window as regular nudges so the buzz log doesn't spam every sweep while the informational inbox sits unread. Mixed inboxes (at least one action-required message present) still nudge; the nudge wording surfaces the full unread count so the worker sees the informational backlog too. Queen-sourced messages remain excluded (her #235 Phase 1 relay already covers them). 7 new tests in `tests/test_inter_worker_watcher.py` pin: `finding` alone skips (the wifi-portal repro), `status` alone skips, `note` alone skips, `dependency` still nudges, `warning` still nudges, mixed inbox nudges on action-required while the count reflects total unread, and the SKIPPED entry is debounced. Existing 11 tests updated: the `_message` fixture defaults `msg_type="dependency"` so nudge-fires tests still pass. Full suite: 3,944 passes.

### Fixes

## [2026.4.24] - 2026-04-24

### Features
- **`swarm_draft_email` MCP tool — workers can create Outlook Drafts via the Graph integration.** Previously only the completion-reply auto-draft path used the Graph integration; that fires when an email-sourced task is completed and drops a reply in-thread. This adds the symmetric worker-initiated path: a worker can call `swarm_draft_email(to=[...], subject, body, cc?, body_type?, reason?)` to create a brand-new draft in the operator's Outlook Drafts folder. Use case: a worker needs the operator to reach out to a stakeholder (e.g. "ask for schema clarification on task #301 before implementing"), so the worker drafts the email + the operator reviews and sends manually from Outlook. **The draft is NEVER auto-sent** — operator must explicitly send from Outlook. New `GraphTokenManager.create_draft(to, subject, body, cc=None, body_type="text")` method in `src/swarm/auth/graph.py` wraps `POST /me/messages` on Graph; returns `{"id": "...", "web_link": "..."}` on success, `None` on failure. Tool handler in `src/swarm/mcp/tools.py` validates inputs (non-empty `to` list, required `subject` + `body`, `body_type ∈ {text, html}`, `cc` list of strings), then fire-and-forget schedules the Graph call as a background asyncio task (keeps `handle_tool_call` synchronous — existing 87-test sync caller surface unaffected). Success / failure writes a `DRAFT_OK` / `DRAFT_FAILED` buzz entry under `LogCategory.SYSTEM` so the dashboard surfaces the outcome without the worker needing to poll. Graph-not-connected / token-expired cases short-circuit with a clear "not connected" message pointing at the config page. 15 new tests in `tests/test_mcp_draft_email.py`: all 6 input-validation branches (missing/empty `to`, non-string entries, missing subject/body, invalid `body_type`), both integration-unavailable paths (`graph_mgr` is None, `is_connected()` returns False), the success round-trip (queued message + Graph call arguments + DRAFT_OK buzz entry + `html` body_type + `cc` list threading), the failure path (`DRAFT_FAILED` buzz entry on `None` return), and a Graph payload-shape pin (`toRecipients`/`ccRecipients`/`body.contentType` all match what Graph expects). README updated: MCP coordination-tool count 11 → 12 with the new tool row. Full suite: 3,937 passes.

### Changes

### Fixes

## [2026.4.23] - 2026-04-23

### Features

### Changes

### Fixes
- **`queen_force_complete_task` spurious `AttributeError` on email-originated tasks (task #270).** Symptom: Queen calls `queen_force_complete_task(number=N, resolution=..., reason=...)`, gets back `Error: '_asyncio.Task' object has no attribute 'assigned_worker'`, but the DB mutation actually landed (next `swarm_task_status` shows the task as `[completed]`, a second force-complete returns `Task ... cannot be modified (completed)`). Root cause: `SwarmDaemon.complete_task` had a local variable `task` bound to the `SwarmTask` at the top of the method, but the email-reply branch further down (`if source_email_id and self.graph_mgr and resolution`) did `task = asyncio.create_task(self._send_completion_reply(...))`, clobbering the local name. The post-ship self-loop added in task #225 Phase 3 (`self._auto_start_next_assigned(task.assigned_worker)`) then tried to read `.assigned_worker` off the `asyncio.Task`. Two consecutive nexus force-completes (tasks #266, #268, both with email sources) hit this in a single session. Fix: rename the local to `reply_bg` so it doesn't shadow the SwarmTask. Two-line change in `src/swarm/server/daemon.py`. Regression test `test_complete_task_email_path_does_not_clobber_task_variable` pins the exact path: assigned task with `source_email_id` + `graph_mgr` set, monkeypatched `_send_completion_reply` + `_auto_start_next_assigned`, asserts the captured worker_name is the original SwarmTask's `assigned_worker` rather than raising. Verified the test catches the pre-fix bug via temporary revert (reproduces the exact reported `AttributeError`). Full suite: 3922 passes.

## [2026.4.22.11] - 2026-04-22

### Features
- **`swarm queen contribute-claude-md` — local → shipped reverse sync (task #258).** Companion to #254's forward reconcile. Where `reconcile_queen_claude_md` pushes the shipped `QUEEN_SYSTEM_PROMPT` into the local `~/.swarm/queen/workdir/CLAUDE.md` on daemon start, this new flow pushes local edits back to the shipped constant. Before this, local improvements (Queen policy authored during operator corrections) accumulated on individual installs with no upstream path — one-off human curation only (the "Two Queens" section in #251, etc.). New module `src/swarm/queen/contribute.py` with: `compute_status()` (diff local vs shipped, return `ContributeStatus` with hunk count + unified diff), `emit_patch()` (produce a `git apply`-able unified diff targeting `src/swarm/queen/runtime.py` by rewriting the in-file `QUEEN_SYSTEM_PROMPT` triple-quoted literal; `_locate_constant_span` + `_rewrite_runtime_source` handle the surgery), `open_pr()` (full gh flow: new branch + rewrite + commit + push + `gh pr create` with graceful failure when `gh` isn't available or the worktree is dirty), `mark_synced()` (update `.claude_md_shipped` post-merge so #254's reconcile doesn't re-flag the same content), and `detect_repo_root()` (looks for the swarm checkout). New CLI subcommand `swarm queen contribute-claude-md [--emit-patch PATH | --open-pr | --mark-synced] [--repo-root DIR]`: no flags = status-only (diff summary, no writes); flags are mutually exclusive; auto-detect repo-root falls back to `--repo-root DIR`. Per operator clarification: the Queen is a global role, not operator-specific, so NO local-only marker subsystem was added — every hunk is a promotion candidate. Operator defers by not running the CLI, or strips a hunk from the emitted patch by hand. Integration with #254: the drift-flagged inbox notification now points at the contribute CLI so the Queen knows the mechanism on any future drift event. Port-in-pass: the `Tier-2-includes-redirect` rule the Queen authored locally under "High-confidence auto-actions" was promoted directly into `QUEEN_SYSTEM_PROMPT` as an exercise of the flow. 17 new tests in `tests/test_queen_claude_md_contribute.py`: `compute_status` diff + in-sync paths; constant-span locate + error on missing header; rewrite surgery; `emit_patch` produces git-applyable diff / writes empty on no-op / raises on missing runtime.py; `mark_synced` updates marker / raises without local file / prevents drift-flag on next reconcile; `count_hunks` utility; CLI smoke (help resolves + mutually exclusive flags rejected); and a commit-time guard test that fails if the live `~/.swarm/queen/workdir/CLAUDE.md` has diverged from the shipped constant (skips gracefully in CI/fresh-env). Full suite: 3921 passes.

### Changes

### Fixes

## [2026.4.22.10] - 2026-04-22

### Features

### Changes

### Fixes
- **Worker MCP tools-dropped recovery via IdleWatcher (task #257).** Root-cause for the recurring `rcg-dev-install` pattern ("swarm MCP server disconnected earlier this session — tools aren't available here anymore"): Claude Code's HTTP MCP transport hits its reconnect-retry ceiling during a daemon reload that the worker sits idle through, gives up, and marks the server's tool registry as unavailable client-side. Nothing ever triggers the server-side auto-revive path from #227 because the worker isn't making any new POSTs. The #239 SSE POST-response piggyback also can't help — it only fires on a POST. Worker wakes up with tools gone. **Fix (Option C per task spec)**: IdleWatcher drone detects the state and injects `/mcp` into the worker's PTY to force Claude Code's re-initialize flow. Detection criteria: worker is RESTING/SLEEPING with an active task, has made zero MCP dispatch calls since the daemon booted, and hasn't already had a refresh fired this boot cycle. Wiring: (a) `src/swarm/mcp/server.py` gains a `_worker_last_mcp_activity: dict[str, float]` module-level tracker updated on every `_dispatch()` call + a `get_worker_last_mcp_activity(worker_name)` getter; (b) `src/swarm/server/daemon.py` records `self.daemon_start_time = time.time()` on init (re-stamped on every `os.execv` reload); (c) `IdleWatcher` gains `mcp_activity_lookup` + `daemon_start_time` constructor args; (d) `pilot.set_idle_nudge_sender()` threads both through; (e) the daemon wires the tracker + boot timestamp when calling `set_idle_nudge_sender`. On a detected stale state, the watcher injects `/mcp` via `send_to_worker` and writes a `MCP_TOOLS_STALE` buzz entry under a new `LogCategory.MCP` (new category added alongside DRONE/TASK/QUEEN/etc. for MCP-session events). Each worker gets at most one refresh per boot cycle (`_mcp_refresh_fired` set); failed PTY injects clear the flag so the next sweep can retry. Operator / Queen get dashboard-visible telemetry for any future occurrence — no more diagnosing from screenshots. 9 new tests in `tests/test_mcp_tools_stale_recovery.py` pinning the fire-on-stale / skip-on-recent / at-most-once-per-boot / send-failure-retry / feature-disabled-when-callbacks-missing paths, plus two tests on the server-side activity tracker itself. Full suite: 3904 passes.

## [2026.4.22.9] - 2026-04-22

### Features

### Changes
- **Doc audit sweep — align README + CLAUDE.md + spec index with the post-#250/#253/#254/#255 reality.** Comprehensive in-repo doc audit (via `/audit-docs`) found 32 drift / stale / missing / structural findings across 16 markdown files. All applied. README.md: tool count 9→11 (added `swarm_report_blocker` and `swarm_note_to_queen`); new `swarm queen sync-claude-md` row in CLI reference; new Queen MCP tools subsection (15 tools); removed stale `"Ask Queen"` action-button example (that action was deleted in #253); corrected `queen.system_prompt` description at its second occurrence to name its headless-only scope. CLAUDE.md: fixed stale line-number references (`routes/system.py:201→218`, `daemon.py:2122→2307`); "Three mechanisms"→"Four mechanisms" (worker-reported blockers from #250 was added as the fourth); expanded module inventory to list specialized drones (idle_watcher, inter_worker_watcher, pressure, oversight_handler, state_tracker, task_lifecycle, directives, decision_executor, coordination, poll_dispatcher), the second Queen module (runtime.py, oversight.py, queue.py, context.py), the blockers store, and the full MCP tool split (11 worker + 15 Queen). docs/multi-llm-providers.md: added SHIPPED banner for Phase 1 (provider extraction refactor) with pointer to `src/swarm/providers/`; rewrote §2.1 Worker Startup (hardcoded `["claude","--continue"]` is gone) and §2.2 State Detection (pattern location moved from deleted `worker/state.py` to `providers/claude.py` + `drones/state_tracker.py`). docs/claude-code-roadmap.md: added "last-reviewed 2026-04-16" note + pointer to CHANGELOG for post-roadmap shipping (#248, #250, #251, #253, #254). Gitignored spec directory: also synced (local only, not committed) — `interactive-queen.md` status `READY_TO_BUILD`→`shipped`; `phase4-mcp-messaging.md` gained post-Phase-4 extensions table for the new tools; `sqlite-unified-storage.md` added full v6 schema (queen_threads/messages/learnings + `proposals.thread_id`) and v7 schema (worker_blockers); `headless-queen-architecture.md` gained YAML frontmatter; two new retrospective specs (`worker-blockers.md`, `pressure-threshold-tuning.md`) cover features that shipped without design docs. No source code touched.

### Fixes

## [2026.4.22.8] - 2026-04-22

### Features

### Changes
- **Fresh-install Queen onboarding audit + regression tests + README refresh (task #255).** Audit of the install path from "user runs swarm init" through "first daemon boot completes" to verify Queen setup lands correctly on a brand-new install. Findings: **(1) Runtime path is clean** — `reconcile_queen_claude_md()` handles the missing-parent-dir case via `mkdir(parents=True, exist_ok=True)`, `auto_migrate()` creates all 21 Queen-critical tables on a non-existent DB idempotently, `QueenConfig()` defaults are sane (enabled=True, empty system_prompt that the daemon seeds from `HEADLESS_DECISION_PROMPT`), `HiveConfig()` always includes `.queen`. `swarm queen sync-claude-md` resolves via the CLI subcommand group. Queen is a synthetic worker (never persisted to `workers` DB table) — by design, re-created per daemon boot. **(2) README had stale content** — fixed. Removed the `Alt+Q | Ask Queen` keyboard shortcut from the shortcuts table (that binding was deleted in task #253 when the Ask Queen UI was removed). Rewrote "Queen & Proposals" section to cover the two-Queens architecture (interactive PTY coordinator vs headless subprocess decision function), with specific mentions of: how to reach the interactive Queen (click her worker tile), what `~/.swarm/queen/workdir/CLAUDE.md` is and that the operator can edit it, the drift-detection / reconcile mechanism from #254, and the `swarm queen sync-claude-md` CLI flags. Corrected the `queen.system_prompt` config description — it's the headless-decision prompt only after #253, not a global Queen prompt (the interactive Queen's role lives in her CLAUDE.md). Added 9 regression tests in `tests/test_fresh_install_queen.py` covering: workdir creation when parent dir missing, CLAUDE.md seeded with expected role markers, marker equals shipped constant at seed time, all Queen-critical tables present after `auto_migrate` on a fresh DB, migrate idempotency, `QueenConfig` + `HiveConfig` defaults, headless-decision seed fires on empty, and a full end-to-end boot sequence (DB migrate → config seed → reconcile) asserting SEEDED action + workdir layout. Full suite: 3,895 passes.

### Fixes

## [2026.4.22.7] - 2026-04-22

### Features
- **Queen CLAUDE.md sync across swarm updates (task #254).** Problem: `~/.swarm/queen/workdir/CLAUDE.md` and the shipped `QUEEN_SYSTEM_PROMPT` constant in `src/swarm/queen/runtime.py` drift every release. The daemon preserves operator / Queen edits (good) but silently misses shipped content updates (bad) — existing installs age without the operator ever knowing. Fix: three-state reconciliation on every daemon startup. `reconcile_queen_claude_md()` compares **SHIPPED_LATEST** (current constant) vs **SHIPPED_AT_LAST_SYNC** (reference copy at `workdir/.claude_md_shipped`) vs **ON_DISK** (the live CLAUDE.md). Decision matrix: (a) shipped unchanged → no-op regardless of local edits; (b) shipped changed, on-disk clean → auto-update; (c) shipped changed, on-disk has local edits → drift-flagged: write side-by-side reference files `CLAUDE.md.shipped-latest` and `CLAUDE.md.shipped-last`, log warning, send a `finding` message to the Queen's inbox via `MessageStore` (triggers the #235 auto-relay so she surfaces it to operator next turn), emit `STATE_TRANSITION` buzz entry so the dashboard shows it; (d) first upgrade against pre-existing CLAUDE.md with no marker → seed marker from current on-disk baseline (treat current state as the reference point). New CLI: `swarm queen sync-claude-md` without flags shows three-way status; `--accept-shipped` overwrites on-disk with current constant + updates marker + clears drift refs; `--keep-local` updates marker only (acknowledge drift, preserve local edits) + clears drift refs. Mutually exclusive. Module-level constants `CLAUDE_MD_FILENAME`, `SHIPPED_MARKER_FILENAME`, `DRIFT_SHIPPED_LATEST_SUFFIX`, `DRIFT_SHIPPED_LAST_SUFFIX`, `ReconcileAction` exposed for test + CLI reuse. Daemon startup calls `reconcile_queen_claude_md(QUEEN_WORK_DIR)` unconditionally before Queen spawn so existing-Queen reloads also pick up new shipped content (not just fresh spawns); `_handle_queen_claude_md_reconcile` dispatches by action. Also synced `QUEEN_SYSTEM_PROMPT` with the Queen-authored "Two Queens: interactive and headless" section from her on-disk edits so shipping this release doesn't immediately trigger the auto-update path and erase her work. 12 new tests in `tests/test_queen_claude_md_reconcile.py` covering all four matrix cells + first-upgrade + idempotency + full lifecycle + CLI mode errors. Full suite: 3886 passes.

### Changes

### Fixes

## [2026.4.22.6] - 2026-04-22

### Features

### Changes

### Fixes
- **Pressure-suspend no longer trips on sticky swap with healthy memory.** Live incident 2026-04-22: 10 workers suspended on a dev machine sitting at `mem=62.7%, swap=60.7%` with no real memory pressure. Root cause was two-fold: (1) the swap-triggered HIGH branch in `classify_pressure` used hardcoded `mem_pct >= 60` and `mem_pct >= 70` guards, so any memory usage above 60% combined with >50% swap (the default `high_swap_pct`) would suspend workers, ignoring the fact that swap is "sticky" in Linux — once cold pages are paged out they stay there until explicit swap-off or a reboot even when RAM is abundant; (2) `high_swap_pct=50` was too tight for a dev machine that has swap enabled. Two coordinated fixes: **(a)** the inner memory guards in `classify_pressure` are now derived from the configured memory thresholds rather than hardcoded — HIGH requires `mem >= elevated_mem_pct` (default 80) alongside `swap >= high_swap_pct`; CRITICAL requires `mem >= high_mem_pct` (default 90) alongside `swap >= critical_swap_pct`. Tuning one pair pushes the coupling in sync. **(b)** Swap threshold defaults bumped to match reality: `elevated_swap_pct` 25→40, `high_swap_pct` 50→70, `critical_swap_pct` 75→85. Memory thresholds unchanged (80 / 90 / 95). Net effect on the reported state: mem=62%/swap=60% now classifies as ELEVATED (informational, no suspend) instead of HIGH (suspend). Genuine pressure (mem >= 80% AND swap >= 70%) still triggers HIGH. Three tests updated and one new regression test pinned (`test_swap_sticky_does_not_suspend`) to the exact observed dev-machine state. Defaults in `ResourceConfig` (`src/swarm/config/models.py`), the loader fallbacks (`src/swarm/config/loader.py`), and the `classify_pressure` / `take_snapshot` signatures all updated in sync so fresh installs get the new behavior. Existing deployments with `config.resources.*` values in swarm.yaml or DB keep their overrides — this only moves the defaults. Full suite: 3874 passes.

## [2026.4.22.5] - 2026-04-22

### Features

### Changes
- **Headless Queen architecture close-out (task #253 follow-up).** Three coordinated changes that locked in the "keep the headless Queen, don't route to interactive" decision from the `/interview` session summarized in `docs/specs/headless-queen-architecture.md`. **(A) High-confidence-not-done backoff** in `src/swarm/drones/task_lifecycle.py`: when Queen returns `done=False` with `confidence >= 0.8` on a completion analysis, the per-task re-propose cooldown extends from 5 min to 30 min (`_HIGH_CONF_NOT_DONE_BACKOFF = 1800`). New callback chain `analyzer.analyze_completion` → `daemon._record_completion_verdict` → `pilot.record_completion_verdict` → `TaskLifecycle.record_completion_verdict` feeds the verdict back. `done=True` clears the entry so completion proposals proceed. Projected savings from audit data: ~1,021 redundant LLM calls / 30d eliminated (34/day), top offender workers were getting 96-162 Queen completion calls on a single task across a 30-day window because the drone kept re-asking on unchanged PTY state. **(B) Periodic hive-coordination caller deleted**: `Queen.coordinate_hive`, `QueenAnalyzer.coordinate`, `EscalationHandler.coordinate_hive`, `DronePilot._coordination_cycle`, `CoordinationHandler.coordination_cycle`/`_process_coordination_result`/`_coordination_snapshot_unchanged`, daemon's `coordinate_hive` delegate, the `POST /api/queen/coordinate` route, and `_COORDINATION_INTERVAL` from `poll_dispatcher.py` — all removed. Coverage was duplicated by specialized drones (IdleWatcher, InterWorkerMessageWatcher, FileOwnership, PressureManager). `CoordinationHandler.capture_worker_outputs` preserved under the same import path since the DirectiveExecutor pipeline still depends on it. **(C) CLAUDE.md gained a "Two Queens: division of labor" section** naming the interactive Queen's conversational role vs the headless Queen's stateless-decision role, the division of labor for future callers (operator-facing → interactive, drone-driven + high-volume → headless), and a pointer to `docs/specs/headless-queen-architecture.md` so the "should we collapse these?" question doesn't recur. Also added the pressure-test heuristic: new "should we add a Queen call?" requests check deterministic drone rules first. 32 coordination-cycle tests removed across `test_pilot.py`, `test_queen.py`, `test_daemon.py`, `test_api.py`, `test_analyzer.py` (all dependent on deleted surface); 2 capture-output tests rewritten to exercise `CoordinationHandler.capture_worker_outputs` directly instead of through the deleted cycle wrapper; 8 new `TestTaskCompletionReproposal` tests pin the high-conf backoff, low-conf passthrough, `done=True` clear, and backoff-expiry paths. `docs/specs/headless-queen-architecture.md` documents the full audit + interview + decision for posterity. Full suite: 3,873 passes.

### Fixes

## [2026.4.22.4] - 2026-04-22

### Features

### Changes
- **Delete redundant "Ask Queen" dashboard UI; repopulate headless-decision prompt (task #253).** Task #252's audit documented that the legacy `swarm.queen.queen` headless path is load-bearing for four programmatic callers (drone auto-assign in `task_lifecycle.py`, oversight monitor in `queen/oversight.py`, hive coordination in `drones/coordination.py`, `QueenAnalyzer.analyze_worker` in `server/analyzer.py`) PLUS a redundant dashboard UI surface. Operator decision: keep the programmatic paths, delete the UI — the interactive Queen's worker tile is the single entry point for operator→Queen conversation. This commit removes: (a) the three `/action/ask-queen*` routes (`src/swarm/web/routes/queen.py` deleted entirely, its `register(app)` pulled from `web/routes/__init__.py`, re-exports removed from `web/app.py`); (b) the Ask Queen header button + mobile menu entry in `dashboard.html`; (c) the ask-query footer inside the queen-modal (question input + Ask/Re-analyze/Apply buttons) — the modal itself stays for proposal/escalation display; (d) the `askQueen` / `askQueenWorker` / `askQueenQuestion` / `applyDirectives` / `applyDirective` / `_execDirective` / `renderQueenResult` / `startQueenCooldown` JS functions (~280 LOC), plus the `lastDirectives` state, the `q` keyboard shortcut, the worker context-menu `queen` case, the `doAction('queen')` branch, and the action-button dropdown's 'Ask Queen' default + 'queen' action entry in `config.html`; (e) the misleading `system_prompt` textarea in the config page (load + save wiring). Post-#251 `config.queen.system_prompt` was cleared, which would have degraded the four programmatic callers to running with no role framing. This commit also adds `HEADLESS_DECISION_PROMPT` as a module constant in `src/swarm/queen/queen.py` — a tight, stateless decision prompt covering the six invocation shapes (task auto-assignment, oversight, completion evaluation, escalation response, hive coordination, prolonged-BUZZING analysis) with decision rules (>=0.85 act, <0.6 wait, destructive→wait unless durably authorized, no cross-worker file overlap) and evidence order (PTY tail > buzz log > messages > learnings, with learnings always taking primacy). Anchored back to the interactive Queen's `~/.swarm/queen/workdir/CLAUDE.md` for policy consistency. The daemon's `__init__` seeds the constant into `config.queen.system_prompt` when the field is empty — covers fresh installs and the post-#251 cleared deployment without a schema migration. Operator override still wins: any non-empty value in swarm.yaml or the DB bypasses the seed. Live DB also repopulated with the new prompt (one-shot SQL) so this deployment picks it up immediately. Six new tests in `tests/test_headless_decision_prompt.py` pin constant presence, required role markers, absence of stale UI references, empty→seed, override→preserve, and default-config→seed behavior. `src/swarm/config/models.py` docstring on `QueenConfig.system_prompt` rewritten: accurately describes it as the headless-decision prompt scope (drone auto-assign / oversight / hive coordination / analyzer) rather than the interactive Queen's role. Full suite: 3903 passes.

### Fixes

## [2026.4.22.3] - 2026-04-22

### Features

### Changes
- **Queen system prompt migrated from DB → `~/.swarm/queen/workdir/CLAUDE.md` (task #251).** The Queen has been running interactively for some time, but `config.queen.system_prompt` in swarm.db still held the old headless-mode prompt from the pre-interactive era — RCG-specific worker names that no longer exist, proposals-require-approval language, "set confidence to 0.0 for plans", "use assign_task not send_message" (both obsolete — Queen now writes via `queen_prompt_worker` / `queen_reassign_task` / etc). The interactive Queen already reads her role from `~/.swarm/queen/workdir/CLAUDE.md`, seeded on first spawn from `QUEEN_SYSTEM_PROMPT` in `swarm.queen.runtime`. This task (a) cleared `config.queen.system_prompt` on this deployment (empty string — idiomatic given the field's empty default + the serializer's omit-when-empty behavior), (b) sent the verbatim old prompt to the Queen's inbox for archival, (c) let the Queen author the interactive-mode CLAUDE.md replacement herself (operator + Queen collaboration — Queen is the subject matter expert on how she operates), (d) synced the refreshed CLAUDE.md content back into the `QUEEN_SYSTEM_PROMPT` module constant so new swarm installs get the same first-pass prompt, (e) added a deprecation note on `QueenConfig.system_prompt` pointing future readers at CLAUDE.md. The field is still read by the legacy headless `claude -p` coordinator path in `swarm.queen.queen` for backward compat — new deployments should leave it empty. The refreshed prompt adds: "Your jurisdiction (don't delegate these)" section listing Queen-owned content (CLAUDE.md, learnings, threads, synthesis memos, Queen-affecting proposals) vs worker jurisdiction (code, shells, tests, DB schema lookups); full Read+Write tool catalogue including the elevated write tools (`queen_prompt_worker`, `queen_reassign_task`, `queen_force_complete_task`, `queen_interrupt_worker`, `queen_save_learning`); inbox auto-push guidance naming the `full=true` flag for verbatim relay; drone-driven routine nudges paragraph distinguishing exception-handling from duplication; `swarm_report_blocker` usage note (task #250 integration); a "Drafting for non-technical staff" voice subsection preserving the email-reply guidance salvaged from the old prompt. Full suite: 3897 passes.

### Fixes

## [2026.4.22.2] - 2026-04-22

### Features
- **`swarm_report_blocker` MCP tool + IdleWatcher skip-on-blocker path (task #250).** Closes the loudest recurring operator-pain pattern from this session: admin has #246 (which is blocked on platform's #245), the operator knows it, admin knows it — and every 3 minutes the IdleWatcher nudges admin anyway with "You have #246 active but appear idle…" because the watcher has no way to distinguish "idle because stuck" from "idle because waiting on a dependency that hasn't shipped". New `swarm_report_blocker(task_number, blocked_by_task, reason)` tool lets a worker persist that declaration. Storage: new `worker_blockers` table (schema v7 migration) keyed on `(worker, task_number)` with `INSERT OR REPLACE` semantics so re-reports refresh the `created_at` — the refresh matters for the message-based auto-clear described below. New `BlockerStore` in `src/swarm/tasks/blockers.py` wraps the table with `report` / `list_for_worker` / `clear` / `has_active_blocker` APIs, sharing the `SwarmDB` connection + lock so writes serialize alongside tasks/messages/buzz. IdleWatcher gains two constructor args (`blocker_store`, `message_has_newer`) and a pre-nudge check: if `has_active_blocker(worker)` returns a live blocker, the sweep skips the nudge and writes an `AUTO_NUDGE_SKIPPED` buzz entry naming both tasks (`reported blocker on #246 (waiting on #245)`). Two auto-clear triggers purge the row without a second MCP call — (a) the `blocked_by_task` flips to `completed` on the task board, (b) a new message lands in the worker's inbox after the blocker was declared (operator-authored "something else changed, check your inbox" escape hatch). Daemon wires `message_store.get_unread()` into `message_has_newer` so option (b) works out of the box. 17 new tests across `tests/test_blockers.py` (persistence, both auto-clear paths, refreshed-timestamp path, multi-active-task guard) and `tests/test_mcp_tools.py::TestReportBlocker` (schema validation + handler). Full suite: 3897 passes. CLAUDE.md's "Autonomous task momentum" section gained bullet #4 documenting the new tool and when workers should call it.
- **`swarm_note_to_queen` MCP tool for side-channel Queen notes (task #248).** Extends #235's inbox-relay mechanism to cover the failure mode where a worker addresses the Queen through PTY side-channel text — pre-response reminders, inline coordination questions, "FYI queen" annotations — that never went through `swarm_send_message`. Live repro 2026-04-22: project-root wrote "Reminder: should I /clear before this dispatch run?" in their own PTY before sending a coordination memo; the Queen missed the reminder until the operator screenshotted it. New tool persists the note in the message store (new `note` msg_type added to `_VALID_MSG_TYPES`) and fires the same `_auto_relay_to_queen` path the formal-message handler uses, so the Queen's next turn sees it naturally. Self-notes (queen → queen) short-circuit to avoid PTY self-loop. Workers calling the tool log an `OPERATOR` buzz entry with an `→ queen (note): ...` prefix so the audit trail disambiguates notes from findings/warnings. Three new tests in `tests/test_mcp_tools.py::TestNoteToQueen` pin the persist + relay path, the missing-content guard, and the self-loop guard. CLAUDE.md's "Queen message-surface elevation" section names the new tool alongside the existing inbox-relay path. Full suite: 3880 passes.

### Changes

### Fixes

## [2026.4.22] - 2026-04-22

### Fixes
- **MCP auto-revive POST now responds as SSE with list_changed piggyback (task #239).** Closes the last propagation gap in the chain of #226 → #227 → #237. `broadcast_tools_list_changed()` delivered to `_broadcast_subscribers`, which only holds clients with an open `GET /mcp` stream. Claude Code's HTTP MCP transport doesn't maintain one — it opens GET briefly around `initialize` and closes it. So the broadcast had no audience for the common case, and every swarm iteration cycle required a manual Claude Code bounce for the Queen + workers to see schema changes (observed 4+ times this session across #195, #198, #225, #237). Fix: when the POST handler auto-revives a session (task #227 path — stale `Mcp-Session-Id` from a pre-reload daemon), it now returns `text/event-stream` carrying the `tools/list_changed` notification FIRST, then the JSON-RPC response. Per MCP Streamable HTTP spec §7 a POST response MAY be an SSE stream with multiple messages. Clients that can't receive out-of-band notifications still get the re-enumerate nudge bundled with their response. Known-session POSTs keep returning plain JSON — only auto-revive sessions (where we know the schema is likely stale) pay the SSE path. Also added diagnostic logging on every `_push_tools_list_changed` call: `[mcp] list_changed_sent session=<id> transport=<sse-get|http-post-piggyback>` for future gap debugging without guesswork. Two new tests in `tests/test_mcp_server.py` — one pinning the SSE response shape (both events in order + new session header), one pinning that known-session POSTs stay JSON. CLAUDE.md's "Live MCP tool-surface propagation" section now names the piggyback as a fourth mechanism alongside initialize advertisement, on-connect push, and broadcast. Full suite: 3877 passes.
- **`queen_view_messages` + `queen_view_message_stream` gain `full=true` for verbatim relay (task #237).** Direct follow-up to #235: the auto-relay prompt fired into the Queen's PTY on inbound messages points her at `queen_view_messages worker=queen` for the full content, but that tool truncated each body at 160 characters for list-view ergonomics. Operator repro on 2026-04-21: project-root sent the Queen a 2 kB decision memo (Option A / Option B / recommendation) and the Queen couldn't read past the Option A header via the view tool. Added a `full` boolean to both tools' input schema (default false) — when true, returns the complete message body and separates multi-row results with `\n\n---\n\n` so boundaries are unambiguous. Default preview behaviour unchanged. `_handle_view_message_stream` grew past the complexity cap as a side effect, so the row-formatting loop was extracted to `_render_message_stream_rows` + `_message_stream_worker_states` helpers. Two new tests pin that default is still truncated and `full=true` returns the complete body for both tools; CLAUDE.md's "Queen message-surface elevation" section names the new flag. Full suite: 3875 passes.
- **Pressure oscillation dampening + measured-value logging + stuck-BUZZING safety net (task #236).** Three coordinated fixes around the hub + realtruth observation: 10–13 rapid SUSPENDED/RESUMED cycles during a single npm install + deploy turn, followed by both workers wedged in BUZZING for 97–113 minutes after actual work ended. (1) **Hysteresis in `PressureManager.on_pressure_changed`.** New `_HYSTERESIS_SECONDS = 30.0` constant and `_last_resume_at` timestamp suppress re-entry into HIGH/CRITICAL for 30 s after any RESUME. Memory-pressure jitter around a threshold boundary no longer produces 10+ SUSPEND/RESUME cycles per turn. The `_last_resume_at` is primed even when a HIGH pressure wave found no SLEEPING workers to suspend, so the next-tick HIGH is still debounced. (2) **Measured mem/swap values in SUSPEND/RESUMED buzz entries.** `on_pressure_changed` now accepts `mem_pct` / `swap_pct` kwargs threaded from `ResourceMonitor`; `_suspend_workers` and `_resume_pressure_suspended` append them to the log detail (e.g. `pressure HIGH (mem=92% swap=55%)`). Future tuning has concrete data alongside each event. (3) **Stuck-BUZZING safety net in the state tracker.** New `_STUCK_BUZZING_THRESHOLD = 600 s` guard plus `_has_active_turn_signal()` helper — if the classifier calls BUZZING, the worker has been BUZZING for 10+ minutes, AND the narrow PTY tail has NONE of the active-turn signals (esc-to-interrupt, monitor, subagent spinner), force the classification back to RESTING. Catches the stuck-BUZZING mode where stale scrollback patterns (recently-completed subagent `↓ N tokens` lines) keep matching the wide-tail regex even though the worker is idle at the ❯ prompt. The narrow-tail check deliberately rejects stale-scrollback false positives. 9 new tests: 3 hysteresis + measured-value pressure tests, 5 stuck-BUZZING safety-net tests, one threshold-floor guard. Full suite: 3873 passes. Companion to #233 (inverse fix direction; fingerprint-cache race was RESTING-while-BUZZING, this is BUZZING-while-RESTING). Diagnostic-log note from the task description (the STATE_TRANSITION entries from #233 didn't appear in the operator's buzz log) is addressed only by the fact that this release needs a daemon reload — #233's logging was already shipped in 9966305 but hadn't been picked up by the running daemon at observation time.

### Features
- **Queen message auto-pickup + inter-worker nudge drone (task #235).** Three coordinated gaps filled around message-driven coordination. **Phase 1 — Queen inbox auto-relay.** Every `swarm_send_message(to="queen", ...)` (direct or `*` broadcast that includes the Queen) now fires a short PTY notification into the Queen's terminal via `send_to_worker`, so her next conversation turn processes the reply naturally. Self-messages (queen → queen) and worker-to-worker messages do NOT auto-relay — that bypass is intentionally Queen-only to preserve the "workers cannot auto-interrupt each other" hierarchy. Every relay logs as `INBOX_AUTO_RELAY` under `LogCategory.MESSAGE`. **Phase 2 — `queen_view_message_stream` MCP tool.** New Queen-only tool that joins recent messages against each recipient's current worker state. `actionable_only=true` narrows to unread messages whose recipient is idle (RESTING / SLEEPING / STUNG) — the subset the Queen needs to act on. Paired with the raw `queen_view_messages` tool. **Phase 3 — `InterWorkerMessageWatcher` drone.** New drone at `src/swarm/drones/inter_worker_watcher.py` mirroring the `IdleWatcher` pattern from #225. Periodic sweep (reuses `DroneConfig.idle_nudge_interval_seconds` / `idle_nudge_debounce_seconds`, defaults 180 s / 900 s) nudges RESTING / SLEEPING recipients of unread inter-worker messages via a server-side PTY inject; the injector is debounced per recipient and respects the rate-limit callback. Queen-sourced messages are skipped to avoid double-nudging (Phase 1 already covers those). Every nudge logs as `AUTO_NUDGE_MESSAGE` under `LogCategory.DRONE`. Acceptance #4 preserved: workers still cannot prompt each other directly via `swarm_send_message` — the auto-injection is a drone/server concern, never a worker privilege. 18 new tests across `tests/test_mcp_tools.py` (Phase 1 + Phase 2) and `tests/test_inter_worker_watcher.py` (Phase 3). Full suite: 3864 passes. CLAUDE.md gained a "Queen message-surface elevation" section documenting the three elevated privileges and the "workers cannot auto-interrupt" boundary.

### Changes

### Fixes
- **State tracker: pressure RESUME now clears fingerprints; STATE_TRANSITION buzz log (task #233).** Two-part fix for the "worker shows RESTING while demonstrably mid-turn" dashboard bug. (1) `PressureManager._resume_pressure_suspended()` now routes through `state_tracker.wake_worker()` via a new callback instead of discarding from the suspended set directly — this clears the content-fingerprint cache too. Without the clear, a worker whose PTY state changed during suspension (e.g. idle → running a Bash tool) kept its pre-suspend fingerprint, the RESTING short-circuit in `_poll_single_worker` kept short-circuiting, and the worker stayed tagged RESTING in the operator dashboard for the whole turn. (2) Every state transition now writes a `STATE_TRANSITION` buzz entry (new `SystemAction` enum value) with metadata: `from`, `to`, `esc_to_interrupt` (was the indicator present in the PTY tail?), `pty_delta_bytes`, `unchanged_streak`, `suspended`. Future mis-classifications leave a diagnostic trail instead of requiring a live operator to catch them. Three new tests: pressure resume routes through `wake_worker` callback, legacy fallback still empties the suspended set, and `_handle_state_change` emits the STATE_TRANSITION entry with the expected metadata shape. Full suite: 3846 passes.

## [2026.4.21.3] - 2026-04-21

### Features

### Changes

### Fixes
- **Holder backpressure threshold raised to 8 MB — root cause of the long-standing "terminal locks after reload, needs 2-3 reloads" bug.** Traced via `[term-trace]` logs collected across several reload events: every post-reload log ended with `dropping slow client (buffer 1178874 bytes)` from `swarm.pty.holder`, followed by 2+ minutes of zero PTY output across every worker (despite all of them being RESTING with live Claude Code sessions). The chain: (1) daemon reloads, new daemon connects to the holder, (2) `ProcessPool.discover()` fires `_send_cmd("snapshot", worker=X)` per worker, (3) holder writes the ~1.3 MB reply (1 MB raw ring buffer × ~1.33 base64 overhead) into the client socket buffer, (4) while the reply is still draining, `_broadcast` fires on a PTY readable event and writes more bytes into the SAME pending buffer, (5) `get_write_buffer_size()` returns ~1.18 MB, exceeds the old `_MAX_WRITE_BUFFER = 1 MB` threshold, and the holder drops the daemon as a "slow client". The daemon's UNIX socket to the holder is killed, no more live PTY output reaches the daemon, every worker's ring buffer freezes at the snapshot — dashboard terminals appear locked and the state tracker classifies every worker as RESTING because the stale content looks idle. The threshold is now 8 MB (6x headroom over a single snapshot reply while still catching genuinely stuck clients; tens of seconds of backlog at typical PTY output rates). Two new tests in `tests/test_holder.py` pin the positive path (1.5 MB mid-drain buffer ≠ slow client) and the negative (8 MB+ still drops). Full suite: 3843 passes.
- **MCP session auto-revive on unknown `Mcp-Session-Id` (task #227).** Replaces the 404-on-unknown-session behaviour shipped in the previous release. The 404 path was spec-correct per MCP Streamable HTTP §8.4 but broke Claude Code in the wild: its HTTP MCP transport didn't recover from 404 — it just kept re-sending the dead session ID, every tool call failed, and the Queen plus all workers went fully isolated after a daemon reload. The handler now auto-revives instead: when a POST arrives with a non-empty `Mcp-Session-Id` the new daemon process doesn't recognise, the server mints a new session ID on the fly, binds the incoming request to it, processes the original call, returns the new ID in the response header, and pushes `tools/list_changed` to any open `GET /mcp` stream so cached tool schemas get refreshed. `initialize` still issues its own fresh session; session-less clients (no header) still pass through unchanged; `DELETE /mcp` still terminates, but a follow-up on the terminated ID is now auto-revived rather than rejected. The server self-heals regardless of whether the client honours reconnect contracts. Seven tests in `tests/test_mcp_server.py` pin the positive path, reuse-after-revive, initialize-with-stale-ID, missing-header passthrough, DELETE-then-revive, and the auto-revive → `tools/list_changed` push. Full suite: 3841 passes. CLAUDE.md's "Live MCP tool-surface propagation" section rewritten to document auto-revive and explicitly call out why the earlier 404-based and listChanged-based attempts missed.
- **MCP session-ID invalidation on daemon reload — the load-bearing fix for stale tool schemas.** Third attempt at making MCP tool-surface changes propagate to running workers. The previous two attempts (advertising `capabilities.tools.listChanged: true` on initialize, and pushing `tools/list_changed` on SSE connect / to active subscribers) all relied on the client *voluntarily* re-enumerating. They didn't stick because Claude Code's HTTP MCP transport kept reusing its pre-restart `Mcp-Session-Id`, the server happily accepted it (we never validated), and so the client never saw its session break — no break signal, no re-initialize, no fresh `tools/list`. This commit closes that loophole per MCP Streamable HTTP spec §8.4: `handle_streamable_http` now tracks issued session IDs in `_active_session_ids` (wiped automatically on `os.execv`) and returns **404 + `session_not_found`** to any POST carrying an unknown non-empty `Mcp-Session-Id` (except `initialize`, which is always allowed). Per spec, Claude Code MUST then start a new session by sending a fresh `InitializeRequest` — which runs through the existing `listChanged` advertisement + `tools/list_changed` SSE push, triggering a `tools/list` re-fetch. `DELETE /mcp` now correctly deregisters the session. Session-less clients (no `Mcp-Session-Id` header) remain accepted for backward compat. Five new tests in `tests/test_mcp_server.py` cover the positive path, 404 on unknown session, initialize-always-allowed with stale ID, missing-session passthrough, and DELETE → 404. Full suite: 3839 passes. CLAUDE.md's "Live MCP tool-surface propagation" section rewritten to document the real load-bearing mechanism and call out why the earlier attempts missed.

## [2026.4.21.2] - 2026-04-21

### Features
- **Live MCP tool-surface propagation (task #226).** The MCP server now exposes `swarm.mcp.server.broadcast_tools_list_changed()` — an async function that pushes `notifications/tools/list_changed` to every currently-subscribed SSE session, both the Streamable HTTP GET `/mcp` stream and the legacy GET `/mcp/sse` stream. Complements the existing "push on connect" behaviour (unchanged): that covers clients reconnecting after a daemon reload, this covers clients that stayed connected while the tool surface changed. `SwarmDaemon.start()` calls it defensively at startup; future hot-reload-of-tools paths should call it whenever they mutate the registry. Also fixes a latent bug where the streamable SSE handler's request-content iterator would EOF on a body-less GET and exit the handler early; replaced with a transport-disconnect poll so the handler actually stays open for the lifetime of the client's stream. Four new tests in `tests/test_mcp_server.py` cover broadcast-to-open-session, no-op-when-empty, dead-subscriber pruning, and reconnect-after-bounce. CLAUDE.md gained a "Live MCP tool-surface propagation" section pointing future authors at the broadcast API.

### Changes

### Fixes

## [2026.4.21] - 2026-04-21

### Features
- **Autonomous worker momentum (task #225).** Workers no longer park on newly assigned tasks waiting to be polled — Swarm now *pushes* work in three coordinated ways:
  - **Phase 1: task-push dispatch on assignment.** `swarm_create_task(target_worker=X)` routes through `daemon.assign_and_start_task()` by default, which injects the task description straight into X's PTY within one poll cycle. Previously the handler only called `assign_task`, leaving the task queued in ASSIGNED status with nothing dispatching it — that's the root of the recurring "5 workers with hours-old in_progress tasks" operator-pain pattern. New `start: bool` argument on the MCP tool (default `true`) preserves queue-only behavior for Queen/operator staging flows (`start=false`). Self-targeted tasks (caller == target) never dispatch — no interleaving with the caller's own turn.
  - **Phase 2: idle-watcher drone (`drones/idle_watcher.py`).** Periodic sweep (`DroneConfig.idle_nudge_interval_seconds`, default 180 s) nudges RESTING / SLEEPING workers that have an ASSIGNED / IN_PROGRESS task but aren't moving on it. Nudge message points the worker at `swarm_task_status filter=mine` + `swarm_check_messages` so it can self-diagnose rather than treating the nudge as a fresh prompt. Per-(worker, task) debounce (default 900 s) prevents spam; new `AUTO_NUDGE` action in `DroneAction`/`SystemAction` makes every auto-prompt auditable in the buzz log. Rate-limited workers are skipped so we don't stack work behind a dead Claude quota.
  - **Phase 3: post-ship self-loop.** `daemon.complete_task()` now fires `start_task()` for the next ASSIGNED task belonging to the same worker (lowest task number first) as soon as the current one ships. IN_PROGRESS follow-ups are skipped — they're already running somewhere else. Empty queues get no follow-up, per spec ("skip if the worker has nothing else assigned, avoid pointless loops").
  - 19 new tests in `tests/test_idle_watcher.py`, `tests/test_mcp_tools.py::TestCreateTaskAutoDispatch`, and `tests/test_daemon.py` (post-ship auto-start). Full suite: 3828 tests pass. CLAUDE.md gained a new "Autonomous task momentum" section documenting the push semantics for future operators.

### Changes

### Fixes
- **Post-restart terminal reload race — output dropped during discovery window.** When the daemon `os.execv`s (the dashboard Reload button's happy path), `ProcessPool.connect()` starts the holder read loop immediately — but the worker map (`_workers`) is still empty and only gets populated one worker at a time by `discover()`, which does a separate snapshot roundtrip per worker. For the ~1–3 seconds that took, any live PTY output the holder broadcast for a not-yet-discovered worker was silently dropped in `_dispatch_message`. That's the race behind the long-standing "type in the terminal, nothing shows, a second Reload fixes it" bug: the worker's local ring buffer was missing a chunk, which sometimes truncated ANSI escape sequences and left the xterm in a glitched state. The fix buffers unknown-worker output into `_pending_output` and relies on the read loop's serial ordering: any chunks already buffered when the snapshot response resolves are pre-snapshot (already inside the snapshot bytes, dropped to avoid duplication); anything that arrives after resolution routes directly to the now-registered `WorkerProcess.feed_output`. Two new tests in `tests/test_pool.py` lock both paths in. Diagnostic `[term-trace]` logging added in the same session stays put until the reload flow has been stable through several restarts.
- **Operator bypass for the PreToolUse approval hook.** `src/swarm/hooks/approval_hook.sh` now honors a `SWARM_OPERATOR=1` escape hatch alongside the existing `SWARM_MANAGED=1` guard — the PTY holder exports `SWARM_MANAGED=1` for *every* worker it spawns, including sessions the operator is driving interactively, so the old "operator's own session is never gated" invariant was unreachable without a second marker. Operators who want a worker session to bypass drone approval rules (e.g. running `/ship` from an attached worker) now set `export SWARM_OPERATOR=1` in that session and the hook exits early before contacting the daemon. The comment at the top of the script was rewritten to describe this boundary accurately. Pinned by three new tests in `tests/test_approval_hook_script.py` that exercise the shell script against a counting HTTP stub (task #211).

## [2026.4.20] - 2026-04-20

### Features

- **The interactive Queen, foundation pass.** The Queen now spawns as a PTY-managed worker with `kind="queen"`, and task assignment, SLEEPING and broadcast helpers all skip her. A new `queen_chat_store` plus a v6 schema migration back threads, messages and learnings with 30-day retention; `/api/queen/health` and a `queen.health` WebSocket event report her state; chat API routes list, create, post to and resolve threads; and she gets a dedicated sidebar card with a bee icon and state-aware honey-lavender styling, visually distinct from the worker list. Her first-pass system prompt lives at `~/.swarm/queen/workdir/CLAUDE.md`.

- **Queen MCP tooling across read, conversation and write surfaces.** Read-only: `queen_view_worker_state`, `queen_view_task_board`, `queen_view_messages`, `queen_view_buzz_log`, `queen_view_drone_actions`, `queen_query_learnings`. Conversation: `queen_post_thread`, `queen_reply`, `queen_update_thread`, `queen_save_learning`. Write-side: `queen_reassign_task`, `queen_interrupt_worker`, `queen_force_complete_task`, and `queen_prompt_worker`, which injects prompts directly into worker PTYs (Claude queues BUZZING targets to the next turn). Every `queen_*` handler is gated on `worker_name == "queen"`.

- **A rebuilt Usage tab.** Usage got its own tab with a time-window filter (24h, 7d, 30d, last month, this month, all), worker-name search, a minimum-cost threshold and a hide-zero toggle, and errors now surface inline instead of leaving the panel blank.

### Changes

- **The C901 complexity threshold moved 10 → 12** in `pyproject.toml` with a rationale comment, and one pre-existing `# noqa: C901` was removed.

### Fixes

- **The PTY terminal showed a stale frame after a daemon reload — a day-1 bug.** The browser xterm instance is now reset before the first post-reconnect frame, so pre-reload content no longer overlays the holder's replay snapshot. No more 1-3 reload retries to get a clean terminal.

- **`swarm_send_message` wildcard broadcasts silently dropped messages.** A single `recipient='*'` row was first-reader-wins; a new `MessageStore.broadcast()` fans out one row per recipient, with the handler and HTTP route updated and the recipient count surfaced in the response.

- **Three usage and state-detection defects.** Claude's monitor-in-background was misclassified as RESTING — the provider now recognises "N monitor still running" / "auto mode on · N monitor" in the tail and returns BUZZING. Usage blanked on every daemon restart because `find_active_session` filtered by `mtime >= start_time`; that filter is gone. And Queen usage was double-counted — `/api/usage` now skips her in the worker loop, since the PTY Queen is the authoritative source. Her `project_dir` encoding also replaces `.` as well as `/`, matching Claude Code.

## [2026.4.19] - 2026-04-19

### Features
- **MCP `tools/list_changed` push on SSE connect.** The MCP server now advertises the `tools.listChanged` capability on initialize and, the moment a client opens the streamable SSE stream (GET `/mcp`) or the legacy SSE stream (GET `/mcp/sse`), pushes a `notifications/tools/list_changed` JSON-RPC message. Conformant MCP clients react by re-calling `tools/list`, so schemas cached from a pre-reload daemon no longer linger on the client side. Closes the gap exposed by task #169 — the fix had landed server-side but worker/host sessions kept the stale tool schema in their local cache because nothing told them to refresh. Legacy SSE's required first event (the `endpoint` URL) is preserved; the refresh notification is the second event. Four new integration tests in `tests/test_mcp_server.py` pin the behaviour.

### Changes

### Fixes

## [2026.4.18.3] - 2026-04-18

### Features
- **MCP tool schema-drift indicator.** `src/swarm/mcp/tools.py` hashes itself at import time; `tools_source_drift()` compares the frozen hash against the current file contents. The dev-mode dashboard footer polls `/api/health` every 30s (new `mcp_schema_drift` field) and highlights the Reload button in honey with "Reload needed (MCP tools edited)" status when the source has changed since daemon start. Standalone `GET /api/mcp/schema-drift` endpoint returns the full `{drift, source_path, startup_hash, current_hash}` payload for external tooling. Surfaces the exact scenario that hid task #169's fix in the running daemon until someone noticed the call still used the legacy code path.
- **Reload button on the config page header.** The dashboard footer Reload button is hidden on mobile, so the same dev-reload flow (POST `/api/server/restart`, poll `/api/health` until the daemon comes back, refresh the page) is now reachable from the config page header. Only rendered when `is_dev` is True.

### Changes

### Fixes

## [2026.4.18.2] - 2026-04-18

### Features

### Changes
- **Queen banners de-dup per worker, not per text.** The dashboard's queen/escalation banners now key dedup off a `data-worker` attribute instead of string-comparing `textContent`, so two banners for the same worker with different copy don't pile up. Selecting a worker in the sidebar now also removes any lingering banners tied to that worker — the operator is addressing it directly, the banner no longer adds signal.

### Fixes
- **`swarm_complete_task` silently closed the wrong task when a worker had multiple in_progress assignments (task #169).** The handler walked `task_board.all_tasks` and closed the first match for the calling worker, arbitrarily picking one task and attaching the caller's resolution to it. The MCP tool now takes an optional `number` parameter: singular active task + no `number` keeps the legacy behaviour, multiple active tasks + no `number` errors with the candidate list instead of guessing, and an explicit `number` validates ownership + status before closing. Seven regression tests pin the new contract.
- **Swarm's own MCP tools (`mcp__swarm__*`) could stall behind a PreToolUse permission prompt.** The hook handler (`routes/hooks.py`) now short-circuits to `approve` for any tool name starting with `mcp__swarm__` — these are the daemon's own coordination primitives (`swarm_check_messages`, `swarm_complete_task`, `swarm_task_status`, …) and gating them behind operator approval could leave a worker waiting indefinitely on something that's definitionally safe. Non-swarm MCP tools (e.g. `mcp__stripe__*`) still flow through the normal rules engine.

## [2026.4.18] - 2026-04-18

### Features

### Changes

### Fixes

- **`swarm_task_status` pagination and ordering overhaul (#142)**, bundled into this release from commit `a730919`. Workers can now see their newer task assignments and look up any task by number. The release commit itself carries only the version bump; the detail is in the bundled commit.

## [2026.4.17.2] - 2026-04-17

### Features
- **Dashboard "Awaiting your input" pill on worker tiles.** When a worker sits in WAITING state past a 15-second grace window, the tile now shows a pulsing amber pill to make operator-action-required cases visually distinct from a plain WAITING badge. Drives off a new `Worker.needs_operator_input` property exposed via the workers API. Fixes the common confusion where a worker presenting an `AskUserQuestion` prompt looked indistinguishable from a stalled/silent worker.

### Fixes
- **Cross-project task attribution on MCP `swarm_create_task`.** When a worker called `swarm_create_task` with `target_worker=X`, the resulting task row landed in the DB with `source_worker=""` — the calling worker's identity was lost. The handler now calls `edit_task` to record `source_worker` (the calling worker) alongside `target_worker` before assigning, so `is_cross_project` lineage is preserved end-to-end. Self-targeted tasks skip the edit to avoid spurious cross-project flags.

## [2026.4.17] - 2026-04-17

### Features
- **`swarm_batch` MCP tool** — ninth coordination tool; runs multiple `swarm_*` ops sequentially in one round-trip so a worker no longer pays N round-trips for N related calls. Nested `swarm_batch` is rejected to prevent runaway recursion. Each op is still buzz-logged individually.
- **Richer MCP tool descriptions** — every `swarm_*` tool now carries a ≥150-char description with trigger hints ("when to call"), enum semantics (e.g. `finding` vs `warning` vs `dependency` vs `status`), and concrete `examples` in the input schema.
- **`swarm analyze-tools` CLI** — aggregates MCP tool usage from the buzz log (`mcp:*` entries) into per-tool stats: calls, errors, active workers, and up to five distinct error snippets per tool. Supports `--since=7d`, `--json` output, and `--db PATH` for offline DB analysis.
- **Approval-rate gauge** — `SystemLog.approval_rate(since=...)` returns `{approvals, escalations, rate}` from recent decisions; new `GET /api/drones/approval-rate?hours=N` endpoint; dashboard header badge shows the percentage over the last 24h.
- **`DroneDecision.confidence`** — optional float field so future LLM-classifier rules can slot in next to the existing rule-based decisions without a schema change.
- **Compact event telemetry** — every `/compact` logs a `SystemAction.COMPACT` entry under new `LogCategory.COMPACT` with `{tokens_before, tokens_after, ratio, trigger}` metadata. Makes compaction effectiveness measurable per worker and per run.
- **Cron-format pipeline schedules** — pipeline steps now accept full 5-field cron expressions (e.g. `"30 14 * * 1-5"` for weekdays at 14:30). Legacy `HH:MM`, `*:MM`, and `HH:*` still work and are translated to cron internally. Adds `croniter` as a dependency.
- **Skills registry** — SQLite-backed skills table (schema v5 migration, idempotent `CREATE TABLE IF NOT EXISTS`). `SkillsStore` CRUD + usage counters; `attach_skills_store()` seeds built-in defaults (`/fix-and-ship`, `/feature`, `/verify`) on first boot. New `GET /api/skills` endpoint. `get_skill_command()` consults the registry before falling back to the in-memory map and increments `usage_count` on each lookup.
- **`claude_code_security` service handler** — new pipeline AUTOMATED step that runs `claude code security scan --json`, parses the findings array, maps severity to Swarm task priority (`critical→urgent`, `high→high`, `medium→normal`, `low/info→low`), and deduplicates against a persistent state file fingerprinted by `sha256(rule_id\x00path\x00line)`. Supports `severity_filter`, configurable command, and custom dedup state path.
- **Test harness infra pinning** — every `swarm test` run captures an `InfraSnapshot` (model, provider, worker_count, port, claude_home, swarm_version, python_version, platform, env_hash, env_keys) and writes it as the first line of `test-run-{id}.jsonl`. The Markdown report gains an "Infrastructure Snapshot" section above the summary. New `swarm test --pin-model=<id>` flag records the model identifier explicitly, and `compute_env_hash` fingerprints tracked env vars (CLAUDE_MODEL, SWARM_PROVIDER, etc.) via SHA-256 so infra drift is debuggable without leaking secrets.
- **Opt-in Claude Code sandbox** — new `sandbox:` config block on `HiveConfig` (`{enabled, min_claude_version, settings_overrides}`). When enabled, `hooks.install.install()` calls `claude --version`, verifies the installed CC version meets `min_claude_version`, and merges `settings_overrides` into `~/.claude/settings.json["sandbox"]`. Unsupported or missing versions silently stay on the legacy approval flow. Disabled by default; no behaviour change for existing installs.
- **In-app feedback** — report bugs, feature requests, and questions directly from the dashboard footer. Submissions go through the GitHub CLI (`gh`) to bypass URL length limits, with a preview-and-edit step before the issue is filed. Sensitive paths and config values are auto-redacted.
- **Resource monitoring** — memory, swap, and load tracked on a 30s tick; workers auto-suspend on HIGH pressure and the operator is paged on CRITICAL. D-state (wedged process) scanning is optional.
- **Jira integration** — two-way sync with Jira Cloud over OAuth 2.0 (3LO). Import issues as tasks, push status and completion comments back, create Jira issues from the task board.
- **Email integration** — Microsoft Graph (Outlook) integration: drop `.eml`/`.msg` onto the task board, fetch emails from the dashboard, and draft a reply in the Drafts folder when a task completes (never auto-sent).
- **MCP server** — HTTP-based MCP server at `/mcp` (Streamable HTTP + legacy SSE). Workers get 9 coordination tools: `swarm_check_messages`, `swarm_send_message`, `swarm_task_status`, `swarm_create_task`, `swarm_complete_task`, `swarm_report_progress`, `swarm_claim_file`, `swarm_get_learnings`, `swarm_batch`.
- **Inter-worker messages** — typed messages (finding, warning, dependency, status, operator) delivered via MCP; dedup + rate-limit per `(sender, recipient, type)` pair.
- **Pipelines** — multi-step workflows combining AGENT, AUTOMATED, and HUMAN steps with per-step dependencies, templates, and start/pause/resume lifecycle. State persisted in SQLite.
- **Queen oversight** — proactive monitoring: prolonged-buzzing detection and task-drift analysis; interventions classified by severity (minor note, pause+redirect, escalate to operator).
- **File ownership & coordination** — single-branch mode (default) with Queen-managed file ownership map; warning or hard-block on overlap; worktree escape hatch when scopes are unavoidable.
- **Auto-pull sync** — workers auto-pull when another worker commits on the shared branch.
- **Multi-provider support** — Claude Code (production), Gemini CLI and Codex CLI (experimental), plus custom providers via `custom_llms` and per-provider overrides.
- **Cloudflare Tunnel** — one-click remote HTTPS access from the dashboard toolbar; optional named-domain configuration via `tunnel_domain`.
- **Dashboard push notifications** — browser push + desktop notifications + terminal bell; persistent Buzz Log history.
- **Interactive terminal attach** — full xterm.js PTY bridge over WebSocket, up to 20 concurrent sessions.
- **PWA** — installable app with service-worker offline shell and badge API for pending proposals.
- **Config editor in the dashboard** — tabbed UI for workers, groups, drones, Queen, workflows, and integrations; changes apply immediately.
- **Drone log & tuning analytics** — per-rule hit stats and AI-suggested approval rule patterns.
- **Speculation (experimental)** — preparatory read-only work on a queued task while a worker is RESTING.
- **Swarm CLI: `swarm db`** — `stats`, `export`, `prune`, `backup`, `check` for inspecting and maintaining the unified SQLite store.
- **Swarm CLI: `swarm test`** — supervised end-to-end orchestration test against a dedicated port with an AI-generated report.
- **Claude Code hook integration** — PreToolUse (drone-based approval), SessionEnd (immediate STUNG detection), and event hooks (SubagentStart/Stop, PreCompact/PostCompact) installed automatically by `swarm init`.

### Changes
- **Unified SQLite storage** — tasks, task history, proposals, messages, pipelines, buzz log, queen sessions, secrets, and config itself all live in `~/.swarm/swarm.db` (WAL mode). The legacy YAML is treated as a seed/import format; the database is the runtime source of truth after first run.
- **Jira auth is OAuth-only** — token auth was removed in favor of Atlassian OAuth 2.0 (3LO).
- **Config mutations are immediate** — dashboard edits write straight to the DB and hot-apply in the same request.
- **Calendar versioning** — version now tracks release date (`YYYY.M.D.patch`) rather than semver; the v1.0.0 section below is preserved for history.

### Fixes
- Numerous fixes to feedback submission (live `HiveConfig` serialization, `gh` CLI fallback for 8 KB URL limits, preview/edit gate before submission).
- See `git log` for the full per-commit history.

---

## v1.0.0

Initial release of Swarm — a hive-mind orchestrator for Claude Code agents.

### Features
- **Web Dashboard** — Browser-based dashboard with real-time WebSocket updates, inline terminal, and full task management
- **Worker Management** — Launch, kill, revive, and monitor Claude Code agents running in managed PTYs
- **Task Board** — Create, assign, complete, and track tasks with priority, tags, dependencies, and file attachments
- **Drones** — Background automation: auto-continue idle workers, auto-approve prompts, escalate stuck agents
- **Queen** — Headless Claude conductor for hive-wide coordination and per-worker analysis
- **Groups** — Organize workers into named groups for targeted broadcasts and management
- **Config** — YAML-based configuration with live-reload and web-based config editor
- **Notifications** — Browser notifications, terminal bell, and persistent Buzz Log
- **Task History** — Audit log tracking full task lifecycle events
- **Themed UI** — Warm beehive color palette, responsive layout, keyboard shortcuts
