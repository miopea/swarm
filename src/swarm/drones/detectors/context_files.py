"""ContextFileTracker — extract file paths from PTY output for revival.

Extracted from :class:`~swarm.drones.state_tracker.WorkerStateTracker`
(Phase 1 of ``docs/specs/state-tracker-refactor.md``). On worker
revive, the recorded paths can be re-injected so the worker doesn't
lose its read context.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from swarm.worker.worker import Worker


# Captures the file path argument from common Read/Edit/Write/Glob/Grep
# tool invocations as they appear in Claude Code PTY output.
_RE_FILE_PATH = re.compile(r"(?:Read|Edit|Write|Glob|Grep)\s*\(?['\"]?(/\S+?)(?:['\")]|\)|\s|$)")

# Soft cap on the per-worker history we hold for context restoration.
_MAX_CONTEXT_FILES = 10


class ContextFileTracker:
    """Watch BUZZING workers' PTY output and record file paths they touched.

    Stateless: the per-worker history lives on
    :attr:`Worker.last_context_files`.  The detector only enforces the
    cap and the BUZZING-only gate.
    """

    def check(self, worker: Worker, content: str) -> None:
        from swarm.worker.worker import WorkerState

        if worker.state != WorkerState.BUZZING:
            return
        # Cap regex scan work: large outputs can match hundreds of paths,
        # and we only keep _MAX_CONTEXT_FILES in the worker's list anyway.
        _scan_cap = _MAX_CONTEXT_FILES * 4
        for i, m in enumerate(_RE_FILE_PATH.finditer(content)):
            if i >= _scan_cap:
                break
            path = m.group(1).rstrip(".,;:)")
            if path not in worker.last_context_files:
                worker.last_context_files.append(path)
                if len(worker.last_context_files) > _MAX_CONTEXT_FILES:
                    worker.last_context_files.pop(0)
