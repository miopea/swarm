"""Remember what each worker was doing, so a restart does not invent an answer.

THE BUG THIS FIXES (#1357, operator-reported with a screenshot). ``Worker.state``
defaults to ``BUZZING`` and was never persisted, so every daemon start constructed all
sixteen workers as "actively working". The dashboard rendered that faithfully for the
four to six seconds it took the pilot's first poll to classify each worker from its PTY
output. The screenshot's tell was every worker reading an identical "BUZZING — 4m".

"Everything is working" is the worst thing to assert while you do not know: an operator
glancing at a fresh dashboard saw a fully-busy swarm.

WHY A CONFIG KEY RATHER THAN A TABLE. This is one small map rewritten in place, never
queried, never joined. A table means a schema migration for data that has no
relationships and no history — the ``config`` key-value table already exists for exactly
this shape, and using it keeps the change to code that can be reverted in one commit.

WHAT IS DELIBERATELY NOT STORED. No history, no per-transition log. The buzz log already
records transitions; this answers only "what was it last time we looked", which is the
one question a cold start needs.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from swarm.logging import get_logger

if TYPE_CHECKING:
    from swarm.db.core import SwarmDB

_log = get_logger("db.worker_state")

_KEY = "worker_states"

# A restored state older than this is discarded rather than shown. A daemon that has
# been down overnight knows nothing useful about what its workers are doing, and stale
# state presented as current is the quieter version of the bug being fixed.
_MAX_AGE_SECONDS = 30 * 60


class WorkerStateStore:
    """Last-known worker states, keyed by worker name."""

    def __init__(self, db: SwarmDB) -> None:
        self._db = db

    def save(self, states: dict[str, str]) -> None:
        """Persist the whole map. Best effort — never raises into a state transition.

        Called on CHANGE rather than on a timer, so the write happens once per real
        transition rather than once per poll across sixteen workers.
        """
        if not states:
            return
        payload = json.dumps({"at": time.time(), "states": states})
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO config (key, value, updated_at) VALUES (?, ?, ?)",
                (_KEY, payload, time.time()),
            )
            self._db.commit()
        except Exception:
            # A failure here costs the next restart its head start, nothing more. It
            # must not propagate into the state machine that called it.
            _log.debug("could not persist worker states", exc_info=True)

    def load(self) -> dict[str, str]:
        """Last-known states, or {} when there are none or they are too old."""
        try:
            row = self._db.fetchone("SELECT value FROM config WHERE key = ?", (_KEY,))
        except Exception:
            _log.debug("could not read worker states", exc_info=True)
            return {}
        if not row:
            return {}
        try:
            data = json.loads(row["value"])
        except (ValueError, TypeError, KeyError):
            return {}
        if not isinstance(data, dict):
            return {}
        age = time.time() - float(data.get("at", 0) or 0)
        if age > _MAX_AGE_SECONDS:
            _log.info(
                "discarding worker states saved %.0f minutes ago — too old to be useful",
                age / 60,
            )
            return {}
        states = data.get("states")
        return {str(k): str(v) for k, v in states.items()} if isinstance(states, dict) else {}
