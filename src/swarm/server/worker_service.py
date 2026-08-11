"""WorkerService — worker CRUD, I/O operations, and lifecycle management."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from swarm.drones.log import DroneAction, DroneLog, LogCategory
from swarm.logging import get_logger
from swarm.pty.process import ProcessError
from swarm.server.helpers import truncate_preview
from swarm.tasks.board import TaskBoard
from swarm.worker.worker import Worker, WorkerState

if TYPE_CHECKING:
    from swarm.config import HiveConfig, WorkerConfig
    from swarm.drones.pilot import DronePilot
    from swarm.pty.provider import WorkerProcessProvider

_log = get_logger("server.worker_service")

# --- Operator-kill graceful-shutdown budget (~3s worst case) ---
# Deliberately short. Kill is an interactive action: an operator who clicked it
# wants the worker gone, and a long wait reads as the click not working — which
# is what makes people click again. The force-kill backstop runs regardless, so
# these bound politeness, not correctness.
_KILL_INTERRUPT_DELAY = 0.15  # after Esc, before the quit command
_KILL_QUIT_TIMEOUT = 2.0  # wait for the agent to exit after its quit command
_KILL_POLL_INTERVAL = 0.1  # how often to check whether it has gone
_KILL_SHELL_EXIT_DELAY = 0.5  # after `exit`, before signalling the process


def _remembered_states(loader: Callable[[], dict[str, Any]] | None) -> dict[str, Any]:
    """Last-known worker states, or {} when unavailable.

    Defensive because worker adoption runs on paths where no loader is wired (several
    fixtures build a partial service), and a missing one must cost the head start, not
    the roster.
    """
    if loader is None:
        return {}
    try:
        return loader()
    except Exception:
        _log.debug("could not load remembered worker states", exc_info=True)
        return {}


def _restore_state(worker: Worker, remembered: dict[str, Any]) -> None:
    """Apply a remembered state to a freshly adopted worker.

    STUNG is never restored. A worker that had crashed before the restart may well have
    been revived BY that restart, and showing a dead worker as dead when it is alive is
    the same class of error as showing an idle worker as busy — just in the other
    direction. The pilot settles it within one poll either way.
    """
    name = getattr(worker, "name", "")
    from swarm.db.worker_state_store import RememberedState

    entry = RememberedState.coerce(remembered.get(name))
    saved = entry.state if entry is not None else ""
    if not saved or saved == WorkerState.STUNG.value:
        return
    try:
        worker.state = WorkerState(saved)
        # SLEEPING is derived from how long the worker has been RESTING, and the
        # operator's "put to sleep" works by backdating this very field. Restoring the
        # state without it silently downgrades every slept worker to plain RESTING —
        # reported after the first version shipped ("some to sleep ... only
        # public-website was resting"). Guarded against a future timestamp, which would
        # make state_duration negative.
        since = entry.since
        if isinstance(since, int | float) and 0 < since <= time.time():
            worker.state_since = float(since)
    except ValueError:
        # An unknown value means the enum changed under a stored map. Leave the default.
        _log.debug("ignoring unrecognised remembered state %r for %s", saved, name)


def _infer_provider_from_name(name: str) -> str:
    """Infer provider from worker name suffix (e.g., foo-codex)."""
    n = name.lower()
    for prov in ("codex", "gemini", "claude"):
        if n.endswith(f"-{prov}"):
            return prov
    return ""


class WorkerService:
    """Manages worker CRUD, process I/O, and lifecycle."""

    def __init__(
        self,
        broadcast_ws: Callable[[dict[str, Any]], None],
        drone_log: DroneLog,
        task_board: TaskBoard,
        get_pilot: Callable[[], DronePilot | None],
        get_pool: Callable[[], WorkerProcessProvider | None],
        get_config: Callable[[], HiveConfig],
        get_workers: Callable[[], list[Worker]],
        set_workers: Callable[[list[Worker]], None],
        worker_lock: asyncio.Lock,
        init_pilot: Callable[[bool], None],
        write_identity: Callable[[WorkerConfig, str], None],
        # #1357. A CALLABLE like every other dependency here, and defaulted because
        # several fixtures build a WorkerService for flows that never restore state.
        load_worker_states: Callable[[], dict[str, Any]] | None = None,
        save_worker_states: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._broadcast_ws = broadcast_ws
        self._drone_log = drone_log
        self._task_board = task_board
        self._get_pilot = get_pilot
        self._get_pool = get_pool
        self._get_config = get_config
        self._load_worker_states = load_worker_states
        self._save_worker_states = save_worker_states
        self._get_workers = get_workers
        self._set_workers = set_workers
        self._worker_lock = worker_lock
        self._init_pilot = init_pilot
        # #1195: required, not optional. Forwarded to the manager spawn
        # helpers, which refuse to start a process without it.
        self._write_identity = write_identity
        # Per-worker write locks to serialize PTY writes
        self._pty_locks: dict[str, asyncio.Lock] = {}

    def get_worker(self, name: str) -> Worker | None:
        """Find a worker by name."""
        return next((w for w in self._get_workers() if w.name == name), None)

    def require_worker(self, name: str) -> Worker:
        """Get worker by name or raise WorkerNotFoundError."""
        from swarm.server.daemon import WorkerNotFoundError

        worker = self.get_worker(name)
        if not worker:
            raise WorkerNotFoundError(f"Worker '{name}' not found")
        return worker

    @staticmethod
    def _require_process(worker: Worker) -> None:
        """Raise ProcessError if the worker has no attached process."""
        if not worker.process:
            raise ProcessError(f"Worker '{worker.name}' has no attached process")

    def _record_override(self, worker_name: str, override_type: str, detail: str) -> None:
        """Record a user override against the most recent drone decision."""
        store = self._drone_log.store
        if store is None:
            return
        from swarm.drones.tuning import OverrideType, record_override

        try:
            otype = OverrideType(override_type)
        except ValueError:
            return
        record_override(store, worker_name=worker_name, override_type=otype, detail=detail)

    def update_worker(
        self, current_name: str, *, name: str | None = None, path: str | None = None
    ) -> None:
        """Update a worker's name and/or path.

        Raises WorkerNotFoundError if the worker doesn't exist.
        Raises ValueError if the new name is malformed (handle_errors → 400).
        Raises SwarmOperationError if the name is already taken
        (handle_errors → 409 Conflict).
        """
        from swarm.server.daemon import SwarmOperationError
        from swarm.server.helpers import validate_worker_name

        worker = self.require_worker(current_name)

        # Determine what actually changes
        new_name = name if name and name != worker.name else None
        new_path = path if path and path != worker.path else None

        if not new_name and not new_path:
            return  # nothing to do

        if new_name:
            if err := validate_worker_name(new_name):
                # Bad input → 400.  Pre-Phase-C this raised SwarmOperationError
                # (which mapped to 400 then but would map to 409 now).
                raise ValueError(f"Invalid worker name: {err}")
            others = (w for w in self._get_workers() if w is not worker)
            if any(w.name.lower() == new_name.lower() for w in others):
                # State conflict (another worker holds this name) → 409.
                raise SwarmOperationError(f"Worker '{new_name}' already exists")

        old_name = worker.name

        if new_name:
            worker.name = new_name
        if new_path:
            worker.path = new_path

        worker._api_dict_cache = None

        # Reassign tasks from old name to new name
        if new_name:
            self._task_board.reassign_worker(old_name, new_name)
            pilot = self._get_pilot()
            if pilot:
                pilot.workers = self._get_workers()

        self._broadcast_ws({"type": "workers_changed"})

    def reorder_workers(self, order: list[str]) -> None:
        """Reorder workers to match the given name order.

        Workers not in *order* are appended at the end.
        """
        workers = self._get_workers()
        by_name = {w.name: w for w in workers}
        reordered: list[Worker] = []
        for name in order:
            if name in by_name:
                reordered.append(by_name.pop(name))
        # Append any workers not mentioned (e.g. newly added)
        reordered.extend(by_name.values())
        self._set_workers(reordered)
        self._broadcast_ws({"type": "workers_changed"})

    # --- Worker I/O operations ---

    def _pty_lock(self, name: str) -> asyncio.Lock:
        """Get or create a per-worker PTY write lock."""
        if name not in self._pty_locks:
            self._pty_locks[name] = asyncio.Lock()
        return self._pty_locks[name]

    async def send_to_worker(
        self,
        name: str,
        message: str,
        *,
        enter: bool = True,
        automated: bool = False,
        _log_operator: bool = True,
    ) -> None:
        """Send text to a worker's process (serialized per-worker).

        ``enter=False`` types the message into the PTY input buffer
        without submitting — used by the Web Share Target flow so the
        operator can add context (or edit the auto-inserted path)
        before hitting Enter themselves. Default stays True to preserve
        the long-standing semantics of `/api/workers/<name>/send`.

        ``automated=True`` marks a message that NO HUMAN chose to send right
        now — a broadcast, a dispatch, an oversight nudge, a proposal. Those are
        held back while a selection prompt is open, because writing into a
        focused question either commits the highlighted option or types the
        message body in as free text, and the answer is then attributed to the
        operator. The default is False so the operator's own dashboard send and
        the terminal bridge keep working while a prompt is up — the operator is
        precisely the human the prompt is waiting for, and a guard that blocked
        them would make an open question unanswerable.

        THIS FLAG IS THE WHOLE FIX. The guard below is inert unless callers pass
        it, which is why #1451 wired every automated call site in one change
        rather than landing the choke point first.
        """
        worker = self.require_worker(name)
        self._require_process(worker)
        pilot = self._get_pilot()
        if pilot:
            pilot.wake_worker(name)
        async with self._pty_lock(name):
            # The hold-and-flush itself lives in WorkerProcess.send_keys, which is
            # the ONE choke point every write passes through — including the ~13
            # sites that hold a PtyProcess and never reach this method.
            await worker.process.send_keys(message, enter=enter, automated=automated)
        if _log_operator:
            self._drone_log.add(
                DroneAction.OPERATOR, name, "sent message", category=LogCategory.OPERATOR
            )
            self._record_override(name, "redirected_worker", "sent message")

    async def continue_worker(self, name: str) -> None:
        """Send Enter to a worker's process (serialized per-worker)."""
        worker = self.require_worker(name)
        self._require_process(worker)
        pilot = self._get_pilot()
        if pilot:
            pilot.wake_worker(name)
            pilot.mark_operator_continue(name)
        async with self._pty_lock(name):
            await worker.process.send_enter()
        self._drone_log.add(
            DroneAction.OPERATOR, name, "continued (manual)", category=LogCategory.OPERATOR
        )
        self._record_override(name, "approved_after_skip", "continued (manual)")

    async def interrupt_worker(self, name: str) -> None:
        """Send Ctrl-C to a worker's process."""
        worker = self.require_worker(name)
        self._require_process(worker)
        pilot = self._get_pilot()
        if pilot:
            pilot.wake_worker(name)
        async with self._pty_lock(name):
            await worker.process.send_interrupt()
        self._drone_log.add(
            DroneAction.OPERATOR, name, "interrupted (Ctrl-C)", category=LogCategory.OPERATOR
        )
        self._record_override(name, "rejected_approval", "interrupted (Ctrl-C)")

    async def escape_worker(self, name: str) -> None:
        """Send Escape to a worker's process."""
        worker = self.require_worker(name)
        self._require_process(worker)
        pilot = self._get_pilot()
        if pilot:
            pilot.wake_worker(name)
        await worker.process.send_escape()
        self._drone_log.add(
            DroneAction.OPERATOR, name, "sent Escape", category=LogCategory.OPERATOR
        )

    async def arrow_up_worker(self, name: str) -> None:
        """Send Up Arrow to a worker's process."""
        worker = self.require_worker(name)
        self._require_process(worker)
        pilot = self._get_pilot()
        if pilot:
            pilot.wake_worker(name)
        await worker.process.send_arrow_up()

    async def arrow_down_worker(self, name: str) -> None:
        """Send Down Arrow to a worker's process."""
        worker = self.require_worker(name)
        self._require_process(worker)
        pilot = self._get_pilot()
        if pilot:
            pilot.wake_worker(name)
        await worker.process.send_arrow_down()

    async def arrow_right_worker(self, name: str) -> None:
        """Send Right Arrow to a worker's process."""
        worker = self.require_worker(name)
        self._require_process(worker)
        pilot = self._get_pilot()
        if pilot:
            pilot.wake_worker(name)
        await worker.process.send_arrow_right()

    async def arrow_left_worker(self, name: str) -> None:
        """Send Left Arrow to a worker's process."""
        worker = self.require_worker(name)
        self._require_process(worker)
        pilot = self._get_pilot()
        if pilot:
            pilot.wake_worker(name)
        await worker.process.send_arrow_left()

    async def redraw_worker(self, name: str) -> None:
        """Send SIGWINCH to force TUI redraw for a worker."""
        worker = self.require_worker(name)
        self._require_process(worker)
        await worker.process.send_sigwinch()

    async def capture_output(self, name: str, lines: int = 80) -> str:
        """Read a worker's process output buffer."""
        worker = self.require_worker(name)
        self._require_process(worker)
        return worker.process.get_content(lines)

    async def safe_capture_output(self, name: str, lines: int = 80) -> str:
        """Read process output, returning a fallback string on failure."""
        from swarm.server.daemon import WorkerNotFoundError

        try:
            return await self.capture_output(name, lines=lines)
        except (TimeoutError, ProcessError, OSError, WorkerNotFoundError):
            return "(output unavailable)"

    async def discover(self) -> list[Worker]:
        """Discover existing workers via the process pool. Updates daemon.workers."""
        pool = self._get_pool()
        config = self._get_config()
        workers = self._get_workers()
        if pool:
            processes = await pool.discover()
            # Wrap WorkerProcess objects in Worker dataclasses.
            # Match against existing workers to preserve state; create new
            # Worker objects for any processes discovered for the first time.
            from swarm.server.shell_service import is_shell_session
            from swarm.worker.worker import infer_worker_kind

            existing = {w.name: w for w in workers}
            # Read ONCE per rebuild, not per worker: a cold start adopts every process
            # in the same pass.
            remembered = _remembered_states(self._load_worker_states)
            new_workers: list[Worker] = []
            # #1357 diagnostic. The first diagnostic proved the restore did not take
            # effect (all sixteen workers read was=BUZZING at first classification) but
            # not WHY, and there are three candidate explanations that look identical
            # from the outside: nothing was loaded, every worker took the `existing`
            # branch so _restore_state was never reached, or it was restored and
            # something overwrote it. Counting each separately tells them apart in one
            # restart. WARNING because the operator runs at the default level.
            _adopted_existing = 0
            _adopted_new = 0
            _restored = 0
            for proc in processes:
                # Operator shells share the pool's flat namespace with real
                # workers but are NOT workers. Adopting one here would put a
                # bash prompt in the sidebar, eligible for task assignment and
                # drone polling — and a task handed to bash is lost silently.
                # See swarm.server.shell_service.
                #
                # Configuration wins over the prefix. The prefix has to use the
                # worker-name charset (the holder rejects anything else), so it
                # can no longer be collision-proof by construction — and a false
                # positive here is the worst kind: the worker is silently absent
                # from the roster, which reports as nothing at all.
                if is_shell_session(proc.name) and config.get_worker(proc.name) is None:
                    continue
                if proc.name in existing:
                    _adopted_existing += 1
                    w = existing[proc.name]
                    w.process = proc
                    # Kind is a property of the name, so discover/restart
                    # must keep it in sync with the name convention.
                    w.kind = infer_worker_kind(proc.name)
                    wc = config.get_worker(proc.name)
                    if wc and wc.provider:
                        w.provider_name = wc.provider
                    elif not wc:
                        inferred = _infer_provider_from_name(proc.name)
                        if inferred:
                            w.provider_name = inferred
                else:
                    wc = config.get_worker(proc.name)
                    if wc:
                        prov_name = wc.provider or config.provider
                    else:
                        prov_name = _infer_provider_from_name(proc.name) or config.provider
                    w = Worker(
                        name=proc.name,
                        path=proc.cwd,
                        provider_name=prov_name,
                        kind=infer_worker_kind(proc.name),
                        process=proc,
                    )
                    # #1357: Worker.state defaults to BUZZING, so without this every
                    # restart claims all workers are actively working until the pilot's
                    # first poll — four to six seconds of a confidently wrong dashboard.
                    # The pilot still re-classifies from the PTY immediately; this only
                    # decides what is shown in the meantime.
                    _adopted_new += 1
                    _restore_state(w, remembered)
                    if w.state.value != WorkerState.BUZZING.value:
                        _restored += 1
                new_workers.append(w)
            if remembered or _adopted_new:
                _log.warning(
                    "[#1357] rebuild: %d remembered (%s), adopted %d existing / %d new, "
                    "%d left non-BUZZING after restore",
                    len(remembered),
                    ", ".join(
                        f"{k}={getattr(v, 'state', v)}" for k, v in sorted(remembered.items())[:4]
                    )
                    or "-",
                    _adopted_existing,
                    _adopted_new,
                    _restored,
                )

            # Sort by default group member order if available, else config sort_order
            dg_name = config.default_group or "default"
            default_grp = next(
                (g for g in config.groups if g.name.lower() == dg_name.lower()),
                None,
            )
            if default_grp and default_grp.workers:
                order_map = {name: i for i, name in enumerate(default_grp.workers)}
                new_workers.sort(key=lambda w: order_map.get(w.name, len(order_map)))
            else:
                config_order = {wc.name: i for i, wc in enumerate(config.workers)}
                new_workers.sort(key=lambda w: config_order.get(w.name, len(config_order)))
            self._set_workers(new_workers)
        return self._get_workers()

    # --- Lifecycle ---

    async def launch(self, worker_configs: list[WorkerConfig]) -> list[Worker]:
        """Launch workers via the process pool. Extends workers and updates pilot."""
        pool = self._get_pool()
        config = self._get_config()
        default_prov = config.provider
        workers = self._get_workers()
        if workers:
            from swarm.worker.manager import add_worker_live

            launched = []
            for wc in worker_configs:
                # ``resume=True`` is critical here: this branch fires when the
                # daemon already has Worker objects (post-Reload, post-holder
                # respawn) and is re-launching child processes for them. We
                # want each provider to use its session-continuation flag
                # (``claude --continue``) so the worker resumes its prior
                # conversation instead of starting fresh. ``add_worker_live``
                # defaults ``resume=False`` for genuinely-new workers spawned
                # by ``swarm spawn-worker``; that's the wrong default here.
                worker = await add_worker_live(
                    pool,
                    wc,
                    [],
                    auto_start=True,
                    default_provider=default_prov,
                    resume=True,
                    write_identity=self._write_identity,
                )
                launched.append(worker)
            async with self._worker_lock:
                self._get_workers().extend(launched)
        else:
            from swarm.worker.manager import launch_workers

            launched = await launch_workers(
                pool,
                worker_configs,
                default_provider=default_prov,
                write_identity=self._write_identity,
            )
            async with self._worker_lock:
                self._get_workers().extend(launched)

        pilot = self._get_pilot()
        if pilot:
            pilot.workers = self._get_workers()
        else:
            self._init_pilot(config.drones.enabled)
        self._broadcast_ws({"type": "workers_changed"})
        return launched

    async def spawn(self, worker_config: WorkerConfig) -> Worker:
        """Spawn a single worker into the running session.

        GUARANTEE (#1195): the worker's ``.mcp.json`` identity file is on disk,
        naming this worker, before its process starts. That does not depend on
        callers reaching this method through a particular entry point — the
        ``write_identity`` this service was constructed with is forwarded to
        ``add_worker_live``, which requires it and calls it before
        ``pool.spawn``.

        This used to be a request that callers go through
        ``daemon.spawn_worker`` instead. #1187 was caused by exactly that shape:
        a writer that existed, worked, and simply was not called. A worker
        without its own file inherits the nearest parent directory's, and since
        ownership guards compare canonicalised names exactly, it silently *is*
        whichever worker owns that file.
        """
        from swarm.server.daemon import SwarmOperationError
        from swarm.worker.manager import add_worker_live

        workers = self._get_workers()
        if any(w.name.lower() == worker_config.name.lower() for w in workers):
            raise SwarmOperationError(f"Worker '{worker_config.name}' already running")

        pool = self._get_pool()
        config = self._get_config()
        async with self._worker_lock:
            worker = await add_worker_live(
                pool,
                worker_config,
                workers,
                auto_start=True,
                default_provider=config.provider,
                write_identity=self._write_identity,
            )
        pilot = self._get_pilot()
        if pilot:
            pilot.workers = self._get_workers()
        self._broadcast_ws({"type": "workers_changed"})
        return worker

    async def sleep_worker(self, name: str) -> None:
        """Put a worker to sleep from whatever state it is currently in.

        SLEEPING is a *display* state — RESTING plus a backdated
        ``state_since`` — which makes it fragile: the state tracker re-reads
        the PTY on its next tick, and if the PTY still shows an active turn
        (BUZZING) or an approval prompt (WAITING) it re-detects that and the
        worker leaves SLEEPING again. Parking a busy worker therefore means
        changing what the PTY *shows*, not just what the daemon records.

        That is why this used to require RESTING: the operator had to run
        *Force to rest* first, and the load-bearing half of that was the
        Escape. Folding the Escape in here is what makes one step work;
        merely loosening the state check would produce a menu item that
        appears to succeed and silently undoes itself seconds later.

        STUNG is refused — the process has exited, and rendering a dead
        worker as SLEEPING files it under a state that reads as idle-and-fine.
        """
        import time

        from swarm.server.daemon import SwarmOperationError

        worker = self.require_worker(name)
        if worker.state == WorkerState.STUNG:
            raise SwarmOperationError(f"Worker '{name}' is STUNG (process exited), not idle")
        # Only interrupt when there is a turn or prompt to interrupt. An
        # already-RESTING worker sits at an idle prompt the operator may be
        # mid-thought in, and Escape there buys nothing.
        if worker.state != WorkerState.RESTING:
            await self.escape_worker(name)
        # Force to RESTING so display_state can become SLEEPING
        worker.state = WorkerState.RESTING
        # Backdate state_since so display_state returns SLEEPING
        worker.state_since = time.time() - worker.sleeping_threshold - 1
        worker._api_dict_cache = None
        self._drone_log.add(
            DroneAction.OPERATOR, name, "put to sleep (manual)", category=LogCategory.OPERATOR
        )
        # Persist explicitly. Everything else that changes a worker's state goes through
        # the pilot and emits state_changed, which is what the publisher persists on —
        # but this sets the attribute directly, and on an ALREADY-RESTING worker there is
        # no transition to emit at all. So the backdated timestamp lived only in memory
        # and died with the daemon: "I set several to resting and some to sleep, but on
        # reloading only public-website was resting."
        self._persist_worker_states()
        workers = [{"name": w.name, "state": w.display_state.value} for w in self._get_workers()]
        self._broadcast_ws({"type": "workers_changed", "workers": workers})

    def _persist_worker_states(self) -> None:
        """Write the current state map. Best effort — never raises into an operator action.

        Mirrors ``StatePublisher._persist_worker_states``; both exist because state is
        changed on two paths (the pilot's classification and a direct operator action)
        and only one of them emits an event.
        """
        if self._save_worker_states is None:
            return
        try:
            from swarm.db.worker_state_store import RememberedState

            self._save_worker_states(
                {
                    w.name: RememberedState(state=w.state.value, since=w.state_since)
                    for w in self._get_workers()
                }
            )
        except Exception:
            _log.warning("could not persist worker states after an operator action", exc_info=True)

    async def _graceful_shutdown(self, worker: Worker) -> None:
        """Ask the agent to exit on its own: Esc, quit command, close the shell.

        Best-effort by design — every step is advisory and the caller force-
        kills afterwards regardless. Failures here are logged at DEBUG and
        swallowed, because a worker whose PTY is already gone is precisely the
        case where a kill must still succeed.
        """
        from swarm.providers import get_provider

        proc = worker.process
        if proc is None:
            return
        provider = get_provider(worker.provider_name)
        try:
            # Esc first: a quit command typed into a busy prompt is just text.
            await proc.send_escape()
            await asyncio.sleep(_KILL_INTERRUPT_DELAY)

            quit_cmd = provider.quit_command()
            if quit_cmd:
                # #1451: DELIBERATELY NOT automated=True. Kill is operator-
                # initiated and its whole purpose is to end a stuck session — a
                # worker hung ON a prompt is the commonest reason to press it.
                # Deferring the quit behind that prompt would make exactly the
                # worker you most need to kill unkillable, which is a worse
                # failure than the one this guard prevents. The Esc interrupt
                # sent just above dismisses the prompt first in any case.
                await proc.send_keys(quit_cmd, enter=True)
                # Wait for the agent to actually go. shell_wrap re-execs a login
                # shell when it exits, so "foreground command is a shell" is the
                # signal that the agent is down and the shell is what remains.
                deadline = asyncio.get_running_loop().time() + _KILL_QUIT_TIMEOUT
                while asyncio.get_running_loop().time() < deadline:
                    if provider._is_shell_exited(proc.get_foreground_command()):
                        break
                    await asyncio.sleep(_KILL_POLL_INTERVAL)

            # Same reasoning as the quit above: an un-killable worker is worse
            # than a stray Enter, and by here the agent has already exited.
            await proc.send_keys("exit", enter=True)
            await asyncio.sleep(_KILL_SHELL_EXIT_DELAY)
        except (ProcessError, OSError):
            _log.debug("graceful shutdown of %s failed; force-killing", worker.name, exc_info=True)

    async def kill(self, name: str) -> None:
        """Operator kill: interrupt, quit the agent, close the shell, remove it.

        THE WORKER LEAVES THE ROSTER FIRST, before any shutdown step. That
        ordering is the whole fix for the auto-revive bug — not a flag.

        ``kill`` used to mark the worker STUNG and leave it in the roster. The
        drone decision rule (``drones/rules.py``) revives *any* STUNG worker,
        which is right for a crash and exactly wrong for a deliberate kill, and
        it had no way to tell them apart. Measured 2026-08-03: rcg-dev-install
        killed at 16:16:44, revived at 16:16:59 — so the operator had to kill
        repeatedly until ``revive_count`` exhausted ``max_revive_attempts``.

        Because that rule only ever sees workers in the roster, removing the
        worker first means there is nothing left to revive. A flag would have
        worked too, but it would leave a window open for the whole ~3s graceful
        sequence and would be one more thing a future edit could forget to
        check. Crash recovery is untouched: a worker that dies on its own is
        still in the roster, still marked STUNG, still revived.

        Graceful shutdown is an ATTEMPT, never a guarantee — the force-kill
        below runs unconditionally, so a wedged agent that ignores its quit
        command still dies. Otherwise "graceful" would be a regression.
        """
        from swarm.worker.manager import kill_worker as _kill_worker

        pool = self._get_pool()
        worker = self.require_worker(name)

        # Out of the roster before anything else can observe it as STUNG.
        async with self._worker_lock:
            self._set_workers([w for w in self._get_workers() if w is not worker])
        pilot = self._get_pilot()
        if pilot:
            pilot.workers = self._get_workers()

        await self._graceful_shutdown(worker)

        async with self._worker_lock:
            await _kill_worker(worker, pool)
            worker.state = WorkerState.STUNG
        self._task_board.unassign_worker(worker.name)
        self._drone_log.add(DroneAction.OPERATOR, name, "killed", category=LogCategory.OPERATOR)
        self._broadcast_ws(
            {
                "type": "workers_changed",
                "workers": [{"name": w.name, "state": w.state.value} for w in self._get_workers()],
            }
        )

    async def revive(self, name: str) -> None:
        """Revive a STUNG worker."""
        from swarm.server.daemon import SwarmOperationError
        from swarm.worker.manager import revive_worker as _revive_worker

        pool = self._get_pool()
        worker = self.require_worker(name)
        if worker.state != WorkerState.STUNG:
            raise SwarmOperationError(f"Worker '{name}' is {worker.state.value}, not STUNG")

        await _revive_worker(worker, pool)
        if not worker.process or not worker.process.is_alive:
            raise SwarmOperationError(f"Failed to revive worker '{name}'")
        worker.state = WorkerState.BUZZING
        worker.record_revive()
        self._drone_log.add(
            DroneAction.OPERATOR, name, "revived (manual)", category=LogCategory.OPERATOR
        )
        self._broadcast_ws({"type": "workers_changed"})

    async def merge_worker(self, name: str) -> dict[str, object]:
        """Merge a worker's worktree branch back to the main branch."""
        worker = self.require_worker(name)
        if not worker.repo_path:
            return {
                "success": False,
                "message": f"Worker '{name}' has no worktree",
                "conflicts": [],
            }
        from swarm.git.worktree import merge_worktree

        repo = __import__("pathlib").Path(worker.repo_path)
        result = await merge_worktree(repo, name)
        _log.info(
            "merge %s: success=%s message=%s",
            name,
            result.success,
            result.message,
        )
        return {
            "success": result.success,
            "message": result.message,
            "conflicts": result.conflicts,
        }

    async def kill_session(self, *, all_sessions: bool = False) -> None:
        """Kill all workers and clean up."""
        pilot = self._get_pilot()
        if pilot:
            pilot.stop()

        workers = self._get_workers()
        for w in list(workers):
            self._task_board.unassign_worker(w.name)

        pool = self._get_pool()
        if pool:
            try:
                await pool.kill_all()
            except (ProcessError, OSError):
                _log.warning(
                    "kill_all failed (processes may already be gone)",
                    exc_info=True,
                )

        # Clean up worktrees for isolated workers
        for w in list(workers):
            if w.repo_path:
                try:
                    from pathlib import Path

                    from swarm.git.worktree import remove_worktree

                    await remove_worktree(Path(w.repo_path), w.name)
                except Exception:
                    _log.debug(
                        "worktree cleanup failed for %s",
                        w.name,
                        exc_info=True,
                    )

        async with self._worker_lock:
            self._get_workers().clear()
        self._drone_log.clear()
        self._broadcast_ws({"type": "workers_changed"})

    # --- Bulk operations ---

    async def _send_to_workers(
        self,
        workers: list[Worker],
        action: Callable[[Worker], Awaitable[None]],
        log_actor: str,
        log_detail: str,
    ) -> int:
        """Send an action to a list of workers. Returns count of successes."""
        count = 0
        for w in workers:
            try:
                await action(w)
                count += 1
            except (TimeoutError, ProcessError, OSError):
                _log.debug("failed to send to %s", w.name)
        if count:
            self._drone_log.add(
                DroneAction.OPERATOR,
                log_actor,
                log_detail.format(count=count),
                category=LogCategory.OPERATOR,
            )
        return count

    async def continue_all(self) -> int:
        """Send Enter to all RESTING/WAITING workers (skips user-active terminals)."""
        targets = [
            w
            for w in self._get_workers()
            if not w.is_queen
            and w.state in (WorkerState.RESTING, WorkerState.WAITING)
            and not (w.process and w.process.is_user_active)
        ]
        return await self._send_to_workers(
            targets, lambda w: w.process.send_enter(), "all", "continued {count} worker(s)"
        )

    async def send_all(self, message: str) -> int:
        """Send a message to all workers (skips user-active terminals)."""
        preview = truncate_preview(message)
        targets = [
            w
            for w in self._get_workers()
            if not w.is_queen and not (w.process and w.process.is_user_active)
        ]
        return await self._send_to_workers(
            targets,
            lambda w: w.process.send_keys(message, automated=True),
            "all",
            f'broadcast to {{count}} worker(s): "{preview}"',
        )

    async def send_group(self, group_name: str, message: str) -> int:
        """Send a message to all workers in a group."""
        config = self._get_config()
        group_workers = config.get_group(group_name)
        group_names = {w.name.lower() for w in group_workers}
        targets = [w for w in self._get_workers() if w.name.lower() in group_names]
        preview = truncate_preview(message)
        return await self._send_to_workers(
            targets,
            lambda w: w.process.send_keys(message, automated=True),
            group_name,
            f'group send to {{count}} worker(s): "{preview}"',
        )
