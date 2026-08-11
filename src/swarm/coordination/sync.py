"""Auto-pull sync — propagate commits across single-branch workers."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from swarm.logging import get_logger

if TYPE_CHECKING:
    from swarm.worker.worker import Worker

_log = get_logger("coordination.sync")

# Delay before sending pull to avoid race with commit flush
_PULL_DELAY = 2.0


async def pull_worker(worker: Worker) -> bool:
    """Send ``git pull --rebase`` to a worker's PTY.

    Only acts on workers that are RESTING and have a live process.
    Returns True if the pull command was sent.
    """
    from swarm.worker.worker import WorkerState

    proc = worker.process
    if proc is None:
        return False
    if proc.is_user_active:
        _log.debug("skip pull for %s: user active", worker.name)
        return False
    if worker.state not in (WorkerState.RESTING, WorkerState.SLEEPING):
        _log.debug("skip pull for %s: state=%s", worker.name, worker.state.value)
        return False

    await proc.send_keys("git pull --rebase --quiet\n", automated=True)
    _log.info("sent git pull to %s", worker.name)
    return True


class AutoPullSync:
    """Orchestrates git pull across workers on a single branch.

    When a worker commits, other idle workers are told to pull the
    latest changes so they don't drift behind.
    """

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled
        self._pull_history: list[dict[str, Any]] = []
        self._pending_task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    async def on_commit(
        self,
        committer: str,
        workers: list[Worker],
    ) -> int:
        """Trigger pull on other workers after a commit.

        Args:
            committer: Name of the worker that committed.
            workers: All workers in the hive.

        Returns:
            Number of workers that received a pull command.
        """
        if not self._enabled:
            return 0

        # Small delay to let the commit flush to disk
        await asyncio.sleep(_PULL_DELAY)

        pulled = 0
        for w in workers:
            if w.name == committer:
                continue
            # Only pull workers on same branch (no worktree isolation)
            if w.repo_path:
                continue
            if await pull_worker(w):
                pulled += 1
                self._pull_history.append(
                    {
                        "timestamp": time.time(),
                        "committer": committer,
                        "target": w.name,
                    }
                )

        # Cap history
        if len(self._pull_history) > 100:
            self._pull_history = self._pull_history[-100:]

        if pulled:
            _log.info(
                "auto-pull: %s committed, pulled %d workers",
                committer,
                pulled,
            )

        return pulled

    def schedule_pull(
        self,
        committer: str,
        workers: list[Worker],
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Fire-and-forget pull scheduling (non-blocking)."""
        if not self._enabled:
            return

        async def _do_pull() -> None:
            await self.on_commit(committer, workers)

        try:
            lp = loop or asyncio.get_running_loop()
            task = lp.create_task(_do_pull())
            # Replace any pending pull task
            if self._pending_task and not self._pending_task.done():
                self._pending_task.cancel()
            self._pending_task = task
        except RuntimeError:
            pass  # No event loop (sync test context)

    def get_status(self) -> dict[str, Any]:
        """Return sync status for API."""
        return {
            "enabled": self._enabled,
            "recent_pulls": self._pull_history[-10:],
            "total_pulls": len(self._pull_history),
        }
