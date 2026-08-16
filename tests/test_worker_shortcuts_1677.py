"""#1677 — Shift+Tab as an ACTION BUTTON, in the mechanism that already existed.

THE CORRECTION IS THE POINT OF THIS FILE. The first implementation read "this is a global
option in the shortcut bar set in the config like we already do" as "a new config list,
like the other config lists" and built a parallel `shortcuts:` section with its own
endpoint and its own rendering. The operator meant the ACTION BUTTONS list — the one that
already has Escape, Arrow Up, Arrow Down, Arrow Left and Arrow Right in its dropdown, all
of which are keystrokes written to a worker's PTY. "Like we already do" was a pointer at
an existing mechanism, not a description of a shape.

The visible symptom was the button rendering in the bottom status strip next to
`Alt+X Quit` and `? Help` instead of in the dashboard action bar beside Kill and Revive.

So Shift+Tab is now one more `action` value on the existing `ActionButtonConfig`, which
means it needs NO config plumbing at all: `action` is a free-form string, so the label,
order, style and mobile/desktop visibility all come from the machinery that already
shipped. That is why the config half of this file is short — the correct fix deleted more
than it added.

WHY SHIFT+TAB SPECIFICALLY: it is Claude Code's permission-mode cycle. #1647 measured
18 of 18 running workers in auto mode, where the drone escalate-guards abstain to a
classifier that does not implement them, and surfaced `permission_mode` on worker state so
the dashboard can SHOW the mode. This is the other half — being able to change it.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.config.models import ActionButtonConfig
from swarm.config.serialization import serialize_config
from swarm.pty.process import WorkerProcess
from swarm.server.worker_service import PromptOpenError, WorkerService

SHIFT_TAB = "\x1b[Z"
_SRC = Path(__file__).resolve().parent.parent / "src" / "swarm"

REAL_PICKER = """\
  Would you like to proceed?
❯ 1. Yes, and use auto mode
   2. Yes, manually approve edits
   3. Tell Claude what to change
"""


# ---------------------------------------------------------------------------
# AC1 — settable in config, with no new config plumbing
# ---------------------------------------------------------------------------


def test_a_shift_tab_button_round_trips_through_the_existing_action_button_config():
    """AC1. The operator adds it in the Action Buttons list on the config page. If this
    fails, the button vanishes on save — which is exactly what happened twice while the
    parallel `shortcuts:` section was being wired, because the config applier and the DB
    store each dropped an unknown section silently."""
    from swarm.config.models import HiveConfig

    cfg = HiveConfig(
        action_buttons=[ActionButtonConfig(label="Mode", action="shift_tab", style="secondary")]
    )

    data = serialize_config(cfg)

    assert {"label": "Mode", "action": "shift_tab"}.items() <= data["action_buttons"][0].items()


def test_the_action_value_needs_no_whitelist_entry():
    """WHY THE CORRECTED FIX IS SMALL. `ActionButtonConfig.action` is a free-form string,
    so a new action type costs zero config-layer changes. A whitelist appearing here later
    would silently drop `shift_tab` on save, so this test is the tripwire for that."""
    import dataclasses

    field = {f.name: f for f in dataclasses.fields(ActionButtonConfig)}["action"]

    assert field.type in ("str", str), "action must stay a plain string"


def test_the_operator_can_name_and_style_the_button_freely():
    """The label is the operator's, not ours — `Mode` in the screenshot. The action is
    what binds behaviour, so the two must be independent."""
    from swarm.config.models import HiveConfig

    cfg = HiveConfig(
        action_buttons=[
            ActionButtonConfig(
                label="Cycle mode", action="shift_tab", style="danger", show_mobile=False
            )
        ]
    )

    got = serialize_config(cfg)["action_buttons"][0]

    assert got["label"] == "Cycle mode"
    assert got["style"] == "danger"
    assert got["show_mobile"] is False


# ---------------------------------------------------------------------------
# The keystroke itself
# ---------------------------------------------------------------------------


def _proc_capturing():
    proc = WorkerProcess(name="swarm", cwd="/tmp")
    sent: list[dict] = []

    async def _send(cmd: dict) -> dict:
        sent.append(cmd)
        return {"ok": True}

    proc.bind_send_cmd(_send)
    return proc, sent


@pytest.mark.asyncio
async def test_send_shift_tab_writes_csi_z_and_nothing_else():
    """Shift+Tab is CSI Z (back-tab), 3 bytes. A wrong sequence would type visible junk
    into the worker's prompt rather than cycling the mode."""
    proc, sent = _proc_capturing()

    await proc.send_shift_tab(actor="operator-shortcut")

    import base64

    writes = [c for c in sent if c.get("cmd") == "write"]
    assert len(writes) == 1
    assert base64.b64decode(writes[0]["data"]) == b"\x1b[Z"


@pytest.mark.asyncio
async def test_the_keystroke_carries_its_actor():
    """AC3, and #1658's requirement: a bare control sequence arriving in
    ~/.swarm/pty-writes.jsonl with no name cannot be traced to whoever pressed the
    button."""
    proc, sent = _proc_capturing()

    await proc.send_shift_tab(actor="operator-shortcut")

    assert [c["actor"] for c in sent if c.get("cmd") == "write"] == ["operator-shortcut"]


@pytest.mark.asyncio
async def test_an_unlabelled_call_still_records_unknown():
    """#1675's rule holds for the new verb too: `unknown` MUST STAY REACHABLE. A confident
    wrong name in a forensic record is worse than an honest gap, because it cannot be
    questioned."""
    proc, sent = _proc_capturing()

    await proc.send_shift_tab()

    assert [c["actor"] for c in sent if c.get("cmd") == "write"] == ["unknown"]


# ---------------------------------------------------------------------------
# AC4 — the open-picker refusal, in the service so both routes inherit it
# ---------------------------------------------------------------------------


def _service(screen: str = "just working\n"):
    svc = WorkerService.__new__(WorkerService)
    worker = MagicMock()
    worker.name = "swarm"
    # SPEC'D against the real class on purpose. A bare MagicMock accepts any kwarg the
    # real object rejects — which is how the first version of this feature passed ten unit
    # tests and then raised `send_keys() got an unexpected keyword argument 'actor'`
    # against the live daemon.
    worker.process = MagicMock(spec=WorkerProcess)
    worker.process.get_content.return_value = screen
    worker.process.send_shift_tab = AsyncMock(spec=WorkerProcess.send_shift_tab)
    svc.require_worker = lambda _n: worker  # type: ignore[method-assign]
    svc._require_process = lambda _w: None  # type: ignore[method-assign]
    svc._get_pilot = lambda: None  # type: ignore[method-assign]
    svc._drone_log = MagicMock()
    return svc, worker


@pytest.mark.asyncio
async def test_an_open_picker_refuses_the_keystroke_and_writes_NOTHING():
    """AC4, and the load-bearing half. Asserted on the WRITE PATH, not just the raised
    type: a guard that raised and still wrote would pass an exception-only test while
    committing the operator's picker to an answer."""
    svc, worker = _service(screen=REAL_PICKER)

    with pytest.raises(PromptOpenError):
        await svc.shift_tab_worker("swarm")

    worker.process.send_shift_tab.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_quiet_worker_gets_the_keystroke():
    """POSITIVE CONTROL. A guard that refused unconditionally would pass the test above
    and make the button permanently inert — indistinguishable from working, which is the
    defect class this codebase kept hitting."""
    svc, worker = _service()

    await svc.shift_tab_worker("swarm")

    worker.process.send_shift_tab.assert_awaited_once()
    assert worker.process.send_shift_tab.await_args.kwargs["actor"] == "operator-shortcut"


@pytest.mark.asyncio
async def test_the_refusal_names_what_to_do_about_it():
    """A refusal that only says no leaves the operator stuck — the #1608 lesson."""
    svc, _ = _service(screen=REAL_PICKER)

    with pytest.raises(PromptOpenError) as exc:
        await svc.shift_tab_worker("swarm")

    assert "open selection prompt" in str(exc.value)
    assert "answer it" in str(exc.value) or "dismiss" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_an_unreadable_screen_does_not_block_the_keystroke():
    """`get_content` raising means "could not tell". Refusing on that would make the
    button fail closed on any transient read error, and the #1451 hold in `_write` is the
    real backstop underneath this advisory check."""
    svc, worker = _service()
    worker.process.get_content.side_effect = OSError("ring buffer gone")

    await svc.shift_tab_worker("swarm")

    worker.process.send_shift_tab.assert_awaited_once()


# ---------------------------------------------------------------------------
# The layers — a source sweep, because silent drops are how this ticket went wrong
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,needle,why",
    [
        (
            "web/templates/config.html",
            'value="shift_tab"',
            "the dropdown the operator picks it from",
        ),
        (
            "web/templates/config.html",
            "'shift_tab'",
            "the JS actions array for rows added after page load",
        ),
        (
            "web/static/dashboard.js",
            "action === 'shift_tab'",
            "doAction, or the button renders and does nothing",
        ),
        (
            "web/static/dashboard.js",
            "sendSpecialKey('shift-tab')",
            "the URL segment must match the route",
        ),
        (
            "web/routes/workers.py",
            '"/action/shift-tab/{name}"',
            "the endpoint the dashboard actually calls",
        ),
        ("server/daemon.py", "async def shift_tab_worker", "the daemon delegate"),
    ],
)
def test_every_layer_of_the_chain_is_present(path, needle, why):
    """THE LESSON OF THIS TICKET, as a test. The first implementation passed its whole
    unit suite while TWO layers — the config applier and the DB store — silently dropped
    the section, because each layer is explicit per-field and a missing row is not an
    error anywhere. An action button is a six-layer chain; a gap in any one of them
    renders a button that looks right and does nothing."""
    assert needle in (_SRC / path).read_text(), f"missing from {path}: {why}"


def test_the_button_is_not_also_wired_into_the_bottom_shortcut_bar():
    """THE REPORTED DEFECT, as a regression test. The parallel mechanism rendered into the
    status strip beside `Alt+X Quit`, which is where the operator found it and asked why.
    Two mechanisms for one capability is also how they drift apart."""
    leftovers = [
        f"{p.relative_to(_SRC.parent.parent)}"
        for p in _SRC.rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".js", ".html"}
        and re.search(r"fireShortcut|WorkerShortcut|config\.shortcuts", p.read_text())
    ]

    assert leftovers == [], f"parallel shortcut mechanism still referenced in: {leftovers}"
