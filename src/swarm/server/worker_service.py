"""WorkerService — worker CRUD, I/O operations, and lifecycle management."""

from __future__ import annotations

import asyncio
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
    ) -> None:
        self._broadcast_ws = broadcast_ws
        self._drone_log = drone_log
        self._task_board = task_board
        self._get_pilot = get_pilot
        self._get_pool = get_pool
        self._get_config = get_config
        self._get_workers = get_workers
        self._set_workers = set_workers
        self._worker_lock = worker_lock
        self._init_pilot = init_pilot
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
        _log_operator: bool = True,
    ) -> None:
        """Send text to a worker's process (serialized per-worker).

        ``enter=False`` types the message into the PTY input buffer
        without submitting — used by the Web Share Target flow so the
        operator can add context (or edit the auto-inserted path)
        before hitting Enter themselves. Default stays True to preserve
        the long-standing semantics of `/api/workers/<name>/send`.
        """
        worker = self.require_worker(name)
        self._require_process(worker)
        pilot = self._get_pilot()
        if pilot:
            pilot.wake_worker(name)
        async with self._pty_lock(name):
            await worker.process.send_keys(message, enter=enter)
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
            from swarm.worker.worker import infer_worker_kind

            existing = {w.name: w for w in workers}
            new_workers: list[Worker] = []
            for proc in processes:
                if proc.name in existing:
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
                new_workers.append(w)
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
        """Spawn a single worker into the running session."""
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
            )
        pilot = self._get_pilot()
        if pilot:
            pilot.workers = self._get_workers()
        self._broadcast_ws({"type": "workers_changed"})
        return worker

    async def sleep_worker(self, name: str) -> None:
        """Force a RESTING worker into SLEEPING by backdating state_since."""
        import time

        from swarm.server.daemon import SwarmOperationError

        worker = self.require_worker(name)
        if worker.state not in (WorkerState.RESTING, WorkerState.WAITING):
            raise SwarmOperationError(f"Worker '{name}' is {worker.state.value}, not idle")
        # Force to RESTING so display_state can become SLEEPING
        worker.state = WorkerState.RESTING
        # Backdate state_since so display_state returns SLEEPING
        worker.state_since = time.time() - worker.sleeping_threshold - 1
        worker._api_dict_cache = None
        self._drone_log.add(
            DroneAction.OPERATOR, name, "put to sleep (manual)", category=LogCategory.OPERATOR
        )
        workers = [{"name": w.name, "state": w.display_state.value} for w in self._get_workers()]
        self._broadcast_ws({"type": "workers_changed", "workers": workers})

    async def kill(self, name: str) -> None:
        """Kill a worker: mark STUNG, unassign tasks, broadcast."""
        from swarm.worker.manager import kill_worker as _kill_worker

        pool = self._get_pool()
        worker = self.require_worker(name)

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
            lambda w: w.process.send_keys(message),
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
            lambda w: w.process.send_keys(message),
            group_name,
            f'group send to {{count}} worker(s): "{preview}"',
        )
