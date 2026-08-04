"""Operator kill: the worker goes away and STAYS away.

THE BUG (measured, from this fleet's own buzz_log):

    2026-08-03 16:16:44 | rcg-dev-install | OPERATOR | killed
    2026-08-03 16:16:59 | rcg-dev-install | REVIVED  | worker exited

Fifteen seconds. Three independent same-worker instances in the log
(rcg-dev-install, sculpt-studio-codex, bfg-solutions).

``kill`` set ``state = STUNG`` and left the worker in the roster. The drone
decision rule (``drones/rules.py``) revives **any** STUNG worker — which is
correct and desirable for a crash, and exactly wrong for a deliberate kill.
The rule had no way to tell the two apart. With ``max_revive_attempts = 3``
the operator had to kill up to four times before ``revive_count`` exhausted
the budget and the worker finally stayed dead, and fewer than four if earlier
crashes had already spent some of it — which is why it presented as
intermittent rather than consistently broken.

THE FIX IS AN ORDERING, NOT A FLAG. The worker leaves the roster *before* any
shutdown step runs. The revive rule iterates the roster, so a worker that is
not in it cannot be revived — there is nothing to coordinate, no flag for a
future edit to forget to check, and no window between "process died" and
"removed" for a drone poll to land in. A flag would have left that window open
for the whole ~3s graceful-shutdown sequence.

Crash recovery is deliberately untouched: a worker that dies on its own is
still marked STUNG by the state tracker, is still in the roster, and is still
revived.
"""

from __future__ import annotations

import pytest

from swarm.worker.worker import WorkerState
from tests.test_worker_service import daemon  # noqa: F401  (fixture)

# --- the regression --------------------------------------------------------


@pytest.mark.asyncio
async def test_killed_worker_leaves_the_roster(daemon):  # noqa: F811
    """AC-1. Gone from the running list, per the operator decision."""
    svc = daemon.worker_svc
    assert svc.get_worker("alice") is not None

    await daemon.kill_worker("alice")

    assert svc.get_worker("alice") is None, "killed worker still in the roster"
    assert "alice" not in {w.name for w in daemon.workers}


@pytest.mark.asyncio
async def test_the_revive_rule_cannot_see_a_killed_worker(daemon):  # noqa: F811
    """AC-2 — THE regression guard for the 15-second revive.

    Asserted through the real decision rule rather than by checking a flag:
    what matters is that ``decide`` is never *reached* for this worker, and
    the roster is what feeds it.
    """
    from swarm.drones.rules import decide

    worker = daemon.worker_svc.get_worker("alice")
    await daemon.kill_worker("alice")

    # The rule still says REVIVE for a STUNG worker — that is the crash-
    # recovery feature and it is intentionally unchanged.
    worker.state = WorkerState.STUNG
    assert decide(worker, "", config=daemon.config.drones).decision.value == "revive"

    # ...but the drone only ever polls workers in the roster, and this one
    # is not in it, so that decision is never taken.
    assert worker not in daemon.workers


@pytest.mark.asyncio
async def test_a_crashed_worker_is_still_revivable(daemon):  # noqa: F811
    """AC-3. The fix must not disable crash recovery.

    A worker that dies on its own was never killed by the operator, stays in
    the roster, and the STUNG→REVIVE path still applies.
    """
    from swarm.drones.rules import decide

    worker = daemon.worker_svc.get_worker("alice")
    worker.state = WorkerState.STUNG  # died on its own; no kill() call

    assert worker in daemon.workers
    assert decide(worker, "", config=daemon.config.drones).decision.value == "revive"


# --- graceful shutdown -----------------------------------------------------


@pytest.mark.asyncio
async def test_kill_interrupts_then_quits_then_exits_the_shell(daemon):  # noqa: F811
    """AC-4. Esc → /quit → exit, in that order, before any signal.

    Order is the assertion: /quit before Esc would type the slash command into
    a busy prompt, and exit before /quit would drop the shell out from under an
    agent that had not saved its session.
    """
    worker = daemon.worker_svc.get_worker("alice")

    await daemon.kill_worker("alice")

    # "<Esc>" is how FakeWorkerProcess records send_escape(); the real process
    # writes 0x1b. Asserting the fake's marker keeps this about ORDER, which is
    # the part that matters, rather than about byte encoding.
    sent = "".join(worker.process.keys_sent)
    assert "<Esc>" in sent, "no Esc — a mid-turn worker is never interrupted"
    assert "/quit" in sent, "no /quit — the agent never gets to save its session"
    assert "exit" in sent, "shell left running"
    assert sent.index("<Esc>") < sent.index("/quit") < sent.index("exit")


@pytest.mark.asyncio
async def test_the_process_is_still_force_killed_as_a_backstop(daemon):  # noqa: F811
    """AC-5. Graceful is an attempt, not a guarantee.

    A wedged agent that ignores /quit must not survive the kill — otherwise
    "graceful" would be a regression against today's behaviour.
    """
    killed: list[str] = []

    class _Pool:
        async def kill(self, name):
            killed.append(name)

    daemon.pool = _Pool()
    await daemon.kill_worker("alice")

    assert killed == ["alice"], "process was not force-killed after the graceful attempt"


@pytest.mark.asyncio
async def test_a_provider_with_no_known_quit_command_still_kills(daemon):  # noqa: F811
    """The base provider returns "" rather than guessing a quit string.

    A guessed command would be typed into the prompt as literal text and left
    there. Providers without one skip that step and still shut down.
    """
    worker = daemon.worker_svc.get_worker("alice")
    from swarm.providers import get_provider

    provider = get_provider(worker.provider_name)
    original = type(provider).quit_command
    try:
        type(provider).quit_command = lambda self: ""
        await daemon.kill_worker("alice")
    finally:
        type(provider).quit_command = original

    sent = "".join(worker.process.keys_sent)
    assert "/quit" not in sent
    assert "exit" in sent, "shell still not closed when no quit command exists"
    assert daemon.worker_svc.get_worker("alice") is None


@pytest.mark.asyncio
async def test_killing_an_unknown_worker_raises(daemon):  # noqa: F811
    """The dashboard can double-fire; the second call must 404, not 500."""
    from swarm.server.daemon import WorkerNotFoundError

    await daemon.kill_worker("alice")
    with pytest.raises(WorkerNotFoundError):
        await daemon.kill_worker("alice")
