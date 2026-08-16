"""WorkerService — worker CRUD, I/O operations, and lifecycle management."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from swarm.drones.log import DroneAction, DroneLog, LogCategory
from swarm.logging import get_logger
from swarm.pty.process import ProcessError
from swarm.pty.prompt_guard import has_open_selection_prompt
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

# #1608: how much screen to read when answering a prompt. Matches
# ``pty.process._PROMPT_SCAN_LINES`` deliberately — the answer path must see exactly what
# the #1451 guard sees, or the two could disagree about whether a prompt is open and an
# answer could be refused for a prompt that is holding writes (or worse, the reverse).
# Prompt HEIGHT is what defeated the earlier detectors, so this is generous on purpose.
_PROMPT_ANSWER_SCAN_LINES = 120

# How long to wait before checking whether the answer actually took. Short enough that
# the Queen is not blocked, long enough for a TUI to repaint. If the prompt is still
# there after this, we report UNCONFIRMED rather than claiming success — a slow path may
# still deliver, and "not yet confirmed" is the honest description of that state.
_ANSWER_SETTLE_SECONDS = 2.0

# Pause between arrow keys so a TUI repaints between them. Without it a burst of escape
# sequences can be coalesced and the cursor lands short of the target.
_ARROW_STEP_SECONDS = 0.12


class PromptOpenError(Exception):
    """A keystroke was refused because the worker has an open selection prompt.

    Distinct from ``ProcessError`` on purpose: nothing went wrong with the PTY, and the
    caller should see a 409 (refused, try again once the picker is cleared) rather than a
    500 (broken). Carrying that distinction in the type is what lets both entry points map
    it to the same status without either of them re-deriving the reason.
    """


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
        # #1357: a remembered state IS a measurement, just an older one. Leaving
        # the worker UNCLASSIFIED here would make the persistence this function
        # exists for invisible on the dashboard — the exact half of the report
        # ("state is not remembered between reloads") that this already fixed.
        worker.state_known = True
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
    ) -> bool:
        """Send text to a worker's process (serialized per-worker). True if DELIVERED.

        #1608: returns False when the write was HELD by the open-prompt guard. It used
        to return None either way, so a held message and a delivered one were
        indistinguishable to every caller — the Queen was told "Prompt sent" for a
        message sitting in ``_deferred_keys``, and spent a night believing she had no
        way to act on a stalled worker.

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
            delivered = await worker.process.send_keys(message, enter=enter, automated=automated)
        # `is not False` so a provider whose send_keys still returns None is read as
        # delivered rather than silently reported as held — the safe direction while
        # the return value propagates through the tree.
        delivered = delivered is not False
        if _log_operator:
            self._drone_log.add(
                DroneAction.OPERATOR,
                name,
                "sent message" if delivered else "message HELD — selection prompt open",
                category=LogCategory.OPERATOR,
            )
            self._record_override(name, "redirected_worker", "sent message")
        return delivered

    async def continue_worker(self, name: str) -> None:
        """Send Enter to a worker's process (serialized per-worker)."""
        worker = self.require_worker(name)
        self._require_process(worker)
        pilot = self._get_pilot()
        if pilot:
            pilot.wake_worker(name)
            pilot.mark_operator_continue(name)
        async with self._pty_lock(name):
            await worker.process.send_enter(actor="operator-continue")
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
        await worker.process.send_escape(actor="operator-escape")
        self._drone_log.add(
            DroneAction.OPERATOR, name, "sent Escape", category=LogCategory.OPERATOR
        )

    async def shift_tab_worker(self, name: str) -> None:
        """Send Shift+Tab — the permission-mode cycle — to a worker's process (#1677).

        REFUSES WHEN A SELECTION PROMPT IS OPEN, and does not queue. The ordinary operator
        write path deliberately BYPASSES the #1451 hold: the operator is the human the
        prompt is waiting for, and a guard there would make an open question unanswerable.
        A mode-cycle button is different in kind — it is not an answer to the question on
        screen, and firing one into an open picker is how an operator's own question gets
        answered by accident (#1443's shape). Refusing beats queueing for the reason #1608
        and #1623 settled on: a discrete action delivered whenever the prompt happens to
        close arrives with no relation to why it was sent.

        The guard lives HERE rather than in the routes because there are two entry points
        (``/action/shift-tab`` and ``/api/workers/{name}/shift-tab``) and a guard
        duplicated per route is a guard that will eventually only be on one of them.
        """
        worker = self.require_worker(name)
        self._require_process(worker)
        assert worker.process is not None

        try:
            screen = worker.process.get_content(_PROMPT_ANSWER_SCAN_LINES)
        except Exception:
            screen = ""
        if has_open_selection_prompt(screen):
            raise PromptOpenError(
                f"NOT SENT — {name} has an open selection prompt, and a mode change fired "
                f"into one would answer a question the operator was asked. Clear the "
                f"picker first (answer it, or queen_dismiss_prompt), then try again."
            )

        pilot = self._get_pilot()
        if pilot:
            pilot.wake_worker(name)
        await worker.process.send_shift_tab(actor="operator-shortcut")
        self._drone_log.add(
            DroneAction.OPERATOR,
            name,
            "sent Shift+Tab (permission-mode cycle)",
            category=LogCategory.OPERATOR,
        )

    async def dismiss_open_prompt(self, name: str) -> str:
        """Send Escape to close a selection prompt, then READ BACK (#1623).

        The last of the four verbs to get this treatment. `escape_worker` reported
        "Escape sent" — a DISPATCH described as an OUTCOME, which is the wording that
        cost the fleet roughly fifteen worker-hours through `queen_prompt_worker`'s
        "Prompt sent" and `queen_interrupt_worker`'s "Interrupt sent".

        NOTHING HERE ASSUMES ESCAPE WORKS. The prompt footer on a real captured picker
        reads "Esc to cancel", and `send_escape` writes 0x1b via `_write` so the #1451
        hold does not apply — but neither of those has been observed to CLOSE a picker,
        and the interrupt lesson is that a plausible code reading is not a measurement.
        This reports what it sees rather than what the code suggests.
        """
        from swarm.pty.prompt_options import parse_open_prompt

        worker = self.require_worker(name)
        self._require_process(worker)
        before = parse_open_prompt(worker.process.get_content(_PROMPT_ANSWER_SCAN_LINES))
        if before is None:
            return "no selection prompt is open — nothing was sent"

        pilot = self._get_pilot()
        if pilot:
            pilot.wake_worker(name)
        await worker.process.send_escape(actor="queen-dismiss")
        await asyncio.sleep(_ANSWER_SETTLE_SECONDS)

        after = parse_open_prompt(worker.process.get_content(_PROMPT_ANSWER_SCAN_LINES))
        gone = after is None or after.fingerprint != before.fingerprint
        verdict = "dismissed" if gone else "SENT BUT NOT CONFIRMED"
        self._drone_log.add(
            DroneAction.OPERATOR,
            name,
            f"{verdict} prompt {before.fingerprint} (Escape)",
            category=LogCategory.OPERATOR,
        )
        if gone:
            return f"dismissed prompt {before.fingerprint} — confirmed, it is gone"
        return (
            f"SENT BUT NOT CONFIRMED — wrote Escape, and {_ANSWER_SETTLE_SECONDS:.0f}s "
            f"later prompt {before.fingerprint} is STILL OPEN. Escape may not close this "
            f"picker. Re-read with queen_view_worker_state; if it is unchanged, "
            f"queen_answer_prompt selecting the deny option is the proven route."
        )

    def check_prompt_answer(self, name: str, option: int, fingerprint: str) -> tuple[bool, str]:
        """Validate an answer WITHOUT sending it. Returns ``(ok, message)``.

        SYNCHRONOUS so the MCP handler can report the refusal truthfully. Firing this
        async and returning "sent" would tell the Queen her answer landed while a stale
        fingerprint was being rejected out of band — which is #1527's defect exactly: an
        unawaited call whose outcome nobody sees. The refusal IS the feature here, so it
        must be the thing that comes back.
        """
        from swarm.pty.prompt_options import parse_open_prompt

        worker = self.require_worker(name)
        self._require_process(worker)
        prompt = parse_open_prompt(worker.process.get_content(_PROMPT_ANSWER_SCAN_LINES))
        if prompt is None:
            return False, "no selection prompt is open — nothing was sent"
        if prompt.fingerprint != fingerprint:
            return False, (
                f"prompt changed since you read it (now {prompt.fingerprint}, "
                f"you sent {fingerprint}) — nothing was sent. Re-read and retry."
            )
        chosen = prompt.option(option)
        if chosen is None:
            available = ", ".join(str(o.number) for o in prompt.options)
            return False, (
                f"option {option} is not on this prompt (available: {available}) — nothing sent"
            )
        return True, f"option {option} ({chosen.label})"

    async def answer_open_prompt(self, name: str, option: int, fingerprint: str) -> str:
        """Answer a worker's open selection prompt by option number (#1608).

        Returns a human-readable outcome. RAISES nothing for the refusal cases — the
        caller needs to report which refusal happened, and an exception type per case
        would be a worse API for a tool whose whole job is to explain itself.

        WHY THIS IS NOT A HOLE IN #1451's GUARD, which is the question to ask of it.
        That guard holds writes NO HUMAN CHOSE TO MAKE RIGHT NOW — a dispatch, a nudge,
        a broadcast — because Enter is the default and such a write either commits the
        highlighted option or types its body in as free text. This call is the opposite:
        it names ONE option, on ONE prompt identified by fingerprint, and refuses if that
        prompt is no longer the one on screen. **The fingerprint is the authorisation.**
        A caller who cannot produce the current one cannot answer, so the failure mode
        the guard exists to prevent — answering a question you did not read — is
        structurally unavailable rather than merely discouraged.

        THE COST OF GETTING THIS WRONG IS WHY IT REFUSES RATHER THAN GUESSES: nexus sat
        8.16h and platform-api 6.64h on prompts one keystroke would have cleared, so the
        pressure to "just send 1" is real. Sending 1 into a prompt that changed is how a
        4-hour stall becomes an unintended approval.
        """
        # RE-VALIDATED here, not just in the sync check above. The gap between the
        # handler's check and this send is microseconds rather than the minutes the
        # fingerprint really guards, but a guard that is cheap to apply twice should be.
        ok, message = self.check_prompt_answer(name, option, fingerprint)
        if not ok:
            return message

        worker = self.require_worker(name)
        pilot = self._get_pilot()
        if pilot:
            pilot.wake_worker(name)
        # automated=False deliberately — see the docstring. This is a deliberate answer
        # to a NAMED prompt, not an automated write, and the fingerprint check is what
        # earns the distinction.
        # MOVE THE CURSOR, THEN ENTER — never type the digit.
        #
        # The first version sent `send_keys("1", enter=True)`, and the first live use
        # reported success while the picker stayed open. Typing a digit is the wrong
        # instrument twice over: a picker that does not consume number keys receives it
        # as FREE TEXT and the trailing Enter submits it — which is precisely the harm
        # #1451's guard exists to prevent, done deliberately by the tool meant to fix it.
        #
        # Arrows and Enter write no printable character, so the worst case is a cursor
        # that moved. This is also how a human answers the prompt, which is the standard
        # the rest of this path is held to.
        from swarm.pty.prompt_options import parse_open_prompt

        prompt = parse_open_prompt(worker.process.get_content(_PROMPT_ANSWER_SCAN_LINES))
        cursor_at = prompt.cursored.number if (prompt and prompt.cursored) else None
        if cursor_at is None:
            return (
                f"cannot answer {fingerprint}: no option is highlighted, so there is "
                f"nothing to move from. Read the prompt again or use queen_dismiss_prompt."
            )
        steps = option - cursor_at
        for _ in range(abs(steps)):
            if steps > 0:
                await worker.process.send_arrow_down(actor="queen-answer")
            else:
                await worker.process.send_arrow_up(actor="queen-answer")
            await asyncio.sleep(_ARROW_STEP_SECONDS)
        await worker.process.send_enter(actor="queen-answer")

        # READ BACK. #1608 was filed because `queen_prompt_worker` reported "sent" for a
        # message the guard was holding, and the caller could not tell. Reporting success
        # here on the strength of having WRITTEN to the PTY would be the same defect in
        # the tool built to fix it — which is exactly what the first live use hit.
        await asyncio.sleep(_ANSWER_SETTLE_SECONDS)
        still_open = self.check_prompt_answer(name, option, fingerprint)[0]
        verdict = "answered" if not still_open else "SENT BUT NOT CONFIRMED"
        self._drone_log.add(
            DroneAction.OPERATOR,
            name,
            f"{verdict} prompt {fingerprint}: {message}",
            category=LogCategory.OPERATOR,
        )
        if still_open:
            return (
                f"SENT BUT NOT CONFIRMED — wrote {message}, and "
                f"{_ANSWER_SETTLE_SECONDS:.0f}s later the prompt {fingerprint} is STILL "
                f"OPEN. The keystroke may not have been accepted. Re-read with "
                f"queen_view_worker_state before assuming it took; if the fingerprint is "
                f"unchanged, try queen_dismiss_prompt or ask the operator."
            )
        return f"answered {message} — confirmed, the prompt is gone"

    async def arrow_up_worker(self, name: str) -> None:
        """Send Up Arrow to a worker's process."""
        worker = self.require_worker(name)
        self._require_process(worker)
        pilot = self._get_pilot()
        if pilot:
            pilot.wake_worker(name)
        await worker.process.send_arrow_up(actor="operator-arrow")

    async def arrow_down_worker(self, name: str) -> None:
        """Send Down Arrow to a worker's process."""
        worker = self.require_worker(name)
        self._require_process(worker)
        pilot = self._get_pilot()
        if pilot:
            pilot.wake_worker(name)
        await worker.process.send_arrow_down(actor="operator-arrow")

    async def arrow_right_worker(self, name: str) -> None:
        """Send Right Arrow to a worker's process."""
        worker = self.require_worker(name)
        self._require_process(worker)
        pilot = self._get_pilot()
        if pilot:
            pilot.wake_worker(name)
        await worker.process.send_arrow_right(actor="operator-arrow")

    async def arrow_left_worker(self, name: str) -> None:
        """Send Left Arrow to a worker's process."""
        worker = self.require_worker(name)
        self._require_process(worker)
        pilot = self._get_pilot()
        if pilot:
            pilot.wake_worker(name)
        await worker.process.send_arrow_left(actor="operator-arrow")

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
                        # #1415: the config field is LABELLED "seconds idle before
                        # RESTING → SLEEPING" and, until this line, controlled nothing.
                        # Nothing assigned it onto a Worker, so display_state always
                        # compared against the 1200s dataclass default no matter what the
                        # operator set. The dataclass default remains the fallback for
                        # Workers built without a config (fixtures, tests).
                        #
                        # THIS IS NO LONGER DISPLAY-ONLY. #1538 keyed INV-2's task
                        # demotion off SLEEPING, which derives from this threshold, so
                        # the value now also decides how long a paused worker keeps its
                        # ACTIVE task. Set it low and tasks demote after a short pause.
                        sleeping_threshold=config.drones.sleeping_threshold,
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
        worker.state_known = True  # #1357: a deliberate set IS a measurement
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
            await proc.send_escape(actor="worker-service")
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
            worker.state_known = True  # #1357: a deliberate set IS a measurement
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
        worker.state_known = True  # #1357: a deliberate set IS a measurement
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
            targets,
            lambda w: w.process.send_enter(actor="operator-continue-all"),
            "all",
            "continued {count} worker(s)",
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
