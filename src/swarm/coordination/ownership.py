"""File ownership tracking — maps files to the worker that owns them."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from swarm.logging import get_logger

_log = get_logger("coordination.ownership")


# Files that Swarm itself installs into every worker's repo as
# scaffolding — slash commands, skills, lockfiles.  They're literally
# identical across every worker and they're updated by the Swarm
# hooks installer, not by any individual worker's task.  Treating
# them as ownable produces ~50 WARNING lines on every reload (one
# per worker × per scaffolding file) with no actionable signal,
# drowning out real overlap alerts.  Skip them at the ownership layer.
_SCAFFOLDING_PREFIXES: tuple[str, ...] = (
    ".claude/commands/swarm-",
    ".claude/skills/swarm-",
    ".claude/scheduled_tasks.lock",
    ".claude/ux-audit.json",
)


def _is_scaffolding(path: str) -> bool:
    """True if ``path`` is a Swarm-managed file present in every worker."""
    return any(path.startswith(p) or p in path for p in _SCAFFOLDING_PREFIXES)


class OwnershipMode(Enum):
    """How file ownership violations are handled."""

    OFF = "off"
    WARNING = "warning"
    HARD_BLOCK = "hard-block"


@dataclass
class Overlap:
    """A file touched by a worker that is owned by another."""

    file_path: str
    owner: str
    intruder: str
    timestamp: float = field(default_factory=time.time)


class FileOwnershipMap:
    """Track per-file ownership across workers.

    Ownership is derived from two sources:
    1. Explicit claims (e.g., Queen assigns files during task distribution)
    2. Runtime tracking (conflict detection feeds changed files)

    KEYS ARE REPO-RELATIVE PATHS, AND THAT IS DELIBERATE (#1510). They come from
    ``get_changed_files``' ``git diff --name-only HEAD``. This map answers "will these
    worktrees CONFLICT WHEN MERGED" — a question about a logical path in the repo.

    IT IS NOT THE SAME MAP AS ``daemon.file_locks``, which ``swarm_claim_file`` and
    ``routes/hooks._check_file_lock`` write. That one keys ``os.path.realpath`` ABSOLUTE
    paths and answers "is another worker WRITING THIS EXACT FILE right now". Because
    workers run in separate worktrees, ``src/foo.py`` in two checkouts is two files on
    disk that will collide at merge — so the key formats are the semantics, not an
    oversight. Unify on realpath and the merge predictor stops relating the checkouts;
    unify on relative and the write lock starts blocking workers who share no file.
    #1510 measured this and declined to merge them.

    KNOWN ROUGH EDGE, left for the operator: both are driven by the single
    ``coordination.file_ownership`` setting, so one knob governs two mechanisms with
    different requirements. That is the shape #1571 had to split apart for INV-2. Note
    only ``file_locks`` currently BLOCKS an edit — this map's owner data reaches
    enforcement solely through ``TaskCoordinator.check_ownership`` at assign time.
    """

    def __init__(self, mode: OwnershipMode = OwnershipMode.WARNING) -> None:
        self._mode = mode
        # file_path → worker_name
        self._owners: dict[str, str] = {}
        # worker_name → set of owned file paths
        self._worker_files: dict[str, set[str]] = {}
        # Recent overlaps for status reporting
        self._overlaps: list[Overlap] = []

    @property
    def mode(self) -> OwnershipMode:
        return self._mode

    @mode.setter
    def mode(self, value: OwnershipMode) -> None:
        self._mode = value

    def claim(self, worker_name: str, files: set[str]) -> list[Overlap]:
        """Register file ownership for a worker.

        Returns any overlaps detected (files owned by other workers).
        Swarm-managed scaffolding (``.claude/commands/swarm-*`` etc.)
        is skipped — those files are installed identically into every
        worker's repo and aren't meaningful for ownership tracking.
        Genuine overlaps are still recorded; the per-file WARNING
        emission is now coalesced to one log line per (owner, intruder)
        pair so a multi-worker poll cycle doesn't dump 50+ lines.
        """
        overlaps: list[Overlap] = []
        coalesced: dict[str, list[str]] = {}

        for f in files:
            if _is_scaffolding(f):
                continue
            existing = self._owners.get(f)
            if existing and existing != worker_name:
                overlap = Overlap(
                    file_path=f,
                    owner=existing,
                    intruder=worker_name,
                )
                overlaps.append(overlap)
                self._overlaps.append(overlap)
                coalesced.setdefault(existing, []).append(f)
            else:
                self._owners[f] = worker_name
                self._worker_files.setdefault(worker_name, set()).add(f)

        # One WARNING per (owner, intruder) pair, naming up to 5 files
        # plus a count.  Pre-fix this was one WARNING per file —
        # operators saw 50+ identical lines per poll cycle when every
        # worker had the same scaffolding files marked dirty by git.
        for owner, files_list in coalesced.items():
            preview = ", ".join(sorted(files_list)[:5])
            extra = f" (+{len(files_list) - 5} more)" if len(files_list) > 5 else ""
            _log.warning(
                "file overlap: %d file(s) owned by %s touched by %s — %s%s",
                len(files_list),
                owner,
                worker_name,
                preview,
                extra,
            )

        # Cap overlap history
        if len(self._overlaps) > 100:
            self._overlaps = self._overlaps[-100:]

        return overlaps

    def release(self, worker_name: str) -> int:
        """Release all files owned by a worker. Returns count released."""
        files = self._worker_files.pop(worker_name, set())
        count = 0
        for f in files:
            if self._owners.get(f) == worker_name:
                del self._owners[f]
                count += 1
        return count

    def release_file(self, file_path: str) -> str | None:
        """Release a single file. Returns the previous owner or None."""
        owner = self._owners.pop(file_path, None)
        if owner:
            worker_files = self._worker_files.get(owner)
            if worker_files:
                worker_files.discard(file_path)
        return owner

    def get_owner(self, file_path: str) -> str | None:
        """Get the current owner of a file."""
        return self._owners.get(file_path)

    def get_worker_files(self, worker_name: str) -> set[str]:
        """Get all files owned by a worker."""
        return set(self._worker_files.get(worker_name, set()))

    def overlaps_for(self, worker_name: str) -> list[Overlap]:
        """Recorded overlaps where ``worker_name`` is the INTRUDER (#1510).

        The direction is the point. ``platform`` OWNING a contested file is not a reason
        to hold ``platform`` back — the worker to stop is the one that touched someone
        else's file. Keying this on ``owner`` instead would block the victim.

        Reads the overlap history rather than deriving anything: ``claim`` already records
        an :class:`Overlap` at the moment it declines to reassign a file, and that is the
        ONLY place the losing side is retained. ``_owners`` keeps the original owner and
        ``_worker_files`` never gains the intruder's entry, which is exactly why the
        previous "check my own files against myself" query could not see a conflict.

        Note ``_overlaps`` is a capped history (100) with no expiry, so this can report a
        conflict that has since been resolved. That is the fail-CLOSED direction for a
        guard that until now never fired at all; a TTL picked by feel is what #1571 was
        about, so it is left for the operator rather than invented here.
        """
        return [o for o in self._overlaps if o.intruder == worker_name]

    def check_overlap(self, worker_name: str, files: set[str]) -> list[Overlap]:
        """Check if files would conflict without claiming them."""
        overlaps: list[Overlap] = []
        for f in files:
            existing = self._owners.get(f)
            if existing and existing != worker_name:
                overlaps.append(
                    Overlap(
                        file_path=f,
                        owner=existing,
                        intruder=worker_name,
                    )
                )
        return overlaps

    def update_from_conflicts(self, changed_files: dict[str, set[str]]) -> list[Overlap]:
        """Update ownership from runtime file detection.

        Args:
            changed_files: Mapping of worker_name → set of changed files.

        Returns overlaps where files are touched by non-owners.
        """
        all_overlaps: list[Overlap] = []
        for worker_name, files in changed_files.items():
            overlaps = self.claim(worker_name, files)
            all_overlaps.extend(overlaps)
        return all_overlaps

    def transfer(self, file_path: str, new_owner: str) -> str | None:
        """Transfer ownership of a file. Returns old owner."""
        old_owner = self.release_file(file_path)
        self._owners[file_path] = new_owner
        self._worker_files.setdefault(new_owner, set()).add(file_path)
        return old_owner

    def to_dict(self) -> dict[str, Any]:
        """Serialize for API/WS responses."""
        return {
            "mode": self._mode.value,
            "files": dict(self._owners),
            "workers": {name: sorted(files) for name, files in self._worker_files.items()},
            "recent_overlaps": [
                {
                    "file": o.file_path,
                    "owner": o.owner,
                    "intruder": o.intruder,
                    "timestamp": o.timestamp,
                }
                for o in self._overlaps[-20:]
            ],
        }

    def clear(self) -> None:
        """Clear all ownership data."""
        self._owners.clear()
        self._worker_files.clear()
        self._overlaps.clear()
