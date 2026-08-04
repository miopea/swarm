"""Sleep straight from the worker's current state — no force-rest first.

The dashboard only offered ``Sleep`` on a RESTING worker, so parking a BUZZING
one meant two round trips through the context menu: *Force to rest*, then
right-click again and *Sleep*.

The reason the two-step existed at all is that ``sleep`` alone does not stick.
Sleeping is a *display* state derived from RESTING plus a backdated
``state_since``, and the state tracker re-reads the PTY on its next tick. If the
PTY still shows an active turn (BUZZING) or an approval prompt (WAITING), the
tracker re-detects that state and the worker pops straight back out of SLEEPING.
*Force to rest* was doing the load-bearing half of the work: it sends Escape,
which is what actually changes what the PTY shows.

So the fix is not "loosen the state check" — that would produce a menu item that
appears to work and silently undoes itself a few seconds later, which is worse
than requiring two clicks. ``sleep_worker`` has to do the Escape itself.

STUNG stays rejected. A STUNG worker's process is dead; rendering it as SLEEPING
would file a broken worker under a state that reads as "idle and fine".
"""

from __future__ import annotations

import pytest

from swarm.server.daemon import SwarmOperationError
from swarm.worker.worker import WorkerState
from tests.test_worker_service import daemon  # noqa: F401  (fixture)


@pytest.mark.asyncio
@pytest.mark.parametrize("start", [WorkerState.BUZZING, WorkerState.WAITING])
async def test_sleep_interrupts_so_it_actually_sticks(daemon, start):  # noqa: F811
    """AC-1. A busy/prompting worker sleeps in ONE step, and Escape is sent.

    Without the Escape this test would still pass on ``display_state`` and the
    feature would still be broken in the dashboard — the tracker would undo it
    on the next tick. Asserting the Escape is asserting the part that lasts.
    """
    svc = daemon.worker_svc
    worker = svc.get_worker("alice")
    worker.state = start

    await svc.sleep_worker("alice")

    assert worker.display_state == WorkerState.SLEEPING
    assert worker.process.keys_sent, "no Escape sent — the tracker will undo this on the next tick"


@pytest.mark.asyncio
async def test_sleep_from_resting_does_not_disturb_the_prompt(daemon):  # noqa: F811
    """AC-2. A RESTING worker is already at an idle prompt.

    Escape there is a keystroke into a session the operator may be mid-thought
    in, and it buys nothing — there is no turn to interrupt.
    """
    svc = daemon.worker_svc
    worker = svc.get_worker("alice")
    worker.state = WorkerState.RESTING

    await svc.sleep_worker("alice")

    assert worker.display_state == WorkerState.SLEEPING
    assert worker.process.keys_sent == [], "Escape sent to an already-idle worker"


@pytest.mark.asyncio
async def test_sleep_refuses_a_stung_worker(daemon):  # noqa: F811
    """AC-3. STUNG means the process exited. SLEEPING reads as 'idle and fine'."""
    svc = daemon.worker_svc
    worker = svc.get_worker("alice")
    worker.state = WorkerState.STUNG

    with pytest.raises(SwarmOperationError, match="STUNG"):
        await svc.sleep_worker("alice")

    assert worker.display_state == WorkerState.STUNG


@pytest.mark.asyncio
async def test_sleeping_again_is_a_no_op_not_an_error(daemon):  # noqa: F811
    """The menu offers Sleep off ``display_state``; a double-click must not 500."""
    svc = daemon.worker_svc
    worker = svc.get_worker("alice")
    worker.state = WorkerState.RESTING

    await svc.sleep_worker("alice")
    await svc.sleep_worker("alice")

    assert worker.display_state == WorkerState.SLEEPING
