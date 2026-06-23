"""StandingLoopManager — operator-controlled background-improvement loops (#765).

Part B of ``docs/specs/native-loop-functions.md`` (§3) — the "my job is to
write loops" model, scoped to Swarm. A standing loop is a **task generator**,
not a board entity (§3.1): when its worker goes idle with an empty queue it
files ONE normal one-shot task (tagged ``standing-loop``) through the existing
board, which then flows through the normal verifier / self-loop untouched — no
new task status, no verifier branching.

Properties, all by construction here:

* **Idle-triggered, preempted by real work (§3.2/§3.3).** The only caller is
  the empty-queue branch of the post-ship self-loop
  (``auto_start_next_assigned``): the generator runs *only* when the worker has
  no ASSIGNED task. Any real assigned / operator / cross-project task is started
  there first and the generator is never reached — preemption for free.
* **Rolling daily per-loop token cap (§3.4).** Output tokens burned by this
  loop's own tasks are summed in a rolling 24h window; once the window crosses
  ``daily_token_cap`` the loop **sleeps** (generates nothing) until the window
  resets. Layered on top of #762's per-task ceiling.
* **Operator-controlled (§3.5).** start / pause / stop per worker plus a global
  kill switch live on this manager and are surfaced through the dashboard. The
  kill switch is the one-click stop for the whole always-on burn source.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from swarm.logging import get_logger

if TYPE_CHECKING:
    from swarm.tasks.task import SwarmTask

_log = get_logger("drones.standing_loop")

# Tag stamped on every generated task so the burn accounting and the board
# can recognise standing-loop work without a new task status.
STANDING_LOOP_TAG = "standing-loop"

_DAY_SECONDS = 86_400.0

# Deterministic v1 generator topics (spec §3.1 use cases). Pressure-tested as a
# plain rule before reaching for a headless-Queen call, per CLAUDE.md.
DEFAULT_TOPICS: list[str] = [
    "Standing loop: find and remove one dead or unused code path",
    "Standing loop: de-duplicate one repeated abstraction",
    "Standing loop: raise test coverage on one under-tested module",
    "Standing loop: fix one doc-drift gap between the code and the docs",
]


@dataclass
class LoopState:
    """Per-worker standing-loop runtime state (not persisted — operator-set)."""

    enabled: bool = False  # operator switched the loop ON
    paused: bool = False  # operator paused it (distinct from never-enabled)
    window_start: float = 0.0  # rolling 24h burn-window start (wall clock)
    tokens_in_window: int = 0  # output tokens this loop's tasks burned in-window
    topic_cursor: int = 0  # round-robin cursor over the topic list

    def active(self) -> bool:
        """Whether the loop is eligible to generate (enabled and not paused)."""
        return self.enabled and not self.paused


class StandingLoopManager:
    """Operator-controlled recurring task generators (one per worker).

    Pure of daemon internals: board access and task filing are injected as
    callbacks so the manager is unit-testable in isolation.
    """

    def __init__(
        self,
        *,
        topics: list[str],
        daily_token_cap: int,
        file_task: Callable[[str, str], SwarmTask | None],
        open_titles: Callable[[str], set[str]],
        now: Callable[[], float] = time.time,
    ) -> None:
        # ``file_task(worker_name, title) -> task|None`` files + dispatches one
        # standing-loop task. ``open_titles(worker_name) -> {titles}`` is the
        # set of this worker's still-open task titles (for dedup). ``now`` is
        # injectable so tests can drive the 24h window deterministically.
        self._topics = topics
        self._daily_cap = daily_token_cap
        self._file_task = file_task
        self._open_titles = open_titles
        self._now = now
        self._states: dict[str, LoopState] = {}
        self._kill_switch = False

    # ------------------------------------------------------------------ #
    # Operator controls (dashboard-surfaced)
    # ------------------------------------------------------------------ #
    def _state(self, worker_name: str) -> LoopState:
        return self._states.setdefault(worker_name, LoopState())

    def start(self, worker_name: str) -> None:
        """Enable (and un-pause) the standing loop for *worker_name*."""
        st = self._state(worker_name)
        st.enabled = True
        st.paused = False

    def pause(self, worker_name: str) -> None:
        """Pause without disabling — resumable with :meth:`start`."""
        self._state(worker_name).paused = True

    def stop(self, worker_name: str) -> None:
        """Disable the loop entirely (operator off)."""
        st = self._state(worker_name)
        st.enabled = False
        st.paused = False

    def set_kill_switch(self, on: bool) -> None:
        """Global kill switch — halts generation for ALL loops at once."""
        self._kill_switch = on
        if on:
            _log.warning("standing-loop GLOBAL KILL SWITCH engaged")

    @property
    def kill_switch(self) -> bool:
        return self._kill_switch

    # ------------------------------------------------------------------ #
    # Burn accounting (rolling 24h per-loop cap)
    # ------------------------------------------------------------------ #
    def _roll_window(self, st: LoopState) -> None:
        now = self._now()
        if st.window_start <= 0.0 or (now - st.window_start) >= _DAY_SECONDS:
            st.window_start = now
            st.tokens_in_window = 0

    def record_burn(self, worker_name: str, output_token_delta: int) -> None:
        """Charge a standing-loop task's output-token delta to the daily window.

        Called from the daemon usage loop ONLY when the worker's ACTIVE task is
        a standing-loop task, with the same delta #762 charges to the task.
        """
        if output_token_delta <= 0:
            return
        st = self._state(worker_name)
        self._roll_window(st)
        st.tokens_in_window += output_token_delta

    def is_exhausted(self, worker_name: str) -> bool:
        """Whether *worker_name*'s loop is asleep on its daily cap right now."""
        st = self._state(worker_name)
        self._roll_window(st)
        return self._daily_cap > 0 and st.tokens_in_window >= self._daily_cap

    # ------------------------------------------------------------------ #
    # Generation (the empty-queue hook calls this)
    # ------------------------------------------------------------------ #
    def maybe_generate(self, worker_name: str) -> SwarmTask | None:
        """File one standing-loop task for an idle, empty-queue worker.

        Returns the filed task, or ``None`` when the loop is off/paused/killed,
        asleep on its daily cap, or every topic already has an open task
        (nothing to do). The caller only invokes this when the worker has no
        ASSIGNED task, so real work always preempts the loop.
        """
        if self._kill_switch:
            return None
        st = self._state(worker_name)
        if not st.active():
            return None
        # Daily cap — sleep until the rolling window resets.
        self._roll_window(st)
        if self._daily_cap > 0 and st.tokens_in_window >= self._daily_cap:
            _log.debug(
                "standing-loop %s asleep: %d/%d output tokens this window",
                worker_name,
                st.tokens_in_window,
                self._daily_cap,
            )
            return None
        n = len(self._topics)
        if n == 0:
            return None
        # Dedup: skip topics that already have an open task for this worker.
        open_now = self._open_titles(worker_name)
        for offset in range(n):
            idx = (st.topic_cursor + offset) % n
            title = self._topics[idx]
            if title in open_now:
                continue
            st.topic_cursor = (idx + 1) % n
            return self._file_task(worker_name, title)
        return None  # all topics already in flight — nothing to file

    # ------------------------------------------------------------------ #
    # Dashboard readout
    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, object]:
        """Snapshot for the dashboard: per-worker state + live burn readout."""
        loops = []
        for name, st in sorted(self._states.items()):
            self._roll_window(st)
            loops.append(
                {
                    "worker": name,
                    "enabled": st.enabled,
                    "paused": st.paused,
                    "exhausted": self._daily_cap > 0 and st.tokens_in_window >= self._daily_cap,
                    "tokens_in_window": st.tokens_in_window,
                    "daily_token_cap": self._daily_cap,
                }
            )
        return {
            "kill_switch": self._kill_switch,
            "daily_token_cap": self._daily_cap,
            "loops": loops,
        }
